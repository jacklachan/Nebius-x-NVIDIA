"""Bundle builder: real git history and log files in, investigable incident out."""

import json
import os
import subprocess

import pytest

from copilot import incidents
from copilot.__main__ import main
from copilot.bundle import BundleError, build_bundle, describe, logs_from_file, parse_time
from copilot.workspace import Workspace

START, END = "2026-10-01T10:00:00Z", "2026-10-01T10:20:00Z"


def _git(repo, *args, when=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="Dev", GIT_AUTHOR_EMAIL="dev@example.com",
               GIT_COMMITTER_NAME="Dev", GIT_COMMITTER_EMAIL="dev@example.com")
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "api"
    path.mkdir()
    _git(path, "init", "-q")
    (path / "pool.py").write_text("MAX_CONNS = 100\n", encoding="utf-8")
    _git(path, "add", "."), _git(path, "commit", "-qm", "initial pool", when="2026-09-20T09:00:00Z")
    (path / "pool.py").write_text("MAX_CONNS = 10\n", encoding="utf-8")
    _git(path, "commit", "-qam", "tune pool size", when="2026-10-01T09:50:00Z")
    (path / "notes.txt").write_text("after the fact\n", encoding="utf-8")
    _git(path, "add", "."), _git(path, "commit", "-qm", "postmortem notes", when="2026-10-02T12:00:00Z")
    return path


@pytest.fixture
def log_file(tmp_path):
    path = tmp_path / "api.log"
    path.write_text("\n".join([
        "2026-09-01T00:00:00Z INFO far too old",
        "2026-10-01T09:58:00Z INFO request ok",
        "[2026-10-01 10:01:05,123] ERROR: pool exhausted",
        "Traceback (most recent call last):",
        '  File "pool.py", line 9, in acquire',
        "2026-10-01T15:31:10+05:30 WARNING slow acquire 900ms",
        '{"ts": "2026-10-01T10:02:00Z", "level": "fatal", "msg": "readiness failed"}',
        "this line has no timestamp and nothing above it in range to attach to",
        "2026-10-01T11:00:00Z ERROR after the incident",
        "garbage {{{",
    ]), encoding="utf-8")
    return path


def test_parse_time_normalises_to_utc():
    assert parse_time("2026-10-01T15:31:10+05:30").isoformat() == "2026-10-01T10:01:10+00:00"
    assert parse_time("2026-10-01 10:00:00").tzinfo is not None
    assert parse_time("2026-10-01T10:00:00+0530") == parse_time("2026-10-01T04:30:00Z")
    with pytest.raises(BundleError):
        parse_time("yesterday")


def test_logs_are_parsed_normalised_and_limited_to_the_window(log_file):
    since, until = parse_time("2026-09-30T10:00:00Z"), parse_time(END)

    entries = logs_from_file(log_file, "api", since, until)

    assert [(e["timestamp"], e["level"]) for e in entries] == [
        ("2026-10-01T09:58:00Z", "INFO"),
        ("2026-10-01T10:01:05Z", "ERROR"),
        ("2026-10-01T10:01:10Z", "WARN"),
        ("2026-10-01T10:02:00Z", "CRITICAL"),
    ]
    assert "Traceback" in entries[1]["message"] and "pool.py" in entries[1]["message"]
    assert "no timestamp" in entries[3]["message"]
    assert len({e["id"] for e in entries}) == 4


def test_bundle_from_real_git_history_is_investigable(repo, log_file, tmp_path):
    services = tmp_path / "services.json"
    services.write_text(json.dumps({"web": ["api"]}), encoding="utf-8")
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps({"config_changes": [{
        "config_id": "cfg-1", "service": "api", "timestamp": "2026-10-01T09:00:00Z",
        "key": "replicas", "old_value": "4", "new_value": "6", "description": "scale out"}]}),
        encoding="utf-8")

    bundle = build_bundle(START, END, repos={"api": repo}, logs={"api": log_file},
                          services_file=services, extra_file=extra,
                          description="Checkout returned 503s")

    # Only the commit inside the lookback window, with its real diff.
    assert [c["message"] for c in bundle["commits"]] == ["tune pool size"]
    commit = bundle["commits"][0]
    assert commit["hash"].startswith("commit-") and commit["timestamp"] == "2026-10-01T09:50:00Z"
    assert "-MAX_CONNS = 100" in commit["diff"] and "+MAX_CONNS = 10" in commit["diff"]
    assert bundle["service_graph"] == {"web": ["api"], "api": []}
    assert "No logs for: web" in describe(bundle)

    ws = Workspace(incidents.from_bundle(bundle))
    assert not ws.has_ground_truth
    assert ws.candidate_ids() == [commit["hash"], "cfg-1"]
    assert "+MAX_CONNS = 10" in ws.call("get_commit", {"commit_hash": commit["hash"]}).result
    during = ws.call("search_logs", {"service": "api", "keyword": "",
                                     "time_window": "during_incident"}).result
    assert "pool exhausted" in during and "request ok" not in during


def test_long_diffs_are_truncated(repo):
    (repo / "big.txt").write_text("x\n" * 20000, encoding="utf-8")
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "add fixture", when="2026-10-01T09:55:00Z")

    bundle = build_bundle(START, END, repos={"api": repo})

    big = next(c for c in bundle["commits"] if c["message"] == "add fixture")
    assert big["diff"].endswith("[diff truncated]") and len(big["diff"]) < 6100


@pytest.mark.parametrize("kwargs, fragment", [
    ({}, "Nothing to bundle"),
    ({"start": END, "end": START}, "must be after"),
    ({"start": "soon"}, "ISO-8601"),
])
def test_bad_inputs_are_explained(kwargs, fragment, log_file):
    args = {"start": START, "end": END, **kwargs}
    if "start" in kwargs:
        args["logs"] = {"api": log_file}
    with pytest.raises(BundleError) as exc:
        build_bundle(**args)
    assert fragment in str(exc.value)


def test_not_a_git_repo_is_explained(tmp_path):
    with pytest.raises(BundleError) as exc:
        build_bundle(START, END, repos={"api": tmp_path})
    assert "git log failed" in str(exc.value)


def test_bundle_with_no_changes_warns(log_file):
    bundle = build_bundle(START, END, logs={"api": log_file})
    assert "nothing it can name as a cause" in describe(bundle)


def test_cli_writes_a_bundle_and_reports_bad_arguments(repo, log_file, tmp_path, capsys):
    out = tmp_path / "out" / "incident.json"
    code = main(["bundle", "--start", START, "--end", END, "--repo", f"api={repo}",
                 "--logs", f"api={log_file}", "--out", str(out)])
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["commits"][0]["message"] == "tune pool size"
    assert "1 commits" in capsys.readouterr().out

    assert main(["bundle", "--start", START, "--end", END, "--repo", "no-equals-sign"]) == 1
    assert "NAME=PATH" in capsys.readouterr().err
