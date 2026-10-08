"""Where incidents come from.

Three sources share one shape (the scenario dict the environment consumes):
the hand-written benchmark tasks, procedurally generated seeds, and incident
bundles exported from a real system.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from data.generator import get_available_tasks, load_scenario
from data.seed_generator import generate_scenario

DIFFICULTIES = ("easy", "medium", "hard")
REQUIRED_KEYS = ("service_graph", "services", "incident_window", "logs")


class IncidentError(ValueError):
    """The requested incident does not exist or is not a valid bundle."""


def from_task(task_id: str) -> dict[str, Any]:
    if task_id not in get_available_tasks():
        raise IncidentError(
            f"Unknown task {task_id!r}. Available: {', '.join(get_available_tasks())}."
        )
    return load_scenario(task_id)


def from_seed(seed: int, difficulty: str = "easy") -> dict[str, Any]:
    if difficulty not in DIFFICULTIES:
        raise IncidentError(f"Difficulty must be one of {', '.join(DIFFICULTIES)}.")
    return generate_scenario(int(seed), difficulty)


def from_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate an uploaded incident bundle and fill in optional sections."""
    if not isinstance(bundle, dict):
        raise IncidentError("An incident bundle must be a JSON object.")
    missing = [k for k in REQUIRED_KEYS if k not in bundle]
    if missing:
        raise IncidentError(f"Incident bundle is missing: {', '.join(missing)}.")
    scenario = dict(bundle)
    for optional in ("traces", "commits", "config_changes", "infra_events"):
        scenario.setdefault(optional, [])
    scenario.setdefault("task_id", "uploaded-incident")
    scenario.setdefault("task_difficulty", "unknown")
    scenario.setdefault("task_description", "Investigate this incident.")
    scenario.setdefault("max_steps", 40)
    return scenario


def from_file(path: str | Path) -> dict[str, Any]:
    try:
        bundle = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IncidentError(f"Could not read incident bundle {path}: {exc}") from exc
    return from_bundle(bundle)
