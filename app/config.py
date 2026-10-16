"""Configuration management using pydantic-settings for TeamPulse."""

from functools import lru_cache
import re
from typing import Any, List, Optional, Union
from zoneinfo import ZoneInfo
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


JIRA_KEY_REGEX = re.compile(r"^[A-Z][A-Z0-9_]{1,30}$")


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


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # GitHub Configuration
    github_token: str = Field(default="", description="GitHub Personal Access Token")
    github_repos: Union[List[str], str] = Field(
        default_factory=list,
        description="List of target repositories formatted as owner/repo",
    )

    # Jira Configuration
    jira_base_url: str = Field(default="", description="Base URL for Jira Cloud instance")
    jira_email: str = Field(default="", description="Jira user email address")
    jira_api_token: str = Field(default="", description="Jira REST API token")
    jira_project_key: str = Field(default="", description="Target Jira project key, e.g. PROJ")

    # Database Configuration
    database_url: str = Field(
        default="sqlite:///./teampulse.db",
        description="SQLAlchemy database connection URL (MySQL with pymysql or SQLite)",
    )

    # Slack Configuration
    slack_bot_token: str = Field(default="", description="Slack Bot User OAuth Token")
    slack_signing_secret: str = Field(default="", description="Slack App Signing Secret")
    slack_channel_id: str = Field(default="", description="Target Slack Channel ID")
    slack_approver_ids: Union[List[str], str] = Field(
        default_factory=list,
        description="Comma-separated Slack user IDs allowed to approve (empty allows anyone)",
    )
    max_flags_in_briefing: int = Field(
        default=10,
        description="Maximum number of flags included in the Slack briefing",
    )
    dry_run_actions: bool = Field(
        default=False,
        description="If True, logs suggested write actions without calling Jira",
    )

    # LLM Configuration
    gemini_api_key: str = Field(default="", description="Google Gemini API Key")
    gemini_model: str = Field(
        default="",
        description="Gemini model name for long-context analysis (from .env, no retired default)",
    )
    gemini_fallback_model: str = Field(
        default="",
        description="Optional fallback Gemini model name (from .env, no retired default)",
    )
    gemini_thinking_level: str = Field(
        default="low",
        description="Gemini reasoning thinking level (e.g. minimal, low, medium, high)",
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="Ollama server base URL",
    )
    ollama_model: str = Field(
        default="llama3",
        description="Local Ollama model name for quick classification",
    )
    ollama_enabled: bool = Field(
        default=True,
        description="Whether local/LAN Ollama is enabled",
    )
    llm_timeout_seconds: float = Field(
        default=60.0,
        description="Timeout in seconds for LLM API requests",
    )

    # Operational Thresholds
    stale_pr_days: int = Field(
        default=7,
        description="Number of inactive days before a PR is considered stale",
    )
    stale_ticket_days: int = Field(
        default=14,
        description="Number of inactive days before a Jira ticket is considered stale",
    )
    abandoned_pr_days: int = Field(
        default=180,
        description="Days after which a PR is classified as abandoned (urgency watch)",
    )
    max_items_in_prompt: int = Field(
        default=15,
        description="Maximum number of stale PRs and Jira issues included in the LLM prompt",
    )
    max_commit_lookups: int = Field(
        default=15,
        description="Maximum oldest stale PRs to fetch commit details for",
    )
    timezone: str = Field(
        default="Asia/Kolkata",
        description="IANA Timezone string for briefing formatting and time displays",
    )

    # Phase 5: Scheduler, Admin API, and Rate Limiting
    enable_scheduler: bool = Field(
        default=False,
        description="Whether background APScheduler is enabled",
    )
    morning_briefing_time: str = Field(
        default="09:30",
        description="Daily morning briefing trigger time (HH:MM)",
    )
    evening_briefing_time: str = Field(
        default="17:30",
        description="Daily evening briefing trigger time (HH:MM)",
    )
    admin_api_key: Optional[str] = Field(
        default=None,
        description="Secret API key required for administrative routes",
    )
    query_rate_limit_per_minute: int = Field(
        default=5,
        description="Maximum natural language queries allowed per minute per Slack user",
    )

    @field_validator("morning_briefing_time", "evening_briefing_time", mode="after")
    @classmethod
    def validate_briefing_time_format(cls, value: str) -> str:
        """Validate briefing time matches 24-hour HH:MM format."""
        parts = value.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid briefing time format '{value}', expected 'HH:MM'")
        try:
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError()
        except ValueError:
            raise ValueError(f"Invalid briefing time '{value}': hour must be 0-23, minute 0-59")
        return value.strip()

    @field_validator("timezone", mode="after")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Validate timezone string using Python's zoneinfo library."""
        try:
            ZoneInfo(value.strip())
            return value.strip()
        except Exception as exc:
            raise ValueError(f"Invalid timezone '{value}': {exc}") from exc

    @field_validator("jira_project_key", mode="after")
    @classmethod
    def validate_jira_project_key_field(cls, value: str) -> str:
        """Validate Jira project key format upon loading settings."""
        return validate_jira_project_key(value)

    @field_validator("github_repos", mode="before")
    @classmethod
    def parse_github_repos(cls, value: Any) -> List[str]:
        """Parse comma-separated repository string or list of repositories into a cleaned list.

        Args:
            value: A string or list containing repository identifiers.

        Returns:
            A cleaned list of repository strings (e.g. ['org/repo-a', 'org/repo-b']).
        """
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            return [repo.strip() for repo in value.split(",") if repo.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(repo).strip() for repo in value if str(repo).strip()]
        return []

    @field_validator("slack_approver_ids", mode="before")
    @classmethod
    def parse_slack_approver_ids(cls, value: Any) -> List[str]:
        """Parse comma-separated user IDs or list into a cleaned list.

        Args:
            value: A string or list containing Slack user IDs.

        Returns:
            Cleaned list of Slack user ID strings.
        """
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            return [uid.strip() for uid in value.split(",") if uid.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(uid).strip() for uid in value if str(uid).strip()]
        return []


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings instance.

    Returns:
        The populated Settings instance.
    """
    return Settings()
