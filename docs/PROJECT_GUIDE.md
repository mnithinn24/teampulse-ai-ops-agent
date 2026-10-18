# TeamPulse Project Guide

A complete beginner-friendly guide to understanding, using, and developing TeamPulse.

---

# Part A: For Non-Technical People

This part explains what TeamPulse does in simple words, without code.

## 1. What TeamPulse Is

TeamPulse is an automated operations assistant for software engineering teams.
It connects to GitHub, Jira, and Slack to help teams manage their daily work:
- It finds pull requests and Jira tickets that have been forgotten or stuck.
- It sends a clean morning briefing to Slack with buttons so team leads can approve actions with one click.
- It breaks down new project ideas into complete Jira plans with Epics, Tasks, and Subtasks.
- It alerts the team when pull requests are ready, lets leads merge them safely with one click, and moves tickets to Done automatically.
- It detects when tasks are marked Done in Jira and posts a review card to Slack with code diff links so leads can verify or reopen them.

## 2. The Problems It Solves

Engineering teams face five common operational headaches every day:
1. **Stale Pull Requests**: A developer opens a pull request, but reviewers forget to check it. Code sits for weeks waiting for a review.
2. **Stuck Jira Tickets**: Tickets stay "In Progress" for weeks because nobody noticed they were blocked.
3. **Morning Tab Overload**: Team leads waste 30 to 60 minutes every morning opening dozens of browser tabs across GitHub and Jira just to figure out what needs attention.
4. **Tedious Project Planning**: Creating dozens of Jira tickets by hand for a new feature takes hours of typing, linking parents, and estimating hours.
5. **Slow Code Merges & Review Handoffs**: When a developer finishes a task, they have to ping their lead in Slack, wait for approval, manually merge on GitHub, and remember to update Jira.

## 3. What TeamPulse Does in 5 Main Workflows

1. **Daily Briefing**: TeamPulse scans GitHub and Jira every morning, finds items waiting too long, and posts an interactive briefing to Slack.
2. **One-Click Triage**: Team leads click `[ Approve ]` or `[ Dismiss ]` in Slack. TeamPulse creates a Jira triage ticket automatically and logs the decision.
3. **AI Project Planning**: You type a simple project sentence (like *"Medical RAG, 2 developers, 2 weeks"*), and TeamPulse creates Epics, Tasks, and Subtasks with balanced hours in Jira.
4. **PR Alert & One-Click Merge**: When a PR is opened, TeamPulse posts a card with Jira links. An authorized lead clicks `[ Approve & Merge ]`. TeamPulse verifies 5 safety checks, merges the code, and marks the Jira ticket Done.
5. **Task Completion Verification**: When a developer moves a ticket to "Done" in Jira, TeamPulse detects it within 15 seconds, finds the matching GitHub code diff search link, and asks the lead in Slack to verify or reopen it.

---

## 4. Full Walkthrough: Daily Briefing & Triage (Illustrative Example)

*(Note: The following ticket IDs, repositories, and pull requests are an **illustrative example** to explain how the system functions).*

### The Stuck Item
A developer opened pull request number 4372 in repository `psf/requests` 28 days ago.
Nobody has reviewed it, and no new code has been pushed for 28 days.

### The Slack Message
TeamPulse posts this message to the `#engineering-leads` Slack channel:

```text
TeamPulse Ops Briefing - Tuesday, October 13, 2026
Overview: 88 open PRs (88 stale) | 3 stale tickets

Urgent Priority
psf/requests#4372: Fix connection pool timeout
- Open for 28 days, no reviewers, last activity 28 days ago.
- Suggested Action: Ping author or create triage ticket.
[ Approve ]   [ Dismiss ]
```

### The Human Click
The team lead reads the card and clicks the `[ Approve ]` button.

### The Result in Slack
The buttons disappear immediately and are replaced by this confirmation:

```text
Approved by @lead - created KAN-15
```

### The Result in Jira
A new Jira ticket appears on the team board:
- **Key**: `KAN-15`
- **Summary**: `Triage stale PR psf/requests#4372`
- **Description**: Details about the pull request, author, and age.

---

## 4b. Walkthrough: AI Project Planner (Illustrative Example)

*(Note: The following project breakdown and hours are an **illustrative example**).*

### What You Type
In Slack or on the Web UI (`http://localhost:8000`), you enter:
```text
/teampulse plan Medical RAG with React UI, 2 developers, 2 weeks
```

### What TeamPulse Computes
1. **Total Available Capacity**: 2 developers $\times$ 2 weeks $\times$ 40 hours/week = **160 hours**.
2. **Epics & Tasks Breakdown**: It breaks the project into 4 Epics with Tasks and Subtasks:
   - Epic 1: System Architecture & Data Pipeline (48h)
     - Task: Ingestion & Document Parser (24h)
       - Subtask: PDF chunking & metadata sanitization (12h)
       - Subtask: Vector database indexing (12h)
   - Epic 2: API & Search Services (48h)
   - Epic 3: User Interface & Experience (40h)
   - Epic 4: Testing, Security & QA (24h)
3. **Capacity Allocation Note**: The hours assigned are mathematical capacity allocations scaled proportionally to fit the 160 hours of available developer time, rather than empirical work estimates.
4. **Jira Provisioning**: TeamPulse creates the entire hierarchy in Jira with one click, linking Subtasks to Tasks and Tasks to Epics.

---

## 4c. Walkthrough: PR Alert & One-Click Auto-Merge (Illustrative Example)

