"""Database engine and session management for TeamPulse.

Supports MySQL (via pymysql) for production/dev and SQLite (including in-memory) for testing.
"""

from contextlib import contextmanager
import logging
from typing import Generator, Optional
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db.models import Base

logger = logging.getLogger(__name__)

# Global instances initialized on demand
_engine: Optional[Engine] = None
_SessionFactory: Optional[sessionmaker[Session]] = None


def reset_db_connections() -> None:
    """Reset cached engine and session factory (useful for test isolation)."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


def get_engine(database_url: Optional[str] = None) -> Engine:
    """Create or return existing SQLAlchemy engine.

    Handles engine configuration based on dialect:
    - SQLite in-memory: Uses StaticPool and disables thread check for tests.
    - SQLite file: Disables thread check.
    - MySQL / other: Enables pool_pre_ping and pool_recycle for connection health.

    Args:
        database_url: Optional explicit connection string. Falls back to settings.database_url.

    Returns:
        SQLAlchemy Engine instance.
    """
    global _engine
    url = database_url or get_settings().database_url

    if _engine is not None and database_url is None:
        return _engine

    if url.startswith("sqlite"):
        if ":memory:" in url:
            engine = create_engine(
                url,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            engine = create_engine(
                url,
                connect_args={"check_same_thread": False},
            )
    else:
        # MySQL or PostgreSQL connection parameters
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
        )

    if database_url is None:
        _engine = engine

    return engine


def get_session_factory(engine: Optional[Engine] = None) -> sessionmaker[Session]:
    """Get or initialize the session factory bound to the active engine.

    Args:
        engine: Optional Engine instance.

    Returns:
        Configured sessionmaker instance.
    """
    global _SessionFactory
    eng = engine or get_engine()

    if _SessionFactory is not None and engine is None:
        return _SessionFactory

    factory = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    if engine is None:
        _SessionFactory = factory

    return factory


def check_schema_drift(engine: Optional[Engine] = None) -> bool:
    """Compare ORM model columns with actual database table columns.

    Returns:
        True if database schema matches all model columns, False if columns are missing.
    """
    from sqlalchemy import inspect
    eng = engine or get_engine()
    inspector = inspect(eng)
    missing_columns = []

    for table_name, table in Base.metadata.tables.items():
        if inspector.has_table(table_name):
            db_cols = {col["name"] for col in inspector.get_columns(table_name)}
            model_cols = {col.name for col in table.columns}
            missing = model_cols - db_cols
            if missing:
                missing_columns.extend([f"{table_name}.{col}" for col in missing])

    if missing_columns:
        logger.error("Schema out of date. Run scripts/reset_db.py")
        return False
    return True


def check_unposted_pending_flags(engine: Optional[Engine] = None) -> None:
    """Log a startup warning if pending flags older than 24h without slack_ts exist."""
    from datetime import timedelta
    from sqlalchemy import func, select
    from app.core.constants import STATUS_PENDING
    from app.db.models import Flag, utc_now

    eng = engine or get_engine()
    cutoff = utc_now() - timedelta(hours=24)
    try:
        factory = get_session_factory(eng)
        with factory() as session:
            stmt = select(func.count(Flag.id)).where(
                Flag.status == STATUS_PENDING,
                Flag.slack_ts.is_(None),
                Flag.created_at < cutoff,
            )
            count = session.scalar(stmt) or 0
            if count > 0:
                logger.warning(
                    "Startup check: found %d pending flag(s) older than 24h without slack_ts.",
                    count,
                )
    except Exception as exc:
        logger.debug("Could not verify unposted pending flags on startup: %s", exc)


def init_db(engine: Optional[Engine] = None) -> bool:
    """Create all database tables on application startup and check for schema drift.

    Args:
        engine: Optional Engine instance. Defaults to configured engine.

    Returns:
        True if schema is up to date, False if columns are missing.
    """
    eng = engine or get_engine()
    logger.info("Initializing database tables for engine: %s", eng.url)
    Base.metadata.create_all(bind=eng)
    schema_ok = check_schema_drift(eng)
    check_unposted_pending_flags(eng)
    return schema_ok


@contextmanager
def get_db_session(engine: Optional[Engine] = None) -> Generator[Session, None, None]:
    """Context manager for database sessions with automatic commit/rollback.

    Yields:
        Active SQLAlchemy Session.
    """
    factory = get_session_factory(engine)
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI-compatible dependency for acquiring a database session."""
    with get_db_session() as session:
        yield session
