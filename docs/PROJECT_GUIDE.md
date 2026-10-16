# TeamPulse Project Guide

A complete guide to understanding, using, and developing TeamPulse.

---

# Part A: For Non-Technical People

This part explains what TeamPulse does in simple words, without code.

## 1. What TeamPulse Is

TeamPulse is an automated assistant for software engineering teams.
It reads open work from GitHub and Jira every day to find tasks that are stuck.
It sends a clean summary to Slack with buttons so team leads can approve actions with one click.

## 2. The Problem

A team lead has 88 open pull requests on GitHub and dozens of tickets on Jira.
Some pull requests have waited weeks for a code review, and some tickets are blocked.
Nobody has time to open every webpage every morning to find which items are stuck.
Work sits forgotten until a customer complains or a deadline is missed.

## 3. What TeamPulse Does in 5 Steps

1. Look: TeamPulse connects to GitHub and Jira and collects all open work.
2. Find stuck work: It calculates how many days each item has waited without progress.
3. Tell the team: It posts a morning briefing directly into a Slack channel.
4. Human decides: A team lead reads the briefing and clicks Approve or Dismiss on each item.
5. Ticket is created: When approved, TeamPulse creates a tracking ticket in Jira automatically.

## 4. Full Example

Here is what happens during a real morning run:

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
The team lead reads the card and clicks the Approve button.

### The Result in Slack
The buttons disappear immediately and are replaced by this confirmation:

```text
Approved by @lead - created KAN-15
```

### The Result in Jira
A new Jira ticket appears on the team board:
- Key: `KAN-15`
- Summary: `Triage stale PR psf/requests#4372`
- Description: Details about the pull request, author, and age.

## 5. Why a Human Click Is Needed

TeamPulse never writes to Jira on its own.
Software projects change quickly, and automated bots should not create tickets without permission.
A human lead knows if a task was discussed in a meeting or if an author is on vacation.
The human stays in control, and the bot only does the repetitive typing.

## 6. Questions You Can Ask TeamPulse

You can type questions to TeamPulse in Slack using the `/teampulse ask` command.

| Question You Ask | Example Answer You Receive |
| :--- | :--- |
| Which pull requests have been waiting the longest? | Top 5 pull requests ranked by days since opened (total: 88): 1. repo#88: Title (3300 days) ... |
| How many pull requests by alice? | There are 15 open pull requests. |
| How many pull requests does bob have? | There are 15 open pull requests. |
| Is KAN-42 blocked? | KAN-42: Database connection drop (15 days, author: alice, status: Open). |
| What is the weather today? | I can only answer questions about your GitHub pull requests and Jira tickets. |

---

# Part B: For Beginner Developers

This part explains how the code works under the hood.

## 7. The Core Pipeline Flow

TeamPulse uses LangGraph, a library for building state machines (step-by-step workflows).
The pipeline has three nodes, followed by a human webhook interaction.

```text
1. Trigger -> 2. Collect Node -> 3. Analyze Node -> 4. Act Node -> 5. Human Action
```

| Step | File | What Goes In | What Comes Out |
| :--- | :--- | :--- | :--- |
| 1. Trigger | `app/scheduler/jobs.py` or `scripts/post_briefing.py` | Schedule timer or manual CLI command | Initial state with workspace ID and run type |
| 2. Collect | `app/graph/pipeline.py` | Target repository names and Jira project key | Full list of pull requests and Jira issues with computed ages |
| 3. Analyze | `app/graph/pipeline.py` and `app/llm/router.py` | Collected items and recent flag history from database | Validated priority flags with suggested actions |
| 4. Act | `app/agents/briefing.py` | Grounded priority flags and source health status | Slack Block Kit message payload posted to Slack channel |
| 5. Human | `app/api/routes/slack.py` and `app/agents/actions.py` | User button click payload signed with Slack HMAC key | Created Jira issue, updated Slack block, and audit record |

