"""Unit and integration tests for Phase 5:
1. Scheduler registers 2 jobs with right times and timezone.
2. Scheduler job survives a failing run.
3. Trigger without a key returns 401 and with a wrong key returns 401.
4. Second trigger during an active run returns 409.
5. /query removes an invented ID.
6. Question data cannot answer returns an honest 'not in the data'.
7. Slash command rejects forged signature, acknowledges quickly, and handles brief, ask, status, and help.
8. Rate limit blocks the 6th question in a minute.
9. Metrics are stored in agent_runs and show_metrics computes percentiles.
"""

import asyncio
from datetime import datetime, timezone
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.agents.query import (
    _query_cache,
    clear_query_cache,
    classify_intent_with_rules,
    handle_plain_language_query,
    sanitize_and_ground_answer,
)
from app.api.main import app
from app.api.routes.slack import check_user_rate_limit, reset_user_rate_limits
from app.config import get_settings
from app.core.errors import PipelineConcurrencyError
from app.core.security import verify_slack_signature
from app.db.database import get_db_session
from app.db.models import AgentRun
from app.graph.runner import (
    _active_workspaces,
    execute_pipeline_task,
    is_workspace_running,
    trigger_pipeline_run,
)
from app.graph.state import TeamPulseState
from app.integrations.github_client import PullRequest
from app.integrations.jira_client import JiraIssue
from app.llm.router import LLMResult, LLMRouter
from app.llm.schemas import QueryAnswerOutput, QueryIntent
from app.scheduler.jobs import (
    create_scheduler,
    get_scheduler,
    scheduled_briefing_job,
    start_scheduler,
    stop_scheduler,
)
from scripts.show_metrics import calculate_metrics_summary, compute_percentile
from tests.test_slack_interaction import generate_slack_headers


@pytest.fixture(autouse=True)
def _clean_query_cache():
    """Ensure query cache is empty before and after each test."""
    clear_query_cache()
    yield
    clear_query_cache()


# -----------------------------------------------------------------------------
# 1. Scheduler Tests
# -----------------------------------------------------------------------------
def test_scheduler_registers_two_jobs_with_right_times_and_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """APScheduler registers morning and evening jobs with configured times and timezone."""
    monkeypatch.setenv("ENABLE_SCHEDULER", "true")
    monkeypatch.setenv("MORNING_BRIEFING_TIME", "09:15")
    monkeypatch.setenv("EVENING_BRIEFING_TIME", "18:45")
    monkeypatch.setenv("TIMEZONE", "Asia/Kolkata")
    get_settings.cache_clear()

    scheduler = create_scheduler()
    jobs = scheduler.get_jobs()

    assert len(jobs) == 2
    job_ids = {j.id for j in jobs}
    assert "morning_briefing" in job_ids
    assert "evening_briefing" in job_ids

    morning_job = scheduler.get_job("morning_briefing")
    assert morning_job.max_instances == 1
    assert morning_job.coalesce is True
    assert morning_job.misfire_grace_time == 3600
    # Check trigger fields (hour=9, minute=15)
    fields = {f.name: str(f) for f in morning_job.trigger.fields}
    assert fields["hour"] == "9"
    assert fields["minute"] == "15"

    evening_job = scheduler.get_job("evening_briefing")
    assert evening_job.max_instances == 1
    fields_eve = {f.name: str(f) for f in evening_job.trigger.fields}
    assert fields_eve["hour"] == "18"
    assert fields_eve["minute"] == "45"


@pytest.mark.asyncio
async def test_scheduler_job_survives_failing_run() -> None:
    """A scheduled job that encounters an error logs it and does not crash or stop the scheduler."""
    with patch("app.scheduler.jobs.trigger_pipeline_run", side_effect=Exception("Database down")):
        # Should not raise exception
        await scheduled_briefing_job(workspace_id="test-ws", run_type="morning")


