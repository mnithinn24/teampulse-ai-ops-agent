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


def init_db(engine: Optional[Engine] = None) -> None:
    """Create all database tables on application startup.

    Args:
        engine: Optional Engine instance. Defaults to configured engine.
    """
    eng = engine or get_engine()
    logger.info("Initializing database tables for engine: %s", eng.url)
    Base.metadata.create_all(bind=eng)


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
