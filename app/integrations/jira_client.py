"""Jira REST API v3 integration client with retry, rate limiting, and Pydantic models."""

import base64
from datetime import datetime, timezone
import logging
import re
from typing import Any, Dict, List, Optional
import httpx
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.errors import IntegrationError, JiraError
from app.core.rate_limiter import AsyncRateLimiter
from app.core.retry import retry_async

logger = logging.getLogger(__name__)

# Terminal status names that indicate a ticket is completed and thus not stale
RESOLVED_STATUSES = {"done", "closed", "resolved", "completed"}

JIRA_KEY_REGEX = re.compile(r"^[A-Z][A-Z0-9_]{1,30}$")

DEFAULT_SEARCH_FIELDS = [
    "summary",
    "status",
    "assignee",
    "created",
    "updated",
    "comment",
    "priority",
    "labels",
    "project",
]


def validate_jira_project_key(project_key: str) -> str:
    """Validate Jira project key format.

    Args:
        project_key: Key to validate (e.g. 'KAN', 'PROJ').

    Returns:
        The validated uppercase project key.

    Raises:
        ValueError: If key is invalid, especially if it looks like a site name or URL.
    """
    key = project_key.strip()
    if not key:
        return ""

    # Check if the user supplied a site name, domain, or lowercase slug instead of project key
    if (
        ".atlassian.net" in key.lower()
        or "http" in key.lower()
        or "/" in key
        or "-" in key
        or any(c.islower() for c in key)
    ):
        raise ValueError(
            f"Invalid JIRA_PROJECT_KEY '{project_key}': looks like a site name or URL. "
            f"Jira project keys must be uppercase letters and numbers (e.g. 'KAN' or 'PROJ'), "
            f"not the site name ('{project_key}')."
        )

    if not JIRA_KEY_REGEX.match(key):
        raise ValueError(
            f"Invalid JIRA_PROJECT_KEY '{project_key}'. "
            f"Jira project keys must start with an uppercase letter followed by uppercase letters or digits (e.g. 'KAN', 'PROJ')."
        )

    return key


def text_to_adf(text: str) -> Dict[str, Any]:
    """Convert plain text string to Atlassian Document Format (ADF) JSON structure.

    Args:
        text: Plain text content.

    Returns:
        ADF document dictionary required by Jira API v3.
    """
    if not text:
        return {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [],
                }
            ],
        }

    paragraphs = []
    for line in text.splitlines():
        if line:
            paragraphs.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": line}],
                }
            )
        else:
            paragraphs.append(
                {
                    "type": "paragraph",
                    "content": [],
                }
            )

    if not paragraphs:
        paragraphs.append(
            {
                "type": "paragraph",
                "content": [],
            }
        )

    return {
        "version": 1,
        "type": "doc",
        "content": paragraphs,
    }


class JiraIssue(BaseModel):
    """Pydantic model representing a Jira issue with staleness metrics."""

    key: str = Field(..., description="Jira issue key, e.g. KAN-123")
    title: str = Field(..., description="Summary title of the issue")
    status: str = Field(..., description="Current status name, e.g. In Progress")
    assignee: Optional[str] = Field(
        default=None, description="Assignee display name or email, or None if unassigned"
    )
    age_days: int = Field(..., description="Days elapsed since ticket creation")
    days_since_update: int = Field(
        ..., description="Days elapsed since the ticket was last updated"
    )
    comment_count: int = Field(default=0, description="Total number of comments on the ticket")
    is_stale: bool = Field(
        ...,
        description="True if the issue is unresolved and has had no updates past threshold",
    )
    project: Optional[str] = Field(default="", description="Jira project key")
    url: Optional[str] = Field(default="", description="Web URL to view the issue")
    created_at: Optional[datetime] = Field(default=None, description="Creation timestamp")
    updated_at: Optional[datetime] = Field(default=None, description="Last update timestamp")


