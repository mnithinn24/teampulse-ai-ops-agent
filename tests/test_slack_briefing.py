"""Unit tests for Slack Block Kit briefing layout generation."""

from datetime import datetime, timezone
import pytest

from app.agents.briefing import (
    SLACK_MAX_BLOCKS,
    build_slack_briefing,
    extract_totals,
    format_item_link,
    truncate_slack_text,
)
from app.graph.state import TeamPulseState
from app.integrations.github_client import PullRequest
from app.integrations.jira_client import JiraIssue


def test_format_item_link() -> None:
    # GitHub PR
    assert (
        format_item_link("psf/requests#6454")
        == "<https://github.com/psf/requests/pull/6454|psf/requests#6454>"
    )
    # Jira key with base url
    assert (
        format_item_link("KAN-42", jira_base_url="https://company.atlassian.net")
        == "<https://company.atlassian.net/browse/KAN-42|KAN-42>"
    )
    # Plain string fallback
    assert format_item_link("UNKNOWN-ITEM") == "UNKNOWN-ITEM"


def test_extract_totals() -> None:
    pr1 = PullRequest(
        id=1, number=1, title="PR 1", author="alice", age_days=2,
        reviewers=[], last_commit_days=1, is_stale=False, repo="org/repo"
    )
    pr2 = PullRequest(
        id=2, number=2, title="PR 2", author="bob", age_days=10,
        reviewers=[], last_commit_days=9, is_stale=True, repo="org/repo"
    )
    issue1 = JiraIssue(
        key="KAN-1", title="Issue 1", status="Open", assignee="alice",
        age_days=15, days_since_update=15, comment_count=0, is_stale=True, project="KAN"
    )

    state = TeamPulseState(
        workspace_id="test",
        github_data=[pr1, pr2],
        jira_data=[issue1],
    )

    open_prs, stale_prs, stale_tickets = extract_totals(state)
    assert open_prs == 2
    assert stale_prs == 1
    assert stale_tickets == 1


def test_briefing_builder_no_flags() -> None:
    state = TeamPulseState(workspace_id="test")
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

    briefing = build_slack_briefing(state=state, flags_with_db=[], now=now)
    assert "All clear!" in briefing["text"]
    blocks = briefing["blocks"]

    # Header block
    assert blocks[0]["type"] == "header"
    assert "Tuesday, October 13, 2026" in blocks[0]["text"]["text"]

    # Overview line
    assert "Overview:" in blocks[1]["text"]["text"]

    # All clear section
    assert any("All clear!" in b.get("text", {}).get("text", "") for b in blocks)
    # No actions block
    assert not any(b.get("type") == "actions" for b in blocks)


def test_briefing_builder_grouping_and_buttons() -> None:
    state = TeamPulseState(workspace_id="test")
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

    flags = [
        {
            "id": 101,
            "item_id": "psf/requests#1",
            "title": "Fix memory leak",
            "reason": "Inactive for 14 days",
            "urgency": "watch",
            "suggested_action": "Ping author",
            "evidence_age_days": 14,
            "evidence_author": "alice",
        },
        {
            "id": 102,
            "item_id": "KAN-5",
            "title": "Database connection drop",
            "reason": "Active customer blocker",
            "urgency": "critical",
            "suggested_action": "Assign senior engineer",
            "evidence_age_days": 3,
            "evidence_author": "bob",
        },
        {
            "id": 103,
            "item_id": "psf/requests#2",
            "title": "Refactor auth",
            "reason": "Stale review",
            "urgency": "urgent",
            "suggested_action": "Request review",
            "evidence_age_days": 8,
            "evidence_author": "carol",
        },
    ]

    briefing = build_slack_briefing(state=state, flags_with_db=flags, now=now)
    blocks = briefing["blocks"]

    # Find the positions of the group context headers
    critical_idx = -1
    urgent_idx = -1
    watch_idx = -1

    for idx, b in enumerate(blocks):
        text = b.get("elements", [{}])[0].get("text", "") if b.get("type") == "context" else ""
        if "Critical Priority" in text:
            critical_idx = idx
        elif "Urgent Priority" in text:
            urgent_idx = idx
        elif "Watch" in text:
            watch_idx = idx

    # Critical must precede Urgent, which precedes Watch
    assert critical_idx != -1
    assert urgent_idx != -1
    assert watch_idx != -1
    assert critical_idx < urgent_idx < watch_idx

    # Check button values: MUST only contain the database ID
    action_blocks = [b for b in blocks if b.get("type") == "actions"]
    assert len(action_blocks) == 3

    for action_block in action_blocks:
        elements = action_block["elements"]
        assert len(elements) == 2
        approve_btn = elements[0]
        dismiss_btn = elements[1]

        assert approve_btn["text"]["text"] == "Approve"
        assert approve_btn["style"] == "primary"
        assert approve_btn["value"].isdigit()  # only database ID

        assert dismiss_btn["text"]["text"] == "Dismiss"
        assert dismiss_btn["value"].isdigit()  # only database ID
        assert approve_btn["value"] == dismiss_btn["value"]


