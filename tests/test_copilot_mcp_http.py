"""The MCP endpoint served by the main web app."""

import json

import pytest
from fastapi.testclient import TestClient

from app import app
from web.mcp_mount import MCP_PATH, transport_security

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(client, method, params=None, session=None, request_id=1, host=None):
    headers = dict(HEADERS)
    if session:
        headers["Mcp-Session-Id"] = session
    if host:
        headers["Host"] = host
    body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    return client.post(MCP_PATH, json=body, headers=headers, follow_redirects=False)


def _result(response):
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return response.json()


INIT = {"protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}}


@pytest.mark.parametrize("round_", [1, 2])
def test_initialize_and_list_tools_over_http(round_):
    """Runs twice: the endpoint must survive the app starting more than once."""
    with TestClient(app) as client:
        started = _rpc(client, "initialize", INIT)
        assert started.status_code == 200
        info = _result(started)["result"]
        assert info["serverInfo"]["name"] == "hindsight"
        assert info["protocolVersion"] >= "2025-11-25"

        session = started.headers.get("mcp-session-id")
        client.post(MCP_PATH, headers={**HEADERS, **({"Mcp-Session-Id": session} if session else {})},
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = _result(_rpc(client, "tools/list", session=session, request_id=2))["result"]["tools"]
        names = {t["name"] for t in tools}
        assert {"investigate_incident", "investigate_sample", "list_sample_incidents"} <= names
        assert not names & {"build_incident_bundle", "investigate_bundle_file"}


def test_unexpected_host_is_refused():
    with TestClient(app) as client:
        assert _rpc(client, "initialize", INIT, host="evil.example").status_code == 421


def test_rest_of_the_app_is_unaffected():
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/copilot/status").status_code == 200


def test_allowed_hosts_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("COPILOT_MCP_ALLOWED_HOSTS", "demo.example.com, other.example")
    settings = transport_security()
    assert settings.enable_dns_rebinding_protection
    assert "demo.example.com" in settings.allowed_hosts and "localhost" in settings.allowed_hosts
    assert "https://demo.example.com" in settings.allowed_origins

    monkeypatch.setenv("COPILOT_MCP_ALLOWED_HOSTS", "*")
    assert not transport_security().enable_dns_rebinding_protection
