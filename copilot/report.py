"""Turn a finished investigation into a postmortem document.

The document is assembled in code from the validated diagnosis, the incident
brief and the evidence ledger, so every factual section (root cause, causal
chain, timeline, citations) is exactly what the investigation established.
The writer model contributes only the prose a human would otherwise have to
write: the title, the summary, the impact statement and the action items.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from copilot.config import ROLE_WRITER
from copilot.core import ChatModel, Diagnosis, brief_text, clip, ledger_text
from copilot.llm import extract_json
from copilot.workspace import Evidence

PRIORITIES = ("P0", "P1", "P2")

WRITER_SYSTEM = """You write the human-facing parts of an incident postmortem. The investigation is finished; you are given its brief, its diagnosis and its evidence. Write for engineers who were not on the call. Be blameless: describe systems and changes, never people. State only what the diagnosis and evidence support.

Reply with ONE JSON object and nothing else:
{
  "title": "<under 12 words, names the failure, not the fix>",
  "summary": "<one paragraph: what broke, why, how it spread>",
  "impact": "<one or two sentences on who or what was affected and for how long>",
  "action_items": [{"action": "<specific change>", "priority": "P0|P1|P2", "why": "<which part of the failure it prevents or shortens>"}],
  "lessons": ["<one sentence each>"]
}

Give three to five action items. At least one must prevent the root cause and at least one must speed up detection. Do not propose anything the evidence gives no reason for."""


async def write_narrative(
    llm: ChatModel,
    brief: dict[str, Any],
    diagnosis: Diagnosis,
    evidence: list[Evidence],
) -> dict[str, Any]:
    """Ask the writer model for the prose sections. Never raises on a bad
    reply: the caller always gets a usable narrative."""
    user = (
        f"INCIDENT BRIEF\n{brief_text(brief)}\n\n"
        f"DIAGNOSIS\n{json.dumps(diagnosis.as_dict(), separators=(',', ':'))}\n\n"
        f"EVIDENCE LEDGER\n{ledger_text(evidence)}"
    )
    reply = await llm.chat(
        ROLE_WRITER,
        [{"role": "system", "content": WRITER_SYSTEM}, {"role": "user", "content": user}],
        max_tokens=2500,
        temperature=0.3,
    )
    try:
        raw = extract_json(reply.text)
    except ValueError:
        raw = {}
    return _clean_narrative(raw, brief, diagnosis)


def _clean_narrative(
    raw: dict[str, Any], brief: dict[str, Any], diagnosis: Diagnosis
) -> dict[str, Any]:
    items = []
    for item in raw.get("action_items") if isinstance(raw.get("action_items"), list) else []:
        if not isinstance(item, dict) or not item.get("action"):
            continue
        priority = str(item.get("priority", "P1")).upper()
        items.append({
            "action": str(item["action"])[:300],
            "priority": priority if priority in PRIORITIES else "P1",
            "why": str(item.get("why", ""))[:300],
        })
    lessons = [str(x)[:300] for x in raw.get("lessons", []) if x] \
        if isinstance(raw.get("lessons"), list) else []
    return {
        "title": str(raw.get("title") or f"Incident {brief['incident_id']}")[:140],
        "summary": str(raw.get("summary") or diagnosis.summary or brief["description"])[:2000],
        "impact": str(raw.get("impact") or "")[:600],
        "action_items": items[:6],
        "lessons": lessons[:5],
    }


def fallback_narrative(brief: dict[str, Any], diagnosis: Diagnosis) -> dict[str, Any]:
    """Narrative built from the diagnosis alone, for when the writer is down."""
    return _clean_narrative({}, brief, diagnosis)


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration(window: dict[str, str]) -> str:
    start, end = _parse(window.get("start", "")), _parse(window.get("end", ""))
    if not start or not end:
        return "unknown"
    minutes = int((end - start).total_seconds() // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h {minutes % 60} min"


def _entities(brief: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Every nameable change or event: id -> kind, service, timestamp, text."""
    out: dict[str, dict[str, str]] = {}
    for c in brief["commits"]:
        out[c["hash"]] = {"kind": "Commit", "service": c.get("service", ""),
                          "timestamp": c.get("timestamp", ""), "text": c.get("message", "")}
    for c in brief["config_changes"]:
        out[c["config_id"]] = {"kind": "Config change", "service": c.get("service", ""),
                               "timestamp": c.get("timestamp", ""),
                               "text": c.get("description", "")}
    for e in brief["infra_events"]:
        out[e["event_id"]] = {"kind": "Infrastructure event", "service": "",
                              "timestamp": e.get("timestamp", ""),
                              "text": e.get("description", "")}
    return out


