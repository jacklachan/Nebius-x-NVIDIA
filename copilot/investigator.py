"""The investigation loop.

Three stages, three models:

  triage     Many short calls on the small Nemotron model. Each one picks the
             next piece of evidence to pull. Cheap, fast, latency-sensitive.
  diagnosis  One or two calls on the large Nemotron model. Reads the whole
             evidence ledger and commits to a root cause and causal chain,
             citing evidence IDs. It may ask for a few more lookups once.
  research   Optional. Web search (Tavily) for the diagnosed failure mode,
             with incident-specific terms kept out of the queries.
  report     One call on the mid-size model for the prose of the postmortem;
             the factual sections are assembled in code (see report.py).

The investigator is an async generator of event dicts so the same code drives
the CLI, the benchmark and the streaming web UI.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from copilot.config import ROLE_REASON, ROLE_TRIAGE
from copilot.core import ChatModel, Diagnosis, brief_text, ledger_text
from copilot.llm import LLMError, extract_json
from copilot.report import fallback_narrative, render_postmortem, write_narrative
from copilot.research import Reference, Researcher, ResearchError
from copilot.workspace import TOOLS, Workspace
from data.seed_generator import FAILURE_TEMPLATES

# Closed vocabulary for labelling each hop of a causal chain. Using a fixed
# failure-mode taxonomy keeps chains comparable across incidents and is what
# lets the grader score them exactly.
EFFECT_TAXONOMY: tuple[str, ...] = tuple(
    sorted({
        step["effect"]
        for template in FAILURE_TEMPLATES.values()
        for step in template["chain_template"]
    })
)

MAX_TRIAGE_STEPS = 12
MAX_STUMBLES = 3          # unparseable replies or rejected calls in a row
MAX_FOLLOW_UPS = 3        # extra lookups the diagnosis stage may request

TRIAGE_SYSTEM = f"""You are the triage stage of a production incident investigation. The outage already happened; you are reading frozen telemetry to find what started it.

Reply with ONE JSON object and nothing else:
  {{"thought": "<one sentence: what you expect to learn>", "tool": "<tool>", "args": {{...}}}}
or, when further lookups would not change the conclusion:
  {{"thought": "<one sentence>", "tool": "done"}}

Tools:
{chr(10).join(f"  {name}({spec['args']}) - {spec['about']}" for name, spec in TOOLS.items())}

How to investigate:
- Start with ERROR logs of the worst-affected service during the incident.
- Outages usually follow a change. Read the diff or value of every commit and config change that landed on a suspect service shortly before the incident began. A commit message alone proves nothing.
- Follow the dependency graph: a failing service is often a victim of something it depends on.
- Check infrastructure events close to the incident start.
- Never repeat a lookup. Only use IDs that appear in the brief.
- Every lookup costs time and money. Stop as soon as you can name the change that started the outage and how it spread."""

DIAGNOSIS_SYSTEM = """You are the diagnosis stage of a production incident investigation. You are given the incident brief and a ledger of evidence (E1, E2, ...) gathered from frozen telemetry. Decide what caused the outage. Claim only what the evidence supports.

Reply with ONE JSON object and nothing else:
{
  "root_cause_ids": ["<ID of the change or event that started the outage>"],
  "summary": "<two or three sentences a tired engineer can act on>",
  "confidence": <0.0 to 1.0>,
  "chain": [{"service": "<service>", "effect": "<label>", "because": "<one sentence>", "evidence": ["E1"]}],
  "ruled_out": [{"id": "<candidate ID>", "why": "<one sentence>"}],
  "open_questions": ["<what the evidence does not settle>"],
  "more_evidence": [{"tool": "<tool>", "args": {...}}]
}

