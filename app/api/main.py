"""FastAPI application entry point for TeamPulse."""

from contextlib import asynccontextmanager
import logging
from typing import Any, AsyncGenerator, Dict, Optional
from fastapi import FastAPI
from sqlalchemy import select

from app.api.routes.briefing import router as briefing_router
from app.api.routes.slack import router as slack_router
from app.config import get_settings
from app.db.database import get_db_session, init_db
from app.db.models import AgentRun
from app.scheduler.jobs import get_scheduler, start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan context manager for startup and shutdown events."""
    logger.info("Initializing TeamPulse database schema on startup...")
    init_db()

    settings = get_settings()
    if settings.enable_scheduler:
        logger.info("Starting background APScheduler...")
        start_scheduler()

    yield

    stop_scheduler()
    logger.info("TeamPulse API shutting down.")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    app = FastAPI(
        title="TeamPulse API",
        description="AI Ops Agent for Engineering Teams",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health", tags=["system"])
    async def health_check() -> Dict[str, Any]:
        """Health check endpoint with scheduler status and last run timestamp."""
        settings = get_settings()

        # Determine scheduler status
        if not settings.enable_scheduler:
            scheduler_status = "disabled"
        else:
            sched = get_scheduler()
            if sched is not None and sched.running:
                scheduler_status = "running"
            else:
                scheduler_status = "stopped"

        # Determine last run time from DB
        last_run_time: Optional[str] = None
        try:
            with get_db_session() as session:
                latest = session.scalars(select(AgentRun).order_by(AgentRun.id.desc())).first()
                if latest is not None:
                    dt = latest.completed_at or latest.created_at
                    if dt is not None:
                        last_run_time = dt.isoformat()
        except Exception as exc:
            logger.warning("Could not retrieve latest run timestamp: %s", exc)

        return {
            "status": "ok",
            "app": "TeamPulse",
            "scheduler_status": scheduler_status,
            "last_run_time": last_run_time,
        }

    # Register routers
    app.include_router(briefing_router)
    app.include_router(slack_router)

    return app


app = create_app()
