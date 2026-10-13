"""Reproduce a checkout outage as real git repositories and log files.

    python scripts/make_demo_incident.py [--out demo/checkout-incident]

Writes three service repositories (web -> orders -> payments) with a day of
commit history, their log files, and a services.json. That is the input a
team would point ``python -m copilot bundle`` (or the MCP tool
``build_incident_bundle``) at. It also writes ``incident.json``: the same
bundle with the known answer attached, so the run can be graded.

What happened: a payments commit three hours before the outage moved the
fraud-check HTTP call inside the database transaction. Each charge now holds
a pooled connection for the length of an external call, which is harmless at
morning traffic and drains the pool at the lunch peak. The most recent change
is an alarming-sounding retry rewrite in web, the worst-hit service, which has
nothing to do with it.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copilot.bundle import build_bundle  # noqa: E402

START = datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=20)
GRAPH = {"web": ["orders"], "orders": ["payments"], "payments": []}
DESCRIPTION = ("Checkout failures spiked at about 14:00 UTC. Customers saw an error "
               "page when paying; browsing the store worked.")

CHARGE_BEFORE = '''"""Charge a card for an order."""

from app import fraud
from app.db import engine
from app.queries import INSERT_CHARGE


class Declined(Exception):
    pass


def charge(order_id: str, amount_cents: int, card_token: str) -> str:
    verdict = fraud.check(card_token, amount_cents)
    if verdict.blocked:
        raise Declined(order_id, verdict.reason)
    with engine.begin() as conn:
        row = conn.execute(INSERT_CHARGE, {"order_id": order_id, "amount": amount_cents,
                                           "verdict_id": verdict.id}).one()
    return row.charge_id
'''

CHARGE_AFTER = '''"""Charge a card for an order."""

from app import fraud
from app.db import engine
from app.queries import INSERT_CHARGE


class Declined(Exception):
    pass


def charge(order_id: str, amount_cents: int, card_token: str) -> str:
    with engine.begin() as conn:
        # One fraud session per charge, so the verdict is recorded in the
        # same transaction as the charge it approved.
        session = fraud.Session(conn)
        verdict = session.check(card_token, amount_cents)
        if verdict.blocked:
            raise Declined(order_id, verdict.reason)
        row = conn.execute(INSERT_CHARGE, {"order_id": order_id, "amount": amount_cents,
                                           "verdict_id": verdict.id}).one()
    return row.charge_id
'''

FRAUD_BEFORE = '''"""Client for the external fraud-scoring API."""

import httpx

from app.settings import FRAUD_API_URL

_client = httpx.Client(base_url=FRAUD_API_URL, timeout=3.0)


def check(card_token: str, amount_cents: int):
    reply = _client.post("/v2/score", json={"card": card_token, "amount": amount_cents})
    reply.raise_for_status()
    return Verdict(**reply.json())


class Verdict:
    def __init__(self, id: str, blocked: bool, reason: str = "") -> None:
        self.id, self.blocked, self.reason = id, blocked, reason
'''

FRAUD_AFTER = FRAUD_BEFORE + '''

class Session:
    """A fraud check whose verdict is stored with the caller's transaction."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def check(self, card_token: str, amount_cents: int) -> Verdict:
        verdict = check(card_token, amount_cents)
        self.conn.execute(
            "INSERT INTO fraud_verdicts (id, blocked, reason) VALUES (:id, :blocked, :reason)",
            {"id": verdict.id, "blocked": verdict.blocked, "reason": verdict.reason})
        return verdict
'''

DB = '''"""Database engine for the payments service."""

from sqlalchemy import create_engine

from app.settings import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_size=10, max_overflow=0, pool_timeout=5)
'''

RETRY_BEFORE = '''"""HTTP client the storefront uses to call backend services."""

import httpx

client = httpx.Client(timeout=6.0)


def get(url: str, **kw) -> httpx.Response:
    return client.get(url, **kw)


def post(url: str, **kw) -> httpx.Response:
    return client.post(url, **kw)
'''

RETRY_AFTER = '''"""HTTP client the storefront uses to call backend services."""

import random
import time

import httpx

client = httpx.Client(timeout=6.0)
RETRIES = 3


def get(url: str, **kw) -> httpx.Response:
    """GETs are idempotent, so retry connection errors with jittered backoff."""
    for attempt in range(RETRIES):
        try:
            return client.get(url, **kw)
        except httpx.ConnectError:
            if attempt == RETRIES - 1:
                raise
            time.sleep((2 ** attempt) * 0.1 + random.random() * 0.1)
    raise AssertionError("unreachable")


def post(url: str, **kw) -> httpx.Response:
    # Never retried: a POST may not be safe to repeat.
    return client.post(url, **kw)
'''

ORDERS_CLIENT = '''"""Calls from orders to payments."""

import httpx

