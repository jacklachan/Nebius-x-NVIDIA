"""The investigator's blind view of one incident.

A ``Workspace`` wraps ``PostmortemEnvironment`` and exposes only what a real
on-call engineer has: read-only evidence tools. The environment's oracle
actions (``hypothesize``, ``explain_chain``) and its ``known_facts`` hints are
never surfaced, so a score earned through a workspace is a score earned
without feedback from the ground truth.

Every tool result becomes a numbered piece of evidence (E1, E2, ...) that the
diagnosis and the postmortem cite.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from typing import Any

from engine.environment import PostmortemEnvironment
from models.action import Action, ActionType

TOOLS: dict[str, dict[str, Any]] = {
    "search_logs": {
        "args": "service, keyword, time_window (optional: during_incident, last_5m, last_1h)",
        "about": "Search one service's logs for a keyword or level such as ERROR.",
    },
    "get_trace": {"args": "trace_id", "about": "Read one distributed trace, span by span."},
    "get_commit": {"args": "commit_hash", "about": "Read a commit's message and diff."},
    "get_config": {"args": "config_id", "about": "Read a config change: key, old and new value."},
    "get_infra_event": {"args": "event_id", "about": "Read an infrastructure event in full."},
}

# Names a model reaches for instead of the documented ones. Accepting them
# costs nothing and saves a wasted turn.
TOOL_ALIASES = {
    "query_logs": "search_logs", "logs": "search_logs", "get_logs": "search_logs",
    "fetch_trace": "get_trace", "trace": "get_trace",
    "diff_commit": "get_commit", "get_commit_diff": "get_commit", "commit": "get_commit",
    "inspect_config": "get_config", "get_config_change": "get_config", "config": "get_config",
    "inspect_infra": "get_infra_event", "get_infra": "get_infra_event",
    "infra_event": "get_infra_event",
}
_ID_ARGS = {"get_trace": "trace_id", "get_commit": "commit_hash",
            "get_config": "config_id", "get_infra_event": "event_id"}
_ID_ALIASES = {"id", "hash", "commit", "commit_id", "trace", "config", "event",
               "infra_event_id", "config_change_id"}


def normalise_call(tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    tool = str(tool).strip()
    tool = TOOL_ALIASES.get(tool, tool)
    args = dict(args)
    wanted = _ID_ARGS.get(tool)
    if wanted and wanted not in args:
        given = [k for k in args if k in _ID_ALIASES] or (list(args) if len(args) == 1 else [])
        if given:
            args[wanted] = args.pop(given[0])
    if tool == "search_logs":
        for alias in ("query", "pattern", "level", "text"):
            if "keyword" not in args and alias in args:
                args["keyword"] = args.pop(alias)
        for alias in ("window", "time_range"):
            if "time_window" not in args and alias in args:
                args["time_window"] = args.pop(alias)
    return tool, args


_LOUD = {"ERROR", "CRITICAL", "FATAL"}


def error_onset(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """When each service first logged an error at or after the incident
    began, earliest first. This is what an error-rate dashboard shows at a
    glance, and it tells an investigator where to look first: the service
    that failed first is usually nearer the cause than the one failing most.
    Computed from the logs alone."""
    window = scenario.get("incident_window") or {}
    start, end = str(window.get("start", "")), str(window.get("end", "")) or "~"
    rows = []
    for service, entries in (scenario.get("logs") or {}).items():
        loud = sorted(str(e.get("timestamp", "")) for e in entries
                      if str(e.get("level", "")).upper() in _LOUD
                      and start <= str(e.get("timestamp", "")) <= end)
        if loud:
            rows.append({"service": service, "first_error": loud[0], "errors": len(loud)})
    rows.sort(key=lambda r: (r["first_error"], r["service"]))
    return rows


_PLACEHOLDER_TRUTH = {"cause": "", "cause_type": "commit", "chain": []}


@dataclass
class Evidence:
    id: str
    tool: str
    args: dict[str, Any]
    result: str
    ok: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Workspace:
    def __init__(self, scenario: dict[str, Any]) -> None:
        scenario = copy.deepcopy(scenario)
        self.has_ground_truth = bool((scenario.get("ground_truth") or {}).get("cause"))
        if not self.has_ground_truth:
            scenario["ground_truth"] = dict(_PLACEHOLDER_TRUTH)
        scenario.setdefault("task_id", "incident")
        scenario.setdefault("task_difficulty", "unknown")

        self._scenario = scenario
        self._env = PostmortemEnvironment()
        obs = self._env.reset_from_scenario(scenario)
        self._brief = {
            "incident_id": scenario["task_id"],
            "description": obs.task_description,
            "incident_window": obs.incident_window,
            "service_graph": obs.service_graph,
            "services": [s.model_dump() for s in obs.services],
            "commits": obs.available_commits,
            "config_changes": obs.available_config_changes,
            "trace_ids": obs.available_trace_ids,
            "infra_events": obs.available_infra_events,
            "error_onset": error_onset(scenario),
        }
        self.evidence: list[Evidence] = []
        self._seen: dict[tuple, str] = {}
        # Keep one environment step in reserve for the final submission.
        self.max_calls = max(1, int(obs.max_steps) - 1)

    # --- what the agent may see up front ---

    def brief(self) -> dict[str, Any]:
        return copy.deepcopy(self._brief)

    def candidate_ids(self) -> list[str]:
        """Entities that can be named as a root cause."""
        b = self._brief
        return (
            [c["hash"] for c in b["commits"]]
            + [c["config_id"] for c in b["config_changes"]]
            + [e["event_id"] for e in b["infra_events"]]
        )

    @property
    def calls_left(self) -> int:
        return self.max_calls - sum(1 for e in self.evidence if e.ok)

    # --- evidence tools ---

    def call(self, tool: str, args: dict[str, Any] | None) -> Evidence:
        """Run one evidence tool. Never raises on bad input: the problem is
        returned as an ``ok=False`` evidence entry the agent can read."""
        args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
        tool, args = normalise_call(tool, args)
        key = (tool, tuple(sorted((k, str(v).lower()) for k, v in args.items())))
        if key in self._seen:
            return self._reject(tool, args, f"Already retrieved as {self._seen[key]}.")
        if self.calls_left <= 0:
            return self._reject(tool, args, "Evidence budget exhausted. Conclude now.")

        problem, action = self._to_action(tool, args)
        if problem:
            return self._reject(tool, args, problem)

        obs = self._env.step(action)
        ev = Evidence(
            id=f"E{sum(1 for e in self.evidence if e.ok) + 1}",
            tool=tool,
            args=args,
            result=obs.query_result,
        )
        self.evidence.append(ev)
        self._seen[key] = ev.id
        return ev

    def _reject(self, tool: str, args: dict[str, Any], why: str) -> Evidence:
        ev = Evidence(id="", tool=tool, args=args, result=why, ok=False)
        self.evidence.append(ev)
        return ev

    def _to_action(self, tool: str, args: dict[str, Any]) -> tuple[str, Action | None]:
        b = self._brief
        if tool == "search_logs":
            service = args.get("service")
            if service not in self._scenario.get("logs", {}):
                known = ", ".join(self._scenario.get("logs", {}))
                return f"Unknown service {service!r}. Services with logs: {known}.", None
            return "", Action(
                action_type=ActionType.QUERY_LOGS,
                service=service,
                keyword=str(args.get("keyword", "")),
                time_window=args.get("time_window"),
            )
        if tool == "get_trace":
            return self._lookup(
                args.get("trace_id"), b["trace_ids"], "trace",
                lambda v: Action(action_type=ActionType.FETCH_TRACE, trace_id=v),
            )
        if tool == "get_commit":
            return self._lookup(
                args.get("commit_hash"), [c["hash"] for c in b["commits"]], "commit",
                lambda v: Action(action_type=ActionType.DIFF_COMMIT, commit_hash=v),
            )
        if tool == "get_config":
            return self._lookup(
                args.get("config_id"), [c["config_id"] for c in b["config_changes"]],
                "config change",
                lambda v: Action(action_type=ActionType.INSPECT_CONFIG, config_id=v),
            )
        if tool == "get_infra_event":
            return self._lookup(
                args.get("event_id"), [e["event_id"] for e in b["infra_events"]],
                "infra event",
                lambda v: Action(action_type=ActionType.INSPECT_INFRA, event_id=v),
            )
        return f"Unknown tool {tool!r}. Tools: {', '.join(TOOLS)}.", None

    @staticmethod
    def _lookup(value, known, noun, build) -> tuple[str, Action | None]:
        if value not in known:
            return f"Unknown {noun} {value!r}. Use an ID from the incident brief.", None
        return "", build(value)

    # --- grading (only meaningful when the incident has a known answer) ---

    def grade(self, cause: str, chain: list[dict[str, str]]) -> dict[str, Any] | None:
        """Submit the diagnosis to the deterministic grader.

        Returns ``None`` for real incidents, which have no ground truth.
        """
        if not self.has_ground_truth:
            return None
        self._env.step(
            Action(action_type=ActionType.SUBMIT, final_cause=cause, final_chain=chain)
        )
        state = self._env.state
        return {
            "score": self._env.get_final_score(),
            "rubrics": self._env.get_final_rubric_breakdown() or [],
            "cause_correct": cause.strip().lower() == state.ground_truth_cause.strip().lower(),
            "ground_truth_cause": state.ground_truth_cause,
            "ground_truth_chain": state.ground_truth_chain,
            "evidence_calls": sum(1 for e in self.evidence if e.ok),
        }
