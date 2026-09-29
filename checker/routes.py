"""
API routes — accounts CRUD, checks, history, stats.
"""

import asyncio

from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from checker.database import (
    add_account, get_account, list_accounts, update_account,
    delete_account, count_accounts, list_groups,
    get_history, get_latest, get_all_latest, get_stats_summary,
    get_growth_data, get_7d_growth, get_global_totals, get_global_history,
    reset_history, reset_all_history,
)
from checker.runner import run_check, run_single_check, is_running, last_run_info, queue_check, clear_proxy_error
from checker.scheduler import start_scheduler, stop_scheduler, get_status as get_scheduler_status

router = APIRouter(prefix="/api/v1/checker", tags=["IG Checker"])


# ── Models ──────────────────────────────────────────────────

class AccountCreate(BaseModel):
    username: str
    label: str = ""
    group_name: str = ""


class AccountUpdate(BaseModel):
    label: str | None = None
    group_name: str | None = None
    enabled: bool | None = None


# ── Health ──────────────────────────────────────────────────

@router.get("/health")
async def health():
    from config import checker_settings as settings
    return {
        "status": "ok",
        "service": "ig_account_checker",
        "mode": "adb",
        "device_id": settings.device_id or "not configured",
        "proxy_enabled": settings.proxy_enabled,
    }


# ── Accounts CRUD ───────────────────────────────────────────

@router.get("/accounts")
async def api_list_accounts(
    enabled: bool | None = None,
    group: str = "",
):
    """List all accounts. Optional filters: ?enabled=true, ?group=mygroup."""
    rows = await list_accounts(
        enabled_only=enabled if enabled is not None else False,
        group_name=group,
    )
    # Enrich with latest metrics
    all_latest = await get_all_latest()
    latest_map = {r["username"]: r for r in all_latest}

    enriched = []
    for acc in rows:
        entry = dict(acc)
        latest = latest_map.get(acc["username"], {})
        entry["followers"] = latest.get("followers")
        entry["following"] = latest.get("following")
        entry["posts"] = latest.get("posts")
        entry["is_private"] = bool(latest.get("is_private", 0))
        entry["last_status"] = latest.get("status", "")
        enriched.append(entry)

    return {"accounts": enriched, "total": len(enriched)}


@router.post("/accounts")
async def api_add_account(body: AccountCreate):
    """Add a new account to monitor."""
    username = body.username.lower().strip().lstrip("@")
    if not username:
        raise HTTPException(400, "Username required")
    existing = await get_account(username)
    if existing:
        raise HTTPException(409, f"Account @{username} already exists")
    result = await add_account(username, body.label, body.group_name)
    return {"added": result}


@router.get("/accounts/{username}")
async def api_get_account(username: str):
    """Get account details with latest metrics."""
    acc = await get_account(username)
    if not acc:
        raise HTTPException(404, f"Account @{username} not found")
    latest = await get_latest(username)
    return {**acc, "latest": latest}


@router.put("/accounts/{username}")
async def api_update_account(username: str, body: AccountUpdate):
    """Update account fields (label, group_name, enabled)."""
    acc = await get_account(username)
    if not acc:
        raise HTTPException(404, f"Account @{username} not found")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    if fields:
        await update_account(username, **fields)
    return {"updated": True}


@router.delete("/accounts/{username}")
async def api_delete_account(username: str):
    """Remove account from monitoring."""
    deleted = await delete_account(username)
    if not deleted:
        raise HTTPException(404, f"Account @{username} not found")
    return {"deleted": True}


@router.get("/groups")
async def api_list_groups():
    """List distinct group names."""
    groups = await list_groups()
    return {"groups": groups}


# ── Checks ──────────────────────────────────────────────────

@router.post("/check/all")
async def api_check_all(background: bool = True):
    """Run check on all enabled accounts."""
    if is_running():
        raise HTTPException(409, "Check already in progress")
    if background:
        asyncio.create_task(run_check())
        return {"started": True, "message": "Check running in background"}
    results = await run_check()
    return {"results": results, "total": len(results)}


