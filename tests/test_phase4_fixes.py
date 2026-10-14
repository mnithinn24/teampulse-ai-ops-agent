"""Tests for Phase 4 real run fixes:
1. reset_db refuses non-local URLs.
2. schema drift is detected.
3. the same flag is not saved twice within 24h.
4. a 503 error goes to fallback model without re-prompt.
5. invalid JSON triggers exactly one re-prompt.
6. commit lookups are capped at MAX_COMMIT_LOOKUPS (15) and run concurrently with Semaphore(5).
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select, text

from app.agents.act import act_on_workspace
from app.agents.analyze import analyze_workspace
from app.core.constants import (
    FLAG_LEVEL_NORMAL,
    ITEM_TYPE_GITHUB_PR,
    ITEM_TYPE_JIRA_ISSUE,
    STATUS_PENDING,
)
from app.core.errors import AnalysisError, InvalidJSONError
from app.db.database import check_schema_drift, get_db_session, get_engine, init_db
from app.db.models import Base, Flag
from app.graph.state import TeamPulseState
from app.integrations.github_client import GitHubClient, PullRequest
from app.integrations.jira_client import JiraIssue
from app.llm.router import LLMResult, LLMRouter
from app.llm.schemas import Analysis, FlagEvidence, PriorityFlag
from scripts.reset_db import is_local_database_url, reset_database


# -------------------------------------------------------------------------
# Test 1: reset_db refuses non-local URL and missing --yes flag
# -------------------------------------------------------------------------
def test_reset_db_refuses_non_local_url() -> None:
    """reset_db must refuse to drop/recreate tables if DATABASE_URL is not localhost or sqlite."""
    # Test refusal without --yes flag
    code_no_force = reset_database(force=False, database_url="sqlite:///:memory:")
    assert code_no_force == 1

    # Test refusal for remote MySQL host
    code_remote = reset_database(force=True, database_url="mysql+pymysql://root:secret@prod-db.cloud.corp:3306/teampulse")
    assert code_remote == 1

    # Test refusal for remote Postgres host
    code_pg = reset_database(force=True, database_url="postgresql://user:pass@192.168.1.100:5432/db")
    assert code_pg == 1

    # Helper function checks
    assert is_local_database_url("sqlite:///:memory:") is True
    assert is_local_database_url("sqlite:///app.db") is True
    assert is_local_database_url("mysql+pymysql://user:pass@localhost:3306/db") is True
    assert is_local_database_url("mysql+pymysql://user:pass@127.0.0.1:3306/db") is True
    assert is_local_database_url("mysql+pymysql://user:pass@remote.domain.com:3306/db") is False


# -------------------------------------------------------------------------
# Test 2: Schema drift is detected
# -------------------------------------------------------------------------
def test_schema_drift_is_detected(caplog: pytest.LogCaptureLogger) -> None:
    """When a table is missing expected model columns, check_schema_drift logs the specific error."""
    # Create an in-memory SQLite database
    engine = create_engine("sqlite:///:memory:")

    # Create the flags table manually missing several columns (e.g. only id and workspace_id)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE flags ("
                "id INTEGER PRIMARY KEY, "
                "workspace_id VARCHAR(64)"
                ")"
            )
        )

    # Check schema drift
    with caplog.at_level(logging.ERROR):
        is_clean = check_schema_drift(engine)

    assert is_clean is False
    assert "Schema out of date. Run scripts/reset_db.py" in caplog.text


def test_schema_drift_clean_when_all_columns_present() -> None:
    """When tables have all columns matching models, check_schema_drift returns True."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    assert check_schema_drift(engine) is True


