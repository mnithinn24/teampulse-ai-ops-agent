"""FastAPI application entry point for TeamPulse."""

from contextlib import asynccontextmanager
import logging
from typing import AsyncGenerator, Dict
from fastapi import FastAPI

from app.api.routes.slack import router as slack_router
from app.db.database import init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan context manager for startup and shutdown events."""
    logger.info("Initializing TeamPulse database schema on startup...")
    init_db()
    yield
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
    async def health_check() -> Dict[str, str]:
        """Health check endpoint for container and uptime monitoring."""
        return {"status": "ok", "app": "TeamPulse"}

    # Register routes
    app.include_router(slack_router)

    return app


app = create_app()
