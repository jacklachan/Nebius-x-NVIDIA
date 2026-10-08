"""The investigator's view of one incident.

A ``Workspace`` holds an incident's telemetry and exposes only what a real
on-call engineer has: read-only evidence tools. There is no way to ask it
whether a guess is right, and nothing it returns carries the answer or the
labels that mark which facts matter. A score earned through a workspace is a
score earned without feedback from the ground truth.

Every successful lookup becomes a numbered exhibit (E1, E2, ...) that the
diagnosis and the postmortem cite. The workspace quietly records which
exhibits revealed a fact that bears on the incident, so that the evaluator
can check whether the citations in an answer are real support.
"""

from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from copilot.evaluate import evaluate

TOOLS: dict[str, dict[str, Any]] = {
    "search_logs": {
        "args": "service, keyword (optional), level (optional minimum: WARN, ERROR), "
                "time_window (optional: during_incident, before_incident, first_5m, last_30m)",
        "about": "Search one service's logs. Results are in time order.",
    },
    "get_trace": {"args": "trace_id", "about": "Read one distributed trace, span by span."},
    "get_commit": {"args": "commit_hash", "about": "Read a commit's message and diff."},
    "get_config": {"args": "config_id", "about": "Read a config change: key, old and new value."},
    "get_infra_event": {"args": "event_id", "about": "Read an infrastructure event in full."},
}

MAX_LOG_RESULTS = 30
DEFAULT_BUDGET = 40
LEVELS = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3, "WARNING": 3,
          "ERROR": 4, "CRITICAL": 5, "FATAL": 5}

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
_RELATIVE_WINDOW = re.compile(r"^(first|last)_(\d+)\s*([smhd])$")
_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_LOUD = {"ERROR", "CRITICAL", "FATAL"}


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
        for alias in ("query", "pattern", "text"):
            if "keyword" not in args and alias in args:
                args["keyword"] = args.pop(alias)
        for alias in ("min_level", "severity"):
            if "level" not in args and alias in args:
                args["level"] = args.pop(alias)
        for alias in ("window", "time_range"):
            if "time_window" not in args and alias in args:
                args["time_window"] = args.pop(alias)
    return tool, args


