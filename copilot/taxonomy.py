"""The vocabulary for describing how a failure spreads.

Each hop of a causal chain is labelled with one failure-mode label from a
fixed list. A closed vocabulary is what makes chains comparable between
incidents and scoreable without a judge model. The descriptions are shown to
the diagnosis model so it picks labels by meaning rather than by guessing at
names.
"""

from __future__ import annotations

EFFECTS: dict[str, str] = {
    # at the origin
    "connection_pool_exhaustion": "every pooled connection is in use and requests queue for one",
    "memory_leak_gc_pressure": "memory grows steadily and garbage collection eats the CPU",
    "response_latency_spike": "the service still answers, but far slower than normal",
    "oom_crash_loop": "the process is killed for exceeding its memory limit and keeps restarting",
    "resource_limit_exceeded": "a configured limit (workers, queue depth, rate) is too low for the load",
    "network_degradation": "packet loss or latency on the network path to the service",
    "failover_triggered": "the primary role moved to a standby",
    # in the service that depends on it
    "upstream_timeout": "calls to the dependency time out",
    "dependency_timeout": "calls to a crashed or restarting dependency time out",
    "cascading_timeout": "timeouts pile up behind a throttled or overloaded dependency",
    "timeout_cascade": "slow responses from the dependency exhaust the caller's own deadlines",
    "connection_failure": "connections to the dependency are closed or refused",
    "zero_connections_state": "the client is left holding no connections and never reconnects",
    # further out
    "service_degradation": "the service works partially, with elevated errors",
    "circuit_breaker_open": "the caller stops sending requests to protect itself",
    "5xx_errors_to_users": "users receive server errors",
    "user_facing_errors": "users see failed requests",
    "complete_unavailability": "the service answers no requests at all",
    "complete_service_failure": "every request to the service fails",
}

# How each failure mode unfolds. "origin" is the service where it starts;
# "caller_1" depends on the origin, "caller_2" depends on caller_1, and so on.
FAILURE_MODES: dict[str, dict] = {
    "connection_pool": {
        "cause_type": "commit",
        "chain": [("origin", "connection_pool_exhaustion"),
                  ("caller_1", "upstream_timeout"),
                  ("caller_2", "5xx_errors_to_users")],
    },
    "memory_leak": {
        "cause_type": "commit",
        "chain": [("origin", "memory_leak_gc_pressure"),
                  ("origin", "response_latency_spike"),
                  ("caller_1", "timeout_cascade"),
                  ("caller_2", "circuit_breaker_open")],
    },
    "oom_cascade": {
        "cause_type": "commit",
        "chain": [("origin", "oom_crash_loop"),
                  ("caller_1", "dependency_timeout"),
                  ("caller_2", "service_degradation"),
                  ("caller_3", "complete_unavailability")],
    },
    "config_drift": {
        "cause_type": "config",
        "chain": [("origin", "resource_limit_exceeded"),
                  ("caller_1", "cascading_timeout"),
                  ("caller_2", "user_facing_errors")],
    },
    "failover_bug": {
        "cause_type": "correlated",
        "chain": [("origin", "network_degradation"),
                  ("origin", "failover_triggered"),
                  ("caller_1", "connection_failure"),
                  ("caller_1", "zero_connections_state"),
                  ("caller_2", "complete_service_failure")],
    },
}

EFFECT_TAXONOMY: tuple[str, ...] = tuple(sorted(EFFECTS))

assert {effect for mode in FAILURE_MODES.values() for _, effect in mode["chain"]} == set(EFFECTS)


def callers_needed(mode: str) -> int:
    """How many services above the origin the mode's chain reaches."""
    return max((int(role.split("_")[1]) for role, _ in FAILURE_MODES[mode]["chain"]
                if role.startswith("caller_")), default=0)


def taxonomy_text() -> str:
    return "\n".join(f"- {label}: {meaning}" for label, meaning in EFFECTS.items())
