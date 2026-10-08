"""Tolerance for the ways real models stray from the requested format."""

import asyncio
import json

import httpx
import pytest

from copilot.config import DEFAULT_MODELS, ROLE_REASON, ROLE_TRIAGE, ROLE_WRITER, Settings
from copilot.investigator import EFFECT_TAXONOMY, Investigator, snap_effect
from copilot.llm import ChatResult, TokenFactoryClient, UsageMeter, extract_json, strip_reasoning
from copilot.workspace import Workspace, normalise_call
from data.incident_generator import generate_incident


def _client(message, finish_reason="stop"):
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    settings = Settings(api_key="k", base_url="https://tf.test/v1",
                        models=dict(DEFAULT_MODELS), tavily_api_key=None)
    return TokenFactoryClient(settings, transport=httpx.MockTransport(handler))


def test_answer_left_in_the_reasoning_field_is_used():
    client = _client({"content": "", "reasoning_content": 'I should stop. {"tool": "done"}'})
    result = asyncio.run(client.chat(ROLE_TRIAGE, []))
    assert extract_json(result.text) == {"tool": "done"}


def test_content_wins_over_reasoning_when_present():
    client = _client({"content": '{"tool": "get_trace"}', "reasoning_content": '{"tool": "done"}'})
    assert asyncio.run(client.chat(ROLE_TRIAGE, [])).text == '{"tool": "get_trace"}'


def test_truncation_is_reported():
    assert asyncio.run(_client({"content": "<think>still going"}, "length").chat(ROLE_TRIAGE, [])).truncated
    assert not asyncio.run(_client({"content": "{}"}).chat(ROLE_TRIAGE, [])).truncated


def test_unclosed_think_block_is_removed():
    assert strip_reasoning('{"a": 1}<think>cut off mid') == '{"a": 1}'
    assert strip_reasoning("<think>never closed") == ""


@pytest.mark.parametrize("raw", [
    'Looking at {service} logs first. {"tool": "done"}',
    'Step {1}: decide. Answer: {"tool": "done"} and that is all.',
    '[1, 2] then {"tool": "done"}',
])
def test_json_is_found_after_prose_that_contains_braces(raw):
    assert extract_json(raw) == {"tool": "done"}


@pytest.mark.parametrize("tool, args, expected", [
    ("diff_commit", {"hash": "commit-1"}, ("get_commit", {"commit_hash": "commit-1"})),
    ("get_commit", {"id": "commit-1"}, ("get_commit", {"commit_hash": "commit-1"})),
    ("get_commit", {"anything": "commit-1"}, ("get_commit", {"commit_hash": "commit-1"})),
    ("fetch_trace", {"trace": "t-1"}, ("get_trace", {"trace_id": "t-1"})),
    ("inspect_infra", {"id": "infra-1"}, ("get_infra_event", {"event_id": "infra-1"})),
    ("query_logs", {"service": "db", "query": "error", "window": "during_incident"},
     ("search_logs", {"service": "db", "keyword": "error", "time_window": "during_incident"})),
    ("get_commit", {"commit_hash": "commit-1", "id": "x"},
     ("get_commit", {"commit_hash": "commit-1", "id": "x"})),
    ("hypothesize", {"cause_entity_id": "c"}, ("hypothesize", {"cause_entity_id": "c"})),
])
def test_common_misnamings_are_accepted(tool, args, expected):
    assert normalise_call(tool, args) == expected


def test_misnamed_call_reaches_the_evidence_and_dedupes_with_the_proper_name():
    incident = generate_incident(3, "easy")
    ws = Workspace(incident)
    commit = incident["commits"][0]["hash"]

    first = ws.call("diff_commit", {"hash": commit})
    again = ws.call("get_commit", {"commit_hash": commit})

    assert first.ok and first.id == "E1" and first.tool == "get_commit"
    assert not again.ok and "E1" in again.result


def test_oracle_actions_are_still_refused_under_any_name():
    ws = Workspace(generate_incident(3, "easy"))
    for tool in ("hypothesize", "explain_chain", "submit", "discover_topology"):
        assert not ws.call(tool, {"cause_entity_id": "commit-x"}).ok


@pytest.mark.parametrize("written, expected", [
    ("Connection pool exhaustion", "connection_pool_exhaustion"),
    ("upstream-timeout", "upstream_timeout"),
    ("5xx errors to users", "5xx_errors_to_users"),
    (" OOM_crash_loop ", "oom_crash_loop"),
    ("something novel", "something novel"),
])
def test_effect_labels_snap_to_the_taxonomy_only_when_they_are_the_same_words(written, expected):
    assert snap_effect(written) == expected
    assert expected in EFFECT_TAXONOMY or expected == written


class Scripted:
    def __init__(self, triage, reason):
        self.queues = {ROLE_TRIAGE: list(triage), ROLE_REASON: list(reason), ROLE_WRITER: []}
        self.prompts, self.meter = [], UsageMeter()

    async def chat(self, role, messages, max_tokens=1024, temperature=0.2):
        self.prompts.append(messages[-1]["content"])
        item = self.queues[role].pop(0) if self.queues[role] else {"tool": "done", "thought": ""}
        text, truncated = item if isinstance(item, tuple) else (item, False)
        text = text if isinstance(text, str) else json.dumps(text)
        result = ChatResult(text=text, role=role, model="fake", input_tokens=1, output_tokens=1,
                            cost_usd=0.0, latency_s=0.0, truncated=truncated)
        self.meter.add(result)
        return result


def test_sloppy_but_right_investigation_scores_as_right():
    incident = generate_incident(4, "easy")
    truth = incident["ground_truth"]
    cause = truth["cause"]
    lookup = ({"thought": "read it", "tool": "inspect_config", "args": {"id": cause}}
              if cause.startswith("cfg") else
              {"thought": "read it", "tool": "diff_commit", "args": {"hash": cause}})
    model = Scripted(
        triage=[("", True), lookup, {"thought": "enough", "tool": "done"}],
        reason=[{"root_cause_ids": cause, "confidence": "0.9",
                 "chain": [{"service": hop["service"],
                            "effect": hop["effect"].replace("_", " ").title(),
                            "because": "seen", "evidence": ["E1"]} for hop in truth["chain"]]}],
    )
    investigator = Investigator(Workspace(incident), model)

    async def collect():
        return [e async for e in investigator.run()]
    events = asyncio.run(collect())

    assert "cut off" in model.prompts[1]
    grade = next(e for e in events if e["type"] == "grade")
    assert grade["cause_correct"]
    chain = next(r for r in grade["rubrics"] if r["rubric"] == "chain_accuracy")
    assert chain["raw_score"] == pytest.approx(1.0)
