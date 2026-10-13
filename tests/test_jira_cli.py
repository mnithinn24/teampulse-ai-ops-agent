"""Unit tests for the stale Jira tickets CLI script."""

import pytest
from app.integrations.jira_client import JiraIssue
from scripts.check_stale_tickets import format_ticket_row, run_check_tickets


def test_format_ticket_row_stale() -> None:
    """Test CLI formatting of a stale Jira ticket."""
    issue = JiraIssue(
        key="BACK-77",
        title="Refactor auth token expiration",
        status="In Progress",
        assignee="Dev Dave",
        age_days=30,
        days_since_update=20,
        comment_count=5,
        is_stale=True,
        project="BACK",
        url="https://example.atlassian.net/browse/BACK-77",
    )
    output = format_ticket_row(issue)
    assert "[STALE] [BACK-77] Refactor auth token expiration" in output
    assert "Status: In Progress" in output
    assert "Assignee: Dev Dave" in output
    assert "Age: 30d" in output
    assert "Last Update: 20d ago" in output
    assert "Comments: 5" in output
    assert "URL: https://example.atlassian.net/browse/BACK-77" in output


def test_format_ticket_row_active() -> None:
    """Test CLI formatting of an active Jira ticket."""
    issue = JiraIssue(
        key="BACK-78",
        title="Quick documentation fix",
        status="To Do",
        assignee=None,
        age_days=2,
        days_since_update=1,
        comment_count=0,
        is_stale=False,
        project="BACK",
        url="https://example.atlassian.net/browse/BACK-78",
    )
    output = format_ticket_row(issue)
    assert "[ACTIVE] [BACK-78] Quick documentation fix" in output
    assert "Assignee: Unassigned" in output


@pytest.mark.asyncio
async def test_run_check_tickets_no_project(capsys: pytest.CaptureFixture[str]) -> None:
    """Test run_check_tickets returns exit code 1 when no project key is provided."""
    exit_code = await run_check_tickets(project_key="")
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "No Jira project key specified" in captured.err


@pytest.mark.asyncio
async def test_run_check_tickets_invalid_site_name(capsys: pytest.CaptureFixture[str]) -> None:
    """Test run_check_tickets rejects site name with clear error message."""
    exit_code = await run_check_tickets(project_key="teampulse-ai-ops")
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "looks like a site name or URL" in captured.err
