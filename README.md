# Hindsight

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**The postmortem, with its evidence.** Hindsight investigates a production incident from its telemetry, names what started the outage, and writes the postmortem. Every claim in the document carries a tag back to the log line, diff or trace it came from.

Built for the Nebius x NVIDIA Global AI Hackathon. It runs on NVIDIA Nemotron models served by Nebius Token Factory.

> **Status: in development.** The pipeline, API and UI work end to end and are covered by tests. Benchmark numbers against real Nemotron models are not in this README yet; they will be added when the runs are done, not before.

## What it does

1. **Takes an incident.** A sample incident, a generated one, or your own. `python -m copilot bundle` builds one from your services' git history and log files; the [bundle format](static/copilot/bundle-format.html) also takes traces, config changes and infrastructure events.
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

- **No oracle.** The agent's workspace offers read-only evidence tools and nothing else ([copilot/workspace.py](copilot/workspace.py)). There is no way to ask whether a guess is right, and nothing it returns carries the answer or the labels that mark which facts matter.
- **The model cannot put facts in the document.** Root cause, chain, timeline and citations are assembled in code from a validated diagnosis. IDs, services and citations the model invents are dropped. The writer model contributes prose only ([copilot/report.py](copilot/report.py)).
- **Nothing internal reaches web search.** Search queries are written to be generic, and any query containing a service name, change ID, trace ID or email address from the incident is dropped before it is sent.
- **Scored by rule, not by a model, and guessing does not pay.** Five scores ([copilot/evaluate.py](copilot/evaluate.py)): root cause, failure path (the right services in order), failure modes (the right label at each hop), grounding, and efficiency. Grounding checks the work: did it open the change it blames, and do the exhibits it cites actually bear on the incident? Naming the right commit without investigating scores 0.55; the same answer properly investigated scores 1.00.
- **The test incidents do not give the answer away.** Generated incidents ([data/incident_generator.py](data/incident_generator.py)) have a symptom-only brief, a realistic diff on every commit, harmless changes that land closer to the outage than the culprit, and alarming-sounding changes that are innocent. In most of them the most recent change is not the cause.
- **A real ceiling, and proof the incidents are solvable.** The oracle ([copilot/oracle.py](copilot/oracle.py)) is given the answer but still has to go through the same evidence tools, open every change it blames, and find an exhibit for each step of the failure. It scores 1.00 on every generated incident in about five lookups, which shows the evidence for each answer is really there and sets the bar for efficiency. A test fails if any generated incident cannot support its own answer.
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

# Your own incident: build a bundle from git history and log files, then investigate it
python -m copilot bundle --start 2026-10-01T10:00:00Z --end 2026-10-01T10:20:00Z     --repo api=../api --repo web=../web --logs api=logs/api.log     --services services.json --out my_incident.json
python -m copilot investigate --file my_incident.json --out postmortem.md

# Score the investigator on 20 generated incidents
python -m copilot bench --seeds 0-19 --difficulty medium --out benchmarks/routed-medium.json
```

Without a key the app still shows real investigations: every sample incident has a **See the reference answer** button, which runs the oracle (below) with no model and no spend. For front-end work, `python scripts/dev_ui_server.py` runs the UI with a scripted stand-in for the models; it demonstrates the interface and nothing about model quality.

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## Use it from a coding agent (MCP)

Hindsight is also an MCP server, so an agent in your editor can investigate an incident without leaving the repository it is working in.

```json
{
  "mcpServers": {
    "hindsight": {
      "command": "/path/to/Nebius-x-NVIDIA/.venv/bin/python",
      "args": ["-m", "copilot", "mcp"],
      "cwd": "/path/to/Nebius-x-NVIDIA"
    }
  }
}
```

| Tool | What it does |
|---|---|
| `build_incident_bundle` | Collects the day before an incident from local git repositories and log files into a bundle file. |
| `investigate_bundle_file` | Investigates that file and returns the root cause, how it spread, open questions and the postmortem. |
| `investigate_incident` | The same, with the bundle passed inline. |
| `investigate_sample`, `list_sample_incidents` | Try it on incidents with a known answer; the result is graded. |

The web app serves the same thing over Streamable HTTP at `/api/copilot/mcp`, under the same spending limits as the UI (`python -m copilot mcp --http` runs it on its own). The two tools that read local paths are only offered over stdio, where the caller is the machine's own user.

To deploy it on a Nebius Serverless Endpoint, see [docs/DEPLOY.md](docs/DEPLOY.md).

## Layout

```
copilot/            the product
  llm.py            Token Factory client: per-role routing, retries, cost metering
  workspace.py      blind evidence tools over the environment
  investigator.py   triage, diagnosis, validation, grading
  research.py       Tavily search with private terms kept out of queries
  report.py         the postmortem document
  bundle.py         build an incident bundle from git repos and log files
  mcp_server.py     the same investigator as MCP tools
  evaluate.py       deterministic scoring, including grounding
  oracle.py         reference investigator: the ceiling, and a solvability check
  taxonomy.py       failure-mode vocabulary
  bench.py          benchmark runner
web/copilot_api.py  HTTP API and live event stream
static/copilot/     the UI
data/incident_generator.py   incidents with coherent telemetry and decoys
web/lab.py, engine/ the original PostmortemEnv, attached at /lab (optional)
```

## What existed before this hackathon

Hindsight is built on **PostmortemEnv**, a reinforcement-learning environment our team made for the Meta PyTorch OpenEnv Hackathon in April 2026. Its original README is kept at [docs/POSTMORTEMENV.md](docs/POSTMORTEMENV.md), and its history is the start of this repository's commit log.

Carried over: the five hand-written sample incidents and the original environment with its console, now an optional add-on at `/lab` ([web/lab.py](web/lab.py)). The product does not import it; a test enforces that.

Built during the Nebius x NVIDIA submission period: everything the product runs on. That is the evidence store, the evaluator, the failure-mode vocabulary, the incident generator, the investigator with Nemotron model routing on Token Factory, the Tavily research step, the postmortem writer, the bundle builder, the MCP server, the benchmark runner, the web API and the product UI (`copilot/`, `data/incident_generator.py`, `web/copilot_api.py`, `web/mcp_mount.py`, `static/copilot/`, `app.py`). The original generator and grader were replaced rather than reused: the generator gave its answers away, and the grader could not tell an investigation from a lucky guess.

## Known limits

- Generated incidents cover five failure modes (connection pool exhaustion, memory leak, out-of-memory crash loop, config change, failover bug triggered by a network event). They are useful for measuring the agent and are simpler than real outages.
- Failure-mode scoring needs exact labels, so the agent labels each hop from a fixed vocabulary ([copilot/taxonomy.py](copilot/taxonomy.py)). The five hand-written incidents use free-text labels, so that one score understates the agent there; root cause and failure path are the figures to read.
- The agent can only name a cause that is present in the bundle. If the change that broke production was never exported, it will not be found.

## Team

L Mohit Jain, Tanush Deepak, Utkarsh Singh Yadav.

## License

[MIT](LICENSE)
