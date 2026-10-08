"""Second-generation incident generator: coherent, and not self-announcing."""

import json
import re

import pytest

from copilot.workspace import Workspace
from data.incident_generator import DIFFICULTY, MODES, generate_incident
from data.seed_generator import FAILURE_TEMPLATES

CASES = [(seed, difficulty) for difficulty in DIFFICULTY for seed in range(40)]
GIVEAWAYS = re.compile(
    r"warning|may cause|root cause|culprit|under (high )?load|starvation|optimi[sz]ation'",
    re.IGNORECASE)


def _entities(incident):
    return ({c["hash"]: c for c in incident["commits"]}
            | {c["config_id"]: c for c in incident["config_changes"]}
            | {e["event_id"]: e for e in incident["infra_events"]})


@pytest.mark.parametrize("seed, difficulty", CASES)
def test_incident_is_coherent(seed, difficulty):
    incident = generate_incident(seed, difficulty)
    graph, truth = incident["service_graph"], incident["ground_truth"]
    entities = _entities(incident)
    start = incident["incident_window"]["start"]

    # Deterministic.
    assert incident == generate_incident(seed, difficulty)

    # The answer names real changes that landed before the outage.
    causes = truth["cause"].split("+")
    assert causes and all(c in entities for c in causes)
    assert all(entities[c]["timestamp"] < start for c in causes)
    assert [entities[c]["relevant"] for c in causes] == [True] * len(causes)

    # The chain uses the shared vocabulary and climbs real dependency edges.
    template = FAILURE_TEMPLATES[incident["failure_mode"]]["chain_template"]
    assert [hop["effect"] for hop in truth["chain"]] == [s["effect"] for s in template]
    hops = [hop["service"] for hop in truth["chain"]]
    for earlier, later in zip(hops, hops[1:]):
        assert earlier == later or earlier in graph[later], (earlier, later)
    distinct = list(dict.fromkeys(hops))
    wanted = len({s["service"] for s in template})
    assert len(distinct) == wanted

    # Services off the failure path are healthy; the ones on it are not.
    by_name = {s["name"]: s for s in incident["services"]}
    for name, row in by_name.items():
        assert (row["status"] != "healthy") == (name in distinct)

    # Every relevant fact exists.
    known = set(entities) | {t["trace_id"] for t in incident["traces"]} | {
        e["id"] for entries in incident["logs"].values() for e in entries}
    assert set(incident["relevant_fact_ids"]) <= known

    # Unique IDs.
    for ids in ([c["hash"] for c in incident["commits"]],
                [e["id"] for entries in incident["logs"].values() for e in entries]):
        assert len(ids) == len(set(ids))


@pytest.mark.parametrize("seed, difficulty", CASES)
def test_nothing_announces_the_answer(seed, difficulty):
    incident = generate_incident(seed, difficulty)
    causes = incident["ground_truth"]["cause"].split("+")

    # Symptom-only brief.
    brief = incident["task_description"]
    assert not any(c in brief for c in causes)
    assert not re.search(r"commit|config|deploy|leak|pool|memory|failover", brief, re.I)

    # No change describes itself as dangerous.
    for commit in incident["commits"]:
        assert not GIVEAWAYS.search(commit["message"] + commit["diff"]), commit["hash"]
    for change in incident["config_changes"]:
        assert not GIVEAWAYS.search(change["description"])

    # Every commit has a real diff, so "the one with a diff" is not a tell.
    assert all(c["diff"].startswith("--- ") and len(c["diff"]) > 80 for c in incident["commits"])


def test_timing_alone_does_not_identify_the_culprit():
    """Across seeds, the change nearest the incident is usually not the culprit."""
    nearest_is_culprit = 0
    total = 0
    for seed in range(60):
        incident = generate_incident(seed, "medium")
        changes = sorted(
            [(c["timestamp"], c["relevant"]) for c in incident["commits"]]
            + [(c["timestamp"], c["relevant"]) for c in incident["config_changes"]])
        before = [c for c in changes if c[0] < incident["incident_window"]["start"]]
        total += 1
        nearest_is_culprit += before[-1][1]
    assert nearest_is_culprit / total < 0.5


def test_every_failure_mode_and_variant_is_reachable():
    seen_modes, seen_text = set(), ""
    for difficulty in DIFFICULTY:
        for seed in range(80):
            incident = generate_incident(seed, difficulty)
            seen_modes.add(incident["failure_mode"])
            seen_text += json.dumps(incident["commits"]) + json.dumps(incident["config_changes"])
    assert seen_modes == set(MODES)
    for mode in MODES.values():
        for variant in mode["culprits"]:
            marker = variant.get("key") or variant["message"].split("{")[0]
            assert marker in seen_text, marker


@pytest.mark.parametrize("difficulty", list(DIFFICULTY))
def test_evidence_for_the_answer_is_discoverable_through_the_workspace(difficulty):
    for seed in range(12):
        incident = generate_incident(seed, difficulty)
        ws = Workspace(incident)
        origin = incident["ground_truth"]["chain"][0]["service"]

        errors = ws.call("search_logs", {"service": origin, "keyword": "",
                                          "time_window": "during_incident"})
        assert errors.ok and "No log entries" not in errors.result

        failing = next(t for t in incident["traces"] if t["relevant"])
        trace = ws.call("get_trace", {"trace_id": failing["trace_id"]})
        assert "ERROR" in trace.result

        grade = ws.grade(incident["ground_truth"]["cause"], incident["ground_truth"]["chain"])
        assert grade["cause_correct"] and grade["score"] > 0.8


def test_callers_log_errors_that_name_their_dependency():
    incident = generate_incident(3, "medium")
    hops = list(dict.fromkeys(h["service"] for h in incident["ground_truth"]["chain"]))
    for dependency, caller in zip(hops, hops[1:]):
        messages = " ".join(e["message"] for e in incident["logs"][caller] if e["relevant"])
        assert dependency in messages or "5xx" in messages


def test_seeds_differ():
    a, b = generate_incident(1, "medium"), generate_incident(2, "medium")
    assert a["ground_truth"]["cause"] != b["ground_truth"]["cause"]
    assert a["service_graph"] != b["service_graph"]


@pytest.mark.parametrize("difficulty", list(DIFFICULTY))
def test_errors_start_at_the_origin_and_spread_outward(difficulty):
    """First-error order across services must follow the causal chain."""
    for seed in range(40):
        incident = generate_incident(seed, difficulty)
        window = incident["incident_window"]
        first_error = {}
        for service, entries in incident["logs"].items():
            loud = sorted(e["timestamp"] for e in entries
                          if e["level"] in ("ERROR", "CRITICAL")
                          and window["start"] <= e["timestamp"] <= window["end"])
            if loud:
                first_error[service] = loud[0]
        chain = list(dict.fromkeys(h["service"] for h in incident["ground_truth"]["chain"]))
        erroring = [s for s in chain if s in first_error]
        assert len(erroring) >= len(chain) - 1, (seed, incident["failure_mode"])
        times = [first_error[s] for s in erroring]
        assert times == sorted(times), (seed, incident["failure_mode"], erroring, times)
        assert set(first_error) <= set(chain)