*(Note: The following pull request and ticket are an **illustrative example**).*

1. **Developer Opens PR**: A developer opens pull request #42 titled `[KAN-10] Implement RAG pipeline`.
2. **Slack Alert**: TeamPulse reads the Jira key `KAN-10` and posts a review card to Slack with direct links and `[ Approve & Merge ]` and `[ Decline ]` buttons.
3. **Safety Verification**: When the lead clicks `[ Approve & Merge ]`, TeamPulse performs 5 strict safety checks:
   - Is the user in `SLACK_APPROVER_IDS`? (If the list is empty, merge is refused).
   - Is dry-run mode active? (If true, it simulates and logs `dry_run` without merging).
   - Is the PR open and not already merged?
   - Are there merge conflicts?
   - Did CI tests pass? (If no CI is configured, it permits the merge with a `(no CI found)` notice; if CI failed or is still running, it refuses).
4. **Result**: TeamPulse merges the PR to `main` via GitHub, moves `KAN-10` to "Done" in Jira, and records `jira_auto_done` in the database.

---

## 4d. Walkthrough: Jira Done Task Verification (Illustrative Example)

*(Note: The following task review is an **illustrative example**).*

1. **Developer Marks Task Done**: In Jira, a developer drags `KAN-275` to "Done".
2. **Auto-Poller Detects It**: Every 15 seconds, TeamPulse checks Jira for tasks moved to "Done".
3. **Deduplication Check**: TeamPulse checks the database: if TeamPulse moved the ticket itself (via auto-merge) or already alerted it, it skips it.
4. **Slack Review Card**: TeamPulse posts a review card to Slack with a GitHub search link (`.../pulls?q=is%3Apr+KAN-275`) showing matching PRs and code diffs.
5. **Lead Verification**:
   - If the lead clicks `[ Approve & Verify ]`, TeamPulse adds a verification comment to Jira, updates Slack, and records `jira_task_verified`.
   - If the lead clicks `[ Decline & Reopen ]`, TeamPulse transitions the ticket back to "In Progress" (using transition ID 21), posts a comment explaining rework is needed, and removes the alert record so it can alert again once rework is finished.

---

## 5. Why a Human Click Is Needed

TeamPulse never modifies external boards or merges code without explicit human oversight:
- Automated bots should not create or resolve tickets without permission.
- A human lead knows if an author is on leave or if an item was discussed in a private meeting.
- The human stays in control, and TeamPulse eliminates the repetitive typing.

## 6. Questions You Can Ask TeamPulse

You can ask operational questions in Slack using `/teampulse ask <question>` or via the API (`POST /query`):

| Question You Ask | Example Answer You Receive |
| :--- | :--- |
| Which pull requests have been waiting the longest? | Top 5 pull requests ranked by days since opened (total: 88): 1. repo#88: Title (3300 days) ... |
| How many pull requests by alice? | There are 15 open pull requests. |
| How many pull requests does bob have? | There are 15 open pull requests. |
| Is KAN-42 blocked? | KAN-42: Database connection drop (15 days, author: alice, status: Open). |
| What is the weather today? | :warning: I can only answer operational questions about open GitHub pull requests and Jira tickets. |

---

# Part B: For Beginner Developers

This part explains how the code works under the hood.

## 7. The Core Pipeline Flow (Agent 1 & 2)

TeamPulse uses LangGraph, a library for building stateful workflows with nodes and edges.
The briefing pipeline has three nodes, followed by an interactive Slack webhook handler:

```text
1. Trigger -> 2. Collect Node -> 3. Analyze Node -> 4. Act Node -> 5. Human Action
```

| Step | File | What Goes In | What Comes Out |
| :--- | :--- | :--- | :--- |
| 1. Trigger | [app/scheduler/jobs.py](app/scheduler/jobs.py) or [scripts/post_briefing.py](scripts/post_briefing.py) | Cron timer, manual CLI, or `POST /briefing/trigger` | Initial `TeamPulseState` with workspace ID and run type |
| 2. Collect | [app/graph/pipeline.py](app/graph/pipeline.py) | Monitored repository names and Jira project key | Full list of pull requests and Jira issues with computed ages |
| 3. Analyze | [app/graph/pipeline.py](app/graph/pipeline.py) and [app/llm/router.py](app/llm/router.py) | Collected items and recent flag history from database | Validated priority flags with suggested actions |
| 4. Act | [app/agents/briefing.py](app/agents/briefing.py) | Grounded priority flags and source health status | Slack Block Kit message payload posted to Slack channel |
| 5. Human | [app/api/routes/slack.py](app/api/routes/slack.py) and [app/agents/actions.py](app/agents/actions.py) | User button click payload signed with Slack HMAC key to `POST /slack/interactions` | Created Jira issue, updated Slack block, and audit record in `actions_taken` |

## 8. Mermaid Flowchart

```mermaid
flowchart TD
    A[Trigger: Scheduler, CLI, or API] --> B[Collect Node in app/graph/pipeline.py]
    B --> C[Fetch GitHub PRs via PyGithub]
    B --> D[Fetch Jira Issues via HTTPX]
    C --> E[Compute age_days and is_stale]
    D --> E
    E --> F[Analyze Node in app/graph/pipeline.py]
    F --> G[LLM Router in app/llm/router.py]
    G --> H[Pydantic Validation in app/llm/schemas.py]
    H --> I[Grounding Check in app/graph/pipeline.py]
    I --> J[Act Node in app/agents/briefing.py]
    J --> K[Post Block Kit to Slack]
    K --> L[Human clicks Approve in Slack]
    L --> M[POST /slack/interactions in app/api/routes/slack.py]
    M --> N[HMAC Signature Verification in app/core/security.py]
    N --> O[Duplicate Check in app/agents/actions.py]
    O --> P[Create Jira Issue via HTTPX]
    P --> Q[Save Audit Log in app/db/models.py]
    Q --> R[Update Slack Block to Resolved in-place]
```

