"""
API routes for Growth Metrics extraction, device management,
history queries, and scheduler control.
"""

import asyncio
import json as _json
import re
import time
import urllib.request
import urllib.parse
import urllib.error

import httpx
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from typing import Optional

from growth.orchestrator import (
    check_single, check_device_accounts, run_check_all, get_active_tasks, cancel_task,
    cancel_all_tasks, track_task, is_device_busy,
)
from growth.registry import (
    get_devices, get_enabled_devices, get_monitored_devices, get_device_by_id,
    load_devices, save_devices, update_device, add_device, remove_device,
    sync_with_adb,
)
from pydantic import BaseModel
from growth.database import (
    get_latest_metrics, get_history, get_last_two_successful, get_successful_history,
    save_viral_alerts, get_viral_alerts, get_viral_alerts_global_top, clear_all_viral_alerts,
    get_trial_reels, get_latest_trial_reels,
    content_add_video, content_get_by_title, content_search, content_list_videos,
    content_get_usernames, content_update_video, content_delete_video, content_count_videos,
    content_add_pending, content_list_pending, content_count_pending,
    content_delete_pending, content_clear_all_pending, content_resolve_pending,
)
from growth.scheduler import start_scheduler, stop_scheduler, get_scheduler_status
from config import growth_settings as settings
from logger import logger
from shared.adb import list_devices as adb_list_devices


router = APIRouter(prefix="/api/v1/growth", tags=["IG Growth"])


# ── URL enrichment — resolve ig_url from content API in real-time ──

def _build_lookup_key(item: dict, kind: str = "video") -> str:
    """
    Build a stable lookup key for content API (same logic as checkers).
    No views in the key — they change between checks.
    """
    title = (item.get("title") or "").strip()
    username = (item.get("username") or "").strip()

    if kind == "trial":
        rd_date = item.get("reel_date", "") or ""
        rd_idx = item.get("reel_index", 0)
        content_id = f"Trial_{rd_date}_t{rd_idx}_{username}" if rd_date else ""
    else:
        vd_date = item.get("date", "") or ""
        vd_pos = item.get("position", 0)
        content_id = f"Reel_{vd_date}_r{vd_pos}_{username}" if vd_date else ""

    return f"{title} {content_id}".strip() if content_id else title


async def _enrich_urls(items: list[dict], title_key: str = "title",
                       kind: str = "video", parent_username: str = "") -> list[dict]:
    """
    For each item missing ig_url, try to resolve it from the local content registry.
    Mutates items in-place and returns them.
    """
    # Inject parent username into items that don't have their own
    if parent_username:
        for it in items:
            if not it.get("username"):
                it["username"] = parent_username

    need_lookup = [it for it in items if not it.get("ig_url") and _build_lookup_key(it, kind)]
    if not need_lookup:
        return items

    async def _resolve_one(item: dict) -> None:
        try:
            key = _build_lookup_key(item, kind)
            if not key:
                return
            video = await content_get_by_title(key)
            if video:
                item["ig_url"] = video.get("url", "")
        except Exception:
            pass

    await asyncio.gather(*[_resolve_one(it) for it in need_lookup])
    return items


async def _run_and_persist_viral(device_id: str | None = None) -> list[dict]:
    """
    Run viral detection, persist alerts, and send Telegram notifications.
    If device_id is given, only check that device; otherwise all.
    Returns the list of alerts saved.
    """
    try:
        from growth.telegram_alerts import send_viral_alerts_batch

        full = await viral_detection()
        alerts = full.get("alerts", [])
        if device_id:
            alerts = [a for a in alerts if a["device_id"] == device_id]
        if alerts:
            saved = await save_viral_alerts(alerts)
            # Send Telegram notifications for newly saved alerts
            if saved > 0:
                await send_viral_alerts_batch(alerts)
        return alerts
    except Exception as e:
        logger.error(f"[viral] Failed to run/persist viral detection: {e}")
        return []

# ── Validation ────────────────────────────────────────────
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]+$")


def _validate_device_id(device_id: str) -> None:
    """Validate device_id to prevent injection."""
    if not device_id or not _DEVICE_ID_RE.match(device_id):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid device_id '{device_id}'. Only alphanumeric, dots, colons, hyphens allowed.",
        )


# ── Health ─────────────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok", "service": "ig-growth-api"}


@router.get("/health/adb")
async def health_adb():
    """Diagnostic — test ADB connectivity."""
    from shared.adb import is_device_online, _base_cmd

    diag = {
        "adb_path": settings.adb_path,
        "adb_host": settings.adb_host or "(default)",
        "adb_port": settings.adb_port or 5037,
        "adb_cmd": " ".join(_base_cmd()),
    }
    try:
        devices = await adb_list_devices()
        diag["devices_found"] = len(devices)
        diag["devices"] = []
        for d in devices:
            online = await is_device_online(d["device_id"]) if d["state"] == "device" else False
            diag["devices"].append({
                "device_id": d["device_id"],
                "state": d["state"],
                "model": d["model"],
                "responds": online,
            })
    except Exception as e:
        diag["error"] = str(e)
        diag["devices_found"] = 0
        diag["devices"] = []

    return diag


# ── Devices ────────────────────────────────────────────────

@router.get("/devices")
async def list_devices_route():
    """Return all devices from devices.json + remote worker devices."""
    local = [d.model_dump() for d in get_devices()]

    # Append remote worker devices (if any workers are connected)
    try:
        from shared.worker_registry import get_worker_devices
        remote = get_worker_devices("growth")
    except Exception:
        remote = []

    all_devices = local + remote
    return {"total": len(all_devices), "devices": all_devices}


@router.get("/devices/discover")
async def discover_devices(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    filter_state: Optional[str] = Query(None, alias="state"),
):
    """Cross-reference ADB devices with registry."""
    adb_devices = await adb_list_devices()
    registry = get_devices()

    adb_ids = {d["device_id"]: d for d in adb_devices}
    reg_ids = {d.device_id: d for d in registry}

    online_registered = []
    offline_registered = []
    unregistered = []

    for dev in registry:
        adb_info = adb_ids.get(dev.device_id)
        if adb_info and adb_info["state"] == "device":
            online_registered.append({
                "device_id": dev.device_id,
                "username": dev.username,
                "label": dev.label,
                "enabled": dev.enabled,
                "proxy_enabled": dev.proxy_enabled,
                "monitored": dev.monitored,
                "model": adb_info.get("model", "unknown"),
                "state": "online",
            })
        else:
            state = adb_info["state"] if adb_info else "disconnected"
            offline_registered.append({
                "device_id": dev.device_id,
                "username": dev.username,
                "label": dev.label,
                "enabled": dev.enabled,
                "proxy_enabled": dev.proxy_enabled,
                "monitored": dev.monitored,
                "state": state,
            })

    for device_id, info in adb_ids.items():
        if device_id not in reg_ids:
            unregistered.append({
                "device_id": device_id,
                "state": info["state"],
                "model": info.get("model", "unknown"),
            })

    if filter_state == "online":
        all_devices = online_registered
    elif filter_state == "offline":
        all_devices = offline_registered
    elif filter_state == "unregistered":
        all_devices = unregistered
    else:
        all_devices = None

    if all_devices is not None:
        total = len(all_devices)
        paginated = all_devices[offset:offset + limit]
        return {"devices": paginated, "total": total, "limit": limit, "offset": offset}

    return {
        "online_registered": online_registered[offset:offset + limit],
        "offline_registered": offline_registered[offset:offset + limit],
        "unregistered": unregistered[offset:offset + limit],
        "summary": {
            "total_adb": len(adb_devices),
            "total_registered": len(registry),
            "online": len(online_registered),
            "offline": len(offline_registered),
            "unregistered": len(unregistered),
        },
    }


