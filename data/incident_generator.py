"""Incident generator, second version.

The first generator (``seed_generator.py``) is kept unchanged for the
original environment and its published results. It has a property that makes
it unsuitable for measuring an investigator: the culprit is the only change
with a real diff, and the diff and config description announce themselves
("reduced limits", "may cause issues under high load").

This version generates incidents whose telemetry hangs together and whose
answer has to be worked out:

* Each failure mode has its own realistic culprit (a diff or a config value),
  its own log lines, and its own early-warning signs.
* Every other change also has a plausible diff. Some decoys land closer to
  the incident than the culprit, and some sound more alarming than it.
* Services that depend on the failing one log errors that name it; services
  off the failure path stay healthy.
* The incident brief reports symptoms only. It never hints at the cause.

Same output shape as the first generator, same causal-chain vocabulary, and
fully determined by ``(seed, difficulty)``.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from data.seed_generator import FAILURE_TEMPLATES

DIFFICULTY = {
    #          services  modes                                             decoy commits  max_steps
    "easy":   ((4, 5),   ("connection_pool", "config_drift", "memory_leak"), (5, 7),      40),
    "medium": ((6, 8),   ("connection_pool", "config_drift", "memory_leak",
                          "oom_cascade"),                                    (8, 11),     75),
    "hard":   ((9, 12),  ("failover_bug", "oom_cascade", "memory_leak",
                          "config_drift"),                                   (11, 15),    120),
}

# Service names by tier, so the generated topology reads like a real one:
# an edge service calls application services, which call stateful backends.
EDGE_SERVICES = ["api-gateway", "web-frontend", "mobile-bff", "partner-api"]
APP_SERVICES = ["checkout", "cart-service", "order-manager", "user-profile", "auth-service",
                "pricing-engine", "catalog-service", "recommendation", "shipping-service",
                "fraud-detector", "notification-worker", "payment-gateway"]
BACKENDS = ["orders-db", "inventory-db", "session-store", "search-index", "ledger-db",
            "profile-db", "catalog-db", "event-queue"]

AUTHORS = ["alice", "bob", "carol", "dave", "eve", "frank", "grace", "heidi"]
ENDPOINTS = ["GET /v1/items", "POST /v1/orders", "GET /v1/profile", "POST /v1/session",
             "GET /v1/search", "PUT /v1/settings", "GET /internal/lookup", "POST /internal/batch"]

# --------------------------------------------------------------------------
# Failure modes. ``lead_minutes`` is how long before the incident the culprit
# lands: a pool misconfiguration bites within minutes, a leak takes hours.
# --------------------------------------------------------------------------

MODES: dict[str, dict[str, Any]] = {
    "connection_pool": {
        "lead_minutes": (6, 35),
        "culprits": [
            {
                "message": "refactor: load db pool settings from shared defaults",
                "diff": """--- a/internal/db/pool.go