## 8. Mermaid Flowchart

```mermaid
flowchart TD
    A[Trigger: Scheduler or CLI] --> B[Collect Node in pipeline.py]
    B --> C[Fetch GitHub PRs via PyGithub]
    B --> D[Fetch Jira Issues via HTTPX]
    C --> E[Compute age_days and is_stale]
    D --> E
    E --> F[Analyze Node in pipeline.py]
    F --> G[LLM Router in router.py]
    G --> H[Pydantic Validation in schemas.py]
    H --> I[Grounding Check in pipeline.py]
    I --> J[Act Node in briefing.py]
    J --> K[Post Block Kit to Slack]
    K --> L[Human clicks Approve in Slack]
    L --> M[POST /slack/actions in routes/slack.py]
    M --> N[HMAC Signature Verification in security.py]
    N --> O[Duplicate Check in actions.py]
    O --> P[Create Jira Issue via HTTPX]
    P --> Q[Save Audit Log in models.py]
    Q --> R[Update Slack Block to Resolved]
```

## 9. Technical Walkthrough of the Worked Example

Here is the exact technical execution of the worked example from Part A:

1. **Collection (`app/graph/pipeline.py`)**:
   `collect_node()` uses `asyncio.gather` to fetch GitHub and Jira data at the same time.
   For `psf/requests#4372`, it calculates `age_days = 28` and `last_commit_days = 28`.
   Because 28 is greater than `STALE_PR_DAYS` (7), it marks `is_stale = True`.

2. **Analysis (`app/llm/router.py`)**:
   `analyze_node()` formats the items into text and calls `router.route()` with schema `Analysis`.
   The router picks an available model (Gemini or Ollama).
   The LLM returns a structured JSON object containing a priority flag with urgency `urgent`.

3. **Grounding Check (`app/graph/pipeline.py`)**:
   Before the flag is accepted, `pipeline.py` checks `normalize_llm_id(flag.item_id, valid_ids)`.
   If the LLM invented an ID that does not exist in the collected data (for example `psf/requests#9999`), the flag is discarded immediately.
   If the LLM changed the age to 50 days, the grounding check overwrites it with the real number (28 days).

4. **Slack Formatting (`app/agents/briefing.py`)**:
   `build_slack_briefing()` creates Slack Block Kit blocks.
   It creates two buttons inside an actions block:
   - Approve button: `value = str(db_flag_id)`
   - Dismiss button: `value = str(db_flag_id)`

5. **Posting to Slack (`app/integrations/slack_client.py`)**:
   The message is sent to Slack using `chat.postMessage`.
   Only after Slack returns success, the flags are committed to the `flags` database table.

6. **Button Click (`app/api/routes/slack.py`)**:
   When the lead clicks Approve, Slack sends an HTTP POST request to `/slack/actions`.
   `verify_slack_signature()` computes `HMAC_SHA256(secret, timestamp + body)` and verifies that the timestamp is less than 5 minutes old.

7. **Duplicate Check and Execution (`app/agents/actions.py`)**:
   `execute_approved_action()` searches Jira using JQL for any open issue containing the text `Triage stale PR psf/requests#4372`.
   If a ticket already exists, it stops and marks the action `skipped_duplicate`.
   If none exists, it calls the Jira REST API v3 to create the ticket.

8. **Audit Log (`app/db/models.py`)**:
   A new record is inserted into the `actions_taken` table:
   `action_type = "create_jira_issue"`, `status = "success"`, `target_id = "psf/requests#4372"`.

9. **Slack Block Replacement**:
   `slack_client.update_message()` updates the original message in Slack.
   It removes the Approve and Dismiss buttons and inserts a note: `Approved by @lead - created KAN-15`.

## 10. The Natural Language Query Path

Users can ask questions through `POST /query` or `/teampulse ask <question>`.

