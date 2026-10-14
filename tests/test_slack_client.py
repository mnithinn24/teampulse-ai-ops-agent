"""Unit tests for SlackClient integration with retry and rate limiting."""

from unittest.mock import AsyncMock, patch
import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from app.core.errors import IntegrationError, SlackError
from app.integrations.slack_client import SlackClient, is_retryable_slack_error


@pytest.fixture
def mock_web_client() -> AsyncMock:
    client = AsyncMock(spec=AsyncWebClient)
    client.chat_postMessage.return_value = AsyncMock(
        data={"ok": True, "channel": "C123", "ts": "1234567890.123456"}
    )
    client.chat_update.return_value = AsyncMock(
        data={"ok": True, "channel": "C123", "ts": "1234567890.123456"}
    )
    client.chat_postEphemeral.return_value = AsyncMock(
        data={"ok": True, "message_ts": "1234567890.123456"}
    )
    return client


@pytest.mark.asyncio
async def test_post_message_success(mock_web_client: AsyncMock) -> None:
    client = SlackClient(token="xoxb-test", client=mock_web_client)
    res = await client.post_message(
        channel="C123",
        text="Fallback text",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "Hello"}}],
    )

    assert res["ok"] is True
    assert res["ts"] == "1234567890.123456"
    mock_web_client.chat_postMessage.assert_awaited_once_with(
        channel="C123",
        text="Fallback text",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "Hello"}}],
        unfurl_links=False,
        unfurl_media=False,
    )


@pytest.mark.asyncio
async def test_update_message_success(mock_web_client: AsyncMock) -> None:
    client = SlackClient(token="xoxb-test", client=mock_web_client)
    res = await client.update_message(
        channel="C123",
        ts="1234567890.123456",
        text="Updated text",
        blocks=[],
    )

    assert res["ok"] is True
    mock_web_client.chat_update.assert_awaited_once_with(
        channel="C123",
        ts="1234567890.123456",
        text="Updated text",
        blocks=[],
        unfurl_links=False,
        unfurl_media=False,
    )


@pytest.mark.asyncio
async def test_post_thread_reply_success(mock_web_client: AsyncMock) -> None:
    client = SlackClient(token="xoxb-test", client=mock_web_client)
    res = await client.post_thread_reply(
        channel="C123",
        thread_ts="1234567890.123456",
        text="Thread reply",
    )

    assert res["ok"] is True
    mock_web_client.chat_postMessage.assert_awaited_once_with(
        channel="C123",
        thread_ts="1234567890.123456",
        text="Thread reply",
        unfurl_links=False,
        unfurl_media=False,
    )


@pytest.mark.asyncio
async def test_slack_error_wrapping(mock_web_client: AsyncMock) -> None:
    # Simulate non-retryable channel_not_found error
    mock_response = {"ok": False, "error": "channel_not_found"}
    mock_web_client.chat_postMessage.side_effect = SlackApiError(
        message="The request to Slack API failed: channel_not_found",
        response=AsyncMock(get=lambda k, default=None: mock_response.get(k, default), status_code=200),
    )

    client = SlackClient(token="xoxb-test", client=mock_web_client)
    with pytest.raises(SlackError) as exc_info:
        await client.post_message(channel="invalid_channel", text="Test")

    assert "channel_not_found" in str(exc_info.value)
    assert isinstance(exc_info.value, IntegrationError)
    # Permanent error should not be retried multiple times
    assert mock_web_client.chat_postMessage.await_count == 1


def test_is_retryable_slack_error_logic() -> None:
    # Permanent error -> False
    perm_err = SlackApiError(
        message="channel_not_found",
        response=AsyncMock(get=lambda k, d=None: "channel_not_found" if k == "error" else d, status_code=200),
    )
    assert is_retryable_slack_error(perm_err) is False

    # Rate limit -> True
    rate_err = SlackApiError(
        message="ratelimited",
        response=AsyncMock(get=lambda k, d=None: "ratelimited" if k == "error" else d, status_code=429),
    )
    assert is_retryable_slack_error(rate_err) is True
