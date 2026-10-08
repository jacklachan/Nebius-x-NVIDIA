"""No-model baselines and the saved benchmark summaries the UI shows."""

import json

import pytest
from fastapi.testclient import TestClient

import web.copilot_api as api
from app import app
from copilot.bench import BASELINES, baseline_row, parse_seeds, run_baseline


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_baselines_are_deterministic_free_and_far_from_perfect():
    seeds = list(range(40))
    for kind in BASELINES:
        first = run_baseline(seeds, "medium", kind)
        assert first == run_baseline(seeds, "medium", kind)
        assert first["summary"]["errors"] == 0
        assert first["summary"]["root_cause_accuracy"] < 0.7
        assert first["summary"]["mean_cost_usd"] == 0


def test_informed_shortcuts_beat_random_guessing():
    seeds = list(range(100))
    accuracy = {kind: run_baseline(seeds, "hard", kind)["summary"]["root_cause_accuracy"]
                for kind in BASELINES}
    assert accuracy["random"] < accuracy["nearest"] < accuracy["reddest"]


def test_unknown_baseline_is_rejected():
    with pytest.raises(ValueError):
        baseline_row(1, "easy", "psychic")


def test_benchmarks_endpoint_lists_summaries_without_rows(client, tmp_path, monkeypatch):
    good = {"label": "routed", "kind": "model", "difficulty": "medium",
            "models": {"triage": "nvidia/nano"}, "rows": [{"seed": 1}],
            "summary": {"incidents": 20, "errors": 0, "root_cause_accuracy": 0.8}}
    (tmp_path / "routed-medium.json").write_text(json.dumps(good), encoding="utf-8")
    (tmp_path / "junk.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(api, "BENCHMARKS_DIR", tmp_path)

    body = client.get("/api/copilot/benchmarks").json()

    assert len(body) == 1
    assert body[0]["summary"]["root_cause_accuracy"] == 0.8
    assert "rows" not in body[0]


def test_committed_baseline_numbers_reproduce_from_the_code(client):
    """Every baseline figure the UI shows must come out of a fresh run."""
    saved = [b for b in client.get("/api/copilot/benchmarks").json() if b["kind"] == "baseline"]
    assert {(b["label"], b["difficulty"]) for b in saved} == {
        (kind, level) for kind in BASELINES for level in ("easy", "medium", "hard")}
    for entry in saved:
        seeds = parse_seeds(f"0-{entry['summary']['incidents'] - 1}")
        fresh = run_baseline(seeds, entry["difficulty"], entry["label"])
        assert fresh["summary"] == entry["summary"], (entry["label"], entry["difficulty"])
