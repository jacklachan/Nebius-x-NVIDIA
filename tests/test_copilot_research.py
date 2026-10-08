"""Research step: generic queries only, references into the postmortem."""

import asyncio
import json

import httpx
import pytest

from copilot.config import ROLE_TRIAGE, ROLE_WRITER
from copilot.core import Diagnosis
from copilot.investigator import Investigator
from copilot.llm import ChatResult, UsageMeter
from copilot.research import (
    Researcher,
    ResearchError,
    TavilyClient,
    private_terms,
    safe_queries,
)
from copilot.workspace import Workspace
from data.seed_generator import generate_scenario


class Model:
    """Replies with one canned JSON object whatever the role."""

    def __init__(self, reply):
        self.reply, self.meter, self.prompts = reply, UsageMeter(), []

    async def chat(self, role, messages, max_tokens=1024, temperature=0.2):
        self.prompts.append((role, messages[-1]["content"]))
        result = ChatResult(text=json.dumps(self.reply), role=role, model=f"fake-{role}",
                            input_tokens=1, output_tokens=1, cost_usd=0.0, latency_s=0.0)
        self.meter.add(result)
        return result


def _tavily(handler):
    return TavilyClient("tvly-test", transport=httpx.MockTransport(handler))


def _hits(request):
    query = json.loads(request.content)["query"]
    return httpx.Response(200, json={"results": [
        {"title": f"Guide to {query}", "url": f"https://example.org/{abs(hash(query))}",
         "content": "Size   pools from\nmeasured concurrency."},
        {"title": "Shared", "url": "https://example.org/shared", "content": "dup"},
        {"title": "Not a link", "url": "javascript:alert(1)", "content": "x"},
    ]})


def _brief_and_diagnosis():
    scenario = generate_scenario(7, "easy")
    ws = Workspace(scenario)
    diagnosis = Diagnosis(
        root_cause_ids=[scenario["ground_truth"]["cause"].split("+")[0]],
        summary="A cache without eviction leaked memory until the service stalled.",
        confidence=0.9,
        chain=[{"service": scenario["ground_truth"]["chain"][0]["service"],
                "effect": "memory_leak_gc_pressure", "because": "Heap grew.", "evidence": []}],
    )
    return scenario, ws, diagnosis


def test_private_terms_cover_every_incident_identifier():
    scenario, ws, _ = _brief_and_diagnosis()
    terms = private_terms(ws.brief())

    assert scenario["commits"][0]["hash"].lower() in terms
    assert scenario["config_changes"][0]["config_id"].lower() in terms
    assert next(iter(scenario["service_graph"])).lower() in terms
    assert "seed_7_easy" in terms


def test_queries_naming_internal_things_are_dropped():
    scenario, ws, _ = _brief_and_diagnosis()
    service = next(iter(scenario["service_graph"]))
    commit = scenario["commits"][0]["hash"]

    kept = safe_queries(
        [
            "unbounded in-memory cache memory leak GC pressure mitigation",
            f"{service} memory leak fix",
            f"why did {commit.upper()} break production",
            "ask heidi@company.com about the leak",
            "short",
            42,
            "unbounded in-memory cache memory leak GC pressure mitigation",
            "detecting memory leaks with heap growth alerts",
            "a third perfectly generic query about timeouts",
        ],
        ws.brief(),
    )

    assert kept == [
        "unbounded in-memory cache memory leak GC pressure mitigation",
        "detecting memory leaks with heap growth alerts",
    ]


def test_researcher_sends_only_safe_queries_and_numbers_references():
    scenario, ws, diagnosis = _brief_and_diagnosis()
    service = next(iter(scenario["service_graph"]))
    sent = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer tvly-test"
        sent.append(json.loads(request.content)["query"])
        return _hits(request)

    model = Model({"queries": ["memory leak gc pressure latency cascade",
                               f"{service} outage root cause",
                               "heap growth alerting best practice"]})
    researcher = Researcher(model, _tavily(handler))

    refs = asyncio.run(researcher.run(ws.brief(), diagnosis))

    assert sent == ["memory leak gc pressure latency cascade",
                    "heap growth alerting best practice"]
    assert researcher.dropped == 1
    assert [r.id for r in refs] == ["R1", "R2", "R3"]
    assert [r.url for r in refs].count("https://example.org/shared") == 1
    assert refs[0].snippet == "Size pools from measured concurrency."
    role, prompt = model.prompts[0]
    assert role == ROLE_TRIAGE
    assert service not in prompt.split("SUMMARY")[0]


def test_no_search_without_a_diagnosed_cause():
    _, ws, _ = _brief_and_diagnosis()

    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("no search expected")

    refs = asyncio.run(Researcher(Model({}), _tavily(handler)).run(ws.brief(), Diagnosis()))
    assert refs == []


def test_provider_failure_raises_research_error():
    client = _tavily(lambda request: httpx.Response(401, text="invalid key"))
    with pytest.raises(ResearchError) as exc:
        asyncio.run(client.search("anything generic"))
    assert "401" in str(exc.value)


def _run(investigator):
    async def collect():
        return [event async for event in investigator.run()]
    return asyncio.run(collect())


def _one_reply(scenario):
    """A single object that satisfies triage, diagnosis, query and writer prompts."""
    return {
        "thought": "stop", "tool": "done",
        "root_cause_ids": scenario["ground_truth"]["cause"].split("+"),
        "summary": "It leaked.", "confidence": 0.9, "chain": [],
        "queries": ["memory leak gc pressure latency cascade"],
        "title": "Cache leak stalled notifications",
        "action_items": [
            {"action": "Bound the cache", "priority": "P0",
             "why": "Prevents the leak [R1] and see also [R9]."}],
    }


def test_references_reach_the_writer_and_the_document():
    scenario = generate_scenario(7, "easy")
    model = Model(_one_reply(scenario))
    investigator = Investigator(
        Workspace(scenario), model, researcher=Researcher(model, _tavily(_hits)))

    events = _run(investigator)

    phases = [e["phase"] for e in events if e["type"] == "phase"]
    assert phases == ["triage", "diagnosis", "research", "report"]
    research = next(e for e in events if e["type"] == "research")
    assert research["queries"] == ["memory leak gc pressure latency cascade"]
    writer_prompt = next(p for role, p in model.prompts if role == ROLE_WRITER)
    assert "[R1] Guide to memory leak" in writer_prompt
    assert "## References" in investigator.report
    assert "- **[R1]** [Guide to memory leak" in investigator.report
    assert "Prevents the leak [R1] and see also." in investigator.report
    assert events[-1]["references"][0]["id"] == "R1"


def test_search_outage_degrades_to_a_report_without_references():
    scenario = generate_scenario(7, "easy")
    model = Model(_one_reply(scenario))
    down = _tavily(lambda request: httpx.Response(503, text="down"))
    investigator = Investigator(Workspace(scenario), model, researcher=Researcher(model, down))

    events = _run(investigator)

    assert any(e["type"] == "warning" and "Research skipped" in e["message"] for e in events)
    assert events[-1]["type"] == "done"
    assert "## References" not in investigator.report
    assert "[R1]" not in investigator.report


def test_no_researcher_means_no_research_phase():
    scenario = generate_scenario(7, "easy")
    events = _run(Investigator(Workspace(scenario), Model(_one_reply(scenario))))
    assert "research" not in [e.get("phase") for e in events]
