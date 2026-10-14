"""Tests for Slack HMAC-SHA256 signature verification, interaction handling, and action executors."""

import hashlib
import hmac
import json
import time
from typing import Dict
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.actions import execute_approved_action
from app.api.main import app
from app.api.routes.slack import process_slack_interaction
from app.config import get_settings
from app.core.errors import ActionError, JiraError
from app.core.security import verify_slack_signature
from app.db.models import ActionTaken, Base, Flag, utc_now
from app.integrations.jira_client import JiraClient, JiraIssue
from app.integrations.slack_client import SlackClient


TEST_SECRET = "super_secret_slack_signing_token"


def generate_slack_headers(
    raw_body: bytes,
    secret: str = TEST_SECRET,
    timestamp: int = None,
) -> Dict[str, str]:
    """Generate valid X-Slack headers for test requests."""
    ts = timestamp if timestamp is not None else int(time.time())
    sig_basestring = b"v0:" + str(ts).encode("utf-8") + b":" + raw_body
    computed = "v0=" + hmac.new(secret.encode("utf-8"), sig_basestring, hashlib.sha256).hexdigest()
    return {
        "x-slack-request-timestamp": str(ts),
        "x-slack-signature": computed,
        "content-type": "application/x-www-form-urlencoded",
    }


@pytest.fixture
def test_db_session():
    """In-memory SQLite session factory for test isolation."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory


# =========================================================================
# 1. Security & Signature Tests
# =========================================================================


def test_signature_valid() -> None:
    body = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
    now = time.time()
    headers = generate_slack_headers(body, secret=TEST_SECRET, timestamp=int(now))

    assert verify_slack_signature(
        raw_body=body,
        timestamp=headers["x-slack-request-timestamp"],
        signature=headers["x-slack-signature"],
        signing_secret=TEST_SECRET,
        current_time=now,
    ) is True


def test_signature_wrong_signature() -> None:
    body = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
    now = time.time()
    headers = generate_slack_headers(body, secret=TEST_SECRET, timestamp=int(now))

    # Tampered signature
    assert verify_slack_signature(
        raw_body=body,
        timestamp=headers["x-slack-request-timestamp"],
        signature="v0=wrong_hex_hash_000000000000000000000000000000",
        signing_secret=TEST_SECRET,
        current_time=now,
    ) is False


def test_signature_old_timestamp() -> None:
    body = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
    now = time.time()
    # Timestamp is 301 seconds old (> 5 minutes)
    old_ts = int(now - 305)
    headers = generate_slack_headers(body, secret=TEST_SECRET, timestamp=old_ts)

    assert verify_slack_signature(
        raw_body=body,
        timestamp=headers["x-slack-request-timestamp"],
        signature=headers["x-slack-signature"],
        signing_secret=TEST_SECRET,
        current_time=now,
    ) is False


def test_signature_body_changed() -> None:
    body = b"payload=original_body_content"
    now = time.time()
    headers = generate_slack_headers(body, secret=TEST_SECRET, timestamp=int(now))

    # Request body modified after signature was created
    tampered_body = b"payload=tampered_body_content"
    assert verify_slack_signature(
        raw_body=tampered_body,
        timestamp=headers["x-slack-request-timestamp"],
        signature=headers["x-slack-signature"],
        signing_secret=TEST_SECRET,
        current_time=now,
    ) is False


def test_endpoint_rejects_invalid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_SIGNING_SECRET", TEST_SECRET)
    get_settings.cache_clear()

    client = TestClient(app)
    body = b"payload=test"
    # Send request with invalid signature
    res = client.post(
        "/slack/interactions",
        content=body,
        headers={
            "x-slack-request-timestamp": str(int(time.time())),
            "x-slack-signature": "v0=bad_signature",
            "content-type": "application/x-www-form-urlencoded",
        },
    )
    assert res.status_code == 401
    assert "Invalid Slack request signature" in res.text
    get_settings.cache_clear()


# =========================================================================
# 2. Interaction & Idempotency Tests
# =========================================================================


@pytest.mark.asyncio
async def test_approve_runs_executor_once_when_callback_sent_twice(test_db_session) -> None:
    """Approve button click runs executor exactly once even when callback is sent twice."""
    # Seed pending flag
    with test_db_session() as session:
        flag = Flag(
            workspace_id="test-ws",
            item_type="jira_ticket",
            item_id="KAN-10",
            title="Database pool exhausted",
            flag_reason="Inactive for 15 days",
            urgency="urgent",
            suggested_action="Increase connection pool size",
            evidence_age_days=15,
            evidence_author="alice",
            status="pending",
        )
        session.add(flag)
        session.commit()
        flag_id = flag.id

    mock_jira = AsyncMock(spec=JiraClient)
    mock_jira.add_comment.return_value = {"id": "1001"}

    mock_slack = AsyncMock(spec=SlackClient)
    mock_slack.update_message.return_value = {"ok": True}

    payload = {
        "user": {"id": "U12345", "username": "alice"},
        "channel": {"id": "C999"},
        "message": {
            "ts": "1234567890.000100",
            "blocks": [
                {
                    "type": "actions",
                    "block_id": f"flag_actions_{flag_id}",
                    "elements": [{"value": str(flag_id)}],
                }
            ],
        },
        "actions": [{"action_id": "approve_action", "value": str(flag_id)}],
        "response_url": "https://hooks.slack.com/actions/test/dummy",
    }

    # 1. First callback execution
    await process_slack_interaction(
        payload=payload,
        session_factory=test_db_session,
        jira_client=mock_jira,
        slack_client=mock_slack,
    )

    # Jira comment was added
    mock_jira.add_comment.assert_awaited_once_with(
        issue_key="KAN-10",
        comment_text="Increase connection pool size",
    )
    mock_slack.update_message.assert_awaited_once()

    # 2. Second callback execution (simulating network retry or double-click)
    with patch("app.api.routes.slack.send_ephemeral_message", new_callable=AsyncMock) as mock_ephemeral:
        await process_slack_interaction(
            payload=payload,
            session_factory=test_db_session,
            jira_client=mock_jira,
            slack_client=mock_slack,
        )

        # Jira executor was NOT called again (still 1 count!)
        assert mock_jira.add_comment.await_count == 1
        # Ephemeral message sent notifying user that item was already handled
        mock_ephemeral.assert_awaited_once()
        assert "already been handled" in mock_ephemeral.call_args[1]["text"]

    # Verify DB state
    with test_db_session() as session:
        updated_flag = session.get(Flag, flag_id)
        assert updated_flag.status == "approved"
        assert updated_flag.approved_by == "U12345"
        assert updated_flag.result == "Commented on KAN-10"

        actions = session.query(ActionTaken).all()
        # Exactly one action logged
        assert len(actions) == 1
        assert actions[0].status == "executed"
        assert actions[0].target_id == "KAN-10"


@pytest.mark.asyncio
async def test_dismiss_marks_flag_dismissed_and_updates_message(test_db_session) -> None:
    """Dismiss button click marks flag dismissed without calling Jira executor."""
    with test_db_session() as session:
        flag = Flag(
            workspace_id="test-ws",
            item_type="jira_ticket",
            item_id="KAN-20",
            flag_reason="Stale review",
            suggested_action="Ping reviewer",
            status="pending",
        )
        session.add(flag)
        session.commit()
        flag_id = flag.id

    mock_jira = AsyncMock(spec=JiraClient)
    mock_slack = AsyncMock(spec=SlackClient)
    mock_slack.update_message.return_value = {"ok": True}

    payload = {
        "user": {"id": "U55555", "username": "bob"},
        "channel": {"id": "C999"},
        "message": {"ts": "123.456", "blocks": []},
        "actions": [{"action_id": "dismiss_action", "value": str(flag_id)}],
    }

    await process_slack_interaction(
        payload=payload,
        session_factory=test_db_session,
        jira_client=mock_jira,
        slack_client=mock_slack,
    )

    # Jira executor must not be called
    mock_jira.add_comment.assert_not_awaited()
    mock_jira.create_issue.assert_not_awaited()

    # Message was updated
    mock_slack.update_message.assert_awaited_once()

    # Check DB state
    with test_db_session() as session:
        updated_flag = session.get(Flag, flag_id)
        assert updated_flag.status == "dismissed"
        assert updated_flag.approved_by == "U55555"

        actions = session.query(ActionTaken).all()
        assert len(actions) == 1
        assert actions[0].status == "dismissed"


@pytest.mark.asyncio
async def test_unauthorized_approver_is_rejected(test_db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Approver not in SLACK_APPROVER_IDS is rejected with ephemeral error."""
    monkeypatch.setenv("SLACK_APPROVER_IDS", "U_MANAGER_1,U_MANAGER_2")
    get_settings.cache_clear()

    with test_db_session() as session:
        flag = Flag(
            workspace_id="test-ws",
            item_type="jira_ticket",
            item_id="KAN-30",
            flag_reason="Stale",
            suggested_action="Review",
            status="pending",
        )
        session.add(flag)
        session.commit()
        flag_id = flag.id

    mock_jira = AsyncMock(spec=JiraClient)
    mock_slack = AsyncMock(spec=SlackClient)

    payload = {
        "user": {"id": "U_UNAUTHORIZED_PEON", "username": "charlie"},
        "channel": {"id": "C999"},
        "message": {"ts": "123.456", "blocks": []},
        "actions": [{"action_id": "approve_action", "value": str(flag_id)}],
        "response_url": "https://hooks.slack.com/actions/test",
    }

    with patch("app.api.routes.slack.send_ephemeral_message", new_callable=AsyncMock) as mock_ephemeral:
        await process_slack_interaction(
            payload=payload,
            session_factory=test_db_session,
            jira_client=mock_jira,
            slack_client=mock_slack,
        )

        mock_jira.add_comment.assert_not_awaited()
        mock_ephemeral.assert_awaited_once()
        assert "not authorized" in mock_ephemeral.call_args[1]["text"]

    # Flag status remains pending
    with test_db_session() as session:
        assert session.get(Flag, flag_id).status == "pending"

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_executor_failure_sets_status_failed_and_shows_error(test_db_session) -> None:
    """When action execution fails, status is marked failed and error is shown."""
    with test_db_session() as session:
        flag = Flag(
            workspace_id="test-ws",
            item_type="jira_ticket",
            item_id="KAN-40",
            flag_reason="Stale ticket",
            suggested_action="Comment on ticket",
            status="pending",
        )
        session.add(flag)
        session.commit()
        flag_id = flag.id

    mock_jira = AsyncMock(spec=JiraClient)
    mock_jira.add_comment.side_effect = JiraError("Jira API 500 Internal Server Error")

    mock_slack = AsyncMock(spec=SlackClient)
    mock_slack.update_message.return_value = {"ok": True}

    payload = {
        "user": {"id": "U_APPROVER", "username": "alice"},
        "channel": {"id": "C999"},
        "message": {
            "ts": "123.456",
            "blocks": [{"type": "actions", "elements": [{"value": str(flag_id)}]}],
        },
        "actions": [{"action_id": "approve_action", "value": str(flag_id)}],
    }

    await process_slack_interaction(
        payload=payload,
        session_factory=test_db_session,
        jira_client=mock_jira,
        slack_client=mock_slack,
    )

    # Message was updated with error text
    mock_slack.update_message.assert_awaited_once()
    assert "action failed" in mock_slack.update_message.call_args[1]["text"].lower()

    # DB state: flag status is failed
    with test_db_session() as session:
        updated_flag = session.get(Flag, flag_id)
        assert updated_flag.status == "failed"
        assert "Jira API 500" in updated_flag.result

        actions = session.query(ActionTaken).all()
        assert len(actions) == 1
        assert actions[0].status == "failed"
        assert "Jira API 500" in actions[0].error


