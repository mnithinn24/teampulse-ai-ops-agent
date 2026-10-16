import re
from typing import Any, Optional


class TeamPulseError(Exception):
    """Base exception for all TeamPulse application errors."""

    def __init__(self, message: str, details: Optional[dict] = None) -> None:
        """Initialize TeamPulseError.

        Args:
            message: Human-readable error description.
            details: Optional dictionary with additional error context or diagnostic data.
        """
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        """Return string representation of the error without leaking details dict."""
        return self.message


def format_log_error(exc: Any, max_len: int = 200) -> str:
    """Format an exception or message for single-line logging.

    Cuts message to at most max_len characters ending with '...'.
    Never logs response bodies or the 'details' dict.
    """
    if exc is None:
        return ""

    if hasattr(exc, "message") and isinstance(exc.message, str):
        msg = exc.message
    else:
        msg = str(exc)

    # Strip details dict patterns and response bodies
    msg = re.sub(r"\(details:.*?\)", "", msg, flags=re.DOTALL)
    msg = re.sub(r"(?:with\s+)?details\s*[:=]\s*\{.*?\}", "", msg, flags=re.DOTALL | re.IGNORECASE)
    msg = re.sub(r"details=\{.*?\}", "", msg, flags=re.DOTALL)
    msg = re.sub(r"'details':\s*\{.*?\}", "", msg, flags=re.DOTALL)
    msg = re.sub(r"'raw_text':\s*'.*?'", "", msg, flags=re.DOTALL)
    msg = re.sub(r"'raw_response':\s*\{.*?\}", "", msg, flags=re.DOTALL)
    msg = re.sub(r"'response_json':\s*\{.*?\}", "", msg, flags=re.DOTALL)
    msg = re.sub(r"(?:token|secret|password|key)\s*[:=]\s*['\"]?[^\s,'\"\}]+['\"]?", "[MASKED]", msg, flags=re.IGNORECASE)

    cleaned = msg.replace("\n", " ").replace("\r", " ").strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;:-")

    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3] + "..."
    return cleaned


class IntegrationError(TeamPulseError):
    """Raised when an external service integration (GitHub, Jira, Slack) fails."""


class GitHubError(IntegrationError):
    """Raised when GitHub API operations fail."""


class GitHubRateLimitError(GitHubError):
    """Raised when GitHub API rate limit is exceeded."""

    def __init__(
        self,
        message: str = "GitHub API rate limit exceeded",
        reset_time: Optional[int] = None,
        details: Optional[dict] = None,
    ) -> None:
        """Initialize GitHubRateLimitError.

        Args:
            message: Description of the rate limit error.
            reset_time: Unix timestamp when the rate limit resets.
            details: Optional error context.
        """
        merged_details = details or {}
        if reset_time is not None:
            merged_details["reset_time"] = reset_time
        super().__init__(message=message, details=merged_details)
        self.reset_time = reset_time


class JiraError(IntegrationError):
    """Raised when Jira API operations fail."""


class SlackError(IntegrationError):
    """Raised when Slack API operations fail."""


class AnalysisError(TeamPulseError):
    """Raised when LLM analysis, prompt validation, or parsing fails."""


class InvalidJSONError(AnalysisError):
    """Raised when LLM returns malformed JSON or invalid schema output."""


class ActionError(TeamPulseError):
    """Raised when an action (e.g., Jira ticket creation, comment, or notification) fails."""


class ConfigurationError(TeamPulseError):
    """Raised when configuration validation or environment variables are missing/invalid."""


class PipelineConcurrencyError(TeamPulseError):
    """Raised when a pipeline run is triggered while another run is already in progress for that workspace."""

