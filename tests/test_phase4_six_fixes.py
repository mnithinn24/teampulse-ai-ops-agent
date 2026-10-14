"""Tests verifying the 6 fixes for Phase 4:
1. Flags count as 'already flagged' only after being posted to Slack with slack_ts.
   Unposted/failed flags are deleted or not deduplicated; posted flags are deduplicated.
2. TIMEZONE setting applies to briefing date and displayed times.
3. Reason text never contains numbers the model wrote. Built from data evidence, LLM text digits stripped.
4. actions_taken.status is 'dry_run' when DRY_RUN_ACTIONS is true, and messages say '[DRY RUN] would create a Jira issue'.
5. unfurl_links=False and unfurl_media=False are sent in Slack calls.
6. Gemini failover makes only 2 attempts (1 retry) on main model for 503/429 when fallback model is configured.
"""

from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy import select

from app.agents.act import act_on_workspace
from app.agents.actions import execute_approved_action
from app.agents.analyze import analyze_workspace, get_recent_flags
from app.agents.briefing import build_slack_briefing
from app.api.routes.slack import process_slack_interaction
from app.config import get_settings
from app.core.constants import ITEM_TYPE_GITHUB_PR, ITEM_TYPE_JIRA_ISSUE, STATUS_PENDING
from app.core.errors import AnalysisError
from app.db.database import get_db_session, get_session_factory
from app.db.models import ActionTaken, Base, Flag
from app.graph.state import TeamPulseState
from app.integrations.github_client import PullRequest
from app.integrations.jira_client import JiraClient, JiraIssue
from app.integrations.slack_client import SlackClient
from app.llm.gemini_client import GeminiClient
from app.llm.router import LLMResult, LLMRouter
from app.llm.schemas import Analysis, FlagEvidence, PriorityFlag


# -----------------------------------------------------------------------------
# Fix 1: Flags only count as already flagged after posted to Slack
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flags_not_deduplicated_when_slack_post_failed() -> None:
    """When Slack post fails, flags are not recorded with slack_ts and are deleted or unposted."""
    mock_slack = AsyncMock(spec=SlackClient)
    mock_slack.post_message.side_effect = Exception("Slack network failure")

    state = TeamPulseState(
        workspace_id="ws-fail-test",
        priority_items=[
            {
                "item_id": "owner/repo#100",
                "title": "Stale Auth PR",
                "reason": "Inactive 10 days",
                "urgency": "watch",
                "suggested_action": "Review",
                "evidence": {"age_days": 10, "author_or_assignee": "bob"},
            }
        ],
    )

    with get_db_session() as session:
        # Act node attempts posting, but Slack fails
        await act_on_workspace(state, slack_client=mock_slack, session=session)

        # Flag created in this run should be deleted since post failed
        flags = session.scalars(
            select(Flag).where(Flag.workspace_id == "ws-fail-test", Flag.item_id == "owner/repo#100")
        ).all()
        assert len(flags) == 0

    # Query recent flags for dedup -> must be empty
    recent = get_recent_flags("ws-fail-test")
    assert not any(f.item_id == "owner/repo#100" for f in recent)


@pytest.mark.asyncio
async def test_flags_deduplicated_after_successful_slack_post() -> None:
    """After a successful post to Slack, slack_ts is written on flags and dedup query finds them."""
    mock_slack = AsyncMock(spec=SlackClient)
    mock_slack.post_message.return_value = {
        "ok": True,
        "channel": "C12345",
        "ts": "1710000000.123456",
    }

    state = TeamPulseState(
        workspace_id="ws-success-test",
        priority_items=[
            {
                "item_id": "owner/repo#200",
                "title": "Stale Feature PR",
                "reason": "Inactive 12 days",
                "urgency": "watch",
                "suggested_action": "Review",
                "evidence": {"age_days": 12, "author_or_assignee": "alice"},
            }
        ],
    )

    with get_db_session() as session:
        # Act node successfully posts
        res = await act_on_workspace(state, slack_client=mock_slack, session=session)
        assert res["briefing"]["flags_saved"] == 1

        flag = session.scalars(
            select(Flag).where(Flag.workspace_id == "ws-success-test", Flag.item_id == "owner/repo#200")
        ).first()
        assert flag is not None
        assert flag.slack_channel == "C12345"
        assert flag.slack_ts == "1710000000.123456"

    # Dedup query in analyze must now include this flag
    recent = get_recent_flags("ws-success-test")
    assert any(f.item_id == "owner/repo#200" for f in recent)


