"""Command line for the copilot.

    python -m copilot investigate --seed 42 --difficulty medium --out postmortem.md
    python -m copilot investigate --task task2_cascade_chain
    python -m copilot investigate --file my_incident.json
    python -m copilot bundle --start ... --end ... --repo api=../api --logs api=api.log
    python -m copilot mcp
    python -m copilot models
    python -m copilot bench --seeds 0-19 --difficulty medium --label routed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from dataclasses import replace

from copilot import incidents
from copilot.bundle import BundleError, _named, build_bundle, describe
from copilot.bench import BASELINES, parse_seeds, run_baseline, run_benchmark
from copilot.config import ROLE_REASON, ROLE_TRIAGE, ROLES, load_dotenv, load_settings
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
    started = time.time()
    events: list[dict] = []
    async for event in investigator.run():
        events.append(event)
        if args.json:
            print(json.dumps(event))
        else:
            _print_event(event)
        failed = failed or event["type"] == "error"
        if event["type"] == "report" and args.out:
            Path(args.out).write_text(event["markdown"], encoding="utf-8")
            print(f"\nPostmortem written to {args.out}")
    if args.record and not failed:
        # Same shape the web API serves, so the hosted demo can replay this
        # real run without spending anything (see web/copilot_api.py).
        record = {
            "id": f"rec-{scenario['task_id']}".replace("_", "-"),
            "title": scenario.get("task_name") or scenario["task_id"],
            "source": "file" if args.file else "task" if args.task else "seed",
            "status": "done", "started_at": started, "ended_at": time.time(),
            "events": events, "report": investigator.report,
        }
        Path(args.record).parent.mkdir(parents=True, exist_ok=True)
        Path(args.record).write_text(json.dumps(record, indent=1), encoding="utf-8")
        print(f"Recording written to {args.record}")
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


def _save_bench(result: dict, args: argparse.Namespace) -> None:
    print(json.dumps(result["summary"], indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"Results written to {args.out}")


async def _bench(args: argparse.Namespace) -> int:
    if args.baseline:
        result = run_baseline(parse_seeds(args.seeds), args.difficulty, args.baseline)
        result.update(label=args.baseline, kind="baseline", models={},
                      about=BASELINES[args.baseline])
        _save_bench(result, args)
        return 0
    settings = load_settings()
    models = dict(settings.models)
    if args.triage:
        models[ROLE_TRIAGE] = args.triage
    if args.reason:
        models[ROLE_REASON] = args.reason
    settings = replace(settings, models=models)

    def show(row: dict) -> None:
        if row["error"]:
            print(f"  seed {row['seed']:>5}  ERROR {row['error'][:90]}")
        else:
            verdict = "correct" if row["cause_correct"] else "wrong  "
            print(f"  seed {row['seed']:>5}  {verdict}  score {row['score']:.3f}  "
                  f"{row['lookups']:>2} lookups  ${row['cost_usd']:.4f}  {row['seconds']}s")

    result = await run_benchmark(
        parse_seeds(args.seeds), args.difficulty,
        lambda: TokenFactoryClient(settings), args.concurrency, show)
    result.update(label=args.label, kind="model", models={
        ROLE_TRIAGE: models[ROLE_TRIAGE], ROLE_REASON: models[ROLE_REASON]})
    _save_bench(result, args)
    return 1 if result["summary"]["errors"] == result["summary"]["incidents"] else 0


def _bundle(args: argparse.Namespace) -> int:
    bundle = build_bundle(
        args.start, args.end,
        repos=_named(args.repo, "repo"), logs=_named(args.logs, "logs"),
        services_file=args.services, extra_file=args.extra,
        description=args.description, lookback_hours=args.lookback_hours)
    incidents.from_bundle(bundle)  # fail here rather than at investigation time
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(bundle, indent=1), encoding="utf-8")
    print(describe(bundle))
    print(f"Bundle written to {args.out}")
    print(f"Next: python -m copilot investigate --file {args.out} --out postmortem.md")
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
    inv.add_argument("--record", help="save the whole run as a replayable recording "
                                      "(put it in copilot/recordings/ to ship it with the demo)")
    inv.add_argument("--json", action="store_true", help="print raw events, one per line")

    sub.add_parser("models", help="check the configured models exist on Token Factory")

    bench = sub.add_parser("bench", help="score the investigator on generated incidents")
    bench.add_argument("--seeds", default="0-9", help="for example 0-19 or 3,7,42")
    bench.add_argument("--difficulty", default="medium", choices=incidents.DIFFICULTIES)
    bench.add_argument("--label", default="routed", help="name for this configuration")
    bench.add_argument("--baseline", choices=sorted(BASELINES),
                       help="score a no-model heuristic instead of the investigator")
    bench.add_argument("--triage", help="override the triage model ID")
    bench.add_argument("--reason", help="override the diagnosis model ID")
    bench.add_argument("--concurrency", type=int, default=2)
    bench.add_argument("--out", help="write full results (JSON) to this path")

    bundle = sub.add_parser("bundle", help="build an incident bundle from git repos and log files")
    bundle.add_argument("--start", required=True, help="incident start, ISO 8601")
    bundle.add_argument("--end", required=True, help="incident end, ISO 8601")
    bundle.add_argument("--repo", action="append", default=[], metavar="SERVICE=PATH",
                        help="a service's git repository; repeat per service")
    bundle.add_argument("--logs", action="append", default=[], metavar="SERVICE=FILE",
                        help="a service's log file; repeat per service")
    bundle.add_argument("--services", help='JSON file mapping each service to what it depends on')
    bundle.add_argument("--extra", help="JSON file with config_changes, infra_events or traces")
    bundle.add_argument("--description", default="", help="what was observed, in a sentence")
    bundle.add_argument("--lookback-hours", type=int, default=24,
                        help="how far before the incident to collect commits and logs")
    bundle.add_argument("--out", default="incident.json")

    mcp = sub.add_parser("mcp", help="serve Hindsight to MCP clients")
    mcp.add_argument("--http", action="store_true",
                     help="serve Streamable HTTP at /mcp instead of stdio")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)
    try:
        if args.command == "investigate":
            return asyncio.run(_investigate(args))
        if args.command == "mcp":
            from copilot.mcp_server import run as run_mcp
            run_mcp(http=args.http, host=args.host, port=args.port)
            return 0
        if args.command == "bundle":
            return _bundle(args)
        if args.command == "bench":
            return asyncio.run(_bench(args))
        return asyncio.run(_models())
    except (incidents.IncidentError, LLMError, BundleError, ValueError) as exc:
        print(f"ERROR  {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
