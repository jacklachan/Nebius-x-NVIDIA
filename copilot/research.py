"""External research: what does the wider world know about this failure mode?

After the diagnosis, the copilot searches the web (Tavily) for the failure
mechanism and its standard remediations, so the action items in the
postmortem rest on published practice rather than on the model's memory.

Nothing internal leaves the building: search queries are written to be
generic, and any query that still contains a service name, a change ID or an
email address from the incident is dropped before it is sent.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from copilot.config import ROLE_TRIAGE
from copilot.core import ChatModel, Diagnosis
from copilot.llm import extract_json

TAVILY_URL = "https://api.tavily.com/search"
MAX_QUERIES = 2
RESULTS_PER_QUERY = 3
SNIPPET_CHARS = 500

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

QUERY_SYSTEM = """You turn an incident diagnosis into web search queries. The goal is to find published engineering guidance on this failure mechanism and how teams prevent or detect it.

Reply with ONE JSON object and nothing else:
{"queries": ["<query>", "<query>"]}

Rules:
- Two queries. One about the failure mechanism, one about prevention or detection.
- Use general technical vocabulary (for example "connection pool exhaustion cascading timeouts mitigation").
- Never include service names, commit or config IDs, people, companies or anything else specific to this incident."""


class ResearchError(Exception):
    """The search provider could not be reached or rejected the request."""


@dataclass
class Reference:
    id: str
    title: str
    url: str
    snippet: str
    query: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class TavilyClient:
    def __init__(
        self, api_key: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.api_key = api_key
        self._transport = transport

    async def search(self, query: str, max_results: int = RESULTS_PER_QUERY) -> list[dict[str, Any]]:
        payload = {"query": query, "max_results": max_results, "search_depth": "basic"}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
                resp = await client.post(TAVILY_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ResearchError(f"Tavily request failed: {exc}") from exc
        if resp.status_code != 200:
            raise ResearchError(f"Tavily {resp.status_code}: {resp.text[:200]}")
        try:
            results = resp.json().get("results", [])
        except ValueError as exc:
            raise ResearchError("Tavily returned non-JSON") from exc
        return results if isinstance(results, list) else []


def private_terms(brief: dict[str, Any]) -> set[str]:
    """Strings from the incident that must never appear in a search query."""
    terms = {s["name"] for s in brief.get("services", [])}
    terms |= set(brief.get("service_graph", {}))
    terms |= {c["hash"] for c in brief.get("commits", [])}
    terms |= {c["config_id"] for c in brief.get("config_changes", [])}
    terms |= {e["event_id"] for e in brief.get("infra_events", [])}
    terms |= set(brief.get("trace_ids", []))
    terms.add(str(brief.get("incident_id", "")))
    return {t.lower() for t in terms if len(t) >= 3}


def safe_queries(queries: Any, brief: dict[str, Any]) -> list[str]:
    """Keep only queries that carry nothing incident-specific."""
    banned = private_terms(brief)
    kept: list[str] = []
    for query in queries if isinstance(queries, list) else []:
        if not isinstance(query, str):
            continue
        query = " ".join(query.split())[:200]
        lowered = query.lower()
        if len(query) < 8 or _EMAIL_RE.search(query):
            continue
        if any(term in lowered for term in banned):
            continue
        if query not in kept:
            kept.append(query)
    return kept[:MAX_QUERIES]


class Researcher:
    def __init__(self, llm: ChatModel, search: TavilyClient) -> None:
        self.llm = llm
        self.search = search
        self.queries: list[str] = []
        self.dropped = 0

    async def run(self, brief: dict[str, Any], diagnosis: Diagnosis) -> list[Reference]:
        """Search for the diagnosed failure mode. Raises ``ResearchError`` if
        the provider fails; returns [] when there is nothing safe to ask."""
        if not diagnosis.root_cause_ids:
            return []
        user = (
            f"SUMMARY\n{diagnosis.summary}\n\n"
            "FAILURE HOPS\n"
            + "\n".join(f"- {hop['effect']}: {hop['because']}" for hop in diagnosis.chain)
        )
        reply = await self.llm.chat(
            ROLE_TRIAGE,
            [{"role": "system", "content": QUERY_SYSTEM}, {"role": "user", "content": user}],
            max_tokens=400,
        )
        try:
            proposed = extract_json(reply.text).get("queries")
        except ValueError:
            proposed = []
        self.queries = safe_queries(proposed, brief)
        self.dropped = (len(proposed) if isinstance(proposed, list) else 0) - len(self.queries)

        references: list[Reference] = []
        seen_urls: set[str] = set()
        for query in self.queries:
            for hit in await self.search.search(query):
                url = str(hit.get("url", ""))
                if not url.startswith("http") or url in seen_urls:
                    continue
                seen_urls.add(url)
                references.append(Reference(
                    id=f"R{len(references) + 1}",
                    title=str(hit.get("title", url))[:160],
                    url=url,
                    snippet=" ".join(str(hit.get("content", "")).split())[:SNIPPET_CHARS],
                    query=query,
                ))
        return references


def references_text(references: list[Reference]) -> str:
    if not references:
        return "(none)"
    return "\n".join(f"[{r.id}] {r.title} ({r.url})\n{r.snippet}" for r in references)
