"""Incident sources, .env loading and the command line's failure paths."""

import json

import pytest

from copilot import incidents
from copilot.__main__ import main
from copilot.config import load_dotenv
from copilot.workspace import Workspace


def test_from_seed_is_deterministic():
    assert incidents.from_seed(3, "medium") == incidents.from_seed(3, "medium")


def test_from_seed_rejects_unknown_difficulty():
    with pytest.raises(incidents.IncidentError):
        incidents.from_seed(1, "impossible")


def test_from_task_rejects_unknown_task():
    with pytest.raises(incidents.IncidentError):
        incidents.from_task("task99_nope")


def test_bundle_with_only_required_sections_is_investigable(tmp_path):
    bundle = {
        "service_graph": {"web": ["db"], "db": []},
        "services": [{"name": "web"}, {"name": "db"}],
        "incident_window": {"start": "2026-10-01T10:00:00Z", "end": "2026-10-01T10:20:00Z"},
        "logs": {"db": [{"id": "l1", "timestamp": "2026-10-01T10:01:00Z",
                         "level": "ERROR", "message": "too many connections"}]},
    }
    path = tmp_path / "incident.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    ws = Workspace(incidents.from_file(path))

    assert not ws.has_ground_truth
    assert "too many connections" in ws.call(
        "search_logs", {"service": "db", "keyword": "error"}).result


def test_bundle_missing_sections_is_rejected():
    with pytest.raises(incidents.IncidentError) as exc:
        incidents.from_bundle({"logs": {}})
    assert "service_graph" in str(exc.value)


def test_unreadable_bundle_file_is_rejected(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(incidents.IncidentError):
        incidents.from_file(path)


def test_load_dotenv_does_not_override_real_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nCOPILOT_T_A=from-file\nCOPILOT_T_B='quoted'\nCOPILOT_T_C=\n",
                   encoding="utf-8")
    monkeypatch.setenv("COPILOT_T_A", "from-env")
    monkeypatch.delenv("COPILOT_T_B", raising=False)
    monkeypatch.delenv("COPILOT_T_C", raising=False)

    load_dotenv(env)

    import os
    assert os.environ["COPILOT_T_A"] == "from-env"
    assert os.environ["COPILOT_T_B"] == "quoted"
    assert "COPILOT_T_C" not in os.environ
    monkeypatch.delenv("COPILOT_T_B")


def test_cli_without_key_reports_the_problem_and_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    code = main(["investigate", "--seed", "1"])

    assert code == 1
    assert "NEBIUS_API_KEY" in capsys.readouterr().err


def test_cli_reports_unknown_task(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["investigate", "--task", "nope"]) == 1
    assert "Unknown task" in capsys.readouterr().err
