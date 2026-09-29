"""
API routes for Account Status checking, device management,
history queries, and scheduler control.
"""

import re
import time

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from typing import Optional

from status.orchestrator import (
    check_single, check_device_accounts, run_check_all, get_active_tasks, cancel_task,
    cancel_all_tasks, track_task,
)
from status.registry import (
    get_devices, get_enabled_devices, get_monitored_devices, get_device_by_id,
    load_devices, save_devices, update_device, add_device, remove_device,
    sync_with_adb,
)
from status.database import get_latest_statuses, get_history
from status.scheduler import start_scheduler, stop_scheduler, get_scheduler_status
from config import status_settings as settings
from logger import logger
from shared.adb import list_devices as adb_list_devices
from status.ui_parser import reload_signals, _signals


router = APIRouter(prefix="/api/v1/status", tags=["IG Status"])

# ── Validation ────────────────────────────────────────────
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]+$")


def _validate_device_id(device_id: str) -> None:
    """Validate device_id to prevent injection. Raises HTTPException on invalid."""
    if not device_id or not _DEVICE_ID_RE.match(device_id):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid device_id '{device_id}'. Only alphanumeric, dots, colons, hyphens allowed.",
        )


# ── Health ─────────────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok", "service": "ig-status-api"}


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
        remote = get_worker_devices("status")
    except Exception:
        remote = []

    all_devices = local + remote
    return {"total": len(all_devices), "devices": all_devices}


@router.get("/devices/discover")
async def discover_devices(
    limit: int = Query(50, ge=1, le=500, description="Max devices per category"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    filter_state: Optional[str] = Query(None, alias="state", description="Filter: online, offline, unregistered, all"),
):
    """Cross-reference ADB devices with registry. Supports pagination and state filter."""
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
                "usernames": getattr(dev, "usernames", []) or [],
                "label": dev.label,
                "enabled": dev.enabled,
                "proxy_enabled": dev.proxy_enabled,
                "monitored": dev.monitored,
                "proxy_rotate_url": getattr(dev, "proxy_rotate_url", "") or "",
                "model": adb_info.get("model", "unknown"),
                "state": "online",
            })
        else:
            state = adb_info["state"] if adb_info else "disconnected"
            offline_registered.append({
                "device_id": dev.device_id,
                "username": dev.username,
                "usernames": getattr(dev, "usernames", []) or [],
                "label": dev.label,
                "enabled": dev.enabled,
                "proxy_enabled": dev.proxy_enabled,
                "monitored": dev.monitored,
                "proxy_rotate_url": getattr(dev, "proxy_rotate_url", "") or "",
                "state": state,
            })

    for device_id, info in adb_ids.items():
        if device_id not in reg_ids:
            unregistered.append({
                "device_id": device_id,
                "state": info["state"],
                "model": info.get("model", "unknown"),
            })

    # Append remote worker devices (if any workers are connected)
    try:
        from shared.worker_registry import get_worker_devices
        remote = get_worker_devices("status")
        for rd in remote:
            online_registered.append({
                "device_id": rd.get("device_id", ""),
                "username": rd.get("username", ""),
                "usernames": rd.get("usernames", []),
                "label": rd.get("label", ""),
                "enabled": rd.get("enabled", False),
                "proxy_enabled": rd.get("proxy_enabled", True),
                "monitored": rd.get("monitored", False),
                "proxy_rotate_url": rd.get("proxy_rotate_url", ""),
                "state": "online",
                "_remote": True,
                "_worker_id": rd.get("_worker_id", ""),
                "_worker_url": rd.get("_worker_url", ""),
            })
    except Exception:
        pass

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
        "limit": limit,
        "offset": offset,
    }


@router.post("/devices/reload")
async def reload_devices():
    """Hot-reload devices.json."""
    devices = load_devices()
    return {"reloaded": True, "total": len(devices)}


@router.post("/devices/refresh")
async def refresh_adb_devices(
    auto_add: bool = Query(False, description="Auto-register new ADB devices into registry"),
):
    """Sync device registry with live ADB connections."""
    adb_devices = await adb_list_devices()
    report = await sync_with_adb(adb_devices, auto_add=auto_add)
    logger.info(f"[API] ADB refresh: {len(report['online'])} online, {len(report['offline'])} offline, {len(report['new_added'])} new")
    return report


@router.post("/devices/{device_id:path}/enable")
async def enable_device(device_id: str):
    _validate_device_id(device_id)
    dev = update_device(device_id, enabled=True)
    if not dev:
        try:
            from shared.worker_registry import proxy_to_worker, update_worker_device_cache
            r = await proxy_to_worker(device_id, "POST", f"/api/v1/status/devices/{device_id}/enable")
            if r:
                if r["status_code"] == 200:
                    update_worker_device_cache(device_id, "status", enabled=True)
                return JSONResponse(content=r["body"], status_code=r["status_code"])
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    save_devices()
    logger.info(f"[API] Device {device_id} ENABLED")
    return {"device_id": device_id, "enabled": True}