def test_briefing_builder_respects_slack_limits() -> None:
    state = TeamPulseState(workspace_id="test")
    # Generate 40 flags (which would normally produce 80+ blocks)
    flags = [
        {
            "id": i,
            "item_id": f"KAN-{i}",
            "title": f"Issue #{i} " + ("x" * 200),
            "reason": "Stale ticket with very long reason " + ("y" * 300),
            "urgency": "urgent",
            "suggested_action": "Take action",
            "evidence_age_days": 10,
            "evidence_author": "dev",
        }
        for i in range(1, 41)
    ]

    briefing = build_slack_briefing(state=state, flags_with_db=flags, max_flags=30)
    blocks = briefing["blocks"]

    # Must never exceed Slack's maximum of 50 blocks
    assert len(blocks) <= SLACK_MAX_BLOCKS

    # Check safe string truncation on every text field
    for b in blocks:
        if "text" in b and isinstance(b["text"], dict):
            assert len(b["text"].get("text", "")) <= 3000


def test_three_briefing_wordings() -> None:
    """Test the three briefing wordings: flags present, 0 new flags with stale items, and all clear (0 stale items)."""
    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

    # 1. New flags present
    state_with_flags = TeamPulseState(workspace_id="test")
    flags = [
        {
            "id": 1,
            "item_id": "org/repo#10",
            "title": "PR Title",
            "reason": "Open for 10 days",
            "urgency": "critical",
            "suggested_action": "Review",
        }
    ]
    briefing1 = build_slack_briefing(state=state_with_flags, flags_with_db=flags, now=now)
    assert any("Critical Priority" in str(b) for b in briefing1["blocks"])
    assert "All clear" not in briefing1["text"]

    # 2. No new flags, but stale items exist (e.g. 3 PRs and 2 tickets still stale)
    pr1 = PullRequest(id=1, number=1, title="PR 1", author="a", age_days=20, reviewers=[], last_commit_days=20, is_stale=True, repo="org/repo")
    pr2 = PullRequest(id=2, number=2, title="PR 2", author="b", age_days=25, reviewers=[], last_commit_days=25, is_stale=True, repo="org/repo")
    pr3 = PullRequest(id=3, number=3, title="PR 3", author="c", age_days=30, reviewers=[], last_commit_days=30, is_stale=True, repo="org/repo")
    t1 = JiraIssue(key="KAN-1", title="Issue 1", status="Open", assignee=None, age_days=15, days_since_update=15, is_stale=True)
    t2 = JiraIssue(key="KAN-2", title="Issue 2", status="In Progress", assignee=None, age_days=18, days_since_update=18, is_stale=True)

    state_stale_items = TeamPulseState(
        workspace_id="test",
        github_data=[pr1, pr2, pr3],
        jira_data=[t1, t2],
    )
    briefing2 = build_slack_briefing(state=state_stale_items, flags_with_db=[], now=now)
    expected_stale_sentence = "No new items since the last briefing. 3 PRs and 2 tickets are still stale."
    assert expected_stale_sentence in briefing2["text"]
    assert any(expected_stale_sentence in b.get("text", {}).get("text", "") for b in briefing2["blocks"])
    # Must NOT show "All clear"
    assert "All clear" not in briefing2["text"]
    assert not any("All clear" in str(b) for b in briefing2["blocks"])

    # 3. All clear: no new flags AND 0 stale items (N=0, M=0)
    pr_fresh = PullRequest(id=4, number=4, title="PR Fresh", author="d", age_days=2, reviewers=[], last_commit_days=1, is_stale=False, repo="org/repo")
    t_fresh = JiraIssue(key="KAN-3", title="Issue Fresh", status="Done", assignee=None, age_days=2, days_since_update=1, is_stale=False)
    state_all_clear = TeamPulseState(
        workspace_id="test",
        github_data=[pr_fresh],
        jira_data=[t_fresh],
    )
    briefing3 = build_slack_briefing(state=state_all_clear, flags_with_db=[], now=now)
    assert "All clear!" in briefing3["text"]
    assert any("All clear!" in b.get("text", {}).get("text", "") for b in briefing3["blocks"])


