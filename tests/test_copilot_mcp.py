"""MCP server, exercised through a real in-process MCP client session."""

import asyncio
import json
import os
import subprocess

import pytest
from mcp import Client

import copilot.mcp_server as mcp_server
from copilot import incidents
from copilot.llm import ChatResult, LLMError, UsageMeter

LOCAL_ONLY = {"build_incident_bundle", "investigate_bundle_file"}
START, END = "2026-10-01T10:00:00Z", "2026-10-01T10:20:00Z"


class Model:
    """Answers correctly for generated seed 5 (easy) and for bundles that
    contain commit-abc; ``broken`` simulates Token Factory being down."""

    def __init__(self, broken=False):
        self.meter, self.broken = UsageMeter(), broken

    async def chat(self, role, messages, max_tokens=1024, temperature=0.2):
        if self.broken:
            raise LLMError("Token Factory 503: overloaded", status=503)
        prompt = messages[-1]["content"]
        if "seed_5_easy" in prompt:
            causes = incidents.from_seed(5, "easy")["ground_truth"]["cause"].split("+")
        else:
            causes = [c for c in ("commit-abc",) if c in prompt]
        reply = {"thought": "stop", "tool": "done", "root_cause_ids": causes,
                 "summary": "The pool was shrunk.", "confidence": 0.8, "chain": [],
                 "open_questions": ["Why no alert?"], "title": "Pool shrink"}
        result = ChatResult(text=json.dumps(reply), role=role, model="fake", input_tokens=1,
                            output_tokens=1, cost_usd=0.001, latency_s=0.0)
        self.meter.add(result)
        return result


@pytest.fixture(autouse=True)
def scripted_model(monkeypatch):
    monkeypatch.setattr(mcp_server, "make_llm", Model)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


def _call(tool, args, local_files=True):
    async def go():
        async with Client(mcp_server.create_server(local_files)) as client:
            return await client.call_tool(tool, args)
    return asyncio.run(go())


def _tool_names(local_files):
    async def go():
        async with Client(mcp_server.create_server(local_files)) as client:
            return {t.name for t in (await client.list_tools()).tools}
    return asyncio.run(go())


BUNDLE = {
    "service_graph": {"web": ["db"], "db": []},
    "services": [{"name": "web"}, {"name": "db"}],
    "incident_window": {"start": START, "end": END},
    "logs": {"db": [{"id": "l1", "timestamp": "2026-10-01T10:01:00Z", "level": "ERROR",
                     "message": "pool exhausted"}], "web": []},
    "commits": [{"hash": "commit-abc", "service": "db", "timestamp": "2026-10-01T09:50:00Z",
                 "author": "dev@example.com", "message": "tune pool",
                 "diff": "-MAX = 100\n+MAX = 10"}],
}


def test_remote_server_does_not_expose_tools_that_read_local_paths():
    remote, local = _tool_names(local_files=False), _tool_names(local_files=True)
    assert LOCAL_ONLY <= local
    assert not (LOCAL_ONLY & remote)
    assert {"list_sample_incidents", "investigate_sample", "investigate_incident"} <= remote


def test_list_sample_incidents():
    result = _call("list_sample_incidents", {})
    items = result.structured_content["result"]
    assert not result.is_error
    assert any(i.get("task_id") == "task1_recent_deploy" for i in items)
    assert "ground_truth" not in json.dumps(items)


def test_sample_investigation_is_graded():
    result = _call("investigate_sample", {"seed": 5, "difficulty": "easy"})
    body = result.structured_content
    assert not result.is_error
    assert body["graded"]["root_cause_correct"] is True
    assert body["root_cause"] == incidents.from_seed(5, "easy")["ground_truth"]["cause"]
    assert body["postmortem_markdown"].startswith("# Pool shrink")
    assert body["cost_usd"] > 0


def test_real_bundle_is_investigated_but_never_graded():
    bundle = {**BUNDLE, "ground_truth": {"cause": "commit-abc", "chain": []}}
    body = _call("investigate_incident", {"bundle": bundle}, local_files=False).structured_content
    assert body["root_cause"] == "commit-abc"
    assert "graded" not in body
    assert body["open_questions"] == ["Why no alert?"]
    assert "commit-abc" in body["postmortem_markdown"]


@pytest.mark.parametrize("tool, args, fragment", [
    ("investigate_incident", {"bundle": {"logs": {}}}, "missing"),
    ("investigate_sample", {"task_id": "nope"}, "Unknown task"),
    ("investigate_sample", {"difficulty": "impossible"}, "Difficulty"),
    ("investigate_bundle_file", {"bundle_path": "does-not-exist.json"}, "Could not read"),
])
def test_bad_input_comes_back_as_a_tool_error_with_the_reason(tool, args, fragment):
    result = _call(tool, args)
    assert result.is_error
    assert fragment in result.content[0].text


def test_model_outage_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(mcp_server, "make_llm", lambda: Model(broken=True))
    result = _call("investigate_incident", {"bundle": BUNDLE})
    assert result.is_error
    assert "503" in result.content[0].text


def test_build_bundle_then_investigate_the_file(tmp_path):
    repo = tmp_path / "db"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="Dev", GIT_AUTHOR_EMAIL="dev@example.com",
               GIT_COMMITTER_NAME="Dev", GIT_COMMITTER_EMAIL="dev@example.com",
               GIT_AUTHOR_DATE="2026-10-01T09:50:00Z", GIT_COMMITTER_DATE="2026-10-01T09:50:00Z")
    (repo / "pool.py").write_text("MAX = 10\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "tune pool"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)
    out = tmp_path / "incident.json"

    built = _call("build_incident_bundle", {
        "start": START, "end": END, "out_path": str(out),
        "repos": {"db": str(repo)}, "services": {"web": ["db"]},
    })
    assert not built.is_error
    assert "2 services, 1 commits" in built.structured_content["contents"]
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["service_graph"] == {"db": [], "web": ["db"]}
    assert {s["name"] for s in saved["services"]} == {"db", "web"}

    investigated = _call("investigate_bundle_file", {"bundle_path": str(out)})
    assert not investigated.is_error
    assert "## Evidence" in investigated.structured_content["postmortem_markdown"]