@router.post("/devices/reload")
async def reload_devices():
    """Hot-reload devices.json."""
    devices = load_devices()
    return {"reloaded": True, "total": len(devices)}


@router.post("/devices/refresh")
async def refresh_adb_devices(
    auto_add: bool = Query(False),
):
    """Sync device registry with live ADB connections."""
    adb_devices = await adb_list_devices()
    report = await sync_with_adb(adb_devices, auto_add=auto_add)
    logger.info(f"[API] ADB refresh: {len(report['online'])} online, {len(report['offline'])} offline")
    return report


@router.post("/devices/{device_id:path}/enable")
async def enable_device(device_id: str):
    _validate_device_id(device_id)
    dev = update_device(device_id, enabled=True)
    if not dev:
        try:
            from shared.worker_registry import proxy_to_worker, update_worker_device_cache
            r = await proxy_to_worker(device_id, "POST", f"/api/v1/growth/devices/{device_id}/enable")
            if r:
                if r["status_code"] == 200:
                    update_worker_device_cache(device_id, "growth", enabled=True)
                return JSONResponse(content=r["body"], status_code=r["status_code"])
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    save_devices()
    return {"device_id": device_id, "enabled": True}


@router.post("/devices/{device_id:path}/disable")
async def disable_device(device_id: str):
    _validate_device_id(device_id)
    dev = update_device(device_id, enabled=False)
    if not dev:
        try:
            from shared.worker_registry import proxy_to_worker, update_worker_device_cache
            r = await proxy_to_worker(device_id, "POST", f"/api/v1/growth/devices/{device_id}/disable")
            if r:
                if r["status_code"] == 200:
                    update_worker_device_cache(device_id, "growth", enabled=False)
                return JSONResponse(content=r["body"], status_code=r["status_code"])
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    save_devices()
    return {"device_id": device_id, "enabled": False}


@router.post("/devices/{device_id:path}/monitor")
async def monitor_device(device_id: str):
    _validate_device_id(device_id)
    dev = update_device(device_id, monitored=True)
    if not dev:
        try:
            from shared.worker_registry import proxy_to_worker, update_worker_device_cache
            r = await proxy_to_worker(device_id, "POST", f"/api/v1/growth/devices/{device_id}/monitor")
            if r:
                if r["status_code"] == 200:
                    update_worker_device_cache(device_id, "growth", monitored=True)
                return JSONResponse(content=r["body"], status_code=r["status_code"])
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    save_devices()
    return {"device_id": device_id, "monitored": True}


@router.delete("/devices/{device_id:path}/monitor")
async def unmonitor_device(device_id: str):
    _validate_device_id(device_id)
    dev = update_device(device_id, monitored=False)
    if not dev:
        try:
            from shared.worker_registry import proxy_to_worker, update_worker_device_cache
            r = await proxy_to_worker(device_id, "DELETE", f"/api/v1/growth/devices/{device_id}/monitor")
            if r:
                if r["status_code"] == 200:
                    update_worker_device_cache(device_id, "growth", monitored=False)
                return JSONResponse(content=r["body"], status_code=r["status_code"])
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    save_devices()
    return {"device_id": device_id, "monitored": False}


@router.put("/devices/{device_id}")
async def update_device_route(
    request: Request,
    device_id: str,
    username: Optional[str] = Query(None),
    label: Optional[str] = Query(None),
    enabled: Optional[bool] = Query(None),
    proxy_enabled: Optional[bool] = Query(None),
    monitored: Optional[bool] = Query(None),
    proxy_rotate_url: Optional[str] = Query(None, description="Proxy rotate URL (GET to rotate IP)"),
    usernames: Optional[str] = Query(None, description="Comma-separated IG usernames for multi-account"),
    notes: Optional[str] = Query(None, description="Free-form notes about this device"),
):
    _validate_device_id(device_id)
    dev = get_device_by_id(device_id)
    if not dev:
        # ── Proxy to worker if device is remote ──
        try:
            from shared.worker_registry import proxy_to_worker, update_worker_device_cache
            qs = request.url.query
            path = f"/api/v1/growth/devices/{device_id}" + (f"?{qs}" if qs else "")
            r = await proxy_to_worker(device_id, "PUT", path)
            if r:
                if r["status_code"] == 200:
                    cache_fields = {}
                    if enabled is not None:
                        cache_fields["enabled"] = enabled
                    if monitored is not None:
                        cache_fields["monitored"] = monitored
                    if proxy_enabled is not None:
                        cache_fields["proxy_enabled"] = proxy_enabled
                    if label is not None:
                        cache_fields["label"] = label
                    if cache_fields:
                        update_worker_device_cache(device_id, "growth", **cache_fields)
                return JSONResponse(content=r["body"], status_code=r["status_code"])
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    kwargs = {}
    if username is not None:
        kwargs["username"] = username
    if label is not None:
        kwargs["label"] = label
    if enabled is not None:
        kwargs["enabled"] = enabled
    if proxy_enabled is not None:
        kwargs["proxy_enabled"] = proxy_enabled
    if monitored is not None:
        kwargs["monitored"] = monitored
    if proxy_rotate_url is not None:
        kwargs["proxy_rotate_url"] = proxy_rotate_url
    if usernames is not None:
        # Preserve enabled state from existing usernames so add/remove via this
        # endpoint doesn't accidentally unarchive previously archived accounts.
        existing_enabled = {u.get("username"): u.get("enabled", True)
                            for u in (getattr(dev, "usernames", None) or [])
                            if isinstance(u, dict)}
        kwargs["usernames"] = [
            {"username": (uname := u.strip().lstrip("@").lower()),
             "enabled": existing_enabled.get(uname, True)}
            for u in usernames.split(",") if u.strip()
        ]
    if notes is not None:
        kwargs["notes"] = notes
    if not kwargs:
        raise HTTPException(status_code=400, detail="No fields to update")
    updated = update_device(device_id, **kwargs)
    save_devices()
    return updated.model_dump()