### Why Code Computes and the LLM Only Phrases
Language models often make mistakes when counting or sorting long lists.
If a prompt includes 88 pull requests, an LLM might count 15 or miss older items.
In TeamPulse, 100 percent of counting, filtering, and ranking is done by Python code over the complete dataset.
The LLM is only used to turn the code output into a sentence.

| Step | What Happens | Example: "Which PR is the oldest?" | Example: "How many PRs by alice?" |
| :--- | :--- | :--- | :--- |
| 1. Rule classification | Checks keyword rules in `app/agents/query.py` | Matches word `oldest` -> `operation = "rank"`, `metric = "age_days"` | Matches `how many` and author pattern `by alice` -> `operation = "count"`, `author = "alice"` |
| 2. Skip LLM intent | If rules match, no LLM call is made | Classified in 0.1 ms with `model = "rule_based_classifier"` | Classified in 0.1 ms with `model = "rule_based_classifier"` |
| 3. Source fetch | Reads cache or calls GitHub/Jira APIs | Loads all 88 pull requests into memory | Loads all 88 pull requests into memory |
| 4. Python calculation | Python code sorts or counts the full list | Sorts all 88 PRs by `age_days` descending. Top item is PR #88 (3300 days). | Filters list where `pr.author.lower() == "alice"`. Total is exactly 15. |
| 5. Template formatting | Code formats the final text string | `Top 5 pull requests ranked by days since opened (total: 88): 1. repo#88: Title (3300 days) ...` | `There are 15 open pull requests.` |
| 6. Direct return | Fast return without rephrasing (default) | Returns code answer in ~35 ms with `model_used = "code"` | Returns code answer in ~35 ms with `model_used = "code"` |

## 11. What Happens When Something Fails

TeamPulse is designed so that a partial briefing is always delivered instead of crashing.

| Situation | What TeamPulse Does |
| :--- | :--- |
| GitHub API is down or rate limited | Marks GitHub as unavailable. Briefing header shows `WARNING: GitHub data unavailable (...)`. Open PR count shows `unknown`. Jira tickets are still displayed normally. Never shows All clear. |
| Jira API is down | Marks Jira as unavailable. Briefing header shows `WARNING: Jira data unavailable (...)`. Ticket count shows `unknown`. Pull requests are still displayed normally. |
| Gemini returns HTTP 429 (quota exhausted) | The router parses the retry delay, marks the model blocked in memory, and immediately skips to the next candidate model. It does not retry the blocked model. |
| Gemini returns HTTP 503 (service unavailable) | The router retries up to 2 times with exponential backoff and jitter. |
| LLM returns invalid JSON | TeamPulse re-prompts the model once with the Pydantic schema. If it fails a second time, it switches to deterministic rule-based analysis (`analyze_fallback()`). |
| Slack posting fails | The error is logged. Flags are not written to the database. The next scheduled run will pick them up again. |
| Button clicked twice | The handler checks the database. If the flag is already approved, it sends an ephemeral message `Already approved by <@user>` and skips creating another ticket. |
| Forged Slack request | The HMAC signature check fails. TeamPulse rejects the request immediately with HTTP 401 Unauthorized. |

## 12. Safety Features Explained Simply

- **Dry-run mode (`DRY_RUN_ACTIONS=true`)**: A test mode where clicking Approve simulates the action, writes an audit record, and updates Slack, but never makes a real ticket in Jira.
- **HMAC signature verification**: Every message from Slack comes with a cryptographic signature. TeamPulse checks this signature with a secret key so no outside attacker can fake an approval.
- **Audit log**: Every run, priority flag, and human decision is permanently stored in MySQL tables (`agent_runs`, `flags`, `actions_taken`).
- **Rate limiting**: Limits queries per user in Slack and per API key on HTTP endpoints (default 5 queries per minute) so users cannot spam the system.

---

# Part C: How to Run It

This part provides exact commands to set up, run, and test TeamPulse.

## 13. Prerequisites