## 9. Technical Walkthrough of the Worked Example (Illustrative Example)

*(Note: This walkthrough uses an illustrative example with `psf/requests#4372` and `KAN-15`).*

1. **Collection ([app/graph/pipeline.py](app/graph/pipeline.py))**:
   `collect_node()` uses `asyncio.gather` to fetch GitHub and Jira data concurrently.
   For `psf/requests#4372`, it calculates `age_days = 28` and `last_commit_days = 28`.
   Because 28 is greater than `STALE_PR_DAYS` (7), it marks `is_stale = True`.

2. **Analysis ([app/llm/router.py](app/llm/router.py))**:
   `analyze_node()` formats the items into text and calls `router.route()` with schema `Analysis`.
   The router picks an available model (Gemini or Ollama).
   The LLM returns a structured JSON object containing a priority flag with urgency `urgent`.

3. **Grounding Check ([app/graph/pipeline.py](app/graph/pipeline.py))**:
   Before the flag is accepted, `pipeline.py` checks `normalize_llm_id(flag.item_id, valid_ids)`.
   If the LLM invented an ID that does not exist in the collected data (for example `psf/requests#9999`), the flag is discarded immediately.
   If the LLM changed the age to 50 days, the grounding check overwrites it with the real number (28 days).

4. **Slack Formatting ([app/agents/briefing.py](app/agents/briefing.py))**:
   `build_slack_briefing()` creates Slack Block Kit blocks.
   It creates two buttons inside an actions block:
   - Approve button: `value = str(db_flag_id)`
   - Dismiss button: `value = str(db_flag_id)`

5. **Persistence & Slack Posting ([app/integrations/slack_client.py](app/integrations/slack_client.py))**:
   Flags are flushed to the database in `pending` status first so their database IDs can be bound to the button values. Then the message is sent to Slack using `chat.postMessage`, and the message timestamp (`slack_ts`) is committed.

6. **Button Click ([app/api/routes/slack.py](app/api/routes/slack.py))**:
   When the lead clicks Approve, Slack sends an HTTP POST request to `POST /slack/interactions`.
   `verify_slack_signature()` computes `HMAC_SHA256(secret, timestamp + body)` and verifies that the timestamp is less than 5 minutes old.

7. **Duplicate Check and Execution ([app/agents/actions.py](app/agents/actions.py))**:
   `execute_approved_action()` searches Jira using JQL for any open issue containing the text `Triage stale PR psf/requests#4372`.
   If a ticket already exists, it stops and marks the action `skipped_duplicate`.
   If none exists, it calls the Jira REST API v3 to create the ticket.

8. **Audit Log ([app/db/models.py](app/db/models.py))**:
   A new record is inserted into the `actions_taken` database table:
   `action_type = "create_jira_issue"`, `status = "executed"` (or `"dry_run"` if testing), `target_id = "psf/requests#4372"`.
   Exact status words permitted: `dry_run`, `executed`, `skipped_duplicate`, `dismissed`, `failed`, `refused`.

9. **Slack Block Replacement**:
   `slack_client.update_message()` updates the original message in Slack in-place.
   It removes the Approve and Dismiss buttons and inserts: `Approved by @lead - created KAN-15`.

---

## 10. The Natural Language Query Path (Agent 6)

Users can ask questions through `POST /query` or `/teampulse ask <question>`.

### Why Code Computes and the LLM Only Phrases
Language models make arithmetic and counting mistakes when inspecting long lists.
In TeamPulse, 100% of counting, filtering, and ranking is performed by deterministic Python code over the complete dataset.
The LLM is only used to format natural phrasing when enabled.

| Step | What Happens | Example: "Which PR is the oldest?" | Example: "How many PRs by alice?" |
| :--- | :--- | :--- | :--- |
| 1. Rule classification | Checks keyword rules in [app/agents/query.py](app/agents/query.py) | Matches word `oldest` -> `operation = "rank"`, `metric = "age_days"` | Matches `how many` and author pattern `by alice` -> `operation = "count"`, `author = "alice"` |
| 2. Skip LLM intent | If rules match, no LLM call is made | Classified in 0.1 ms with `model = "rule_based_classifier"` | Classified in 0.1 ms with `model = "rule_based_classifier"` |
| 3. Source fetch | Reads cache or calls GitHub/Jira APIs | Loads all 88 pull requests into memory | Loads all 88 pull requests into memory |
| 4. Python calculation | Python code sorts or counts the full list | Sorts all 88 PRs by `age_days` descending. Top item is PR #88 (3300 days). | Filters list where `pr.author.lower() == "alice"`. Total is exactly 15. |
| 5. Template formatting | Code formats the final text string | `Top 5 pull requests ranked by days since opened (total: 88): 1. repo#88: Title (3300 days) ...` | `There are 15 open pull requests.` |
| 6. Direct return (Default) | Governed by `QUERY_LLM_REPHRASE` setting | Returns code answer directly in ~35 ms with `model_used = "code"` | Returns code answer directly in ~35 ms with `model_used = "code"` |

