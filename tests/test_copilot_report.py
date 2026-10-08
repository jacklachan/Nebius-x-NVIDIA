"""Postmortem document: assembled from facts, with model-written prose."""

import asyncio
import json

from copilot.config import ROLE_WRITER
from copilot.core import Diagnosis
from copilot.investigator import Investigator
from copilot.llm import ChatResult, LLMError, UsageMeter
from copilot.report import fallback_narrative, render_postmortem, write_narrative
from copilot.workspace import Workspace
from data.seed_generator import generate_scenario


class Writer:
    def __init__(self, reply=None, fail=False):
        self.reply, self.fail, self.meter, self.prompts = reply, fail, UsageMeter(), []

    async def chat(self, role, messages, max_tokens=1024, temperature=0.2):
        if self.fail and role == ROLE_WRITER:
            raise LLMError("Token Factory 503: overloaded", status=503)
        self.prompts.append((role, messages[-1]["content"]))
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        result = ChatResult(text=text, role=role, model=f"fake-{role}", input_tokens=50,
                            output_tokens=40, cost_usd=0.001, latency_s=0.0)
        self.meter.add(result)
        return result


NARRATIVE = {
    "title": "Connection pool shrink took down checkout",
    "summary": "A deploy cut the pool size and requests queued until callers timed out.",
    "impact": "Checkout failed for most users for the length of the incident.",
    "action_items": [
        {"action": "Alert on pool saturation above 80%", "priority": "P1", "why": "detects it sooner"},
        {"action": "Require load test for pool changes", "priority": "P0", "why": "prevents the cause"},
        {"action": "Tidy the runbook", "priority": "urgent", "why": "clarity"},
        {"priority": "P0", "why": "no action text"},
    ],
    "lessons": ["Capacity settings are code and need the same review."],
}


def _investigated():
    scenario = generate_scenario(7, "easy")
    ws = Workspace(scenario)
    cause = scenario["ground_truth"]["cause"].split("+")[0]
    tool, key = ("get_config", "config_id") if cause.startswith("cfg") else ("get_commit", "commit_hash")
    ws.call(tool, {key: cause})
    ws.call("search_logs", {"service": "nope", "keyword": "x"})  # rejected, must not appear
    truth = scenario["ground_truth"]
    diagnosis = Diagnosis(
        root_cause_ids=[cause],
        summary="The change leaked memory until the service stalled.",
        confidence=0.85,
        chain=[{**hop, "because": "Seen in the diff.", "evidence": ["E1"]} for hop in truth["chain"]],
        ruled_out=[{"id": scenario["commits"][-1]["hash"], "why": "Landed after the incident."}],
        open_questions=["Why did the alert fire late?"],
    )
    return scenario, ws, diagnosis


def test_writer_gets_brief_diagnosis_and_ledger_and_reply_is_cleaned():
    _, ws, diagnosis = _investigated()
    writer = Writer(NARRATIVE)

    narrative = asyncio.run(write_narrative(writer, ws.brief(), diagnosis, ws.evidence))

    role, prompt = writer.prompts[0]
    assert role == ROLE_WRITER
    assert diagnosis.root_cause_ids[0] in prompt and "[E1]" in prompt
    assert "ground_truth" not in prompt
    assert [a["priority"] for a in narrative["action_items"]] == ["P1", "P0", "P1"]
    assert narrative["title"] == NARRATIVE["title"]


def test_unparseable_writer_reply_falls_back_to_the_diagnosis():
    _, ws, diagnosis = _investigated()

    narrative = asyncio.run(write_narrative(Writer("sorry, no"), ws.brief(), diagnosis, ws.evidence))

    assert narrative["summary"] == diagnosis.summary
    assert narrative["action_items"] == []
    assert ws.brief()["incident_id"] in narrative["title"]


def test_document_states_the_facts_the_investigation_established():
    scenario, ws, diagnosis = _investigated()
    narrative = asyncio.run(write_narrative(Writer(NARRATIVE), ws.brief(), diagnosis, ws.evidence))
    usage = {"by_role": {"triage": {"model": "nano", "calls": 2, "input_tokens": 900,
                                    "output_tokens": 80, "cost_usd": 0.0001}},
             "total_cost_usd": 0.0001}

    doc = render_postmortem(ws.brief(), diagnosis, ws.evidence, narrative, usage)

    cause = diagnosis.root_cause_ids[0]
    assert doc.startswith("# Connection pool shrink took down checkout\n")
    assert f"`{cause}`" in doc.split("## Root cause")[1].split("##")[0]
    spread = doc.split("## How it spread")[1].split("##")[0]
    for hop in scenario["ground_truth"]["chain"]:
        assert hop["service"] in spread
    assert "[E1]" in spread
    assert doc.index("| P0 |") < doc.index("| P1 |")
    assert "Landed after the incident." in doc
    assert "Why did the alert fire late?" in doc
    assert "**[E1]**" in doc and "**[E2]**" not in doc
    assert "Unknown service" not in doc
    assert "Total: $0.0001, 1 evidence lookups." in doc
    assert "ground truth" not in doc.lower()


def test_document_is_honest_when_no_cause_was_found():
    _, ws, _ = _investigated()
    empty = Diagnosis(open_questions=["Nothing conclusive in the logs."])

    doc = render_postmortem(ws.brief(), empty, ws.evidence, fallback_narrative(ws.brief(), empty))

    assert "Root cause not established" in doc
    assert "not enough to name a cause" in doc
    assert "## How it spread" not in doc
    assert "Nothing conclusive in the logs." in doc


def _run(investigator):
    async def collect():
        return [event async for event in investigator.run()]
    return asyncio.run(collect())


def test_investigation_ends_with_a_report_event():
    scenario = generate_scenario(7, "easy")
    reply = {"thought": "stop", "tool": "done",
             "root_cause_ids": scenario["ground_truth"]["cause"].split("+"),
             "confidence": 0.9, "chain": [], **NARRATIVE}
    investigator = Investigator(Workspace(scenario), Writer(reply))

    events = _run(investigator)

    kinds = [e["type"] for e in events]
    assert kinds.index("grade") < kinds.index("report") < kinds.index("done")
    assert investigator.report.startswith("# Connection pool shrink")
    assert "| writer | fake-writer | 1 |" in investigator.report


def test_writer_outage_still_delivers_a_report():
    scenario = generate_scenario(7, "easy")
    reply = {"thought": "stop", "tool": "done",
             "root_cause_ids": scenario["ground_truth"]["cause"].split("+"),
             "summary": "It leaked.", "confidence": 0.9, "chain": []}
    investigator = Investigator(Workspace(scenario), Writer(reply, fail=True))

    events = _run(investigator)

    assert [e for e in events if e["type"] == "warning"]
    assert events[-1]["type"] == "done"
    assert "It leaked." in investigator.report
