"""Pytest cases covering PR merge safety, Jira alert loop deduplication, and Jira webhook auth."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.agents.pr_alert import handle_pr_approval_action
from app.api.routes.jira import (
    _notified_done_keys,
    is_ticket_handled_or_alerted,
    mark_issue_reopened,
    poll_jira_completed_tasks_loop,
    record_ticket_alerted,
)
from app.config import get_settings
from app.db.database import get_session_factory
from app.db.models import ActionTaken
from app.integrations.github_client import GitHubClient
from app.integrations.jira_client import JiraClient
from app.api.main import app


# ==============================================================================
# 1. Merge Safety Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_merge_refused_for_empty_approver_list(monkeypatch: pytest.MonkeyPatch):
    """Refuse PR merge when SLACK_APPROVER_IDS is empty and explain how to configure it."""
    monkeypatch.setenv("SLACK_APPROVER_IDS", "")
    get_settings.cache_clear()

    mock_gh = MagicMock(spec=GitHubClient)
    mock_gh.merge_pull_request = AsyncMock()
    mock_gh.close = AsyncMock()

    session_factory = get_session_factory()
    outcome = await handle_pr_approval_action(
        action_id="pr_approve_and_merge",
        value="owner/repo#10|KAN-1",
        user_id="U_ANYONE",
        dry_run=False,
        github_client=mock_gh,
        session_factory=session_factory,
    )

    assert "Merge refused" in outcome
    assert "SLACK_APPROVER_IDS" in outcome
    assert "configure" in outcome.lower()
    mock_gh.merge_pull_request.assert_not_called()

    # Verify audit record is marked as refused
    with session_factory() as session:
        record = (
            session.query(ActionTaken)
            .filter_by(action_type="pr_approve_and_merge", target_id="owner/repo#10")
            .first()
        )
        assert record is not None
        assert record.status == "refused"


@pytest.mark.asyncio
async def test_merge_refused_for_non_approver(monkeypatch: pytest.MonkeyPatch):
    """Refuse PR merge when clicking Slack user is not in SLACK_APPROVER_IDS."""
    monkeypatch.setenv("SLACK_APPROVER_IDS", "U_LEAD,U_TECH_LEAD")
    get_settings.cache_clear()

    mock_gh = MagicMock(spec=GitHubClient)
    mock_gh.merge_pull_request = AsyncMock()
    mock_gh.close = AsyncMock()

    session_factory = get_session_factory()
    outcome = await handle_pr_approval_action(
        action_id="pr_approve_and_merge",
        value="owner/repo#11|KAN-2",
        user_id="U_RANDOM_DEV",
        dry_run=False,
        github_client=mock_gh,
        session_factory=session_factory,
    )

    assert "Merge refused" in outcome
    assert "U_RANDOM_DEV" in outcome
    assert "not authorized" in outcome.lower()
    mock_gh.merge_pull_request.assert_not_called()


@pytest.mark.asyncio
async def test_merge_refused_for_already_merged_pr(monkeypatch: pytest.MonkeyPatch):
    """Refuse PR merge when PR is already merged or not open."""
    monkeypatch.setenv("SLACK_APPROVER_IDS", "U_LEAD")
    get_settings.cache_clear()

    mock_gh = MagicMock(spec=GitHubClient)
    mock_gh.get_pull_request = AsyncMock(
        return_value={"state": "closed", "merged": True, "head": {"sha": "abc1234"}}
    )
    mock_gh.merge_pull_request = AsyncMock()
    mock_gh.close = AsyncMock()

    session_factory = get_session_factory()
    outcome = await handle_pr_approval_action(
        action_id="pr_approve_and_merge",
        value="owner/repo#12|KAN-3",
        user_id="U_LEAD",
        dry_run=False,
        github_client=mock_gh,
        session_factory=session_factory,
    )

    assert "Merge refused" in outcome
    assert "already merged" in outcome.lower()
    mock_gh.merge_pull_request.assert_not_called()


@pytest.mark.asyncio
async def test_merge_refused_for_conflicting_pr(monkeypatch: pytest.MonkeyPatch):
    """Refuse PR merge when PR has merge conflicts (mergeable=False)."""
    monkeypatch.setenv("SLACK_APPROVER_IDS", "U_LEAD")
    get_settings.cache_clear()

    mock_gh = MagicMock(spec=GitHubClient)
    mock_gh.get_pull_request = AsyncMock(
        return_value={
            "state": "open",
            "merged": False,
            "mergeable": False,
            "mergeable_state": "dirty",
            "head": {"sha": "abc1234"},
        }
    )
    mock_gh.merge_pull_request = AsyncMock()
    mock_gh.close = AsyncMock()

    session_factory = get_session_factory()
    outcome = await handle_pr_approval_action(
        action_id="pr_approve_and_merge",
        value="owner/repo#13|KAN-4",
        user_id="U_LEAD",
        dry_run=False,
        github_client=mock_gh,
        session_factory=session_factory,
    )

    assert "Merge refused" in outcome
    assert "conflicts" in outcome.lower()
    mock_gh.merge_pull_request.assert_not_called()


@pytest.mark.asyncio
async def test_merge_refused_for_failing_ci(monkeypatch: pytest.MonkeyPatch):
    """Refuse PR merge when commit status or check runs indicate failing CI."""
    monkeypatch.setenv("SLACK_APPROVER_IDS", "U_LEAD")
    get_settings.cache_clear()

    mock_gh = MagicMock(spec=GitHubClient)
    mock_gh.get_pull_request = AsyncMock(
        return_value={
            "state": "open",
            "merged": False,
            "mergeable": True,
            "head": {"sha": "c0ffee1"},
        }
    )
    # Combined commit status API indicates failure
    mock_gh.get_commit_status = AsyncMock(
        return_value={"total_count": 1, "state": "failure"}
    )
    mock_gh.get_commit_check_runs = AsyncMock(
        return_value={"total_count": 0, "check_runs": []}
    )
    mock_gh.merge_pull_request = AsyncMock()
    mock_gh.close = AsyncMock()

    session_factory = get_session_factory()
    outcome = await handle_pr_approval_action(
        action_id="pr_approve_and_merge",
        value="owner/repo#14|KAN-5",
        user_id="U_LEAD",
        dry_run=False,
        github_client=mock_gh,
        session_factory=session_factory,
    )

    assert "Merge refused" in outcome
    assert "CI status is failure" in outcome
    mock_gh.merge_pull_request.assert_not_called()


@pytest.mark.asyncio
async def test_merge_allowed_with_no_ci_note(monkeypatch: pytest.MonkeyPatch):
    """Allow PR merge when no CI is configured but include '(no CI found)' in the message."""
    monkeypatch.setenv("SLACK_APPROVER_IDS", "U_LEAD")
    get_settings.cache_clear()

    mock_gh = MagicMock(spec=GitHubClient)
    mock_gh.get_pull_request = AsyncMock(
        return_value={
            "state": "open",
            "merged": False,
            "mergeable": True,
            "head": {"sha": "c0ffee2"},
        }
    )
    # Zero statuses and zero check runs
    mock_gh.get_commit_status = AsyncMock(return_value={"total_count": 0, "state": ""})
    mock_gh.get_commit_check_runs = AsyncMock(return_value={"total_count": 0, "check_runs": []})
    mock_gh.merge_pull_request = AsyncMock(return_value={"merged": True, "sha": "c0ffee2999"})
    mock_gh.close = AsyncMock()

    session_factory = get_session_factory()
    outcome = await handle_pr_approval_action(
        action_id="pr_approve_and_merge",
        value="owner/repo#15",
        user_id="U_LEAD",
        dry_run=False,
        github_client=mock_gh,
        session_factory=session_factory,
    )

    assert "Merged owner/repo#15 to main" in outcome
    assert "no CI found" in outcome
    mock_gh.merge_pull_request.assert_called_once()


# ==============================================================================
# 2. Alert Loop & Poller Deduplication Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_poller_skips_ticket_teampulse_moved_to_done():
    """Jira poller must skip tickets that have a recorded merge action in actions_taken."""
    _notified_done_keys.clear()
    session_factory = get_session_factory()

    # Pre-record a ticket moved to Done by TeamPulse
    with session_factory() as session:
        action = ActionTaken(
            workspace_id="default",
            action_type="jira_auto_done",
            target_id="KAN-99",
            payload=json.dumps({"pr": "owner/repo#20", "user_id": "U_LEAD"}),
            status="executed",
            approved_by="U_LEAD",
            result="Moved KAN-99 to Done after merging owner/repo#20",
        )
        session.add(action)
        session.commit()

    # Mock Jira search_issues returning KAN-99 (Done by TeamPulse) and KAN-100 (Done by developer)
    mock_issue_99 = MagicMock()
    mock_issue_99.key = "KAN-99"
    mock_issue_99.title = "Merged feature"
    mock_issue_99.status = "Done"
    mock_issue_99.assignee = "alice"

    mock_issue_100 = MagicMock()
    mock_issue_100.key = "KAN-100"
    mock_issue_100.title = "Developer completed task"
    mock_issue_100.status = "Done"
    mock_issue_100.assignee = "bob"

    mock_jira = MagicMock(spec=JiraClient)
    mock_jira.search_issues = AsyncMock(return_value=[mock_issue_99, mock_issue_100])
    mock_jira.close = AsyncMock()

    with patch("app.api.routes.jira.JiraClient", return_value=mock_jira):
        with patch("app.api.routes.jira.notify_jira_task_completion", new_callable=AsyncMock) as mock_notify:
            with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
                try:
                    await poll_jira_completed_tasks_loop(interval_seconds=1.0)
                except asyncio.CancelledError:
                    pass

            # KAN-99 was moved to Done by TeamPulse and must be skipped
            # KAN-100 was not handled by TeamPulse and must be notified
            notified_keys = [call.kwargs.get("issue_key") for call in mock_notify.call_args_list]
            assert "KAN-99" not in notified_keys
            assert "KAN-100" in notified_keys


@pytest.mark.asyncio
async def test_poller_remembers_alerted_tickets_across_restart():
    """Alerted tickets recorded in the DB are remembered and not re-alerted after a restart."""
    _notified_done_keys.clear()
    session_factory = get_session_factory()

    # Pre-record an alert in the database
    record_ticket_alerted("KAN-77", session_factory=session_factory)

    # Simulate restart by clearing in-memory set
    _notified_done_keys.clear()
    assert "KAN-77" not in _notified_done_keys

    # Verify is_ticket_handled_or_alerted checks database and recognizes it
    assert is_ticket_handled_or_alerted("KAN-77", session_factory=session_factory) is True
    # And populates in-memory cache
    assert "KAN-77" in _notified_done_keys


# ==============================================================================
# 3. /jira/webhook Authentication Tests
# ==============================================================================


def test_jira_webhook_rejects_without_secret(monkeypatch: pytest.MonkeyPatch):
    """Webhook must reject request with 401 if secret is missing or incorrect."""
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "super-secret-token-xyz")
    get_settings.cache_clear()

    with TestClient(app) as client:
        # 1. No secret header or parameter
        res_no_secret = client.post(
            "/jira/webhook",
            json={"webhookEvent": "jira:issue_updated", "issue": {"key": "KAN-10"}},
        )
        assert res_no_secret.status_code == 401
        assert "invalid or missing" in res_no_secret.json()["detail"].lower()

        # 2. Wrong secret header
        res_wrong_secret = client.post(
            "/jira/webhook",
            headers={"X-Atlassian-Webhook-Token": "wrong-secret-token"},
            json={"webhookEvent": "jira:issue_updated", "issue": {"key": "KAN-10"}},
        )
        assert res_wrong_secret.status_code == 401


def test_jira_webhook_accepts_valid_secret(monkeypatch: pytest.MonkeyPatch):
    """Webhook accepts request with valid secret via header or query param and processes Done status."""
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "super-secret-token-xyz")
    get_settings.cache_clear()
    _notified_done_keys.clear()

    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "KAN-55",
            "fields": {
                "summary": "Implement feature X",
                "status": {"name": "Done", "statusCategory": {"key": "done"}},
                "assignee": {"displayName": "Developer Dan"},
            },
        },
        "changelog": {
            "items": [{"field": "status", "fromString": "In Progress", "toString": "Done"}]
        },
    }

    with patch("app.api.routes.jira.notify_jira_task_completion", new_callable=AsyncMock):
        with TestClient(app) as client:
            # 1. Via X-Atlassian-Webhook-Token header
            res_header = client.post(
                "/jira/webhook",
                headers={"X-Atlassian-Webhook-Token": "super-secret-token-xyz"},
                json=payload,
            )
            assert res_header.status_code == 200
            assert res_header.json()["status"] == "alert_queued"

            # 2. Calling again for the same ticket should be skipped (already alerted)
            res_dup = client.post(
                "/jira/webhook",
                headers={"X-Atlassian-Webhook-Token": "super-secret-token-xyz"},
                json=payload,
            )
            assert res_dup.status_code == 200
            assert res_dup.json()["status"] == "skipped"
