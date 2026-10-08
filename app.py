"""Hindsight web app.

    /                     the product UI
    /api/copilot/...      investigations, event streams, benchmarks
    /api/copilot/mcp      the same investigator for MCP clients
    /health               liveness

The original PostmortemEnv console and its endpoints are attached at ``/lab``
when the OpenEnv runtime is installed (see ``web/lab.py``). The product does
not depend on them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from web.copilot_api import BodyLimit
from web.copilot_api import router as copilot_router
from web.mcp_mount import install as install_mcp

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Hindsight",
    description="Investigates production incidents and writes the postmortem.",
    version="2.0.0",
)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "copilot" / "index.html")


@app.get("/manifest.json", include_in_schema=False)
async def manifest() -> dict:
    return {
        "name": "Hindsight",
        "short_name": "Hindsight",
        "description": "Investigates production incidents and writes the postmortem.",
        "version": app.version,
        "lab": LAB_AVAILABLE,
    }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(copilot_router)

try:
    from web.lab import attach as attach_lab
except ImportError as exc:  # the OpenEnv runtime is optional
    logging.getLogger(__name__).info("Lab not attached: %s", exc)
    LAB_AVAILABLE = False
else:
    attach_lab(app)
    LAB_AVAILABLE = True

install_mcp(app)
app.add_middleware(BodyLimit)
