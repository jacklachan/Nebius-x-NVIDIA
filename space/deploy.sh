#!/usr/bin/env bash
# Deploy the product to the Hugging Face Space. Run from the repository root
# after `hf auth login`. The Space gets only what the product needs, plus the
# README, requirements and entry point in this folder.
set -euo pipefail
SPACE="${SPACE:-Auenchanters/hindsight}"

# --delete removes files under these folders that are no longer in the repo,
# so a renamed or deleted module does not linger on the Space.
hf upload "$SPACE" . . --repo-type space --commit-message "Deploy from GitHub" \
  --include "app.py" --include "LICENSE" --include "copilot/**" \
  --include "web/__init__.py" --include "web/copilot_api.py" --include "web/mcp_mount.py" \
  --include "data/__init__.py" --include "data/generator.py" \
  --include "data/incident_generator.py" --include "data/scenarios/**" \
  --include "data/solutions/**" --include "static/copilot/**" --include "benchmarks/**" \
  --exclude "**/__pycache__/**" \
  --delete "copilot/**" --delete "web/**" --delete "data/**" --delete "static/**" \
  --delete "benchmarks/**"
hf upload "$SPACE" space . --repo-type space --commit-message "Space entry point" \
  --exclude "deploy.sh"
