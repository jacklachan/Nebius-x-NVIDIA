"""Serve the MCP server from the main web app at ``/api/copilot/mcp``.

(``/mcp`` itself belongs to the OpenEnv runtime the environment is built on.)

One deployment then offers the UI, the HTTP API and MCP on the same URL.
Only the remote-safe tools are exposed here (nothing that reads local paths).

Host checking: the MCP transport rejects requests whose ``Host`` header it
does not expect, which protects a locally running server from DNS-rebinding
attacks. Local hosts are allowed by default. A hosted deployment must list
its public hostname in ``COPILOT_MCP_ALLOWED_HOSTS`` (comma-separated), or
set it to ``*`` to turn the check off behind a trusted proxy.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

import web.copilot_api as api
from copilot.mcp_server import create_server

MCP_PATH = "/api/copilot/mcp"
LOCAL_HOSTS = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "testserver"]


def transport_security() -> TransportSecuritySettings:
    raw = os.environ.get("COPILOT_MCP_ALLOWED_HOSTS", "").strip()
    if raw == "*":
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    extra = [h.strip() for h in raw.split(",") if h.strip()]
    hosts = LOCAL_HOSTS + extra
    origins = [f"{scheme}://{h}" for h in hosts for scheme in ("http", "https")]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


_running = 0


@asynccontextmanager
async def spend_guard():
    """Hold MCP callers to the same limits as the web UI: both spend the
    server's own Token Factory key."""
    global _running
    if _running >= api.MAX_CONCURRENT:
        raise ToolError("Other investigations are running. Try again in a minute.")
    try:
        api.store.admit()
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from exc
    _running += 1
    try:
        yield
    finally:
        _running -= 1


class _Gateway:
    """ASGI entry point that forwards to the MCP app of the current lifespan.

    The MCP session manager can be started only once per instance, while a
    web app's lifespan may run many times (every test client, every reload),
    so a fresh MCP app is built per lifespan and swapped in here.
    """

    def __init__(self) -> None:
        self.app: Any = None

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if self.app is None:
            response = JSONResponse({"detail": "MCP endpoint is starting."}, status_code=503)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def install(app: FastAPI, path: str = MCP_PATH) -> None:
    gateway = _Gateway()
    outer = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance: FastAPI):
        server = create_server(local_files=False, guard=spend_guard)
        mcp_app = server.streamable_http_app(
            streamable_http_path=path, transport_security=transport_security())
        async with outer(instance):
            async with server.session_manager.run():
                gateway.app = mcp_app
                try:
                    yield
                finally:
                    gateway.app = None

    app.router.lifespan_context = lifespan
    # A plain route, not a mount: a mount would answer POST {path} with a
    # redirect to {path}/, which not every MCP client follows.
    app.router.routes.append(
        Route(path, endpoint=gateway, methods=["GET", "POST", "DELETE"]))
