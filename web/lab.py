"""The original PostmortemEnv, attached to the product app as an add-on.

Hindsight grew out of PostmortemEnv, a reinforcement-learning environment
built on the OpenEnv runtime. Its console (baselines, curriculum, live
training) is kept at ``/lab`` together with the endpoints its scripts use:

    POST /reset, POST /step, GET /state, GET /schema, GET /metadata, /ws

None of this is needed to investigate an incident. If ``openenv-core`` is not
installed, ``attach`` is simply not called and the product runs without it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, FastAPI
from fastapi.responses import FileResponse
from openenv.core.env_server import create_app

from engine.environment import PostmortemEnvironment
from models.action import Action
from models.observation import Observation
from models.state import EnvironmentState
from web.runner import router as live_router

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
# Routes the OpenEnv runtime provides itself and the lab still needs.
OPENENV_PATHS = {"/ws", "/mcp"}


def _serialize(obs: Observation) -> dict[str, Any]:
    data = obs.model_dump() if hasattr(obs, "model_dump") else vars(obs)
    reward = data.pop("reward", None)
    done = data.pop("done", False)
    data.pop("metadata", None)
    return {"observation": data, "reward": reward, "done": done}


def attach(app: FastAPI) -> None:
    env = PostmortemEnvironment()
    router = APIRouter(tags=["lab"])

    @router.post("/reset")
    async def reset(request: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        obs = env.reset(seed=request.get("seed"), episode_id=request.get("episode_id"),
                        task_id=request.get("task_id", "task1_recent_deploy"))
        return _serialize(obs)

    @router.post("/step")
    async def step(request: dict[str, Any] = Body(...)) -> dict[str, Any]:
        action_data = request.get("action", request)
        if isinstance(action_data, dict):
            action_data = dict(action_data)
            action_data.pop("metadata", None)
        obs = env.step(Action(**action_data), timeout_s=request.get("timeout_s"))
        return _serialize(obs)

    @router.get("/state")
    async def state() -> dict[str, Any]:
        current = env.state
        return current.model_dump() if hasattr(current, "model_dump") else vars(current)

    @router.get("/schema")
    async def schema() -> dict[str, Any]:
        return {
            "action": Action.model_json_schema(),
            "observation": Observation.model_json_schema(),
            "state": EnvironmentState.model_json_schema(),
        }

    @router.get("/metadata")
    async def metadata() -> dict[str, Any]:
        meta = env.get_metadata()
        return meta.model_dump() if hasattr(meta, "model_dump") else vars(meta)

    @router.get("/lab", include_in_schema=False)
    async def lab() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.include_router(router)
    app.include_router(live_router)

    # The session endpoints come from the OpenEnv runtime; borrow them, and
    # its startup hooks, from an app it builds.
    runtime = create_app(env=PostmortemEnvironment, action_cls=Action, observation_cls=Observation)
    app.router.routes.extend(
        route for route in runtime.router.routes if getattr(route, "path", "") in OPENENV_PATHS)
    app.router.on_startup.extend(runtime.router.on_startup)
    app.router.on_shutdown.extend(runtime.router.on_shutdown)
