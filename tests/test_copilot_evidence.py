"""The evidence tools: filters, limits and what they must never reveal."""

import json

import pytest

from copilot.workspace import MAX_LOG_RESULTS, Workspace
from data.incident_generator import generate_incident

WINDOW = {"start": "2026-10-01T10:00:00Z", "end": "2026-10-01T10:20:00Z"}


def _ws(logs, **extra):
    return Workspace({
        "service_graph": {"api": []}, "services": [{"name": "api"}],
        "incident_window": WINDOW, "logs": {"api": logs}, **extra})


def _line(minute, level, message, hour=10):
    return {"id": f"l{hour}{minute}{level}", "timestamp": f"2026-10-01T{hour:02d}:{minute:02d}:00Z",
            "level": level, "message": message}


LOGS = [
    _line(30, "WARN", "pool wait rising", hour=9),
    _line(59, "INFO", "request ok", hour=9),
    _line(1, "ERROR", "pool exhausted"),
    _line(2, "CRITICAL", "readiness failed"),
    _line(3, "INFO", "retrying"),
    _line(19, "ERROR", "still exhausted"),
    _line(40, "INFO", "recovered"),
]


def _messages(evidence):
    return [line.split(": ", 1)[1] for line in evidence.result.splitlines()[1:]]


@pytest.mark.parametrize("args, expected", [
    ({}, [entry["message"] for entry in LOGS]),
    ({"level": "ERROR"}, ["pool exhausted", "readiness failed", "still exhausted"]),
    ({"level": "warn"}, ["pool wait rising", "pool exhausted", "readiness failed", "still exhausted"]),
    ({"keyword": "EXHAUSTED"}, ["pool exhausted", "still exhausted"]),
    ({"keyword": "error"}, ["pool exhausted", "still exhausted"]),
    ({"time_window": "during_incident"},
     ["pool exhausted", "readiness failed", "retrying", "still exhausted"]),
    ({"time_window": "before_incident"}, ["pool wait rising", "request ok"]),
    ({"time_window": "first_5m"}, ["pool exhausted", "readiness failed", "retrying"]),
    ({"time_window": "last_2m"}, ["still exhausted"]),
    ({"time_window": "before_incident", "level": "WARN"}, ["pool wait rising"]),
])
def test_log_filters(args, expected):
    evidence = _ws(LOGS).call("search_logs", {"service": "api", **args})
    assert evidence.ok and _messages(evidence) == expected


def test_no_match_is_an_answer_not_an_error():
    evidence = _ws(LOGS).call("search_logs", {"service": "api", "keyword": "kernel panic"})
    assert evidence.ok and evidence.id == "E1" and "no matching entries" in evidence.result


@pytest.mark.parametrize("args, fragment", [
    ({"level": "LOUD"}, "Unknown level"),
    ({"time_window": "yesterday"}, "Unknown time_window"),
    ({"service": "nope"}, "Unknown service"),
])
def test_bad_filters_are_explained_and_cost_nothing(args, fragment):
    ws = _ws(LOGS)
    evidence = ws.call("search_logs", {"service": "api", **args})
    assert not evidence.ok and fragment in evidence.result and ws.lookups == 0


def test_long_results_are_capped_and_say_how_many_were_left_out():
    many = [_line(i % 20, "ERROR", f"failure {i:03d}") for i in range(MAX_LOG_RESULTS + 12)]
    evidence = _ws(many).call("search_logs", {"service": "api"})
    assert f"{len(many)} matching" in evidence.result
    assert "12 more matches not shown" in evidence.result
    assert len(evidence.result.splitlines()) == MAX_LOG_RESULTS + 2  # header + lines + note


def test_lines_without_a_usable_timestamp_are_never_hidden():
    logs = [{"id": "x", "timestamp": "not-a-time", "level": "ERROR", "message": "undated failure"}]
    evidence = _ws(logs).call("search_logs", {"service": "api", "time_window": "during_incident"})
    assert "undated failure" in evidence.result


def test_nothing_the_tools_return_carries_labels_or_the_answer():
    for seed in range(8):
        incident = generate_incident(seed, "hard")
        ws = Workspace(incident)
        seen = [json.dumps(ws.brief())]
        for service in incident["logs"]:
            seen.append(ws.call("search_logs", {"service": service}).result)
        for commit in incident["commits"]:
            seen.append(ws.call("get_commit", {"commit_hash": commit["hash"]}).result)
        for change in incident["config_changes"]:
            seen.append(ws.call("get_config", {"config_id": change["config_id"]}).result)
        for event in incident["infra_events"]:
            seen.append(ws.call("get_infra_event", {"event_id": event["event_id"]}).result)
        for trace in incident["traces"][:6]:
            seen.append(ws.call("get_trace", {"trace_id": trace["trace_id"]}).result)
        text = "\n".join(seen).lower()
        for forbidden in ('"relevant"', "relevant:", "ground_truth", "ground truth",
                          "failure_mode", incident["failure_mode"]):
            assert forbidden not in text, (seed, forbidden)


def test_budget_comes_from_the_incident_and_is_enforced():
    ws = _ws(LOGS, max_steps=2)
    assert ws.call("search_logs", {"service": "api"}).ok
    assert ws.call("search_logs", {"service": "api", "level": "ERROR"}).ok
    blocked = ws.call("search_logs", {"service": "api", "level": "WARN"})
    assert not blocked.ok and "budget" in blocked.result.lower()
    assert ws.calls_left == 0
