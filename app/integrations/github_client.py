"""GitHub REST API integration client with retry, rate limiting, and ETag caching."""

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple
import httpx
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.errors import GitHubError, GitHubRateLimitError
from app.core.rate_limiter import AsyncRateLimiter
from app.core.retry import retry_async

logger = logging.getLogger(__name__)


class PullRequest(BaseModel):
    """Pydantic representation of a GitHub Pull Request with staleness indicators."""

    id: int = Field(..., description="Unique GitHub PR ID or number")
    title: str = Field(..., description="Title of the pull request")
    author: str = Field(..., description="GitHub login of the PR author")
    age_days: int = Field(..., description="Days elapsed since PR creation")
    reviewers: List[str] = Field(
        default_factory=list,
        description="List of requested reviewers or reviewers assigned",
    )
    last_commit_days: int = Field(
        ...,
        description="Days elapsed since the most recent commit on the PR",
    )
    is_stale: bool = Field(
        ...,
        description="Flag indicating if the PR is considered stale based on configured threshold",
    )
    number: Optional[int] = Field(default=None, description="PR number within the repository")
    repo: Optional[str] = Field(default="", description="Repository name in owner/repo format")
    url: Optional[str] = Field(default="", description="HTML URL to view the PR on GitHub")
    created_at: Optional[datetime] = Field(default=None, description="PR creation timestamp")
    updated_at: Optional[datetime] = Field(default=None, description="PR last updated timestamp")


