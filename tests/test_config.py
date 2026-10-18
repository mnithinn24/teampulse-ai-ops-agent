"""Unit tests for configuration management."""

import pytest
from pydantic import ValidationError
from app.config import Settings, get_settings


def test_default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test default values of application settings."""
    monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("DRY_RUN_ACTIONS", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("SLACK_APPROVER_IDS", raising=False)
    monkeypatch.delenv("JIRA_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("JIRA_REOPEN_TRANSITION_ID", raising=False)
    settings = Settings(_env_file=None)
    assert settings.stale_pr_days == 7
    assert settings.stale_ticket_days == 14
    assert settings.jira_project_key == ""
    assert settings.gemini_model == ""
    assert settings.gemini_fallback_model == ""
    assert settings.gemini_thinking_level == "low"
    assert settings.slack_approver_ids == []
    assert settings.max_flags_in_briefing == 10
    assert settings.dry_run_actions is False
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.ollama_model == "llama3"
    assert settings.abandoned_pr_days == 180
    assert settings.max_items_in_prompt == 15


def test_slack_approver_ids_parsing() -> None:
    """Test parsing comma-separated Slack approver user IDs into list."""
    settings = Settings(slack_approver_ids=" U1234, U5678 , U9999 ", _env_file=None)  # type: ignore
    assert settings.slack_approver_ids == ["U1234", "U5678", "U9999"]



def test_github_repos_parsing_comma_separated() -> None:
    """Test parsing of comma-separated string into repository list."""
    settings = Settings(github_repos="org/repo1, org/repo2, owner/repo3 ", _env_file=None)  # type: ignore
    assert settings.github_repos == ["org/repo1", "org/repo2", "owner/repo3"]


def test_github_repos_parsing_empty_string() -> None:
    """Test parsing empty or whitespace string yields empty list."""
    settings = Settings(github_repos="   ", _env_file=None)  # type: ignore
    assert settings.github_repos == []


def test_github_repos_parsing_list() -> None:
    """Test parsing list of repository strings directly."""
    settings = Settings(github_repos=["org/repo1", " org/repo2 "], _env_file=None)
    assert settings.github_repos == ["org/repo1", "org/repo2"]


def test_environment_variable_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test overriding configuration settings via environment variables."""
    monkeypatch.setenv("STALE_PR_DAYS", "10")
    monkeypatch.setenv("GITHUB_TOKEN", "test-secret-token")
    monkeypatch.setenv("GITHUB_REPOS", "custom/repo-a,custom/repo-b")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "KAN")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-ultra-custom")

    settings = Settings(_env_file=None)
    assert settings.stale_pr_days == 10
    assert settings.github_token == "test-secret-token"
    assert settings.github_repos == ["custom/repo-a", "custom/repo-b"]
    assert settings.jira_project_key == "KAN"
    assert settings.gemini_model == "gemini-ultra-custom"


def test_jira_project_key_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test validation of JIRA_PROJECT_KEY in settings."""
    monkeypatch.setenv("JIRA_PROJECT_KEY", "teampulse-ai-ops")
    with pytest.raises(ValidationError, match="looks like a site name or URL"):
        Settings(_env_file=None)

    monkeypatch.setenv("JIRA_PROJECT_KEY", "OPS")
    valid_settings = Settings(_env_file=None)
    assert valid_settings.jira_project_key == "OPS"


def test_get_settings_cached() -> None:
    """Test that get_settings returns the same cached instance."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