@router.put("/devices/{device_id:path}/accounts/{acct_username}/toggle")
async def toggle_account_route(
    request: Request,
    device_id: str,
    acct_username: str,
    enabled: bool = Query(..., description="Enable or disable this account"),
):
    """Enable or disable a single account on a device."""
    _validate_device_id(device_id)
    dev = get_device_by_id(device_id)
    if not dev:
        # ── Proxy to worker if device is remote ──
        try:
            from shared.worker_registry import proxy_to_worker, update_worker_device_cache
            qs = request.url.query
            path = f"/api/v1/growth/devices/{device_id}/accounts/{acct_username}/toggle" + (f"?{qs}" if qs else "")
            r = await proxy_to_worker(device_id, "PUT", path)
            if r:
                if r["status_code"] == 200:
                    # Update the usernames list in cache
                    update_worker_device_cache(device_id, "growth")  # touch to refresh
                return JSONResponse(content=r["body"], status_code=r["status_code"])
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    acct_username = acct_username.lower().strip().lstrip("@")
    found = dev.set_username_enabled(acct_username, enabled)
    if not found:
        raise HTTPException(status_code=404, detail=f"Account @{acct_username} not found on device")
    save_devices()
    return {"device_id": device_id, "username": acct_username, "enabled": enabled}


@router.post("/devices")
async def add_device_route(
    device_id: str = Query(..., description="Device serial (from ADB)"),
    username: str = Query("", description="Instagram username"),
    label: str = Query("", description="Friendly label"),
    enabled: bool = Query(True),
    proxy_enabled: bool = Query(True),
    monitored: bool = Query(False),
    proxy_rotate_url: str = Query("", description="Proxy rotate URL (GET to rotate IP)"),
    usernames: str = Query("", description="Comma-separated IG usernames for multi-account"),
):
    _validate_device_id(device_id)
    existing = get_device_by_id(device_id)
    if existing:
        raise HTTPException(status_code=409, detail=f"Device '{device_id}' already registered")
    dev = add_device(device_id=device_id, username=username, label=label, enabled=enabled)
    parsed_usernames = [{"username": u.strip().lstrip("@").lower(), "enabled": True}
                        for u in usernames.split(",") if u.strip()] if usernames else []
    update_device(device_id, proxy_enabled=proxy_enabled, monitored=monitored, proxy_rotate_url=proxy_rotate_url, usernames=parsed_usernames)
    save_devices()
    return get_device_by_id(device_id).model_dump()


@router.delete("/devices/{device_id:path}")
async def delete_device_route(device_id: str):
    _validate_device_id(device_id)
    dev = get_device_by_id(device_id)
    if not dev:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    removed = remove_device(device_id)
    if not removed:
        raise HTTPException(status_code=500, detail="Failed to remove device")
    save_devices()
    return {"deleted": True, "device_id": device_id, "username": dev.username}


# ── Growth Metrics Extraction ────────────────────────────────

@router.post("/check/force")
async def check_force_all(
    views_detail: bool = Query(True, description="Include Views drill-down detail"),
    background: bool = Query(False, description="Run in background"),
    all_enabled: bool = Query(False, description="Check ALL enabled, not just monitored"),
):
    """Force growth metrics extraction on monitored devices (or all enabled)."""
    logger.info(f"[API] /check/force triggered, views_detail={views_detail}, background={background}")

    monitored_only = not all_enabled

    if background:
        task_id = track_task(
            run_check_all(include_views_detail=views_detail, monitored_only=monitored_only),
            device_id="all",
        )
        return {"task_id": task_id, "status": "running", "message": "Check started in background."}

    start = time.monotonic()
    results = await run_check_all(include_views_detail=views_detail, monitored_only=monitored_only)
    elapsed = int((time.monotonic() - start) * 1000)

    # Auto-persist viral alerts after all checks complete
    any_success = any(r.get("success") for r in results)
    if any_success:
        await _run_and_persist_viral()

    return {"total": len(results), "results": results, "duration_ms": elapsed}


@router.post("/check/force/{device_id:path}")
async def check_force_single(
    device_id: str,
    views_detail: bool = Query(True),
    background: bool = Query(False, description="Run in background — return immediately"),
):
    """Force growth metrics extraction for ONE device."""
    _validate_device_id(device_id)
    device = get_device_by_id(device_id)
    if not device:
        # Remote worker device — fire the check and return immediately
        # The result will arrive async via hub_reporter
        try:
            from shared.worker_registry import fire_and_forget_to_worker, register_remote_check, find_worker_for_device
            result = await fire_and_forget_to_worker(
                device_id, "POST",
                f"/api/v1/growth/check/force/{device_id}?views_detail={str(views_detail).lower()}"
            )
            if result:
                # Worker responded quickly (e.g. 409 conflict) — relay as-is
                if result["status_code"] != 200:
                    return JSONResponse(content=result["body"], status_code=result["status_code"])
                # 200 = check finished instantly (rare) — return data
                return JSONResponse(content=result["body"], status_code=200)
        except httpx.ReadTimeout:
            # Worker accepted the check but it takes long — that's fine
            # Result will arrive via hub_reporter.report()
            found = find_worker_for_device(device_id)
            register_remote_check("growth", device_id, found[0] if found else "unknown")
            return {"status": "accepted", "device_id": device_id, "message": "Check started on remote worker. Results will appear when complete."}
        except Exception as e:
            logger.warning(f"[API] Worker proxy failed for {device_id}: {e}")
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    if not device.enabled:
        raise HTTPException(status_code=400, detail=f"Device '{device_id}' is disabled")

    usernames = device.usernames or []
    logger.info(f"[API] /check/force/{device_id} → @{device.username} (usernames={usernames}, background={background})")

    # Reject if a check is already running on this device
    if is_device_busy(device_id):
        raise HTTPException(
            status_code=409,
            detail=f"Check already in progress for '{device_id}' — wait for it to finish",
        )

    # ── Background mode: dispatch and return immediately ──
    if background:
        async def _check_and_persist():
            results = await check_device_accounts(device, include_views_detail=views_detail)
            any_success = any(r.get("success") for r in results)
            if any_success:
                await _run_and_persist_viral(device_id=device.device_id)
            return results

        task_id = track_task(_check_and_persist(), device_id=device_id)
        return {
            "task_id": task_id,
            "status": "running",
            "device_id": device_id,
            "message": "Check started in background.",
        }

    start = time.monotonic()
    results = await check_device_accounts(device, include_views_detail=views_detail)
    elapsed = int((time.monotonic() - start) * 1000)

    # Auto-persist viral alerts after successful check
    any_success = any(r.get("success") for r in results)
    if any_success:
        await _run_and_persist_viral(device_id=device.device_id)

    if len(results) == 1:
        results[0]["duration_ms"] = elapsed
        return results[0]
    return {"device_id": device_id, "total": len(results), "results": results, "duration_ms": elapsed}


# ── Task Management ──────────────────────────────────────

@router.get("/check/tasks")
async def list_check_tasks():
    tasks = get_active_tasks()
    return {"total": len(tasks), "tasks": tasks}


@router.post("/check/cancel/{task_id}")
async def cancel_check_task(task_id: str):
    if cancel_task(task_id):
        return {"cancelled": True, "task_id": task_id}
    raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found or already finished")


@router.post("/check/cancel-all")
async def cancel_all_check_tasks():
    count = cancel_all_tasks()
    return {"cancelled": count}


