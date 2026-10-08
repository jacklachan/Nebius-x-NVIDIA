"""Benchmark the investigator on generated incidents.

Each seed is a fresh incident with a known answer. The investigator works
blind and the deterministic grader scores it, so the numbers here measure
the models and prompts, not a judge model's opinion.
"""

from __future__ import annotations

import asyncio
import random
import time
from statistics import mean
from typing import Any, Callable

from copilot import incidents
from copilot.core import ChatModel
from copilot.investigator import Investigator
from copilot.oracle import oracle_row
from copilot.workspace import Workspace


async def run_one(seed: int, difficulty: str, llm: ChatModel) -> dict[str, Any]:
    """Investigate one generated incident and return its scored row."""
    workspace = Workspace(incidents.from_seed(seed, difficulty))
    investigator = Investigator(workspace, llm, write_report=False)
    started = time.monotonic()
    row: dict[str, Any] = {"seed": seed, "difficulty": difficulty, "error": None}
    async for event in investigator.run():
        if event["type"] == "error":
            row["error"] = event["message"]
        elif event["type"] == "done":
            grade = event["grade"] or {}
            row.update(
                cause=event["diagnosis"]["cause"],
                truth=grade.get("ground_truth_cause"),
                cause_correct=bool(grade.get("cause_correct")),
                score=grade.get("score"),
                confidence=event["diagnosis"]["confidence"],
                lookups=len(event["evidence"]),
                cost_usd=event["usage"]["total_cost_usd"],
            )
    row["seconds"] = round(time.monotonic() - started, 2)
    return row


BASELINES = {
    "random": "Pick any change at random",
    "nearest": "Blame the most recent change before the incident",
    "reddest": "Blame the most recent change on the service with the highest error rate",
}


def baseline_row(seed: int, difficulty: str, kind: str) -> dict[str, Any]:
    """Score a no-model heuristic on one incident. These are the shortcuts a
    hurried human reaches for; the investigator has to beat them to matter."""
    workspace = Workspace(incidents.from_seed(seed, difficulty))
    brief = workspace.brief()
    start = brief["incident_window"]["start"]
    changes = [(c["timestamp"], c["hash"], c["service"]) for c in brief["commits"]]
    changes += [(c["timestamp"], c["config_id"], c["service"]) for c in brief["config_changes"]]
    before = sorted(c for c in changes if c[0] < start) or sorted(changes)

    if kind == "random":
        cause = random.Random(f"baseline:{seed}:{difficulty}").choice(workspace.candidate_ids())
    elif kind == "nearest":
        cause = before[-1][1]
    elif kind == "reddest":
        reddest = max(brief["services"],
                      key=lambda s: s.get("error_rate_during_incident") or 0)["name"]
        on_service = [c for c in before if c[2] == reddest]
        cause = (on_service or before)[-1][1]
    else:
        raise ValueError(f"Unknown baseline {kind!r}. Choose from {', '.join(BASELINES)}.")

    grade = workspace.grade(cause, []) or {}
    return {
        "seed": seed, "difficulty": difficulty, "error": None, "cause": cause,
        "truth": grade.get("ground_truth_cause"),
        "cause_correct": bool(grade.get("cause_correct")), "score": grade.get("score"),
        "confidence": None, "lookups": 0, "cost_usd": 0.0, "seconds": 0.0,
    }


ORACLE = "oracle"
ORACLE_ABOUT = ("Given the answer, but still has to retrieve the evidence for every step "
                "through the same tools. The ceiling.")


def run_baseline(seeds: list[int], difficulty: str, kind: str) -> dict[str, Any]:
    if kind == ORACLE:
        rows = [oracle_row(seed, difficulty) for seed in seeds]
    else:
        rows = [baseline_row(seed, difficulty, kind) for seed in seeds]
    return {"difficulty": difficulty, "summary": summarize(rows), "rows": rows}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    finished = [r for r in rows if not r["error"]]
    summary: dict[str, Any] = {
        "incidents": len(rows),
        "errors": len(rows) - len(finished),
    }
    if finished:
        summary.update(
            root_cause_accuracy=round(mean(r["cause_correct"] for r in finished), 4),
            mean_score=round(mean(r["score"] for r in finished), 4),
            mean_lookups=round(mean(r["lookups"] for r in finished), 2),
            mean_cost_usd=round(mean(r["cost_usd"] for r in finished), 6),
            mean_seconds=round(mean(r["seconds"] for r in finished), 2),
        )
    return summary


async def run_benchmark(
    seeds: list[int],
    difficulty: str,
    make_llm: Callable[[], ChatModel],
    concurrency: int = 2,
    on_row: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run every seed with a fresh model client (so cost is metered per
    incident) and return rows plus a summary."""
    gate = asyncio.Semaphore(max(1, concurrency))

    async def guarded(seed: int) -> dict[str, Any]:
        async with gate:
            row = await run_one(seed, difficulty, make_llm())
            if on_row:
                on_row(row)
            return row

    rows = list(await asyncio.gather(*(guarded(seed) for seed in seeds)))
    rows.sort(key=lambda r: r["seed"])
    return {"difficulty": difficulty, "summary": summarize(rows), "rows": rows}


def parse_seeds(spec: str) -> list[int]:
    """'0-19' or '3,7,42' or a mix: '0-4,42'."""
    seeds: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            low, high = part.split("-", 1)
            seeds.extend(range(int(low), int(high) + 1))
        elif part:
            seeds.append(int(part))
    if not seeds:
        raise ValueError("no seeds given")
    return sorted(set(seeds))