# -----------------------------------------------------------------------------
# Fix 2: Timezone setting applies to briefing date and displayed times
# -----------------------------------------------------------------------------
def test_timezone_applied_to_briefing_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """Briefing date is rendered in the configured TIMEZONE."""
    # UTC: 2026-10-14 20:00:00
    # In Asia/Kolkata (+05:30), this is 2026-10-15 01:30:00 (Thursday)
    monkeypatch.setenv("TIMEZONE", "Asia/Kolkata")
    get_settings.cache_clear()

    utc_dt = datetime(2026, 10, 14, 20, 0, 0, tzinfo=timezone.utc)
    state = TeamPulseState(workspace_id="ws-tz")

    briefing = build_slack_briefing(state, flags_with_db=[], now=utc_dt)
    # The header must reflect Thursday, October 15, 2026 in Asia/Kolkata
    assert "Thursday, October 15, 2026" in briefing["blocks"][0]["text"]["text"]
    assert "Thursday, October 15, 2026" in briefing["text"]


# -----------------------------------------------------------------------------
# Fix 3: LLM reason with numbers replaced with data-based text
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_llm_reason_with_wrong_numbers_replaced_with_data_based_text() -> None:
    """Reason text never contains numbers hallucinated by the model; uses facts from collected data."""
    pr = PullRequest(
        id=42,
        number=42,
        title="Payment Service Migration",
        author="carol",
        age_days=8,
        last_commit_days=3,
        reviewers=["dave"],
        is_stale=True,
        repo="acme/payments",
    )
    state = TeamPulseState(workspace_id="ws-ground-reason", github_data=[pr])

    # LLM hallucinates wrong numbers: 99 days old, 50 commits, 42 reviews
    hallucinated_flag = PriorityFlag(
        title="Payment Service Migration",
        reason="Open for 99 days with 50 commits and 42 reviews, blocking deployment",
        urgency="watch",
        source=ITEM_TYPE_GITHUB_PR,
        item_id="acme/payments#42",
        suggested_action="Review and merge",
        evidence=FlagEvidence(age_days=8, author_or_assignee="carol"),
    )

    llm_analysis = Analysis(
        patterns=["Migration pattern"],
        priority_flags=[hallucinated_flag],
        velocity_note="Velocity steady",
        one_recommendation="Unblock acme/payments#42",
    )

    mock_router = AsyncMock(spec=LLMRouter)
    mock_router.route.return_value = LLMResult(
        data=llm_analysis,
        model_name="test-model",
        provider="gemini",
        task_type="analysis",
        latency_ms=100.0,
    )

    analysis, _, _ = await analyze_workspace(state, router=mock_router)

    assert len(analysis.priority_flags) == 1
    flag = analysis.priority_flags[0]

    # Real data numbers: 8 days age, 3 days last activity
    assert "8 days" in flag.reason
    assert "3 days" in flag.reason
    assert "reviewers: dave" in flag.reason
    # Hallucinated numbers (99, 50, 42) must NOT exist in reason
    assert "99" not in flag.reason
    assert "50" not in flag.reason
    assert "42" not in flag.reason