@router.post("/devices/{device_id:path}/disable")
async def disable_device(device_id: str):
    _validate_device_id(device_id)
    dev = update_device(device_id, enabled=False)
    if not dev:
        try:
            from shared.worker_registry import proxy_to_worker, update_worker_device_cache
            r = await proxy_to_worker(device_id, "POST", f"/api/v1/status/devices/{device_id}/disable")
            if r:
                if r["status_code"] == 200:
                    update_worker_device_cache(device_id, "status", enabled=False)
                return JSONResponse(content=r["body"], status_code=r["status_code"])
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    save_devices()
    logger.info(f"[API] Device {device_id} DISABLED")
    return {"device_id": device_id, "enabled": False}


@router.post("/devices/{device_id:path}/monitor")
async def monitor_device(device_id: str):
    """Enable monitoring for a device."""
    _validate_device_id(device_id)
    dev = update_device(device_id, monitored=True)
    if not dev:
        try:
            from shared.worker_registry import proxy_to_worker, update_worker_device_cache
            r = await proxy_to_worker(device_id, "POST", f"/api/v1/status/devices/{device_id}/monitor")
            if r:
                if r["status_code"] == 200:
                    update_worker_device_cache(device_id, "status", monitored=True)
                return JSONResponse(content=r["body"], status_code=r["status_code"])
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    save_devices()
    logger.info(f"[API] Device {device_id} MONITORING ENABLED")
    return {"device_id": device_id, "monitored": True}


@router.delete("/devices/{device_id:path}/monitor")
async def unmonitor_device(device_id: str):
    """Disable monitoring for a device."""
    _validate_device_id(device_id)
    dev = update_device(device_id, monitored=False)
    if not dev:
        try:
            from shared.worker_registry import proxy_to_worker, update_worker_device_cache
            r = await proxy_to_worker(device_id, "DELETE", f"/api/v1/status/devices/{device_id}/monitor")
            if r:
                if r["status_code"] == 200:
                    update_worker_device_cache(device_id, "status", monitored=False)
                return JSONResponse(content=r["body"], status_code=r["status_code"])
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    save_devices()
    logger.info(f"[API] Device {device_id} MONITORING DISABLED")
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
    """Update device fields (username, label, enabled, proxy_enabled, monitored, proxy_rotate_url, usernames, notes)."""
    _validate_device_id(device_id)
    dev = get_device_by_id(device_id)
    if not dev:
        # ── Proxy to worker if device is remote ──
        try:
            from shared.worker_registry import proxy_to_worker, update_worker_device_cache
            qs = request.url.query
            path = f"/api/v1/status/devices/{device_id}" + (f"?{qs}" if qs else "")
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
                        update_worker_device_cache(device_id, "status", **cache_fields)
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
            path = f"/api/v1/status/devices/{device_id}/accounts/{acct_username}/toggle" + (f"?{qs}" if qs else "")
            r = await proxy_to_worker(device_id, "PUT", path)
            if r:
                if r["status_code"] == 200:
                    update_worker_device_cache(device_id, "status")
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
    """Manually register a new device into the registry (devices.json)."""
    _validate_device_id(device_id)
    existing = get_device_by_id(device_id)
    if existing:
        raise HTTPException(status_code=409, detail=f"Device '{device_id}' already registered")
    dev = add_device(device_id=device_id, username=username, label=label, enabled=enabled)
    parsed_usernames = [{"username": u.strip().lstrip("@").lower(), "enabled": True}
                        for u in usernames.split(",") if u.strip()] if usernames else []
    update_device(device_id, proxy_enabled=proxy_enabled, monitored=monitored, proxy_rotate_url=proxy_rotate_url, usernames=parsed_usernames)
    save_devices()
    logger.info(f"[API] Device {device_id} (@{username}) ADDED to registry")
    return get_device_by_id(device_id).model_dump()


@router.delete("/devices/{device_id:path}")
async def delete_device_route(device_id: str):
    """Remove a device from the registry (devices.json). Does NOT affect ADB."""
    _validate_device_id(device_id)
    dev = get_device_by_id(device_id)
    if not dev:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    removed = remove_device(device_id)
    if not removed:
        raise HTTPException(status_code=500, detail="Failed to remove device")
    save_devices()
    logger.info(f"[API] Device {device_id} (@{dev.username}) DELETED from registry")
    return {"deleted": True, "device_id": device_id, "username": dev.username}


# ── Account Status Checking ───────────────────────────────

