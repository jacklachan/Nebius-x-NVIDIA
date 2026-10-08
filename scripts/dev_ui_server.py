"""Run the web app with a scripted stand-in for the Nemotron models.

For front-end work only: it lets you watch a full investigation stream
through the UI with no API key and no spend. The stand-in reads the answer
from the scenario, so its results say nothing about real model quality, and
its model names are labelled "scripted" wherever the UI shows them.

    python scripts/dev_ui_server.py            # http://localhost:7860
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("NEBIUS_API_KEY", "scripted-dev-key")

import uvicorn  # noqa: E402

import web.copilot_api as api  # noqa: E402
from copilot import incidents  # noqa: E402
from copilot.config import ROLE_REASON, ROLE_TRIAGE  # noqa: E402
from copilot.llm import ChatResult, UsageMeter  # noqa: E402

DELAY_S = float(os.environ.get("DEV_DELAY_S", "0.7"))


def _scenario(prompt: str) -> dict | None:
    match = re.search(r'"incident_id":"([^"]+)"', prompt)
    if not match:
        return None
    incident_id = match.group(1)
    seed = re.fullmatch(r"seed_(\d+)_(\w+)", incident_id)
    try:
        if seed:
            return incidents.from_seed(int(seed.group(1)), seed.group(2))
        return incidents.from_task(incident_id)
    except incidents.IncidentError:
        return None


class ScriptedModel:
    def __init__(self) -> None:
        self.meter = UsageMeter()
        self.step = 0

    async def chat(self, role, messages, max_tokens=1024, temperature=0.2):
        await asyncio.sleep(DELAY_S)
        prompt = messages[-1]["content"]
        scenario = _scenario(prompt)
        reply = self._reply(role, scenario)
        result = ChatResult(
            text=json.dumps(reply), role=role, model=f"scripted/{role}-stand-in",
            input_tokens=len(prompt) // 4, output_tokens=len(json.dumps(reply)) // 4,
            cost_usd=0.0, latency_s=DELAY_S)
        self.meter.add(result)
        return result

    def _reply(self, role: str, scenario: dict | None) -> dict:
        if scenario is None or "ground_truth" not in scenario:
            if role == ROLE_TRIAGE:
                return {"thought": "Scripted stand-in cannot read uploaded bundles.", "tool": "done"}
            return {"root_cause_ids": [], "summary": "", "confidence": 0,
                    "open_questions": ["The scripted stand-in only knows the sample incidents."]}
        truth = scenario["ground_truth"]
        causes = truth["cause"].split("+")
        if role == ROLE_TRIAGE:
            self.step += 1
            worst = max(scenario["services"],
                        key=lambda s: s.get("error_rate_during_incident") or 0)["name"]
            plan = [("Start with errors on the worst-hit service.", "search_logs",
                     {"service": worst, "keyword": "error"})]
            for cause in causes:
                tool, key = {"commit": ("get_commit", "commit_hash"),
                             "cfg": ("get_config", "config_id"),
                             "infra": ("get_infra_event", "event_id")}[cause.split("-")[0]]
                plan.append((f"{cause} landed shortly before the incident; read it.", tool, {key: cause}))
            if scenario["traces"]:
                plan.append(("Check how a failing request travelled.", "get_trace",
                             {"trace_id": scenario["traces"][0]["trace_id"]}))
            if self.step > len(plan):
                return {"queries": ["resource exhaustion cascading timeouts mitigation",
                                    "detecting saturation before user facing errors"],
                        "thought": "The change and its effect are both in evidence.", "tool": "done"}
            thought, tool, args = plan[self.step - 1]
            return {"thought": thought, "tool": tool, "args": args}
        if role == ROLE_REASON:
            return {
                "root_cause_ids": causes,
                "summary": "Scripted stand-in diagnosis: the change above reduced capacity on the "
                           "target service and its dependants failed in turn.",
                "confidence": 0.86,
                "chain": [{**hop, "because": "Scripted stand-in reasoning for this hop.",
                           "evidence": ["E1", "E2"]} for hop in truth["chain"]],
                "ruled_out": [{"id": c["hash"], "why": "Unrelated service, landed hours earlier."}
                              for c in scenario["commits"] if c["hash"] not in causes][:2],
                "open_questions": ["Why did alerting not fire before users noticed?"],
                "more_evidence": [],
            }
        return {
            "title": "Scripted stand-in postmortem",
            "summary": "This document was produced by the scripted development model, not by Nemotron.",
            "impact": "Users of the affected services saw errors for the length of the incident.",
            "action_items": [
                {"action": "Load-test capacity changes before rollout", "priority": "P0",
                 "why": "Prevents the root cause."},
                {"action": "Alert on saturation at 80%", "priority": "P1",
                 "why": "Shortens detection."},
            ],
            "lessons": ["Capacity settings deserve the same review as code."],
        }


if __name__ == "__main__":
    api.make_llm = ScriptedModel
    from app import app
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "7860")))
