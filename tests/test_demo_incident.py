"""The reproduced checkout outage (scripts/make_demo_incident.py) must be
solvable from its repos and logs, and must not reward blaming the latest change."""

import importlib.util
import shutil
from pathlib import Path

import pytest

from copilot import incidents
from copilot.oracle import Oracle

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "make_demo_incident.py"
_spec = importlib.util.spec_from_file_location("make_demo_incident", _SCRIPT)
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return incidents.from_bundle(demo.build(tmp_path_factory.mktemp("demo") / "incident"))


def test_oracle_solves_it_from_the_evidence(bundle):
    oracle = Oracle(bundle)
    oracle.investigate()
    assert oracle.unsupported == []
    assert oracle.grade["score"] == 1.0


def test_culprit_is_a_real_commit_but_not_the_latest(bundle):
    commits = bundle["commits"]
    culprit = bundle["ground_truth"]["cause"]
    by_hash = {c["hash"]: c for c in commits}
    assert by_hash[culprit]["message"] == demo.CULPRIT_MESSAGE
    assert "with engine.begin() as conn" in by_hash[culprit]["diff"]
    assert commits[-1]["hash"] != culprit
    assert commits[-1]["service"] == "web"


def test_the_two_day_old_history_is_outside_the_bundle(bundle):
    assert all(not c["message"].startswith("Initial") for c in bundle["commits"])
