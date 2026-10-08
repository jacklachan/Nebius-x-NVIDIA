"""The product app stands on its own; the original environment is an add-on."""

import ast
import builtins
import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
LEGACY = ("engine", "models", "openenv", "data.seed_generator", "web.runner",
          "web.agents", "web.training_loop", "web.lab", "training_utils", "train")
PRODUCT_FILES = sorted((ROOT / "copilot").glob("*.py")) + [
    ROOT / "data" / "incident_generator.py", ROOT / "web" / "copilot_api.py",
    ROOT / "web" / "mcp_mount.py"]


def _imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_product_code_never_imports_the_legacy_environment():
    offenders = [(path.name, module) for path in PRODUCT_FILES for module in _imports(path)
                 if any(module == name or module.startswith(name + ".") for name in LEGACY)]
    assert offenders == []


def test_lab_endpoints_still_work_when_the_runtime_is_installed():
    from app import LAB_AVAILABLE, app

    assert LAB_AVAILABLE
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "healthy"}
        assert client.get("/lab").status_code == 200
        reset = client.post("/reset", json={"task_id": "task1_recent_deploy"})
        assert reset.status_code == 200 and "observation" in reset.json()
        stepped = client.post("/step", json={"action": {
            "action_type": "query_logs", "service": "data", "keyword": "error"}})
        assert stepped.status_code == 200 and stepped.json()["done"] is False
        assert client.get("/state").json()["task_id"] == "task1_recent_deploy"
        assert "action" in client.get("/schema").json()
        assert client.get("/api/tasks").status_code == 200
        with client.websocket_connect("/ws") as socket:
            socket.close()


def test_product_runs_without_the_openenv_runtime(monkeypatch):
    """Simulate a machine where openenv-core is not installed."""
    real_import = builtins.__import__

    def without_openenv(name, *args, **kwargs):
        if name == "openenv" or name.startswith("openenv."):
            raise ImportError("No module named 'openenv'")
        return real_import(name, *args, **kwargs)

    saved = {name: module for name, module in sys.modules.items()
             if name == "app" or name.startswith(("openenv", "web.lab", "engine", "models"))}
    for name in saved:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(builtins, "__import__", without_openenv)

    bare = importlib.import_module("app")
    try:
        assert bare.LAB_AVAILABLE is False
        with TestClient(bare.app) as client:
            assert client.get("/").status_code == 200
            assert client.get("/health").status_code == 200
            assert client.get("/api/copilot/incidents").status_code == 200
            assert client.get("/api/copilot/benchmarks").status_code == 200
            assert client.get("/lab").status_code == 404
            assert client.get("/manifest.json").json()["lab"] is False
    finally:
        monkeypatch.undo()
        sys.modules.pop("app", None)
        sys.modules.update(saved)
