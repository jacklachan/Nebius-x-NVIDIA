# Handoff

For Tanush and Utkarsh. Written 8 October 2026. Read this, then the README.

## Where things stand

We are entering the **Nebius x NVIDIA Global AI Hackathon** with **Hindsight**: it investigates a production incident from its telemetry, names the root cause, and writes the postmortem with every claim tied to the evidence behind it.

- **Deadline:** 30 October 2026, 10:00 am Pacific (10:30 pm IST).
- **Repo:** https://github.com/jacklachan/Nebius-x-NVIDIA, branch `main`. CI is green.
- **Built and tested:** the whole pipeline, the web UI, the API, the MCP server and the benchmark. 606 tests pass.
- **Not done:** it has never run against a real Nemotron model. We have no Nebius API key yet. Everything that needs real model output is still open.

That last point is the main risk. The prompts are untested on real models, and the leaderboard has no Nemotron row.

## What it is built from

The product runs on new code. Our April project (PostmortemEnv) is still in the repo but is only used by the old console at `/lab`. A test fails if product code imports it.

| Part | File | What it does |
|---|---|---|
| Model client | `copilot/llm.py` | Calls Nebius Token Factory, routes each job to a model, tracks cost |
| Evidence tools | `copilot/workspace.py` | Read-only log, trace, commit and config lookups. No way to check a guess |
| Investigator | `copilot/investigator.py` | Nemotron Nano picks lookups, Ultra diagnoses, Super writes |
| Research | `copilot/research.py` | Tavily search, with internal names stripped from queries |
| Postmortem | `copilot/report.py` | Facts assembled in code, prose from the model |
| Scoring | `copilot/evaluate.py` | Root cause, failure path, failure modes, grounding, efficiency |
| Oracle | `copilot/oracle.py` | Given the answer, still has to find the evidence. The ceiling |
| Incidents | `data/incident_generator.py` | Generated incidents with decoys and symptom-only briefs |
| Bundle builder | `copilot/bundle.py` | Builds an incident from real git repos and log files |
| MCP server | `copilot/mcp_server.py` | Same investigator as tools for coding agents |
| Web | `app.py`, `web/copilot_api.py`, `static/copilot/` | UI, API, live event stream |

## Run it

Python 3.11 or later.

```bash
git clone https://github.com/jacklachan/Nebius-x-NVIDIA.git
cd Nebius-x-NVIDIA
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest tests/ -q
.venv/Scripts/python -m uvicorn app:app --port 7860
```

On macOS or Linux use `.venv/bin/python`.

Open http://localhost:7860. With no key, **Investigate** is disabled, but every sample incident has **See the reference answer**, which runs the oracle and shows the full flow.

## What to do when the key arrives

Put it in a file named `.env` in the repo root. Never commit it and never paste it in chat.

```
NEBIUS_API_KEY=...
TAVILY_API_KEY=...
```

Then, in this order:

1. **Check the model names.** `python -m copilot models`. The three IDs in `copilot/config.py` came from the Nebius catalog and have not been confirmed against a live account. Fix them there or in `.env` if any is reported missing.
2. **Run one investigation and read it.** `python -m copilot investigate --seed 42 --difficulty medium --out postmortem.md`. Look for unparseable replies, wasted lookups and a wrong or uncited diagnosis.
3. **Tune the prompts.** They are `TRIAGE_SYSTEM` and `DIAGNOSIS_SYSTEM` in `copilot/investigator.py`, and `WRITER_SYSTEM` in `copilot/report.py`.
4. **Run the benchmark.** `python -m copilot bench --seeds 0-49 --difficulty medium --label routed --out benchmarks/routed-medium.json`, then easy and hard. Results appear on the home page leaderboard automatically.
5. **Compare configurations.** Rerun with `--triage` and `--reason` set to the same model, with a different `--label`, to show what routing buys in accuracy and cost.
6. **Record real runs for the demo.** `python -m copilot investigate --seed 42 --difficulty medium --record copilot/recordings/seed-42-medium.json`. Recordings replay without a key, so the hosted demo survives running out of credits.
7. **Deploy.** Follow `docs/DEPLOY.md`. Those steps were written from the Nebius docs and have not been run against a live account.
8. **Put the numbers in the README**, record the video, write the Devpost entry.

Watch the spend while tuning. We have not measured what an investigation costs yet; the CLI prints the cost per model after each run. Ultra is the expensive part and the benchmark runs many investigations, so start with a few seeds.

## Numbers we have today

Root-cause accuracy on 200 generated incidents per difficulty. No model involved.

| Approach | Easy | Medium | Hard |
|---|---|---|---|
| Guess at random | 11% | 4% | 3% |
| Blame the latest change | 26% | 29% | 20% |
| Blame the latest change on the worst-hit service | 41% | 51% | 37% |
| Oracle (given the answer) | 100% | 100% | 100% |

Nemotron has to land well above the third row for the project to make its case. If it does not, that is the first thing to fix.

## Submission checklist

From the official rules. All are required.

- [ ] Working project that calls Token Factory at runtime with an NVIDIA model
- [ ] Track chosen: Coding and Agentic Engineering, or Best Apps and Agents
- [ ] Text description
- [ ] Demo URL that judges can open
- [ ] Public YouTube video, under 3 minutes, showing it working and how Nemotron and Token Factory are used
- [x] Public repo with an open source licence visible on the repo page (MIT)
- [x] README with setup instructions and how the models are used
- [ ] Feedback on Token Factory and the NVIDIA models
- [x] Written account of what changed since the pre-existing project (README, "What existed before this hackathon")
- [ ] Optional: Tavily is called at runtime, which qualifies for the $3,000 bonus once a key is in

## Open decisions

- **Name.** "Hindsight" was picked so the UI had something to show. Nobody on the team has confirmed it. Changing it after the video is recorded is expensive.
- **Track.** Not chosen. The MCP server and bundle builder lean toward Coding and Agentic Engineering.
- **Old code.** The April environment is still in the repo at `/lab`. It could be removed, but it is the evidence for the "what existed before" statement.
- **Amazon hackathon.** Deadline 24 October, 12:30 am IST. Not started. The MCP server is the basis for an Alexa+ entry if we decide to do it.

## Things that will trip you up

- **Do not kill all Python processes** to stop a server. It takes down every other Python program on the machine. Stop the one process by its ID.
- **Do not commit dev-server output as a recording.** `scripts/dev_ui_server.py` uses a stand-in that reads the answer. Only real runs go in `copilot/recordings/`.
- **The MCP path is `/api/copilot/mcp`**, not `/mcp`. A hosted deployment also needs `COPILOT_MCP_ALLOWED_HOSTS` set to its hostname.
- **MCP SDK is version 2.** `FastMCP` is now `MCPServer`, and errors meant for the caller must be raised as `ToolError`.
- **Hand-written sample incidents score low on failure modes** because their chains use free-text labels. Root cause and failure path are the scores to read there.
- **Commit in small pieces** with tests passing, and push to `main`. CI runs the tests on Python 3.11 and builds the Docker image.
- **No numbers in the README that we have not measured.** The baselines and oracle rows are reproducible from the code, and a test checks that.
