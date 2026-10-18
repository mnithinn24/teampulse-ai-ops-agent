# TeamPulse AI Ops Agent — Complete Architecture, Flow & Functionality Guide

> **Document Purpose**: This comprehensive guide details the entire design, architecture, multi-agent workflows, and functional mechanisms of **TeamPulse AI Ops Agent**. It is structured for both human engineers and AI models (Claude, ChatGPT, Gemini) to immediately understand the project end-to-end.

---

## 1. Executive Summary & Core Mission

**TeamPulse** is an autonomous, production-grade AI Operations Agent that bridges **GitHub**, **Jira Cloud**, and **Slack**. It eliminates the daily operational overhead of tracking aging pull requests, stalled Jira tickets, unreviewed code changes, and manual project planning.

### Core Problems Solved
1. **Aging Pull Requests & Stalled Work**: PRs sit without reviews, and tickets linger in "In Progress" or "Done" without oversight.
2. **Context Switching**: Leads waste hours manually clicking through multiple GitHub tabs and Jira boards every morning.
3. **No Centralized Code Review & Verification**: Leads need to review PR diffs and commit histories before giving official approval in Slack.
4. **Tedious Project Planning**: Converting high-level business ideas into structured Epics, Tasks, Subtasks, and realistic hour estimates usually takes hours of manual entry in Jira.

---

## 2. System Architecture & Component Topology

```mermaid
graph TB
    subgraph "External Integrations"
        GH["GitHub API v3 & Webhooks<br/>(PRs, Commits, Merges, Diffs)"]
        JIRA["Jira Cloud REST API v3<br/>(Issues, Transitions, Epics, Comments)"]
        SLACK["Slack API & Block Kit<br/>(Interactive Buttons, Messages, Slash Commands)"]
    end

    subgraph "FastAPI Server (Port 8000)"
        MAIN["app/api/main.py<br/>(Lifespan, Middleware, Routing)"]
        
        subgraph "API Routes"
            R_BRIEF["/briefing, /runs, /query (Trigger, History, Query)"]
            R_SLACK["/slack (Interactions, Slash Commands)"]
            R_GITHUB["/github (Webhooks, PR Alerts, Merge)"]
            R_JIRA["/jira (Task Alerts, Poller, Webhooks)"]
            R_PLAN["/project (AI Planner UI & API)"]
        end
        
        subgraph "Autonomous Background Poller"
            POLLER["15s Jira Status Poller<br/>(Detects 'Done' -> Alerts Slack)"]
        end
    end

    subgraph "Core AI & Operations Agents"
        A1["Agent 1: Daily Briefing & Triage Agent"]
        A2["Agent 2: Human-in-the-Loop Action Agent"]
        A3["Agent 3: AI Project Planner & Provisioner"]
        A4["Agent 4: PR Completion & Code Review Alert"]
        A5["Agent 5: Jira Completion & Verification Agent"]
        A6["Agent 6: Grounded Operational Query Agent"]
    end

    subgraph "Intelligence & Guardrail Layer"
        ROUTER["Multi-Provider LLM Router<br/>(app/llm/router.py)"]
        GROUND["Grounding & Hallucination Defense<br/>(app/graph/pipeline.py)"]
        RETRY["Exponential Backoff & 429/503 Shield"]
        
        subgraph "LLM Providers"
            GEMINI_MAIN["Gemini 3.6-flash (Primary Cloud)"]
            GEMINI_FALLBACK["Gemini 3.5-flash-lite (Fast Cloud)"]
            OLLAMA["🦙 Ollama llama3.1 (LAN / Local)"]
            RULES["Deterministic Rule-Based Fallback"]
        end
    end

    subgraph "Persistence Layer"
        DB[("MySQL 8.0 / In-Memory SQLite<br/>(Workspaces, Runs, Flags, Actions, Plans)")]
    end

    GH <--> R_GITHUB
    JIRA <--> R_JIRA
    SLACK <--> R_SLACK
    MAIN --> R_BRIEF & R_SLACK & R_GITHUB & R_JIRA & R_PLAN & POLLER
    
    R_BRIEF --> A1 & A6
    R_SLACK --> A2 & A5
    R_GITHUB --> A4
    R_JIRA --> A5
    R_PLAN --> A3
    POLLER --> A5

    A1 & A2 & A3 & A4 & A5 & A6 --> ROUTER
    A1 & A6 --> GROUND
    ROUTER --> RETRY
    RETRY --> GEMINI_MAIN --> GEMINI_FALLBACK --> OLLAMA --> RULES
    MAIN --> DB
```

---

## 3. The 6 Specialized Agents

TeamPulse is organized into **6 purpose-driven agents**. Each agent owns a distinct phase of the engineering lifecycle:

