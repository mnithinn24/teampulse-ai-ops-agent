"""Unit tests for configuration management."""

import pytest
from app.config import Settings, get_settings


def test_default_settings() -> None:
    """Test default values of application settings."""
    settings = Settings()
    assert settings.stale_pr_days == 7
    assert settings.stale_ticket_days == 14
    assert settings.gemini_model == "gemini-1.5-pro"
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.ollama_model == "llama3"


def test_github_repos_parsing_comma_separated() -> None:
    """Test parsing of comma-separated string into repository list."""
    settings = Settings(github_repos="org/repo1, org/repo2, owner/repo3 ")  # type: ignore
    assert settings.github_repos == ["org/repo1", "org/repo2", "owner/repo3"]


def test_github_repos_parsing_empty_string() -> None:
    """Test parsing empty or whitespace string yields empty list."""
    settings = Settings(github_repos="   ")  # type: ignore
    assert settings.github_repos == []


def test_github_repos_parsing_list() -> None:
    """Test parsing list of repository strings directly."""
    settings = Settings(github_repos=["org/repo1", " org/repo2 "])
    assert settings.github_repos == ["org/repo1", "org/repo2"]


def test_environment_variable_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test overriding configuration settings via environment variables."""
    monkeypatch.setenv("STALE_PR_DAYS", "10")
    monkeypatch.setenv("GITHUB_TOKEN", "test-secret-token")
    monkeypatch.setenv("GITHUB_REPOS", "custom/repo-a,custom/repo-b")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-ultra-custom")

    settings = Settings()
    assert settings.stale_pr_days == 10
    assert settings.github_token == "test-secret-token"
    assert settings.github_repos == ["custom/repo-a", "custom/repo-b"]
    assert settings.gemini_model == "gemini-ultra-custom"


def test_get_settings_cached() -> None:
    """Test that get_settings returns the same cached instance."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
