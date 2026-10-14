"""Slack API integration client using slack_sdk AsyncWebClient with retry and rate limiting."""

import logging
from typing import Any, Dict, List, Optional
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from app.config import get_settings
from app.core.errors import IntegrationError, SlackError
from app.core.rate_limiter import AsyncRateLimiter
from app.core.retry import retry_async

logger = logging.getLogger(__name__)


def is_retryable_slack_error(exc: Exception) -> bool:
    """Determine if a Slack error should be retried.

    Permanent errors like channel_not_found or invalid_auth fail immediately.
    Rate limits and 5xx network issues are retried.
    """
    non_retryable = {
        "channel_not_found",
        "invalid_auth",
        "not_in_channel",
        "account_inactive",
        "token_revoked",
        "user_not_found",
        "is_archived",
        "msg_too_long",
        "no_text",
        "cant_update_message",
        "message_not_found",
    }

    # Check details dictionary if already wrapped in SlackError
    if hasattr(exc, "details") and isinstance(exc.details, dict):
        err = exc.details.get("error", "")
        if err in non_retryable:
            return False

    target_exc = exc
    if exc.__cause__ is not None and isinstance(exc.__cause__, SlackApiError):
        target_exc = exc.__cause__

    if isinstance(target_exc, SlackApiError):
        error_code = target_exc.response.get("error", "") if target_exc.response else ""
        if error_code in non_retryable:
            return False
        if error_code == "ratelimited" or (
            target_exc.response and getattr(target_exc.response, "status_code", 0) in (429, 500, 502, 503, 504)
        ):
            return True
        return False
    return True


