"""Unit tests for configuration management."""

import pytest
from pydantic import ValidationError
from app.config import Settings, get_settings


def test_default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test default values of application settings."""
    monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)
    settings = Settings(_env_file=None)
    assert settings.stale_pr_days == 7
    assert settings.stale_ticket_days == 14
    assert settings.jira_project_key == ""
    assert settings.gemini_model == "gemini-1.5-pro"
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.ollama_model == "llama3"


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
