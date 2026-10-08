"""The oracle: the ceiling, and the check that incidents are solvable."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import web.copilot_api as api
from app import app
from copilot import incidents
from copilot.bench import ORACLE, run_baseline
from copilot.oracle import Oracle, OracleError
from data.generator import get_available_tasks
from data.incident_generator import DIFFICULTY, generate_incident


@pytest.mark.parametrize("difficulty", list(DIFFICULTY))
def test_every_generated_incident_is_fully_solvable_from_its_telemetry(difficulty):
    """If this fails, the generator produced an incident whose evidence does
    not support its own answer, and no investigator could score full marks."""
    for seed in range(60):
        oracle = Oracle(generate_incident(seed, difficulty))
        oracle.investigate()
        assert oracle.unsupported == [], (seed, oracle.unsupported)
        assert oracle.grade["score"] == 1.0, (seed, oracle.grade["rubrics"])


@pytest.mark.parametrize("task_id", get_available_tasks())
def test_hand_written_incidents_are_solvable_too(task_id):
    oracle = Oracle(incidents.from_task(task_id))
    oracle.investigate()
    assert oracle.unsupported == [] and oracle.grade["score"] == 1.0


def test_oracle_does_the_work_through_the_same_tools():
    incident = generate_incident(11, "hard")      # a failover: two joint causes
    oracle = Oracle(incident)
    exhibits = oracle.investigate()
    causes = incident["ground_truth"]["cause"].split("+")

    assert len(causes) == 2
    opened = {str(v) for e in exhibits for v in e.args.values()}
    assert set(causes) <= opened                              # it read what it blames
    assert {e.tool for e in exhibits} <= {"search_logs", "get_trace", "get_commit",
                                          "get_config", "get_infra_event"}
    assert all(hop["evidence"] for hop in oracle.diagnosis.chain)
    cited = {c for hop in oracle.diagnosis.chain for c in hop["evidence"]}
    assert cited <= {e.id for e in exhibits}
    assert len(exhibits) <= 8                                 # no dumping the whole incident


def test_oracle_is_frugal_enough_to_keep_full_efficiency():
    for difficulty in DIFFICULTY:
        for seed in range(30):
            oracle = Oracle(generate_incident(seed, difficulty))
            oracle.investigate()
            assert oracle.ws.lookups <= oracle.ws.max_calls * 0.25


def test_oracle_refuses_an_incident_without_a_known_answer():
    incident = generate_incident(1, "easy")
    del incident["ground_truth"]
    with pytest.raises(OracleError):
        Oracle(incident)


def test_oracle_exposes_an_incident_that_cannot_support_its_answer():
    """Strip a caller's logs: the oracle must report the hop it cannot back
    with evidence from that service."""
    incident = generate_incident(1, "easy")
    last = incident["ground_truth"]["chain"][-1]
    incident["logs"][last["service"]] = []
    oracle = Oracle(incident)
    oracle.investigate()
    assert oracle.unsupported == [last]
    assert oracle.grade["cause_correct"]


def test_oracle_event_stream_ends_with_a_document():
    oracle = Oracle(generate_incident(3, "medium"))

    async def collect():
        return [event async for event in oracle.run()]
    events = asyncio.run(collect())

    kinds = [e["type"] for e in events]
    assert kinds[0] == "brief" and kinds[-1] == "done"
    assert kinds.index("diagnosis") < kinds.index("grade") < kinds.index("report")
    assert oracle.report.startswith("# Reference answer: seed_3_medium")
    assert "## Evidence" in oracle.report
    assert events[-1]["usage"]["total_cost_usd"] == 0.0


def test_oracle_benchmark_is_the_ceiling():
    result = run_baseline(list(range(25)), "medium", ORACLE)
    summary = result["summary"]
    assert summary["root_cause_accuracy"] == 1 and summary["mean_score"] == 1.0
    assert 4 <= summary["mean_lookups"] <= 8
    assert all(row["unsupported_hops"] == 0 for row in result["rows"])


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)     # no key anywhere
    monkeypatch.setattr(api, "store", api._Store())
    monkeypatch.setattr(api, "ORACLE_PACE_S", 0.0)
    with TestClient(app) as c:
        yield c


def test_reference_run_works_with_no_key_and_is_labelled(client):
    started = client.post("/api/copilot/investigations",
                          json={"source": "seed", "seed": 7, "difficulty": "medium",
                                "investigator": "oracle"})
    assert started.status_code == 200
    inv_id = started.json()["id"]

    with client.stream("GET", f"/api/copilot/investigations/{inv_id}/events") as resp:
        events = [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]
    assert events[-1]["type"] == "_eof"
    diagnosis = next(e for e in events if e["type"] == "diagnosis")
    assert "oracle" in diagnosis["model"]

    record = client.get(f"/api/copilot/investigations/{inv_id}").json()
    assert record["reference"] is True and record["score"] == 1.0 and record["cost_usd"] == 0.0
    assert record["report"].startswith("# Reference answer")

    # A normal run is still refused without a key.
    assert client.post("/api/copilot/investigations", json={"source": "seed"}).status_code == 503


def test_reference_run_is_refused_for_uploaded_bundles(client):
    resp = client.post("/api/copilot/investigations",
                       json={"source": "bundle", "bundle": {}, "investigator": "oracle"})
    assert resp.status_code == 400 and "known answer" in resp.json()["detail"]
