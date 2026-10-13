"""Unit tests for GitHub REST client with mocked HTTP transport."""

from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, List
import httpx
import pytest

from app.core.errors import GitHubError, GitHubRateLimitError
from app.integrations.github_client import GitHubClient, PullRequest


def _iso_days_ago(days: int) -> str:
    """Helper to return ISO-8601 UTC timestamp string N days ago."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.asyncio
async def test_fetch_open_prs_success() -> None:
    """Test fetching open pull requests successfully with commit calculation."""
    sample_prs = [
        {
            "id": 101,
            "number": 12,
            "title": "Add Ops Agent feature",
            "user": {"login": "alice"},
            "created_at": _iso_days_ago(10),
            "updated_at": _iso_days_ago(8),
            "html_url": "https://github.com/org/repo/pull/12",
            "requested_reviewers": [{"login": "bob"}, {"login": "carol"}],
            "commits_url": "https://api.github.com/repos/org/repo/pulls/12/commits",
        },
        {
            "id": 102,
            "number": 13,
            "title": "Fix logging typo",
            "user": {"login": "dave"},
            "created_at": _iso_days_ago(2),
            "updated_at": _iso_days_ago(1),
            "html_url": "https://github.com/org/repo/pull/13",
            "requested_reviewers": [],
            "commits_url": "https://api.github.com/repos/org/repo/pulls/13/commits",
        },
    ]

    commits_pr12 = [
        {
            "sha": "abc1234",
            "commit": {
                "committer": {"date": _iso_days_ago(8)},
                "author": {"date": _iso_days_ago(8)},
            },
        }
    ]

    commits_pr13 = [
        {
            "sha": "def5678",
            "commit": {
                "committer": {"date": _iso_days_ago(1)},
                "author": {"date": _iso_days_ago(1)},
            },
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/repos/org/repo/pulls/12/commits" in url_str:
            return httpx.Response(200, json=commits_pr12)
        if "/repos/org/repo/pulls/13/commits" in url_str:
            return httpx.Response(200, json=commits_pr13)
        if "/repos/org/repo/pulls" in url_str:
            return httpx.Response(200, json=sample_prs)
        return httpx.Response(404, json={"message": "Not found"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = GitHubClient(token="test-token", stale_pr_days=7, client=mock_http)
        prs = await client.fetch_open_prs("org/repo")

    assert len(prs) == 2

    pr1 = prs[0]
    assert pr1.id == 101
    assert pr1.number == 12
    assert pr1.title == "Add Ops Agent feature"
    assert pr1.author == "alice"
    assert pr1.reviewers == ["bob", "carol"]
    assert pr1.age_days >= 10
    assert pr1.last_commit_days >= 8
    assert pr1.is_stale is True  # last commit > 7 days ago

    pr2 = prs[1]
    assert pr2.id == 102
    assert pr2.number == 13
    assert pr2.author == "dave"
    assert pr2.reviewers == []
    assert pr2.age_days <= 3
    assert pr2.last_commit_days <= 2
    assert pr2.is_stale is False  # active, within 7 days


@pytest.mark.asyncio
async def test_etag_caching() -> None:
    """Test that ETag is stored and 304 response returns cached data."""
    sample_prs = [
        {
            "id": 501,
            "number": 1,
            "title": "Initial Draft",
            "user": {"login": "tester"},
            "created_at": _iso_days_ago(1),
            "updated_at": _iso_days_ago(1),
            "html_url": "https://github.com/test/cache-repo/pull/1",
            "requested_reviewers": [],
            "commits_url": "",
        }
    ]

    request_count = 0
    received_if_none_match = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        etag_header = request.headers.get("if-none-match")
        received_if_none_match.append(etag_header)

        if etag_header == 'W/"etag-12345"':
            return httpx.Response(304, headers={"ETag": 'W/"etag-12345"'})

        return httpx.Response(
            200,
            json=sample_prs,
            headers={"ETag": 'W/"etag-12345"'},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = GitHubClient(token="test-token", client=mock_http)

        # 1st call: returns 200, caches ETag
        prs_first = await client.fetch_open_prs("test/cache-repo")
        assert len(prs_first) == 1
        assert received_if_none_match[0] is None

        # 2nd call: sends If-None-Match, returns 304, retrieves from cache
        prs_second = await client.fetch_open_prs("test/cache-repo")
        assert len(prs_second) == 1
        assert received_if_none_match[1] == 'W/"etag-12345"'
        assert prs_second[0].id == prs_first[0].id


@pytest.mark.asyncio
async def test_github_rate_limit_error() -> None:
    """Test that HTTP 403 with rate limit exhaustion raises GitHubRateLimitError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            text="API rate limit exceeded for user",
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "1700000000",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = GitHubClient(token="test-token", client=mock_http)
        with pytest.raises(GitHubRateLimitError) as exc_info:
            await client.fetch_open_prs("org/repo")

        assert exc_info.value.reset_time == 1700000000


@pytest.mark.asyncio
async def test_github_api_error_raises_github_error() -> None:
    """Test that standard 4xx errors raise GitHubError without infinite retry."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = GitHubClient(token="test-token", client=mock_http)
        with pytest.raises(GitHubError) as exc_info:
            await client.fetch_open_prs("nonexistent/repo")

        assert "404" in str(exc_info.value)


@pytest.mark.asyncio
async def test_fetch_all_open_prs_continues_on_failure() -> None:
    """Test that fetch_all_open_prs continues fetching other repos when one repo fails."""
    repo_good_prs = [
        {
            "id": 999,
            "number": 99,
            "title": "Healthy Repo PR",
            "user": {"login": "good-dev"},
            "created_at": _iso_days_ago(3),
            "updated_at": _iso_days_ago(2),
            "html_url": "https://github.com/org/good/pull/99",
            "requested_reviewers": [],
            "commits_url": "",
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/repos/org/broken/pulls" in url_str:
            return httpx.Response(404, json={"message": "Repository not found"})
        if "/repos/org/good/pulls" in url_str:
            return httpx.Response(200, json=repo_good_prs)
        return httpx.Response(404, json={"message": "Unknown"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = GitHubClient(token="test-token", client=mock_http)
        all_prs = await client.fetch_all_open_prs(repos=["org/broken", "org/good"])

    # Gracefully skipped broken repo, returned good repo PRs
    assert len(all_prs) == 1
    assert all_prs[0].id == 999
    assert all_prs[0].repo == "org/good"