| Agent | Module | Primary Trigger | Core Responsibility |
| :--- | :--- | :--- | :--- |
| **1. Daily Briefing & Triage Agent** | `app/agents/briefing.py`<br/>`app/graph/pipeline.py` | APScheduler (09:30 / 17:30)<br/>`/teampulse brief`<br/>`POST /briefing/trigger` | Fetches PRs & Jira tickets, detects stale work (>7d PR, >14d ticket), invokes LLM to triage urgency, grounds evidence, and posts interactive Slack briefing. |
| **2. Interactive Action Agent** | `app/agents/actions.py`<br/>`app/api/routes/slack.py` | Slack Button Click (`approve_action` / `dismiss_action`) on `POST /slack/interactions` | Validates HMAC-SHA256 signature, enforces idempotency against double-clicks, checks duplicate Jira tickets, adds comments or creates triage tickets in Jira, logs audit records in `actions_taken` (`dry_run`, `executed`, `skipped_duplicate`, `dismissed`, `failed`), and updates Slack message in-place. |
| **3. AI Project Planner Agent** | `app/agents/planner.py`<br/>`app/api/routes/planner.py` | Web UI (`/`)<br/>`/teampulse plan <desc>`<br/>`POST /project/plan` | Deconstructs free-form project ideas into 3-5 Epics, Tasks, and Subtasks. Calculates team capacity hours ($Devs \times Weeks \times 40h$) and scales hours proportionally to capacity (capacity allocations, not empirical estimates). Provisions complete hierarchy in Jira Cloud with one click. |
| **4. PR Completion & Code Review Alert** | `app/agents/pr_alert.py`<br/>`app/api/routes/github.py` | GitHub Webhook (`pull_request.opened`)<br/>`POST /github/pr/alert`<br/>`POST /github/pr/merge` | Extracts Jira keys (`[KAN-123]`) from PR title/branch, links Jira ticket in Slack, provides direct PR link, and enables `[ Approve & Merge ]` / `[ Decline ]` action buttons with 5 merge safety checks (approver list authorization, open state, mergeability, CI checks, dry run). Marks Jira ticket as Done on merge and records `jira_auto_done` in `actions_taken`. |
| **5. Jira Task Done & Verification Agent** | `app/api/routes/jira.py`<br/>`app/api/routes/slack.py` | 15s Background Poller<br/>Jira Webhook (`jira:issue_updated`) | Automatically detects when developer marks ticket "Done" in Jira. Resolves GitHub PR search URL (`.../pulls?q=is:pr+KEY`), posts review card to Slack with `[ 🔍 Open Pull Request ↗ ]`, `[ Approve & Verify ]`, and `[ Decline & Reopen ]`. Skips tickets TeamPulse moved itself (`jira_auto_done`, `jira_task_verified`) or already alerted (`jira_task_alerted`). |
| **6. Grounded Query Agent & Shield** | `app/agents/query.py`<br/>`app/api/routes/briefing.py` | `/teampulse ask <query>`<br/>`POST /query` | Answers operational questions (ranking by age, counting by author) over 100% real collected data. Uses Python for 100% of arithmetic, sorting, and counting. Governed by `QUERY_LLM_REPHRASE`: when `false` (default), returns the code-built answer directly (`model_used="code"`); when `true`, the LLM rephrases the answer with grounding verification. Shields against non-operational prompts. |

---

## 4. In-Depth Agent Workflows & Life of an Event

### Flow 1: Daily Scheduled Briefing & Triage (Agent 1 & 2)

```mermaid
sequenceDiagram
    autonumber
    participant Sched as APScheduler / Admin
    participant Pipe as LangGraph Pipeline
    participant GH as GitHub REST API
    participant Jira as Jira Cloud REST API
    participant Router as Multi-Provider LLM Router
    participant Ground as Grounding Engine
    participant Slack as Slack Channel (#teampulse)
    participant Lead as Engineering Lead

    Sched->>Pipe: Trigger Briefing Run
    par Concurrent Ingestion
        Pipe->>GH: GET /repos/{repo}/pulls (Fetch open PRs)
        Pipe->>Jira: POST /rest/api/3/search/jql (Fetch open tickets)
    end
    Note over Pipe: Compute elapsed age, days since update, and stale flags
    Pipe->>Router: Send Stale Items + Prompt
    Router-->>Pipe: Structured Priority Flags (Pydantic validated)
    Pipe->>Ground: Run Grounding Verification
    Note over Ground: Verify all IDs exist in real data. Drop hallucinations.
    Pipe->>Slack: Post Block Kit Briefing (Flags + Approve / Dismiss buttons)
    Lead->>Slack: Clicks [ Approve ] on Stale PR Flag
    Slack->>Pipe: POST /slack/interactions (HMAC signed)
    Pipe->>Jira: Check duplicate -> Create "Triage stale PR" ticket
    Pipe->>Slack: Replace buttons with "✅ Approved by @Lead (Created KAN-123)"
```