# -----------------------------------------------------------------------------
# Fix 4: Dry run is logged as dry_run and result text matches specification
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dry_run_is_logged_as_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """When DRY_RUN_ACTIONS is true, actions_taken status is 'dry_run' and message includes [DRY RUN]."""
    monkeypatch.setenv("DRY_RUN_ACTIONS", "true")
    get_settings.cache_clear()

    with get_db_session() as session:
        flag = Flag(
            workspace_id="ws-dry-run",
            item_type=ITEM_TYPE_GITHUB_PR,
            item_id="acme/api#15",
            title="Old PR",
            flag_reason="Open for 20 days",
            urgency="urgent",
            suggested_action="Create triage ticket",
            status=STATUS_PENDING,
        )
        session.add(flag)
        session.commit()
        flag_id = flag.id

    mock_jira = AsyncMock(spec=JiraClient)
    mock_slack = AsyncMock(spec=SlackClient)
    mock_slack.update_message.return_value = {"ok": True}

    payload = {
        "user": {"id": "U_APPROVER", "username": "alice"},
        "channel": {"id": "C_TEST"},
        "message": {"ts": "123.456", "blocks": []},
        "actions": [{"action_id": "approve_action", "value": str(flag_id)}],
    }

    await process_slack_interaction(
        payload=payload,
        session_factory=get_session_factory(),
        jira_client=mock_jira,
        slack_client=mock_slack,
    )

    # Jira client was not actually called
    mock_jira.create_issue.assert_not_awaited()

    # DB records status 'dry_run'
    with get_db_session() as session:
        action = session.scalars(select(ActionTaken).where(ActionTaken.target_id == "acme/api#15")).first()
        assert action is not None
        assert action.status == "dry_run"
        assert "[DRY RUN] would create a Jira issue" in action.result

    # Slack update text indicates dry run
    mock_slack.update_message.assert_awaited_once()
    slack_text = mock_slack.update_message.call_args[1]["text"]
    assert "[DRY RUN] would create a Jira issue" in slack_text


# -----------------------------------------------------------------------------
# Fix 5: unfurl_links=False and unfurl_media=False in Slack API calls
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unfurl_flags_are_sent() -> None:
    """Slack client sets unfurl_links=False and unfurl_media=False on post_message and update_message."""
    mock_web = AsyncMock()
    mock_web.chat_postMessage.return_value = AsyncMock(data={"ok": True, "ts": "123.456", "channel": "C1"})
    mock_web.chat_update.return_value = AsyncMock(data={"ok": True, "ts": "123.456", "channel": "C1"})

    client = SlackClient(token="xoxb-test", client=mock_web)

    await client.post_message(channel="C1", text="Hello")
    mock_web.chat_postMessage.assert_awaited_once_with(
        channel="C1",
        text="Hello",
        unfurl_links=False,
        unfurl_media=False,
    )

    await client.update_message(channel="C1", ts="123.456", text="Updated")
    mock_web.chat_update.assert_awaited_once_with(
        channel="C1",
        ts="123.456",
        text="Updated",
        unfurl_links=False,
        unfurl_media=False,
    )


# -----------------------------------------------------------------------------
# Fix 6: Gemini failover makes only 2 attempts before moving to fallback
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gemini_failover_makes_only_two_attempts_before_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """When GEMINI_FALLBACK_MODEL is configured, main model retries only once (2 attempts total) on 503."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "gemini-2.0-flash")
    get_settings.cache_clear()

    mock_models = AsyncMock()
    # Simulate 503 Service Unavailable error
    error_503 = Exception("503 Service Unavailable")
    setattr(error_503, "code", 503)
    mock_models.generate_content.side_effect = error_503

    mock_client = MagicMock()
    mock_client.aio.models = mock_models

    client = GeminiClient(api_key="test-key", model="gemini-2.5-flash", client=mock_client)

    # Patch asyncio.sleep to avoid waiting for exponential backoff in test
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(AnalysisError):
            await client.generate_json(prompt="Hello", schema=PriorityFlag)

    # Exactly 2 attempts (initial call + 1 retry) were made on the main model
    assert mock_models.generate_content.await_count == 2
    assert mock_sleep.await_count == 1
