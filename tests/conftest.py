"""Pytest configuration and environment isolation for TeamPulse test suite."""

import pytest
from app.config import get_settings
from app.db.database import get_engine, init_db
from app.db.models import Base


@pytest.fixture(autouse=True)
def isolate_test_environment(monkeypatch: pytest.MonkeyPatch):
    """Ensure tests run with valid, isolated in-memory SQLite database and test settings."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "TEST")
    monkeypatch.setenv("GITHUB_REPOS", "test-org/test-repo")
    monkeypatch.setenv("DRY_RUN_ACTIONS", "false")
    get_settings.cache_clear()

    import app.db.database as db_module
    db_module.reset_db_connections()
    engine = db_module.get_engine()
    db_module.init_db(engine)

    yield

    Base.metadata.drop_all(bind=engine)
    db_module.reset_db_connections()
    get_settings.cache_clear()