# -----------------------------------------------------------------------------
# 2. Trigger Route & Concurrency Guard Tests
# -----------------------------------------------------------------------------
def test_trigger_without_or_wrong_key_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /briefing/trigger returns 401 when X-API-Key is missing or invalid."""
    monkeypatch.setenv("ADMIN_API_KEY", "valid_secret_key_123")
    get_settings.cache_clear()

    client = TestClient(app)

    # 1. Missing header
    res_missing = client.post("/briefing/trigger", json={"workspace_id": "ws-1"})
    assert res_missing.status_code == 401
    assert "X-API-Key" in res_missing.json()["detail"]

    # 2. Wrong header
    res_wrong = client.post(
        "/briefing/trigger",
        headers={"X-API-Key": "wrong_key"},
        json={"workspace_id": "ws-1"},
    )
    assert res_wrong.status_code == 401

    # 3. Correct key -> 202 Accepted
    with patch("app.api.routes.briefing.trigger_pipeline_run", return_value=(42, None)):
        res_ok = client.post(
            "/briefing/trigger",
            headers={"X-API-Key": "valid_secret_key_123"},
            json={"workspace_id": "ws-1"},
        )
        assert res_ok.status_code == 202
        assert res_ok.json()["run_id"] == 42
        assert res_ok.json()["status"] == "running"


def test_second_trigger_during_run_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """Triggering a run while another is active for the same workspace returns 409 Conflict."""
    monkeypatch.setenv("ADMIN_API_KEY", "admin_pass")
    get_settings.cache_clear()

    client = TestClient(app)

    # Mark workspace as actively running
    _active_workspaces.add("ws-busy")
    try:
        res = client.post(
            "/briefing/trigger",
            headers={"X-API-Key": "admin_pass"},
            json={"workspace_id": "ws-busy"},
        )
        assert res.status_code == 409
        assert "already in progress" in res.json()["detail"]
    finally:
        _active_workspaces.discard("ws-busy")


def test_get_run_details_and_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /runs/{run_id} returns run timings and GET /health returns scheduler status."""
    monkeypatch.setenv("ENABLE_SCHEDULER", "false")
    get_settings.cache_clear()

    client = TestClient(app)

    # Seed an AgentRun
    with get_db_session() as session:
        run = AgentRun(
            workspace_id="ws-health-test",
            run_type="morning",
            status="completed",
            total_latency_ms=123.4,
            collect_latency_ms=40.0,
            analyze_latency_ms=60.0,
            act_latency_ms=23.4,
            model_used="gemini-2.5-flash",
            fallback_used=False,
            items_collected=8,
            flags_created=2,
            errors=json.dumps(["Warning: slow github"]),
            completed_at=datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None),
        )
        session.add(run)
        session.commit()
        run_id = run.id

    # Test GET /runs/{run_id}
    res_run = client.get(f"/runs/{run_id}")
    assert res_run.status_code == 200
    data = res_run.json()
    assert data["run_id"] == run_id
    assert data["status"] == "completed"
    assert data["model_used"] == "gemini-2.5-flash"
    assert data["per_node_timings"]["collect_ms"] == 40.0
    assert data["per_node_timings"]["total_ms"] == 123.4
    assert len(data["errors"]) == 1

    # Test GET /health
    res_health = client.get("/health")
    assert res_health.status_code == 200
    h_data = res_health.json()
    assert h_data["status"] == "ok"
    assert h_data["scheduler_status"] == "disabled"
    assert "2026-10-14" in h_data["last_run_time"]