1. **Trigger**: APScheduler triggers at 09:30 or 17:30 (or manual API/Slack call).
2. **Collect Node**: Concurrently queries GitHub for open PRs and Jira for open tickets using `asyncio.gather`.
3. **Analyze Node**: Filters out items flagged in the last 24 hours. Passes new stale items to the LLM Router.
4. **Grounding Defense**: Every item ID returned by the LLM is checked against actual fetched objects. If an ID was invented, it is immediately discarded. Real author names and ages overwrite model text.
5. **Slack Delivery & Persistence**: Flags are saved to the database in `pending` status *before* posting so their generated database IDs can be bound into Slack interactive button action values (`value=str(flag.id)`). Then the Block Kit briefing is posted to Slack, and the message timestamp (`slack_ts`) is committed.
6. **Human Approval**: The lead reviews flags in Slack and clicks `[ Approve ]` or `[ Dismiss ]`. TeamPulse executes the action (adds comment or creates Jira ticket), records an audit log entry in `actions_taken` (`dry_run`, `executed`, `skipped_duplicate`, `dismissed`, or `failed`), and updates the Slack message in place.

---

### Flow 2: AI Project Planner & Jira Hierarchy Provisioning (Agent 3)

```mermaid
sequenceDiagram
    autonumber
    actor Lead as Engineering Lead
    participant UI as Web UI / Slack (/teampulse plan)
    participant Planner as AI Project Planner (planner.py)
    participant Router as LLM Router (Gemini / Ollama)
    participant Jira as Jira Cloud (REST API v3)

    Lead->>UI: "Medical RAG, 2 developers, 2 weeks"
    UI->>Planner: parse_plan_request_text()
    Note over Planner: Extract Devs=2, Weeks=2.0 -> Capacity = 160 Hours
    Planner->>Router: Generate ProjectPlan (3-5 Epics, Tasks, Subtasks, 5-Line Descriptions)
    Router-->>Planner: Validated ProjectPlan Schema
    Note over Planner: normalize_and_balance_hours() -> Scales hours proportionally to match 160h capacity
    Planner-->>UI: Return Interactive Plan Preview (with model badge)
    Lead->>UI: Clicks [ Provision to Jira ]
    loop Hierarchical Provisioning
        Planner->>Jira: 1. Create Epics (issuetype: Epic)
        Planner->>Jira: 2. Create Tasks (issuetype: Task, parent: Epic Key)
        Planner->>Jira: 3. Create Subtasks (issuetype: Sub-task, parent: Task Key)
    end
    Planner-->>UI: Return Provisioned Jira Keys (e.g. KAN-274, KAN-275...)
```

#### The Capacity Formula & Allocation
- **Capacity Formula**: $\text{Total Capacity Hours} = \text{Developers} \times \text{Weeks} \times 40\text{ hours/week}$.
  - *Example*: 2 developers for 2 weeks = **160 hours**.
- **Hours are Capacity Allocations, Not Real Estimates**: The hours assigned to Epics, Tasks, and Subtasks are mathematical capacity allocations scaled proportionally by `normalize_and_balance_hours()` to fit the total available developer capacity ($\text{Developers} \times \text{Weeks} \times 40\text{h}$), rather than empirical work estimates or historical velocity.
- **Hour Normalization**: `normalize_and_balance_hours()` ensures that $\sum \text{Subtask Hours} = \text{Task Hours}$, $\sum \text{Task Hours} = \text{Epic Hours}$, and $\sum \text{Epic Hours} = \text{Total Capacity}$.
- **5-Point Task Description Standards**:
  - **Line 1 (Technical Goal)**: Feature scope and business necessity.
  - **Line 2 (Architecture & Schemas)**: Relevant models, database tables, and API contracts.
  - **Line 3 (Implementation Steps)**: Step-by-step algorithms, chunking/query logic, libraries.
  - **Line 4 (Dependencies & Errors)**: Timeout retries, input sanitization, edge cases.
  - **Line 5 (Acceptance Criteria)**: Testable condition for Done.

---

