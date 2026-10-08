# Hindsight

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**The postmortem, with its evidence.** Hindsight investigates a production incident from its telemetry, names what started the outage, and writes the postmortem. Every claim in the document carries a tag back to the log line, diff or trace it came from.

Built for the Nebius x NVIDIA Global AI Hackathon. It runs on NVIDIA Nemotron models served by Nebius Token Factory.

> **Status: in development.** The pipeline, API and UI work end to end and are covered by tests. Benchmark numbers against real Nemotron models are not in this README yet; they will be added when the runs are done, not before.

## What it does

1. **Takes an incident.** A sample incident, a generated one, or your own bundle of logs, traces, commits, config changes and infrastructure events ([bundle format](static/copilot/bundle-format.html)).
2. **Investigates it blind.** The agent has read-only evidence tools and nothing else. Each lookup becomes a numbered exhibit: E1, E2, E3.
3. **Names the cause.** It commits to a root cause and the chain of failures that followed, citing exhibits for each hop, and says what it ruled out and what it could not settle.
4. **Checks the wider world.** It searches for published guidance on that failure mode, without sending anything incident-specific to the search engine.
5. **Writes the postmortem.** Summary, impact, root cause, how it spread, timeline, action items, open questions, evidence appendix and what the investigation cost. Download it as Markdown.

For incidents with a known answer, a deterministic grader scores the investigation, so accuracy is measured, not asserted.

## How it uses NVIDIA Nemotron on Nebius

An investigation is three different jobs, and each goes to the smallest Nemotron model that can do it. All calls are runtime calls to the Token Factory chat-completions API ([copilot/llm.py](copilot/llm.py)).

| Stage | Job | Model | Why this one |
|---|---|---|---|
| Triage | Choose the next lookup. Many short calls. | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Fast and cheap; each decision is small. |
| Diagnosis | Read the whole evidence ledger, name the cause and chain. One or two calls. | `nvidia/Nemotron-3-Ultra-550b-a55b` | The one step where reasoning quality decides the outcome. |
| Postmortem | Write the summary, impact and action items. One call. | `nvidia/nemotron-3-super-120b-a12b` | Good prose without paying Ultra prices. |

The UI shows which model made each move and the running cost per stage. Models are configurable per role ([.env.example](.env.example)), and `python -m copilot bench` compares routed against single-model configurations on the same incidents.

Web research uses the **Tavily** API ([copilot/research.py](copilot/research.py)).

## Design choices that matter

- **No oracle.** The underlying environment has actions that reveal whether a guess is right. The agent cannot reach them ([copilot/workspace.py](copilot/workspace.py)). A graded score reflects an investigation done without feedback from the answer.
- **The model cannot put facts in the document.** Root cause, chain, timeline and citations are assembled in code from a validated diagnosis. IDs, services and citations the model invents are dropped. The writer model contributes prose only ([copilot/report.py](copilot/report.py)).
- **Nothing internal reaches web search.** Search queries are written to be generic, and any query containing a service name, change ID, trace ID or email address from the incident is dropped before it is sent.
- **Graded by rubric, not by a model.** Cause correctness, chain accuracy, efficiency, investigation quality and anti-gaming, scored deterministically ([engine/grader.py](engine/grader.py)).
- **The test incidents do not give the answer away.** Generated incidents ([data/incident_generator.py](data/incident_generator.py)) have a symptom-only brief, a realistic diff on every commit, harmless changes that land closer to the outage than the culprit, and alarming-sounding changes that are innocent. In most of them the most recent change is not the cause.
- **It says when it does not know.** An investigation that cannot name a cause produces a document that says so and lists the open questions.

## Run it

Requires Python 3.11 or later.

```bash
git clone https://github.com/jacklachan/Nebius-x-NVIDIA.git
cd Nebius-x-NVIDIA
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt    # macOS/Linux: .venv/bin/python
cp .env.example .env                                        # then add your keys
```

Put a Nebius Token Factory key in `.env` as `NEBIUS_API_KEY`. `TAVILY_API_KEY` is optional and enables the research step.

```bash
# Check the configured Nemotron models exist on your account
python -m copilot models

# Web UI at http://localhost:7860
uvicorn app:app --port 7860

# One investigation from the command line
python -m copilot investigate --seed 42 --difficulty medium --out postmortem.md

# Your own incident
python -m copilot investigate --file my_incident.json --out postmortem.md

# Score the investigator on 20 generated incidents
python -m copilot bench --seeds 0-19 --difficulty medium --out benchmarks/routed-medium.json
```

Without a key you can still see the interface: `python scripts/dev_ui_server.py` runs it with a scripted stand-in for the models. The stand-in reads the answer, so it demonstrates the UI and nothing about model quality.

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

To deploy it on a Nebius Serverless Endpoint, see [docs/DEPLOY.md](docs/DEPLOY.md).

## Layout

```
copilot/            the product
  llm.py            Token Factory client: per-role routing, retries, cost metering
  workspace.py      blind evidence tools over the environment
  investigator.py   triage, diagnosis, validation, grading
  research.py       Tavily search with private terms kept out of queries
  report.py         the postmortem document
  bench.py          benchmark runner
web/copilot_api.py  HTTP API and live event stream
static/copilot/     the UI
data/incident_generator.py   incidents with coherent telemetry and decoys
engine/, data/      PostmortemEnv: scenarios, original generator, deterministic grader
```

## What existed before this hackathon

Hindsight is built on **PostmortemEnv**, a reinforcement-learning environment our team made for the Meta PyTorch OpenEnv Hackathon in April 2026. Its original README is kept at [docs/POSTMORTEMENV.md](docs/POSTMORTEMENV.md), and its history is the start of this repository's commit log.

Carried over: the incident scenarios and procedural generator, the environment, the deterministic grader, and the original console (now at `/lab`).

Built during the Nebius x NVIDIA submission period: everything in `copilot/`, `web/copilot_api.py` and `static/copilot/`. That is the blind investigator, Nemotron model routing on Token Factory, the Tavily research step, the postmortem writer, support for uploaded incidents, the benchmark runner, a new incident generator and the product UI.

## Known limits

- Generated incidents cover five failure modes (connection pool exhaustion, memory leak, out-of-memory crash loop, config change, failover bug triggered by a network event). They are useful for measuring the agent and are simpler than real outages.
- Causal-chain scoring needs exact labels, so the agent labels each hop from a fixed taxonomy of failure modes. The five hand-written tasks use free-text labels, so chain scores on those understate the agent; root-cause accuracy is the number to trust there.
- The agent can only name a cause that is present in the bundle. If the change that broke production was never exported, it will not be found.

## Team

L Mohit Jain, Tanush Deepak, Utkarsh Singh Yadav.

## License

[MIT](LICENSE)