> 📌 **The `QUERY_LLM_REPHRASE` Setting**:
> - **When `QUERY_LLM_REPHRASE=false` (Default)**: The code-built template answer is returned directly to the user (`model_used="code"`). Zero LLM tokens are spent, latency is instantaneous, and hallucination risk is completely eliminated.
> - **When `QUERY_LLM_REPHRASE=true`**: The code passes the pre-computed template to the LLM to rephrase conversationally. The output is validated by `verify_rephrased_answer()` to ensure all IDs and numbers match the real data. If validation fails or times out, TeamPulse safely falls back to the code template.

---

## 11. What Happens When Something Fails

TeamPulse is designed so that a partial briefing is always delivered instead of crashing:

| Situation | What TeamPulse Does |
| :--- | :--- |
| GitHub API is down or rate limited | Marks GitHub as unavailable. Briefing header shows `WARNING: GitHub data unavailable (...)`. Open PR count shows `unknown`. Jira tickets are still displayed normally. Never shows All clear. |
| Jira API is down | Marks Jira as unavailable. Briefing header shows `WARNING: Jira data unavailable (...)`. Ticket count shows `unknown`. Pull requests are still displayed normally. |
| Gemini returns HTTP 429 (quota exhausted) | The router parses the retry delay, marks the model blocked in memory, and immediately skips to the next candidate model. It does not retry the blocked model. |
| Gemini returns HTTP 503 (service unavailable) | The router retries up to 2 times with exponential backoff and jitter. If still unavailable, cascades to fallback models. |
| LLM returns invalid JSON | TeamPulse re-prompts the model once with the Pydantic schema. If it fails a second time, it switches to deterministic rule-based analysis (`analyze_fallback()`). |
| Slack posting fails | The error is logged. Flags are not committed to the database. The next scheduled run will pick them up again. |
| Button clicked twice | The handler checks the database. If the flag is already approved, it sends an ephemeral message `Already approved by <@user>` and skips creating another ticket. |
| Forged Slack request | The HMAC signature check fails. TeamPulse rejects the request immediately with HTTP 401 Unauthorized. |

---

## 12. Safety Features Explained Simply

- **Dry-Run Mode (`DRY_RUN_ACTIONS=true`)**: A test mode where clicking Approve simulates the action, writes an audit record with `status="dry_run"`, and updates Slack, but never modifies Jira tickets or merges code on GitHub.
- **HMAC Signature Verification**: Every webhook request from Slack and GitHub comes with a cryptographic signature. TeamPulse verifies this signature with a secret key so no outside attacker can fake an action.
- **Merge Safety Guardrails**: 5 strict checks before any PR is merged to `main`:
  1. User must be in `SLACK_APPROVER_IDS` (empty list strictly refuses merge actions).
  2. Dry run switch (`DRY_RUN_ACTIONS=true` simulates and logs `dry_run`).
  3. PR must be open and not already merged.
  4. PR must be mergeable without conflicts.
  5. CI checks must be passed (0 CI checks allows merge with `(no CI found)` notice; failing or in-progress CI blocks merge).
- **Poller Deduplication & Restart Persistence**: Tickets moved to Done by TeamPulse auto-merge or marked verified are recorded in `actions_taken` (`jira_auto_done`, `jira_task_verified`) so the 15-second background poller never re-alerts on actions TeamPulse performed itself. Alerted tickets are permanently stored as `jira_task_alerted` in the DB so server restarts never re-alert.
- **Exact Audit Status Words**: Every action recorded in `actions_taken` uses strictly defined words matching the code:
  `dry_run`, `executed`, `skipped_duplicate`, `dismissed`, `failed`, `refused`. (`simulated` and `success` are never used).
- **Rate Limiting**: Limits queries per user in Slack and per API key on HTTP endpoints (default 5 queries per minute) so users cannot spam the system.

---

# Part C: How to Run It

This part provides exact commands to set up, run, and test TeamPulse.

## 13. Prerequisites

| Tool | Why It Is Needed | How to Check It Is Installed |
| :--- | :--- | :--- |
| Python 3.11+ | Runs the TeamPulse application (Python 3.14 used in development) | Run `python --version` in terminal |
| MySQL Server | Stores workspaces, flags, runs, actions, and project plans | Run `mysql --version` in terminal |
| Git | Clones the project repository | Run `git --version` in terminal |
| ngrok | Exposes local port 8000 to the public internet for Slack and GitHub webhooks | Run `ngrok --version` in terminal |

---

## 14. Step-by-Step Setup

### Step 1: Clone the Repository
```bash
git clone https://github.com/mnithinn24/teampulse-ai-ops-agent.git
cd teampulse-ai-ops-agent
```