# -------------------------------------------------------------------------
# Test 3: The same flag is not saved twice within 24h
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_same_flag_is_not_saved_twice_within_24h() -> None:
    """Act node prevents creating duplicate pending flags for canonical item_id within 24 hours."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    mock_slack = AsyncMock()
    mock_slack.post_message = AsyncMock(return_value={"ok": True, "ts": "12345.67", "channel": "C123"})

    with get_db_session(engine) as session:
        state = TeamPulseState(
            workspace_id="ws-dedup-test",
            priority_items=[
                {
                    "item_id": "owner/repo#42",
                    "title": "Stale PR: Add Auth",
                    "reason": "Inactive for 15 days",
                    "urgency": "urgent",
                    "suggested_action": "Ping reviewers",
                    "evidence": {"age_days": 15, "author_or_assignee": "alice"},
                }
            ],
        )

        # First run saves the pending flag
        res1 = await act_on_workspace(state, slack_client=mock_slack, session=session)
        assert res1["briefing"]["flags_saved"] == 1

        flags_after_first = session.scalars(
            select(Flag).where(
                Flag.workspace_id == "ws-dedup-test",
                Flag.item_id == "owner/repo#42",
                Flag.status == STATUS_PENDING,
            )
        ).all()
        assert len(flags_after_first) == 1
        initial_flag_id = flags_after_first[0].id

        # Second run with the same canonical item within 24 hours
        res2 = await act_on_workspace(state, slack_client=mock_slack, session=session)
        assert res2["briefing"]["flags_saved"] == 1

        # Must still have only ONE pending flag in the database (upserted, not duplicated)
        flags_after_second = session.scalars(
            select(Flag).where(
                Flag.workspace_id == "ws-dedup-test",
                Flag.item_id == "owner/repo#42",
                Flag.status == STATUS_PENDING,
            )
        ).all()
        assert len(flags_after_second) == 1
        assert flags_after_second[0].id == initial_flag_id


# -------------------------------------------------------------------------
# Test 4: A 503 goes to fallback model without a re-prompt
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_503_goes_to_fallback_model_without_reprompt() -> None:
    """When a provider call fails with 503, advance directly to fallback model without re-prompt."""
    valid_analysis = Analysis(
        patterns=["Pattern from fallback"],
        priority_flags=[
            PriorityFlag(
                title="Stale PR",
                reason="Inactive 10d by alice",
                urgency="watch",
                source=ITEM_TYPE_GITHUB_PR,
                item_id="owner/repo#10",
                suggested_action="Review",
                evidence=FlagEvidence(age_days=10, author_or_assignee="alice"),
            )
        ],
        velocity_note="Fallback velocity",
        one_recommendation="Unblock owner/repo#10",
    )

    pr = PullRequest(
        id=10,
        number=10,
        title="Stale PR",
        author="alice",
        age_days=10,
        last_commit_days=10,
        is_stale=True,
        repo="owner/repo",
    )
    state = TeamPulseState(workspace_id="ws-503-test", github_data=[pr])

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.get_candidates.return_value = [
        ("gemini", "gemini-2.5-flash"),
        ("gemini_fallback", "gemini-2.0-flash"),
    ]

    # Candidate 1 (Gemini main) fails with 503 error
    # Candidate 2 (Gemini fallback) succeeds
    mock_router.call_candidate.side_effect = [
        AnalysisError("503 Service Unavailable"),
        LLMResult(
            data=valid_analysis,
            model_name="gemini-2.0-flash",
            provider="gemini_fallback",
            task_type="analysis",
            latency_ms=120.0,
        ),
    ]

    analysis, model_used, latency = await analyze_workspace(state, router=mock_router)

    # Candidate 1 was called only once (NO re-prompt on 503), then candidate 2 was called
    assert mock_router.call_candidate.call_count == 2
    first_call_args = mock_router.call_candidate.call_args_list[0]
    second_call_args = mock_router.call_candidate.call_args_list[1]

    assert first_call_args.kwargs["model_name"] == "gemini-2.5-flash"
    assert second_call_args.kwargs["model_name"] == "gemini-2.0-flash"
    assert model_used == "gemini-2.0-flash"
    assert "CRITICAL RE-PROMPT INSTRUCTION" not in second_call_args.kwargs["prompt"]


# -------------------------------------------------------------------------
# Test 5: Invalid JSON triggers exactly one re-prompt
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invalid_json_triggers_exactly_one_reprompt() -> None:
    """When a candidate returns invalid JSON, re-prompt that candidate with schema reminder once."""
    valid_analysis = Analysis(
        patterns=["Recovered from invalid JSON"],
        priority_flags=[
            PriorityFlag(
                title="Stale PR",
                reason="Inactive 10d by alice",
                urgency="watch",
                source=ITEM_TYPE_GITHUB_PR,
                item_id="owner/repo#10",
                suggested_action="Review",
                evidence=FlagEvidence(age_days=10, author_or_assignee="alice"),
            )
        ],
        velocity_note="Recovered velocity",
        one_recommendation="Unblock owner/repo#10",
    )

    pr = PullRequest(
        id=10,
        number=10,
        title="Stale PR",
        author="alice",
        age_days=10,
        last_commit_days=10,
        is_stale=True,
        repo="owner/repo",
    )
    state = TeamPulseState(workspace_id="ws-invalid-json-test", github_data=[pr])

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.get_candidates.return_value = [
        ("gemini", "gemini-2.5-flash"),
        ("gemini_fallback", "gemini-2.0-flash"),
    ]

    # First call to Candidate 1 returns invalid JSON; second call (re-prompt) succeeds
    mock_router.call_candidate.side_effect = [
        InvalidJSONError("Invalid JSON structure returned by model"),
        LLMResult(
            data=valid_analysis,
            model_name="gemini-2.5-flash",
            provider="gemini",
            task_type="analysis",
            latency_ms=150.0,
        ),
    ]

    analysis, model_used, latency = await analyze_workspace(state, router=mock_router)

    # Exactly 2 calls to Candidate 1 (first attempt + 1 re-prompt)
    assert mock_router.call_candidate.call_count == 2
    first_call_args = mock_router.call_candidate.call_args_list[0]
    second_call_args = mock_router.call_candidate.call_args_list[1]

    # Both calls went to candidate 1
    assert first_call_args.kwargs["model_name"] == "gemini-2.5-flash"
    assert second_call_args.kwargs["model_name"] == "gemini-2.5-flash"

    # The second call contains the re-prompt schema reminder
    assert "CRITICAL RE-PROMPT INSTRUCTION" in second_call_args.kwargs["prompt"]
    assert model_used == "gemini-2.5-flash"


# -------------------------------------------------------------------------
# Test 6: Commit lookups are capped at MAX_COMMIT_LOOKUPS (15) and concurrent
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_commit_lookups_capped_at_15_and_concurrent() -> None:
    """Fetch PR commit details only for MAX_COMMIT_LOOKUPS oldest stale PRs using Semaphore(5)."""
    now = datetime.now(timezone.utc)

    # Create 25 open pull requests all older than stale threshold (7 days)
    mock_pulls = []
    for i in range(1, 26):
        # PR 1 is 35 days old, PR 25 is 11 days old
        created = (now - timedelta(days=35 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        mock_pulls.append({
            "id": i,
            "number": i,
            "title": f"PR {i}",
            "user": {"login": f"user{i}"},
            "created_at": created,
            "updated_at": created,
            "requested_reviewers": [],
            "commits_url": f"https://api.github.com/repos/owner/repo/pulls/{i}/commits",
            "html_url": f"https://github.com/owner/repo/pull/{i}",
        })

    client = GitHubClient(token="ghp_test_token", max_commit_lookups=15, stale_pr_days=7)

    commit_fetch_urls = []
    active_concurrent_calls = 0
    max_observed_concurrency = 0

    async def mock_request(endpoint: str, params=None):
        nonlocal active_concurrent_calls, max_observed_concurrency
        if "/pulls" in endpoint and "/commits" not in endpoint:
            # PR listing call
            return mock_pulls

        if "/commits" in endpoint:
            # Commit lookup call: record URL and test concurrency
            commit_fetch_urls.append(endpoint)
            active_concurrent_calls += 1
            if active_concurrent_calls > max_observed_concurrency:
                max_observed_concurrency = active_concurrent_calls

            # Simulate brief async I/O
            await asyncio.sleep(0.01)
            active_concurrent_calls -= 1

            return [
                {
                    "commit": {
                        "committer": {
                            "date": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        }
                    }
                }
            ]
        return []

    with patch.object(client, "_request", side_effect=mock_request):
        prs = await client.fetch_open_prs("owner/repo")

    assert len(prs) == 25

    # Commit details must be fetched for EXACTLY 15 PRs (the oldest ones)
    assert len(commit_fetch_urls) == 15

    # The 15 oldest PRs are PR 1 through PR 15 (age 34 down to 20 days)
    expected_fetched_numbers = {f"/pulls/{i}/commits" for i in range(1, 16)}
    for fetch_url in commit_fetch_urls:
        assert any(sub in fetch_url for sub in expected_fetched_numbers)

    # Younger PRs (PR 16 through 25) must NOT have had commit lookups
    for i in range(16, 26):
        assert not any(f"/pulls/{i}/commits" in fetch_url for fetch_url in commit_fetch_urls)

    # Concurrency was bounded by asyncio.Semaphore(5)
    assert max_observed_concurrency <= 5
    assert max_observed_concurrency > 1