| Tool | Why It Is Needed | How to Check It Is Installed |
| :--- | :--- | :--- |
| Python 3.11+ | Runs the TeamPulse application | Run `python --version` in terminal |
| MySQL Server | Stores workspaces, flags, runs, and actions | Run `mysql --version` in terminal |
| Git | Clones the project repository | Run `git --version` in terminal |
| ngrok | Exposes local port 8000 to the public internet for Slack webhooks | Run `ngrok --version` in terminal |

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

| Setting Name | What It Controls |
| :--- | :--- |
| `GITHUB_TOKEN` | Token for reading GitHub pull requests |
| `GITHUB_REPOS` | Comma-separated repositories to monitor |
| `JIRA_BASE_URL` | Web address of your Jira instance |
| `JIRA_EMAIL` | Email address for Jira API login |
| `JIRA_API_TOKEN` | Secret API token for Jira |
| `JIRA_PROJECT_KEY` | Project key in Jira where issues belong |
| `SLACK_BOT_TOKEN` | Token starting with `xoxb-` for Slack bot actions |
| `SLACK_SIGNING_SECRET` | Secret key from Slack to verify webhook requests |
| `SLACK_CHANNEL_ID` | Channel ID where briefings are posted |
| `SLACK_APPROVER_IDS` | Slack user IDs permitted to click Approve |
| `MAX_FLAGS_IN_BRIEFING` | Maximum priority items shown in one briefing |
| `DRY_RUN_ACTIONS` | Set to true to test without creating real Jira tickets |
| `GEMINI_API_KEY` | Secret key for Google Gemini model |
| `GEMINI_MODEL` | Name of primary Gemini model |
| `GEMINI_FALLBACK_MODEL` | Name of backup Gemini model |
| `GEMINI_THINKING_LEVEL` | Thinking budget for Gemini (`low` or `normal`) |
| `OLLAMA_BASE_URL` | Web address for local Ollama server |
| `OLLAMA_MODEL` | Name of local Ollama model |
| `OLLAMA_ENABLED` | Set to true to allow local Ollama use |
| `LLM_TIMEOUT_SECONDS` | Maximum seconds to wait for an LLM answer |
| `STALE_PR_DAYS` | Days without activity before a PR is marked stale |
| `STALE_TICKET_DAYS` | Days without update before a ticket is marked stale |
| `ABANDONED_PR_DAYS` | Days before an old PR is treated as abandoned |
| `MAX_ITEMS_IN_PROMPT` | Maximum items sent in one prompt to an LLM |
| `MAX_COMMIT_LOOKUPS` | Limit on individual commit checks to save GitHub quota |
| `DATABASE_URL` | MySQL connection string |
| `TIMEZONE` | Timezone name for briefing timestamps |
| `ENABLE_SCHEDULER` | Set to true to run scheduled briefings automatically |
| `MORNING_BRIEFING_TIME` | Time in `HH:MM` format for morning run |
| `EVENING_BRIEFING_TIME` | Time in `HH:MM` format for evening run |
| `ADMIN_API_KEY` | Secret token for protected HTTP endpoints |
| `QUERY_RATE_LIMIT_PER_MINUTE` | Maximum queries allowed per minute per user |
| `QUERY_CACHE_TTL_SECONDS` | Seconds to keep collected work items in cache |
| `QUERY_LLM_REPHRASE` | Set to true to let LLM rephrase answers |

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

Configure Slack App Settings:
- In Slack App Management, go to **Interactivity & Shortcuts**. Set Request URL to:
  `https://abcdef123456.ngrok-free.app/slack/actions`
- Go to **Slash Commands**. Set Request URL for `/teampulse` to:
  `https://abcdef123456.ngrok-free.app/slack/commands`

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

What to look for in Slack:
A new briefing appears in your Slack channel with priority flags and Approve/Dismiss buttons.

## 16. How to Test

