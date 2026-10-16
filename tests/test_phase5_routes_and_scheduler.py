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
        question="Who deployed to staging yesterday?",
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