Rules:
- root_cause_ids must come from the candidate list. Name one ID. Name two only when two independent changes were both necessary (for example a code bug that needed an infrastructure event to trigger it).
- chain is ordered from the origin to the user-visible symptom, one entry per failure hop. A service may appear more than once.
- effect must be one of the taxonomy labels.
- Cite evidence IDs for every hop. Do not cite IDs that are not in the ledger.
- more_evidence: at most three lookups that would materially change your answer. Leave it empty when you are confident or when told no further lookups are possible."""


class Investigator:
    def __init__(
        self,
        workspace: Workspace,
        llm: ChatModel,
        max_triage_steps: int = MAX_TRIAGE_STEPS,
        researcher: Researcher | None = None,
    ) -> None:
        self.ws = workspace
        self.llm = llm
        self.researcher = researcher
        self.references: list[Reference] = []
        self.max_triage_steps = max_triage_steps
        self.diagnosis: Diagnosis | None = None
        self.grade: dict[str, Any] | None = None
        self.narrative: dict[str, Any] | None = None
        self.report: str = ""

    async def run(self) -> AsyncGenerator[dict[str, Any], None]:
        yield {"type": "brief", "brief": self.ws.brief(),
               "graded": self.ws.has_ground_truth}
        try:
            yield {"type": "phase", "phase": "triage"}
            async for event in self._triage():
                yield event

            yield {"type": "phase", "phase": "diagnosis"}
            async for event in self._diagnose():
                yield event
        except LLMError as exc:
            yield {"type": "error", "message": str(exc), "status": exc.status}
            return

        assert self.diagnosis is not None
        self.grade = self.ws.grade(self.diagnosis.cause, self.diagnosis.graded_chain())
        if self.grade is not None:
            yield {"type": "grade", **self.grade}

        brief = self.ws.brief()
        if self.researcher is not None and self.diagnosis.root_cause_ids:
            yield {"type": "phase", "phase": "research"}
            try:
                self.references = await self.researcher.run(brief, self.diagnosis)
                yield {"type": "research",
                       "queries": self.researcher.queries,
                       "queries_dropped_as_private": self.researcher.dropped,
                       "references": [ref.as_dict() for ref in self.references]}
            except (ResearchError, LLMError) as exc:
                # Research improves the action items; it is never required.
                yield {"type": "warning", "message": f"Research skipped: {exc}"}

        yield {"type": "phase", "phase": "report"}
        try:
            self.narrative = await write_narrative(
                self.llm, brief, self.diagnosis, self.ws.evidence, self.references)
        except LLMError as exc:
            # The diagnosis is the valuable part; never lose it to a failed
            # prose call. Fall back to a document built from the facts alone.
            yield {"type": "warning", "message": f"Writer model unavailable: {exc}"}
            self.narrative = fallback_narrative(brief, self.diagnosis)
        usage = self.llm.meter.snapshot()
        self.report = render_postmortem(
            brief, self.diagnosis, self.ws.evidence, self.narrative, usage, self.references)
        yield {"type": "report", "narrative": self.narrative, "markdown": self.report}
        yield {
            "type": "done",
            "diagnosis": self.diagnosis.as_dict(),
            "grade": self.grade,
            "evidence": [e.as_dict() for e in self.ws.evidence if e.ok],
            "references": [ref.as_dict() for ref in self.references],
            "usage": usage,
        }

    # --- stage 1: triage ---

    async def _triage(self) -> AsyncGenerator[dict[str, Any], None]:
        brief = brief_text(self.ws.brief())
        stumbles = 0
        note = ""
        for _ in range(self.max_triage_steps):
            if self.ws.calls_left <= 0 or stumbles >= MAX_STUMBLES:
                break
            user = (
                f"INCIDENT BRIEF\n{brief}\n\n"
                f"EVIDENCE SO FAR\n{ledger_text(self.ws.evidence)}\n\n"
                f"Lookups left: {self.ws.calls_left}."
                + (f"\nYour last reply was not usable: {note}" if note else "")
                + "\nWhat is the next lookup?"
            )
            reply = await self.llm.chat(
                ROLE_TRIAGE,
                [{"role": "system", "content": TRIAGE_SYSTEM},
                 {"role": "user", "content": user}],
                max_tokens=600,
            )
            yield {"type": "usage", **self.llm.meter.snapshot()}
            try:
                step = extract_json(reply.text)
            except ValueError as exc:
                stumbles += 1
                note = f"{exc}. Reply with one JSON object."
                continue

            thought = str(step.get("thought", ""))[:300]
            tool = str(step.get("tool", ""))
            yield {"type": "thought", "role": ROLE_TRIAGE, "model": reply.model,
                   "text": thought, "tool": tool}
            if tool == "done":
                break

            evidence = self.ws.call(tool, step.get("args") if isinstance(step.get("args"), dict) else {})
            yield {"type": "evidence", "role": ROLE_TRIAGE, **evidence.as_dict()}
            if evidence.ok:
                stumbles, note = 0, ""
            else:
                stumbles += 1
                note = evidence.result

    # --- stage 2: diagnosis ---

    async def _diagnose(self) -> AsyncGenerator[dict[str, Any], None]:
        raw = await self._ask_diagnosis(allow_follow_ups=True)
        yield {"type": "usage", **self.llm.meter.snapshot()}

        follow_ups = raw.get("more_evidence") or []
        gathered = False
        if isinstance(follow_ups, list):
            for request in follow_ups[:MAX_FOLLOW_UPS]:
                if not isinstance(request, dict) or self.ws.calls_left <= 0:
                    continue
                args = request.get("args")
                evidence = self.ws.call(
                    str(request.get("tool", "")), args if isinstance(args, dict) else {}
                )
                yield {"type": "evidence", "role": ROLE_REASON, **evidence.as_dict()}
                gathered = gathered or evidence.ok
        if gathered:
            raw = await self._ask_diagnosis(allow_follow_ups=False)
            yield {"type": "usage", **self.llm.meter.snapshot()}

        self.diagnosis = self._validated(raw)
        yield {"type": "diagnosis", "role": ROLE_REASON,
               "model": self.llm.meter.by_role[ROLE_REASON]["model"],
               **self.diagnosis.as_dict()}

    async def _ask_diagnosis(self, allow_follow_ups: bool) -> dict[str, Any]:
        brief = self.ws.brief()
        user = (
            f"INCIDENT BRIEF\n{brief_text(brief)}\n\n"
            f"CANDIDATE ROOT CAUSE IDS\n{', '.join(self.ws.candidate_ids())}\n\n"
            f"EFFECT TAXONOMY\n{', '.join(EFFECT_TAXONOMY)}\n\n"
            f"EVIDENCE LEDGER\n{ledger_text(self.ws.evidence)}\n\n"
            + ("You may request more evidence once."
               if allow_follow_ups and self.ws.calls_left > 0
               else "No further lookups are possible. Give your final diagnosis.")
        )
        messages = [{"role": "system", "content": DIAGNOSIS_SYSTEM},
                    {"role": "user", "content": user}]
        reply = await self.llm.chat(ROLE_REASON, messages, max_tokens=4000)
        try:
            return extract_json(reply.text)
        except ValueError:
            # One repair attempt; a diagnosis we cannot parse is no diagnosis.
            messages += [
                {"role": "assistant", "content": reply.text[:2000]},
                {"role": "user", "content": "That was not a single JSON object. "
                                            "Reply again with only the JSON object."},
            ]
            reply = await self.llm.chat(ROLE_REASON, messages, max_tokens=4000)
            try:
                return extract_json(reply.text)
            except ValueError:
                return {}

    def _validated(self, raw: dict[str, Any]) -> Diagnosis:
        """Keep only what refers to things that exist. The model's output is a
        claim about this incident; anything it invented is dropped here."""
        candidates = set(self.ws.candidate_ids())
        services = set(self.ws.brief()["service_graph"]) | {
            s["name"] for s in self.ws.brief()["services"]
        }
        evidence_ids = {e.id for e in self.ws.evidence if e.ok}

        ids = raw.get("root_cause_ids")
        if isinstance(ids, str):
            ids = ids.split("+")
        root_ids = []
        for i in ids if isinstance(ids, list) else []:
            i = str(i).strip()
            if i in candidates and i not in root_ids:
                root_ids.append(i)

        chain = []
        for hop in raw.get("chain") if isinstance(raw.get("chain"), list) else []:
            if not isinstance(hop, dict) or hop.get("service") not in services:
                continue
            cited = hop.get("evidence") if isinstance(hop.get("evidence"), list) else []
            chain.append({
                "service": hop["service"],
                "effect": str(hop.get("effect", "")).strip(),
                "because": str(hop.get("because", ""))[:400],
                "evidence": [c for c in cited if c in evidence_ids],
            })

        try:
            confidence = min(1.0, max(0.0, float(raw.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0

        ruled_out = [
            {"id": str(r.get("id", "")), "why": str(r.get("why", ""))[:300]}
            for r in (raw.get("ruled_out") if isinstance(raw.get("ruled_out"), list) else [])
            if isinstance(r, dict) and r.get("id") in candidates
        ]
        questions = [
            str(q)[:300]
            for q in (raw.get("open_questions") if isinstance(raw.get("open_questions"), list) else [])
        ]
        return Diagnosis(
            root_cause_ids=root_ids[:2],
            summary=str(raw.get("summary", ""))[:1200],
            confidence=confidence if root_ids else 0.0,
            chain=chain,
            ruled_out=ruled_out,
            open_questions=questions,
        )
