"""Unit tests verifying that real network calls to external services are blocked by autouse fixture."""

import httpx
import pytest
from tests.conftest import RealNetworkForbiddenError


@pytest.mark.asyncio
async def test_real_github_http_call_fails() -> None:
    """A test that tries an unmocked real HTTP call to api.github.com fails with a clear message."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(RealNetworkForbiddenError, match="Real network request to 'api.github.com' is forbidden in tests. Mock this request instead."):
            await client.get("https://api.github.com/repos/org/repo")


def test_real_slack_http_call_fails() -> None:
    """A test that tries an unmocked real HTTP call to slack.com fails with a clear message."""
    with httpx.Client() as client:
        with pytest.raises(RealNetworkForbiddenError, match="Real network request to 'slack.com' is forbidden in tests. Mock this request instead."):
            client.get("https://slack.com/api/api.test")


@pytest.mark.asyncio
async def test_real_jira_http_call_fails() -> None:
    """A test that tries an unmocked real HTTP call to atlassian.net fails with a clear message."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(RealNetworkForbiddenError, match="Real network request to 'dummy.atlassian.net' is forbidden in tests. Mock this request instead."):
            await client.get("https://dummy.atlassian.net/rest/api/3/issue/KAN-1")


@pytest.mark.asyncio
async def test_real_gemini_http_call_fails() -> None:
    """A test that tries an unmocked real HTTP call to generativelanguage.googleapis.com fails with a clear message."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(RealNetworkForbiddenError, match="Real network request to 'generativelanguage.googleapis.com' is forbidden in tests. Mock this request instead."):
            await client.get("https://generativelanguage.googleapis.com/v1beta/models")


@pytest.mark.asyncio
async def test_mocked_calls_allowed() -> None:
    """Tests with properly mocked transports are not blocked."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"mocked": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://api.github.com/repos/org/repo")
        assert response.status_code == 200
        assert response.json() == {"mocked": True}