def test_briefing_github_failure_warning_and_no_all_clear() -> None:
    """Requirement 1: GitHub failure produces WARNING line at start, no 'All clear', and 'unknown' counts."""
    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    t_fresh = JiraIssue(key="KAN-1", title="Fresh Issue", status="Done", assignee=None, age_days=1, days_since_update=1, is_stale=False)

    state_gh_failed = TeamPulseState(
        workspace_id="test",
        github_data={"status": "unavailable", "error": "rate limit exceeded, reset at 15:30 IST"},
        jira_data=[t_fresh],
        errors=["GitHub collection failed: rate limit exceeded, reset at 15:30 IST"],
    )

    briefing = build_slack_briefing(state=state_gh_failed, flags_with_db=[], now=now)

    # 1. Briefing fallback text must start with WARNING
    assert briefing["text"].startswith("WARNING: GitHub data unavailable (rate limit exceeded, reset at 15:30 IST). This briefing may be incomplete.")
    # 2. First block in blocks must be the WARNING section
    first_block_text = briefing["blocks"][0]["text"]["text"]
    assert first_block_text.startswith("WARNING: GitHub data unavailable (rate limit exceeded, reset at 15:30 IST). This briefing may be incomplete.")

    # 3. Must NEVER show "All clear"
    assert "All clear" not in briefing["text"]
    assert not any("All clear" in str(b) for b in briefing["blocks"])

    # 4. Must NEVER show "0 open PRs"; counts must show "unknown"
    assert "0 open PRs" not in briefing["text"]
    assert "unknown open PRs (unknown stale)" in briefing["text"]
    assert any("unknown open PRs (unknown stale)" in str(b) for b in briefing["blocks"])


def test_briefing_all_clear_only_when_all_sources_succeeded() -> None:
    """Requirement 1: 'All clear' is allowed only when every source was fetched successfully and N and M are 0."""
    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    pr_fresh = PullRequest(id=1, number=1, title="PR", author="alice", age_days=1, reviewers=[], last_commit_days=1, is_stale=False, repo="org/repo")

    # Jira failed
    state_jira_failed = TeamPulseState(
        workspace_id="test",
        github_data=[pr_fresh],
        jira_data={"status": "unavailable", "error": "auth failed: 401 Unauthorized"},
        errors=["Jira collection failed: auth failed: 401 Unauthorized"],
    )
    briefing = build_slack_briefing(state=state_jira_failed, flags_with_db=[], now=now)
    assert briefing["text"].startswith("WARNING: Jira data unavailable")
    assert "All clear" not in briefing["text"]
    assert "unknown stale tickets" in briefing["text"]

    # Both succeeded and stale items are 0
    t_fresh = JiraIssue(key="KAN-1", title="Issue", status="Done", assignee=None, age_days=1, days_since_update=1, is_stale=False)
    state_ok = TeamPulseState(
        workspace_id="test",
        github_data=[pr_fresh],
        jira_data=[t_fresh],
    )
    briefing_ok = build_slack_briefing(state=state_ok, flags_with_db=[], now=now)
    assert "All clear!" in briefing_ok["text"]
    assert not briefing_ok["text"].startswith("WARNING:")


def test_briefing_warning_never_leaks_details_or_secret_token() -> None:
    """Requirement: failure WARNING text in briefing never leaks details, bodies, dicts, or tokens."""
    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    state = TeamPulseState(
        workspace_id="test",
        github_data={
            "status": "unavailable",
            "error": "HTTP 403 Forbidden with details: {secret_token: 12345}",
        },
        jira_data=[JiraIssue(key="KAN-1", title="Issue", status="Done", age_days=1, days_since_update=1, is_stale=False)],
        errors=["GitHub collection failed: HTTP 403 Forbidden with details: {secret_token: 12345}"],
    )
    briefing = build_slack_briefing(state=state, flags_with_db=[], now=now)
    text = briefing["text"]
    blocks_str = str(briefing["blocks"])

    # Must contain safe short reason
    assert "HTTP 403 Forbidden" in text
    # Must fail if warning contains 'details' or 'secret_token'
    assert "details" not in text.lower(), f"Found 'details' in briefing text: {text}"
    assert "secret_token" not in text, f"Found 'secret_token' in briefing text: {text}"
    assert "details" not in blocks_str.lower(), f"Found 'details' in briefing blocks: {blocks_str}"
    assert "secret_token" not in blocks_str, f"Found 'secret_token' in briefing blocks: {blocks_str}"