### Automated Unit Tests
Run pytest across the entire test suite:
```bash
pytest -q
```
Expected result:
```text
170 passed, 1 warning in 12.09s
```

### Offline Evaluation Suite
Run the 48-case deterministic evaluation suite:
```bash
python evals/run_eval.py
```
Expected result:
```text
==========================================================================================
                        TeamPulse Phase 6 Evaluation Suite
==========================================================================================
Category                 Cases    Passed   Accuracy   Mean (ms)    p95 (ms)  
------------------------------------------------------------------------------------------
action_safety            5        5        100.0%     4.03         15.54     
briefing_wording         5        5        100.0%     0.08         0.18      
failure_handling         5        5        100.0%     0.36         1.04      
grounding                6        6        100.0%     0.06         0.1       
query_count              6        6        100.0%     27.56        29.04     
query_lookup             5        5        100.0%     27.74        27.86     
query_out_of_scope       5        5        100.0%     27.13        27.73     
query_rank               5        5        100.0%     39.8         70.93     
stale_detection          6        6        100.0%     0.0          0.01      
==========================================================================================
Overall: 48/48 passed (100.0%) | p50: 0.44 ms | p95: 29.45 ms
Grounding Protection: 2/2 invented IDs blocked (simulated LLM output)
==========================================================================================
SUCCESS: All deterministic evaluation cases passed!
```

## 17. First 10 Minutes Checklist for a New Developer

- [ ] Cloned repo and created a virtual environment with Python 3.11+.
- [ ] Installed packages using `pip install -r requirements.txt`.
- [ ] Created `.env` from `.env.example`.
- [ ] Created MySQL `teampulse` database.
- [ ] Ran `python scripts/reset_db.py --yes` and verified tables created.
- [ ] Ran `pytest -q` and confirmed all 170 tests passed.
- [ ] Ran `python evals/run_eval.py` and confirmed all 48 cases passed.
- [ ] Started server with `uvicorn app.api.main:app --port 8000` and checked `curl http://localhost:8000/health`.

## 18. Common Problems and Real Fixes

| Problem | Cause | How to Fix |
| :--- | :--- | :--- |
| Jira returns HTTP 410 Gone | Outdated Jira REST endpoint or cookie login was used | TeamPulse uses modern REST API v3 endpoints. Check that `JIRA_BASE_URL` is an HTTPS Cloud address and your token has Jira Cloud permissions. |
| Briefing shows placeholder repos like `owner/repo1` | Default values remain in Windows environment variables or `.env` | Edit `.env` and set `GITHUB_REPOS=your-org/your-repo`. If set in Windows system environment variables, update them and restart terminal. |
| Slack returns `not_in_channel` error | The Slack bot user is not a member of the target channel | Open the channel in Slack and type `/invite @TeamPulse` to add the bot. |
| Server fails with `[Errno 10048]` (port in use) | A previous Uvicorn process is still running on port 8000 | In PowerShell, run `Get-Process python \| Stop-Process -Force` or use `taskkill /PID <pid> /F`. |
| Database error about missing column or table | Database was created with an older version of the models | Run `python scripts/reset_db.py --yes` to recreate the schema cleanly. |
| Gemini calls fail with HTTP 429 Resource Exhausted | Daily free tier quota (20 requests per day) reached | TeamPulse router tracks quota resets and skips blocked models. You can turn on Ollama (`OLLAMA_ENABLED=true`) or wait for quota reset. |

---

# End

## 19. Glossary

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

## 20. Things I Could Not Verify in the Code

1. **Cloud Deployment**: `PROJECT_SPEC.md` lists Google Cloud Run as a deployment target, but no Dockerfile or Cloud Run configuration files exist in the repository yet.
2. **LangChain Usage**: `PROJECT_SPEC.md` mentions LangChain in the stack list, but the implementation uses LangGraph, FastAPI, and Pydantic directly without importing the `langchain` package.
