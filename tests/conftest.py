"""Pytest configuration and environment isolation for TeamPulse test suite."""

import socket
import urllib.request
import httpx
import pytest

from app.config import Settings, get_settings
from app.db.database import get_engine, init_db
from app.db.models import Base

# Ensure real .env is never loaded during test runs
Settings.model_config["env_file"] = None


class RealNetworkForbiddenError(RuntimeError, pytest.fail.Exception):
    """Raised when an unmocked network request is attempted to a forbidden external service."""
    pass


FORBIDDEN_EXTERNAL_DOMAINS = (
    "slack.com",
    "atlassian.net",
    "api.github.com",
    "generativelanguage.googleapis.com",
)


def _check_forbidden_target(target: str) -> None:
    if not target or not isinstance(target, str):
        return
    lowered = target.lower().strip()
    for domain in FORBIDDEN_EXTERNAL_DOMAINS:
        if domain in lowered:
            raise RealNetworkForbiddenError(
                f"Real network request to '{target}' is forbidden in tests. Mock this request instead."
            )


@pytest.fixture(autouse=True)
def block_real_external_network(monkeypatch: pytest.MonkeyPatch):
    """Autouse fixture that blocks any unmocked real network calls to external services."""
    orig_getaddrinfo = socket.getaddrinfo

    def patched_getaddrinfo(host, port, *args, **kwargs):
        _check_forbidden_target(str(host))
        return orig_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", patched_getaddrinfo)

    # Patch httpx default async transport
    orig_async_handle = httpx.AsyncHTTPTransport.handle_async_request

    async def patched_async_handle(self, request: httpx.Request, *args, **kwargs):
        _check_forbidden_target(request.url.host or "")
        return await orig_async_handle(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", patched_async_handle)

    # Patch httpx default sync transport
    orig_sync_handle = httpx.HTTPTransport.handle_request

    def patched_sync_handle(self, request: httpx.Request, *args, **kwargs):
        _check_forbidden_target(request.url.host or "")
        return orig_sync_handle(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", patched_sync_handle)

    # Patch urllib.request.urlopen
    orig_urlopen = urllib.request.urlopen

    def patched_urlopen(url, *args, **kwargs):
        target = url.full_url if hasattr(url, "full_url") else str(url)
        _check_forbidden_target(target)
        return orig_urlopen(url, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", patched_urlopen)


@pytest.fixture(autouse=True)
def isolate_test_environment(monkeypatch: pytest.MonkeyPatch):
    """Ensure tests run with dummy environment variables and isolated in-memory SQLite DB."""
    # Dummy service environment variables
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-dummy-test-token")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "dummy-signing-secret-1234567890")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C12345678")

    monkeypatch.setenv("JIRA_BASE_URL", "https://dummy.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "dummy-jira-token")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "TEST")

    monkeypatch.setenv("GITHUB_TOKEN", "dummy-github-token")
    monkeypatch.setenv("GITHUB_REPOS", "test-org/test-repo")

    monkeypatch.setenv("GEMINI_API_KEY", "dummy-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "gemini-2.0-flash")
    monkeypatch.setenv("ADMIN_API_KEY", "dummy-admin-key")

    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("DRY_RUN_ACTIONS", "true")
    monkeypatch.setenv("ENABLE_SCHEDULER", "false")
    monkeypatch.setenv("OLLAMA_ENABLED", "false")

    # Prevent loading .env
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()

    import app.db.database as db_module
    db_module.reset_db_connections()
    engine = db_module.get_engine()
    db_module.init_db(engine)

    yield

    Base.metadata.drop_all(bind=engine)
    db_module.reset_db_connections()
    get_settings.cache_clear()
