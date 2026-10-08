"""Build an incident bundle from what a team already has.

    python -m copilot bundle \
        --start 2026-10-01T10:00:00Z --end 2026-10-01T10:20:00Z \
        --repo api=../api --repo web=../web \
        --logs api=logs/api.log --logs web=logs/web.log \
        --services services.json --out incident.json

Commits come from ``git log`` in each service's repository. Logs come from
plain-text or JSON-lines files. The service graph comes from a small JSON
file. Everything stays on this machine: the bundle is a local file, and the
only thing ever sent to a model is what the investigator looks up in it.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOOKBACK_HOURS = 24
MAX_DIFF_CHARS = 6000
MAX_LOG_LINES_PER_SERVICE = 4000
QUIET_LEVELS = {"DEBUG", "TRACE", "INFO"}

_RECORD, _FIELD = "\x1e", "\x1f"
_TEXT_LINE = re.compile(
    r"^\[?(?P<ts>\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?)\]?"
    r"\s+\[?(?P<level>[A-Za-z]+)\]?\s*:?\s+(?P<message>.*)$"
)
_LEVELS = {"WARNING": "WARN", "ERR": "ERROR", "FATAL": "CRITICAL", "CRIT": "CRITICAL",
           "INFORMATION": "INFO"}
_KNOWN_LEVELS = {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"}


class BundleError(ValueError):
    """The inputs cannot be turned into a bundle."""


def parse_time(value: str) -> datetime:
    """Parse an ISO-8601 time. A time without a zone is taken as UTC."""
    text = str(value).strip().replace(",", ".")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    # Accept +0530 as well as +05:30.
    text = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", text)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BundleError(f"Not an ISO-8601 time: {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def named_paths(pairs: list[str], what: str) -> dict[str, Path]:
    """Turn ['api=../api', ...] into {'api': Path('../api')}."""
    out: dict[str, Path] = {}
    for pair in pairs:
        name, sep, path = pair.partition("=")
        if not sep or not name.strip() or not path.strip():
            raise BundleError(f"--{what} takes NAME=PATH, got {pair!r}.")
        out[name.strip()] = Path(path.strip())
    return out


# --- commits ---------------------------------------------------------------

def commits_from_git(repo: Path, service: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Commits on the current branch of ``repo`` between ``since`` and ``until``."""
    command = [
        "git", "-C", str(repo), "log",
        f"--since={_iso(since)}", f"--until={_iso(until)}",
        "--no-color", "--patch", "--no-merges",
        "--pretty=format:%x1e%H%x1f%aI%x1f%ae%x1f%s%x1f",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=False)
    except OSError as exc:
        raise BundleError(f"Could not run git: {exc}") from exc
    if result.returncode != 0:
        raise BundleError(f"git log failed in {repo}: {result.stderr.strip()[:300]}")

    commits = []
    for record in result.stdout.split(_RECORD)[1:]:
        sha, when, author, subject, diff = record.split(_FIELD, 4)
        diff = diff.strip("\n")
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n...[diff truncated]"
        commits.append({
            "hash": f"commit-{sha[:10]}",
            "service": service,
            "timestamp": _iso(parse_time(when)),
            "author": author,
            "message": subject,
            "diff": diff or "(no textual changes)",
        })
    commits.sort(key=lambda c: c["timestamp"])
    return commits


# --- logs ------------------------------------------------------------------

def _level(raw: Any) -> str:
    level = str(raw or "INFO").upper()
    level = _LEVELS.get(level, level)
    return level if level in _KNOWN_LEVELS else "INFO"


def _parse_log_line(line: str) -> tuple[datetime, str, str] | None:
    line = line.rstrip("\n")
    if line.startswith("{"):
        try:
            row = json.loads(line)
            when = row.get("timestamp") or row.get("ts") or row.get("time") or row.get("@timestamp")
            message = row.get("message") or row.get("msg") or ""
            if when and message:
                return parse_time(when), _level(row.get("level") or row.get("severity")), str(message)
        except (ValueError, BundleError, AttributeError):
            return None
        return None
    match = _TEXT_LINE.match(line)
    if not match:
        return None
    try:
        return parse_time(match["ts"]), _level(match["level"]), match["message"]
    except BundleError:
        return None


