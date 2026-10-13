# TeamPulse: AI Ops Agent for Small Engineering Teams

## Goal
A multi-agent system that reads GitHub and Jira, finds stalled work, and posts
a prioritized briefing to Slack. Any write action (Jira comment or ticket)
needs human approval through a Slack button.

## Stack
Python 3.11, FastAPI, LangGraph, LangChain, Pydantic v2, pydantic-settings,
httpx, PyGithub, slack-bolt (async), APScheduler, SQLite (SQLAlchemy),
pytest, pytest-asyncio, Docker. Deploy target: GCP Cloud Run.

## LLM layer
- Router chooses the model per task:
  - Simple classification and small tasks -> local Ollama model
  - Long-context analysis -> Gemini (cloud)
- Model names come from env vars (OLLAMA_MODEL, GEMINI_MODEL). Never hardcode them.
- All LLM output must be validated with Pydantic. On invalid JSON, re-prompt
  once with the schema. If it still fails, fall back to a rule-based summary.

## Graph (LangGraph), 3 nodes
Collect -> Analyze -> Act
- Collect: fetch GitHub PRs and Jira issues in parallel (asyncio.gather).
  Compute age in days and a stale flag. If a source fails, mark it
  "unavailable" and continue.
- Analyze: LLM finds patterns and priority flags. Skips items flagged
  in the last 24h unless they escalated (read the flags table in SQLite).
- Act: build the Slack Block Kit briefing. Add Approve/Reject buttons for
  suggested Jira actions. Only execute after approval. Log every action.

Shared state is a typed Pydantic model (TeamPulseState) with:
workspace_id, run_type (morning|evening|ondemand), query, github_data,
jira_data, analysis, priority_items, briefing, previous_flags, errors.

## Folder structure
teampulse/
  app/
    agents/ (collect.py, analyze.py, act.py)
    graph/ (state.py, pipeline.py)
    integrations/ (github_client.py, jira_client.py, slack_client.py)
    llm/ (router.py, ollama_client.py, gemini_client.py, schemas.py)
    core/ (errors.py, retry.py, rate_limiter.py, security.py)
    db/ (database.py, models.py)
    api/ (main.py, routes/)
    scheduler/ (jobs.py)
    config.py
  tests/
  evals/ (labeled_cases.json, run_eval.py)
  Dockerfile
  docker-compose.yml
  requirements.txt
  .env.example
  .gitignore
  README.md
  PROJECT_SPEC.md

## Rules for every phase
1. Build ONLY the phase I ask for. Stop and show me how to run and test it.
2. No secrets in code. All config in .env, with a complete .env.example.
3. Type hints and docstrings on every function.
4. Every integration has retry with exponential backoff and rate limiting.
5. Custom errors: TeamPulseError -> IntegrationError, AnalysisError, ActionError.
   A partial briefing is better than no briefing.
6. Write pytest tests for each module, mocking external APIs. Report the real
   test count after each phase.
7. Slack requests must be verified with HMAC-SHA256 signature and timestamp check
   (reject if older than 5 minutes).
8. Keep the code simple and readable. I must be able to explain every file.
9. After each phase, suggest a git commit message.

## Phases
1 Setup, config, GitHub client, stale PR detection (no LLM)
2 Jira client, LangGraph skeleton with 3 nodes, state model
3 LLM layer: router, Ollama, Gemini, Pydantic validation, fallback
4 Slack briefing, approval buttons, action log in SQLite
5 FastAPI routes, scheduler, Slack signature verification
6 Pytest suite and evals (labeled cases, accuracy and latency report)
7 Docker, Cloud Run deploy script, README with architecture diagram