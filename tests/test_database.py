"""Unit tests for SQLAlchemy models and database management using in-memory SQLite."""

from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select

from app.db.database import get_db_session, get_engine, init_db
from app.db.models import ActionTaken, AgentRun, Flag, Workspace


@pytest.fixture
def sqlite_engine():
    """Create a temporary in-memory SQLite engine for tests."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    yield engine
    engine.dispose()


def test_workspace_crud(sqlite_engine) -> None:
    """Test creating and retrieving Workspace record."""
    with get_db_session(sqlite_engine) as session:
        ws = Workspace(
            workspace_id="team-alpha",
            name="Alpha Team",
            github_repos="org/repo-a,org/repo-b",
            jira_project_key="ALPHA",
        )
        session.add(ws)

    with get_db_session(sqlite_engine) as session:
        saved = session.scalar(select(Workspace).where(Workspace.workspace_id == "team-alpha"))
        assert saved is not None
        assert saved.name == "Alpha Team"
        assert saved.jira_project_key == "ALPHA"
        assert "org/repo-a" in saved.github_repos


def test_agent_run_crud(sqlite_engine) -> None:
    """Test creating and retrieving AgentRun record."""
    with get_db_session(sqlite_engine) as session:
        run = AgentRun(
            workspace_id="team-alpha",
            run_type="morning",
            status="completed",
            analysis='{"summary": "All good"}',
            briefing='{"text": "Briefing"}',
        )
        session.add(run)

    with get_db_session(sqlite_engine) as session:
        runs = session.scalars(select(AgentRun).where(AgentRun.workspace_id == "team-alpha")).all()
        assert len(runs) == 1
        assert runs[0].run_type == "morning"
        assert runs[0].status == "completed"


def test_flag_filtering_last_24h(sqlite_engine) -> None:
    """Test creating flags and querying those within the last 24 hours."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    recent_time = now - timedelta(hours=2)
    old_time = now - timedelta(hours=36)

    with get_db_session(sqlite_engine) as session:
        recent_flag = Flag(
            workspace_id="team-alpha",
            item_type="github_pr",
            item_id="org/repo#10",
            flag_reason="stale",
            flag_level="normal",
            created_at=recent_time,
        )
        old_flag = Flag(
            workspace_id="team-alpha",
            item_type="jira_ticket",
            item_id="ALPHA-99",
            flag_reason="blocker",
            flag_level="escalated",
            created_at=old_time,
        )
        session.add_all([recent_flag, old_flag])

    # Query flags created in the last 24 hours
    cutoff = now - timedelta(hours=24)
    with get_db_session(sqlite_engine) as session:
        active_flags = session.scalars(
            select(Flag).where(
                Flag.workspace_id == "team-alpha",
                Flag.created_at >= cutoff,
            )
        ).all()

        assert len(active_flags) == 1
        assert active_flags[0].item_id == "org/repo#10"


def test_actions_taken_crud(sqlite_engine) -> None:
    """Test logging actions and updating approval status."""
    with get_db_session(sqlite_engine) as session:
        action = ActionTaken(
            workspace_id="team-alpha",
            action_type="jira_comment",
            target_id="ALPHA-12",
            payload='{"comment": "Please provide an update."}',
            status="suggested",
        )
        session.add(action)

    with get_db_session(sqlite_engine) as session:
        saved_action = session.scalar(select(ActionTaken).where(ActionTaken.target_id == "ALPHA-12"))
        assert saved_action is not None
        assert saved_action.status == "suggested"

        # Simulate Slack button approval
        saved_action.status = "approved"
        saved_action.approved_by = "U1234567"
        saved_action.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)

    with get_db_session(sqlite_engine) as session:
        updated = session.scalar(select(ActionTaken).where(ActionTaken.target_id == "ALPHA-12"))
        assert updated.status == "approved"
        assert updated.approved_by == "U1234567"
        assert updated.executed_at is not None


def test_db_session_rollback_on_exception(sqlite_engine) -> None:
    """Test that session automatically rolls back changes when an exception is raised."""
    try:
        with get_db_session(sqlite_engine) as session:
            ws = Workspace(
                workspace_id="fail-ws",
                name="Fail Workspace",
            )
            session.add(ws)
            raise RuntimeError("Forced error to test rollback")
    except RuntimeError:
        pass

    with get_db_session(sqlite_engine) as session:
        found = session.scalar(select(Workspace).where(Workspace.workspace_id == "fail-ws"))
        assert found is None
