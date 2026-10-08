"""Scoring an investigation against a known answer.

Deterministic, no judge model. Five questions, each scored 0 to 1:

  root_cause     Did it name what actually started the outage?
  failure_path   Did it trace the failure through the right services, in order?
  failure_modes  Did it say correctly what went wrong at each hop?
  grounding      Is the answer backed by evidence it actually retrieved: did it
                 read the change it blames, and do the exhibits it cites for
                 each hop really bear on the incident?
  efficiency     How little of its lookup budget did a correct answer take?

``grounding`` is the one that separates an investigation from a lucky guess:
naming the right commit without ever opening it scores nothing there.
"""

from __future__ import annotations

from typing import Any

WEIGHTS = {
    "root_cause": 0.45,
    "failure_path": 0.20,
    "failure_modes": 0.15,
    "grounding": 0.10,
    "efficiency": 0.10,
}
FREE_LOOKUP_SHARE = 0.25   # using up to a quarter of the budget costs nothing


def _ids(cause: str) -> set[str]:
    return {part.strip().lower() for part in str(cause).split("+") if part.strip()}


def score_root_cause(submitted: str, truth: str) -> float:
    """1 for exactly the right set of causes. Half credit for an answer that
    overlaps it: one of two joint causes, or the right cause plus a wrong one."""
    got, want = _ids(submitted), _ids(truth)
    if not got or not want:
        return 0.0
    if got == want:
        return 1.0
    return 0.5 if got & want else 0.0


def _collapse(services: list[str]) -> list[str]:
    """Drop consecutive repeats: a service with two hops is one step on the path."""
    out: list[str] = []
    for service in services:
        if not out or out[-1] != service:
            out.append(service)
    return out


def _lcs(a: list[Any], b: list[Any]) -> int:
    table = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            table[i + 1][j + 1] = table[i][j] + 1 if x == y else max(table[i][j + 1], table[i + 1][j])
    return table[-1][-1]


def score_failure_path(chain: list[dict[str, Any]], truth: list[dict[str, Any]]) -> float:
    """Longest run of services in the right order, over the longer of the two paths."""
    got = _collapse([hop.get("service", "") for hop in chain])
    want = _collapse([hop.get("service", "") for hop in truth])
    if not got or not want:
        return 0.0
    return _lcs(got, want) / max(len(got), len(want))


def score_failure_modes(chain: list[dict[str, Any]], truth: list[dict[str, Any]]) -> float:
    """Right label on the right service, in order, over the longer chain."""
    got = [(hop.get("service", ""), hop.get("effect", "")) for hop in chain]
    want = [(hop.get("service", ""), hop.get("effect", "")) for hop in truth]
    if not got or not want:
        return 0.0
    return _lcs(got, want) / max(len(got), len(want))


def score_grounding(
    submitted: str,
    chain: list[dict[str, Any]],
    retrieved_entities: set[str],
    relevant_evidence: set[str],
) -> float:
    """Half for having retrieved every change it blames; half for the share
    of hops that cite at least one exhibit which revealed a relevant fact."""
    blamed = _ids(submitted)
    read_it = bool(blamed) and blamed <= {e.lower() for e in retrieved_entities}
    if chain:
        supported = sum(1 for hop in chain
                        if set(hop.get("evidence") or []) & relevant_evidence)
        cited = supported / len(chain)
    else:
        cited = 0.0
    return 0.5 * read_it + 0.5 * cited


def score_efficiency(lookups: int, budget: int, root_cause: float) -> float:
    """Full marks within the free share of the budget, falling to zero when
    the budget is spent. Scaled by root-cause credit: a fast wrong answer is
    not efficient."""
    if budget <= 0:
        return 0.0
    free = budget * FREE_LOOKUP_SHARE
    spent = max(0.0, lookups - free) / max(budget - free, 1e-9)
    return root_cause * max(0.0, 1.0 - spent)


def evaluate(
    submitted_cause: str,
    submitted_chain: list[dict[str, Any]],
    ground_truth: dict[str, Any],
    retrieved_entities: set[str],
    relevant_evidence: set[str],
    lookups: int,
    budget: int,
) -> dict[str, Any]:
    truth_cause, truth_chain = ground_truth["cause"], ground_truth.get("chain", [])
    root = score_root_cause(submitted_cause, truth_cause)
    raw = {
        "root_cause": root,
        "failure_path": score_failure_path(submitted_chain, truth_chain),
        "failure_modes": score_failure_modes(submitted_chain, truth_chain),
        "grounding": score_grounding(submitted_cause, submitted_chain,
                                     retrieved_entities, relevant_evidence),
        "efficiency": score_efficiency(lookups, budget, root),
    }
    rubrics = [{"rubric": name, "raw_score": round(raw[name], 4), "weight": weight,
                "weighted_score": round(raw[name] * weight, 4)}
               for name, weight in WEIGHTS.items()]
    return {
        "score": round(sum(r["weighted_score"] for r in rubrics), 4),
        "rubrics": rubrics,
        "cause_correct": root == 1.0,
        "ground_truth_cause": truth_cause,
        "ground_truth_chain": truth_chain,
        "evidence_calls": lookups,
    }
