"""Runtime configuration for the copilot.

Everything is read from environment variables so the same code runs locally,
in Docker and on a Nebius Serverless Endpoint. See ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"

# The three jobs an investigation needs, cheapest first. Each maps to one
# NVIDIA Nemotron model served on Nebius Token Factory.
ROLE_TRIAGE = "triage"    # many small calls: pick the next piece of evidence
ROLE_REASON = "reason"    # one or two big calls: root cause + causal chain
ROLE_WRITER = "writer"    # one call: turn the diagnosis into a postmortem
ROLES = (ROLE_TRIAGE, ROLE_REASON, ROLE_WRITER)

# Model IDs and prices are from the Token Factory catalog as of 2026-10-08.
DEFAULT_MODELS = {
    ROLE_TRIAGE: "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    ROLE_REASON: "nvidia/Nemotron-3-Ultra-550b-a55b",
    ROLE_WRITER: "nvidia/nemotron-3-super-120b-a12b",
}

# USD per 1M tokens: (input, output). Keys are lower-cased model IDs.
PRICES_PER_MTOK = {
    "nvidia/nvidia-nemotron-3-nano-30b-a3b": (0.06, 0.24),
    "nvidia/nemotron-3-super-120b-a12b": (0.30, 0.90),
    "nvidia/nemotron-3-ultra-550b-a55b": (1.00, 3.00),
    "nvidia/nemotron-3_5-lightning": (0.06, 0.24),
}

_ENV_BY_ROLE = {
    ROLE_TRIAGE: "COPILOT_MODEL_TRIAGE",
    ROLE_REASON: "COPILOT_MODEL_REASON",
    ROLE_WRITER: "COPILOT_MODEL_WRITER",
}


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    base_url: str
    models: dict[str, str]
    tavily_api_key: str | None

    def model_for(self, role: str) -> str:
        return self.models[role]


def load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=VALUE lines from a local .env file. Real environment
    variables win, so a deployment's secrets are never overridden."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if value:
            os.environ.setdefault(key.strip(), value)


def load_settings() -> Settings:
    models = {
        role: os.environ.get(_ENV_BY_ROLE[role]) or DEFAULT_MODELS[role]
        for role in ROLES
    }
    return Settings(
        api_key=os.environ.get("NEBIUS_API_KEY") or None,
        base_url=(os.environ.get("NEBIUS_BASE_URL") or NEBIUS_BASE_URL).rstrip("/"),
        models=models,
        tavily_api_key=os.environ.get("TAVILY_API_KEY") or None,
    )


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimated cost of one call. Unknown models cost 0 rather than guessing."""
    price = PRICES_PER_MTOK.get(model.lower())
    if price is None:
        return 0.0
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
