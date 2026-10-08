"""Hindsight as an MCP server.

Any MCP client (a coding agent in your editor, a chat assistant, another
service) can hand Hindsight an incident and get the diagnosis and postmortem
back as a tool result.

    python -m copilot mcp                    # stdio, for a local coding agent
    python -m copilot mcp --http --port 8765 # Streamable HTTP at /mcp

Over stdio the server runs on your machine with your files, so it also offers
tools that read local paths: build a bundle from git repositories and log
files, and investigate a bundle file. Over HTTP those tools are not
registered; a remote caller passes the bundle itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from copilot import incidents
from copilot.bundle import BundleError, build_bundle, describe
from copilot.config import load_settings
from copilot.investigator import Investigator
from copilot.llm import TokenFactoryClient
from copilot.research import Researcher, TavilyClient
from copilot.workspace import Workspace
from data.generator import get_available_tasks, load_scenario

INSTRUCTIONS = """Hindsight investigates production incidents from their telemetry and writes the postmortem.

Use investigate_incident with a bundle (logs, commits, config changes, infrastructure events, traces around the incident). It reads the evidence, names the change that started the outage, traces how the failure spread, and returns a postmortem in Markdown. Every claim cites evidence IDs (E1, E2, ...) that appear in the document's evidence section.

It can only name a cause that is present in the bundle. If root_cause is empty, the evidence was not enough: read open_questions and add the missing telemetry rather than guessing."""

PHASES = {"triage": 1, "diagnosis": 2, "research": 3, "report": 4}


def make_llm() -> Any:
    """Seam for tests."""
    return TokenFactoryClient()


async def _investigate(scenario: dict[str, Any], ctx: Context | None) -> dict[str, Any]:
    llm = make_llm()
    key = load_settings().tavily_api_key
    researcher = Researcher(llm, TavilyClient(key)) if key else None
    investigator = Investigator(Workspace(scenario), llm, researcher=researcher)

    lookups = 0
    async for event in investigator.run():
        kind = event["type"]
        if kind == "error":
            raise ToolError(f"The investigation stopped: {event['message']}")
        if ctx is None:
            continue
        if kind == "phase":
            await ctx.report_progress(PHASES.get(event["phase"], 0), len(PHASES),
                                      f"Stage: {event['phase']}")
        elif kind == "evidence" and event["ok"]:
            lookups += 1
            await ctx.info(f"{event['id']}: {event['tool']}({event['args']})")

    diagnosis, grade = investigator.diagnosis, investigator.grade
    assert diagnosis is not None
    result: dict[str, Any] = {
        "root_cause": diagnosis.cause,
        "confidence": diagnosis.confidence,
        "summary": diagnosis.summary,
        "how_it_spread": [
            {"service": hop["service"], "effect": hop["effect"],
             "because": hop["because"], "evidence": hop["evidence"]}
            for hop in diagnosis.chain],
        "ruled_out": diagnosis.ruled_out,
        "open_questions": diagnosis.open_questions,
        "evidence_lookups": sum(1 for e in investigator.ws.evidence if e.ok),
        "cost_usd": llm.meter.total_cost(),
        "postmortem_markdown": investigator.report,
    }
    if grade is not None:
        result["graded"] = {"score": grade["score"], "root_cause_correct": grade["cause_correct"]}
    return result


def create_server(local_files: bool) -> MCPServer:
    """Build the server. ``local_files`` adds the tools that read paths on
    this machine; only enable it when the caller is the machine's own user."""
    server = MCPServer(
        "hindsight",
        title="Hindsight",
        description="Investigates production incidents and writes the postmortem.",
        instructions=INSTRUCTIONS,
        website_url="https://github.com/jacklachan/Nebius-x-NVIDIA",
    )

    @server.tool(title="List sample incidents")
    def list_sample_incidents() -> list[dict[str, Any]]:
        """Sample incidents with a known answer, for trying Hindsight out.
        Investigate one with investigate_sample."""
        items = []
        for task_id in get_available_tasks():
            scenario = load_scenario(task_id)
            items.append({"task_id": task_id, "title": scenario.get("task_name") or task_id,
                          "description": scenario.get("task_description", "")})
        items.append({"seed": "any integer", "difficulty": list(incidents.DIFFICULTIES),
                      "title": "Generated incident",
                      "description": "A fresh incident for every seed and difficulty."})
        return items

    @server.tool(title="Investigate a sample incident")
    async def investigate_sample(
        ctx: Context, seed: int = 42, difficulty: str = "medium", task_id: str | None = None,
    ) -> dict[str, Any]:
        """Investigate a sample incident and grade the result against its known
        answer. Pass task_id for a hand-written incident, or seed and difficulty
        (easy, medium, hard) for a generated one."""
        try:
            scenario = (incidents.from_task(task_id) if task_id
                        else incidents.from_seed(seed, difficulty))
        except incidents.IncidentError as exc:
            raise ToolError(str(exc)) from exc
        return await _investigate(scenario, ctx)

    @server.tool(title="Investigate an incident")
    async def investigate_incident(ctx: Context, bundle: dict[str, Any]) -> dict[str, Any]:
        """Investigate a real incident. ``bundle`` is a JSON object with
        service_graph, services, incident_window and logs (required) and
        commits, config_changes, infra_events and traces (optional but
        needed to name a cause). Returns the root cause, how the failure
        spread, open questions and the postmortem as Markdown."""
        try:
            scenario = incidents.from_bundle(bundle)
        except incidents.IncidentError as exc:
            raise ToolError(str(exc)) from exc
        scenario.pop("ground_truth", None)
        scenario.pop("relevant_fact_ids", None)
        return await _investigate(scenario, ctx)

    if not local_files:
        return server

    @server.tool(title="Build an incident bundle from local repos and logs")
    def build_incident_bundle(
        start: str, end: str, out_path: str,
        repos: dict[str, str] | None = None, logs: dict[str, str] | None = None,
        services: dict[str, list[str]] | None = None, description: str = "",
    ) -> dict[str, Any]:
        """Collect the 24 hours before an incident into a bundle file.
        start and end are ISO 8601 times. repos maps a service name to its
        git repository path; logs maps a service name to a log file path;
        services maps each service to the services it depends on. Then call
        investigate_bundle_file with out_path."""
        import json

        try:
            bundle = build_bundle(
                start, end,
                repos={k: Path(v) for k, v in (repos or {}).items()},
                logs={k: Path(v) for k, v in (logs or {}).items()},
                description=description)
        except BundleError as exc:
            raise ToolError(str(exc)) from exc
        for service, deps in (services or {}).items():
            bundle["service_graph"][service] = list(deps)
            for dep in deps:
                bundle["service_graph"].setdefault(dep, [])
        known = {s["name"] for s in bundle["services"]}
        for name, deps in bundle["service_graph"].items():
            if name in known:
                next(s for s in bundle["services"] if s["name"] == name)["dependencies"] = deps
            else:
                bundle["services"].append({"name": name, "status": "unknown",
                                           "dependencies": deps, "recent_deploy_count": 0})
                bundle["logs"].setdefault(name, [])
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(bundle, indent=1), encoding="utf-8")
        return {"bundle_path": out_path, "contents": describe(bundle)}

    @server.tool(title="Investigate a bundle file")
    async def investigate_bundle_file(ctx: Context, bundle_path: str) -> dict[str, Any]:
        """Investigate the incident in a local bundle file, such as one written
        by build_incident_bundle."""
        try:
            scenario = incidents.from_file(bundle_path)
        except incidents.IncidentError as exc:
            raise ToolError(str(exc)) from exc
        scenario.pop("ground_truth", None)
        scenario.pop("relevant_fact_ids", None)
        return await _investigate(scenario, ctx)

    return server


def run(http: bool = False, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(local_files=not http)
    if http:
        server.run("streamable-http", host=host, port=port)
    else:
        server.run("stdio")
