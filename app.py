"""
PostmortemEnv FastAPI server using the official OpenEnv runtime.

The OpenEnv app provides the canonical WebSocket session endpoint at ``/ws``.
PostmortemEnv also preserves the original stateful HTTP contract used by the
repo's smoke tests and inference script:

  * POST /reset
  * POST /step
  * GET  /state
  * GET  /health
  * GET  /schema
  * GET  /metadata

The live console remains at ``/`` with streaming agent routes under ``/api``.
"""

from pathlib import Path
from typing import Any

from fastapi import Body
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from openenv.core.env_server import create_app

from models.action import Action
from models.observation import Observation
from models.state import EnvironmentState
from engine.environment import PostmortemEnvironment
from web.copilot_api import router as copilot_router
from web.runner import router as live_router

app = create_app(
    env=PostmortemEnvironment,
    action_cls=Action,
    observation_cls=Observation,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
_http_env = PostmortemEnvironment()


def _serialize_observation(obs: Observation) -> dict[str, Any]:
    data = obs.model_dump() if hasattr(obs, "model_dump") else vars(obs)
    reward = data.pop("reward", None)
    done = data.pop("done", False)
    data.pop("metadata", None)
    return {"observation": data, "reward": reward, "done": done}


def _remove_generated_routes(paths: set[str]) -> None:
    """Remove OpenEnv's stateless HTTP routes we intentionally replace."""
    kept = []
    for route in app.router.routes:
        if isinstance(route, APIRoute) and route.path in paths:
            continue
        kept.append(route)
    app.router.routes = kept


_remove_generated_routes({"/reset", "/step", "/state", "/schema", "/health", "/metadata"})


@app.post("/reset", tags=["Environment Control"])
async def reset(request: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    task_id = request.get("task_id", "task1_recent_deploy")
    seed = request.get("seed")
    episode_id = request.get("episode_id")
    obs = _http_env.reset(seed=seed, episode_id=episode_id, task_id=task_id)
    return _serialize_observation(obs)


@app.post("/step", tags=["Environment Control"])
async def step(request: dict[str, Any] = Body(...)) -> dict[str, Any]:
    action_data = request.get("action", request)
    if isinstance(action_data, dict):
        action_data = dict(action_data)
        action_data.pop("metadata", None)
    action = Action(**action_data)
    timeout_s = request.get("timeout_s") if isinstance(request, dict) else None
    obs = _http_env.step(action, timeout_s=timeout_s)
    return _serialize_observation(obs)


@app.get("/state", tags=["State Management"])
async def get_state() -> dict[str, Any]:
    state = _http_env.state
    return state.model_dump() if hasattr(state, "model_dump") else vars(state)


@app.get("/health", tags=["Health"])
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/schema", tags=["Schema"])
async def schema() -> dict[str, Any]:
    return {
        "action": Action.model_json_schema(),
        "observation": Observation.model_json_schema(),
        "state": EnvironmentState.model_json_schema(),
    }


@app.get("/metadata", tags=["Environment Info"])
async def metadata() -> dict[str, Any]:
    meta = _http_env.get_metadata()
    return meta.model_dump() if hasattr(meta, "model_dump") else vars(meta)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(live_router)
app.include_router(copilot_router)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/manifest.json", include_in_schema=False)
async def manifest() -> dict:
    return {
        "name": "PostmortemEnv",
        "short_name": "PostmortemEnv",
        "description": "Epistemic RL environment for cloud outage investigation",
        "version": "1.0.0",
        "endpoints": {
            "openenv": ["/health", "/reset", "/step", "/state", "/schema", "/ws"],
            "live": ["/api/tasks", "/api/curriculum", "/api/runs", "/api/stream/{run_id}"],
        },
    }
