"""
Scheduler for daily Growth Metrics extraction.
Uses APScheduler with CronTrigger for daily execution.
Supports timezone configuration via settings.scheduler_timezone.
"""

import asyncio
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from config import growth_settings as settings
from logger import logger


_scheduler: AsyncIOScheduler | None = None
_last_run: datetime | None = None
_last_run_type: str | None = None
_active_hour: int | None = None
_active_minute: int | None = None
JOB_ID = "daily_growth_metrics_check"


def _get_timezone() -> str | None:
    """Return the configured timezone string, or None for system local."""
    tz = settings.scheduler_timezone.strip() if settings.scheduler_timezone else ""
    return tz if tz else None


def init_scheduler() -> None:
    """Create the scheduler instance."""
    global _scheduler
    _scheduler = AsyncIOScheduler()


def start_scheduler(hour: int | None = None, minute: int | None = None) -> None:
    """Start or restart the daily scheduler."""
    global _scheduler, _active_hour, _active_minute
    if not _scheduler:
        init_scheduler()

    h = hour if hour is not None else settings.scheduler_daily_hour
    m = minute if minute is not None else settings.scheduler_daily_minute
    _active_hour = h
    _active_minute = m
    tz = _get_timezone()

    if _scheduler.get_job(JOB_ID):
        _scheduler.remove_job(JOB_ID)

    trigger_kwargs = {"hour": h, "minute": m}
    if tz:
        trigger_kwargs["timezone"] = tz

    _scheduler.add_job(
        _scheduled_check,
        CronTrigger(**trigger_kwargs),
        id=JOB_ID,
        replace_existing=True,
    )

    if not _scheduler.running:
        _scheduler.start()

    tz_label = tz or "system local"
    logger.info(f"[scheduler] Daily growth check scheduled at {h:02d}:{m:02d} ({tz_label})")


def stop_scheduler() -> None:
    """Stop the scheduler."""
    global _scheduler, _active_hour, _active_minute
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        _active_hour = None
        _active_minute = None
        logger.info("[scheduler] Scheduler stopped")


async def _scheduled_check() -> None:
    """Run the daily scheduled growth metrics check."""
    global _last_run, _last_run_type
    logger.info("[scheduler] Starting daily Growth Metrics extraction")
    _last_run = datetime.now(timezone.utc)
    _last_run_type = "scheduled"

    try:
        from growth.orchestrator import run_check_all
        results = await run_check_all()
        logger.info(f"[scheduler] Daily growth check complete — {len(results)} devices checked")
    except Exception as e:
        logger.error(f"[scheduler] Daily growth check failed: {e}")


async def run_force_check() -> list[dict]:
    """Run immediate check on all enabled devices."""
    global _last_run, _last_run_type
    _last_run = datetime.now(timezone.utc)
    _last_run_type = "force"

    from growth.orchestrator import run_check_all
    return await run_check_all()


def get_scheduler_status() -> dict:
    """Return current scheduler state."""
    running = _scheduler is not None and _scheduler.running
    next_run = None

    if running and _scheduler:
        job = _scheduler.get_job(JOB_ID)
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()

    return {
        "running": running,
        "daily_time": f"{_active_hour:02d}:{_active_minute:02d}" if _active_hour is not None else f"{settings.scheduler_daily_hour:02d}:{settings.scheduler_daily_minute:02d}",
        "timezone": _get_timezone() or "system local",
        "next_run": next_run,
        "last_run": _last_run.isoformat() if _last_run else None,
        "last_run_type": _last_run_type,
    }
