"""Inputs that used to crash or mislead: chunked uploads, model output of the
wrong type, timestamps in other ISO forms, and Markdown-breaking prose."""

from fastapi.testclient import TestClient

from app import app
from copilot import incidents
from copilot.core import Diagnosis
from copilot.investigator import Investigator
from copilot.report import _duration, fallback_narrative, render_postmortem
from copilot.research import count_private
from copilot.workspace import Evidence, Workspace, error_onset


def test_chunked_upload_over_the_limit_is_refused():
    def body():
        yield b'{"source":"bundle","bundle":{"x":"' + b"a" * 5_000_000 + b'"}}'

    with TestClient(app) as client:
        resp = client.post("/api/copilot/investigations", content=body(),
                           headers={"content-type": "application/json"})
    assert resp.status_code == 413


def test_model_values_of_the_wrong_type_are_dropped_not_fatal():
    investigator = Investigator(Workspace(incidents.from_seed(1, "easy")), llm=None)
    diagnosis = investigator._validated({
        "root_cause_ids": [["not", "a", "string"]],
        "chain": [{"service": ["a", "b"], "effect": "x"},
                  {"service": {"name": "a"}, "effect": "x"}],
        "ruled_out": [{"id": ["x"], "why": "list id"}],
    })
    assert diagnosis.chain == [] and diagnosis.ruled_out == []


def test_error_onset_reads_times_in_any_iso_form():
    scenario = {
        "incident_window": {"start": "2026-10-10T14:00:00Z", "end": "2026-10-10T14:20:00Z"},
        "logs": {
            "api": [{"timestamp": "2026-10-10 14:06:00", "level": "ERROR"}],
            "db": [{"timestamp": "2026-10-10T14:05:00+00:00", "level": "ERROR"},
                   {"timestamp": "2026-10-10T13:59:59Z", "level": "ERROR"}],
        },
    }
    onset = error_onset(scenario)
    assert [r["service"] for r in onset] == ["db", "api"]
    assert onset[0]["errors"] == 1  # the line before the window is not counted


def test_duration_tolerates_a_window_with_and_without_a_zone():
    assert _duration({"start": "2026-10-10T14:00:00Z", "end": "2026-10-10T14:20:00"}) == "20 min"


def test_postmortem_keeps_tables_and_code_blocks_intact():
    ws = Workspace(incidents.from_seed(1, "easy"))
    brief = ws.brief()
    diagnosis = Diagnosis(summary="s")
    narrative = fallback_narrative(brief, diagnosis)
    narrative["action_items"] = [{"action": "alert when p99 > 2s | queue > 80%",
                                  "priority": "P1", "why": "detect it"}]
    diff = "README.md\n+```bash\n+make dev\n+```"
    evidence = [Evidence(id="E1", tool="get_commit", args={"commit_hash": "c"}, result=diff)]

    doc = render_postmortem(brief, diagnosis, evidence, narrative)

    assert "p99 > 2s \\| queue > 80%" in doc
    assert "````\n" + diff + "\n````" in doc


def test_only_privacy_drops_are_reported():
    brief = Workspace(incidents.from_seed(1, "easy")).brief()
    service = next(iter(brief["service_graph"]))
    proposed = ["connection pool exhaustion mitigation"] * 2 + ["tiny", f"{service} outage"]
    assert count_private(proposed, brief) == 1
