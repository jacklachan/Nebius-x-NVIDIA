"""The oracle: an investigator that is told the answer.

It is the ceiling on the leaderboard and a check on the incidents themselves.
Knowing the answer does not let it skip the work: it goes through the same
evidence tools as the real investigator, has to open every change it blames,
and has to find, for each hop of the chain, an exhibit that actually shows
that hop. It is scored by the same evaluator.

That makes it useful for three things:

* **Ceiling.** Its score is the best an investigator can do on an incident.
* **Solvability.** If the oracle cannot support a hop with evidence, the
  incident's telemetry does not contain that evidence and no investigator
  could find it. ``unsupported`` lists those hops.
* **Cost of a perfect answer.** Its lookup count is the fewest lookups a
  complete, fully cited investigation needs, which is what the efficiency
  score should be read against.

Nothing here is used by the real investigator.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from copilot.core import Diagnosis
from copilot.report import fallback_narrative, render_postmortem
from copilot.taxonomy import EFFECTS
from copilot.workspace import Evidence, Workspace

ORACLE_MODEL = "oracle (given the answer)"

_CAUSE_TOOLS = {"commit": ("get_commit", "commit_hash"),
                "cfg": ("get_config", "config_id"),
                "infra": ("get_infra_event", "event_id")}
# Narrowest search first: the oracle prefers the exhibit a careful engineer
# would cite, not a dump of the whole log.
_LOG_SEARCHES = (
    {"level": "ERROR", "time_window": "during_incident"},
    {"level": "WARN", "time_window": "during_incident"},
    {"level": "WARN", "time_window": "before_incident"},
    {"time_window": "during_incident"},
    {},
)


class OracleError(ValueError):
    """The incident has no known answer for an oracle to work from."""


class Oracle:
    def __init__(self, scenario: dict[str, Any], pace: float = 0.0) -> None:
        self.pace = pace            # seconds between exhibits when streamed to a viewer
        self.report = ""
        self.ws = Workspace(scenario)
        if not self.ws.has_ground_truth:
            raise OracleError("An oracle needs an incident with a known answer.")
        self._scenario = scenario
        self._truth = scenario["ground_truth"]
        self._services = {c["hash"]: c.get("service") for c in scenario.get("commits", [])}
        self._services |= {c["config_id"]: c.get("service")
                           for c in scenario.get("config_changes", [])}
        self._traces = scenario.get("traces", [])
        self.diagnosis: Diagnosis | None = None
        self.grade: dict[str, Any] | None = None
        self.unsupported: list[dict[str, str]] = []

    def _bears_on_incident(self, evidence: Evidence) -> bool:
        # The oracle may look at what the evaluator knows; that is the point of it.
        return evidence.ok and evidence.id in self.ws._relevant_evidence

    def investigate(self) -> list[Evidence]:
        """Gather the evidence for a complete answer and grade it. Returns
        the exhibits in the order they were retrieved."""
        causes = self._truth["cause"].split("+")
        by_service: dict[str, list[str]] = {}

        # 1. Open every change that is blamed.
        for cause in causes:
            tool, key = _CAUSE_TOOLS.get(cause.split("-")[0], _CAUSE_TOOLS["commit"])
            exhibit = self.ws.call(tool, {key: cause})
            if self._bears_on_incident(exhibit) and self._services.get(cause):
                by_service.setdefault(self._services[cause], []).append(exhibit.id)

        # 2. For each service on the failure path, find logs that show it failing.
        path = list(dict.fromkeys(hop["service"] for hop in self._truth["chain"]))
        #    It knows where to look, so it tries searches on a scratch copy and
        #    spends a real lookup only on the one that pays off: no dead ends.
        scratch = Workspace(self._scenario)
        for service in path:
            for search in _LOG_SEARCHES:
                args = {"service": service, **search}
                trial = scratch.call("search_logs", args)
                if trial.ok and trial.id in scratch._relevant_evidence:
                    exhibit = self.ws.call("search_logs", args)
                    by_service.setdefault(service, []).append(exhibit.id)
                    break

        # 3. One failing request, end to end, backs the whole path.
        trace_exhibit = ""
        for trace in self._traces:
            spans = {span.get("service") for span in trace.get("spans", [])}
            if trace.get("relevant") and set(path) <= spans:
                exhibit = self.ws.call("get_trace", {"trace_id": trace["trace_id"]})
                if self._bears_on_incident(exhibit):
                    trace_exhibit = exhibit.id
                    break

        chain = []
        for hop in self._truth["chain"]:
            cited = list(by_service.get(hop["service"], []))
            if trace_exhibit:
                cited.append(trace_exhibit)
            if not by_service.get(hop["service"]):
                self.unsupported.append(dict(hop))
            chain.append({"service": hop["service"], "effect": hop["effect"],
                          "because": EFFECTS.get(hop["effect"], hop["effect"]).capitalize() + ".",
                          "evidence": cited})

        self.diagnosis = Diagnosis(
            root_cause_ids=causes,
            summary="Reference answer: the known cause and failure path of this incident, "
                    "with the evidence that shows each step.",
            confidence=1.0,
            chain=chain,
        )
        self.grade = self.ws.grade(self.diagnosis.cause, chain)
        return [e for e in self.ws.evidence if e.ok]

    async def run(self) -> AsyncGenerator[dict[str, Any], None]:
        """The investigation as the event stream the UI and API consume."""
        yield {"type": "brief", "brief": self.ws.brief(), "graded": True}
        yield {"type": "phase", "phase": "triage"}
        for exhibit in self.investigate():
            yield {"type": "evidence", "role": "oracle", **exhibit.as_dict()}
            if self.pace:
                await asyncio.sleep(self.pace)
        assert self.diagnosis is not None and self.grade is not None
        yield {"type": "phase", "phase": "diagnosis"}
        yield {"type": "diagnosis", "role": "oracle", "model": ORACLE_MODEL,
               **self.diagnosis.as_dict()}
        yield {"type": "grade", **self.grade}
        yield {"type": "phase", "phase": "report"}
        brief = self.ws.brief()
        narrative = fallback_narrative(brief, self.diagnosis)
        narrative["title"] = f"Reference answer: {brief['incident_id']}"
        self.report = render_postmortem(brief, self.diagnosis, self.ws.evidence, narrative)
        yield {"type": "report", "narrative": narrative, "markdown": self.report}
        yield {"type": "done", "diagnosis": self.diagnosis.as_dict(), "grade": self.grade,
               "evidence": [e.as_dict() for e in self.ws.evidence if e.ok], "references": [],
               "usage": {"by_role": {}, "total_cost_usd": 0.0}}


def oracle_row(seed: int, difficulty: str) -> dict[str, Any]:
    """One benchmark row for the oracle on a generated incident."""
    from copilot import incidents

    oracle = Oracle(incidents.from_seed(seed, difficulty))
    oracle.investigate()
    assert oracle.grade is not None and oracle.diagnosis is not None
    return {
        "seed": seed, "difficulty": difficulty, "error": None,
        "cause": oracle.diagnosis.cause, "truth": oracle.grade["ground_truth_cause"],
        "cause_correct": oracle.grade["cause_correct"], "score": oracle.grade["score"],
        "confidence": 1.0, "lookups": oracle.ws.lookups, "cost_usd": 0.0, "seconds": 0.0,
        "unsupported_hops": len(oracle.unsupported),
    }
