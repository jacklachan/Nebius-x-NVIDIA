"""HTTP API for the incident copilot.

An investigation runs as a background task and pushes its events to any
number of Server-Sent-Events subscribers. Every event is also kept, so a
client that connects late (or reloads the page) replays the whole run.

Routes (all under /api/copilot):
    GET  /status                         is the server configured to run?
    GET  /incidents                      sample incidents to try
    GET  /benchmarks                     saved benchmark summaries
    POST /investigations                 start one; returns its id
    GET  /investigations                 recent investigations
    GET  /investigations/{id}            full record
    GET  /investigations/{id}/events     SSE stream
    GET  /investigations/{id}/postmortem.md
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from copilot import incidents
from copilot.config import ROLES, load_dotenv, load_settings
from copilot.investigator import Investigator
from copilot.llm import TokenFactoryClient
from copilot.research import Researcher, TavilyClient
from copilot.workspace import Workspace
from data.generator import get_available_tasks, load_scenario

router = APIRouter(prefix="/api/copilot", tags=["copilot"])

RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "copilot" / "recordings"
BENCHMARKS_DIR = Path(__file__).resolve().parent.parent / "benchmarks"
MAX_KEPT = 100
MAX_BUNDLE_BYTES = 2_000_000
# The hosted demo runs on the server's own key, so cap what a visitor can spend.
MAX_CONCURRENT = int(os.environ.get("COPILOT_MAX_CONCURRENT", "2"))
MAX_PER_DAY = int(os.environ.get("COPILOT_MAX_RUNS_PER_DAY", "200"))

# Generated incidents offered next to the hand-written tasks.
SAMPLE_SEEDS = [(42, "easy"), (7, "medium"), (2024, "medium"), (99, "hard")]

load_dotenv()


@dataclass
class Investigation:
    id: str
    title: str
    source: str
    status: str = "running"            # running | done | error
    recorded: bool = False
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    report: str = ""
    subscribers: list[asyncio.Queue] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        done = next((e for e in reversed(self.events) if e["type"] == "done"), None)
        grade = (done or {}).get("grade")
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "status": self.status,
            "recorded": self.recorded,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "cause": (done or {}).get("diagnosis", {}).get("cause"),
            "score": grade["score"] if grade else None,
            "cause_correct": grade["cause_correct"] if grade else None,
            "cost_usd": (done or {}).get("usage", {}).get("total_cost_usd"),
        }

    def record(self) -> dict[str, Any]:
        return {**self.summary(), "events": self.events, "report": self.report}


class _Store:
    def __init__(self) -> None:
        self.items: dict[str, Investigation] = {}
        self._day = ""
        self._started_today = 0

    def add(self, inv: Investigation) -> None:
        live = [i for i in self.items.values() if not i.recorded]
        if len(live) >= MAX_KEPT:
            oldest = min((i for i in live if i.status != "running"),
                         key=lambda i: i.started_at, default=None)
            if oldest:
                self.items.pop(oldest.id, None)
        self.items[inv.id] = inv

    def admit(self) -> None:
        """Raise 429 if starting another run would exceed the demo limits."""
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self._started_today = today, 0
        running = sum(1 for i in self.items.values() if i.status == "running")
        if running >= MAX_CONCURRENT:
            raise HTTPException(429, "Other investigations are running. Try again in a minute.")
        if self._started_today >= MAX_PER_DAY:
            raise HTTPException(429, "Daily demo limit reached. Open a recorded investigation instead.")
        self._started_today += 1

    def load_recordings(self) -> None:
        for path in sorted(RECORDINGS_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.items[data["id"]] = Investigation(
                    id=data["id"], title=data["title"], source=data["source"],
                    status=data["status"], recorded=True,
                    started_at=data["started_at"], ended_at=data["ended_at"],
                    events=data["events"], report=data.get("report", ""),
                )
            except (OSError, ValueError, KeyError):
                continue


store = _Store()
store.load_recordings()


def make_llm() -> TokenFactoryClient:
    """Seam for tests: swap this to drive the API with a scripted model."""
    return TokenFactoryClient()


def make_researcher(llm: Any) -> Researcher | None:
    key = load_settings().tavily_api_key
    return Researcher(llm, TavilyClient(key)) if key else None


class StartBody(BaseModel):
    source: Literal["task", "seed", "bundle"] = "seed"
    task_id: str | None = None
    seed: int = 42
    difficulty: str = "easy"
    bundle: dict[str, Any] | None = None


def _scenario_for(body: StartBody) -> tuple[dict[str, Any], str]:
    try:
        if body.source == "task":
            scenario = incidents.from_task(body.task_id or "")
            return scenario, scenario.get("task_name") or scenario["task_id"]
        if body.source == "bundle":
            if body.bundle is None:
                raise incidents.IncidentError("source 'bundle' needs a bundle.")
            if len(json.dumps(body.bundle)) > MAX_BUNDLE_BYTES:
                raise incidents.IncidentError("Incident bundle is larger than 2 MB.")
            scenario = incidents.from_bundle(body.bundle)
            # An uploaded incident is investigated, never graded.
            scenario.pop("ground_truth", None)
            scenario.pop("relevant_fact_ids", None)
            return scenario, str(scenario.get("task_name") or scenario["task_id"])
        scenario = incidents.from_seed(body.seed, body.difficulty)
        return scenario, scenario["task_name"]
    except incidents.IncidentError as exc:
        raise HTTPException(400, str(exc)) from exc


async def _push(inv: Investigation, event: dict[str, Any]) -> None:
    inv.events.append(event)
    for queue in list(inv.subscribers):
        await queue.put(event)


async def _run(inv: Investigation, investigator: Investigator) -> None:
    try:
        async for event in investigator.run():
            await _push(inv, event)
            if event["type"] == "report":
                inv.report = event["markdown"]
            if event["type"] == "error":
                inv.status = "error"
        if inv.status == "running":
            inv.status = "done"
    except Exception as exc:  # noqa: BLE001 - a crash must still close the stream
        inv.status = "error"
        await _push(inv, {"type": "error", "message": f"Investigation crashed: {exc}"})
    finally:
        inv.ended_at = time.time()
        for queue in inv.subscribers:
            await queue.put(None)


def _get(investigation_id: str) -> Investigation:
    inv = store.items.get(investigation_id)
    if inv is None:
        raise HTTPException(404, "Investigation not found.")
    return inv


@router.get("/status")
async def status() -> dict[str, Any]:
    settings = load_settings()
    return {
        "ready": bool(settings.api_key),
        "research": bool(settings.tavily_api_key),
        "models": {role: settings.model_for(role) for role in ROLES},
        "recordings": sum(1 for i in store.items.values() if i.recorded),
    }


@router.get("/incidents")
async def list_incidents() -> list[dict[str, Any]]:
    items = []
    # Generated incidents first: their telemetry is coherent and their answer
    # is not signposted. The hand-written tasks come from the original
    # environment and are kept for comparison.
    for seed, difficulty in SAMPLE_SEEDS:
        scenario = incidents.from_seed(seed, difficulty)
        items.append({
            "source": "seed", "seed": seed, "difficulty": difficulty,
            "title": scenario["task_name"],
            "description": scenario["task_description"],
            "services": len(scenario["service_graph"]),
        })
    for task_id in get_available_tasks():
        scenario = load_scenario(task_id)
        items.append({
            "source": "task", "task_id": task_id,
            "title": scenario.get("task_name") or task_id,
            "difficulty": scenario.get("task_difficulty", "?"),
            "description": scenario.get("task_description", ""),
            "services": len(scenario.get("service_graph", {})),
        })
    return items


@router.get("/benchmarks")
async def benchmarks() -> list[dict[str, Any]]:
    """Summaries of saved benchmark runs (`python -m copilot bench --out ...`)."""
    out = []
    for path in sorted(BENCHMARKS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append({
                "label": data["label"], "kind": data.get("kind", "model"),
                "about": data.get("about", ""), "difficulty": data["difficulty"],
                "models": data.get("models", {}), "summary": data["summary"],
            })
        except (OSError, ValueError, KeyError):
            continue
    return out


@router.post("/investigations")
async def start(body: StartBody) -> dict[str, str]:
    if not load_settings().api_key:
        raise HTTPException(
            503, "This server has no NEBIUS_API_KEY, so it cannot run new "
                 "investigations. Recorded investigations still open.")
    scenario, title = _scenario_for(body)
    store.admit()
    llm = make_llm()
    investigator = Investigator(Workspace(scenario), llm, researcher=make_researcher(llm))
    inv = Investigation(id=uuid.uuid4().hex[:12], title=title, source=body.source)
    store.add(inv)
    asyncio.create_task(_run(inv, investigator))
    return {"id": inv.id}


@router.get("/investigations")
async def list_investigations() -> list[dict[str, Any]]:
    items = sorted(store.items.values(), key=lambda i: i.started_at, reverse=True)
    return [i.summary() for i in items]


@router.get("/investigations/{investigation_id}")
async def get_investigation(investigation_id: str) -> dict[str, Any]:
    return _get(investigation_id).record()


@router.get("/investigations/{investigation_id}/postmortem.md")
async def postmortem(investigation_id: str) -> PlainTextResponse:
    inv = _get(investigation_id)
    if not inv.report:
        raise HTTPException(409, "This investigation has not produced a postmortem yet.")
    return PlainTextResponse(
        inv.report,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="postmortem-{inv.id}.md"'},
    )


@router.get("/investigations/{investigation_id}/events")
async def events(investigation_id: str, request: Request) -> StreamingResponse:
    inv = _get(investigation_id)
    queue: asyncio.Queue = asyncio.Queue()
    inv.subscribers.append(queue)
    backlog = list(inv.events)
    finished = inv.status != "running"

    async def stream() -> AsyncGenerator[bytes, None]:
        try:
            for event in backlog:
                yield _sse(event)
            if finished:
                yield _sse({"type": "_eof"})
                return
            while True:
                if await request.is_disconnected():
                    break
                event = await queue.get()
                if event is None:
                    yield _sse({"type": "_eof"})
                    break
                yield _sse(event)
        finally:
            if queue in inv.subscribers:
                inv.subscribers.remove(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


class BodyLimit:
    """Refuse oversized uploads before they are read into memory.

    Pure ASGI rather than a framework middleware so that streaming responses
    (the event streams) pass through untouched.
    """

    def __init__(self, app: Any, prefix: str = "/api/copilot",
                 max_bytes: int = 2 * MAX_BUNDLE_BYTES) -> None:
        self.app, self.prefix, self.max_bytes = app, prefix, max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"].startswith(self.prefix):
            length = dict(scope["headers"]).get(b"content-length", b"0")
            if length.isdigit() and int(length) > self.max_bytes:
                body = json.dumps({"detail": "Request body is too large."}).encode()
                await send({"type": "http.response.start", "status": 413,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def _sse(event: dict[str, Any]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n".encode()
