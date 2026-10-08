"""Command line for the copilot.

    python -m copilot investigate --seed 42 --difficulty medium --out postmortem.md
    python -m copilot investigate --task task2_cascade_chain
    python -m copilot investigate --file my_incident.json
    python -m copilot models
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from copilot import incidents
from copilot.config import ROLES, load_dotenv, load_settings
from copilot.investigator import Investigator
from copilot.llm import LLMError, TokenFactoryClient
from copilot.research import Researcher, TavilyClient
from copilot.workspace import Workspace


def _print_event(event: dict) -> None:
    kind = event["type"]
    if kind == "brief":
        b = event["brief"]
        print(f"\nINCIDENT {b['incident_id']}\n{b['description']}\n")
    elif kind == "phase":
        print(f"--- {event['phase']} ---")
    elif kind == "thought":
        print(f"  [{event['model']}] {event['text']}")
    elif kind == "evidence":
        tag = event["id"] if event["ok"] else "rejected"
        first_line = event["result"].splitlines()[0] if event["result"] else ""
        print(f"    {tag}: {event['tool']}({json.dumps(event['args'])}) -> {first_line[:100]}")
    elif kind == "diagnosis":
        print(f"\nROOT CAUSE  {event['cause'] or '(none named)'}"
              f"   confidence {event['confidence']:.0%}")
        print(event["summary"])
        for i, hop in enumerate(event["chain"], start=1):
            cites = ", ".join(hop["evidence"]) or "no citation"
            print(f"  {i}. {hop['service']}: {hop['effect']}  [{cites}]")
    elif kind == "research":
        for query in event["queries"]:
            print(f"  searched: {query}")
        for ref in event["references"]:
            print(f"    {ref['id']}: {ref['title']}  {ref['url']}")
    elif kind == "grade":
        verdict = "correct" if event["cause_correct"] else "WRONG"
        print(f"\nGRADE  {event['score']:.3f}   root cause {verdict}"
              f" (truth: {event['ground_truth_cause']})")
    elif kind == "done":
        usage = event["usage"]
        for role, row in usage["by_role"].items():
            print(f"  {role:<7} {row['model']}  {row['calls']} calls, "
                  f"{row['input_tokens']}+{row['output_tokens']} tokens, ${row['cost_usd']:.4f}")
        print(f"  total   ${usage['total_cost_usd']:.4f}")
    elif kind == "warning":
        print(f"  warning: {event['message']}", file=sys.stderr)
    elif kind == "error":
        print(f"\nERROR  {event['message']}", file=sys.stderr)


async def _investigate(args: argparse.Namespace) -> int:
    if args.file:
        scenario = incidents.from_file(args.file)
    elif args.task:
        scenario = incidents.from_task(args.task)
    else:
        scenario = incidents.from_seed(args.seed, args.difficulty)

    llm = TokenFactoryClient()
    key = llm.settings.tavily_api_key
    researcher = Researcher(llm, TavilyClient(key)) if key else None
    investigator = Investigator(Workspace(scenario), llm, researcher=researcher)
    failed = False
    async for event in investigator.run():
        if args.json:
            print(json.dumps(event))
        else:
            _print_event(event)
        failed = failed or event["type"] == "error"
        if event["type"] == "report" and args.out:
            Path(args.out).write_text(event["markdown"], encoding="utf-8")
            print(f"\nPostmortem written to {args.out}")
    return 1 if failed else 0


async def _models() -> int:
    settings = load_settings()
    available = await TokenFactoryClient(settings).list_models()
    by_lower = {m.lower(): m for m in available}
    for role in ROLES:
        wanted = settings.model_for(role)
        found = by_lower.get(wanted.lower())
        status = "ok" if found == wanted else (f"use {found}" if found else "NOT FOUND")
        print(f"{role:<7} {wanted}  [{status}]")
    print("\nNVIDIA models on Token Factory:")
    for model in sorted(m for m in available if "nvidia" in m.lower()):
        print(f"  {model}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="copilot", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("investigate", help="investigate one incident")
    inv.add_argument("--seed", type=int, default=42)
    inv.add_argument("--difficulty", default="easy", choices=incidents.DIFFICULTIES)
    inv.add_argument("--task", help="a hand-written benchmark task ID")
    inv.add_argument("--file", help="path to an incident bundle (JSON)")
    inv.add_argument("--out", help="write the postmortem (Markdown) to this path")
    inv.add_argument("--json", action="store_true", help="print raw events, one per line")

    sub.add_parser("models", help="check the configured models exist on Token Factory")

    args = parser.parse_args(argv)
    try:
        if args.command == "investigate":
            return asyncio.run(_investigate(args))
        return asyncio.run(_models())
    except (incidents.IncidentError, LLMError) as exc:
        print(f"ERROR  {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