def _cites(ids: list[str]) -> str:
    return " ".join(f"[{i}]" for i in ids) if ids else "_(no evidence cited)_"


def render_postmortem(
    brief: dict[str, Any],
    diagnosis: Diagnosis,
    evidence: list[Evidence],
    narrative: dict[str, Any],
    usage: dict[str, Any] | None = None,
) -> str:
    """Assemble the postmortem as Markdown."""
    window = brief.get("incident_window", {})
    entities = _entities(brief)
    kept = [e for e in evidence if e.ok]
    lines: list[str] = [f"# {narrative['title']}", ""]

    status = "Root cause identified" if diagnosis.root_cause_ids else "Root cause not established"
    lines += [
        f"**Incident** `{brief['incident_id']}`  ",
        f"**Window** {window.get('start', '?')} to {window.get('end', '?')} ({_duration(window)})  ",
        f"**Status** {status}, confidence {diagnosis.confidence:.0%}",
        "",
        "## Summary", "", narrative["summary"], "",
    ]

    lines += ["## Impact", ""]
    if narrative["impact"]:
        lines += [narrative["impact"], ""]
    lines += ["| Service | Status | Error rate during incident |", "|---|---|---|"]
    for s in brief["services"]:
        rate = s.get("error_rate_during_incident")
        lines.append(f"| {s['name']} | {s.get('status', '?')} | "
                     f"{f'{rate:.1f}%' if isinstance(rate, (int, float)) else 'n/a'} |")
    lines.append("")

    lines += ["## Root cause", ""]
    if diagnosis.root_cause_ids:
        for cause_id in diagnosis.root_cause_ids:
            e = entities.get(cause_id, {})
            where = f" on `{e['service']}`" if e.get("service") else ""
            lines.append(f"- **{e.get('kind', 'Change')} `{cause_id}`**{where}, "
                         f"{e.get('timestamp', 'time unknown')}: {e.get('text', '')}")
        lines += ["", diagnosis.summary, ""]
    else:
        lines += ["The evidence gathered was not enough to name a cause. "
                  "See open questions below.", ""]

    if diagnosis.chain:
        lines += ["## How it spread", ""]
        for i, hop in enumerate(diagnosis.chain, start=1):
            lines.append(f"{i}. **{hop['service']}**: {hop['effect'].replace('_', ' ')}. "
                         f"{hop['because']} {_cites(hop['evidence'])}")
        lines.append("")

    timeline = []
    for cause_id in diagnosis.root_cause_ids:
        e = entities.get(cause_id)
        if e and e["timestamp"]:
            timeline.append((e["timestamp"], f"{e['kind']} `{cause_id}` lands"))
    if window.get("start"):
        timeline.append((window["start"], "Incident begins"))
    if window.get("end"):
        timeline.append((window["end"], "Incident ends"))
    if timeline:
        lines += ["## Timeline", ""]
        lines += [f"- `{ts}` {what}" for ts, what in sorted(timeline)]
        lines.append("")

    if narrative["action_items"]:
        lines += ["## Action items", "", "| Priority | Action | Why |", "|---|---|---|"]
        order = {p: i for i, p in enumerate(PRIORITIES)}
        for item in sorted(narrative["action_items"], key=lambda a: order[a["priority"]]):
            lines.append(f"| {item['priority']} | {item['action']} | {item['why']} |")
        lines.append("")

    if diagnosis.ruled_out:
        lines += ["## Ruled out", ""]
        lines += [f"- `{r['id']}`: {r['why']}" for r in diagnosis.ruled_out]
        lines.append("")

    if diagnosis.open_questions:
        lines += ["## Open questions", ""]
        lines += [f"- {q}" for q in diagnosis.open_questions]
        lines.append("")

    if narrative["lessons"]:
        lines += ["## Lessons", ""]
        lines += [f"- {lesson}" for lesson in narrative["lessons"]]
        lines.append("")

    lines += ["## Evidence", ""]
    for e in kept:
        args = ", ".join(f"{k}={v}" for k, v in e.args.items())
        lines += [f"**[{e.id}]** `{e.tool}({args})`", "", "```", clip(e.result, 1200), "```", ""]

    if usage and usage.get("by_role"):
        lines += ["## Investigation cost", "", "| Stage | Model | Calls | Tokens in / out | Cost |",
                  "|---|---|---|---|---|"]
        for role, row in usage["by_role"].items():
            lines.append(f"| {role} | {row['model']} | {row['calls']} | "
                         f"{row['input_tokens']} / {row['output_tokens']} | ${row['cost_usd']:.4f} |")
        lines += ["", f"Total: ${usage['total_cost_usd']:.4f}, "
                      f"{len(kept)} evidence lookups.", ""]

    return "\n".join(lines).rstrip() + "\n"
