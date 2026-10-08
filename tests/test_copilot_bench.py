"""Benchmark runner: scoring, aggregation and seed parsing."""

import asyncio
import json

import pytest

from copilot.bench import parse_seeds, run_benchmark, summarize
from copilot.config import ROLE_WRITER
from copilot.llm import ChatResult, LLMError, UsageMeter
from data.incident_generator import generate_incident as generate_scenario


class AnswerKey:
    """Diagnoses correctly for the seeds in ``knows``; names nothing otherwise."""

    calls: list[str] = []

    def __init__(self, knows, difficulty, broken=()):
        self.knows, self.difficulty, self.broken = set(knows), difficulty, set(broken)
        self.meter = UsageMeter()

    async def chat(self, role, messages, max_tokens=1024, temperature=0.2):
        AnswerKey.calls.append(role)
        prompt = messages[-1]["content"]
        seed = int(prompt.split('"incident_id":"seed_')[1].split("_")[0])
        if seed in self.broken:
            raise LLMError("Token Factory 500: boom", status=500)
        truth = generate_scenario(seed, self.difficulty)["ground_truth"]
        reply = {"thought": "stop", "tool": "done", "confidence": 0.8,
                 "root_cause_ids": truth["cause"].split("+") if seed in self.knows else [],
                 "chain": truth["chain"] if seed in self.knows else []}
        result = ChatResult(text=json.dumps(reply), role=role, model="fake", input_tokens=1,
                            output_tokens=1, cost_usd=0.002, latency_s=0.0)
        self.meter.add(result)
        return result


def test_benchmark_scores_each_seed_and_aggregates():
    AnswerKey.calls = []
    seen = []
    result = asyncio.run(run_benchmark(
        [3, 1, 2, 4], "easy",
        lambda: AnswerKey(knows={1, 2, 3}, difficulty="easy"),
        concurrency=2, on_row=seen.append))

    assert [r["seed"] for r in result["rows"]] == [1, 2, 3, 4]
    assert [r["cause_correct"] for r in result["rows"]] == [True, True, True, False]
    assert len(seen) == 4
    summary = result["summary"]
    assert summary["incidents"] == 4 and summary["errors"] == 0
    assert summary["root_cause_accuracy"] == 0.75
    assert summary["mean_cost_usd"] == pytest.approx(0.004)  # one triage + one diagnosis call
    assert result["rows"][0]["score"] > result["rows"][3]["score"]
    assert ROLE_WRITER not in AnswerKey.calls  # benchmark mode skips the prose


def test_failed_incidents_are_counted_not_averaged():
    result = asyncio.run(run_benchmark(
        [1, 2], "easy", lambda: AnswerKey(knows={1, 2}, difficulty="easy", broken={2})))

    assert result["summary"]["errors"] == 1
    assert result["summary"]["root_cause_accuracy"] == 1.0
    assert "500" in result["rows"][1]["error"]


def test_summary_of_all_failures_has_no_averages():
    assert summarize([{"error": "x"}]) == {"incidents": 1, "errors": 1}


@pytest.mark.parametrize("spec, expected", [
    ("0-3", [0, 1, 2, 3]), ("7", [7]), ("3, 1,1", [1, 3]), ("0-2,42", [0, 1, 2, 42]),
])
def test_parse_seeds(spec, expected):
    assert parse_seeds(spec) == expected


@pytest.mark.parametrize("spec", ["", "a-b", "1,x"])
def test_parse_seeds_rejects_garbage(spec):
    with pytest.raises(ValueError):
        parse_seeds(spec)