def logs_from_file(path: Path, service: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Read one service's log file. Understands ``TIMESTAMP LEVEL message``
    lines and JSON lines. A line that is neither (a stack trace, say) is
    attached to the entry above it."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise BundleError(f"Could not read log file {path}: {exc}") from exc

    entries: list[dict[str, Any]] = []
    keep_previous = False
    for line in lines:
        parsed = _parse_log_line(line)
        if parsed is None:
            if keep_previous and line.strip() and len(entries[-1]["message"]) < 2000:
                entries[-1]["message"] += "\n" + line.rstrip()
            continue
        when, level, message = parsed
        keep_previous = since <= when <= until
        if keep_previous:
            entries.append({"timestamp": _iso(when), "level": level, "message": message})

    if len(entries) > MAX_LOG_LINES_PER_SERVICE:
        # Too much to carry: keep every warning and error, then the most
        # recent quiet lines until the cap is reached.
        loud = [e for e in entries if e["level"] not in QUIET_LEVELS]
        quiet = [e for e in entries if e["level"] in QUIET_LEVELS]
        room = max(0, MAX_LOG_LINES_PER_SERVICE - len(loud))
        entries = sorted(loud[-MAX_LOG_LINES_PER_SERVICE:] + (quiet[-room:] if room else []),
                         key=lambda e: e["timestamp"])
    for index, entry in enumerate(entries):
        entry["id"] = f"log-{service}-{index:05d}"
    return entries


# --- the bundle ------------------------------------------------------------

def build_bundle(
    start: str,
    end: str,
    repos: dict[str, Path] | None = None,
    logs: dict[str, Path] | None = None,
    services_file: Path | None = None,
    extra_file: Path | None = None,
    description: str = "",
    lookback_hours: int = LOOKBACK_HOURS,
) -> dict[str, Any]:
    repos, logs = repos or {}, logs or {}
    began, ended = parse_time(start), parse_time(end)
    if ended <= began:
        raise BundleError("--end must be after --start.")
    since = began - timedelta(hours=lookback_hours)

    graph: dict[str, list[str]] = {}
    if services_file:
        try:
            graph = json.loads(Path(services_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BundleError(f"Could not read services file {services_file}: {exc}") from exc
        if not isinstance(graph, dict) or not all(isinstance(v, list) for v in graph.values()):
            raise BundleError('The services file must look like {"web": ["api"], "api": []}.')
    for name in list(repos) + list(logs) + [d for deps in graph.values() for d in deps]:
        graph.setdefault(name, [])
    if not graph:
        raise BundleError("Nothing to bundle: give at least one --repo or --logs.")

    commits = [c for service, repo in repos.items()
               for c in commits_from_git(repo, service, since, ended)]
    commits.sort(key=lambda c: c["timestamp"])
    log_entries = {service: [] for service in graph}
    for service, path in logs.items():
        log_entries[service] = logs_from_file(path, service, since, ended)

    bundle: dict[str, Any] = {
        "task_id": f"incident-{began.strftime('%Y%m%d-%H%M')}",
        "task_name": description[:80] or f"Incident of {began.strftime('%Y-%m-%d %H:%M')} UTC",
        "task_description": description or (
            f"Incident from {_iso(began)} to {_iso(ended)}. "
            "Find what started it and how the failure spread."),
        "incident_window": {"start": _iso(began), "end": _iso(ended)},
        "service_graph": graph,
        "services": [{"name": name, "status": "unknown", "dependencies": deps,
                      "recent_deploy_count": sum(1 for c in commits if c["service"] == name)}
                     for name, deps in graph.items()],
        "logs": log_entries,
        "commits": commits,
        "config_changes": [],
        "infra_events": [],
        "traces": [],
    }

    if extra_file:
        try:
            extra = json.loads(Path(extra_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BundleError(f"Could not read extra file {extra_file}: {exc}") from exc
        for key in ("config_changes", "infra_events", "traces"):
            if isinstance(extra.get(key), list):
                bundle[key] = extra[key]
    return bundle


def describe(bundle: dict[str, Any]) -> str:
    """One paragraph on what ended up in the bundle."""
    lines = sum(len(v) for v in bundle["logs"].values())
    silent = [s for s, v in bundle["logs"].items() if not v]
    text = (f"{len(bundle['service_graph'])} services, {len(bundle['commits'])} commits, "
            f"{lines} log lines, {len(bundle['config_changes'])} config changes, "
            f"{len(bundle['infra_events'])} infrastructure events.")
    if silent:
        text += f" No logs for: {', '.join(silent)}."
    if not bundle["commits"] and not bundle["config_changes"] and not bundle["infra_events"]:
        text += (" Warning: the bundle contains no changes, so the investigator "
                 "will have nothing it can name as a cause.")
    return text
