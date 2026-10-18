"""FastAPI application entry point for TeamPulse."""

from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.api.routes.briefing import router as briefing_router
from app.api.routes.github import router as github_router
from app.api.routes.jira import router as jira_router
from app.api.routes.planner import router as planner_router
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

    from app.api.routes.jira import start_jira_sync_poller, stop_jira_sync_poller
    start_jira_sync_poller()

    yield

    stop_jira_sync_poller()
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

    # UI Web Frontend Routes (Interactive AI Project Planner)
    static_dir = Path(__file__).resolve().parent.parent / "static"

    @app.get("/", response_class=HTMLResponse, tags=["ui"])
    @app.get("/ui", response_class=HTMLResponse, tags=["ui"])
    @app.get("/planner", response_class=HTMLResponse, tags=["ui"])
    async def serve_planner_ui() -> HTMLResponse:
        """Serve the interactive TeamPulse AI Project Planner web frontend."""
        html_file = static_dir / "index.html"
        if html_file.exists():
            return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
        return HTMLResponse(content="<h1>TeamPulse Planner UI</h1>")

    @app.get("/static/styles.css", tags=["ui"])
    async def serve_styles_css() -> Response:
        """Serve Vanilla CSS styles for the frontend."""
        css_file = static_dir / "styles.css"
        if css_file.exists():
            return Response(content=css_file.read_text(encoding="utf-8"), media_type="text/css")
        return Response(content="", media_type="text/css")

    @app.get("/static/app.js", tags=["ui"])
    async def serve_app_js() -> Response:
        """Serve frontend logic and interaction script."""
        js_file = static_dir / "app.js"
        if js_file.exists():
            return Response(content=js_file.read_text(encoding="utf-8"), media_type="text/javascript")
        return Response(content="", media_type="text/javascript")

    # Register routers
    app.include_router(briefing_router)
    app.include_router(github_router)
    app.include_router(jira_router)
    app.include_router(planner_router)
    app.include_router(slack_router)

    return app


app = create_app()