class GitHubClient:
    """Asynchronous client for interacting with the GitHub REST API.

    Features:
    - Async HTTP requests via httpx.AsyncClient
    - Token-bucket rate limiting
    - Exponential backoff retry on transient errors
    - ETag HTTP 304 caching for bandwidth & rate-limit efficiency
    """

    def __init__(
        self,
        token: Optional[str] = None,
        base_url: str = "https://api.github.com",
        rate_limiter: Optional[AsyncRateLimiter] = None,
        stale_pr_days: Optional[int] = None,
        timeout: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """Initialize the GitHub client.

        Args:
            token: GitHub personal access token (falls back to settings).
            base_url: GitHub API base URL (default: https://api.github.com).
            rate_limiter: Optional AsyncRateLimiter instance (defaults to 5 req/s).
            stale_pr_days: Days threshold for staleness (falls back to settings).
            timeout: Request timeout in seconds.
            client: Optional pre-configured httpx.AsyncClient instance.
        """
        settings = get_settings()
        self.token = token if token is not None else settings.github_token
        self.base_url = base_url.rstrip("/")
        self.stale_pr_days = (
            stale_pr_days if stale_pr_days is not None else settings.stale_pr_days
        )
        self.rate_limiter = rate_limiter or AsyncRateLimiter(rate=5.0, capacity=10.0)
        self.timeout = timeout

        self._external_client = client is not None
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(timeout=self.timeout)

        # In-memory ETag cache: url -> (etag, cached_data)
        self._etag_cache: Dict[str, Tuple[str, Any]] = {}

    def _get_headers(self, etag: Optional[str] = None) -> Dict[str, str]:
        """Construct request headers with authorization and conditional ETag if available.

        Args:
            etag: Optional cached ETag to include in If-None-Match header.

        Returns:
            Dictionary of HTTP headers.
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "TeamPulse-Ops-Agent",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if etag:
            headers["If-None-Match"] = etag
        return headers

    @staticmethod
    def _parse_datetime(date_str: str) -> datetime:
        """Parse ISO-8601 UTC timestamp string to datetime object.

        Args:
            date_str: ISO formatted timestamp (e.g., '2026-10-01T12:00:00Z').

        Returns:
            Timezone-aware datetime object in UTC.
        """
        clean_str = date_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean_str)

    @staticmethod
    def _calculate_days_ago(target_time: datetime, now: Optional[datetime] = None) -> int:
        """Calculate elapsed days between target datetime and now.

        Args:
            target_time: Timezone-aware target datetime.
            now: Optional reference datetime (defaults to current UTC time).

        Returns:
            Non-negative integer representing elapsed days.
        """
        current_time = now or datetime.now(timezone.utc)
        elapsed = current_time - target_time
        return max(0, int(elapsed.total_seconds() // 86400))

    @retry_async(
        max_retries=3,
        base_delay=1.0,
        backoff_factor=2.0,
        retryable_exceptions=(httpx.RequestError, httpx.HTTPStatusError),
    )
    async def _request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Execute a rate-limited and cached HTTP GET request against the GitHub API.

        Args:
            endpoint: API endpoint relative or absolute (e.g. '/repos/owner/repo/pulls').
            params: Optional query parameters.

        Returns:
            Parsed JSON response body or cached data.

        Raises:
            GitHubRateLimitError: If rate limit is exceeded (HTTP 403 or 429).
            GitHubError: If an unexpected HTTP or API error occurs.
        """
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}{endpoint}"
        cache_key = f"{url}?{sorted(params.items()) if params else ''}"

        cached_etag: Optional[str] = None
        if cache_key in self._etag_cache:
            cached_etag, _ = self._etag_cache[cache_key]

        headers = self._get_headers(etag=cached_etag)

        async with self.rate_limiter:
            try:
                response = await self._client.get(url, headers=headers, params=params)
            except httpx.RequestError as exc:
                logger.error("Network error requesting %s: %s", url, exc)
                raise

        # Check for conditional cache hit
        if response.status_code == 304 and cache_key in self._etag_cache:
            logger.debug("ETag cache hit for %s", url)
            _, cached_data = self._etag_cache[cache_key]
            return cached_data

        # Check for rate limiting
        if response.status_code in (403, 429):
            rate_remaining = response.headers.get("x-ratelimit-remaining")
            reset_header = response.headers.get("x-ratelimit-reset")
            reset_time = int(reset_header) if reset_header and reset_header.isdigit() else None
            if rate_remaining == "0" or "rate limit" in response.text.lower():
                raise GitHubRateLimitError(
                    message=f"GitHub rate limit exceeded for {url}",
                    reset_time=reset_time,
                    details={"url": url, "status_code": response.status_code},
                )

        if response.is_error:
            # Let 5xx trigger retry, raise GitHubError for permanent 4xx errors
            if response.status_code >= 500:
                response.raise_for_status()
            raise GitHubError(
                message=f"GitHub API error {response.status_code}: {response.text}",
                details={"status_code": response.status_code, "url": url},
            )

        data = response.json()
        etag = response.headers.get("ETag")
        if etag:
            self._etag_cache[cache_key] = (etag, data)

        return data

    async def fetch_open_prs(self, repo: str) -> List[PullRequest]:
        """Fetch all open pull requests for a given repository.

        Args:
            repo: Repository identifier in 'owner/repo' format.

        Returns:
            List of PullRequest models with age and staleness computed.

        Raises:
            GitHubError: If the repository pull requests cannot be retrieved.
        """
        endpoint = f"/repos/{repo}/pulls"
        params = {"state": "open", "per_page": 100}

        try:
            raw_prs = await self._request(endpoint, params=params)
        except Exception as exc:
            logger.error("Failed to fetch open PRs for repository %s: %s", repo, exc)
            raise

        if not isinstance(raw_prs, list):
            logger.warning("Unexpected response format from %s: %s", endpoint, raw_prs)
            return []

        now = datetime.now(timezone.utc)
        results: List[PullRequest] = []

        for pr_data in raw_prs:
            pr_id = pr_data.get("id") or pr_data.get("number", 0)
            pr_number = pr_data.get("number", 0)
            title = pr_data.get("title", "")
            user_data = pr_data.get("user") or {}
            author = user_data.get("login", "unknown")

            created_at_raw = pr_data.get("created_at")
            created_at = self._parse_datetime(created_at_raw) if created_at_raw else now
            age_days = self._calculate_days_ago(created_at, now)

            # Extract requested reviewers
            reviewers_data = pr_data.get("requested_reviewers", [])
            reviewers = [r.get("login") for r in reviewers_data if r.get("login")]

            # Determine last commit days
            # Attempt to fetch commits if available, or fall back to updated_at
            last_commit_days = age_days
            commits_url = pr_data.get("commits_url")
            if commits_url:
                try:
                    commits_data = await self._request(commits_url, params={"per_page": 50})
                    if isinstance(commits_data, list) and commits_data:
                        last_commit = commits_data[-1]
                        commit_details = last_commit.get("commit", {})
                        committer = commit_details.get("committer") or commit_details.get("author") or {}
                        commit_date_str = committer.get("date")
                        if commit_date_str:
                            last_commit_dt = self._parse_datetime(commit_date_str)
                            last_commit_days = self._calculate_days_ago(last_commit_dt, now)
                except Exception as commit_err:
                    logger.debug(
                        "Could not fetch commits for PR #%s in %s, falling back to updated_at: %s",
                        pr_number,
                        repo,
                        commit_err,
                    )
                    updated_at_raw = pr_data.get("updated_at")
                    if updated_at_raw:
                        updated_at_dt = self._parse_datetime(updated_at_raw)
                        last_commit_days = self._calculate_days_ago(updated_at_dt, now)

            # Stale if either age >= threshold or last commit age >= threshold
            is_stale = (age_days >= self.stale_pr_days) or (last_commit_days >= self.stale_pr_days)

            updated_at_raw = pr_data.get("updated_at")
            updated_at = self._parse_datetime(updated_at_raw) if updated_at_raw else None

            results.append(
                PullRequest(
                    id=pr_id,
                    number=pr_number,
                    title=title,
                    author=author,
                    age_days=age_days,
                    reviewers=reviewers,
                    last_commit_days=last_commit_days,
                    is_stale=is_stale,
                    repo=repo,
                    url=pr_data.get("html_url", ""),
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )

        return results

    async def fetch_all_open_prs(self, repos: Optional[List[str]] = None) -> List[PullRequest]:
        """Fetch open pull requests across multiple configured repositories.

        Args:
            repos: Optional list of repositories. Defaults to settings.github_repos.

        Returns:
            Consolidated list of PullRequest objects across all repositories.
        """
        settings = get_settings()
        target_repos = repos if repos is not None else settings.github_repos

        if not target_repos:
            logger.info("No repositories specified in GITHUB_REPOS")
            return []

        all_prs: List[PullRequest] = []
        for repo in target_repos:
            try:
                prs = await self.fetch_open_prs(repo)
                all_prs.extend(prs)
            except Exception as exc:
                logger.error("Failed to retrieve PRs for repository %s: %s", repo, exc)
                # Spec: 'If a source fails, mark it "unavailable" and continue'
                # We continue collecting from remaining repositories.
                continue

        return all_prs

    async def close(self) -> None:
        """Close the underlying HTTP client session if internally created."""
        if not self._external_client:
            await self._client.aclose()

    async def __aenter__(self) -> "GitHubClient":
        """Async context manager entry."""
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[object],
    ) -> None:
        """Async context manager exit, ensuring client connection is closed."""
        await self.close()
