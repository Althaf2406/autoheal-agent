# AutoHeal Data Agent

AutoHeal Data Agent is an autonomous **GitHub Actions** that functions as an AI data engineer. This system uses a Multi-Agent System architecture to automatically detect, research, and fix broken data pipelines.

## System Architecture (Multi-Agent)
This system is divided into two main agents that work together:

1. **Agent Researcher (Entry Point & Analysis):**
   - Receives error reports (via Webhook) from GitHub Actions failures.
   - Uses the **Tavily API** to conduct smart research regarding the root cause (Root Cause Analysis).
2. **Agent Engineer (Execution & Repair):**
   - Receives research results from the Agent Researcher.
   - Uses **Nemotron 3 Ultra** to generate the fix code (SQL/Python).
   - Automatically opens a Pull Request (PR) with the fix to the target repository via the GitHub API.

## Directory Structure
This project uses a Monorepo structure:
```text
autoheal-agent/
├── backend/      # FastAPI, LangGraph/Multi-Agent Logic, API Integration
├── frontend/     # Observability Dashboard (Streamlit)
├── demo-etl-repo/ # Demo repo with a broken ETL + GitHub Actions (pushed as its own repo)
├── docs/          # Setup guides
└── requirements.txt
```

### Backend modules
| File | Owner | Purpose |
|---|---|---|
| `backend/main.py` | Shared | `POST /webhook`, `GET /runs`, history/resume, rate-limit endpoints; runs the graph in the background |
| `backend/graph.py` | Shared | LangGraph state machine with checkpoints (`data/checkpoints.sqlite`) |
| `backend/contracts.py` | Shared | Contract types between A and B + stubs for A's modules until they exist |
| `backend/agent_researcher.py` | Anggota A | Tavily research and Root Cause Analysis |
| `backend/agent_engineer.py` | Anggota B | Nemotron generates the fixed file, takes feedback from guardrails/sandbox |
| `backend/guardrails.py` | Anggota B | SAST (ast, ruff, bandit, semgrep) + destructive operation policy + diff budget |
| `backend/sandbox.py` | Anggota B | Clone at the failing commit, reproduce, apply patch, run pytest in Docker (`--network none`) |
| `backend/rate_limiter.py` | Anggota B | PR quota, fingerprint cooldown, circuit breaker, GitHub API backoff |
| `backend/escalation.py` | Anggota B | Escalation issue body + Slack notification |
| `backend/github_integration.py` | Anggota B | Auth, Draft PR + `autoheal-bot` label, comments, escalation issues |
| `backend/run_store.py` | Shared | Stores runs in `data/runs.json` for the dashboard |

### V2 flow
```text
webhook -> redact -> dedup --(duplicate)--> comment "Terjadi lagi (xN)" on the open PR
                       \-> research -> retrieve_context -> engineer -> SAST -> sandbox -> Draft PR (autoheal-bot)
                                                        ^__________|________|   max 3 iterations
                                                                    \________\-> escalate: GitHub Issue (+ Slack)
```
Docker is needed for the sandbox. Without it the sandbox is skipped (the PR body says so);
`AUTOHEAL_SANDBOX=local` runs the tests on the host for development only.

See [docs/github-app-setup.md](docs/github-app-setup.md) to set up the GitHub App, the demo repo and the full flow.

## How to Run Locally

1. **Clone the Repository & Install Dependencies**
   ```bash
   git clone https://github.com/Althaf2406/autoheal-agent.git
   cd autoheal-agent
   pip install -r requirements.txt
   ```

   Then copy `.env.example` to `.env` and fill in your API keys.

2. **Run the Backend (FastAPI)**
   ```bash
   uvicorn backend.main:app --reload
   ```
   *The backend server will run at `http://127.0.0.1:8000`*

3. **Run the Frontend (Streamlit)**
   Open a new terminal and run:
   ```bash
   streamlit run frontend/app.py
   ```
   *The dashboard will open in the browser at `http://localhost:8501`*

## Tech Stack
- **Backend:** Python, FastAPI, Uvicorn, LangGraph (Multi-Agent Workflow)
- **Frontend:** Streamlit
- **AI / LLM:** Nemotron 3 Ultra, Tavily API
- **Integration:** GitHub API (PyGithub), GitHub Actions
