"""Script to drop and recreate all database tables for local development."""

import argparse
from pathlib import Path
import sys

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.config import get_settings
from app.db.database import get_engine, reset_db_connections
from app.db.models import Base


def is_local_database_url(url: str) -> bool:
    """Check if database URL points to localhost, 127.0.0.1, ::1 or SQLite."""
    if not url:
        return True
    url_lower = url.lower()
    if url_lower.startswith("sqlite"):
        return True

    # Check host for MySQL / Postgres
    try:
        from sqlalchemy.engine import make_url
        parsed = make_url(url)
        host = (parsed.host or "").lower()
        if host in ("localhost", "127.0.0.1", "::1", ""):
            return True
        return False
    except Exception:
        return any(h in url_lower for h in ("localhost", "127.0.0.1", "::1"))


def reset_database(force: bool = False, database_url: str = None) -> int:
    """Drop and recreate all database tables.

    Requires force=True (--yes flag).
    Refuses to run if database URL does not point to localhost or sqlite.

    Args:
        force: Must be True (corresponds to --yes flag).
        database_url: Optional explicit database URL.

    Returns:
        0 on success, 1 on refusal/error.
    """
    settings = get_settings()
    url = database_url or settings.database_url

    if not force:
        print("Refusing to reset database: --yes flag is required.")
        return 1

    if not is_local_database_url(url):
        print(f"Refusing to reset non-local database: '{url}'. DATABASE_URL must point to localhost or sqlite.")
        return 1

    print(f"Resetting database at '{url}'...")
    reset_db_connections()
    engine = get_engine(url)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    print("Database tables successfully dropped and recreated.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Drop and recreate all tables for development.")
    parser.add_argument("--yes", action="store_true", help="Confirm database reset")
    args = parser.parse_args()

    return reset_database(force=args.yes)


if __name__ == "__main__":
    sys.exit(main())
