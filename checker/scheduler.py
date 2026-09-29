"""
Scheduler — runs account checks on a configurable interval.
Uses APScheduler with asyncio integration.
"""

import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from config import checker_settings as settings
from logger import logger

_scheduler: AsyncIOScheduler | None = None


async def _scheduled_check():
    """Callback for scheduled check."""
    logger.info("[scheduler] Running scheduled account check")
    from checker.runner import run_check
    await run_check(notify=True, trigger="scheduler")


def start_scheduler(
    hour: int | None = None,
    minute: int | None = None,
    interval_hours: int | None = None,
):
    """Start the scheduler.

    Two modes (can be combined):
      - **Daily at time**: fires once per day at *hour:minute*.
        Pass hour/minute to override config defaults.
      - **Every N hours**: fires on a repeating interval.
        Pass interval_hours (1-24) to enable.  24 = once per day from now.

    Args:
        hour: Hour for daily check (0-23). None = use config.
        minute: Minute for daily check (0-59). None = use config.
        interval_hours: Repeat every N hours (1-24). None or 0 = disabled.
    """
    global _scheduler
    if _scheduler and _scheduler.running:
        logger.warning("[scheduler] Already running — stop first")
        return

    h = hour if hour is not None else settings.scheduler_hour
    m = minute if minute is not None else settings.scheduler_minute
    # 0 = off (no interval), None = use config default (now 0 = off)
    ivl = interval_hours if interval_hours is not None else settings.scheduler_interval_hours

    _scheduler = AsyncIOScheduler()
    parts = []

    # Daily check at specific time
    _scheduler.add_job(
        _scheduled_check,
        CronTrigger(hour=h, minute=m),
        id="daily_check",
        name="Daily Account Check",
        replace_existing=True,
    )
    parts.append(f"daily at {h:02d}:{m:02d}")

    # Interval check (every N hours)
    if ivl and 0 < ivl < 24:
        _scheduler.add_job(
            _scheduled_check,
            IntervalTrigger(hours=ivl),
            id="interval_check",
            name=f"Interval Check (every {ivl}h)",
            replace_existing=True,
        )
        parts.append(f"every {ivl}h")

    _scheduler.start()
    logger.info(f"[scheduler] Started — {', '.join(parts)}")


def stop_scheduler():
    """Stop the scheduler."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] Stopped")
    _scheduler = None


def get_status() -> dict:
    """Get scheduler status."""
    if not _scheduler or not _scheduler.running:
        return {"enabled": False, "running": False, "jobs": []}

    jobs = []
    for job in _scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
        })

    return {"enabled": True, "running": True, "jobs": jobs}
