"""Workspace: the blind evidence layer over PostmortemEnvironment."""

import json

from copilot.workspace import Workspace
from data.generator import load_scenario
from data.seed_generator import generate_scenario


def _scenario(seed=7, difficulty="easy"):
    return generate_scenario(seed, difficulty)


def test_brief_never_exposes_ground_truth_or_relevance_labels():
    scenario = _scenario()
    ws = Workspace(scenario)
    text = json.dumps(ws.brief())

    assert "ground_truth" not in text
    assert '"relevant"' not in text
    assert "diff" not in ws.brief()["commits"][0]


def test_tool_results_become_numbered_evidence():
    scenario = _scenario()
    ws = Workspace(scenario)
    service = next(iter(scenario["logs"]))
    commit = scenario["commits"][0]["hash"]

    first = ws.call("search_logs", {"service": service, "keyword": "error"})
    second = ws.call("get_commit", {"commit_hash": commit})

    assert (first.id, second.id) == ("E1", "E2")
    assert first.ok and second.ok
    assert commit in second.result


def test_evidence_never_carries_oracle_hints():
    scenario = _scenario()
    ws = Workspace(scenario)
    for commit in scenario["commits"]:
        ev = ws.call("get_commit", {"commit_hash": commit["hash"]})
        assert "relevant" not in ev.result.lower()
        assert "ground truth" not in ev.result.lower()


def test_repeated_call_is_rejected_without_spending_budget():
    scenario = _scenario()
    ws = Workspace(scenario)
    args = {"commit_hash": scenario["commits"][0]["hash"]}

    ws.call("get_commit", args)
    left = ws.calls_left
    again = ws.call("get_commit", dict(args))

    assert not again.ok
    assert "E1" in again.result
    assert ws.calls_left == left


def test_bad_input_is_rejected_without_touching_the_environment():
    ws = Workspace(_scenario())
    left = ws.calls_left

    for tool, args in [
        ("search_logs", {"service": "nope", "keyword": "x"}),
        ("get_commit", {"commit_hash": "commit-missing"}),
        ("get_trace", {}),
        ("hypothesize", {"cause_entity_id": "commit-x"}),
    ]:
        ev = ws.call(tool, args)
        assert not ev.ok and ev.id == ""

    assert ws.calls_left == left


def test_budget_stops_evidence_gathering():
    scenario = _scenario()
    ws = Workspace(scenario)
    ws.max_calls = 1

    assert ws.call("get_commit", {"commit_hash": scenario["commits"][0]["hash"]}).ok
    blocked = ws.call("get_commit", {"commit_hash": scenario["commits"][1]["hash"]})

    assert not blocked.ok
    assert "budget" in blocked.result.lower()


def test_grade_rewards_the_correct_diagnosis():
    scenario = _scenario()
    truth = scenario["ground_truth"]

    right = Workspace(scenario)
    right.call("get_commit", {"commit_hash": scenario["commits"][0]["hash"]})
    good = right.grade(truth["cause"], truth["chain"])

    wrong = Workspace(scenario)
    wrong.call("get_commit", {"commit_hash": scenario["commits"][0]["hash"]})
    bad = wrong.grade("commit-not-it", [])

    assert good["cause_correct"] and not bad["cause_correct"]
    assert good["score"] > bad["score"]
    assert {r["rubric"] for r in good["rubrics"]}


def test_incident_without_ground_truth_is_investigable_but_not_graded():
    scenario = load_scenario("task1_recent_deploy")
    scenario.pop("ground_truth", None)
    ws = Workspace(scenario)

    assert not ws.has_ground_truth
    assert ws.call("search_logs", {"service": "data", "keyword": "error"}).ok
    assert ws.grade("commit-a1b2c3", []) is None


def test_candidate_ids_cover_commits_configs_and_infra():
    scenario = _scenario()
    ids = Workspace(scenario).candidate_ids()

    assert scenario["commits"][0]["hash"] in ids
    assert scenario["config_changes"][0]["config_id"] in ids
    assert scenario["infra_events"][0]["event_id"] in ids


def test_error_onset_orders_services_by_first_error_during_the_incident():
    from data.incident_generator import generate_incident

    for seed in range(15):
        incident = generate_incident(seed, "medium")
        onset = Workspace(incident).brief()["error_onset"]
        window = incident["incident_window"]

        assert onset == sorted(onset, key=lambda r: (r["first_error"], r["service"]))
        assert all(window["start"] <= row["first_error"] <= window["end"] for row in onset)
        failing = {hop["service"] for hop in incident["ground_truth"]["chain"]}
        healthy = {s["name"] for s in incident["services"] if s["status"] == "healthy"}
        assert {row["service"] for row in onset} <= failing
        assert not {row["service"] for row in onset} & healthy


def test_error_onset_is_empty_without_errors_and_carries_no_labels():
    scenario = {
        "service_graph": {"web": []}, "services": [{"name": "web"}],
        "incident_window": {"start": "2026-10-01T10:00:00Z", "end": "2026-10-01T10:20:00Z"},
        "logs": {"web": [
            {"id": "a", "timestamp": "2026-10-01T09:00:00Z", "level": "ERROR", "message": "old"},
            {"id": "b", "timestamp": "2026-10-01T10:05:00Z", "level": "INFO", "message": "fine"}]},
        "commits": [], "config_changes": [], "infra_events": [], "traces": [],
        "task_difficulty": "unknown",
    }
    brief = Workspace(scenario).brief()
    assert brief["error_onset"] == []
    assert "relevant" not in json.dumps(brief)
