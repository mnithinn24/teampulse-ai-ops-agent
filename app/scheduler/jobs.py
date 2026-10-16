"""Background APScheduler jobs for TeamPulse morning and evening briefings."""

import logging
from typing import Optional
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.graph.runner import trigger_pipeline_run

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None


async def scheduled_briefing_job(workspace_id: str = "default", run_type: str = "morning") -> None:
    """Execute scheduled briefing run. Catches and logs errors without stopping scheduler."""
    logger.info("Running scheduled %s briefing job for workspace '%s'", run_type, workspace_id)
    try:
        await trigger_pipeline_run(workspace_id=workspace_id, run_type=run_type, background=False)
        logger.info("Scheduled %s briefing job finished successfully.", run_type)
    except Exception as exc:
        # A job that fails must log the error and not stop the scheduler.
        logger.error("Scheduled %s briefing job failed: %s", run_type, exc)


def create_scheduler(settings=None) -> AsyncIOScheduler:
    """Create and configure AsyncIOScheduler with morning and evening briefing jobs.

    Configured with max_instances=1, coalesce=True, and misfire_grace_time.
    """
    cfg = settings or get_settings()
    tz = ZoneInfo(cfg.timezone)

    scheduler = AsyncIOScheduler(timezone=tz)

    # Parse morning time
    morning_h, morning_m = map(int, cfg.morning_briefing_time.split(":"))
    scheduler.add_job(
        scheduled_briefing_job,
        trigger=CronTrigger(hour=morning_h, minute=morning_m, timezone=tz),
        id="morning_briefing",
        name="TeamPulse Morning Briefing",
        args=["default", "morning"],
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # Parse evening time
    evening_h, evening_m = map(int, cfg.evening_briefing_time.split(":"))
    scheduler.add_job(
        scheduled_briefing_job,
        trigger=CronTrigger(hour=evening_h, minute=evening_m, timezone=tz),
        id="evening_briefing",
        name="TeamPulse Evening Briefing",
        args=["default", "evening"],
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
    )

    return scheduler


def get_scheduler() -> Optional[AsyncIOScheduler]:
    """Return active global scheduler instance if running."""
    global _scheduler
    return _scheduler


def start_scheduler() -> Optional[AsyncIOScheduler]:
    """Start the scheduler if ENABLE_SCHEDULER is True and not already running."""
    global _scheduler
    settings = get_settings()
    if not settings.enable_scheduler:
        logger.info("ENABLE_SCHEDULER is False. Scheduler will not start.")
        return None

    if _scheduler is not None and _scheduler.running:
        return _scheduler

    _scheduler = create_scheduler(settings)
    _scheduler.start()
    logger.info(
        "APScheduler started: morning=%s, evening=%s, timezone=%s",
        settings.morning_briefing_time,
        settings.evening_briefing_time,
        settings.timezone,
    )
    return _scheduler


def stop_scheduler() -> None:
    """Shut the scheduler down cleanly on app shutdown."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        logger.info("Shutting down APScheduler...")
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("APScheduler stopped successfully.")