@router.post("/check/{username}")
async def api_check_single(username: str):
    """Check a single account. If a batch is running, queues it instead."""
    acc = await get_account(username)
    if not acc:
        raise HTTPException(404, f"Account @{username} not found")

    if is_running():
        # Queue for the active batch session instead of rejecting
        return queue_check(username)

    # No batch running — start a fresh single check in background
    asyncio.create_task(run_check(usernames=[username], notify=False))
    return {"started": True, "username": username, "message": "Check started"}


@router.get("/check/status")
async def api_check_status():
    """Current check status."""
    return last_run_info()


@router.post("/check/clear-proxy-error")
async def api_clear_proxy_error():
    """Clear the proxy error flag (after manual intervention)."""
    return clear_proxy_error()


# ── History ─────────────────────────────────────────────────

@router.get("/history/{username}")
async def api_get_history(
    username: str,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(100, ge=1, le=1000),
):
    """Get check history for an account."""
    acc = await get_account(username)
    if not acc:
        raise HTTPException(404, f"Account @{username} not found")
    rows = await get_history(username, days=days, limit=limit)
    return {"username": username, "history": rows, "total": len(rows)}


@router.get("/history")
async def api_get_all_latest():
    """Latest check for every account."""
    rows = await get_all_latest()
    return {"latest": rows, "total": len(rows)}


# ── Reset ──────────────────────────────────────────────────

@router.delete("/history/{username}")
async def api_reset_history(username: str):
    """Delete all check history for a single account."""
    acc = await get_account(username)
    if not acc:
        raise HTTPException(404, f"Account @{username} not found")
    deleted = await reset_history(username)
    return {"reset": True, "username": username, "deleted": deleted}


@router.delete("/history")
async def api_reset_all_history():
    """Delete all check history for every account."""
    deleted = await reset_all_history()
    return {"reset": True, "deleted": deleted}


# ── Stats ───────────────────────────────────────────────────

@router.get("/stats")
async def api_stats():
    """Summary stats."""
    stats = await get_stats_summary()
    run_info = last_run_info()
    return {**stats, **run_info}


@router.get("/stats/growth")
async def api_growth():
    """Growth deltas for all accounts (latest vs previous)."""
    data = await get_growth_data()
    # Sort by delta_followers descending (biggest gainers first)
    data.sort(key=lambda x: x.get("delta_followers") or 0, reverse=True)
    return {"growth": data, "total": len(data)}


@router.get("/stats/growth-7d")
async def api_growth_7d():
    """7-day growth for all accounts (latest vs 7 days ago)."""
    data = await get_7d_growth()
    data.sort(key=lambda x: x.get("pct_followers") or 0, reverse=True)
    return {"growth": data, "total": len(data)}


@router.get("/stats/totals")
async def api_totals():
    """Aggregate totals across all accounts."""
    totals = await get_global_totals()
    return totals


@router.get("/stats/global-history")
async def api_global_history(days: int = Query(30, ge=1, le=365)):
    """Daily aggregate history for charts."""
    rows = await get_global_history(days=days)
    return {"history": rows, "days": days}


# ── Scheduler ──────────────────────────────────────────────

@router.get("/scheduler/status")
async def api_scheduler_status():
    """Get scheduler status and next run times."""
    return get_scheduler_status()


@router.post("/scheduler/start")
async def api_scheduler_start(
    hour: Optional[int] = Query(None, ge=0, le=23),
    minute: Optional[int] = Query(None, ge=0, le=59),
    interval_hours: Optional[int] = Query(None, ge=0, le=24),
):
    """Start the scheduler. Optionally override daily check time and/or interval."""
    start_scheduler(hour=hour, minute=minute, interval_hours=interval_hours)
    return get_scheduler_status()


@router.post("/scheduler/stop")
async def api_scheduler_stop():
    """Stop the scheduler."""
    stop_scheduler()
    return get_scheduler_status()