class JiraClient:
    """Asynchronous client for interacting with the Jira Cloud REST API v3."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        project_key: Optional[str] = None,
        rate_limiter: Optional[AsyncRateLimiter] = None,
        stale_ticket_days: Optional[int] = None,
        timeout: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """Initialize the Jira client.

        Args:
            base_url: Jira base URL (falls back to settings.jira_base_url).
            email: Jira user email address (falls back to settings.jira_email).
            api_token: Jira API token (falls back to settings.jira_api_token).
            project_key: Default Jira project key (falls back to settings.jira_project_key).
            rate_limiter: Optional AsyncRateLimiter instance (defaults to 5 req/s).
            stale_ticket_days: Inactivity threshold for stale tickets (falls back to settings).
            timeout: Request timeout in seconds.
            client: Optional pre-configured httpx.AsyncClient instance.
        """
        settings = get_settings()
        self.base_url = (base_url or settings.jira_base_url).rstrip("/")
        self.email = email if email is not None else settings.jira_email
        self.api_token = api_token if api_token is not None else settings.jira_api_token

        raw_key = project_key if project_key is not None else settings.jira_project_key
        self.project_key = validate_jira_project_key(raw_key) if raw_key else ""

        self.stale_ticket_days = (
            stale_ticket_days if stale_ticket_days is not None else settings.stale_ticket_days
        )
        self.rate_limiter = rate_limiter or AsyncRateLimiter(rate=5.0, capacity=10.0)
        self.timeout = timeout

        self._external_client = client is not None
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(timeout=self.timeout)

    def _get_headers(self) -> Dict[str, str]:
        """Construct authorization and content headers for Jira REST API v3.

        Returns:
            Dictionary of request headers.
        """
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "TeamPulse-Ops-Agent",
        }
        if self.email and self.api_token:
            auth_str = f"{self.email}:{self.api_token}"
            encoded = base64.b64encode(auth_str.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        return headers

    @staticmethod
    def _parse_datetime(date_str: str) -> datetime:
        """Parse Jira timestamp into timezone-aware UTC datetime.

        Args:
            date_str: Jira date string (e.g., '2026-10-01T12:00:00.000+0000').

        Returns:
            Timezone-aware datetime in UTC.
        """
        clean_str = date_str.replace("Z", "+00:00")
        if "+" in clean_str and len(clean_str.split("+")[-1]) == 4:
            prefix, tz = clean_str.rsplit("+", 1)
            clean_str = f"{prefix}+{tz[:2]}:{tz[2:]}"
        return datetime.fromisoformat(clean_str)

    @staticmethod
    def _calculate_days_ago(target_time: datetime, now: Optional[datetime] = None) -> int:
        """Calculate elapsed days between target datetime and reference time.

        Args:
            target_time: Timezone-aware target datetime.
            now: Optional reference datetime (defaults to current UTC time).

        Returns:
            Non-negative integer of elapsed days.
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
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Execute rate-limited HTTP request against Jira REST API.

        Args:
            method: HTTP method (GET, POST, etc.).
            endpoint: API endpoint path (e.g. '/rest/api/3/search/jql').
            params: Optional query parameters.
            json_data: Optional JSON request payload.

        Returns:
            Parsed JSON response.

        Raises:
            JiraError: If API responds with an error or request fails.
        """
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}{endpoint}"
        headers = self._get_headers()

        async with self.rate_limiter:
            try:
                response = await self._client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                )
            except httpx.RequestError as exc:
                logger.error("Jira request to %s failed with network error: %s", url, exc)
                raise JiraError(
                    message=f"Network error connecting to Jira: {exc}",
                    details={"url": url, "error": str(exc)},
                ) from exc

        if response.is_error:
            # Allow 5xx errors to retry via retry_async
            if response.status_code >= 500:
                response.raise_for_status()

            error_msg = f"Jira API error {response.status_code}: {response.text}"
            logger.error(error_msg)
            raise JiraError(
                message=error_msg,
                details={"status_code": response.status_code, "url": url, "body": response.text},
            )

        if response.status_code == 204:
            return {}

        return response.json()

    def _parse_issue_data(self, issue_raw: Dict[str, Any]) -> JiraIssue:
        """Parse raw Jira issue JSON dict into a JiraIssue Pydantic model.

        Args:
            issue_raw: Raw issue dictionary from Jira API v3.

        Returns:
            Populated JiraIssue instance.
        """
        now = datetime.now(timezone.utc)
        key = issue_raw.get("key", "UNKNOWN")
        fields = issue_raw.get("fields", {})

        title = fields.get("summary", "")
        status_info = fields.get("status", {})
        status = status_info.get("name", "Unknown") if isinstance(status_info, dict) else "Unknown"

        assignee_info = fields.get("assignee")
        assignee = None
        if isinstance(assignee_info, dict):
            assignee = assignee_info.get("displayName") or assignee_info.get("name")

        created_str = fields.get("created")
        created_at = self._parse_datetime(created_str) if created_str else now
        age_days = self._calculate_days_ago(created_at, now)

        updated_str = fields.get("updated")
        updated_at = self._parse_datetime(updated_str) if updated_str else created_at
        days_since_update = self._calculate_days_ago(updated_at, now)

        comment_data = fields.get("comment", {})
        if isinstance(comment_data, dict):
            comment_count = comment_data.get("total", 0)
        elif isinstance(comment_data, list):
            comment_count = len(comment_data)
        else:
            comment_count = 0

        # A ticket is stale if it is unresolved and has had no updates past threshold
        is_resolved = status.lower() in RESOLVED_STATUSES
        is_stale = (not is_resolved) and (days_since_update >= self.stale_ticket_days)

        proj_info = fields.get("project", {})
        project_key = proj_info.get("key", "") if isinstance(proj_info, dict) else ""

        url = f"{self.base_url}/browse/{key}" if self.base_url else ""

        return JiraIssue(
            key=key,
            title=title,
            status=status,
            assignee=assignee,
            age_days=age_days,
            days_since_update=days_since_update,
            comment_count=comment_count,
            is_stale=is_stale,
            project=project_key,
            url=url,
            created_at=created_at,
            updated_at=updated_at,
        )

    async def search_issues(
        self,
        jql: str,
        max_results: int = 50,
        fields: Optional[List[str]] = None,
    ) -> List[JiraIssue]:
        """Search Jira issues using GET /rest/api/3/search/jql with nextPageToken pagination.

        Args:
            jql: Jira Query Language expression.
            max_results: Maximum results per page.
            fields: List of field IDs to return (without fields, only IDs are returned).

        Returns:
            List of all populated JiraIssue models across paginated pages.

        Raises:
            JiraError: If search query fails.
        """
        endpoint = "/rest/api/3/search/jql"
        field_list = fields or DEFAULT_SEARCH_FIELDS
        fields_str = ",".join(field_list)

        all_issues: List[JiraIssue] = []
        next_page_token: Optional[str] = None

        while True:
            params: Dict[str, Any] = {
                "jql": jql,
                "fields": fields_str,
                "maxResults": max_results,
            }
            if next_page_token:
                params["nextPageToken"] = next_page_token

            data = await self._request("GET", endpoint, params=params)
            raw_issues = data.get("issues", [])
            for issue in raw_issues:
                all_issues.append(self._parse_issue_data(issue))

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        return all_issues

    async def get_issue(self, issue_key: str) -> JiraIssue:
        """Retrieve details of a specific Jira issue by key using Jira API v3.

        Args:
            issue_key: Issue identifier (e.g., 'KAN-123').

        Returns:
            Populated JiraIssue model.

        Raises:
            JiraError: If issue lookup fails.
        """
        endpoint = f"/rest/api/3/issue/{issue_key}"
        params = {"fields": ",".join(DEFAULT_SEARCH_FIELDS)}
        data = await self._request("GET", endpoint, params=params)
        return self._parse_issue_data(data)

    async def add_comment(self, issue_key: str, comment_text: str) -> Dict[str, Any]:
        """Add an ADF-formatted comment to an existing Jira issue using API v3.

        Args:
            issue_key: Issue identifier (e.g., 'KAN-123').
            comment_text: Comment content in plain text (converted to ADF).

        Returns:
            Jira API response dictionary for the created comment.

        Raises:
            JiraError: If comment submission fails.
        """
        endpoint = f"/rest/api/3/issue/{issue_key}/comment"
        payload = {"body": text_to_adf(comment_text)}
        return await self._request("POST", endpoint, json_data=payload)

    async def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str = "",
        issue_type: str = "Task",
    ) -> JiraIssue:
        """Create a new Jira issue using API v3 with ADF description.

        Args:
            project_key: Jira project key where issue will be created.
            summary: Title or summary of the issue.
            description: Description of the issue (converted to ADF).
            issue_type: Issue type name (default: 'Task').

        Returns:
            Newly created JiraIssue model.

        Raises:
            JiraError: If ticket creation fails.
        """
        valid_project_key = validate_jira_project_key(project_key)
        endpoint = "/rest/api/3/issue"
        fields: Dict[str, Any] = {
            "project": {"key": valid_project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
        if description:
            fields["description"] = text_to_adf(description)

        payload = {"fields": fields}
        res = await self._request("POST", endpoint, json_data=payload)
        new_key = res.get("key")
        if new_key:
            return await self.get_issue(new_key)
        raise JiraError(
            message="Jira issue was created but response omitted key",
            details={"response": res},
        )

    async def fetch_open_issues(self, project_key: Optional[str] = None) -> List[JiraIssue]:
        """Fetch unresolved open issues for the configured or specified project.

        Args:
            project_key: Optional project key. Defaults to self.project_key.

        Returns:
            List of unresolved JiraIssue models.
        """
        target_project = project_key or self.project_key
        if not target_project:
            logger.warning("No project key specified for fetching open Jira issues")
            return []

        validated_key = validate_jira_project_key(target_project)
        jql = f"project = '{validated_key}' AND resolution = Unresolved ORDER BY updated ASC"
        return await self.search_issues(jql=jql)

    async def close(self) -> None:
        """Close the internal HTTP client session if internally created."""
        if not self._external_client:
            await self._client.aclose()

    async def __aenter__(self) -> "JiraClient":
        """Async context manager entry."""
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[object],
    ) -> None:
        """Async context manager exit."""
        await self.close()
