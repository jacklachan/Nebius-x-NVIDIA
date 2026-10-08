"""Nebius Token Factory client.

Token Factory speaks the OpenAI chat-completions protocol, so this is a thin
async wrapper that adds the three things the copilot needs: routing a call to
the model configured for a role, retrying transient failures, and accounting
for tokens and cost per role.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from copilot.config import Settings, cost_usd, load_settings

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
RETRY_DELAYS = (1.0, 3.0, 8.0)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_OPEN_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL)


def strip_reasoning(text: str) -> str:
    """Remove ``<think>`` blocks, including one cut off before it closed."""
    return _OPEN_THINK_RE.sub("", _THINK_RE.sub("", text or "")).strip()


class LLMError(Exception):
    """A Token Factory call failed in a way the caller should surface."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ChatResult:
    text: str
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_s: float
    truncated: bool = False     # the model hit max_tokens before finishing


@dataclass
class UsageMeter:
    """Running totals per role, so the UI can show where the money went."""

    by_role: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, result: ChatResult) -> None:
        row = self.by_role.setdefault(
            result.role,
            {"model": result.model, "calls": 0, "input_tokens": 0,
             "output_tokens": 0, "cost_usd": 0.0},
        )
        row["calls"] += 1
        row["input_tokens"] += result.input_tokens
        row["output_tokens"] += result.output_tokens
        row["cost_usd"] = round(row["cost_usd"] + result.cost_usd, 6)

    def total_cost(self) -> float:
        return round(sum(r["cost_usd"] for r in self.by_role.values()), 6)

    def snapshot(self) -> dict[str, Any]:
        return {
            "by_role": {k: dict(v) for k, v in self.by_role.items()},
            "total_cost_usd": self.total_cost(),
        }


class TokenFactoryClient:
    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
    ) -> None:
        self.settings = settings or load_settings()
        self.meter = UsageMeter()
        self._transport = transport
        self._retry_delays = retry_delays

    async def chat(
        self,
        role: str,
        messages: list[dict[str, str]],
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> ChatResult:
        """Run one chat completion on the model configured for ``role``."""
        if not self.settings.api_key:
            raise LLMError(
                "NEBIUS_API_KEY is not set. Create a key at "
                "tokenfactory.nebius.com and export it.",
                status=401,
            )
        model = self.settings.model_for(role)
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        started = time.monotonic()
        data = await self._post("/chat/completions", payload)
        latency = time.monotonic() - started

        choices = data.get("choices") or []
        if not choices or "message" not in choices[0]:
            raise LLMError(f"Token Factory returned no choices: {json.dumps(data)[:300]}")
        message = choices[0]["message"]
        text = strip_reasoning(message.get("content") or "")
        if not text:
            # Reasoning models sometimes spend the whole reply thinking and
            # leave the answer in the reasoning field. Better to try parsing
            # that than to treat the turn as empty.
            text = strip_reasoning(
                message.get("reasoning_content") or message.get("reasoning") or "")
        truncated = choices[0].get("finish_reason") == "length"

        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        result = ChatResult(
            text=text,
            role=role,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd(model, input_tokens, output_tokens),
            latency_s=round(latency, 3),
            truncated=truncated,
        )
        self.meter.add(result)
        return result

    async def list_models(self) -> list[str]:
        data = await self._request("GET", "/models")
        return [m.get("id", "") for m in data.get("data", [])]

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, payload)

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.settings.api_key}"}
        url = f"{self.settings.base_url}{path}"
        last_error: LLMError | None = None
        for attempt in range(len(self._retry_delays) + 1):
            if attempt:
                await asyncio.sleep(self._retry_delays[attempt - 1])
            try:
                async with httpx.AsyncClient(
                    timeout=180.0, transport=self._transport
                ) as client:
                    resp = await client.request(method, url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_error = LLMError(f"Token Factory request failed: {exc}")
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise LLMError(
                        f"Token Factory returned non-JSON: {resp.text[:200]}"
                    ) from exc
            last_error = LLMError(
                f"Token Factory {resp.status_code}: {resp.text[:300]}",
                status=resp.status_code,
            )
            if resp.status_code not in RETRY_STATUSES:
                break
        assert last_error is not None
        raise last_error


def extract_json(raw: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Tolerates code fences, ``<think>`` blocks and prose around the object.
    Raises ``ValueError`` when there is no parseable object.
    """
    s = strip_reasoning(raw)
    decoder = json.JSONDecoder()
    start = s.find("{")
    if start == -1:
        raise ValueError("no JSON object found")
    error: Exception | None = None
    # Prose before the answer may contain braces of its own; try each "{".
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(s[start:])
        except json.JSONDecodeError as exc:
            error = error or exc
        else:
            if isinstance(obj, dict):
                return obj
        start = s.find("{", start + 1)
    if error:
        raise ValueError(f"invalid JSON: {error}") from error
    raise ValueError("JSON value is not an object")