### Step 2: Create and Activate Virtual Environment
On Windows (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

On Linux or macOS:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Step 3: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 4: Create `.env` from Example
```bash
cp .env.example .env
```

### Step 5: Configure Settings in `.env`
Open `.env` in an editor. Fill in your settings using the names below.

> 💡 **Important Safety Tip for First Run**: Always set `DRY_RUN_ACTIONS=true` for your first run! When true, TeamPulse simulates all write operations across GitHub and Jira and records them with status `dry_run` without altering your real boards or repositories.

| Setting Name | What It Controls |
| :--- | :--- |
| `GITHUB_TOKEN` | Token for reading GitHub pull requests and commits |
| `GITHUB_REPOS` | Comma-separated repositories to monitor (`owner/repo`) |
| `GITHUB_WEBHOOK_SECRET` | Secret key for verifying HMAC-SHA256 signatures on incoming GitHub webhooks |
| `JIRA_BASE_URL` | Web address of your Jira instance (e.g. `https://your-domain.atlassian.net`) |
| `JIRA_EMAIL` | Email address for Jira API login |
| `JIRA_API_TOKEN` | Secret API token for Jira |
| `JIRA_PROJECT_KEY` | Project key in Jira where issues belong (e.g. `KAN`) |
| `JIRA_WEBHOOK_SECRET` | Secret token for verifying incoming Atlassian Jira Cloud webhooks |
| `JIRA_REOPEN_TRANSITION_ID` | Jira workflow transition ID to reopen declined tasks back to In Progress (default 21) |
| `SLACK_BOT_TOKEN` | Token starting with `xoxb-` for Slack bot actions |
| `SLACK_SIGNING_SECRET` | Secret key from Slack to verify webhook requests |
| `SLACK_CHANNEL_ID` | Channel ID where briefings and alerts are posted (e.g. `C0123456789`) |
| `SLACK_APPROVER_IDS` | Comma-separated list of Slack user IDs allowed to approve actions and merge PRs (empty list strictly refuses merge actions for safety) |
| `MAX_FLAGS_IN_BRIEFING` | Maximum priority items shown in one briefing (default 10) |
| `DRY_RUN_ACTIONS` | Safety switch: set to true to test without modifying Jira tickets or merging PRs |
| `GEMINI_API_KEY` | Secret key for Google Gemini model |
| `GEMINI_MODEL` | Name of primary Gemini model (`gemini-3.6-flash`) |
| `GEMINI_FALLBACK_MODEL` | Name of backup Gemini model (`gemini-3.5-flash-lite`) |
| `GEMINI_THINKING_LEVEL` | Thinking budget for Gemini (`low` or `normal`) |
| `OLLAMA_BASE_URL` | Web address for local Ollama server (`http://localhost:11434`) |
| `OLLAMA_MODEL` | Name of local Ollama model (`llama3.1:latest`) |
| `OLLAMA_ENABLED` | Set to true to allow local Ollama use (default false) |
| `LLM_TIMEOUT_SECONDS` | Maximum seconds to wait for an LLM answer (default 60.0) |
| `QUERY_LLM_REPHRASE` | Set to true to let LLM rephrase query answers; false returns code-built answers |
| `STALE_PR_DAYS` | Days without activity before a PR is marked stale (default 7) |
| `STALE_TICKET_DAYS` | Days without update before a ticket is marked stale (default 14) |
| `ABANDONED_PR_DAYS` | Days before an old PR is treated as abandoned (default 90) |
| `MAX_ITEMS_IN_PROMPT` | Maximum items sent in one prompt to an LLM (default 30) |
| `MAX_COMMIT_LOOKUPS` | Limit on individual commit checks to save GitHub quota (default 20) |
| `DATABASE_URL` | MySQL connection string (`mysql+pymysql://root:password@localhost:3306/teampulse`) |
| `TIMEZONE` | Timezone name for briefing timestamps (`Asia/Kolkata`) |
| `ENABLE_SCHEDULER` | Set to true to run scheduled briefings automatically (default false) |
| `MORNING_BRIEFING_TIME` | Time in `HH:MM` format for morning run (`09:30`) |
| `EVENING_BRIEFING_TIME` | Time in `HH:MM` format for evening run (`17:30`) |
| `ADMIN_API_KEY` | Secret token for protected HTTP endpoints |
| `QUERY_RATE_LIMIT_PER_MINUTE` | Maximum queries allowed per minute per user (default 5) |
| `QUERY_CACHE_TTL_SECONDS` | Seconds to keep collected work items in cache (default 300) |

### Step 6: Create MySQL Database
Log into MySQL:
```bash
mysql -u root -p
```

Run this command inside MySQL:
```sql
CREATE DATABASE teampulse CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
EXIT;
```

Update your `DATABASE_URL` in `.env`:
```text
DATABASE_URL=mysql+pymysql://root:password@localhost:3306/teampulse
```

### Step 7: Create Tables with `reset_db.py`
```bash
python scripts/reset_db.py --yes
```

Expected result:
```text
Resetting database at 'mysql+pymysql://root:***@localhost:3306/teampulse'...
Database tables successfully dropped and recreated.
```

---

## 15. Run Order (3 Terminals)

### Terminal 1: Start the API Server
```bash
uvicorn app.api.main:app --host 0.0.0.0 --port 8000
```
Expected result:
```text
INFO:     Started server process
INFO:     Initializing TeamPulse database schema on startup...
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

### Terminal 2: Expose with ngrok
```bash
ngrok http 8000
```
Expected result:
```text
Forwarding   https://abcdef123456.ngrok-free.app -> http://localhost:8000
```

Configure Webhook Settings:
- In Slack App Management $\to$ **Interactivity & Shortcuts**, set **Request URL** to:
  `https://abcdef123456.ngrok-free.app/slack/interactions`
- In Slack App Management $\to$ **Slash Commands**, set Request URL for `/teampulse` to:
  `https://abcdef123456.ngrok-free.app/slack/commands`
- In GitHub Repository Settings $\to$ **Webhooks**, set Payload URL to:
  `https://abcdef123456.ngrok-free.app/github/webhook`
- In Jira Cloud $\to$ **System** $\to$ **WebHooks**, set URL to:
  `https://abcdef123456.ngrok-free.app/jira/webhook?secret=your-jira-webhook-secret`

### Terminal 3: Post a Briefing
```bash
python scripts/post_briefing.py
```
Expected result:
```text
============================================================
 TeamPulse Ops Briefing Runner (Phase 4)
============================================================
[1/3] Initializing database...
[2/3] Executing LangGraph pipeline (Collect -> Analyze -> Act)...
============================================================
 Pipeline Execution Complete
============================================================
Flags Saved to DB: 2
Slack Status     : Posted to channel C0123456789
```

---

## 16. How to Test

### Automated Unit Tests (Phase 11)
Run pytest across the entire test suite locally. All 222 unit tests execute safely in-memory against mock fixtures without external network dependencies:
```bash
pytest -q
```
Expected result:
```text
222 passed, 7 warnings in 13.33s
```

### End-to-End Local Feature Simulation
Run the full 10-feature local simulator without real Slack, Jira, or GitHub credentials:
```bash
python scripts/simulate_all_features.py
```
Output:
```text
===========================================================================
         TeamPulse Phase 11: End-to-End Local Simulation Suite
===========================================================================
Safety Mode: Isolated in-memory SQLite | Dry-Run Active | 0 Network Calls

[01/10] 1 & 2: Ingestion & Stale Detection .................. [PASS] (0.06ms)
[02/10] 3: Grounded Analysis & Hallucination Guard .......... [PASS] (0.12ms)
[03/10] 4: Slack Morning Briefing Block Kit ................. [PASS] (0.4ms)
[04/10] 5: Interactive Actions & Idempotency ................ [PASS] (17.91ms)
[05/10] 6: Plain-Language Query Engine & Shield ............. [PASS] (195.33ms)
[06/10] 7: AI Project Planner (Jira Hierarchy) .............. [PASS] (2380.9ms)
[07/10] 8: PR Completion Slack Alert & Jira Link ............ [PASS] (1.62ms)
[08/10] 9: One-Click Slack Auto-Merge & Done ................ [PASS] (7.73ms)
[09/10] 10: Decline PR Action & State Sync .................. [PASS] (4.69ms)
[10/10] 11: Security & Network Isolation .................... [PASS] (0.03ms)

===========================================================================
Summary: 10/10 Features Verified (100.0%) | Time: 2.61s
Report saved to: simulations/simulation_report.json
===========================================================================
```

### Offline Deterministic Evaluation Suite
Run the 48-case offline evaluation suite:
```bash
python evals/run_eval.py
```
Expected result (from [evals/REPORT.md](evals/REPORT.md)):
```text
==========================================================================================
                        TeamPulse Phase 6 Evaluation Suite
==========================================================================================
Category                 Cases    Passed   Accuracy   Mean (ms)    p95 (ms)  
------------------------------------------------------------------------------------------
action_safety            5        5        100.0%     7.39         28.32     
briefing_wording         5        5        100.0%     0.11         0.22      
failure_handling         5        5        100.0%     0.58         1.34      
grounding                6        6        100.0%     0.08         0.12      
query_count              6        6        100.0%     36.63        39.77     
query_lookup             5        5        100.0%     36.62        38.64     
query_out_of_scope       5        5        100.0%     35.71        38.04     
query_rank               5        5        100.0%     45.42        78.42     
stale_detection          6        6        100.0%     0.0          0.0       
==========================================================================================
Overall: 48/48 passed (100.0%) | p50: 0.81 ms | p95: 38.67 ms
Grounding Protection: 2/2 invented IDs blocked (simulated LLM output)
==========================================================================================
SUCCESS: All deterministic evaluation cases passed!
```

---

## 17. First 10 Minutes Checklist for a New Developer

- [ ] Cloned repo and created a virtual environment with Python 3.11+.
- [ ] Installed packages using `pip install -r requirements.txt`.
- [ ] Created `.env` from `.env.example` and set `DRY_RUN_ACTIONS=true` for first run.
- [ ] Created MySQL `teampulse` database.
- [ ] Ran `python scripts/reset_db.py --yes` and verified tables created.
- [ ] Ran `pytest -q` and confirmed all 222 tests passed.
- [ ] Ran `python scripts/simulate_all_features.py` and confirmed 10/10 features passed.
- [ ] Ran `python evals/run_eval.py` and confirmed all 48 cases passed.
- [ ] Started server with `uvicorn app.api.main:app --port 8000` and checked `curl http://localhost:8000/health`.

---

## 18. Phase 8: AI Project Planner (Agent 3)

### What It Does
Instead of manually typing dozens of Jira tickets for a new project, you provide a project description, headcount, and duration:
`Medical RAG, 2 developers, 2 weeks`

TeamPulse executes:
1. **Capacity Calculation**: $\text{Capacity} = \text{Developers} \times \text{Weeks} \times 40\text{ hours/week} = 2 \times 2 \times 40 = 160\text{ hours}$.
2. **Epics & Tasks Decomposition**: Generates 3-5 Epics with child Tasks and Subtasks.
3. **Capacity Hour Normalization**:
   > ⚠️ **Capacity Allocations vs Empirical Estimates**: Hours assigned to Epics, Tasks, and Subtasks are mathematical capacity allocations scaled proportionally by `normalize_and_balance_hours()` to match total developer capacity ($Devs \times Weeks \times 40h$), rather than empirical work estimates or historical velocity.
4. **Hierarchical Provisioning**:
   - Creates Epics (`issuetype: "Epic"`).
   - Creates child Tasks linked to parent Epics (`parent: {"key": epic_key}`).
   - Creates child Subtasks linked to parent Tasks (`parent: {"key": task_key}`, `issuetype: "Subtask"`).
5. **Interactive Web UI**: TeamPulse serves an interactive dashboard at `http://localhost:8000/` to preview, edit hours, and provision plans.

### Planner API Routes (`/project`)
- `GET /`: Serves the interactive planner web dashboard.
- `POST /project/plan`: Generates a structured project plan and provisions Epics/Tasks/Subtasks to Jira.
- `POST /project/plan/preview`: Generates plan preview and hour allocation without modifying Jira.
- `POST /project/provision`: Provisions a previously previewed plan hierarchy into Jira Cloud.
- `GET /project/config`: Retrieves connected Jira project key, base URL, and UI defaults.
- `POST /project/ticket/update`: Updates summary, description, and hours of a Jira ticket or preview card.

---

## 19. Phase 9 & 10: PR Completion Alert & One-Click Auto-Merge (Agent 4)

### What It Does
When a developer finishes their assigned task and opens a pull request:
1. GitHub notifies TeamPulse via webhook (`POST /github/webhook`).
2. TeamPulse reads the PR title, branch, and description to extract the linked Jira ticket (e.g. `KAN-42`).
3. TeamPulse fetches the Jira issue status and summary, then posts an alert card to Slack with:
   - PR title and branch link
   - Jira ticket link and current status
   - Interactive `[ Approve & Merge ]` and `[ Decline ]` buttons

### The 5 Merge Safety Guardrails
Clicking `[ Approve & Merge ]` invokes `handle_pr_approval_action()`, which executes 5 strict safety checks before calling the GitHub merge API. If any check fails, the merge is refused and recorded in `actions_taken` with `status="refused"`:
1. **Approver Authorization**: Clicking user must be in `SLACK_APPROVER_IDS`. If `SLACK_APPROVER_IDS` is empty or unconfigured, TeamPulse strictly **refuses** all merge actions with a clear warning explaining how to configure `.env`.
2. **Dry Run Safety Switch**: When `DRY_RUN_ACTIONS=true` (or `dry_run=True`), the merge and Jira transitions are simulated and recorded with `status="dry_run"` without altering GitHub or Jira.
3. **PR State & Merged Check**: PR must be currently `open` and not already `merged`.
4. **Mergeability (Conflict) Check**: Inspects `mergeable` and `mergeable_state`. If conflicts exist, merge is refused.
5. **CI Status & Check Runs**: Queries commit status and check runs:
   - If 0 CI checks are configured on the repo, merge is allowed with ` (no CI found)` noted in Slack.
   - If commit status is not `success` or any check run failed/in-progress, merge is refused.
6. **Post-Merge Transition & Deduplication**: Merges PR to `main` via GitHub REST API, marks Jira ticket as Done, and records `ActionTaken(action_type="jira_auto_done", status="executed")` to prevent the poller from re-alerting.

### GitHub Routes (`/github`)
- `POST /github/webhook`: Receives incoming GitHub pull request webhooks (`opened`, `ready_for_review`).
- `POST /github/pr/alert`: Manually dispatches a PR review alert to Slack (alias `/github/alert`).
- `POST /github/pr/merge`: Executes one-click PR auto-merge on GitHub and marks Jira ticket Done.

---

## 20. Jira Task Done & Verification Poller (Agent 5)

### What It Does
A 15-second background poller (`poll_jira_completed_tasks_loop`) and Jira webhook (`POST /jira/webhook`) detect when tasks transition to "Done" in Jira:
1. **GitHub PR Search Link**: Resolves a GitHub search URL (`https://github.com/{primary_repo}/pulls?q=is%3Apr+{issue_key}`), not an exact direct PR link, to inspect matching PRs and diffs.
2. **Task Review Card in Slack**: Posts review card with `[ 🔍 Open Pull Request ↗ ]`, `[ Approve & Verify ]`, and `[ Decline & Reopen ]`.
3. **Deduplication Check**: Skips tickets TeamPulse moved itself (`jira_auto_done`, `jira_task_verified`) or already alerted (`jira_task_alerted`) by checking `actions_taken` in the database.
4. **Restart Persistence**: Alerted tickets are permanently stored as `jira_task_alerted` in the database so server restarts never re-alert already alerted tickets.
5. **Reopen Lifecycle**: If a lead clicks `[ Decline & Reopen ]`:
   - Transitions ticket back to "In Progress" using transition ID 21 (`JIRA_REOPEN_TRANSITION_ID`).
   - Comments on Jira that the task was declined for rework.
   - Removes the `jira_task_alerted` record from the database so rework can alert again when marked Done.

### Jira Routes (`/jira`)
- `POST /jira/webhook`: Receives live Jira issue status webhooks (`jira:issue_updated`).
- `POST /jira/alert`: Dispatches Jira task review alert to Slack.

---

## 21. Complete REST API & Webhook Endpoints Reference

| Method | Endpoint | Description | Auth / Security |
| :--- | :--- | :--- | :--- |
| `GET` | `/` | Serves AI Project Planner Web UI dashboard | Public |
| `GET` | `/health` | Application health check, scheduler status & last run | Public |
| `GET` | `/static/{file}` | Serves static assets (`styles.css`, `app.js`) | Public |
| `POST` | `/briefing/trigger` | Triggers immediate briefing execution pipeline | `X-API-Key: ADMIN_API_KEY` |
| `GET` | `/runs/{run_id}` | Fetches run execution status, model used, and latencies | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/query` | Executes natural language query over collected items | `X-API-Key: ADMIN_API_KEY` |
| `GET` | `/llm/status` | Reports blocked status and cooldown timer of LLM models | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/project/plan` | Generates project plan and provisions hierarchy to Jira | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/project/plan/preview` | Generates plan and hour allocation preview without modifying Jira | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/project/provision` | Provisions a previously previewed plan hierarchy into Jira Cloud | `X-API-Key: ADMIN_API_KEY` |
| `GET` | `/project/config` | Retrieves connected Jira project key, base URL, and UI defaults | Public |
| `POST` | `/project/ticket/update` | Updates summary, description, and hours of a Jira ticket or preview card | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/github/webhook` | Receives GitHub pull request webhooks (`opened`, `ready_for_review`) | `X-Hub-Signature-256` |
| `POST` | `/github/pr/alert` | Manually dispatches PR review alert to Slack | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/github/pr/merge` | Executes one-click PR auto-merge on GitHub and marks Jira ticket Done | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/jira/webhook` | Receives live Jira issue status webhooks (`jira:issue_updated`) | Jira Webhook Secret (`X-Atlassian-Webhook-Token`, `X-Jira-Webhook-Secret`, or `?secret=`) |
| `POST` | `/jira/alert` | Dispatches Jira task review alert to Slack | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/slack/interactions` | Receives button clicks from Slack Block Kit (Approve/Dismiss, Merge, Verify) | HMAC-SHA256 Signature |
| `POST` | `/slack/commands` | Handles `/teampulse` slash command interactions (`brief`, `ask`, `plan`, `status`) | HMAC-SHA256 Signature |

---

## 22. Common Problems and Real Fixes

| Problem | Cause | How to Fix |
| :--- | :--- | :--- |
| Jira returns HTTP 410 Gone | Outdated Jira REST endpoint or cookie login was used | TeamPulse uses modern REST API v3 endpoints. Check that `JIRA_BASE_URL` is an HTTPS Cloud address and your token has Jira Cloud permissions. |
| Briefing shows placeholder repos like `owner/repo1` | Default values remain in Windows environment variables or `.env` | Edit `.env` and set `GITHUB_REPOS=your-org/your-repo`. If set in Windows system environment variables, update them and restart terminal. |
| Slack returns `not_in_channel` error | The Slack bot user is not a member of the target channel | Open the channel in Slack and type `/invite @TeamPulse` to add the bot. |
| Server fails with `[Errno 10048]` (port in use) | A previous Uvicorn process is still running on port 8000 | In PowerShell, run `Get-Process python \| Stop-Process -Force` or use `taskkill /PID <pid> /F`. |
| Database error about missing column or table | Database was created with an older version of the models | Run `python scripts/reset_db.py --yes` to recreate the schema cleanly. |
| Gemini calls fail with HTTP 429 Resource Exhausted | Daily free tier quota (20 requests per day) reached | TeamPulse router tracks quota resets and skips blocked models. You can turn on Ollama (`OLLAMA_ENABLED=true`) or wait for quota reset. |
| Merge refused: `SLACK_APPROVER_IDS is empty` | No approver IDs are configured in `.env` | Add your Slack user ID (e.g. `U0123456789`) to `SLACK_APPROVER_IDS` in `.env` and restart Uvicorn. |

---

## 23. Glossary

| Term | One-Line Explanation |
| :--- | :--- |
| Pull Request (PR) | A proposed code change submitted by a developer for team review before merging. |
| Jira Ticket | A work item tracking a bug, task, or feature on a Jira board. |
| Large Language Model (LLM) | An AI model (such as Gemini or Llama 3) that understands and generates natural language text. |
| Router | Code that inspects a task and chooses the best available LLM provider or model. |
| Fallback | A safe backup plan (such as switching models or using code rules) when an external system fails. |
| Grounding | Verifying that every fact and ID returned by an AI matches real, confirmed data. |
| HMAC | A secure method of verifying that a network message came from a trusted sender and was not altered. |
| Webhook | An automated HTTP request sent from one application to another when an event happens. |
| ngrok | A tool that creates a secure public web address that forwards traffic to your local computer. |
| Dry Run | Running a program to test logic and logs without writing changes to external systems. |
| Idempotent | An operation that produces the exact same result even if you run it multiple times. |
| LangGraph | A library for building stateful multi-step applications with graph nodes. |
| Pydantic | A Python library for checking that data structures match strict type definitions. |
| Audit Log | A permanent database record of every action, run, and human decision for security and tracking. |
| Stale | An item (pull request or ticket) that has had no activity for longer than a configured number of days. |

---

## 24. Things I Could Not Verify in the Code

1. **Cloud Deployment (Not Deployed Yet)**: `PROJECT_SPEC.md` lists Google Cloud Run as a deployment target, but no Dockerfile or Cloud Run configuration files exist in the repository yet. The application runs locally or on developer workstations.
2. **LangChain Package Usage**: `PROJECT_SPEC.md` mentions LangChain in the stack list, but the implementation uses LangGraph, FastAPI, and Pydantic directly without importing the `langchain` package.
3. **Multi-Tenant Automated Isolation**: Database tables include a `workspace_id` column, but dynamic multi-tenant authentication and per-workspace credential routing are not yet implemented (a single default workspace is used).