+++ b/internal/db/pool.go
@@ -41,9 +41,9 @@ func NewPool(cfg Config) (*Pool, error) {
 	db, err := sql.Open("pgx", cfg.DSN)
 	if err != nil {
 		return nil, err
 	}
-	db.SetMaxOpenConns(cfg.MaxOpenConns)
-	db.SetMaxIdleConns(cfg.MaxIdleConns)
+	db.SetMaxOpenConns(defaults.MaxOpenConns)
+	db.SetMaxIdleConns(defaults.MaxIdleConns)
 	db.SetConnMaxLifetime(cfg.ConnMaxLifetime)
--- /dev/null
+++ b/internal/defaults/defaults.go
@@ -0,0 +1,6 @@
+package defaults
+
+const (
+	MaxOpenConns = {small}
+	MaxIdleConns = 2
+)""",
            },
            {
                "message": "chore: bump pgx to v5 and adopt the new pool API",
                "diff": """--- a/go.mod
+++ b/go.mod
@@ -7,3 +7,3 @@ require (
-	github.com/jackc/pgx/v4 v4.18.3
+	github.com/jackc/pgx/v5 v5.7.2
--- a/internal/db/pool.go
+++ b/internal/db/pool.go
@@ -22,8 +22,7 @@ func Connect(ctx context.Context, cfg Config) (*pgxpool.Pool, error) {
 	poolCfg, err := pgxpool.ParseConfig(cfg.DSN)
 	if err != nil {
 		return nil, err
 	}
-	poolCfg.MaxConns = int32(cfg.MaxOpenConns)
 	poolCfg.MinConns = 2
-	return pgxpool.ConnectConfig(ctx, poolCfg)
+	return pgxpool.NewWithConfig(ctx, poolCfg)""",
            },
        ],
        "precursors": [("WARN", "db pool wait time p95={ms}ms (threshold 100ms)")],
        "errors": [
            ("ERROR", "db pool exhausted: all {small} connections in use, {queued} requests queued"),
            ("ERROR", "acquire connection: context deadline exceeded after 5000ms"),
            ("CRITICAL", "readiness probe failed: could not acquire db connection in 2s"),
        ],
        "span_error": "acquire connection: context deadline exceeded",
    },
    "memory_leak": {
        "lead_minutes": (180, 480),
        "culprits": [
            {
                "message": "feat: cache resolved lookups in process",
                "diff": """--- a/src/cache/lookup_cache.py
+++ b/src/cache/lookup_cache.py
@@ -1,14 +1,12 @@
-from cachetools import TTLCache
-
-_cache = TTLCache(maxsize=10_000, ttl=300)
+_cache: dict[str, Result] = {}


 def get(key: str) -> Result | None:
     return _cache.get(key)


 def put(key: str, value: Result) -> None:
-    _cache[key] = value
+    _cache[f"{key}:{value.version}"] = value""",
            },
            {
                "message": "fix: record request latency from the finish event",
                "diff": """--- a/src/server/middleware/timing.ts
+++ b/src/server/middleware/timing.ts
@@ -8,7 +8,7 @@ export function timing(req: Request, res: Response, next: NextFunction) {
   req.startedAt = Date.now();
-  const stop = histogram.startTimer();
-  res.once("finish", () => stop());
+  lifecycle.on("finish", () =>
+    histogram.observe((Date.now() - req.startedAt) / 1000));
   next();
 }""",
            },
        ],
        "precursors": [
            ("INFO", "heap in use {heap_low} MB of 2048 MB"),
            ("WARN", "heap in use {heap_mid} MB of 2048 MB"),
            ("WARN", "GC pause {ms}ms"),
            ("WARN", "heap in use {heap_high} MB of 2048 MB"),
        ],
        "errors": [
            ("ERROR", "GC overhead: {pct}% of CPU time spent in collection"),
            ("ERROR", "request latency p99={slow}ms (SLO 800ms)"),
            ("CRITICAL", "heap in use 2031 MB of 2048 MB, allocation stalls"),
        ],
        "span_error": "deadline exceeded after {slow}ms",
    },
    "oom_cascade": {
        "lead_minutes": (5, 30),
        "culprits": [
            {
                "message": "feat: build the full lookup index at startup for faster first requests",
                "diff": """--- a/cmd/server/main.go
+++ b/cmd/server/main.go
@@ -58,6 +58,11 @@ func main() {
 	store := storage.Open(cfg.Storage)
 	defer store.Close()

+	idx, err := index.BuildAll(ctx, store)
+	if err != nil {
+		log.Fatalf("build index: %v", err)
+	}
+	srv.UseIndex(idx)
 	log.Fatal(srv.ListenAndServe())
 }""",
            },
            {
                "message": "perf: sort export rows in memory before streaming",
                "diff": """--- a/internal/export/stream.go
+++ b/internal/export/stream.go
@@ -30,9 +30,12 @@ func (e *Exporter) Stream(ctx context.Context, w io.Writer, q Query) error {
 	enc := json.NewEncoder(w)
-	for rows.Next() {
-		if err := enc.Encode(scan(rows)); err != nil {
-			return err
-		}
+	all, err := scanAll(rows)
+	if err != nil {
+		return err
 	}
-	return rows.Err()
+	sort.Slice(all, func(i, j int) bool { return all[i].Key < all[j].Key })
+	return enc.Encode(all)
 }""",
            },
        ],
        "precursors": [("INFO", "starting, version {version}")],
        "errors": [
            ("CRITICAL", "container terminated: OOMKilled (limit 1024Mi, rss {rss}Mi)"),
            ("ERROR", "back-off restarting failed container, restart count {restarts}"),
            ("ERROR", "readiness probe failed: connection refused"),
        ],
        "span_error": "connection refused",
    },
    "config_drift": {
        "lead_minutes": (4, 25),
        "culprits": [
            {
                "key": "worker_concurrency", "old": "64", "new": "8",
                "description": "Align worker_concurrency with the new instance class",
                "errors": [
                    ("ERROR", "worker pool saturated: 8/8 busy, {queued} jobs queued"),
                    ("ERROR", "request rejected: work queue full"),
                    ("CRITICAL", "queue wait p99={slow}ms, shedding load"),
                ],
                "span_error": "503 work queue full",
            },
            {
                "key": "rate_limit_rps", "old": "2000", "new": "200",
                "description": "Apply the standard rate limit profile",
                "errors": [
                    ("ERROR", "rate limit exceeded: {rps} rps against limit 200"),
                    ("ERROR", "returned 429 to internal caller"),
                    ("CRITICAL", "{pct}% of requests throttled in the last minute"),
                ],
                "span_error": "429 too many requests",
            },
            {
                "key": "max_request_queue", "old": "500", "new": "50",
                "description": "Reduce queue depth per capacity review",
                "errors": [
                    ("ERROR", "request queue full (50), rejecting"),
                    ("ERROR", "load shed: {queued} requests dropped in the last 10s"),
                    ("CRITICAL", "accept backlog overflow"),
                ],
                "span_error": "503 queue full",
            },
        ],
        "precursors": [],
        "errors": [],
        "span_error": "",
    },
    "failover_bug": {
        "lead_minutes": (360, 1200),
        "culprits": [
            {
                "message": "refactor: simplify primary-change handling in the {target} client",
                "diff": """--- a/internal/client/failover.go
+++ b/internal/client/failover.go
@@ -44,10 +44,8 @@ func (c *Client) watch(ctx context.Context) {
 func (c *Client) onPrimaryChange(next Endpoint) {
-	fresh := c.dial(next)
-	old := c.swap(fresh)
-	old.CloseIdle()
+	c.pool.CloseAll()
+	c.endpoint = next
 }""",
            },
        ],
        "precursors": [],
        "errors": [
            ("WARN", "replication link degraded: packet loss {loss}%"),
            ("WARN", "primary role moved to standby in zone b"),
            ("INFO", "accepting connections on new primary"),
        ],
        "span_error": "no available connection",
    },
}

# Errors logged by the service that carries the failover bug.
FAILOVER_CLIENT_ERRORS = [
    ("INFO", "{target} primary changed, closing {conns} connections"),
    ("ERROR", "connection pool size 0 for {target}, no dial scheduled"),
    ("CRITICAL", "no available connection to {target}"),
]

DEPENDANT_ERRORS = [
    ("ERROR", "call to {dep} failed: {span_error}"),
    ("ERROR", "timeout calling {dep} after {slow}ms"),
    ("WARN", "circuit breaker for {dep} opened"),
    ("ERROR", "serving 5xx for {pct}% of requests"),
]

NOISE_LOGS = [
    ("INFO", "health check ok"),
    ("INFO", "request latency p99={fast}ms"),
    ("INFO", "config reloaded"),
    ("DEBUG", "cache hit ratio {ratio}%"),
    ("INFO", "scheduled job finished in {fast}ms"),
    ("WARN", "slow query {fast}ms on replica"),
    ("WARN", "retrying webhook delivery, attempt 2"),
    ("INFO", "rotated access log"),
]

# (message, diff). ``{svc}`` is the service the commit lands on.
DECOY_COMMITS = [
    ("chore: bump logging library to 2.4.1",
     "--- a/go.mod\n+++ b/go.mod\n@@ -12,3 +12,3 @@\n-\tgo.uber.org/zap v1.26.0\n+\tgo.uber.org/zap v1.27.0"),
    ("docs: update runbook links",
     "--- a/docs/runbook.md\n+++ b/docs/runbook.md\n@@ -3,3 +3,3 @@\n-See the old wiki for escalation.\n+See go/oncall-{svc} for escalation."),
    ("test: cover retry backoff jitter",
     "--- a/internal/retry/backoff_test.go\n+++ b/internal/retry/backoff_test.go\n@@ -40,0 +41,9 @@\n+func TestJitterStaysWithinBounds(t *testing.T) {\n+\tfor i := 0; i < 1000; i++ {\n+\t\td := Backoff(3)\n+\t\tif d < 400*time.Millisecond || d > 1600*time.Millisecond {\n+\t\t\tt.Fatalf(\"out of bounds: %v\", d)\n+\t\t}\n+\t}\n+}"),
    ("feat: add request id to access logs",
     "--- a/internal/http/accesslog.go\n+++ b/internal/http/accesslog.go\n@@ -18,3 +18,4 @@\n \t\tzap.String(\"path\", r.URL.Path),\n+\t\tzap.String(\"request_id\", r.Header.Get(\"X-Request-Id\")),\n \t\tzap.Int(\"status\", rec.status),"),
    ("fix: correct typo in validation error",
     "--- a/internal/api/errors.go\n+++ b/internal/api/errors.go\n@@ -9,3 +9,3 @@\n-\tErrInvalid = errors.New(\"invlaid request body\")\n+\tErrInvalid = errors.New(\"invalid request body\")"),
    ("refactor: extract config loader into its own package",
     "--- a/cmd/server/main.go\n+++ b/cmd/server/main.go\n@@ -21,5 +21,3 @@\n-\traw, err := os.ReadFile(path)\n-\tif err != nil { log.Fatal(err) }\n-\tcfg := parse(raw)\n+\tcfg := config.MustLoad(path)"),
    ("fix: handle empty page token in list endpoint",
     "--- a/internal/api/list.go\n+++ b/internal/api/list.go\n@@ -33,2 +33,5 @@\n \ttoken := r.URL.Query().Get(\"page_token\")\n+\tif token == \"\" {\n+\t\ttoken = firstPage\n+\t}"),
    ("ci: pin base image digest",
     "--- a/Dockerfile\n+++ b/Dockerfile\n@@ -1 +1 @@\n-FROM gcr.io/distroless/static:nonroot\n+FROM gcr.io/distroless/static:nonroot@sha256:6ec5aa99dc335666e79dc64e4a6c8b89c33a543a1967f20d360922a80dd21f02"),
    ("feat: expose build info on /version",
     "--- a/internal/http/routes.go\n+++ b/internal/http/routes.go\n@@ -14,2 +14,3 @@\n \tmux.Handle(\"/healthz\", health)\n+\tmux.Handle(\"/version\", version.Handler())"),
    ("chore: remove unused feature flag for legacy onboarding",
     "--- a/internal/flags/flags.go\n+++ b/internal/flags/flags.go\n@@ -6,3 +6,2 @@\n \tNewCheckout = \"new_checkout\"\n-\tLegacyOnboarding = \"legacy_onboarding\""),
]

# Decoys whose message sounds like it could break production but whose diff
# is harmless. Used at medium and hard.
ALARMING_DECOYS = [
    ("perf: lower timeout for health probes",
     "--- a/deploy/k8s/deployment.yaml\n+++ b/deploy/k8s/deployment.yaml\n@@ -41,3 +41,3 @@\n           readinessProbe:\n-            timeoutSeconds: 5\n+            timeoutSeconds: 4\n             periodSeconds: 10"),
    ("fix: tighten client retry budget",
     "--- a/internal/client/retry.go\n+++ b/internal/client/retry.go\n@@ -11,3 +11,3 @@\n const (\n-\tmaxAttempts = 4\n+\tmaxAttempts = 3\n \tbaseDelay   = 200 * time.Millisecond"),
    ("perf: reuse upstream connections with keep-alive",
     "--- a/internal/client/transport.go\n+++ b/internal/client/transport.go\n@@ -8,3 +8,4 @@\n \tt := &http.Transport{\n+\t\tIdleConnTimeout:     90 * time.Second,\n \t\tMaxIdleConnsPerHost: 32,"),
    ("refactor: rewrite connection handling in metrics exporter",
     "--- a/internal/metrics/exporter.go\n+++ b/internal/metrics/exporter.go\n@@ -27,6 +27,5 @@\n-\tconn, err := net.Dial(\"udp\", e.addr)\n-\tif err != nil { return err }\n-\tdefer conn.Close()\n-\t_, err = conn.Write(buf)\n+\t_, err := e.conn.Write(buf)\n \treturn err"),
]

DECOY_CONFIGS = [
    ("log_level", "info", "debug", "Temporarily raise log level for a support ticket"),
    ("feature.new_dashboard", "off", "on", "Enable new dashboard for internal users"),
    ("cache_ttl_seconds", "300", "600", "Lengthen cache TTL for catalogue pages"),
    ("alert_threshold_p99_ms", "800", "900", "Reduce paging noise on p99 alert"),
    ("replicas", "6", "8", "Scale out ahead of the weekend campaign"),
    ("tracing_sample_rate", "0.01", "0.05", "Sample more traces while debugging checkout"),
    ("cors_allowed_origins", "2 entries", "3 entries", "Allow the new partner origin"),
    ("session_ttl_minutes", "60", "120", "Extend session lifetime per product request"),
]

DECOY_INFRA = [
    ("certificate_rotation", "TLS certificate rotated for *.internal. Completed."),
    ("node_pool_upgrade", "Node pool upgraded in zone c, one node at a time. Completed."),
    ("scaling_event", "Autoscaler added 2 nodes to the general pool."),
    ("dns_update", "TTL lowered to 60s for the internal zone. Completed."),
    ("storage_maintenance", "Snapshot of the analytics volume completed."),
    ("kernel_update", "Kernel patch applied to batch nodes during the maintenance window."),
]


def _short_hash(seed: int, salt: str) -> str:
    return hashlib.md5(f"v2:{seed}:{salt}".encode(), usedforsecurity=False).hexdigest()[:8]


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fill(text: str, rng: random.Random, **names: str) -> str:
    """Substitute placeholders without tripping over braces in code diffs."""
    values = {
        "small": names.get("small", "10"),
        "ms": str(rng.randint(140, 900)), "queued": str(rng.randint(40, 400)),
        "heap_low": str(rng.randint(700, 950)), "heap_mid": str(rng.randint(1200, 1500)),
        "heap_high": str(rng.randint(1750, 1950)), "pct": str(rng.randint(62, 97)),
        "slow": str(rng.randint(4200, 14000)), "fast": str(rng.randint(12, 180)),
        "rss": str(rng.randint(1010, 1023)), "restarts": str(rng.randint(3, 14)),
        "version": f"{rng.randint(2, 6)}.{rng.randint(0, 30)}.{rng.randint(0, 9)}",
        "rps": str(rng.randint(900, 1900)), "loss": str(rng.randint(8, 30)),
        "conns": str(rng.randint(24, 96)), "ratio": str(rng.randint(71, 98)),
        **names,
    }
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def _build_graph(rng: random.Random, n: int) -> tuple[list[str], dict[str, list[str]]]:
    """A DAG where services[0] is user-facing and edges point at dependencies."""
    n_backends = max(1, n // 3)
    services = ([rng.choice(EDGE_SERVICES)]
                + rng.sample(APP_SERVICES, n - 1 - n_backends)
                + rng.sample(BACKENDS, n_backends))
    graph: dict[str, list[str]] = {s: [] for s in services}
    last_caller = n - n_backends - 1          # backends call nothing
    for i in range(1, n):
        high = min(i - 1, last_caller)
        caller = services[rng.randint(max(0, high - 2), high)]
        graph[caller].append(services[i])
    for _ in range(n // 3):
        a = rng.randint(0, last_caller)
        b = rng.randint(a + 1, n - 1)
        if services[b] not in graph[services[a]]:
            graph[services[a]].append(services[b])
    return services, graph


def _caller_chains(graph: dict[str, list[str]], service: str) -> list[str]:
    """Longest chain of callers above ``service``: [direct caller, its caller, ...]."""
    callers = [s for s, deps in graph.items() if service in deps]
    best: list[str] = []
    for caller in sorted(callers):
        chain = [caller] + _caller_chains(graph, caller)
        if len(chain) > len(best):
            best = chain
    return best


def generate_incident(seed: int, difficulty: str = "easy") -> dict[str, Any]:
    """Generate one incident. Deterministic in ``(seed, difficulty)``."""
    (n_low, n_high), modes, (decoy_low, decoy_high), max_steps = DIFFICULTY[difficulty]
    rng = random.Random(f"v2:{seed}:{difficulty}")

    mode_name = rng.choice(modes)
    mode = MODES[mode_name]
    chain_template = FAILURE_TEMPLATES[mode_name]["chain_template"]
    hops_needed = max(
        (int(step["service"][len("{upstream_"):-1])
         for step in chain_template if step["service"].startswith("{upstream_")),
        default=0,
    )

    # Topology: redraw until some service has enough callers above it to carry
    # the whole chain, so every hop lands on a different, real service.
    for _ in range(50):
        services, graph = _build_graph(rng, rng.randint(max(n_low, hops_needed + 1), n_high))
        eligible = [s for s in services if len(_caller_chains(graph, s)) >= hops_needed]
        if eligible:
            break
    else:  # pragma: no cover - 50 failed draws does not happen at these sizes
        raise RuntimeError("could not build a topology deep enough for the chain")
    if mode_name == "failover_bug":
        # A primary/standby failover happens to a stateful backend.
        eligible = [s for s in eligible if s in BACKENDS] or eligible
    target = rng.choice(eligible)
    callers = _caller_chains(graph, target)[:hops_needed]

    start = datetime(2026, rng.randint(5, 9), rng.randint(1, 28),
                     rng.randint(0, 23), rng.randint(0, 59), tzinfo=timezone.utc)
    duration = timedelta(minutes=rng.randint(9, 28))
    end = start + duration
    lead = timedelta(minutes=rng.randint(*mode["lead_minutes"]))
    variant = rng.choice(mode["culprits"])
    small = str(rng.choice([4, 5, 8, 10]))
    span_error = _fill(variant.get("span_error") or mode["span_error"], rng)
    target_errors = variant.get("errors") or mode["errors"]

    def service_for(placeholder: str) -> str:
        if placeholder == "{target_service}":
            return target
        return callers[int(placeholder[len("{upstream_"):-1]) - 1]

    chain = [{"service": service_for(step["service"]), "effect": step["effect"]}
             for step in chain_template]
    affected = [target] + callers
    # The failover bug lives in the service that calls the target.
    culprit_service = callers[0] if mode_name == "failover_bug" else target

    # ---- commits ----
    commits: list[dict[str, Any]] = []

    def add_commit(service: str, when: datetime, message: str, diff: str, relevant: bool) -> str:
        commit_hash = f"commit-{_short_hash(seed, f'c{len(commits)}')}"
        commits.append({
            "hash": commit_hash, "service": service, "timestamp": _iso(when),
            "author": f"{rng.choice(AUTHORS)}@example.com",
            "message": message, "diff": diff, "relevant": relevant,
        })
        return commit_hash

    culprit_commit = ""
    if "diff" in variant:
        culprit_commit = add_commit(
            culprit_service, start - lead,
            _fill(variant["message"], rng, target=target),
            _fill(variant["diff"], rng, small=small), relevant=True)

    decoys = rng.sample(DECOY_COMMITS, min(len(DECOY_COMMITS), rng.randint(decoy_low, decoy_high)))
    decoys = [(m, d, None) for m, d in decoys]
    if difficulty != "easy":
        # Alarming-sounding changes on services that are not on the failure path.
        bystanders = [s for s in services if s not in affected] or services
        count = 1 if difficulty == "medium" else 3
        for message, diff in rng.sample(ALARMING_DECOYS, count):
            decoys.append((message, diff, rng.choice(bystanders)))
    # Timing must not give the answer away, and must not be a reliable tell in
    # the other direction either. Usually a harmless change lands minutes
    # before the outage: often on the failing service, sometimes elsewhere,
    # and sometimes there is none.
    roll = rng.random()
    for index, (message, diff, forced_service) in enumerate(decoys):
        service = forced_service or rng.choice(services)
        minutes_before = rng.randint(45, 24 * 60)
        if index == 0 and roll < 0.55:
            service, minutes_before = culprit_service, rng.randint(3, 20)
        elif index == 0 and roll < 0.80:
            minutes_before = rng.randint(2, 15)
        add_commit(service, start - timedelta(minutes=minutes_before),
                   message, _fill(diff, rng, svc=service), relevant=False)
    commits.sort(key=lambda c: c["timestamp"])

    # ---- config changes ----
    config_changes: list[dict[str, Any]] = []
    culprit_config = ""
    config_rows = [(k, o, n, d, None, False) for k, o, n, d in
                   rng.sample(DECOY_CONFIGS, rng.randint(3, min(len(DECOY_CONFIGS), 3 + decoy_low // 2)))]
    if "key" in variant:
        config_rows.append((variant["key"], variant["old"], variant["new"],
                            variant["description"], start - lead, True))
    rng.shuffle(config_rows)
    for index, (key, old, new, description, when, is_culprit) in enumerate(config_rows):
        config_id = f"cfg-{seed}-{index:03d}"
        if is_culprit:
            culprit_config = config_id
        config_changes.append({
            "config_id": config_id,
            "service": target if is_culprit else rng.choice(services),
            "timestamp": _iso(when or start - timedelta(minutes=rng.randint(30, 24 * 60))),
            "key": key, "old_value": old, "new_value": new,
            "description": description, "relevant": is_culprit,
        })
    config_changes.sort(key=lambda c: c["timestamp"])

    # ---- infrastructure events ----
    infra_events: list[dict[str, Any]] = []
    culprit_infra = ""
    infra_rows = [(t, d, None, False) for t, d in
                  rng.sample(DECOY_INFRA, rng.randint(2, 4 if difficulty != "easy" else 2))]
    if mode_name == "failover_bug":
        infra_rows.append((
            "network_degradation",
            f"Packet loss on subnet 10.{rng.randint(1, 60)}.0.0/16 in zone a. "
            f"{target} primary moved to the standby in zone b.",
            start - timedelta(minutes=rng.randint(1, 3)), True))
    rng.shuffle(infra_rows)
    for index, (event_type, description, when, is_culprit) in enumerate(infra_rows):
        event_id = f"infra-{seed}-{index:03d}"
        if is_culprit:
            culprit_infra = event_id
        infra_events.append({
            "event_id": event_id,
            "timestamp": _iso(when or start - timedelta(minutes=rng.randint(20, 18 * 60))),
            "type": event_type, "description": description, "relevant": is_culprit,
        })
    infra_events.sort(key=lambda e: e["timestamp"])

    # ---- logs ----
    logs: dict[str, list[dict[str, Any]]] = {}
    noise_per_service = {"easy": 8, "medium": 12, "hard": 16}[difficulty]

    def during() -> datetime:
        return start + timedelta(seconds=rng.randint(5, int(duration.total_seconds()) - 5))

    for service in services:
        entries: list[tuple[datetime, str, str, bool]] = []
        for _ in range(noise_per_service):
            level, text = rng.choice(NOISE_LOGS)
            when = start - timedelta(minutes=rng.randint(-int(duration.total_seconds() // 60), 24 * 60))
            entries.append((when, level, _fill(text, rng), False))

        if service == target:
            precursors = mode["precursors"]
            for i, (level, text) in enumerate(precursors):
                # Early warnings are spread across the gap between the change and the outage.
                fraction = (i + 1) / (len(precursors) + 1)
                entries.append((start - lead * (1 - fraction), level,
                                _fill(text, rng, small=small), True))
            for level, text in target_errors:
                for _ in range(2):
                    entries.append((during(), level, _fill(text, rng, small=small), True))
        if mode_name == "failover_bug" and service == callers[0]:
            moment = start
            for level, text in FAILOVER_CLIENT_ERRORS:
                # In order: connections closed, pool empty, then requests failing.
                moment += timedelta(seconds=rng.randint(2, 25))
                entries.append((moment, level, _fill(text, rng, target=target), True))
        if service in callers:
            dependency = affected[affected.index(service) - 1]
            for level, text in rng.sample(DEPENDANT_ERRORS, 3):
                entries.append((during(), level,
                                _fill(text, rng, dep=dependency, span_error=span_error), True))

        entries.sort(key=lambda e: e[0])
        logs[service] = [
            {"id": f"log-{seed}-{services.index(service):02d}-{i:03d}", "timestamp": _iso(when),
             "level": level, "message": message, "relevant": relevant}
            for i, (when, level, message, relevant) in enumerate(entries)
        ]

    # ---- traces ----
    traces: list[dict[str, Any]] = []
    failing_path = list(reversed(affected))          # outermost caller ... target
    n_traces = {"easy": 8, "medium": 12, "hard": 16}[difficulty]
    for index in range(n_traces):
        failing = index % 2 == 0
        if failing:
            when = during()
            spans = []
            for service in failing_path:
                at_origin = service == target
                # In a failover the origin itself answers; its caller cannot reach it.
                broken = not (mode_name == "failover_bug" and at_origin)
                spans.append({
                    "service": service, "operation": rng.choice(ENDPOINTS),
                    "duration_ms": rng.randint(4000, 14000) if broken else rng.randint(5, 60),
                    "status": "ERROR" if broken else "OK",
                    **({"error": span_error if at_origin or (
                        mode_name == "failover_bug" and service == callers[0])
                        else f"upstream {affected[affected.index(service) - 1]} failed"}
                       if broken else {}),
                })
        else:
            when = start - timedelta(minutes=rng.randint(10, 600))
            walk = [services[0]]
            while graph[walk[-1]] and len(walk) < 4:
                walk.append(rng.choice(graph[walk[-1]]))
            spans = [{"service": s, "operation": rng.choice(ENDPOINTS),
                      "duration_ms": rng.randint(4, 180), "status": "OK"} for s in walk]
        traces.append({"trace_id": f"trace-{seed}-{index:03d}", "timestamp": _iso(when),
                       "spans": spans, "relevant": failing})
    traces.sort(key=lambda t: t["timestamp"])

    # ---- service summary ----
    service_rows = []
    for service in services:
        if service in affected:
            depth = affected.index(service)
            origin_down = service == (callers[0] if mode_name == "failover_bug" else target)
            rate = rng.uniform(88, 99) if origin_down else rng.uniform(35, 80) - depth * 4
            status = "down" if origin_down else "degraded"
        else:
            rate, status = rng.uniform(0, 1.5), "healthy"
        service_rows.append({
            "name": service, "status": status, "dependencies": list(graph[service]),
            "recent_deploy_count": sum(1 for c in commits if c["service"] == service),
            "error_rate_during_incident": round(max(rate, 0.0), 1),
        })

    # ---- ground truth ----
    cause_type = FAILURE_TEMPLATES[mode_name]["root_cause_type"]
    ground_truth: dict[str, Any] = {"cause_type": cause_type, "chain": chain}
    if cause_type == "config":
        ground_truth["cause"] = culprit_config
    elif cause_type == "correlated":
        ground_truth["cause"] = f"{culprit_commit}+{culprit_infra}"
        ground_truth["contributing_causes"] = [culprit_commit, culprit_infra]
    else:
        ground_truth["cause"] = culprit_commit

    relevant = [e["id"] for entries in logs.values() for e in entries if e["relevant"]]
    relevant += [t["trace_id"] for t in traces if t["relevant"]]
    relevant += [c["hash"] for c in commits if c["relevant"]]
    relevant += [c["config_id"] for c in config_changes if c["relevant"]]
    relevant += [e["event_id"] for e in infra_events if e["relevant"]]

    edge = failing_path[0]
    description = (
        f"Starting {start.strftime('%H:%M')} UTC, {edge} returned errors for "
        f"{int(duration.total_seconds() // 60)} minutes and {len(affected)} services were "
        f"degraded or down. Find what started the outage and how the failure spread."
    )

    return {
        "task_id": f"seed_{seed}_{difficulty}",
        "task_name": f"Generated incident #{seed}",
        "task_difficulty": difficulty,
        "task_description": description,
        "max_steps": max_steps,
        "service_graph": graph,
        "services": service_rows,
        "incident_window": {"start": _iso(start), "end": _iso(end)},
        "logs": logs,
        "traces": traces,
        "commits": commits,
        "config_changes": config_changes,
        "infra_events": infra_events,
        "relevant_fact_ids": relevant,
        "ground_truth": ground_truth,
        "failure_mode": mode_name,
    }
