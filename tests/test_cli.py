"""Unit tests for the stale PR checker CLI."""

import pytest
from app.integrations.github_client import PullRequest
from scripts.check_stale_prs import format_pr_row, run_check


def test_format_pr_row_stale() -> None:
    """Test string formatting for a stale PR."""
    pr = PullRequest(
        id=42,
        number=42,
        title="Migrate database schema",
        author="charlie",
        age_days=14,
        reviewers=["dave", "eve"],
        last_commit_days=10,
        is_stale=True,
        repo="acme/service",
        url="https://github.com/acme/service/pull/42",
    )
    output = format_pr_row(pr)
    assert "[STALE] acme/service #42: Migrate database schema" in output
    assert "Author: charlie" in output
    assert "Age: 14d" in output
    assert "Last Commit: 10d ago" in output
    assert "Reviewers: dave, eve" in output
    assert "URL: https://github.com/acme/service/pull/42" in output


def test_format_pr_row_active() -> None:
    """Test string formatting for an active PR."""
    pr = PullRequest(
        id=15,
        number=15,
        title="Quick hotfix",
        author="alice",
        age_days=1,
        reviewers=[],
        last_commit_days=0,
        is_stale=False,
        repo="acme/service",
        url="https://github.com/acme/service/pull/15",
    )
    output = format_pr_row(pr)
    assert "[ACTIVE] acme/service #15: Quick hotfix" in output
    assert "Reviewers: None assigned" in output


@pytest.mark.asyncio
async def test_run_check_no_repos_configured(capsys: pytest.CaptureFixture[str]) -> None:
    """Test run_check returns exit code 1 with helpful message when no repos are provided."""
    exit_code = await run_check(repos=[])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "No repositories configured" in captured.err