payments = httpx.Client(base_url="http://payments:8080", timeout=5.0)


def charge(order_id: str, amount_cents: int, card_token: str) -> str:
    reply = payments.post("/charge", json={"order_id": order_id, "amount": amount_cents,
                                          "card": card_token})
    reply.raise_for_status()
    return reply.json()["charge_id"]
'''

DAY_BEFORE = START - timedelta(days=2)

# (service, when, author, message, {path: content}); the first commit of each
# repository is two days old, outside the bundle's 24-hour lookback.
HISTORY = [
    ("payments", DAY_BEFORE, "dev@shop.example", "Initial payments service",
     {"app/charge.py": CHARGE_BEFORE, "app/fraud.py": FRAUD_BEFORE, "app/db.py": DB,
      "app/settings.py": 'import os\nDATABASE_URL = os.environ["DATABASE_URL"]\n'
                         'FRAUD_API_URL = os.environ["FRAUD_API_URL"]\n',
      "app/queries.py": 'INSERT_CHARGE = "INSERT INTO charges (order_id, amount, verdict_id) '
                        'VALUES (:order_id, :amount, :verdict_id) RETURNING charge_id"\n'}),
    ("orders", DAY_BEFORE, "dev@shop.example", "Initial orders service",
     {"orders/payments_client.py": ORDERS_CLIENT,
      "requirements.txt": "fastapi==0.115.0\nhttpx==0.27.2\nrequests==2.31.0\n"}),
    ("web", DAY_BEFORE, "dev@shop.example", "Initial storefront",
     {"storefront/http.py": RETRY_BEFORE,
      "storefront/images.py": 'CACHE_SECONDS = 0\n'}),
    ("payments", START - timedelta(hours=5, minutes=48), "sam@shop.example",
     "docs: local setup notes",
     {"README.md": "# payments\n\nRun locally with `make dev`. Needs DATABASE_URL and "
                   "FRAUD_API_URL.\n"}),
    ("orders", START - timedelta(hours=4, minutes=20), "lee@shop.example",
     "Bump requests to 2.32.3",
     {"requirements.txt": "fastapi==0.115.0\nhttpx==0.27.2\nrequests==2.32.3\n"}),
    ("payments", START - timedelta(hours=2, minutes=55), "priya@shop.example",
     "Record fraud verdicts with the charge",
     {"app/charge.py": CHARGE_AFTER, "app/fraud.py": FRAUD_AFTER}),
    ("web", START - timedelta(hours=2, minutes=30), "sam@shop.example",
     "Cache product images for 10 minutes",
     {"storefront/images.py": 'CACHE_SECONDS = 600\n'}),
    ("web", START - timedelta(minutes=22), "lee@shop.example",
     "Rewrite retry logic in the storefront HTTP client",
     {"storefront/http.py": RETRY_AFTER}),
]
CULPRIT_MESSAGE = "Record fraud verdicts with the charge"

CHAIN = [{"service": "payments", "effect": "connection_pool_exhaustion"},
         {"service": "orders", "effect": "upstream_timeout"},
         {"service": "web", "effect": "5xx_errors_to_users"}]
# Log lines that show a hop of the chain, by service.
RELEVANT_MARKERS = {"payments": ("QueuePool limit", "waited"),
                    "orders": ("payments:8080/charge timed out",),
                    "web": ("502",)}


def git(repo: Path, *args: str, when: datetime | None = None, author: str = "") -> str:
    env = dict(os.environ)
    if when is not None:
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        name = author.split("@")[0]
        env |= {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
                "GIT_AUTHOR_NAME": name, "GIT_COMMITTER_NAME": name,
                "GIT_AUTHOR_EMAIL": author, "GIT_COMMITTER_EMAIL": author}
    result = subprocess.run(["git", "-C", str(repo), *args], env=env, check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def make_repos(root: Path) -> str:
    """Create the repositories and return the culprit's commit ID as the bundle names it."""
    for service in GRAPH:
        (root / service).mkdir(parents=True)
        git(root / service, "init", "-q")
        git(root / service, "config", "core.autocrlf", "false")
    culprit = ""
    for service, when, author, message, files in HISTORY:
        repo = root / service
        for path, content in files.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(content, encoding="utf-8", newline="\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", message, when=when, author=author)
        if message == CULPRIT_MESSAGE:
            culprit = "commit-" + git(repo, "rev-parse", "HEAD")[:10]
    return culprit


def make_logs(rng: random.Random) -> dict[str, list[str]]:
    """Two and a half hours of logs. Charges hold a connection for the fraud
    call after the culprit lands; the pool runs dry when lunch traffic peaks."""
    lines: dict[str, list[tuple[datetime, str, str]]] = {s: [] for s in GRAPH}
    t = START - timedelta(hours=2)
    order = 48000
    while t < END:
        peak = t >= START - timedelta(minutes=8)
        failing = t >= START + timedelta(minutes=1)
        for _ in range(3 if peak else 1):
            order += 1
            at = t + timedelta(milliseconds=rng.randint(0, 19000))
            oid = f"ord-{order}"
            lines["web"].append((at, "INFO", f"GET /product/{rng.randint(100, 999)} 200 "
                                             f"{rng.randint(20, 90)}ms"))
            if failing and rng.random() < 0.8:
                pool_at = at + timedelta(milliseconds=200)
                lines["payments"].append((
                    pool_at, "ERROR",
                    f"charge failed for {oid}: sqlalchemy.exc.TimeoutError: QueuePool limit "
                    "of size 10 overflow 0 reached, connection timed out, timeout 5.00"))
                lines["orders"].append((at + timedelta(seconds=5, milliseconds=100), "ERROR",
                                        f"POST http://payments:8080/charge timed out after "
                                        f"5000ms (order={oid})"))
                lines["web"].append((at + timedelta(seconds=5, milliseconds=300), "ERROR",
                                     f"POST /checkout 502 Bad Gateway: orders upstream "
                                     f"timeout (order={oid})"))
                if rng.random() < 0.6:   # the customer presses Pay again
                    lines["web"].append((at + timedelta(seconds=rng.randint(8, 15)), "ERROR",
                                         f"POST /checkout 502 Bad Gateway: orders upstream "
                                         f"timeout (order={oid}, retry by customer)"))
                continue
            if peak and rng.random() < 0.5:
                lines["payments"].append((at, "WARN", f"connection checkout waited "
                                                      f"{rng.randint(700, 4800)}ms "
                                                      f"(pool in use 10/10, order={oid})"))
            ms = rng.randint(380, 900)
            lines["payments"].append((at + timedelta(milliseconds=ms), "INFO",
                                      f"charge ok {oid} amount={rng.randint(900, 25000)} "
                                      f"fraud_ms={ms - 40}"))
            lines["orders"].append((at + timedelta(milliseconds=ms + 30), "INFO",
                                    f"order {oid} paid"))
            lines["web"].append((at + timedelta(milliseconds=ms + 60), "INFO",
                                 f"POST /checkout 200 {ms + 60}ms"))
        if rng.random() < 0.04:
            lines["web"].append((t, "WARN", "image CDN slow: /img/hero.webp took 2310ms"))
        if rng.random() < 0.03:
            lines["orders"].append((t, "WARN", "inventory cache miss rate 12% (threshold 10%)"))
        t += timedelta(seconds=20)

    def fmt(at: datetime, level: str, message: str) -> str:
        return f"{at.strftime('%Y-%m-%dT%H:%M:%S')}.{at.microsecond // 1000:03d}Z {level} {message}"

    return {s: [fmt(*row) for row in sorted(rows)] for s, rows in lines.items()}


def build(out: Path, seed: int = 7) -> dict:
    if out.exists():
        shutil.rmtree(out, onerror=lambda f, p, _: (os.chmod(p, 0o700), f(p)))
    culprit = make_repos(out)
    (out / "logs").mkdir()
    for service, lines in make_logs(random.Random(seed)).items():
        (out / "logs" / f"{service}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "services.json").write_text(json.dumps(GRAPH, indent=1), encoding="utf-8")

    bundle = build_bundle(
        start=START.isoformat(), end=END.isoformat(),
        repos={s: out / s for s in GRAPH}, logs={s: out / "logs" / f"{s}.log" for s in GRAPH},
        services_file=out / "services.json", description=DESCRIPTION)
    relevant = [culprit] + [
        entry["id"] for service, markers in RELEVANT_MARKERS.items()
        for entry in bundle["logs"][service]
        if entry["level"] in ("WARN", "ERROR") and any(m in entry["message"] for m in markers)]
    bundle["ground_truth"] = {"cause_type": "commit", "cause": culprit, "chain": CHAIN}
    bundle["relevant_fact_ids"] = relevant
    (out / "incident.json").write_text(json.dumps(bundle, indent=1), encoding="utf-8")
    return bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="demo/checkout-incident")
    args = parser.parse_args()
    out = Path(args.out)
    bundle = build(out)
    print(f"Wrote {out}/ with repos {', '.join(GRAPH)}, logs/, services.json and incident.json.")
    print(f"Culprit: {bundle['ground_truth']['cause']}. To bundle it yourself:")
    print(f"  python -m copilot bundle --start {START:%Y-%m-%dT%H:%M:%SZ} "
          f"--end {END:%Y-%m-%dT%H:%M:%SZ} "
          + " ".join(f"--repo {s}={out / s}" for s in GRAPH) + " "
          + " ".join(f"--logs {s}={out / 'logs' / s}.log" for s in GRAPH)
          + f" --services {out / 'services.json'} --out my_incident.json")


if __name__ == "__main__":
    main()
