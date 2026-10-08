"""Copilot HTTP API, driven end to end with a scripted model."""

import json

import pytest
from fastapi.testclient import TestClient

import web.copilot_api as api
from app import app
from copilot.llm import ChatResult, UsageMeter
from data.seed_generator import generate_scenario


class Model:
    def __init__(self, reply):
        self.reply, self.meter = reply, UsageMeter()

    async def chat(self, role, messages, max_tokens=1024, temperature=0.2):
        result = ChatResult(text=json.dumps(self.reply), role=role, model=f"fake-{role}",
                            input_tokens=10, output_tokens=5, cost_usd=0.0005, latency_s=0.0)
        self.meter.add(result)
        return result


def _reply(scenario):
    return {
        "thought": "stop", "tool": "done",
        "root_cause_ids": scenario["ground_truth"]["cause"].split("+"),
        "summary": "It leaked.", "confidence": 0.9, "chain": [],
        "title": "Leak stalled the worker", "action_items": [],
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(api, "make_llm", lambda: Model(_reply(generate_scenario(42, "easy"))))
    monkeypatch.setattr(api, "store", api._Store())
    with TestClient(app) as c:
        yield c


def _events(client, investigation_id):
    out = []
    with client.stream("GET", f"/api/copilot/investigations/{investigation_id}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def test_status_reports_configuration(client):
    body = client.get("/api/copilot/status").json()
    assert body["ready"] is True and body["research"] is False
    assert set(body["models"]) == {"triage", "reason", "writer"}


def test_incident_list_offers_tasks_and_generated_seeds(client):
    items = client.get("/api/copilot/incidents").json()
    assert {i["source"] for i in items} == {"task", "seed"}
    assert all(i["description"] for i in items)
    assert "ground_truth" not in json.dumps(items)


def test_investigation_streams_to_completion_and_can_be_replayed(client):
    started = client.post("/api/copilot/investigations",
                          json={"source": "seed", "seed": 42, "difficulty": "easy"})
    assert started.status_code == 200
    inv_id = started.json()["id"]

    events = _events(client, inv_id)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "brief" and kinds[-2:] == ["done", "_eof"]
    assert "report" in kinds

    assert [e["type"] for e in _events(client, inv_id)] == kinds  # late subscriber

    record = client.get(f"/api/copilot/investigations/{inv_id}").json()
    assert record["status"] == "done"
    assert record["cause_correct"] is True
    assert record["cost_usd"] > 0
    assert record["report"].startswith("# Leak stalled the worker")

    listing = client.get("/api/copilot/investigations").json()
    assert listing[0]["id"] == inv_id and "events" not in listing[0]

    md = client.get(f"/api/copilot/investigations/{inv_id}/postmortem.md")
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert "attachment" in md.headers["content-disposition"]


def test_uploaded_bundle_is_never_graded_even_if_it_carries_an_answer(client):
    bundle = generate_scenario(42, "easy")
    inv_id = client.post("/api/copilot/investigations",
                         json={"source": "bundle", "bundle": bundle}).json()["id"]

    events = _events(client, inv_id)

    assert events[0]["graded"] is False
    assert "grade" not in [e["type"] for e in events]
    assert client.get(f"/api/copilot/investigations/{inv_id}").json()["score"] is None


@pytest.mark.parametrize("body, fragment", [
    ({"source": "task", "task_id": "nope"}, "Unknown task"),
    ({"source": "seed", "seed": 1, "difficulty": "impossible"}, "Difficulty"),
    ({"source": "bundle"}, "needs a bundle"),
    ({"source": "bundle", "bundle": {"logs": {}}}, "missing"),
])
def test_bad_requests_are_rejected_with_a_reason(client, body, fragment):
    resp = client.post("/api/copilot/investigations", json=body)
    assert resp.status_code == 400
    assert fragment in resp.json()["detail"]


def test_start_without_server_key_is_refused(client, monkeypatch):
    monkeypatch.delenv("NEBIUS_API_KEY")
    resp = client.post("/api/copilot/investigations", json={"source": "seed"})
    assert resp.status_code == 503
    assert "NEBIUS_API_KEY" in resp.json()["detail"]
    assert client.get("/api/copilot/status").json()["ready"] is False


def test_daily_limit_protects_the_server_key(client, monkeypatch):
    monkeypatch.setattr(api, "MAX_PER_DAY", 1)
    first = client.post("/api/copilot/investigations", json={"source": "seed"})
    _events(client, first.json()["id"])
    second = client.post("/api/copilot/investigations", json={"source": "seed"})
    assert first.status_code == 200 and second.status_code == 429


def test_unknown_investigation_is_404(client):
    assert client.get("/api/copilot/investigations/missing").status_code == 404
    assert client.get("/api/copilot/investigations/missing/events").status_code == 404


def test_recordings_load_from_disk_and_are_marked(tmp_path, monkeypatch):
    record = {"id": "rec1", "title": "Recorded", "source": "seed", "status": "done",
              "started_at": 1.0, "ended_at": 2.0, "report": "# R",
              "events": [{"type": "done", "diagnosis": {"cause": "commit-x"},
                          "grade": None, "usage": {"total_cost_usd": 0.01}}]}
    (tmp_path / "rec1.json").write_text(json.dumps(record), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(api, "RECORDINGS_DIR", tmp_path)

    fresh = api._Store()
    fresh.load_recordings()

    assert list(fresh.items) == ["rec1"]
    assert fresh.items["rec1"].summary()["recorded"] is True
    assert fresh.items["rec1"].summary()["cause"] == "commit-x"
