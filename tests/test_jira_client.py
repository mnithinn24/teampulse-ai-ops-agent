"""Unit tests for Jira REST client v3 with mocked HTTP transport."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
import httpx
import pytest

from app.core.errors import IntegrationError, JiraError
from app.integrations.jira_client import (
    JiraClient,
    JiraIssue,
    text_to_adf,
    validate_jira_project_key,
)


def _iso_days_ago(days: int) -> str:
    """Helper to generate ISO-8601 UTC timestamp string N days ago."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def test_text_to_adf() -> None:
    """Test text_to_adf conversion for plain strings, multiline strings, and empty input."""
    # Simple line
    adf_simple = text_to_adf("Hello World")
    assert adf_simple["version"] == 1
    assert adf_simple["type"] == "doc"
    assert len(adf_simple["content"]) == 1
    assert adf_simple["content"][0]["type"] == "paragraph"
    assert adf_simple["content"][0]["content"][0]["text"] == "Hello World"

    # Multiline
    adf_multi = text_to_adf("Line 1\nLine 2")
    assert len(adf_multi["content"]) == 2
    assert adf_multi["content"][0]["content"][0]["text"] == "Line 1"
    assert adf_multi["content"][1]["content"][0]["text"] == "Line 2"

    # Empty string
    adf_empty = text_to_adf("")
    assert adf_empty["version"] == 1
    assert len(adf_empty["content"]) == 1
    assert adf_empty["content"][0]["content"] == []


def test_validate_jira_project_key() -> None:
    """Test validation of Jira project keys and clear error message for site names."""
    # Valid project keys
    assert validate_jira_project_key("KAN") == "KAN"
    assert validate_jira_project_key("OPS") == "OPS"
    assert validate_jira_project_key("PROJ1") == "PROJ1"
    assert validate_jira_project_key("  TP  ") == "TP"
    assert validate_jira_project_key("") == ""

    # Site names / lowercase slugs
    with pytest.raises(ValueError, match="looks like a site name or URL"):
        validate_jira_project_key("teampulse-ai-ops")

    with pytest.raises(ValueError, match="looks like a site name or URL"):
        validate_jira_project_key("https://my-team.atlassian.net")

    with pytest.raises(ValueError, match="looks like a site name or URL"):
        validate_jira_project_key("teampulse.atlassian.net")

    with pytest.raises(ValueError, match="looks like a site name or URL"):
        validate_jira_project_key("kanban-board")


@pytest.mark.asyncio
async def test_search_issues_v3_pagination_across_two_pages() -> None:
    """Test searching issues via GET /rest/api/3/search/jql paginating across two pages using nextPageToken."""
    page_1_response = {
        "issues": [
            {
                "key": "OPS-101",
                "fields": {
                    "summary": "Database connection pool exhaustion",
                    "status": {"name": "In Progress"},
                    "assignee": {"displayName": "Alice Smith"},
                    "created": _iso_days_ago(25),
                    "updated": _iso_days_ago(16),  # > 14 days ago, unresolved -> stale
                    "comment": {"total": 4},
                    "project": {"key": "OPS"},
                },
            }
        ],
        "nextPageToken": "token_for_page_2",
    }

    page_2_response = {
        "issues": [
            {
                "key": "OPS-102",
                "fields": {
                    "summary": "Update readme badge",
                    "status": {"name": "In Progress"},
                    "assignee": None,
                    "created": _iso_days_ago(5),
                    "updated": _iso_days_ago(2),  # < 14 days ago -> active
                    "comment": {"total": 0},
                    "project": {"key": "OPS"},
                },
            }
        ],
        "nextPageToken": None,
    }

    requests_recorded = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/rest/api/3/search/jql" in str(request.url)
        params = dict(request.url.params)
        requests_recorded.append(params)

        assert "jql" in params
        assert "fields" in params
        assert "summary" in params["fields"]
        assert "status" in params["fields"]
        assert "assignee" in params["fields"]
        assert "created" in params["fields"]
        assert "updated" in params["fields"]
        assert "comment" in params["fields"]
        assert "priority" in params["fields"]
        assert "labels" in params["fields"]

        if params.get("nextPageToken") == "token_for_page_2":
            return httpx.Response(200, json=page_2_response)
        return httpx.Response(200, json=page_1_response)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = JiraClient(
            base_url="https://example.atlassian.net",
            email="bot@example.com",
            api_token="token123",
            stale_ticket_days=14,
            client=mock_http,
        )
        issues = await client.search_issues("project = OPS")

    assert len(requests_recorded) == 2
    assert "nextPageToken" not in requests_recorded[0]
    assert requests_recorded[1].get("nextPageToken") == "token_for_page_2"

    assert len(issues) == 2
    assert issues[0].key == "OPS-101"
    assert issues[0].is_stale is True
    assert issues[0].comment_count == 4

    assert issues[1].key == "OPS-102"
    assert issues[1].is_stale is False
    assert issues[1].comment_count == 0


