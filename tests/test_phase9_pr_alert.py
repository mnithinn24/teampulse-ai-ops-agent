"""Tests for Phase 9: PR Completion -> Slack Alert with Jira links and Approve/Decline actions."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.agents.pr_alert import (
    build_pr_slack_blocks,
    extract_jira_keys,
    fetch_jira_ticket_context,
    handle_pr_approval_action,
    notify_new_pull_request,
)
from app.api.main import app
from app.api.routes.github import verify_github_signature
from app.api.routes.slack import process_slack_interaction
from app.db.database import get_session_factory
from app.db.models import ActionTaken
from app.integrations.github_client import GitHubClient
from app.integrations.jira_client import JiraClient, JiraIssue
from app.integrations.slack_client import SlackClient


def test_extract_jira_keys():
    """Test extracting Jira issue keys from titles, branches, and bodies."""
    # From title
    keys1 = extract_jira_keys("[KAN-42] Implement Medical RAG Chunking")
    assert keys1 == ["KAN-42"]

    # From branch
    keys2 = extract_jira_keys("feature/KAN-99-react-frontend-integration")
    assert keys2 == ["KAN-99"]

    # Multiple distinct keys
    keys3 = extract_jira_keys("Fixes PROJ-101 and links to KAN-42 in description")
    assert keys3 == ["PROJ-101", "KAN-42"]

    # Duplicates are deduplicated
    keys4 = extract_jira_keys("KAN-42: KAN-42 fix")
    assert keys4 == ["KAN-42"]

    # Empty text
    assert extract_jira_keys("") == []
    assert extract_jira_keys("Regular PR without Jira reference") == []


def test_build_pr_slack_blocks():
    """Test Slack Block Kit generation with Approve & Merge and Decline buttons."""
    blocks = build_pr_slack_blocks(
        repo="test-org/medical-rag",
        pr_number=101,
        pr_title="[KAN-42] Medical Document Ingestion",
        pr_author="nithinm",
        pr_url="https://github.com/test-org/medical-rag/pull/101",
        head_ref="feature/KAN-42-ingestion",
        base_ref="main",
        jira_info={
            "key": "KAN-42",
            "title": "Medical Document Ingestion Engine",
            "status": "In Progress",
            "url": "https://jira.test/browse/KAN-42",
        },
    )

    assert len(blocks) == 3
    assert blocks[0]["type"] == "header"
    assert "Pull Request Ready for Review" in blocks[0]["text"]["text"]

    # Section with details & Jira link
    body_text = blocks[1]["text"]["text"]
    assert "test-org/medical-rag#101" in body_text
    assert "KAN-42: Medical Document Ingestion Engine" in body_text
    assert "In Progress" in body_text

    # Actions block
    actions = blocks[2]["elements"]
    assert len(actions) == 2
    assert actions[0]["text"]["text"] == "Approve & Merge"
    assert actions[0]["action_id"] == "pr_approve_and_merge"
    assert actions[0]["value"] == "test-org/medical-rag#101|KAN-42"

    assert actions[1]["text"]["text"] == "Decline"
    assert actions[1]["action_id"] == "pr_decline"
    assert actions[1]["value"] == "test-org/medical-rag#101|KAN-42"


@pytest.mark.asyncio
async def test_notify_new_pull_request():
    """Test notifying Slack channel when a new PR is opened."""
    mock_slack = MagicMock(spec=SlackClient)
    mock_slack.post_message = AsyncMock(return_value={"ok": True, "ts": "1719000000.123456"})
    mock_slack.close = AsyncMock()

    mock_jira = MagicMock(spec=JiraClient)
    mock_jira.get_issue = AsyncMock(
        return_value=JiraIssue(
            key="KAN-42",
            title="Implement RAG Engine",
            status="In Progress",
            assignee="nithin",
            age_days=1,
            days_since_update=0,
            comment_count=1,
            is_stale=False,
            project="KAN",
            url="https://jira.test/browse/KAN-42",
        )
    )
    mock_jira.close = AsyncMock()

    res = await notify_new_pull_request(
        repo="test-org/rag-app",
        pr_number=55,
        pr_title="[KAN-42] Add fast text chunker",
        pr_author="dev1",
        pr_url="https://github.com/test-org/rag-app/pull/55",
        head_ref="feature/KAN-42-chunker",
        base_ref="main",
        slack_client=mock_slack,
        jira_client=mock_jira,
        channel_id="C_ALERTS",
    )

    assert res["status"] == "posted"
    assert res["ts"] == "1719000000.123456"
    assert res["jira_key"] == "KAN-42"
    mock_slack.post_message.assert_called_once()
    assert mock_slack.post_message.call_args.kwargs["channel"] == "C_ALERTS"


@pytest.mark.asyncio
async def test_handle_pr_approval_action_dry_run():
    """Test PR approve & decline actions in dry-run mode."""
    session_factory = get_session_factory()

    # Dry run approve
    res_approve = await handle_pr_approval_action(
        action_id="pr_approve_and_merge",
        value="test-org/repo#10|KAN-42",
        user_id="U_APPROVER",
        dry_run=True,
        session_factory=session_factory,
    )
    assert "[DRY RUN] Would merge" in res_approve
    assert "KAN-42" in res_approve

    # Dry run decline
    res_decline = await handle_pr_approval_action(
        action_id="pr_decline",
        value="test-org/repo#10|KAN-42",
        user_id="U_APPROVER",
        dry_run=True,
        session_factory=session_factory,
    )
    assert "[DRY RUN] Would decline" in res_decline

    # Verify audit log entries
    with session_factory() as session:
        actions = session.query(ActionTaken).filter(
            ActionTaken.action_type.in_(["pr_approve_and_merge", "pr_decline"])
        ).all()
        assert len(actions) >= 2


@pytest.mark.asyncio
async def test_handle_pr_approval_action_live():
    """Test live PR approve & merge and decline execution."""
    mock_github = MagicMock(spec=GitHubClient)
    mock_github.merge_pull_request = AsyncMock(return_value={"merged": True, "sha": "abc1234"})
    mock_github.close_pull_request = AsyncMock(return_value={"state": "closed"})
    mock_github.close = AsyncMock()

    mock_jira = MagicMock(spec=JiraClient)
    mock_jira.add_comment = AsyncMock(return_value={"id": "comment-123"})
    mock_jira.close = AsyncMock()

    session_factory = get_session_factory()

    # 1. Live Approve & Merge
    res_merge = await handle_pr_approval_action(
        action_id="pr_approve_and_merge",
        value="test-org/repo#42|KAN-10",
        user_id="U_LEAD",
        dry_run=False,
        github_client=mock_github,
        jira_client=mock_jira,
        session_factory=session_factory,
    )
    assert "Merged test-org/repo#42 to main" in res_merge
    mock_github.merge_pull_request.assert_called_once_with(
        repo="test-org/repo",
        pull_number=42,
        commit_title="Merge pull request #42 from test-org/repo",
        commit_message="Approved and merged by @U_LEAD via TeamPulse Slack action.",
    )
    mock_jira.add_comment.assert_called_once()

    # 2. Live Decline
    res_decline = await handle_pr_approval_action(
        action_id="pr_decline",
        value="test-org/repo#42|KAN-10",
        user_id="U_LEAD",
        dry_run=False,
        github_client=mock_github,
        jira_client=mock_jira,
        session_factory=session_factory,
    )
    assert "Declined and closed test-org/repo#42" in res_decline
    mock_github.close_pull_request.assert_called_once_with(
        repo="test-org/repo",
        pull_number=42,
    )


def test_verify_github_signature():
    """Test GitHub HMAC-SHA256 signature verification."""
    secret = "my_webhook_secret_key"
    payload = b'{"action":"opened","pull_request":{"number":1}}'

    # Compute valid signature
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    header_val = f"sha256={sig}"

    # Valid
    assert verify_github_signature(payload, header_val, secret) is True

    # Invalid signature
    assert verify_github_signature(payload, "sha256=invalidhex", secret) is False

    # Missing sha256 prefix
    assert verify_github_signature(payload, sig, secret) is False

    # Empty secret allows request
    assert verify_github_signature(payload, None, "") is True


def test_github_webhook_endpoint():
    """Test POST /github/webhook handles opened PR and ignores other events."""
    secret = "dummy_secret"
    payload = {
        "action": "opened",
        "repository": {"full_name": "psf/requests"},
        "pull_request": {
            "number": 1234,
            "title": "[KAN-99] Fix connection pooling",
            "html_url": "https://github.com/psf/requests/pull/1234",
            "user": {"login": "contributor"},
            "head": {"ref": "feature/KAN-99-pool"},
            "base": {"ref": "main"},
            "body": "Fixes connection pool timeout",
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    with TestClient(app) as client:
        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(github_webhook_secret=secret, slack_channel_id="C123")
            with patch("app.api.routes.github.notify_new_pull_request", new_callable=AsyncMock) as mock_notify:
                res = client.post(
                    "/github/webhook",
                    content=raw_body,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-Hub-Signature-256": sig,
                        "Content-Type": "application/json",
                    },
                )
                assert res.status_code == 200
                data = res.json()
                assert data["status"] == "ok"
                assert "1234" in data["message"]


def test_github_manual_pr_alert_endpoint():
    """Test POST /github/pr/alert triggers alert with API key."""
    with TestClient(app) as client:
        with patch("app.api.routes.github.notify_new_pull_request", new_callable=AsyncMock) as mock_notify:
            mock_notify.return_value = {"status": "posted", "ts": "123.456", "jira_key": "KAN-10"}

            res = client.post(
                "/github/pr/alert",
                json={
                    "repo": "owner/repo",
                    "pr_number": 42,
                    "title": "[KAN-10] Implement RAG pipeline",
                    "author": "engineer",
                },
                headers={"X-API-Key": "dummy-admin-key"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "posted"
            assert data["jira_key"] == "KAN-10"


@pytest.mark.asyncio
async def test_slack_interaction_pr_approve_and_merge():
    """Test clicking [Approve & Merge] button updates the Slack message."""
    mock_slack = MagicMock(spec=SlackClient)
    mock_slack.update_message = AsyncMock(return_value={"ok": True})

    interaction_payload = {
        "user": {"id": "U_LEAD"},
        "channel": {"id": "C_ALERTS"},
        "message": {
            "ts": "1719000000.555555",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "PR Ready"}},
                {"type": "actions", "block_id": "pr_actions_repo_42", "elements": []},
            ],
        },
        "actions": [
            {
                "action_id": "pr_approve_and_merge",
                "value": "org/repo#42|KAN-10",
            }
        ],
    }

    session_factory = get_session_factory()
    with patch("app.api.routes.slack.handle_pr_approval_action", new_callable=AsyncMock) as mock_action:
        mock_action.return_value = "Merged org/repo#42 to main"

        await process_slack_interaction(
            payload=interaction_payload,
            session_factory=session_factory,
            slack_client=mock_slack,
        )

        mock_action.assert_called_once_with(
            action_id="pr_approve_and_merge",
            value="org/repo#42|KAN-10",
            user_id="U_LEAD",
            session_factory=session_factory,
        )
        mock_slack.update_message.assert_called_once()
        sent_blocks = mock_slack.update_message.call_args.kwargs["blocks"]
        # Actions block was replaced with context block
        assert sent_blocks[-1]["type"] == "context"
        assert "Approved & Merged by <@U_LEAD>" in sent_blocks[-1]["elements"][0]["text"]
