"""Token Factory client: routing, retries, accounting and JSON extraction."""

import asyncio
import json

import httpx
import pytest

from copilot.config import (
    DEFAULT_MODELS,
    ROLE_REASON,
    ROLE_TRIAGE,
    Settings,
    cost_usd,
)
from copilot.llm import LLMError, TokenFactoryClient, extract_json


def _settings(api_key="test-key"):
    return Settings(
        api_key=api_key,
        base_url="https://tf.test/v1",
        models=dict(DEFAULT_MODELS),
        tavily_api_key=None,
    )


def _completion(text, prompt_tokens=100, completion_tokens=20):
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def _client(handler, **kwargs):
    return TokenFactoryClient(
        settings=_settings(**kwargs),
        transport=httpx.MockTransport(handler),
        retry_delays=(0.0, 0.0),
    )


def test_chat_routes_role_to_model_and_sends_bearer_key():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion("hello"))

    result = asyncio.run(
        _client(handler).chat(ROLE_REASON, [{"role": "user", "content": "hi"}])
    )

    assert seen["url"] == "https://tf.test/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == DEFAULT_MODELS[ROLE_REASON]
    assert result.text == "hello"
    assert result.model == DEFAULT_MODELS[ROLE_REASON]


def test_chat_strips_think_blocks():
    def handler(request):
        return httpx.Response(200, json=_completion('<think>hmm</think>{"a": 1}'))

    result = asyncio.run(_client(handler).chat(ROLE_TRIAGE, []))
    assert result.text == '{"a": 1}'


def test_chat_retries_transient_errors_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json=_completion("ok"))

    result = asyncio.run(_client(handler).chat(ROLE_TRIAGE, []))
    assert result.text == "ok"
    assert len(calls) == 3


def test_chat_does_not_retry_auth_errors():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, text="bad key")

    with pytest.raises(LLMError) as exc:
        asyncio.run(_client(handler).chat(ROLE_TRIAGE, []))
    assert exc.value.status == 401
    assert len(calls) == 1


def test_chat_without_key_fails_before_any_request():
    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("no request expected")

    with pytest.raises(LLMError):
        asyncio.run(_client(handler, api_key=None).chat(ROLE_TRIAGE, []))


def test_meter_accumulates_tokens_and_cost_per_role():
    def handler(request):
        return httpx.Response(200, json=_completion("x", 1_000_000, 1_000_000))

    client = _client(handler)
    asyncio.run(client.chat(ROLE_TRIAGE, []))
    asyncio.run(client.chat(ROLE_TRIAGE, []))
    asyncio.run(client.chat(ROLE_REASON, []))

    snap = client.meter.snapshot()
    assert snap["by_role"][ROLE_TRIAGE]["calls"] == 2
    assert snap["by_role"][ROLE_TRIAGE]["cost_usd"] == pytest.approx(0.60)
    assert snap["by_role"][ROLE_REASON]["cost_usd"] == pytest.approx(4.00)
    assert snap["total_cost_usd"] == pytest.approx(4.60)


def test_cost_is_zero_for_unknown_model():
    assert cost_usd("someone/else", 1000, 1000) == 0.0


@pytest.mark.parametrize(
    "raw",
    [
        '{"tool": "done"}',
        '```json\n{"tool": "done"}\n```',
        'Sure, here it is: {"tool": "done"} hope that helps',
        '<think>{"tool": "wrong"}</think>{"tool": "done"}',
    ],
)
def test_extract_json_tolerates_wrapping(raw):
    assert extract_json(raw) == {"tool": "done"}


@pytest.mark.parametrize("raw", ["", "no json here", "[1, 2]", '{"broken": '])
def test_extract_json_rejects_non_objects(raw):
    with pytest.raises(ValueError):
        extract_json(raw)
