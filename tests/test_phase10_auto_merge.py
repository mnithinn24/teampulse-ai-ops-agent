"""Tests for Phase 10: One-Click Slack Auto-Merge and Jira Done Transition."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.agents.pr_alert import handle_pr_approval_action
from app.api.main import app
from app.api.routes.slack import process_slack_interaction
from app.db.database import get_session_factory
from app.db.models import ActionTaken
from app.integrations.github_client import GitHubClient
from app.integrations.jira_client import JiraClient
from app.integrations.slack_client import SlackClient


@pytest.mark.asyncio
async def test_jira_transitions_and_mark_done():
    """Test get_transitions, transition_issue, and mark_issue_done logic."""
    client = JiraClient()
    mock_request = AsyncMock()

    # 1. Mock get_transitions
    mock_request.side_effect = [
        # Response for GET transitions
        {
            "transitions": [
                {"id": "11", "name": "In Progress", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
                {"id": "31", "name": "Done", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
            ]
        },
        # Response for POST transition
        {},
    ]
    client._request = mock_request

    # Test mark_issue_done finds 'Done' transition (id 31) and applies it
    status_result = await client.mark_issue_done("KAN-42")
    assert status_result == "Done"

    # Verify calls
    assert mock_request.call_count == 2
    get_call = mock_request.call_args_list[0]
    assert get_call.args[1] == "/rest/api/3/issue/KAN-42/transitions"

    post_call = mock_request.call_args_list[1]
    assert post_call.args[0] == "POST"
    assert post_call.args[1] == "/rest/api/3/issue/KAN-42/transitions"
    assert post_call.kwargs["json_data"] == {"transition": {"id": "31"}}


@pytest.mark.asyncio
async def test_jira_mark_done_none_when_no_terminal_status():
    """Test mark_issue_done returns None gracefully if no Done/Closed transition exists."""
    client = JiraClient()
    client._request = AsyncMock(
        return_value={
            "transitions": [
                {"id": "11", "name": "To Do", "to": {"name": "To Do", "statusCategory": {"key": "new"}}},
                {"id": "21", "name": "In Review", "to": {"name": "In Review", "statusCategory": {"key": "indeterminate"}}},
            ]
        }
    )

    status_result = await client.mark_issue_done("KAN-10")
    assert status_result is None


@pytest.mark.asyncio
async def test_one_click_auto_merge_live_execution():
    """Test live One-Click Auto-Merge: GitHub merges PR and Jira marks ticket as Done."""
    mock_github = MagicMock(spec=GitHubClient)
    mock_github.merge_pull_request = AsyncMock(
        return_value={"merged": True, "sha": "e4f8b9c1d2e3f4a5b6c7", "message": "Pull Request merged"}
    )
    mock_github.close = AsyncMock()

    mock_jira = MagicMock(spec=JiraClient)
    mock_jira.mark_issue_done = AsyncMock(return_value="Done")
    mock_jira.add_comment = AsyncMock(return_value={"id": "comment-456"})
    mock_jira.close = AsyncMock()

    session_factory = get_session_factory()

    outcome = await handle_pr_approval_action(
        action_id="pr_approve_and_merge",
        value="acme-org/payment-service#88|KAN-42",
        user_id="U_TECH_LEAD",
        dry_run=False,
        github_client=mock_github,
        jira_client=mock_jira,
        session_factory=session_factory,
    )

    # Verify outcome message includes short SHA and Jira status
    assert "Merged acme-org/payment-service#88 to main (SHA: e4f8b9c)" in outcome
    assert "marked Jira KAN-42 as Done" in outcome

    # Verify GitHub API was called to merge
    mock_github.merge_pull_request.assert_called_once_with(
        repo="acme-org/payment-service",
        pull_number=88,
        commit_title="Merge pull request #88 from acme-org/payment-service",
        commit_message="Approved and merged by @U_TECH_LEAD via TeamPulse Slack action.",
    )

    # Verify Jira ticket was transitioned to Done
    mock_jira.mark_issue_done.assert_called_once_with("KAN-42")

    # Verify comment was posted to Jira
    mock_jira.add_comment.assert_called_once()
    comment_text = mock_jira.add_comment.call_args.kwargs["comment_text"]
    assert "Ticket transitioned to Done" in comment_text
    assert "U_TECH_LEAD" in comment_text

    # Verify audit record in database
    with session_factory() as session:
        record = (
            session.query(ActionTaken)
            .filter(
                ActionTaken.action_type == "pr_approve_and_merge",
                ActionTaken.target_id == "acme-org/payment-service#88",
            )
            .order_by(ActionTaken.id.desc())
            .first()
        )
        assert record is not None
        assert record.status == "executed"
        assert record.approved_by == "U_TECH_LEAD"
        assert "e4f8b9c" in record.result


@pytest.mark.asyncio
async def test_one_click_auto_merge_dry_run():
    """Test dry_run mode logs simulated merge and Done transition without API calls."""
    mock_github = MagicMock(spec=GitHubClient)
    mock_jira = MagicMock(spec=JiraClient)
    session_factory = get_session_factory()

    outcome = await handle_pr_approval_action(
        action_id="pr_approve_and_merge",
        value="acme-org/payment-service#88|KAN-42",
        user_id="U_TECH_LEAD",
        dry_run=True,
        github_client=mock_github,
        jira_client=mock_jira,
        session_factory=session_factory,
    )

    assert "[DRY RUN] Would merge acme-org/payment-service#88 to main" in outcome
    assert "and mark Jira KAN-42 as Done" in outcome
    mock_github.merge_pull_request.assert_not_called()
    mock_jira.mark_issue_done.assert_not_called()


@pytest.mark.asyncio
async def test_slack_interaction_auto_merge_block_update():
    """Test Slack card is updated in place with Done status when team lead clicks Approve & Merge."""
    mock_slack = MagicMock(spec=SlackClient)
    mock_slack.update_message = AsyncMock(return_value={"ok": True})

    interaction_payload = {
        "user": {"id": "U_LEAD"},
        "channel": {"id": "C_ALERTS"},
        "message": {
            "ts": "1719000000.999999",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "PR Ready"}},
                {"type": "actions", "block_id": "pr_actions_acme_88", "elements": []},
            ],
        },
        "actions": [
            {
                "action_id": "pr_approve_and_merge",
                "value": "acme/repo#88|KAN-42",
            }
        ],
    }

    session_factory = get_session_factory()
    with patch("app.api.routes.slack.handle_pr_approval_action", new_callable=AsyncMock) as mock_action:
        mock_action.return_value = "Merged acme/repo#88 to main (SHA: 77a88b9) & marked Jira KAN-42 as Done"

        await process_slack_interaction(
            payload=interaction_payload,
            session_factory=session_factory,
            slack_client=mock_slack,
        )

        mock_slack.update_message.assert_called_once()
        sent_blocks = mock_slack.update_message.call_args.kwargs["blocks"]
        assert sent_blocks[-1]["type"] == "context"
        context_text = sent_blocks[-1]["elements"][0]["text"]
        assert "Approved & Merged by <@U_LEAD>" in context_text
        assert "marked Jira KAN-42 as Done" in context_text


def test_api_endpoint_pr_merge():
    """Test POST /github/pr/merge endpoint."""
    with TestClient(app) as client:
        # Auth check
        res_unauth = client.post("/github/pr/merge", json={"repo": "owner/repo", "pr_number": 1})
        assert res_unauth.status_code == 401

        # Valid call with mock
        with patch("app.agents.pr_alert.handle_pr_approval_action", new_callable=AsyncMock) as mock_action:
            mock_action.return_value = "Merged owner/repo#42 to main (SHA: abc1234) & marked Jira KAN-10 as Done"

            res = client.post(
                "/github/pr/merge",
                json={
                    "repo": "owner/repo",
                    "pr_number": 42,
                    "jira_key": "KAN-10",
                    "user_id": "lead_dev",
                    "dry_run": False,
                },
                headers={"X-API-Key": "dummy-admin-key"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "success"
            assert "marked Jira KAN-10 as Done" in data["result"]
            assert data["pr_number"] == 42