def _when(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


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
        self._scenario = scenario = copy.deepcopy(scenario)
        self._truth = scenario.get("ground_truth") or {}
        self.has_ground_truth = bool(self._truth.get("cause"))
        self._relevant = set(scenario.get("relevant_fact_ids") or [])

        window = scenario.get("incident_window") or {}
        self._start, self._end = _when(window.get("start")), _when(window.get("end"))
        self._logs: dict[str, list[dict]] = scenario.get("logs") or {}
        self._traces = {t["trace_id"]: t for t in scenario.get("traces") or []}
        self._commits = {c["hash"]: c for c in scenario.get("commits") or []}
        self._configs = {c["config_id"]: c for c in scenario.get("config_changes") or []}
        self._infra = {e["event_id"]: e for e in scenario.get("infra_events") or []}

        graph = scenario.get("service_graph") or {}
        self._brief = {
            "incident_id": scenario.get("task_id", "incident"),
            "description": scenario.get("task_description", "Investigate this incident."),
            "incident_window": dict(window),
            "service_graph": graph,
            "services": [
                {"name": s["name"], "status": s.get("status", "unknown"),
                 "dependencies": s.get("dependencies", graph.get(s["name"], [])),
                 "recent_deploy_count": s.get("recent_deploy_count", 0),
                 "error_rate_during_incident": s.get("error_rate_during_incident")}
                for s in scenario.get("services") or []],
            "commits": [{"hash": c["hash"], "service": c.get("service", ""),
                         "timestamp": c.get("timestamp", ""), "message": c.get("message", "")}
                        for c in self._commits.values()],
            "config_changes": [{"config_id": c["config_id"], "service": c.get("service", ""),
                                "timestamp": c.get("timestamp", ""), "key": c.get("key", ""),
                                "description": c.get("description", "")}
                               for c in self._configs.values()],
            "trace_ids": list(self._traces),
            "infra_events": [{"event_id": e["event_id"], "timestamp": e.get("timestamp", ""),
                              "description": e.get("description", "")}
                             for e in self._infra.values()],
            "error_onset": error_onset(scenario),
        }
        self.evidence: list[Evidence] = []
        self.max_calls = max(1, int(scenario.get("max_steps") or DEFAULT_BUDGET))
        self._seen: dict[tuple, str] = {}
        self._retrieved: set[str] = set()            # change and trace IDs fetched
        self._relevant_evidence: set[str] = set()    # exhibits that revealed a relevant fact

    # --- what the agent may see up front ---

    def brief(self) -> dict[str, Any]:
        return copy.deepcopy(self._brief)

    def candidate_ids(self) -> list[str]:
        """Entities that can be named as a root cause."""
        return list(self._commits) + list(self._configs) + list(self._infra)

    @property
    def lookups(self) -> int:
        return sum(1 for e in self.evidence if e.ok)

    @property
    def calls_left(self) -> int:
        return self.max_calls - self.lookups

    # --- evidence tools ---

    def call(self, tool: str, args: dict[str, Any] | None) -> Evidence:
        """Run one evidence tool. Never raises on bad input: the problem is
        returned as an ``ok=False`` entry the agent can read."""
        args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
        tool, args = normalise_call(tool, args)
        key = (tool, tuple(sorted((k, str(v).lower()) for k, v in args.items())))
        if key in self._seen:
            return self._reject(tool, args, f"Already retrieved as {self._seen[key]}.")
        if self.calls_left <= 0:
            return self._reject(tool, args, "Evidence budget exhausted. Conclude now.")

        handler = {
            "search_logs": self._search_logs,
            "get_trace": self._get_trace,
            "get_commit": self._get_commit,
            "get_config": self._get_config,
            "get_infra_event": self._get_infra_event,
        }.get(tool)
        if handler is None:
            return self._reject(tool, args, f"Unknown tool {tool!r}. Tools: {', '.join(TOOLS)}.")
        problem, text, revealed = handler(args)
        if problem:
            return self._reject(tool, args, problem)

        ev = Evidence(id=f"E{self.lookups + 1}", tool=tool, args=args, result=text)
        self.evidence.append(ev)
        self._seen[key] = ev.id
        if revealed & self._relevant:
            self._relevant_evidence.add(ev.id)
        return ev

    def _reject(self, tool: str, args: dict[str, Any], why: str) -> Evidence:
        ev = Evidence(id="", tool=tool, args=args, result=why, ok=False)
        self.evidence.append(ev)
        return ev

    # Each handler returns (problem, text, ids of the facts it revealed).

    def _window(self, spec: str) -> tuple[datetime | None, datetime | None] | None:
        """Resolve a time_window to (from, to). ``None`` means not understood."""
        spec = spec.strip().lower()
        if spec in ("", "all", "any"):
            return None, None
        if spec in ("during_incident", "during", "incident"):
            return self._start, self._end
        if spec in ("before_incident", "before"):
            return None, self._start
        match = _RELATIVE_WINDOW.match(spec)
        if match:
            span = timedelta(seconds=int(match[2]) * _SECONDS[match[3]])
            if match[1] == "first" and self._start:
                return self._start, self._start + span
            if match[1] == "last" and self._end:
                return self._end - span, self._end
        return None

    def _search_logs(self, args: dict[str, Any]) -> tuple[str, str, set[str]]:
        service = args.get("service")
        if service not in self._logs:
            return f"Unknown service {service!r}. Services with logs: {', '.join(self._logs)}.", "", set()
        keyword = str(args.get("keyword", "")).strip().lower()
        level = str(args.get("level", "")).strip().upper()
        if level and level not in LEVELS:
            return f"Unknown level {level!r}. Use one of DEBUG, INFO, WARN, ERROR, CRITICAL.", "", set()
        spec = str(args.get("time_window", ""))
        window = self._window(spec)
        if window is None:
            return (f"Unknown time_window {spec!r}. Use during_incident, before_incident, "
                    "first_<N>m or last_<N>m."), "", set()
        after, before = window
        floor = LEVELS.get(level, 0)

        def matches(entry: dict) -> bool:
            entry_level = str(entry.get("level", "INFO")).upper()
            if LEVELS.get(entry_level, 2) < floor:
                return False
            # A keyword that names a level ("error") also matches by level.
            if keyword and keyword not in str(entry.get("message", "")).lower() \
                    and keyword != entry_level.lower():
                return False
            moment = _when(entry.get("timestamp"))
            if moment is None:
                return True          # never hide a line we cannot place in time
            return (after is None or moment >= after) and (before is None or moment <= before)

        found = sorted((e for e in self._logs[service] if matches(e)),
                       key=lambda e: str(e.get("timestamp", "")))
        shown = found[:MAX_LOG_RESULTS]
        filters = ", ".join(f"{k}={v}" for k, v in
                            (("keyword", keyword), ("level", level), ("time_window", spec)) if v)
        header = f"Logs for {service}" + (f" ({filters})" if filters else "")
        if not shown:
            return "", f"{header}: no matching entries.", set()
        lines = [f"[{e.get('timestamp', '')}] {e.get('level', 'INFO')}: {e.get('message', '')}"
                 for e in shown]
        if len(found) > len(shown):
            lines.append(f"... {len(found) - len(shown)} more matches not shown. "
                         "Narrow with keyword, level or time_window.")
        return "", f"{header}: {len(found)} matching\n" + "\n".join(lines), \
            {str(e.get("id", "")) for e in shown}

    def _lookup(self, value: Any, table: dict[str, dict], noun: str):
        if value not in table:
            return f"Unknown {noun} {value!r}. Use an ID from the incident brief.", None
        self._retrieved.add(str(value))
        return "", table[value]

    def _get_trace(self, args: dict[str, Any]) -> tuple[str, str, set[str]]:
        problem, trace = self._lookup(args.get("trace_id"), self._traces, "trace")
        if problem:
            return problem, "", set()
        lines = [f"Trace {trace['trace_id']} at {trace.get('timestamp', '')}"]
        for span in trace.get("spans", []):
            line = (f"  {span.get('service', '?')} | {span.get('operation', '?')} | "
                    f"{span.get('duration_ms', 0)}ms | {span.get('status', 'OK')}")
            if span.get("error"):
                line += f" | {span['error']}"
            lines.append(line)
        return "", "\n".join(lines), {trace["trace_id"]}

    def _get_commit(self, args: dict[str, Any]) -> tuple[str, str, set[str]]:
        problem, commit = self._lookup(args.get("commit_hash"), self._commits, "commit")
        if problem:
            return problem, "", set()
        text = (f"Commit {commit['hash']}\nService: {commit.get('service', '?')}\n"
                f"Author: {commit.get('author', '?')}\nTimestamp: {commit.get('timestamp', '?')}\n"
                f"Message: {commit.get('message', '')}\n\n{commit.get('diff') or '(no diff available)'}")
        return "", text, {commit["hash"]}

    def _get_config(self, args: dict[str, Any]) -> tuple[str, str, set[str]]:
        problem, change = self._lookup(args.get("config_id"), self._configs, "config change")
        if problem:
            return problem, "", set()
        text = (f"Config change {change['config_id']}\nService: {change.get('service', '?')}\n"
                f"Timestamp: {change.get('timestamp', '?')}\nKey: {change.get('key', '?')}\n"
                f"Old value: {change.get('old_value', '?')}\nNew value: {change.get('new_value', '?')}\n"
                f"Description: {change.get('description', '')}")
        return "", text, {change["config_id"]}

    def _get_infra_event(self, args: dict[str, Any]) -> tuple[str, str, set[str]]:
        problem, event = self._lookup(args.get("event_id"), self._infra, "infra event")
        if problem:
            return problem, "", set()
        text = (f"Infrastructure event {event['event_id']}\nType: {event.get('type', '?')}\n"
                f"Timestamp: {event.get('timestamp', '?')}\n"
                f"Description: {event.get('description', '')}")
        return "", text, {event["event_id"]}

    # --- grading (only meaningful when the incident has a known answer) ---

    def grade(self, cause: str, chain: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Score a diagnosis. ``chain`` hops may carry the ``evidence`` they
        cite. Returns ``None`` for real incidents, which have no known answer."""
        if not self.has_ground_truth:
            return None
        return evaluate(
            submitted_cause=cause,
            submitted_chain=chain,
            ground_truth=self._truth,
            retrieved_entities=self._retrieved,
            relevant_evidence=self._relevant_evidence,
            lookups=self.lookups,
            budget=self.max_calls,
        )
