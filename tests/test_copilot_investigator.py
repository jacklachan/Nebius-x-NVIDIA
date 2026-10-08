"""Investigator: triage loop, diagnosis, validation and grading, driven by a
scripted model so no network or API key is needed."""

import asyncio
import json

from copilot.config import ROLE_REASON, ROLE_TRIAGE
from copilot.investigator import EFFECT_TAXONOMY, Investigator
from copilot.llm import ChatResult, LLMError, UsageMeter
from copilot.workspace import Workspace
from data.seed_generator import generate_scenario


class ScriptedModel:
    """Replays canned replies per role and records what it was asked."""

    def __init__(self, triage=(), reason=(), fail_with=None):
        self.replies = {ROLE_TRIAGE: list(triage), ROLE_REASON: list(reason)}
        self.prompts = {ROLE_TRIAGE: [], ROLE_REASON: []}
        self.meter = UsageMeter()
        self.fail_with = fail_with

    async def chat(self, role, messages, max_tokens=1024, temperature=0.2):
        if self.fail_with:
            raise self.fail_with
        self.prompts[role].append(messages[-1]["content"])
        queue = self.replies[role]
        text = queue.pop(0) if queue else '{"thought": "nothing left", "tool": "done"}'
        if not isinstance(text, str):
            text = json.dumps(text)
        result = ChatResult(text=text, role=role, model=f"fake-{role}",
                            input_tokens=10, output_tokens=5, cost_usd=0.0, latency_s=0.0)
        self.meter.add(result)
        return result


def _run(investigator):
    async def collect():
        return [event async for event in investigator.run()]
    return asyncio.run(collect())


def _of(events, kind):
    return [e for e in events if e["type"] == kind]


def _scenario():
    return generate_scenario(7, "easy")


def _truth_diagnosis(scenario, **extra):
    truth = scenario["ground_truth"]
    return {
        "root_cause_ids": truth["cause"].split("+"),
        "summary": "The change exhausted a shared resource.",
        "confidence": 0.9,
        "chain": [{**hop, "because": "seen in logs", "evidence": ["E1"]}
                  for hop in truth["chain"]],
        "ruled_out": [],
        "open_questions": [],
        "more_evidence": [],
        **extra,
    }


def _lookup_culprit(scenario):
    cause = scenario["ground_truth"]["cause"].split("+")[0]
    if cause.startswith("cfg"):
        return {"thought": "read the config change", "tool": "get_config",
                "args": {"config_id": cause}}
    return {"thought": "read the diff", "tool": "get_commit",
            "args": {"commit_hash": cause}}


def test_correct_investigation_is_graded_as_correct():
    scenario = _scenario()
    model = ScriptedModel(
        triage=[_lookup_culprit(scenario), {"thought": "enough", "tool": "done"}],
        reason=[_truth_diagnosis(scenario)],
    )
    investigator = Investigator(Workspace(scenario), model)

    events = _run(investigator)

    assert [e["phase"] for e in _of(events, "phase")] == ["triage", "diagnosis"]
    assert _of(events, "evidence")[0]["id"] == "E1"
    grade = _of(events, "grade")[0]
    assert grade["cause_correct"]
    assert grade["score"] > 0.8
    done = events[-1]
    assert done["type"] == "done"
    assert done["diagnosis"]["cause"] == scenario["ground_truth"]["cause"]
    assert done["usage"]["by_role"][ROLE_TRIAGE]["calls"] == 2


def test_wrong_diagnosis_scores_low():
    scenario = _scenario()
    wrong = next(c["hash"] for c in scenario["commits"]
                 if c["hash"] != scenario["ground_truth"]["cause"])
    model = ScriptedModel(
        triage=[{"thought": "enough", "tool": "done"}],
        reason=[{"root_cause_ids": [wrong], "confidence": 0.9, "chain": []}],
    )

    events = _run(Investigator(Workspace(scenario), model))

    grade = _of(events, "grade")[0]
    assert not grade["cause_correct"]
    assert grade["score"] < 0.5


def test_diagnosis_never_sees_ground_truth_and_gets_the_taxonomy():
    scenario = _scenario()
    model = ScriptedModel(triage=[_lookup_culprit(scenario)],
                          reason=[_truth_diagnosis(scenario)])

    _run(Investigator(Workspace(scenario), model))

    prompt = model.prompts[ROLE_REASON][0]
    assert "ground_truth" not in prompt
    assert '"relevant"' not in prompt
    assert all(label in prompt for label in EFFECT_TAXONOMY)
    assert "[E1]" in prompt


