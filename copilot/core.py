"""Shared vocabulary of the copilot: the model interface, the diagnosis
record, and how evidence is rendered into prompts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from copilot.llm import ChatResult, UsageMeter
from copilot.workspace import Evidence

RESULT_CHARS = 1800       # per-evidence text shown to the models

_CAUSE_ORDER = {"commit": 0, "infra": 1, "cfg": 2}


class ChatModel(Protocol):
    meter: UsageMeter

    async def chat(
        self, role: str, messages: list[dict[str, str]],
        max_tokens: int = ..., temperature: float = ...,
    ) -> ChatResult: ...


@dataclass
class Diagnosis:
    root_cause_ids: list[str] = field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0
    chain: list[dict[str, Any]] = field(default_factory=list)
    ruled_out: list[dict[str, str]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)

    @property
    def cause(self) -> str:
        """Root cause in the grader's format: IDs joined by '+', commits first."""
        ordered = sorted(
            self.root_cause_ids,
            key=lambda i: (_CAUSE_ORDER.get(i.split("-")[0], 9), i),
        )
        return "+".join(ordered)

    def graded_chain(self) -> list[dict[str, str]]:
        return [{"service": s["service"], "effect": s["effect"]} for s in self.chain]

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "cause": self.cause}


def clip(text: str, limit: int = RESULT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def ledger_text(evidence: list[Evidence]) -> str:
    kept = [e for e in evidence if e.ok]
    if not kept:
        return "(no evidence gathered yet)"
    return "\n\n".join(
        f"[{e.id}] {e.tool}({json.dumps(e.args, separators=(',', ':'))})\n{clip(e.result)}"
        for e in kept
    )


def brief_text(brief: dict[str, Any]) -> str:
    return json.dumps(brief, separators=(",", ":"))