# =========================================================================
# 3. Action Execution & Dry-Run Tests
# =========================================================================


@pytest.mark.asyncio
async def test_github_pr_action_creates_jira_ticket() -> None:
    """Approving a GitHub PR flag creates a Jira issue in JIRA_PROJECT_KEY."""
    flag = Flag(
        workspace_id="ws",
        item_type="github_pr",
        item_id="psf/requests#6454",
        flag_reason="Inactive for 45 days",
        suggested_action="Triage and decide whether to close",
        evidence_age_days=45,
        evidence_author="contributor",
        status="pending",
    )

    mock_jira = AsyncMock(spec=JiraClient)
    mock_jira.create_issue.return_value = JiraIssue(
        key="KAN-99",
        title="Triage stale PR psf/requests#6454",
        status="Open",
        assignee="unassigned",
        age_days=0,
        days_since_update=0,
        comment_count=0,
        is_stale=False,
        project="KAN",
    )

    result = await execute_approved_action(flag, jira_client=mock_jira, dry_run=False)

    assert result == "Created KAN-99"
    mock_jira.create_issue.assert_awaited_once()
    kwargs = mock_jira.create_issue.call_args[1]
    assert "Triage stale PR psf/requests#6454" in kwargs["summary"]
    assert "https://github.com/psf/requests/pull/6454" in kwargs["description"]
    assert "contributor" in kwargs["description"]


@pytest.mark.asyncio
async def test_dry_run_does_not_call_jira() -> None:
    """When DRY_RUN_ACTIONS is True, actions log what would happen without calling Jira."""
    flag_pr = Flag(
        workspace_id="ws",
        item_type="github_pr",
        item_id="owner/repo#42",
        flag_reason="Stale PR",
        suggested_action="Review",
        evidence_age_days=20,
        evidence_author="alice",
    )
    flag_jira = Flag(
        workspace_id="ws",
        item_type="jira_ticket",
        item_id="KAN-50",
        flag_reason="Stale ticket",
        suggested_action="Add comment",
    )

    mock_jira = AsyncMock(spec=JiraClient)

    # PR Dry run
    res_pr = await execute_approved_action(flag_pr, jira_client=mock_jira, dry_run=True)
    assert "[DRY RUN]" in res_pr
    mock_jira.create_issue.assert_not_awaited()

    # Jira Dry run
    res_jira = await execute_approved_action(flag_jira, jira_client=mock_jira, dry_run=True)
    assert "[DRY RUN]" in res_jira
    mock_jira.add_comment.assert_not_awaited()
