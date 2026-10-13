"""Configuration management using pydantic-settings for TeamPulse."""

from functools import lru_cache
import re
from typing import Any, List, Union
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

    # LLM Configuration
    gemini_api_key: str = Field(default="", description="Google Gemini API Key")
    gemini_model: str = Field(
        default="gemini-1.5-pro",
        description="Gemini model name for long-context analysis",
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="Ollama server base URL",
    )
    ollama_model: str = Field(
        default="llama3",
        description="Local Ollama model name for quick classification",
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


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings instance.

    Returns:
        The populated Settings instance.
    """
    return Settings()
