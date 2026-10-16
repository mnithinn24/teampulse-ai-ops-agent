"""Custom error hierarchy for TeamPulse."""

from typing import Optional


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
        """Return string representation of the error."""
        if self.details:
            return f"{self.message} (details: {self.details})"
        return self.message


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

