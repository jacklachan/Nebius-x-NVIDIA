"""The evaluator: what each score rewards and what it refuses to reward."""

import pytest

from copilot.evaluate import (
    WEIGHTS,
    evaluate,
    score_efficiency,
    score_failure_modes,
    score_failure_path,
    score_grounding,
    score_root_cause,
)
from copilot.workspace import Workspace
from data.incident_generator import generate_incident

TRUTH = [{"service": "db", "effect": "connection_pool_exhaustion"},
         {"service": "api", "effect": "upstream_timeout"},
         {"service": "web", "effect": "5xx_errors_to_users"}]


def _hops(*names):
    return [{"service": name} for name in names]


def test_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


@pytest.mark.parametrize("submitted, truth, expected", [
    ("commit-a", "commit-a", 1.0),
    (" Commit-A ", "commit-a", 1.0),
    ("infra-1+commit-a", "commit-a+infra-1", 1.0),      # order does not matter
    ("commit-a", "commit-a+infra-1", 0.5),              # one of two joint causes
    ("commit-a+commit-b", "commit-a", 0.5),             # right one plus a wrong one
    ("commit-b", "commit-a", 0.0),
    ("", "commit-a", 0.0),
])
def test_root_cause(submitted, truth, expected):
    assert score_root_cause(submitted, truth) == expected


def test_failure_path_rewards_right_services_in_the_right_order():
    assert score_failure_path(_hops("db", "api", "web"), TRUTH) == 1.0
    assert score_failure_path(_hops("db", "db", "api", "web"), TRUTH) == 1.0   # repeats collapse
    assert score_failure_path(_hops("web", "api", "db"), TRUTH) == pytest.approx(1 / 3)
    assert score_failure_path(_hops("db", "web"), TRUTH) == pytest.approx(2 / 3)
    assert score_failure_path(_hops("db", "api", "web", "cdn", "dns", "lb"), TRUTH) == 0.5
    assert score_failure_path([], TRUTH) == 0.0


def test_failure_modes_need_the_right_label_on_the_right_service():
    assert score_failure_modes(TRUTH, TRUTH) == 1.0
    mislabelled = [dict(TRUTH[0], effect="oom_crash_loop"), TRUTH[1], TRUTH[2]]
    assert score_failure_modes(mislabelled, TRUTH) == pytest.approx(2 / 3)
    swapped = [dict(TRUTH[0], service="api"), dict(TRUTH[1], service="db"), TRUTH[2]]
    assert score_failure_modes(swapped, TRUTH) == pytest.approx(1 / 3)


def test_grounding_separates_an_investigation_from_a_lucky_guess():
    cited = [{"service": "db", "evidence": ["E1"]}, {"service": "api", "evidence": ["E9"]}]
    # Read the blamed change, and one of two hops cites a relevant exhibit.
    assert score_grounding("commit-a", cited, {"commit-a"}, {"E1"}) == 0.75
    # Named it without ever opening it.
    assert score_grounding("commit-a", cited, set(), {"E1"}) == 0.25
    # Opened it, but explained nothing.
    assert score_grounding("commit-a", [], {"commit-a"}, {"E1"}) == 0.5
    # A joint cause needs both parts read.
    assert score_grounding("commit-a+infra-1", [], {"commit-a"}, set()) == 0.0
    assert score_grounding("", [], set(), set()) == 0.0


def test_efficiency_is_free_for_a_quarter_of_the_budget_and_nothing_for_a_wrong_answer():
    assert score_efficiency(5, 40, 1.0) == 1.0
    assert score_efficiency(10, 40, 1.0) == 1.0
    assert score_efficiency(25, 40, 1.0) == pytest.approx(0.5)
    assert score_efficiency(40, 40, 1.0) == 0.0
    assert score_efficiency(2, 40, 0.0) == 0.0
    assert score_efficiency(2, 40, 0.5) == 0.5


def test_perfect_and_empty_answers_bracket_the_scale():
    truth = {"cause": "commit-a", "chain": TRUTH}
    chain = [dict(hop, evidence=["E1"]) for hop in TRUTH]
    perfect = evaluate("commit-a", chain, truth, {"commit-a"}, {"E1"}, lookups=3, budget=40)
    nothing = evaluate("", [], truth, set(), set(), lookups=0, budget=40)

    assert perfect["score"] == 1.0 and perfect["cause_correct"]
    assert nothing["score"] == 0.0 and not nothing["cause_correct"]
    assert [r["rubric"] for r in perfect["rubrics"]] == list(WEIGHTS)


def _read_cause(workspace, cause):
    tool, key = ("get_config", "config_id") if cause.startswith("cfg") else ("get_commit", "commit_hash")
    return workspace.call(tool, {key: cause})


def test_guessing_right_scores_well_below_investigating_right():
    """Through a real workspace: same correct answer, with and without the work."""
    incident = generate_incident(2, "easy")
    truth = incident["ground_truth"]
    cause = truth["cause"]

    guessed = Workspace(incident).grade(cause, [])

    worker = Workspace(incident)
    read = _read_cause(worker, cause)
    origin = truth["chain"][0]["service"]
    logs = worker.call("search_logs", {"service": origin, "level": "ERROR",
                                       "time_window": "during_incident"})
    worked = worker.grade(cause, [dict(hop, evidence=[read.id, logs.id]) for hop in truth["chain"]])

    assert guessed["cause_correct"] and worked["cause_correct"]
    assert guessed["score"] == pytest.approx(0.55)   # root cause + efficiency only
    assert worked["score"] == 1.0
    assert {r["rubric"]: r["raw_score"] for r in guessed["rubrics"]}["grounding"] == 0.0


def test_citing_irrelevant_exhibits_earns_no_grounding():
    incident = generate_incident(2, "easy")
    truth = incident["ground_truth"]
    ws = Workspace(incident)
    bystander = next(s["name"] for s in incident["services"] if s["status"] == "healthy")
    noise = ws.call("search_logs", {"service": bystander})
    grade = ws.grade(truth["cause"], [dict(hop, evidence=[noise.id]) for hop in truth["chain"]])
    scores = {r["rubric"]: r["raw_score"] for r in grade["rubrics"]}
    assert scores["grounding"] == 0.0 and scores["failure_modes"] == 1.0