@pytest.mark.asyncio
async def test_get_issue_by_key_v3() -> None:
    """Test retrieving a single issue by key using API v3."""
    single_issue = {
        "key": "DEV-55",
        "fields": {
            "summary": "Fix flaky integration test",
            "status": {"name": "To Do"},
            "assignee": {"displayName": "Charlie"},
            "created": _iso_days_ago(3),
            "updated": _iso_days_ago(1),
            "comment": {"total": 1},
            "project": {"key": "DEV"},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/rest/api/3/issue/DEV-55" in str(request.url)
        return httpx.Response(200, json=single_issue)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = JiraClient(
            base_url="https://example.atlassian.net",
            email="user@test.com",
            api_token="key",
            client=mock_http,
        )
        issue = await client.get_issue("DEV-55")

    assert issue.key == "DEV-55"
    assert issue.title == "Fix flaky integration test"
    assert issue.assignee == "Charlie"
    assert issue.is_stale is False


@pytest.mark.asyncio
async def test_add_comment_v3_uses_adf() -> None:
    """Test posting an ADF comment to /rest/api/3/issue/{key}/comment."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "/rest/api/3/issue/OPS-101/comment" in str(request.url)
        payload = request.read().decode("utf-8")
        assert '"type":"doc"' in payload
        assert '"text":"Ping for update"' in payload
        return httpx.Response(201, json={"id": "999", "body": "Ping for update"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = JiraClient(
            base_url="https://example.atlassian.net",
            client=mock_http,
        )
        res = await client.add_comment("OPS-101", "Ping for update")

    assert res["id"] == "999"


@pytest.mark.asyncio
async def test_create_issue_v3_uses_adf() -> None:
    """Test creating an issue on API v3 with ADF description."""
    created_issue = {
        "key": "OPS-200",
        "fields": {
            "summary": "Automated incident report",
            "status": {"name": "To Do"},
            "assignee": None,
            "created": _iso_days_ago(0),
            "updated": _iso_days_ago(0),
            "comment": {"total": 0},
            "project": {"key": "OPS"},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/rest/api/3/issue" in str(request.url):
            payload = request.read().decode("utf-8")
            assert '"type":"doc"' in payload
            assert '"text":"Incident description details"' in payload
            return httpx.Response(201, json={"id": "10200", "key": "OPS-200"})
        if request.method == "GET" and "/rest/api/3/issue/OPS-200" in str(request.url):
            return httpx.Response(200, json=created_issue)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = JiraClient(
            base_url="https://example.atlassian.net",
            client=mock_http,
        )
        issue = await client.create_issue(
            project_key="OPS",
            summary="Automated incident report",
            description="Incident description details",
        )

    assert issue.key == "OPS-200"
    assert issue.title == "Automated incident report"


@pytest.mark.asyncio
async def test_jira_error_wrapping() -> None:
    """Test that HTTP errors are wrapped in JiraError (IntegrationError)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errorMessages": ["Issue Does Not Exist"]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        client = JiraClient(
            base_url="https://example.atlassian.net",
            client=mock_http,
        )
        with pytest.raises(IntegrationError) as exc_info:
            await client.get_issue("INVALID-99")

        assert isinstance(exc_info.value, JiraError)
        assert "404" in str(exc_info.value)