### Flow 3: Developer Marks Task "Done" in Jira -> Code Review in Slack (Agent 5)

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer (Nithin)
    participant Jira as Jira Cloud (KAN-275)
    participant Poller as TeamPulse 15s Auto-Poller
    participant Slack as Slack Channel (#rag-project)
    actor Lead as Team Lead
    participant Git as GitHub (mnithinn24/teampulse-ai-ops-agent)

    Dev->>Jira: Moves KAN-275 to "Done"
    Poller->>Jira: Polls: project = 'KAN' AND status in ('Done') ORDER BY updated DESC
    Poller->>Poller: is_ticket_handled_or_alerted() -> Checks actions_taken in DB
    Note over Poller: Skips tickets TeamPulse moved itself or already alerted
    Poller->>Git: Resolves PR Search URL (https://github.com/.../pulls?q=is:pr+KAN-275)
    Poller->>Poller: record_ticket_alerted() -> Persists jira_task_alerted to DB
    Poller->>Slack: Posts Task Review Card with [ 🔍 Open Pull Request ↗ ]
    Lead->>Git: Clicks [ 🔍 Open Pull Request ↗ ] -> Inspects Commits & Diffs
    alt Approved
        Lead->>Slack: Clicks [ Approve & Verify ]
        Slack->>Jira: Adds Comment: "Task approved & verified by @Lead at YYYY-MM-DD HH:MM"
        Slack->>Slack: In-place update: "✅ Task Approved & Verified by @Lead at YYYY-MM-DD HH:MM"
        Slack->>Poller: Records ActionTaken(action_type="jira_task_verified", status="executed")
    else Declined
        Lead->>Slack: Clicks [ Decline & Reopen ]
        Slack->>Jira: Transitions ticket back to "In Progress" (Transition ID: 21)
        Slack->>Jira: Adds Comment: "Task declined by @Lead at YYYY-MM-DD HH:MM. Reopened for rework."
        Slack->>Slack: In-place update: "❌ Task Declined by @Lead — Reopened to In Progress"
        Slack->>Poller: mark_issue_reopened() -> Deletes jira_task_alerted from DB
    end
```

#### Key Implementation Details
1. **GitHub PR Search Link (Not Direct PR Link)**: The pull request link resolved by `resolve_pr_url_for_issue()` in `app/api/routes/jira.py` is a **GitHub search URL** (`https://github.com/{primary_repo}/pulls?q=is%3Apr+{issue_key}`), not an exact direct PR link (such as `/pull/42`). This searches open and closed PRs matching the ticket key across the repository.
2. **How the Poller Skips Tickets TeamPulse Moved Itself & Deduplicates**:
   - When TeamPulse auto-merges a PR in Flow 4, it transitions the Jira ticket to "Done" and immediately writes `ActionTaken(action_type="jira_auto_done", target_id=jira_key, status="executed")`.
   - When a lead verifies a task, TeamPulse writes `ActionTaken(action_type="jira_task_verified", target_id=issue_key, status="executed")`.
   - Whenever an alert is posted to Slack, TeamPulse writes `ActionTaken(action_type="jira_task_alerted", target_id=issue_key, status="executed")`.
   - The 15-second background poller and `/jira/webhook` call `is_ticket_handled_or_alerted(issue_key)`. This queries `actions_taken` in the database: if a record exists with `action_type in ("jira_auto_done", "jira_task_verified", "jira_task_alerted")` and `status="executed"`, or if a `pr_approve_and_merge` payload contains the key, the ticket is safely skipped.
   - **Cross-Restart Persistence**: Because alerted tickets are recorded in the database table `actions_taken` (`jira_task_alerted`), restarting the server will never cause TeamPulse to re-alert tickets that were already alerted.
   - **Reopen Lifecycle**: If a lead clicks `[ Decline & Reopen ]`, `mark_issue_reopened(issue_key)` removes the `jira_task_alerted` record from both the database and the in-memory cache, allowing the ticket to trigger a fresh review card if the developer moves it back to "Done" later.

---

### Flow 4: Developer Opens PR -> Slack Alert -> One-Click Auto-Merge (Agent 4)

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer
    participant Git as GitHub
    participant TP as TeamPulse (/github/webhook)
    participant Jira as Jira Cloud
    participant Slack as Slack Channel (#teampulse)
    actor Lead as Team Lead

    Dev->>Git: Opens Pull Request #42 ("[KAN-10] Implement RAG pipeline")
    Git->>TP: Webhook POST /github/webhook
    TP->>TP: extract_jira_keys() -> Finds "KAN-10"
    TP->>Jira: GET /rest/api/3/issue/KAN-10 (Fetch ticket context)
    TP->>Slack: Posts PR Review Card (PR Link + Jira Link + [Approve & Merge] / [Decline])
    Lead->>Git: Clicks PR link to review code changes
    Lead->>Slack: Clicks [ Approve & Merge ]
    Note over TP: Runs 5-point Merge Safety Checks
    TP->>Git: PUT /repos/{repo}/pulls/42/merge (Merge to main)
    TP->>Jira: Transition KAN-10 to "Done" + Adds timestamped merge comment
    TP->>TP: Records ActionTaken(action_type="jira_auto_done", status="executed")
    TP->>Slack: Replaces buttons with "✅ Approved & Merged by @Lead at YYYY-MM-DD HH:MM"
```

#### Merge Safety Checks & Guardrails (`handle_pr_approval_action`)
Before calling the GitHub Merge API, TeamPulse executes 5 strict safety checks. If any check fails, the merge is refused and recorded in `actions_taken` with `status="refused"`:
1. **Approver List Authorization**: The clicking user must be in `SLACK_APPROVER_IDS`.
   - **Empty List Refusal**: If `SLACK_APPROVER_IDS` is empty or unconfigured, TeamPulse **REFUSES** all merge actions with a clear Slack warning explaining how to configure authorized approver IDs in `.env`.
   - **Unauthorized User Refusal**: If the clicking user ID is not in `SLACK_APPROVER_IDS`, the merge is refused with an unauthorized notice.
2. **Dry Run Safety Switch**: If `DRY_RUN_ACTIONS=true` (or `dry_run=True`), TeamPulse simulates the merge and Jira transitions, returning `[DRY RUN] Would merge {repo}#{pull_number} to main` and logging `status="dry_run"` in `actions_taken` without modifying GitHub or Jira.
3. **PR State & Merged Check**: Fetches live PR details from GitHub.
   - If `merged == True`, refuses with `:warning: Merge refused: Pull request {repo}#{pull_number} is already merged.`
   - If `state != "open"`, refuses with `:warning: Merge refused: Pull request {repo}#{pull_number} is not open (current state: {state}).`
4. **Mergeability (Conflict) Check**: Inspects `mergeable` and `mergeable_state`. If `mergeable is False` or `mergeable_state` is in `("dirty", "conflict")`, refuses with `:warning: Merge refused: Pull request {repo}#{pull_number} has merge conflicts and is not mergeable.`
5. **CI Status & Check Runs**: Queries GitHub's commit status API (`get_commit_status`) and check runs API (`get_commit_check_runs`) for the head commit SHA:
   - **No CI Configured**: If both status count and check runs count are 0, merge is permitted, and ` (no CI found)` is appended to the resolution message.
   - **Combined Commit Status**: If status count > 0 and combined state is not `success`, refuses with `:warning: Merge refused: CI status is {state} for commit {sha}.`
   - **Check Runs**: If check runs exist, every run must have `status == "completed"` and `conclusion in ("success", "neutral", "skipped")`. If any check run is still in progress or failed, merge is refused.
6. **Alert Loop Prevention**: Upon a successful live merge, TeamPulse marks the linked Jira ticket as Done and records `ActionTaken(action_type="jira_auto_done", target_id=jira_key, status="executed")`. This prevents Flow 3's background poller from re-alerting for the same ticket.

---

### Flow 5: Plain Language Query with Grounded Shield (Agent 6)

```mermaid
sequenceDiagram
    autonumber
    actor User as Engineer / Lead
    participant Slack as Slack Slash Command (/teampulse ask)
    participant QueryEngine as Grounded Query Engine (query.py)
    participant Ingestion as In-Memory Live Ingestion
    participant Router as LLM Router (Ollama / Gemini)

    User->>Slack: /teampulse ask "Who has the most open PRs?"
    Slack->>QueryEngine: POST /query
    QueryEngine->>Ingestion: Fetch 100% of open PRs and Jira tickets
    Note over QueryEngine: Rule-Based Classifier & Keyword Detection
    Note over QueryEngine: Python computes ranking & counting (Zero arithmetic error)
    alt QUERY_LLM_REPHRASE=false (Default)
        Note over QueryEngine: Returns code-built template directly (model_used="code")
    else QUERY_LLM_REPHRASE=true
        QueryEngine->>Router: Pass pre-computed tables to LLM for conversational phrasing
        Router-->>QueryEngine: Formatted natural language response
        Note over QueryEngine: verify_rephrased_answer() validates numbers and IDs
    end
    QueryEngine-->>Slack: Return grounded operational answer with exact citations
```

#### Hallucination Defense & The `QUERY_LLM_REPHRASE` Setting
- **"Code Computes, LLM Phrases" (Governed by `QUERY_LLM_REPHRASE`)**:
  - Python executes 100% of arithmetic, sorting, filtering, and counting operations.
  - **When `QUERY_LLM_REPHRASE=false` (Default in `app/config.py`)**: The code-built template answer is returned directly to the user (`model_used="code"`). Zero LLM tokens are spent on phrasing, latency is instantaneous, and arithmetic hallucination risk is completely eliminated.
  - **When `QUERY_LLM_REPHRASE=true`**: The code passes the pre-computed template to the LLM router to generate a conversational sentence. The output is then verified by `verify_rephrased_answer()`. If verification fails or the call times out (15s), TeamPulse reverts safely to the code-built template answer.
- **Out-of-Scope Shield**: If a user asks a non-operational question (e.g. *"What is quantum computing?"* or *"Write a poem"*), the query engine immediately rejects it with:  
  `:warning: I can only answer operational questions about open GitHub pull requests and Jira tickets.` (Zero LLM tokens spent).

---

## 5. Multi-Provider LLM Router & 4-Tier Fallback

TeamPulse features an automated, resilient model router (`app/llm/router.py`) that shields the platform from model outages, network timeouts, and quota exhaustion:

```mermaid
graph TD
    A["Incoming Task (Analysis / Intent / Planning)"] --> B{"Task Nature?"}
    
    B -->|"Simple (Intent, Classify)"| C["1. 🦙 Ollama llama3.1 (LAN / Local)"]
    C -->|"If Offline"| D["2. Gemini 3.6-flash (Cloud)"]
    D -->|"If Busy"| E["3. Gemini 3.5-flash-lite"]
    
    B -->|"Complex (Planning, Triage)"| F["1. Gemini 3.6-flash (Primary Cloud)"]
    F -->|"503 High Traffic or 429 Quota"| G["2. Gemini 3.5-flash-lite (Fast Cloud)"]
    G -->|"If Unavailable"| H["3. 🦙 Ollama llama3.1 (Local: http://localhost:11434)"]
    H -->|"If Local Offline"| I["4. Deterministic Rule-Based Planner"]

    style A fill:#4338ca,stroke:#312e81,color:#fff
    style C fill:#10b981,stroke:#047857,color:#fff
    style F fill:#2563eb,stroke:#1d4ed8,color:#fff
    style H fill:#f59e0b,stroke:#b45309,color:#fff
    style I fill:#ef4444,stroke:#b91c1c,color:#fff
```

### Quota Exhaustion (429) & Traffic Spike (503) Defense
1. **HTTP 429 (Resource Exhausted)**: The router parses the `retry-after` header or delay string, and places the affected model into a `_blocked_models` registry. Subsequent requests automatically skip that model until the cool-down timer expires.
2. **HTTP 503 (Unavailable / High Demand)**: Automatically caught by `retry_async(retry_if=is_retryable_llm_error)` with exponential backoff and jitter. If the primary model remains unavailable, it immediately cascades to `gemini-3.5-flash-lite`, then to `ollama`, and finally to deterministic rules.
3. **Structured Pydantic Enforcement**: All outputs are strictly validated via Pydantic v2 schemas (`ProjectPlan`, `EpicSpec`, `TaskSpec`, `AnalysisResponse`). Markdown fences (` ```json `) and trailing junk are automatically sanitized by `clean_json_text()`.

---

## 6. Database Schema & Persistence

TeamPulse uses **SQLAlchemy 2.0** with complete connection resilience (MySQL with `pymysql` for production; in-memory SQLite for test suites):

```mermaid
erDiagram
    workspaces ||--o{ agent_runs : executes
    agent_runs ||--o{ flags : produces
    workspaces ||--o{ actions_taken : records
    workspaces ||--o{ project_plans : stores

    workspaces {
        int id PK
        string workspace_id
        string name
        string github_repos
        string jira_project_key
        datetime created_at
        datetime updated_at
    }

    agent_runs {
        int id PK
        int workspace_id FK
        string run_type
        string status
        int items_collected
        int flags_created
        float total_latency_ms
        string model_used
        datetime created_at
        datetime completed_at
    }

    flags {
        int id PK
        int run_id FK
        string workspace_id
        string item_type
        string item_id
        string urgency
        string flag_reason
        string suggested_action
        string status
        string approved_by
        datetime decided_at
    }

    actions_taken {
        int id PK
        string workspace_id
        string action_type
        string target_id
        string payload
        string status
        string approved_by
        string result
        string error
        datetime executed_at
        datetime created_at
    }

    project_plans {
        int id PK
        string workspace_id
        string project_name
        int developers_count
        float weeks_duration
        int total_capacity_hours
        int total_estimated_hours
        string plan_json
        string jira_epic_keys
        string status
        string generated_by
        datetime created_at
    }
```

---

## 7. Security, Idempotency & Operational Safeguards

1. **HMAC-SHA256 Slack Request Verification**:
   - Every incoming request to `/slack/interactions` or `/slack/commands` is validated against `SLACK_SIGNING_SECRET` using `hmac.new(secret, basestring, hashlib.sha256)`.
   - Replay attacks are blocked using a strict **5-minute timestamp window** (`abs(now - timestamp) <= 300`).
2. **Double-Click Shielding & Idempotent Database Transitions**:
   - When an approver clicks `Approve`, an atomic SQL conditional update executes:  
     `UPDATE flags SET status='approved' WHERE id=:id AND status='pending'`
   - If `rowcount == 0`, TeamPulse immediately stops execution and sends an ephemeral message: *"Item has already been handled"*, preventing duplicate Jira tickets.
3. **Pre-Creation Jira Duplicate Search**:
   - Before Agent 2 creates a new Jira ticket for a stale PR, it runs a JQL search:  
     `project = 'KAN' AND text ~ 'owner/repo#42'`
   - If an issue is already tracking the PR, ticket creation is safely skipped and recorded as `skipped_duplicate`.
4. **Safety Switches & Exact Audit Status Words**:
   - All actions recorded in the `actions_taken` database table use strictly defined status words matching the implementation in `app/agents/actions.py`, `app/agents/pr_alert.py`, and `app/api/routes/slack.py`:
     - **`dry_run`**: When `DRY_RUN_ACTIONS=true` (or `dry_run=True`), external API writes to GitHub and Jira are simulated and logged without altering external tools.
     - **`executed`**: External operations successfully executed (e.g. Jira triage ticket created, comment posted, PR merged to main, task verified in Jira, or task alerted to Slack).
     - **`skipped_duplicate`**: Action safely skipped because an open Jira issue already exists tracking the item.
     - **`dismissed`**: Flag dismissed by an authorized user without taking external actions.
     - **`failed`**: External API call or database update threw an unhandled exception.
     - **`refused`**: Merge action safely blocked by PR merge guardrails (empty approver list, unauthorized user, PR not open/already merged, merge conflicts, or CI check failure/in-progress).
     *(Note: `simulated` and `success` are never used as status values in `actions_taken`).*

---

## 8. Complete API & Slash Commands Reference

### REST API Endpoints

| Method | Endpoint | Description | Auth / Security |
| :--- | :--- | :--- | :--- |
| `GET` | `/` | Serves AI Project Planner Web UI dashboard | Public |
| `GET` | `/health` | Application health check, scheduler status & last run | Public |
| `GET` | `/static/{file}` | Serves static assets (`styles.css`, `app.js`) | Public |
| `POST` | `/briefing/trigger` | Triggers immediate briefing execution pipeline (202 Accepted) | `X-API-Key: ADMIN_API_KEY` |
| `GET` | `/runs/{run_id}` | Fetches run execution status, model used, node latencies, and errors | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/query` | Executes natural language operational query over collected items | `X-API-Key: ADMIN_API_KEY` |
| `GET` | `/llm/status` | Reports blocked status and cooldown timer of configured LLM models | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/project/plan` | Generates structured project plan and provisions Epics/Tasks/Subtasks to Jira | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/project/plan/preview` | Generates plan and hour allocation preview without creating Jira tickets | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/project/provision` | Provisions a previously previewed plan hierarchy directly to Jira Cloud | `X-API-Key: ADMIN_API_KEY` |
| `GET` | `/project/config` | Retrieves connected Jira project key, base URL, and planner UI defaults | Public |
| `POST` | `/project/ticket/update` | Updates summary, description, and hours of a Jira ticket or preview ticket | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/github/webhook` | Receives GitHub pull request webhooks (`opened`, `ready_for_review`) | `X-Hub-Signature-256` |
| `POST` | `/github/pr/alert` | Manually dispatches PR review alert to Slack (alias `/github/alert`) | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/github/pr/merge` | Executes one-click PR auto-merge on GitHub and marks Jira ticket Done | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/jira/webhook` | Receives live Jira issue status webhooks (`jira:issue_updated`) | Jira Webhook Secret (`X-Atlassian-Webhook-Token`, `X-Jira-Webhook-Secret`, or `?secret=`) |
| `POST` | `/jira/alert` | Dispatches Jira task review alert to Slack | `X-API-Key: ADMIN_API_KEY` |
| `POST` | `/slack/interactions` | Receives button clicks from Slack Block Kit (Approve/Dismiss, Merge, Verify) | HMAC-SHA256 Signature |
| `POST` | `/slack/commands` | Handles `/teampulse` slash command interactions (`brief`, `ask`, `plan`, `status`) | HMAC-SHA256 Signature |

### Slack Slash Commands (`/teampulse`)

| Command | Example | What Happens |
| :--- | :--- | :--- |
| `/teampulse brief` | `/teampulse brief` | Triggers a fresh, live briefing run and posts the interactive report to the channel. |
| `/teampulse ask <query>` | `/teampulse ask Who has the most open PRs?` | Answers operational queries with grounded metrics and citations. |
| `/teampulse plan <text>` | `/teampulse plan Medical RAG, 2 devs, 2 weeks` | Runs the AI Project Planner and returns a structured plan preview in Slack. |
| `/teampulse status` | `/teampulse status` | Displays execution telemetry, model used, and flag metrics from the latest agent run. |
| `/teampulse help` | `/teampulse help` | Displays the command guide and examples. |

---

## 9. Comprehensive Environment Configuration (`.env`)

```ini
# ==============================================================================
# GitHub Configuration
# ==============================================================================
GITHUB_TOKEN=github_pat_xxx               # GitHub Personal Access Token
GITHUB_REPOS=mnithinn24/teampulse-ai-ops-agent,psf/requests  # Monitored repositories
GITHUB_WEBHOOK_SECRET=xxx                 # Secret for HMAC-SHA256 webhook validation

# ==============================================================================
# Jira Cloud Configuration
# ==============================================================================
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_EMAIL=engineer@company.com
JIRA_API_TOKEN=xxx                       # Atlassian API Token
JIRA_PROJECT_KEY=KAN                     # Target Jira project key
JIRA_WEBHOOK_SECRET=xxx                  # Secret for verifying incoming Jira Cloud webhooks
JIRA_REOPEN_TRANSITION_ID=21             # Jira workflow transition ID to reopen issues to In Progress

# ==============================================================================
# Slack Configuration
# ==============================================================================
SLACK_BOT_TOKEN=xoxb-xxx                 # Slack Bot User OAuth Token
SLACK_SIGNING_SECRET=xxx                 # Slack App Signing Secret for HMAC verification
SLACK_CHANNEL_ID=C0123456789             # Target channel (e.g. #rag-project)
SLACK_APPROVER_IDS=                      # Comma-separated user IDs (empty refuses merge actions)
MAX_FLAGS_IN_BRIEFING=10                 # Flag truncation threshold
DRY_RUN_ACTIONS=true                     # Safety switch: true = simulate write actions

# ==============================================================================
# LLM Providers Configuration
# ==============================================================================
GEMINI_API_KEY=AIzaSyxxx                 # Google Gemini API Key
GEMINI_MODEL=gemini-3.6-flash            # Primary cloud model
GEMINI_FALLBACK_MODEL=gemini-3.5-flash-lite # Fast cloud fallback
GEMINI_THINKING_LEVEL=low                # Reasoning level (minimal, low, medium, high)

OLLAMA_BASE_URL=http://localhost:11434   # Local / LAN Ollama endpoint
OLLAMA_MODEL=llama3.1:latest             # Local fallback model
OLLAMA_ENABLED=false                     # Enable local/LAN model routing
LLM_TIMEOUT_SECONDS=60.0                 # Timeout before attempting fallback
QUERY_LLM_REPHRASE=false                 # Rephrase query answers with LLM (false returns code template)

# ==============================================================================
# Operational Thresholds
# ==============================================================================
STALE_PR_DAYS=7                          # Days of inactivity before flagging PR
STALE_TICKET_DAYS=14                     # Days of inactivity before flagging Jira ticket
TIMEZONE=Asia/Kolkata                    # Localization timezone

# ==============================================================================
# Persistence & Database
# ==============================================================================
DATABASE_URL=mysql+pymysql://root:password@127.0.0.1:3306/teampulse

# ==============================================================================
# Security & Scheduling
# ==============================================================================
ADMIN_API_KEY=your-secret-admin-api-key   # API key for administrative REST endpoints
ENABLE_SCHEDULER=false                   # Run automated morning/evening cron jobs
MORNING_BRIEFING_TIME=09:30
EVENING_BRIEFING_TIME=17:30
```

---

## 10. Test Suite & Verification Matrix

The codebase features an exhaustive, deterministic test suite with **222 passing automated tests** requiring zero live network calls:

```bash
# Run complete test suite
pytest -q
# Output: 222 passed, 7 warnings in 13.33s
```

### Key Test Categories:
1. **GitHub Client Suite** (`tests/test_github_client.py`): ETag caching (304), rate-limit handling, commit age calculation.
2. **Jira Client Suite** (`tests/test_jira_client.py`): JQL pagination, hierarchy creation, rate limiting.
3. **Slack Interactions & Security** (`tests/test_slack_interaction.py`): HMAC signature verification, timestamp expiration, duplicate action shielding.
4. **AI Project Planner** (`tests/test_planner.py`): Capacity calculation, hour balancing, schema validation, Jira provisioning dry-run.
5. **Phase 9 PR Alert & Auto-Merge** (`tests/test_phase9_pr_alert.py`): Webhook parsing, key extraction, live merge execution.
6. **Phase 10 Auto-Merge** (`tests/test_phase10_auto_merge.py`): GitHub merge API calls and Jira transition chaining.
7. **Simulation & E2E Verification** (`tests/test_simulation_e2e.py`): End-to-end full pipeline simulations across all 10 features.

---

## 11. Known Limits

1. **Not Deployed to Cloud Production**:
   - TeamPulse currently runs as a local FastAPI application (defaulting to `http://localhost:8000`), using either an in-memory SQLite database (test suite) or local MySQL (`mysql+pymysql`). It is not yet packaged or hosted on cloud serverless infrastructure (AWS Lambda, Google Cloud Run) or a production container orchestrator.
2. **Free Gemini API Quotas**:
   - The application relies primarily on Google Gemini Free Tier quotas (Requests Per Minute / Requests Per Day). High-frequency calls or rapid automated bursts can trigger HTTP 429 (`ResourceExhausted`). While the multi-provider router shields against downtime by cascading to `gemini-3.5-flash-lite`, local Ollama, or deterministic rules, production scaling requires a paid tier.
3. **Synthetic Evaluation Cases**:
   - The evaluation framework (`evals/labeled_cases.json`) and test suite rely on curated, deterministic fixtures and synthetic mock cases rather than live multi-tenant production repositories and enterprise Jira boards.
4. **Jira and GitHub Webhooks Require Public Ingress**:
   - Real-time inbound webhooks from cloud SaaS providers (`/jira/webhook`, `/github/webhook`, `/slack/interactions`) require a public HTTPS URL. For local developer environments, a tunneling service (e.g. ngrok or Cloudflare Tunnel) is required to receive live push events. However, the built-in 15-second background Jira poller (`poll_jira_completed_tasks_loop`) works autonomously without requiring webhooks or public ingress.