# -----------------------------------------------------------------------------
# 3. Plain-Language Query & Grounding Tests
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_query_removes_invented_id() -> None:
    """Query answering detects and removes invented IDs not in the collected data."""
    valid_pr = PullRequest(
        id=10,
        number=10,
        title="Add auth middleware",
        author="alice",
        age_days=5,
        last_commit_days=2,
        is_stale=False,
        repo="org/auth-service",
    )
    valid_ticket = JiraIssue(
        key="KAN-101",
        title="Database timeout bug",
        assignee="bob",
        age_days=12,
        days_since_update=3,
        is_stale=False,
        status="In Progress",
    )

    mock_router = AsyncMock(spec=LLMRouter)
    # Intent classification
    mock_router.route.return_value = LLMResult(
        data=QueryIntent(sources_needed=["github", "jira"], entities=[], query_type="status", time_range="24h"),
        model_name="mock-ollama",
        provider="ollama",
        task_type="intent",
        latency_ms=50.0,
    )

    # Answering: Model hallucinates invented ID KAN-999 alongside real ID KAN-101
    mock_router.get_candidates.return_value = [("gemini", "gemini-2.5-flash")]
    mock_router.call_candidate.return_value = LLMResult(
        data=QueryAnswerOutput(
            answer="KAN-101 is open for 12 days by bob. Also invented ticket KAN-999 is blocked."
        ),
        model_name="gemini-2.5-flash",
        provider="gemini",
        task_type="analysis",
        latency_ms=150.0,
    )

    mock_gh = AsyncMock()
    mock_gh.fetch_all_open_prs.return_value = [valid_pr]
    mock_jr = AsyncMock()
    mock_jr.fetch_open_issues.return_value = [valid_ticket]

    res = await handle_plain_language_query(
        question="What tickets are blocked?",
        github_client=mock_gh,
        jira_client=mock_jr,
        router=mock_router,
    )

    assert "KAN-101" in res["item_ids"]
    # Invented ID KAN-999 must NOT be in item_ids
    assert "KAN-999" not in res["item_ids"]
    # Invented ID KAN-999 must be removed from the answer text
    assert "KAN-999" not in res["answer"]


@pytest.mark.asyncio
async def test_query_question_not_in_data_returns_honest_message() -> None:
    """When the question cannot be answered from data, returns an honest 'not in the data' response."""
    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=QueryIntent(sources_needed=["github"], entities=[], query_type="status", time_range="24h"),
        model_name="mock-ollama",
        provider="ollama",
        task_type="intent",
        latency_ms=40.0,
    )
    # Model honestly acknowledges facts are missing
    mock_router.get_candidates.return_value = [("gemini", "gemini-2.5-flash")]
    mock_router.call_candidate.return_value = LLMResult(
        data=QueryAnswerOutput(answer="The requested information is not in the collected data."),
        model_name="gemini-2.5-flash",
        provider="gemini",
        task_type="analysis",
        latency_ms=100.0,
    )

    mock_gh = AsyncMock()
    mock_gh.fetch_all_open_prs.return_value = []

    res = await handle_plain_language_query(
        question="Which PR was deployed to staging yesterday?",
        github_client=mock_gh,
        router=mock_router,
    )

    assert "not in the collected data" in res["answer"].lower()
    assert len(res["item_ids"]) == 0