class SlackClient:
    """Asynchronous client for Slack Web API operations."""

    def __init__(
        self,
        token: Optional[str] = None,
        client: Optional[AsyncWebClient] = None,
        rate_limit: float = 1.0,
        rate_capacity: float = 3.0,
    ) -> None:
        """Initialize SlackClient.

        Args:
            token: Slack Bot token. Falls back to settings.slack_bot_token.
            client: Optional pre-configured AsyncWebClient instance.
            rate_limit: Token bucket refill rate per second.
            rate_capacity: Token bucket burst capacity.
        """
        settings = get_settings()
        self.token = token if token is not None else settings.slack_bot_token
        self._rate_limiter = AsyncRateLimiter(rate=rate_limit, capacity=rate_capacity)

        if client is not None:
            self._client = client
        elif self.token:
            self._client = AsyncWebClient(token=self.token)
        else:
            self._client = None

    def _ensure_client(self) -> AsyncWebClient:
        """Ensure an initialized AsyncWebClient is available.

        Returns:
            Configured AsyncWebClient.

        Raises:
            SlackError: If client is not initialized or token is missing.
        """
        if self._client is None:
            raise SlackError(
                message="Slack client cannot perform request: SLACK_BOT_TOKEN is not configured."
            )
        return self._client

    @retry_async(
        max_retries=3,
        base_delay=1.0,
        backoff_factor=2.0,
        retryable_exceptions=(SlackApiError, IntegrationError),
        retry_if=is_retryable_slack_error,
    )
    async def post_message(
        self,
        channel: str,
        text: str,
        blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Post a top-level message to a Slack channel with text fallback and optional blocks.

        Args:
            channel: Target Slack channel ID or name.
            text: Plain text fallback or message text.
            blocks: Optional Block Kit layout array.

        Returns:
            Dictionary response from Slack API including 'ts' and 'channel'.

        Raises:
            SlackError: If posting fails.
        """
        client = self._ensure_client()
        await self._rate_limiter.acquire()
        try:
            logger.info("Posting message to Slack channel %s", channel)
            kwargs: Dict[str, Any] = {
                "channel": channel,
                "text": text,
                "unfurl_links": False,
                "unfurl_media": False,
            }
            if blocks is not None:
                kwargs["blocks"] = blocks
            response = await client.chat_postMessage(**kwargs)
            return response.data
        except SlackApiError as exc:
            error_code = exc.response.get("error", "unknown_error") if exc.response else "slack_error"
            logger.error("Slack chat.postMessage failed on channel %s: %s", channel, error_code)
            raise SlackError(
                message=f"Slack chat.postMessage failed: {error_code}",
                details={"channel": channel, "error": error_code, "status_code": exc.response.status_code if exc.response else None},
            ) from exc
        except Exception as exc:
            logger.error("Unexpected error during Slack post_message: %s", exc)
            raise SlackError(
                message=f"Unexpected error during Slack post_message: {exc}",
                details={"channel": channel, "error": str(exc)},
            ) from exc

    @retry_async(
        max_retries=3,
        base_delay=1.0,
        backoff_factor=2.0,
        retryable_exceptions=(SlackApiError, IntegrationError),
        retry_if=is_retryable_slack_error,
    )
    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Update an existing Slack message.

        Args:
            channel: Slack channel ID containing the message.
            ts: Message timestamp string.
            text: Updated fallback text.
            blocks: Updated Block Kit layout array.

        Returns:
            Dictionary response from Slack API.

        Raises:
            SlackError: If update fails.
        """
        client = self._ensure_client()
        await self._rate_limiter.acquire()
        try:
            logger.info("Updating message in channel %s at ts %s", channel, ts)
            kwargs: Dict[str, Any] = {
                "channel": channel,
                "ts": ts,
                "text": text,
                "unfurl_links": False,
                "unfurl_media": False,
            }
            if blocks is not None:
                kwargs["blocks"] = blocks
            response = await client.chat_update(**kwargs)
            return response.data
        except SlackApiError as exc:
            error_code = exc.response.get("error", "unknown_error") if exc.response else "slack_error"
            logger.error("Slack chat.update failed for ts %s: %s", ts, error_code)
            raise SlackError(
                message=f"Slack chat.update failed: {error_code}",
                details={"channel": channel, "ts": ts, "error": error_code},
            ) from exc
        except Exception as exc:
            logger.error("Unexpected error during Slack update_message: %s", exc)
            raise SlackError(
                message=f"Unexpected error during Slack update_message: {exc}",
                details={"channel": channel, "ts": ts, "error": str(exc)},
            ) from exc

    @retry_async(
        max_retries=3,
        base_delay=1.0,
        backoff_factor=2.0,
        retryable_exceptions=(SlackApiError, IntegrationError),
        retry_if=is_retryable_slack_error,
    )
    async def post_thread_reply(
        self,
        channel: str,
        thread_ts: str,
        text: str,
        blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Post a threaded reply to an existing message.

        Args:
            channel: Slack channel ID.
            thread_ts: Parent message timestamp.
            text: Reply message text.
            blocks: Optional Block Kit layout array.

        Returns:
            Dictionary response from Slack API.

        Raises:
            SlackError: If reply fails.
        """
        client = self._ensure_client()
        await self._rate_limiter.acquire()
        try:
            logger.info("Posting thread reply to channel %s thread %s", channel, thread_ts)
            kwargs: Dict[str, Any] = {
                "channel": channel,
                "thread_ts": thread_ts,
                "text": text,
                "unfurl_links": False,
                "unfurl_media": False,
            }
            if blocks is not None:
                kwargs["blocks"] = blocks
            response = await client.chat_postMessage(**kwargs)
            return response.data
        except SlackApiError as exc:
            error_code = exc.response.get("error", "unknown_error") if exc.response else "slack_error"
            logger.error("Slack thread reply failed: %s", error_code)
            raise SlackError(
                message=f"Slack thread reply failed: {error_code}",
                details={"channel": channel, "thread_ts": thread_ts, "error": error_code},
            ) from exc
        except Exception as exc:
            logger.error("Unexpected error during Slack post_thread_reply: %s", exc)
            raise SlackError(
                message=f"Unexpected error during Slack post_thread_reply: {exc}",
                details={"channel": channel, "thread_ts": thread_ts, "error": str(exc)},
            ) from exc

    @retry_async(
        max_retries=3,
        base_delay=1.0,
        backoff_factor=2.0,
        retryable_exceptions=(SlackApiError, IntegrationError),
        retry_if=is_retryable_slack_error,
    )
    async def post_ephemeral(
        self,
        channel: str,
        user: str,
        text: str,
        blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Post an ephemeral message visible only to a specific user.

        Args:
            channel: Slack channel ID.
            user: Slack user ID.
            text: Ephemeral message text.
            blocks: Optional Block Kit layout array.

        Returns:
            Dictionary response from Slack API.

        Raises:
            SlackError: If posting fails.
        """
        client = self._ensure_client()
        await self._rate_limiter.acquire()
        try:
            logger.info("Posting ephemeral message to user %s in channel %s", user, channel)
            kwargs: Dict[str, Any] = {"channel": channel, "user": user, "text": text}
            if blocks is not None:
                kwargs["blocks"] = blocks
            response = await client.chat_postEphemeral(**kwargs)
            return response.data
        except SlackApiError as exc:
            error_code = exc.response.get("error", "unknown_error") if exc.response else "slack_error"
            logger.error("Slack chat.postEphemeral failed: %s", error_code)
            raise SlackError(
                message=f"Slack chat.postEphemeral failed: {error_code}",
                details={"channel": channel, "user": user, "error": error_code},
            ) from exc
        except Exception as exc:
            logger.error("Unexpected error during Slack post_ephemeral: %s", exc)
            raise SlackError(
                message=f"Unexpected error during Slack post_ephemeral: {exc}",
                details={"channel": channel, "user": user, "error": str(exc)},
            ) from exc
