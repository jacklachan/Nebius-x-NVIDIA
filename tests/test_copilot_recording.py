"""A run saved by the CLI must replay through the web API."""

import json

from fastapi.testclient import TestClient

import copilot.__main__ as cli
import web.copilot_api as api
from app import app
from copilot import incidents
from copilot.llm import ChatResult, UsageMeter


class Model:
    def __init__(self, settings=None):
        self.meter = UsageMeter()
        self.settings = type("S", (), {"tavily_api_key": None})()

    async def chat(self, role, messages, max_tokens=1024, temperature=0.2):
        truth = incidents.from_seed(5, "easy")["ground_truth"]
        reply = {"thought": "stop", "tool": "done", "confidence": 0.9, "chain": [],
                 "root_cause_ids": truth["cause"].split("+"), "title": "Recorded run"}
        result = ChatResult(text=json.dumps(reply), role=role, model=f"fake-{role}",
                            input_tokens=1, output_tokens=1, cost_usd=0.001, latency_s=0.0)
        self.meter.add(result)
        return result


def test_cli_recording_replays_through_the_api(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "TokenFactoryClient", Model)
    path = tmp_path / "recordings" / "seed-5-easy.json"

    code = cli.main(["investigate", "--seed", "5", "--difficulty", "easy",
                     "--record", str(path), "--json"])
    assert code == 0 and path.exists()

    monkeypatch.setattr(api, "RECORDINGS_DIR", path.parent)
    store = api._Store()
    store.load_recordings()
    monkeypatch.setattr(api, "store", store)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)

    with TestClient(app) as client:
        listing = client.get("/api/copilot/investigations").json()
        assert [(r["id"], r["recorded"], r["cause_correct"]) for r in listing] == [
            ("rec-seed-5-easy", True, True)]
        with client.stream("GET", "/api/copilot/investigations/rec-seed-5-easy/events") as resp:
            kinds = [json.loads(line[6:])["type"] for line in resp.iter_lines()
                     if line.startswith("data: ")]
        assert kinds[0] == "brief" and kinds[-2:] == ["done", "_eof"]
        md = client.get("/api/copilot/investigations/rec-seed-5-easy/postmortem.md")
        assert md.text.startswith("# Recorded run")


def test_failed_run_is_not_recorded(tmp_path, monkeypatch):
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "rec.json"
    assert cli.main(["investigate", "--seed", "5", "--record", str(path)]) == 1
    assert not path.exists()