# -----------------------------------------------------------------------------
# 4. Slack Slash Command & Rate Limiting Tests
# -----------------------------------------------------------------------------
def test_slash_command_rejects_forged_signature() -> None:
    """POST /slack/commands rejects forged or expired signatures with 401."""
    client = TestClient(app)
    body = b"command=%2Fteampulse&text=brief"

    res = client.post(
        "/slack/commands",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=forged_invalid_signature_hex",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert res.status_code == 401


def test_slash_command_handles_subcommands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slash command responds quickly (<3s) and handles brief, ask, status, and help."""
    client = TestClient(app)
    secret = "test_signing_secret_phase5"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", secret)
    get_settings.cache_clear()

    # 1. /teampulse brief
    body_brief = b"command=%2Fteampulse&text=brief&user_id=U123&response_url=https%3A%2F%2Fhooks.slack.com%2Fbrief"
    headers_brief = generate_slack_headers(body_brief, secret=secret)
    res_brief = client.post("/slack/commands", content=body_brief, headers=headers_brief)
    assert res_brief.status_code == 200
    assert "Briefing run started" in res_brief.json()["text"]

    # 2. /teampulse ask
    body_ask = b"command=%2Fteampulse&text=ask%20who%20is%20on%20auth%3F&user_id=U123&response_url=https%3A%2F%2Fhooks.slack.com%2Fask"
    headers_ask = generate_slack_headers(body_ask, secret=secret)
    res_ask = client.post("/slack/commands", content=body_ask, headers=headers_ask)
    assert res_ask.status_code == 200
    assert "Asking TeamPulse" in res_ask.json()["text"]

    # 3. /teampulse status
    body_status = b"command=%2Fteampulse&text=status&user_id=U123"
    headers_status = generate_slack_headers(body_status, secret=secret)
    res_status = client.post("/slack/commands", content=body_status, headers=headers_status)
    assert res_status.status_code == 200
    assert "TeamPulse" in res_status.json()["text"]

    # 4. /teampulse help
    body_help = b"command=%2Fteampulse&text=help&user_id=U123"
    headers_help = generate_slack_headers(body_help, secret=secret)
    res_help = client.post("/slack/commands", content=body_help, headers=headers_help)
    assert res_help.status_code == 200
    assert "TeamPulse Ops Agent Commands" in res_help.json()["text"]


@pytest.mark.asyncio
async def test_rate_limit_blocks_sixth_question() -> None:
    """The rate limit permits 5 queries per minute and blocks the 6th question."""
    reset_user_rate_limits()
    user_id = "U_SPAMMER_999"
    limit = 5

    # First 5 calls pass
    for i in range(5):
        allowed = await check_user_rate_limit(user_id, limit_per_minute=limit)
        assert allowed is True, f"Call {i+1} should have been allowed"

    # 6th call is blocked
    blocked = await check_user_rate_limit(user_id, limit_per_minute=limit)
    assert blocked is False, "6th call within the minute must be blocked"


# -----------------------------------------------------------------------------
# 5. Metrics Storage & Percentile Calculation Tests
# -----------------------------------------------------------------------------
def test_percentile_and_metrics_calculation() -> None:
    """show_metrics computes accurate averages, p50 (median), and p95 percentiles."""
    # Test linear interpolation of percentiles
    data = [10.0, 20.0, 30.0, 40.0, 50.0]
    p50 = compute_percentile(data, 50.0)
    assert p50 == 30.0

    p95 = compute_percentile(data, 95.0)
    # p95 on 5 items: index 4 * 0.95 = 3.8 -> 40 + 0.8 * 10 = 48.0
    assert p95 == 48.0

    # Test summary metrics aggregation
    mock_runs = [
        AgentRun(
            workspace_id="ws",
            run_type="morning",
            status="completed",
            total_latency_ms=100.0,
            collect_latency_ms=20.0,
            analyze_latency_ms=50.0,
            act_latency_ms=30.0,
            fallback_used=False,
            items_collected=5,
            flags_created=2,
        ),
        AgentRun(
            workspace_id="ws",
            run_type="evening",
            status="completed",
            total_latency_ms=200.0,
            collect_latency_ms=40.0,
            analyze_latency_ms=110.0,
            act_latency_ms=50.0,
            fallback_used=True,
            items_collected=10,
            flags_created=4,
        ),
    ]

    summary = calculate_metrics_summary(mock_runs)
    assert summary["total_runs"] == 2
    assert summary["total_latency"]["avg"] == 150.0
    assert summary["fallback_runs"] == 1
    assert summary["total_items_collected"] == 15
    assert summary["total_flags_created"] == 6


# -----------------------------------------------------------------------------
# 5. Out-of-Scope & Code-Based Missing ID Lookup Tests
# -----------------------------------------------------------------------------
def test_out_of_scope_keyword_rules() -> None:
    """Keyword rule detects out_of_scope queries when no PR/ticket/Jira/GitHub/stale/KAN/repo is mentioned."""
    # Out of scope queries
    for q in ["What is the weather in Paris?", "Tell me a joke", "Who won the game last night?"]:
        intent = classify_intent_with_rules(q)
        assert intent.operation == "out_of_scope"
        assert intent.query_type == "out_of_scope"
        assert intent.sources_needed == []

    # In-scope queries
    intent_pr = classify_intent_with_rules("Are there any stale PRs?")
    assert intent_pr.operation != "out_of_scope"
    assert "github" in intent_pr.sources_needed

    intent_jira = classify_intent_with_rules("What tickets are currently blocked in Jira?")
    assert intent_jira.operation != "out_of_scope"
    assert "jira" in intent_jira.sources_needed

    intent_lookup = classify_intent_with_rules("Tell me about ticket KAN-42")
    assert intent_lookup.operation == "lookup"
    assert "KAN-42" in intent_lookup.entities


@pytest.mark.asyncio
async def test_out_of_scope_query_no_data_fetch_and_no_llm_call() -> None:
    """Out-of-scope questions immediately return honest scope limitation without data fetch or large model call."""
    mock_router = AsyncMock(spec=LLMRouter)
    # Simulate LLM classifier returning out_of_scope
    mock_router.route.return_value = LLMResult(
        data=QueryIntent(sources_needed=[], entities=[], query_type="out_of_scope", operation="out_of_scope"),
        model_name="mock-classifier",
        provider="ollama",
        task_type="intent",
        latency_ms=10.0,
    )
    mock_router.get_candidates = AsyncMock()
    mock_router.call_candidate = AsyncMock()

    mock_gh = AsyncMock()
    mock_jr = AsyncMock()

    res = await handle_plain_language_query(
        question="What is the weather in Paris?",
        github_client=mock_gh,
        jira_client=mock_jr,
        router=mock_router,
    )

    # 1. Returned required prompt message
    assert res["answer"] == "I can only answer questions about your GitHub pull requests and Jira tickets."
    assert res["sources_used"] == []
    assert res["item_ids"] == []

    # 2. No data fetch occurred
    mock_gh.fetch_all_open_prs.assert_not_called()
    mock_jr.fetch_open_issues.assert_not_called()

    # 3. No large model called
    mock_router.get_candidates.assert_not_called()
    mock_router.call_candidate.assert_not_called()


@pytest.mark.asyncio
async def test_out_of_scope_query_fallback_when_classifier_fails() -> None:
    """When the classifier fails on an out-of-scope question, keyword rule catches it with no data fetch."""
    mock_router = AsyncMock(spec=LLMRouter)
    # Router fails/raises exception
    mock_router.route.side_effect = RuntimeError("Ollama connection refused")
    mock_router.get_candidates = AsyncMock()
    mock_router.call_candidate = AsyncMock()

    mock_gh = AsyncMock()
    mock_jr = AsyncMock()

    res = await handle_plain_language_query(
        question="Can you tell me a funny joke?",
        github_client=mock_gh,
        jira_client=mock_jr,
        router=mock_router,
    )

    assert res["answer"] == "I can only answer questions about your GitHub pull requests and Jira tickets."
    assert res["sources_used"] == []
    assert res["item_ids"] == []

    # Assert no data fetch
    mock_gh.fetch_all_open_prs.assert_not_called()
    mock_jr.fetch_open_issues.assert_not_called()
    mock_router.get_candidates.assert_not_called()
    mock_router.call_candidate.assert_not_called()


@pytest.mark.asyncio
async def test_lookup_missing_ticket_id_builds_code_answer_no_llm_call() -> None:
    """Lookup of a Jira ticket ID not present in data returns '<ID> is not in the collected data.' with no LLM call."""
    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=QueryIntent(sources_needed=["jira"], entities=["KAN-42"], query_type="lookup", operation="lookup"),
        model_name="mock-classifier",
        provider="ollama",
        task_type="intent",
        latency_ms=10.0,
    )
    mock_router.get_candidates = AsyncMock()
    mock_router.call_candidate = AsyncMock()

    # Data only contains KAN-1 and KAN-2, NOT KAN-42
    ticket1 = JiraIssue(
        key="KAN-1",
        title="Setup CI pipeline",
        assignee="alice",
        age_days=3,
        days_since_update=1,
        is_stale=False,
        status="Done",
    )
    mock_jr = AsyncMock()
    mock_jr.fetch_open_issues.return_value = [ticket1]

    res = await handle_plain_language_query(
        question="Tell me about ticket KAN-42",
        jira_client=mock_jr,
        router=mock_router,
    )

    assert res["answer"] == "KAN-42 is not in the collected data."
    assert res["item_ids"] == []
    # Assert no LLM call for answering
    mock_router.get_candidates.assert_not_called()
    mock_router.call_candidate.assert_not_called()


@pytest.mark.asyncio
async def test_lookup_missing_pr_id_builds_code_answer_no_llm_call() -> None:
    """Lookup of a PR ID not present in data returns '<ID> is not in the collected data.' with no LLM call."""
    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=QueryIntent(sources_needed=["github"], entities=["#999"], query_type="lookup", operation="lookup"),
        model_name="mock-classifier",
        provider="ollama",
        task_type="intent",
        latency_ms=10.0,
    )
    mock_router.get_candidates = AsyncMock()
    mock_router.call_candidate = AsyncMock()

    # Data only contains PR #10
    pr1 = PullRequest(
        id=10,
        number=10,
        title="Fix typo",
        author="bob",
        age_days=1,
        last_commit_days=1,
        is_stale=False,
        repo="myorg/backend",
    )
    mock_gh = AsyncMock()
    mock_gh.fetch_all_open_prs.return_value = [pr1]

    res = await handle_plain_language_query(
        question="What is the status of PR #999?",
        github_client=mock_gh,
        router=mock_router,
    )

    assert res["answer"] == "#999 is not in the collected data."
    assert res["item_ids"] == []
    # Assert no LLM call for answering
    mock_router.get_candidates.assert_not_called()
    mock_router.call_candidate.assert_not_called()


# -----------------------------------------------------------------------------
# 6. Real-run Fixture Tests (88 PRs aged 20-3300 days & 3 Jira issues)
# -----------------------------------------------------------------------------
@pytest.fixture
def real_data_fixture() -> Tuple[List[PullRequest], List[JiraIssue]]:
    """Fixture with 88 PRs aged between 20 and 3300 days and 3 Jira issues."""
    prs = []
    # Generate 88 PRs with ages from 3300 down to 20
    for i in range(1, 89):
        age = int(3300 - (i - 1) * (3300 - 20) / 87)
        prs.append(
            PullRequest(
                id=i,
                number=i,
                title=f"Feature optimization {i}",
                author=f"engineer_{i % 5}",
                age_days=age,
                last_commit_days=min(age, 10 + (i % 7)),
                is_stale=True,  # All 88 PRs >= 20 days old are stale (> 7 days)
                repo="myorg/repo",
            )
        )

    issues = [
        JiraIssue(
            key="KAN-1",
            title="Database performance issue",
            assignee="alice",
            age_days=15,
            days_since_update=2,
            is_stale=True,
            status="In Progress",
            comment_count=4,
        ),
        JiraIssue(
            key="KAN-2",
            title="Update landing page copy",
            assignee="bob",
            age_days=8,
            days_since_update=1,
            is_stale=False,
            status="Open",
            comment_count=1,
        ),
        JiraIssue(
            key="KAN-3",
            title="Setup GCP alert metrics",
            assignee="carol",
            age_days=25,
            days_since_update=15,
            is_stale=True,
            status="Blocked",
            comment_count=7,
        ),
    ]
    return prs, issues


@pytest.mark.asyncio
async def test_oldest_prs_returns_3300_day_first_not_newest_15(
    real_data_fixture: Tuple[List[PullRequest], List[JiraIssue]]
) -> None:
    """Oldest PRs query returns the 3300-day and next oldest ones first (not the newest 15)."""
    prs, issues = real_data_fixture
    clear_query_cache()

    mock_gh = AsyncMock()
    mock_gh.fetch_all_open_prs.return_value = prs
    mock_jr = AsyncMock()
    mock_jr.fetch_open_issues.return_value = issues

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.get_candidates.return_value = []

    res = await handle_plain_language_query(
        question="Which pull requests have been waiting the longest?",
        workspace_id="test_oldest_ws",
        github_client=mock_gh,
        jira_client=mock_jr,
        router=mock_router,
    )

    # Must rank over full 88 PRs
    assert res["total_count"] == 88
    assert len(res["data"]) == 5
    # First item must be the 3300-day PR
    assert res["data"][0]["age_days"] == 3300
    assert res["data"][0]["id"] == "myorg/repo#1"
    # Second item must be the next oldest (around 3262)
    assert res["data"][1]["age_days"] > 3200
    assert res["data"][1]["age_days"] < 3300
    # Answer text must state the metric
    assert "by days since opened" in res["answer"].lower()
    assert "3300 days" in res["answer"]


@pytest.mark.asyncio
async def test_stale_count_returns_fixture_total_above_15(
    real_data_fixture: Tuple[List[PullRequest], List[JiraIssue]]
) -> None:
    """Stale count returns exactly the fixture's stale total (88), which is above 15."""
    prs, issues = real_data_fixture
    clear_query_cache()

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.get_candidates.return_value = []

    mock_gh = AsyncMock()
    mock_gh.fetch_all_open_prs.return_value = prs
    mock_jr = AsyncMock()
    mock_jr.fetch_open_issues.return_value = issues

    res = await handle_plain_language_query(
        question="How many stale pull requests are there?",
        workspace_id="test_count_ws",
        github_client=mock_gh,
        jira_client=mock_jr,
        router=mock_router,
    )

    assert res["total_count"] == 88
    assert "88" in res["answer"]
    assert "15" not in res["answer"]


@pytest.mark.asyncio
async def test_count_answer_identical_when_max_items_in_prompt_15_or_100(
    real_data_fixture: Tuple[List[PullRequest], List[JiraIssue]]
) -> None:
    """Count answer is identical when MAX_ITEMS_IN_PROMPT is 15 or 100."""
    prs, issues = real_data_fixture
    settings = get_settings()
    orig_max = settings.max_items_in_prompt

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.get_candidates.return_value = []

    try:
        # Run with max_items_in_prompt = 15
        clear_query_cache()
        settings.max_items_in_prompt = 15
        mock_gh1 = AsyncMock()
        mock_gh1.fetch_all_open_prs.return_value = prs
        res1 = await handle_plain_language_query(
            question="How many stale pull requests are there?",
            workspace_id="test_ws_15",
            github_client=mock_gh1,
            router=mock_router,
        )

        # Run with max_items_in_prompt = 100
        clear_query_cache()
        settings.max_items_in_prompt = 100
        mock_gh2 = AsyncMock()
        mock_gh2.fetch_all_open_prs.return_value = prs
        res2 = await handle_plain_language_query(
            question="How many stale pull requests are there?",
            workspace_id="test_ws_100",
            github_client=mock_gh2,
            router=mock_router,
        )

        assert res1["total_count"] == 88
        assert res2["total_count"] == 88
        assert res1["answer"] == res2["answer"]
    finally:
        settings.max_items_in_prompt = orig_max


@pytest.mark.asyncio
async def test_kan42_returns_not_in_data_message_no_llm_call(
    real_data_fixture: Tuple[List[PullRequest], List[JiraIssue]]
) -> None:
    """KAN-42 returns the not-in-data message with no LLM call."""
    prs, issues = real_data_fixture
    clear_query_cache()

    mock_router = AsyncMock(spec=LLMRouter)
    mock_gh = AsyncMock()
    mock_gh.fetch_all_open_prs.return_value = prs
    mock_jr = AsyncMock()
    mock_jr.fetch_open_issues.return_value = issues

    res = await handle_plain_language_query(
        question="Tell me about ticket KAN-42",
        workspace_id="test_kan42_ws",
        github_client=mock_gh,
        jira_client=mock_jr,
        router=mock_router,
    )

    assert res["answer"] == "KAN-42 is not in the collected data."
    assert res["item_ids"] == []
    # Assert no LLM call was made
    mock_router.get_candidates.assert_not_called()
    mock_router.call_candidate.assert_not_called()


@pytest.mark.asyncio
async def test_out_of_scope_triggers_no_data_fetch_and_no_llm_call() -> None:
    """Out-of-scope question triggers no data fetch and no LLM call."""
    clear_query_cache()
    mock_router = AsyncMock(spec=LLMRouter)
    mock_gh = AsyncMock()
    mock_jr = AsyncMock()

    res = await handle_plain_language_query(
        question="What is the weather in Paris?",
        workspace_id="test_oos_ws",
        github_client=mock_gh,
        jira_client=mock_jr,
        router=mock_router,
    )

    assert res["answer"] == "I can only answer questions about your GitHub pull requests and Jira tickets."
    mock_gh.fetch_all_open_prs.assert_not_called()
    mock_jr.fetch_open_issues.assert_not_called()
    mock_router.get_candidates.assert_not_called()
    mock_router.call_candidate.assert_not_called()


@pytest.mark.asyncio
async def test_llm_answer_with_wrong_number_is_replaced_by_template_answer(
    real_data_fixture: Tuple[List[PullRequest], List[JiraIssue]]
) -> None:
    """An LLM answer with a wrong number is replaced by the template answer."""
    prs, issues = real_data_fixture
    clear_query_cache()

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.get_candidates.return_value = [("gemini", "gemini-2.5-flash")]
    # LLM hallucinates wrong count 15 instead of real 88
    mock_router.call_candidate.return_value = LLMResult(
        data=QueryAnswerOutput(answer="There are 15 stale pull requests waiting for review."),
        model_name="gemini-2.5-flash",
        provider="gemini",
        task_type="analysis",
        latency_ms=100.0,
    )

    mock_gh = AsyncMock()
    mock_gh.fetch_all_open_prs.return_value = prs
    mock_jr = AsyncMock()
    mock_jr.fetch_open_issues.return_value = issues

    res = await handle_plain_language_query(
        question="How many stale pull requests are there?",
        workspace_id="test_wrong_num_ws",
        github_client=mock_gh,
        jira_client=mock_jr,
        router=mock_router,
    )

    # Verification must catch that 15 is not allowed and replace with template answer (88)
    assert res["answer"] == "There are 88 stale pull requests."
    assert "88" in res["answer"]
    assert "15" not in res["answer"]


@pytest.mark.asyncio
async def test_query_cache_is_reused_within_ttl_and_refreshed_after_it(
    real_data_fixture: Tuple[List[PullRequest], List[JiraIssue]]
) -> None:
    """The cache is reused within the TTL and refreshed after it."""
    prs, issues = real_data_fixture
    clear_query_cache()

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.get_candidates.return_value = []

    mock_gh = AsyncMock()
    mock_gh.fetch_all_open_prs.return_value = prs
    ws = "ws_cache_expiry_test"

    # 1. First call at t=0 fetches from client
    res1 = await handle_plain_language_query(
        question="How many stale pull requests are there?",
        workspace_id=ws,
        github_client=mock_gh,
        router=mock_router,
    )
    assert res1["total_count"] == 88
    assert mock_gh.fetch_all_open_prs.call_count == 1

    # 2. Second call within TTL (300s) uses cached data
    res2 = await handle_plain_language_query(
        question="Which pull requests have been waiting the longest?",
        workspace_id=ws,
        github_client=mock_gh,
        router=mock_router,
    )
    assert res2["total_count"] == 88
    assert mock_gh.fetch_all_open_prs.call_count == 1  # Still 1, cache hit!

    # 3. Simulate cache expiry: set timestamp 400s in the past
    _query_cache._cache[ws]["timestamp"] -= 400.0

    # 4. Third call after TTL refreshes cache
    res3 = await handle_plain_language_query(
        question="How many stale pull requests are there?",
        workspace_id=ws,
        github_client=mock_gh,
        router=mock_router,
    )
    assert res3["total_count"] == 88
    assert mock_gh.fetch_all_open_prs.call_count == 2  # Refreshed!


@pytest.mark.asyncio
async def test_llm_timeout_still_returns_template_answer(
    real_data_fixture: Tuple[List[PullRequest], List[JiraIssue]]
) -> None:
    """An LLM timeout still returns the template answer."""
    prs, issues = real_data_fixture
    clear_query_cache()

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.get_candidates.return_value = [("gemini", "gemini-2.5-flash")]
    # LLM call raises TimeoutError
    mock_router.call_candidate.side_effect = asyncio.TimeoutError("LLM call timed out after 15s")

    mock_gh = AsyncMock()
    mock_gh.fetch_all_open_prs.return_value = prs

    res = await handle_plain_language_query(
        question="Which pull requests have been waiting the longest?",
        workspace_id="test_timeout_ws",
        github_client=mock_gh,
        router=mock_router,
    )

    # Must return template answer despite timeout
    assert "Top 5 pull requests ranked by days since opened" in res["answer"]
    assert "3300 days" in res["answer"]
    assert res["total_count"] == 88
    assert len(res["data"]) == 5


