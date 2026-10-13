"""Pytest configuration and environment isolation for TeamPulse test suite."""

import pytest
from app.config import get_settings


@pytest.fixture(autouse=True)
def isolate_test_environment(monkeypatch: pytest.MonkeyPatch):
    """Ensure tests run with valid, isolated environment variables regardless of local .env contents."""
    monkeypatch.setenv("JIRA_PROJECT_KEY", "TEST")
    monkeypatch.setenv("GITHUB_REPOS", "test-org/test-repo")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