@router.post("/check/force")
async def check_force_all(
    details: bool = Query(True),
    background: bool = Query(False, description="Run in background — returns task_id immediately"),
    all_enabled: bool = Query(False, description="Check ALL enabled devices, not just monitored"),
):
    """Force check monitored devices (or all enabled with ?all_enabled=true).
    Use ?background=true for async execution (returns task_id to poll via /check/tasks).
    """
    logger.info(f"[API] /check/force (all_enabled={all_enabled}) triggered, details={details}, background={background}")

    monitored_only = not all_enabled

    if background:
        task_id = track_task(run_check_all(include_details=details, monitored_only=monitored_only), device_id="all")
        return {"task_id": task_id, "status": "running", "message": "Check started in background. Poll /check/tasks for status."}

    start = time.monotonic()
    results = await run_check_all(include_details=details, monitored_only=monitored_only)
    elapsed = int((time.monotonic() - start) * 1000)

    return {"total": len(results), "results": results, "duration_ms": elapsed}


@router.post("/check/force/{device_id:path}")
async def check_force_single(
    device_id: str,
    details: bool = Query(True),
    background: bool = Query(False, description="Run in background — return immediately"),
):
    """Force check ONE device."""
    _validate_device_id(device_id)
    device = get_device_by_id(device_id)
    if not device:
        # Remote worker device — fire the check and return immediately
        # The result will arrive async via hub_reporter
        try:
            from shared.worker_registry import fire_and_forget_to_worker, register_remote_check, find_worker_for_device
            result = await fire_and_forget_to_worker(
                device_id, "POST",
                f"/api/v1/status/check/force/{device_id}?details={str(details).lower()}"
            )
            if result:
                if result["status_code"] != 200:
                    return JSONResponse(content=result["body"], status_code=result["status_code"])
                return JSONResponse(content=result["body"], status_code=200)
        except httpx.ReadTimeout:
            found = find_worker_for_device(device_id)
            register_remote_check("status", device_id, found[0] if found else "unknown")
            return {"status": "accepted", "device_id": device_id, "message": "Check started on remote worker. Results will appear when complete."}
        except Exception as e:
            logger.warning(f"[status] Proxy check failed for {device_id}: {e}")
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    if not device.enabled:
        raise HTTPException(status_code=400, detail=f"Device '{device_id}' is disabled")

    usernames = device.usernames or []
    logger.info(f"[API] /check/force/{device_id} → @{device.username} (usernames={usernames}), details={details}, background={background}")

    # ── Background mode: dispatch and return immediately ──
    if background:
        task_id = track_task(
            check_device_accounts(device, include_details=details),
            device_id=device_id,
        )
        return {
            "task_id": task_id,
            "status": "running",
            "device_id": device_id,
            "message": "Check started in background.",
        }

    start = time.monotonic()
    results = await check_device_accounts(device, include_details=details)
    elapsed = int((time.monotonic() - start) * 1000)

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


# ── Status / History ───────────────────────────────────────

@router.get("/status/latest")
async def latest_statuses():
    """Return last known Account Status for every device."""
    data = await get_latest_statuses()

    for account in data:
        sections = account.get("sections", {})
        state = account.get("account_state", "unknown")

        if state != "active":
            account["all_ok"] = False
        elif not sections:
            account["all_ok"] = None
        else:
            statuses = [s.get("status", "unknown") for s in sections.values()]
            if all(s == "ok" for s in statuses):
                account["all_ok"] = True
            elif any(s == "problem" for s in statuses):
                account["all_ok"] = False
            else:
                account["all_ok"] = None

    return {"total": len(data), "accounts": data}


@router.get("/status/latest/{device_id:path}")
async def latest_status_single(device_id: str):
    _validate_device_id(device_id)
    all_statuses = await get_latest_statuses()
    status = None
    for s in all_statuses:
        if s.get("device_id") == device_id:
            status = s
            break
    if not status:
        raise HTTPException(status_code=404, detail=f"No status data for device '{device_id}'")

    history = await get_history(device_id=device_id, limit=1, offset=0)
    if history:
        last_check = history[0]
        status["summary"] = last_check.get("summary", {})
        status["details"] = last_check.get("details", {})
        status["navigation_steps"] = last_check.get("navigation_steps", [])
        status["duration_ms"] = last_check.get("duration_ms", 0)

    sections = status.get("sections", {})
    state = status.get("account_state", "unknown")
    if state != "active":
        status["all_ok"] = False
    elif not sections:
        status["all_ok"] = None
    else:
        statuses = [s.get("status", "unknown") for s in sections.values()]
        if all(s == "ok" for s in statuses):
            status["all_ok"] = True
        elif any(s == "problem" for s in statuses):
            status["all_ok"] = False
        else:
            status["all_ok"] = None

    return status


@router.get("/history")
async def query_history(
    device_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    records = await get_history(device_id=device_id, limit=limit, offset=offset)
    return {"total": len(records), "records": records}


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


# ── Signals ────────────────────────────────────────────────

@router.get("/signals")
async def get_signals():
    return _signals


@router.post("/signals/reload")
async def reload_ui_signals():
    reload_signals()
    logger.info("[API] UI signals reloaded")
    return {"reloaded": True}