# ── Metrics / History ────────────────────────────────────────

@router.get("/metrics/latest")
async def latest_metrics():
    """Return last known growth metrics for every device."""
    data = await get_latest_metrics()
    # Enrich video_details ig_url from content API (real-time resolve)
    for m in data:
        ext = m.get("extended") or {}
        vids = ext.get("video_details", [])
        if vids:
            await _enrich_urls(vids, kind="video", parent_username=m.get("username", ""))
    return {"total": len(data), "metrics": data}


@router.get("/metrics/latest/{device_id:path}")
async def latest_metrics_single(device_id: str):
    _validate_device_id(device_id)
    all_metrics = await get_latest_metrics()
    metric = None
    for m in all_metrics:
        if m.get("device_id") == device_id:
            metric = m
            break
    if not metric:
        raise HTTPException(status_code=404, detail=f"No metrics data for device '{device_id}'")
    return metric


@router.get("/metrics/history")
async def query_history(
    device_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    records = await get_history(device_id=device_id, limit=limit, offset=offset)
    return {"total": len(records), "records": records}


# ── Growth Analysis ───────────────────────────────────────

def _calc_delta(current, previous):
    """Calculate delta and percentage change between two values."""
    if current is None or previous is None:
        return {"value": None, "pct": None, "direction": "flat"}
    diff = current - previous
    if previous == 0:
        pct = 100.0 if diff > 0 else (0.0 if diff == 0 else -100.0)
    else:
        pct = round((diff / previous) * 100, 1)
    direction = "up" if diff > 0 else ("down" if diff < 0 else "flat")
    return {"value": diff, "pct": pct, "direction": direction}


def _growth_score(deltas: dict) -> int:
    """
    Compute a 1-100 growth score based on metric deltas.
    Weights: views 0.30, followers 0.30, interactions 0.25, reached 0.15
    Each metric contributes 0-100 scaled by its pct change (capped at ±200%).
    Score = 50 (neutral) + weighted sum of contributions.
    Clamped to [1, 100] so it's always renderable as a 1-100% indicator.
    """
    weights = {
        "views": 0.30,
        "new_followers": 0.30,
        "interactions": 0.25,
        "accounts_reached": 0.15,
    }
    total = 0.0
    active_weight = 0.0
    for key, w in weights.items():
        d = deltas.get(key, {})
        pct = d.get("pct")
        if pct is None:
            continue
        # Cap at ±200% so a single viral post doesn't dominate
        capped = max(-200, min(200, pct))
        # Scale: 0% → 0 contribution, +200% → +50, -200% → -50
        contribution = (capped / 200) * 50
        total += contribution * w
        active_weight += w

    if active_weight == 0:
        return 50  # neutral — no data

    # Normalize if not all metrics present
    total = total / active_weight
    score = int(round(50 + total))
    return max(1, min(100, score))


@router.get("/metrics/growth")
async def growth_analysis():
    """
    Return growth comparison for all accounts (device+username combos).
    Compares latest check vs previous check: deltas, pct change, growth score.
    """
    all_devices = get_monitored_devices() + [
        d for d in get_enabled_devices() if not d.monitored
    ]
    # Deduplicate by device_id
    seen = set()
    unique = []
    for d in all_devices:
        if d.device_id not in seen:
            seen.add(d.device_id)
            unique.append(d)

    results = []
    metric_keys = ["views", "interactions", "new_followers", "accounts_reached", "content_shared"]

    for d in unique:
        # Build list of all accounts for this device
        accounts = []
        if d.username:
            accounts.append(d.username)
        for u in d.get_all_usernames():
            if u and u not in accounts:
                accounts.append(u)
        if not accounts:
            accounts = [d.username or ""]

        for acct_username in accounts:
            last_two = await get_last_two_successful(d.device_id, username=acct_username or None)

            entry = {
                "device_id": d.device_id,
                "username": acct_username,
                "has_data": len(last_two) >= 1,
                "has_comparison": len(last_two) >= 2,
                "current": {},
                "previous": {},
                "deltas": {},
                "score": 50,
                "score_label": "neutral",
            }

            if len(last_two) >= 1:
                curr = last_two[0]
                entry["current"] = {k: curr.get(k) for k in metric_keys}
                entry["current"]["checked_at"] = curr.get("checked_at")
                entry["current"]["ui_model"] = curr.get("ui_model", "")

            if len(last_two) >= 2:
                prev = last_two[1]
                entry["previous"] = {k: prev.get(k) for k in metric_keys}
                entry["previous"]["checked_at"] = prev.get("checked_at")

                # Calculate deltas
                for k in metric_keys:
                    entry["deltas"][k] = _calc_delta(curr.get(k), prev.get(k))

                # Growth score
                score = _growth_score(entry["deltas"])
                entry["score"] = score
                if score >= 70:
                    entry["score_label"] = "growing"
                elif score >= 55:
                    entry["score_label"] = "stable"
                elif score >= 40:
                    entry["score_label"] = "slow"
                else:
                    entry["score_label"] = "declining"

            results.append(entry)

    return {"total": len(results), "devices": results}


@router.get("/metrics/growth/{device_id:path}")
async def growth_analysis_single(device_id: str):
    """Growth analysis for a single device (all accounts)."""
    _validate_device_id(device_id)
    full = await growth_analysis()
    matches = [d for d in full["devices"] if d["device_id"] == device_id]
    if not matches:
        raise HTTPException(status_code=404, detail=f"No growth data for '{device_id}'")
    return {"total": len(matches), "devices": matches}


@router.get("/metrics/growth-7d")
async def growth_7d_all():
    """
    7-day growth for all devices.
    Compares latest check vs oldest check within the last 7 days.
    Returns deltas and percentage for views, interactions, new_followers,
    accounts_reached, content_shared, plus a growth score.
    """
    all_devices = get_monitored_devices() + [
        d for d in get_enabled_devices() if not d.monitored
    ]
    seen = set()
    unique = []
    for d in all_devices:
        if d.device_id not in seen:
            seen.add(d.device_id)
            unique.append(d)

    metric_keys = ["views", "interactions", "new_followers", "accounts_reached", "content_shared"]
    results = []

    for d in unique:
        # Build list of all accounts for this device
        accounts = []
        if d.username:
            accounts.append(d.username)
        for u in d.get_all_usernames():
            if u and u not in accounts:
                accounts.append(u)
        if not accounts:
            accounts = [d.username or ""]

        for acct_username in accounts:
            history = await get_successful_history(d.device_id, days=7, username=acct_username or None)
            if not history:
                continue

            latest = history[-1]   # newest (list sorted ASC)
            oldest = history[0]    # oldest within 7d

            entry = {
                "device_id": d.device_id,
                "username": acct_username,
                "checks_in_period": len(history),
                "latest_at": latest.get("checked_at", ""),
                "oldest_at": oldest.get("checked_at", ""),
                "current": {k: latest.get(k) for k in metric_keys},
                "previous": {k: oldest.get(k) for k in metric_keys},
                "deltas": {},
                "score": 50,
                "score_label": "neutral",
            }

            has_comparison = len(history) >= 2
            if has_comparison:
                for k in metric_keys:
                    entry["deltas"][k] = _calc_delta(latest.get(k), oldest.get(k))

                score = _growth_score(entry["deltas"])
                entry["score"] = score
                if score >= 70:
                    entry["score_label"] = "growing"
                elif score >= 55:
                    entry["score_label"] = "stable"
                elif score >= 40:
                    entry["score_label"] = "slow"
                else:
                    entry["score_label"] = "declining"

            results.append(entry)

    # Sort by score descending
    results.sort(key=lambda x: x.get("score", 50), reverse=True)
    return {"total": len(results), "period": "7d", "devices": results}


@router.get("/metrics/growth-7d/{device_id:path}")
async def growth_7d_single(device_id: str):
    """7-day growth for a single device (all accounts)."""
    _validate_device_id(device_id)
    full = await growth_7d_all()
    matches = [d for d in full["devices"] if d["device_id"] == device_id]
    if not matches:
        raise HTTPException(status_code=404, detail=f"No 7-day data for '{device_id}'")
    return {"total": len(matches), "period": "7d", "devices": matches}


@router.get("/metrics/extended/{device_id:path}")
async def extended_metrics(device_id: str):
    """
    Return extended (Tier 1 + Tier 2) metrics for a device.
    Includes computed fields: engagement_rate, follower_conversion_rate, views_per_content.
    """
    _validate_device_id(device_id)
    all_metrics = await get_latest_metrics()
    metric = None
    for m in all_metrics:
        if m.get("device_id") == device_id:
            metric = m
            break
    if not metric:
        raise HTTPException(status_code=404, detail=f"No metrics data for device '{device_id}'")

    extended = metric.get("extended", {})
    views = metric.get("views")
    interactions = metric.get("interactions")
    new_followers = metric.get("new_followers")
    accounts_reached = metric.get("accounts_reached")
    content_shared = metric.get("content_shared")

    # Compute derived rates
    engagement_rate = None
    if interactions is not None and accounts_reached and accounts_reached > 0:
        engagement_rate = round((interactions / accounts_reached) * 100, 2)

    follower_conversion_rate = None
    if new_followers is not None and accounts_reached and accounts_reached > 0:
        follower_conversion_rate = round((new_followers / accounts_reached) * 100, 2)

    views_per_content = None
    if views is not None and content_shared and content_shared > 0:
        views_per_content = round(views / content_shared, 1)

    return {
        "device_id": device_id,
        "username": metric.get("username", ""),
        "checked_at": metric.get("checked_at"),
        "summary": {
            "views": views,
            "interactions": interactions,
            "new_followers": new_followers,
            "accounts_reached": accounts_reached,
            "content_shared": content_shared,
        },
        "computed": {
            "engagement_rate": engagement_rate,
            "follower_conversion_rate": follower_conversion_rate,
            "views_per_content": views_per_content,
        },
        "extended": extended,
    }


# ── Video Details ─────────────────────────────────────────

@router.get("/metrics/videos/{device_id:path}")
async def video_details(device_id: str):
    """
    Return individual video detail metrics for a device.
    Extracted from extended.video_details (carrusel tap flow).
    Each video includes: title, date, duration, likes, comments, shares,
    reposts, saves, views, watch_time, interactions, profile_activity, ig_url.
    """
    _validate_device_id(device_id)
    all_metrics = await get_latest_metrics()
    metric = None
    for m in all_metrics:
        if m.get("device_id") == device_id:
            metric = m
            break
    if not metric:
        raise HTTPException(status_code=404, detail=f"No metrics data for device '{device_id}'")

    extended = metric.get("extended", {})
    videos = extended.get("video_details", [])
    await _enrich_urls(videos, kind="video", parent_username=metric.get("username", ""))

    return {
        "device_id": device_id,
        "username": metric.get("username", ""),
        "checked_at": metric.get("checked_at"),
        "video_count": len(videos),
        "videos": videos,
    }


# ── Export ─────────────────────────────────────────────────

@router.get("/metrics/export")
async def export_metrics(
    format: str = Query("json", description="Export format: json or csv"),
):
    """
    Export all device metrics in a single call for external dashboards.
    Includes: summary metrics, growth deltas, score, extended data, top content.
    """
    from fastapi.responses import PlainTextResponse

    all_devices = get_monitored_devices() + [
        d for d in get_enabled_devices() if not d.monitored
    ]
    seen = set()
    unique = []
    for d in all_devices:
        if d.device_id not in seen:
            seen.add(d.device_id)
            unique.append(d)

    all_latest = await get_latest_metrics()
    latest_map = {m["device_id"]: m for m in all_latest}

    metric_keys = ["views", "interactions", "new_followers", "accounts_reached", "content_shared"]
    rows = []

    for d in unique:
        m = latest_map.get(d.device_id, {})
        last_two = await get_last_two_successful(d.device_id)

        # Compute deltas + score
        deltas = {}
        score = 50
        score_label = "neutral"
        if len(last_two) >= 2:
            curr, prev = last_two[0], last_two[1]
            for k in metric_keys:
                deltas[k] = _calc_delta(curr.get(k), prev.get(k))
            score = _growth_score(deltas)
            score_label = "growing" if score >= 70 else "stable" if score >= 55 else "slow" if score >= 40 else "declining"

        # Compute rates
        views = m.get("views")
        interactions = m.get("interactions")
        new_followers = m.get("new_followers")
        accounts_reached = m.get("accounts_reached")
        content_shared = m.get("content_shared")

        engagement_rate = round((interactions / accounts_reached) * 100, 2) if interactions is not None and accounts_reached and accounts_reached > 0 else None
        follower_conversion = round((new_followers / accounts_reached) * 100, 2) if new_followers is not None and accounts_reached and accounts_reached > 0 else None
        views_per_content = round(views / content_shared, 1) if views is not None and content_shared and content_shared > 0 else None

        # Parse extended + views_detail
        extended = m.get("extended", {})
        views_detail = m.get("views_detail", [])

        row = {
            "device_id": d.device_id,
            "username": d.username or m.get("username", ""),
            "ui_model": m.get("ui_model", ""),
            "date_range": m.get("date_range", ""),
            "checked_at": m.get("checked_at"),
            "views": views,
            "interactions": interactions,
            "new_followers": new_followers,
            "accounts_reached": accounts_reached,
            "content_shared": content_shared,
            "engagement_rate": engagement_rate,
            "follower_conversion_rate": follower_conversion,
            "views_per_content": views_per_content,
            "growth_score": score,
            "growth_label": score_label,
            "deltas": deltas,
            "extended": extended,
            "top_content": views_detail,
        }
        rows.append(row)

    if format == "csv":
        import csv
        import io
        output = io.StringIO()
        csv_fields = [
            "device_id", "username", "ui_model", "date_range", "checked_at",
            "views", "interactions", "new_followers", "accounts_reached", "content_shared",
            "engagement_rate", "follower_conversion_rate", "views_per_content",
            "growth_score", "growth_label",
            "top_content_1", "top_content_1_views", "top_content_1_date",
            "top_content_2", "top_content_2_views", "top_content_2_date",
            "top_content_3", "top_content_3_views", "top_content_3_date",
        ]
        writer = csv.DictWriter(output, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat = {k: row[k] for k in csv_fields if k in row}
            tc = row.get("top_content", [])
            for i in range(3):
                if i < len(tc):
                    flat[f"top_content_{i+1}"] = tc[i].get("content_id", "")
                    flat[f"top_content_{i+1}_views"] = tc[i].get("views", "")
                    flat[f"top_content_{i+1}_date"] = tc[i].get("date", "")
            writer.writerow(flat)
        return PlainTextResponse(content=output.getvalue(), media_type="text/csv",
                                  headers={"Content-Disposition": "attachment; filename=ig_growth_export.csv"})

    return {"total": len(rows), "exported_at": datetime.now(timezone.utc).isoformat(), "devices": rows}


# ── Viral Detection ───────────────────────────────────────


def _parse_views_detail_from_record(record: dict) -> list[dict]:
    """Extract views_detail list from a metric record (handles JSON or list)."""
    if isinstance(record.get("views_detail"), list):
        return record["views_detail"]
    raw = record.get("views_detail_json")
    if raw:
        try:
            import json as _json
            return _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            pass
    return []


def _calc_acceleration(history_records: list[dict], match_key: str) -> dict | None:
    """
    Look at a content item across multiple checks (oldest→newest).
    Returns acceleration info if consistent growth detected.
    history_records should be in chronological order (oldest first).
    """
    # Collect view snapshots for this content across checks
    snapshots = []
    for rec in history_records:
        detail = _parse_views_detail_from_record(rec)
        for item in detail:
            key = f"{item.get('type', '')}_{item.get('date', '')}"
            if key == match_key:
                snapshots.append(item.get("views", 0))
                break

    if len(snapshots) < 3:
        return None  # Need at least 3 data points for acceleration

    # Calculate growth rates between consecutive checks
    growth_rates = []
    for i in range(1, len(snapshots)):
        prev_v = snapshots[i - 1]
        curr_v = snapshots[i]
        if prev_v > 0:
            rate = ((curr_v - prev_v) / prev_v) * 100
            growth_rates.append(rate)

    if len(growth_rates) < 2:
        return None

    # Check if growth is accelerating (each rate > previous)
    accelerating_count = sum(
        1 for i in range(1, len(growth_rates)) if growth_rates[i] > growth_rates[i - 1]
    )
    consistent_growth = all(r > 0 for r in growth_rates)
    avg_growth = sum(growth_rates) / len(growth_rates)
    total_growth = 0
    if snapshots[0] > 0:
        total_growth = ((snapshots[-1] - snapshots[0]) / snapshots[0]) * 100

    is_accelerating = accelerating_count >= len(growth_rates) // 2 and consistent_growth

    if not consistent_growth or avg_growth < 10:
        return None

    return {
        "snapshots": len(snapshots),
        "total_growth_pct": round(total_growth, 1),
        "avg_growth_pct": round(avg_growth, 1),
        "is_accelerating": is_accelerating,
        "growth_rates": [round(r, 1) for r in growth_rates],
    }


@router.get("/metrics/viral")
async def viral_detection():
    """
    Detect content going viral by comparing top content between checks.
    A video is flagged as viral/trending if views increase significantly.
    Includes acceleration scoring across multiple checks.
    """
    all_devices = get_monitored_devices() + [
        d for d in get_enabled_devices() if not d.monitored
    ]
    seen = set()
    unique = []
    for d in all_devices:
        if d.device_id not in seen:
            seen.add(d.device_id)
            unique.append(d)

    alerts = []

    for d in unique:
        last_two = await get_last_two_successful(d.device_id)
        if len(last_two) < 2:
            continue

        curr = last_two[0]
        prev = last_two[1]

        # ── Guard: require minimum time gap between checks (30 min) ──
        # Prevents false positives from rapid consecutive checks
        from datetime import datetime, timezone
        MIN_CHECK_GAP_MINUTES = 30
        try:
            curr_ts = curr.get("checked_at", "")
            prev_ts = prev.get("checked_at", "")
            if curr_ts and prev_ts:
                # Parse ISO timestamps
                ct = datetime.fromisoformat(curr_ts.replace("Z", "+00:00"))
                pt = datetime.fromisoformat(prev_ts.replace("Z", "+00:00"))
                gap_min = (ct - pt).total_seconds() / 60
                if gap_min < MIN_CHECK_GAP_MINUTES:
                    continue  # Too soon — skip viral detection for this device
        except (ValueError, TypeError):
            pass  # Can't parse timestamps — proceed anyway

        curr_detail = _parse_views_detail_from_record(curr)
        prev_detail = _parse_views_detail_from_record(prev)

        if not curr_detail:
            continue

        # Fetch extended history for acceleration scoring (up to 10 recent checks)
        history = await get_successful_history(d.device_id, days=7)
        # Limit to last 10 records, keep chronological order (oldest first)
        history = history[-10:] if len(history) > 10 else history

        # ── Guard: require at least 3 checks for acceleration signals ──
        has_enough_history = len(history) >= 3

        # Build lookup of previous content by type+date
        prev_map = {}
        for item in prev_detail:
            match_key = f"{item.get('type', '')}_{item.get('date', '')}"
            prev_map[match_key] = item

        # Check each current item for viral signals
        for item in curr_detail:
            curr_views = item.get("views", 0)
            match_key = f"{item.get('type', '')}_{item.get('date', '')}"
            prev_item = prev_map.get(match_key)

            viral_signals = []
            viral_score = 0  # 0-100
            rank = item.get("rank", 99)
            is_new = prev_item is None

            if prev_item:
                # ── Existing content: compare growth ──
                prev_views = prev_item.get("views", 0)
                if prev_views > 0:
                    growth_pct = ((curr_views - prev_views) / prev_views) * 100
                    if growth_pct > 100:
                        viral_signals.append(f"views +{growth_pct:.0f}% since last check")
                        viral_score = min(100, int(growth_pct / 5))
                    elif growth_pct > 50:
                        viral_signals.append(f"views +{growth_pct:.0f}% since last check")
                        viral_score = min(60, int(growth_pct / 3))
            else:
                # ── New content: just entered the top ranking ──
                # Only flag if we have enough history to know this is truly new
                if has_enough_history:
                    if curr_views >= 8000:
                        viral_signals.append(f"new content entered top with {curr_views:,} views")
                        viral_score = max(viral_score, 75)
                    elif curr_views >= 3000 and rank <= 2:
                        viral_signals.append(f"new content at #{rank} with {curr_views:,} views")
                        viral_score = max(viral_score, 75)
                    elif curr_views >= 3000:
                        viral_signals.append(f"new content at #{rank} with {curr_views:,} views")
                        viral_score = max(viral_score, 55)
                    elif curr_views >= 1000 and rank <= 2:
                        viral_signals.append(f"new content at #{rank} with fast traction ({curr_views:,})")
                        viral_score = max(viral_score, 55)
                    elif curr_views >= 1000:
                        viral_signals.append(f"new content at #{rank} gaining traction ({curr_views:,})")
                        viral_score = max(viral_score, 35)

            # ── Acceleration bonus (multi-check history) ──
            accel = _calc_acceleration(history, match_key) if has_enough_history else None
            if accel:
                if accel["is_accelerating"] and accel["total_growth_pct"] > 200:
                    viral_signals.append(
                        f"accelerating growth: +{accel['total_growth_pct']:.0f}% over {accel['snapshots']} checks"
                    )
                    viral_score = max(viral_score, 80)
                elif accel["is_accelerating"]:
                    viral_signals.append(
                        f"accelerating: +{accel['total_growth_pct']:.0f}% over {accel['snapshots']} checks"
                    )
                    viral_score = max(viral_score, 60)
                elif accel["total_growth_pct"] > 100:
                    viral_signals.append(
                        f"consistent growth: +{accel['total_growth_pct']:.0f}% over {accel['snapshots']} checks"
                    )
                    viral_score = max(viral_score, 50)

            # Absolute thresholds — ONLY as bonus when there's actual growth
            # (not standalone — prevents false alerts on every check for popular content)
            if viral_signals:
                if curr_views >= 10000:
                    viral_signals.append(f"high views ({curr_views:,})")
                    viral_score = max(viral_score, 70)
                elif curr_views >= 5000:
                    viral_signals.append(f"strong views ({curr_views:,})")
                    viral_score = max(viral_score, 50)

            # Top position bonus (existing content with actual growth)
            if not is_new and rank == 1 and curr_views >= 1000 and viral_signals:
                viral_score = max(viral_score, 40)

            if viral_score >= 30:
                alert_entry = {
                    "device_id": d.device_id,
                    "username": d.username,
                    "content_id": item.get("content_id", ""),
                    "type": item.get("type", ""),
                    "date": item.get("date", ""),
                    "rank": rank,
                    "current_views": curr_views,
                    "previous_views": prev_item.get("views") if prev_item else None,
                    "viral_score": viral_score,
                    "signals": viral_signals,
                    "checked_at": curr.get("checked_at"),
                }
                if accel:
                    alert_entry["acceleration"] = accel
                alerts.append(alert_entry)

    # Sort by viral_score descending
    alerts.sort(key=lambda x: x["viral_score"], reverse=True)

    # Classify
    for a in alerts:
        if a["viral_score"] >= 70:
            a["status"] = "viral"
        elif a["viral_score"] >= 50:
            a["status"] = "trending"
        elif a["viral_score"] >= 30:
            a["status"] = "rising"

    return {
        "total": len(alerts),
        "alerts": alerts,
    }


@router.get("/metrics/viral/history")
async def viral_history(
    device_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_score: int = Query(0, ge=0, le=100),
    days: int = Query(0, ge=0, le=3650, description="Only alerts within the last N days (0 = no filter)"),
):
    """Query persisted viral alert history."""
    records = await get_viral_alerts(
        device_id=device_id, limit=limit, offset=offset, min_score=min_score, days=days,
    )
    return {"total": len(records), "records": records}


@router.get("/metrics/viral/top")
async def viral_global_top(
    limit: int = Query(7, ge=1, le=50),
    days: int = Query(0, ge=0, le=3650, description="Only alerts within the last N days (0 = no filter)"),
):
    """Return top N viral alerts globally (latest per content, by score)."""
    records = await get_viral_alerts_global_top(limit=limit, days=days)
    return {"total": len(records), "alerts": records}


@router.delete("/metrics/viral/alerts")
async def viral_clear_all():
    """Delete ALL persisted viral alerts (local + remote workers)."""
    count = await clear_all_viral_alerts()
    return {"deleted": count}


@router.get("/metrics/viral/{device_id:path}")
async def viral_detection_single(device_id: str):
    """Viral detection for a single device."""
    _validate_device_id(device_id)
    full = await viral_detection()
    device_alerts = [a for a in full["alerts"] if a["device_id"] == device_id]
    return {"total": len(device_alerts), "alerts": device_alerts}


# ── Content Activity Rate ────────────────────────────────

@router.get("/metrics/activity")
async def content_activity():
    """
    Detect content upload activity per account.
    Compares top content across checks to find when new content last appeared.
    Returns: last_new_content (days ago), content_frequency, inactive flag.
    """
    all_devices = get_monitored_devices() + [
        d for d in get_enabled_devices() if not d.monitored
    ]
    seen = set()
    unique = []
    for d in all_devices:
        if d.device_id not in seen:
            seen.add(d.device_id)
            unique.append(d)

    results = []

    for d in unique:
        # Build list of all accounts for this device
        accounts = []
        if d.username:
            accounts.append(d.username)
        for u in d.get_all_usernames():
            if u and u not in accounts:
                accounts.append(u)
        if not accounts:
            accounts = [d.username or ""]

        for acct_username in accounts:
            history = await get_successful_history(d.device_id, days=30, username=acct_username or None)
            if not history:
                results.append({
                    "device_id": d.device_id,
                    "username": acct_username,
                    "total_checks": 0,
                    "unique_content_seen": 0,
                    "last_new_content_ago": None,
                    "content_frequency": None,
                    "status": "no_data",
                })
                continue

            # Track all unique content_ids across all checks (chronological)
            all_content_keys = {}  # match_key → first_seen_check_index
            for idx, rec in enumerate(history):
                detail = _parse_views_detail_from_record(rec)
                for item in detail:
                    key = f"{item.get('type', '')}_{item.get('date', '')}"
                    if key not in all_content_keys:
                        all_content_keys[key] = idx

            # Find the most recently discovered content
            last_new_idx = max(all_content_keys.values()) if all_content_keys else 0
            total_checks = len(history)
            unique_content = len(all_content_keys)

            # Calculate days since last new content appeared
            last_new_content_ago = None
            if history and all_content_keys:
                last_new_check = history[last_new_idx]
                checked_at = last_new_check.get("checked_at", "")
                if checked_at:
                    try:
                        check_dt = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
                        delta = datetime.now(timezone.utc) - check_dt
                        last_new_content_ago = delta.days
                    except (ValueError, TypeError):
                        pass

            # Content frequency: new content items per check on average
            # Count how many checks introduced new content
            checks_with_new = len(set(all_content_keys.values()))
            frequency = round(checks_with_new / total_checks, 2) if total_checks > 0 else 0

            # Status: active, slowing, inactive
            if last_new_content_ago is not None:
                if last_new_content_ago <= 3:
                    status = "active"
                elif last_new_content_ago <= 7:
                    status = "slowing"
                else:
                    status = "inactive"
            else:
                status = "unknown"

            results.append({
                "device_id": d.device_id,
                "username": acct_username,
                "total_checks": total_checks,
                "unique_content_seen": unique_content,
                "last_new_content_ago": last_new_content_ago,
                "content_frequency": frequency,
                "status": status,
            })

    return {"total": len(results), "devices": results}


# ── Scheduler ──────────────────────────────────────────────

@router.get("/scheduler/status")
async def scheduler_status():
    return get_scheduler_status()


@router.post("/scheduler/start")
async def scheduler_start(
    hour: Optional[int] = Query(None, ge=0, le=23),
    minute: Optional[int] = Query(None, ge=0, le=59),
):
    start_scheduler(hour=hour, minute=minute)
    return get_scheduler_status()


@router.post("/scheduler/stop")
async def scheduler_stop():
    stop_scheduler()
    return get_scheduler_status()


# ── Trial Reels ──────────────────────────────────────────

@router.get("/metrics/trials")
async def get_trials(
    device_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    """Get trial reel history, newest first. Optionally filter by device."""
    reels = await get_trial_reels(device_id=device_id, limit=limit)
    await _enrich_urls(reels, kind="trial")
    return {"count": len(reels), "reels": reels}


@router.get("/metrics/trials/{device_id}")
async def get_device_trials(device_id: str):
    """Get the latest trial reels for a specific device."""
    reels = await get_latest_trial_reels(device_id)
    await _enrich_urls(reels, kind="trial")
    return {"device_id": device_id, "count": len(reels), "reels": reels}


# ═══════════════════════════════════════════════════════════
# Content Registry — Videos + Pending (merged from ig_content_api)
# ═══════════════════════════════════════════════════════════

class _VideoCreate(BaseModel):
    title: str
    url: str
    username: str = ""

class _VideoUpdate(BaseModel):
    title: str | None = None
    url: str | None = None
    username: str | None = None

class _PendingCreate(BaseModel):
    title: str
    username: str = ""
    device_id: str = ""

class _PendingResolve(BaseModel):
    url: str


@router.get("/content/videos")
async def content_get_videos(
    title: str | None = Query(None, description="Exact title lookup"),
    q: str | None = Query(None, description="Search by partial title"),
    username: str | None = Query(None, description="Filter by IG username"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """List, search, or lookup content videos."""
    if title:
        video = await content_get_by_title(title)
        if not video:
            raise HTTPException(status_code=404, detail=f"Video not found: {title}")
        return {"title": video["title"], "url": video["url"]}
    if q:
        results = await content_search(q)
        return {"count": len(results), "videos": results}
    videos = await content_list_videos(limit=limit, offset=offset, username=username)
    total = await content_count_videos()
    return {"count": len(videos), "total": total, "videos": videos}


@router.get("/content/usernames")
async def content_get_all_usernames():
    """Get distinct usernames from content registry."""
    usernames = await content_get_usernames()
    return {"usernames": usernames}


@router.post("/content/videos")
async def content_create_video(body: _VideoCreate):
    """Register a new video title → URL."""
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="Title cannot be empty")
    if not body.url.strip():
        raise HTTPException(status_code=400, detail="URL cannot be empty")
    try:
        video = await content_add_video(body.title, body.url, body.username)
        return {"ok": True, "video": video}
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=409, detail=f"Video with title '{body.title}' already exists")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/content/videos/set")
async def content_set_video(body: _VideoCreate):
    """Upsert a video URL by title — used by the inline URL input in each reel card.

    Replaces the Video Library tab workflow:
      - If title already registered: updates the URL.
      - If title is new: inserts.
      - If a pending entry exists with this title: cleans it up (since the
        URL is now provided).

    Returns the resulting video row.
    """
    title = body.title.strip()
    url = body.url.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty")
    if not url:
        raise HTTPException(status_code=400, detail="URL cannot be empty")
    try:
        # content_add_video uses INSERT OR REPLACE on title → upsert is automatic
        video = await content_add_video(title, url, body.username or "")
        # Clean up any pending row for this title — no-op if there is none
        try:
            from growth.database import content_list_pending, content_delete_pending
            pendings = await content_list_pending(limit=500)
            for p in pendings:
                if p.get("title", "").strip() == title:
                    await content_delete_pending(p["id"])
        except Exception as e:
            logger.warning(f"[content/set] pending cleanup failed for '{title}': {e}")
        return {"ok": True, "video": video}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/content/videos/by-title")
async def content_remove_by_title(title: str = Query(..., description="Exact title to remove")):
    """Remove a video by exact title — used by the inline 🗑 button in each reel card.

    Looks up the row id by title, deletes it, and also clears any leftover
    pending entry sharing the same title.

    Returns 404 if no video with that title exists.
    """
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty")
    video = await content_get_by_title(title)
    if not video:
        raise HTTPException(status_code=404, detail=f"No video registered with title '{title}'")
    try:
        await content_delete_video(video["id"])
        # Also clear any pending row for the same title (defensive — usually none)
        try:
            from growth.database import content_list_pending, content_delete_pending
            pendings = await content_list_pending(limit=500)
            for p in pendings:
                if p.get("title", "").strip() == title:
                    await content_delete_pending(p["id"])
        except Exception as e:
            logger.warning(f"[content/by-title] pending cleanup failed for '{title}': {e}")
        return {"ok": True, "deleted_id": video["id"], "title": title}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/content/videos/{video_id}")
async def content_edit_video(video_id: int, body: _VideoUpdate):
    """Update a registered video."""
    result = await content_update_video(video_id, title=body.title, url=body.url, username=body.username)
    if not result:
        raise HTTPException(status_code=404, detail="Video not found")
    return {"ok": True, "video": result}


@router.delete("/content/videos/{video_id}")
async def content_remove_video(video_id: int):
    """Delete a registered video."""
    deleted = await content_delete_video(video_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Video not found")
    return {"ok": True}


# ── Content Pending ──

@router.get("/content/pending")
async def content_get_pending():
    """List pending videos awaiting URL registration."""
    items = await content_list_pending()
    total = await content_count_pending()
    return {"count": total, "pending": items}


@router.post("/content/pending")
async def content_create_pending(body: _PendingCreate):
    """Add a video to pending queue (called by checker on unknown titles)."""
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="Title cannot be empty")
    result = await content_add_pending(body.title, body.username, body.device_id)
    if result is None:
        return {"ok": True, "skipped": True, "detail": "Already registered or pending"}
    return {"ok": True, "pending": result}


@router.post("/content/pending/{pending_id}/resolve")
async def content_resolve_pending_video(pending_id: int, body: _PendingResolve):
    """Resolve a pending video with a URL — moves to videos table."""
    if not body.url.strip():
        raise HTTPException(status_code=400, detail="URL cannot be empty")
    video = await content_resolve_pending(pending_id, body.url)
    if not video:
        raise HTTPException(status_code=404, detail="Pending video not found")
    return {"ok": True, "video": video}


@router.delete("/content/pending/all")
async def content_clear_pending():
    """Delete ALL pending videos."""
    count = await content_clear_all_pending()
    return {"ok": True, "deleted": count}


@router.delete("/content/pending/{pending_id}")
async def content_remove_pending(pending_id: int):
    """Delete/dismiss a pending video."""
    deleted = await content_delete_pending(pending_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Pending video not found")
    return {"ok": True, "deleted": True, "pending_id": pending_id}