def test_invented_ids_services_and_citations_are_dropped():
    scenario = _scenario()
    real = scenario["commits"][0]["hash"]
    service = next(iter(scenario["service_graph"]))
    model = ScriptedModel(
        triage=[{"thought": "look", "tool": "get_commit", "args": {"commit_hash": real}}],
        reason=[{
            "root_cause_ids": ["commit-made-up", real],
            "confidence": 7,
            "chain": [
                {"service": "imaginary-svc", "effect": "x", "evidence": ["E1"]},
                {"service": service, "effect": "upstream_timeout", "evidence": ["E1", "E99"]},
            ],
            "ruled_out": [{"id": "commit-made-up", "why": "n/a"}],
        }],
    )
    investigator = Investigator(Workspace(scenario), model)

    _run(investigator)

    d = investigator.diagnosis
    assert d.root_cause_ids == [real]
    assert d.confidence == 1.0
    assert [hop["service"] for hop in d.chain] == [service]
    assert d.chain[0]["evidence"] == ["E1"]
    assert d.ruled_out == []


def test_diagnosis_can_request_follow_up_evidence_once():
    scenario = _scenario()
    extra = scenario["commits"][1]["hash"]
    model = ScriptedModel(
        triage=[{"thought": "enough", "tool": "done"}],
        reason=[
            _truth_diagnosis(scenario, more_evidence=[
                {"tool": "get_commit", "args": {"commit_hash": extra}}]),
            _truth_diagnosis(scenario, more_evidence=[
                {"tool": "get_commit", "args": {"commit_hash": scenario["commits"][2]["hash"]}}]),
        ],
    )
    ws = Workspace(scenario)

    events = _run(Investigator(ws, model))

    follow_ups = [e for e in _of(events, "evidence") if e["role"] == ROLE_REASON]
    assert [e["args"]["commit_hash"] for e in follow_ups] == [extra]
    assert len(model.prompts[ROLE_REASON]) == 2
    assert "No further lookups" in model.prompts[ROLE_REASON][1]


def test_triage_gives_up_after_repeated_unusable_replies():
    scenario = _scenario()
    model = ScriptedModel(
        triage=["not json", "still not json", "nope", _lookup_culprit(scenario)],
        reason=[_truth_diagnosis(scenario)],
    )
    ws = Workspace(scenario)

    _run(Investigator(ws, model))

    assert len(model.prompts[ROLE_TRIAGE]) == 3
    assert not [e for e in ws.evidence if e.ok]
    assert "not usable" in model.prompts[ROLE_TRIAGE][1]


def test_triage_respects_step_cap():
    scenario = _scenario()
    lookups = [{"thought": "t", "tool": "get_commit", "args": {"commit_hash": c["hash"]}}
               for c in scenario["commits"]]
    model = ScriptedModel(triage=lookups, reason=[_truth_diagnosis(scenario)])
    ws = Workspace(scenario)

    _run(Investigator(ws, model, max_triage_steps=3))

    assert sum(1 for e in ws.evidence if e.ok) == 3


def test_unparseable_diagnosis_yields_an_empty_one_not_a_crash():
    scenario = _scenario()
    model = ScriptedModel(triage=[{"thought": "x", "tool": "done"}],
                          reason=["garbage", "more garbage"])
    investigator = Investigator(Workspace(scenario), model)

    events = _run(investigator)

    assert events[-1]["type"] == "done"
    assert investigator.diagnosis.cause == ""
    assert investigator.diagnosis.confidence == 0.0


def test_llm_failure_becomes_an_error_event():
    model = ScriptedModel(fail_with=LLMError("Token Factory 401: bad key", status=401))

    events = _run(Investigator(Workspace(_scenario()), model))

    assert events[-1] == {"type": "error", "message": "Token Factory 401: bad key",
                          "status": 401}


def test_ungraded_incident_finishes_without_a_grade():
    scenario = _scenario()
    answer = _truth_diagnosis(scenario)
    scenario.pop("ground_truth")
    model = ScriptedModel(triage=[{"thought": "x", "tool": "done"}], reason=[answer])

    events = _run(Investigator(Workspace(scenario), model))

    assert not _of(events, "grade")
    assert events[-1]["grade"] is None
    assert events[0]["graded"] is False
