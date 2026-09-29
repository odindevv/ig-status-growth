"""
Orchestrator — coordinates combined checks across devices.
Handles concurrency via semaphore (ADB can't handle too many parallel ops).
Supports task tracking, cancellation, and background execution.
"""

import asyncio
import random
import time
import uuid
import httpx
from datetime import datetime, timezone
from config import status_settings as settings
from logger import logger
from status.registry import get_enabled_devices, get_monitored_devices, get_device_by_id
from status.database import save_check, prune_old_checks
from status.account_status import check_account, read_current_ig_username, switch_ig_account
from shared.device_lock import acquire_device_lock


_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None

# Per-device locks — prevent two checks on the same device at the same time
_device_locks: dict[str, asyncio.Lock] = {}

# Task tracking for cancellation support
_active_tasks: dict[str, asyncio.Task] = {}
_task_status: dict[str, dict] = {}


def _get_semaphore() -> asyncio.Semaphore:
    """Get or create semaphore, ensuring it belongs to the current event loop."""
    global _semaphore, _semaphore_loop
    current_loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not current_loop:
        _semaphore = asyncio.Semaphore(settings.max_parallel_devices)
        _semaphore_loop = current_loop
    return _semaphore


def _get_device_lock(device_id: str) -> asyncio.Lock:
    """Get or create per-device lock (prevents concurrent checks on same device)."""
    if device_id not in _device_locks:
        _device_locks[device_id] = asyncio.Lock()
    return _device_locks[device_id]


def is_device_busy(device_id: str) -> bool:
    """Check if a device currently has a check running."""
    lock = _device_locks.get(device_id)
    return lock is not None and lock.locked()


async def _start_scrcpy_session(device_id: str, max_attempts: int = 3) -> None:
    """Start scrcpy UHID session for a device (if enabled).

    Retries up to *max_attempts* times, killing any stale scrcpy process
    on the device between attempts.  Falls back to ADB input if all fail.
    """
    if not settings.use_scrcpy:
        return
    from shared.scrcpy_uhid import start_session

    for attempt in range(1, max_attempts + 1):
        _stop_scrcpy_session(device_id)
        if attempt > 1:
            await asyncio.sleep(1.5)

        try:
            ok = await start_session(device_id)
            if ok:
                logger.info(f"[orchestrator] Scrcpy UHID session started for {device_id} (attempt {attempt}/{max_attempts})")
                return
            else:
                logger.warning(f"[orchestrator] Scrcpy UHID attempt {attempt}/{max_attempts} failed on {device_id}")
        except Exception as e:
            logger.warning(f"[orchestrator] Scrcpy UHID attempt {attempt}/{max_attempts} error on {device_id}: {e}")

    logger.info(f"[orchestrator] Scrcpy UHID unavailable after {max_attempts} attempts on {device_id} — will use input tap fallback")


def _stop_scrcpy_session(device_id: str) -> None:
    """Stop scrcpy UHID session for a device (if any)."""
    try:
        from shared.scrcpy_uhid import stop_session
        stop_session(device_id)
    except Exception as e:
        logger.debug(f"[orchestrator] Scrcpy UHID stop error for {device_id}: {e}")


async def check_single(
    device_id: str,
    username: str = "",
    include_details: bool = True,
    proxy_enabled: bool = True,
) -> dict:
    """
    Combined check for a single device:
    Layer 2 (ADB state + Account Status).
    Protected by per-device lock to prevent concurrent ADB access.
    """
    device_lock = _get_device_lock(device_id)

    # Non-blocking: if device already has a check running, reject immediately
    if device_lock.locked():
        logger.warning(f"[orchestrator] Device {device_id} already busy — skipping")
        return {
            "device_id": device_id, "username": username, "success": False,
            "error": "Device busy (another check in progress)",
            "account_state": "unknown", "account_state_signal": None,
            "proxy": None, "summary": {}, "details": {}, "navigation_steps": [],
            "duration_ms": 0, "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    sem = _get_semaphore()

    async with device_lock, sem:
        # Start scrcpy UHID session for anti-detection touches
        await _start_scrcpy_session(device_id)

        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                check_account(
                    device_id=device_id,
                    username=username,
                    include_details=include_details,
                    proxy_enabled=proxy_enabled,
                ),
                timeout=settings.check_timeout,
            )
        except asyncio.TimeoutError:
            logger.error(f"[orchestrator] Check timed out after {settings.check_timeout}s for {device_id}")
            result = {
                "device_id": device_id,
                "username": username,
                "success": False,
                "error": f"Check timed out after {settings.check_timeout}s",
                "account_state": "unknown",
                "account_state_signal": None,
                "proxy": None,
                "summary": {},
                "details": {},
                "navigation_steps": [],
            }
        finally:
            # Always stop scrcpy session after single check
            _stop_scrcpy_session(device_id)
        elapsed = int((time.monotonic() - start) * 1000)

    result["duration_ms"] = elapsed
    result["checked_at"] = datetime.now(timezone.utc).isoformat()

    await save_check(result)

    # Report to hub (no-op if HUB_URL not configured)
    try:
        from shared.hub_reporter import report as hub_report
        await hub_report("status", result)
    except Exception as e:
        logger.warning(f"[orchestrator] Hub report failed for {device_id}: {e}")

    return result


async def _human_delay() -> None:
    """Sleep ~3 seconds with ±20% jitter to mimic human pacing."""
    base = 3.0
    jitter = base * 0.2
    await asyncio.sleep(base + random.uniform(-jitter, jitter))


async def check_device_accounts(
    device,
    include_details: bool = True,
) -> list[dict]:
    """
    Check all accounts registered on a device sequentially, sharing the
    proxy session.  Falls back to [device.username] when the new
    ``usernames`` list is empty.
    """
    usernames: list[str] = device.get_enabled_usernames() if hasattr(device, "get_enabled_usernames") else (getattr(device, "usernames", None) or [])
    if not usernames:
        usernames = [device.username]

    device_id = device.device_id
    proxy_enabled = device.proxy_enabled

    # ── Global cross-service lock (prevents Growth/Checker collision) ──
    try:
        async with acquire_device_lock(device_id, "status"):
            return await _check_device_accounts_inner(
                device_id, usernames, include_details, proxy_enabled,
            )
    except TimeoutError:
        return [{
            "device_id": device_id, "username": usernames[0], "success": False,
            "error": "Device busy (another service is using it)",
            "account_state": "unknown", "account_state_signal": None,
            "proxy": None, "summary": {}, "details": {}, "navigation_steps": [],
            "duration_ms": 0, "checked_at": datetime.now(timezone.utc).isoformat(),
        }]


async def _check_device_accounts_inner(
    device_id: str,
    usernames: list[str],
    include_details: bool,
    proxy_enabled: bool,
) -> list[dict]:
    """Inner logic for check_device_accounts, runs under the global device lock."""

    # Single account — fast path, identical to the original behaviour.
    if len(usernames) == 1:
        return [await check_single(
            device_id=device_id,
            username=usernames[0],
            include_details=include_details,
            proxy_enabled=proxy_enabled,
        )]

    # ── Multiple accounts on the same device ──────────────────
    results: list[dict] = []
    device_lock = _get_device_lock(device_id)

    if device_lock.locked():
        logger.warning(f"[orchestrator] Device {device_id} already busy — skipping multi-account check")
        return [{
            "device_id": device_id, "username": usernames[0], "success": False,
            "error": "Device busy (another check in progress)",
            "account_state": "unknown", "account_state_signal": None,
            "proxy": None, "summary": {}, "details": {}, "navigation_steps": [],
            "duration_ms": 0, "checked_at": datetime.now(timezone.utc).isoformat(),
        }]

    sem = _get_semaphore()

    async with device_lock, sem:
        # Start scrcpy UHID session for anti-detection touches
        await _start_scrcpy_session(device_id)

        # -- Pre-step: detect current IG account & reorder to avoid unnecessary switches --
        # If IG is already open on one of our accounts, check that one first.
        # This is opportunistic — if we can't read the username, we keep the original order.
        # We always remember the original primary for switch_back at the end.
        primary_username = usernames[0]  # always the device's primary account

        if len(usernames) > 1:
            try:
                from shared.adb import is_instagram_foreground
                if await is_instagram_foreground(device_id):
                    pre_username = await read_current_ig_username(device_id)
                    if pre_username and pre_username in usernames and pre_username != usernames[0]:
                        old_order = list(usernames)
                        usernames.remove(pre_username)
                        usernames.insert(0, pre_username)
                        logger.info(
                            f"[orchestrator] Reordered accounts: IG already on @{pre_username} — "
                            f"checking first to avoid extra switch "
                            f"(was {old_order} → now {usernames}) on {device_id}"
                        )
            except Exception as e:
                logger.debug(f"[orchestrator] Pre-step username detection skipped on {device_id}: {e}")

        # -- Step (a): first account — starts proxy, keeps IG open ------
        first_username = usernames[0]
        start = time.monotonic()
        try:
            first_result = await asyncio.wait_for(
                check_account(
                    device_id=device_id,
                    username=first_username,
                    include_details=include_details,
                    proxy_enabled=proxy_enabled,
                    _skip_cleanup=True,
                ),
                timeout=settings.check_timeout,
            )
        except asyncio.TimeoutError:
            logger.error(f"[orchestrator] First account timed out for {device_id}/{first_username}")
            first_result = {
                "device_id": device_id,
                "username": first_username,
                "success": False,
                "error": f"Check timed out after {settings.check_timeout}s",
                "account_state": "unknown",
                "account_state_signal": None,
                "proxy": None,
                "summary": {},
                "details": {},
                "navigation_steps": [],
            }
        elapsed = int((time.monotonic() - start) * 1000)
        first_result["duration_ms"] = elapsed
        first_result["checked_at"] = datetime.now(timezone.utc).isoformat()
        await _save_single_check(first_result, device_id, first_username)
        results.append(first_result)

        # -- Proxy failure on first account → skip all remaining --------
        first_error = first_result.get("error") or ""
        if "proxy_no_connection" in first_error:
            logger.warning(
                f"[orchestrator] Proxy failed on first account {device_id}/{first_username} "
                f"— skipping all remaining accounts on this device"
            )
            remaining_usernames = [u for u in usernames if u != first_username]
            for skip_user in remaining_usernames:
                skip_result = {
                    "device_id": device_id,
                    "username": skip_user,
                    "success": False,
                    "error": f"Skipped: proxy failed on first account ({first_error})",
                    "account_state": "unknown",
                    "account_state_signal": None,
                    "proxy": None,
                    "summary": {},
                    "details": {},
                    "navigation_steps": ["SKIPPED:proxy_failed_on_device"],
                    "duration_ms": 0,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                results.append(skip_result)
                # Persist skip so latest_status reflects it (parity with growth)
                await _save_single_check(skip_result, device_id, skip_user)
            # check_account's finally with _skip_cleanup=True does NOT stop proxy,
            # so we must do full cleanup here: back out, HOME, stop proxy
            try:
                from shared.adb import press_back as _pb, press_home as _ph, force_portrait as _fp
                for _ in range(5):
                    await _pb(device_id)
                    await asyncio.sleep(0.4)
                await _ph(device_id)
                logger.debug(f"[orchestrator] Pressed HOME on {device_id} — IG minimized (proxy skip cleanup)")

                if proxy_enabled:
                    await asyncio.sleep(1.0)
                    logger.info(f"[orchestrator] Stopping proxy on {device_id} (proxy failure skip)")
                    try:
                        from status.proxy import stop_proxy
                        stop_result = await asyncio.shield(stop_proxy(device_id))
                    except asyncio.CancelledError:
                        stop_result = {"success": False, "state": "error", "error": "cancelled"}
                    if stop_result["success"]:
                        logger.info(f"[orchestrator] Proxy stopped on {device_id}")
                    else:
                        logger.warning(f"[orchestrator] Failed to stop proxy on {device_id}: {stop_result.get('error')}")

                await _fp(device_id)
            except Exception as e:
                logger.warning(f"[orchestrator] Cleanup error during proxy skip on {device_id}: {e}")

            _stop_scrcpy_session(device_id)
            return results

        # -- Step (b): detect currently-logged-in account & reorder -----
        try:
            current_ig = await read_current_ig_username(device_id)
        except Exception:
            current_ig = None

        remaining = [u for u in usernames if u != first_username]
        if current_ig and current_ig in remaining:
            remaining.remove(current_ig)
            remaining.insert(0, current_ig)

        # -- Step (c/d): iterate remaining accounts ---------------------
        for idx, next_username in enumerate(remaining):
            is_last = idx == len(remaining) - 1

            await _human_delay()

            try:
                switched = await switch_ig_account(device_id, next_username)
            except Exception as e:
                logger.error(f"[orchestrator] Account switch failed {device_id}/{next_username}: {e}")
                fail_result = {
                    "device_id": device_id,
                    "username": next_username,
                    "success": False,
                    "error": f"Account switch failed: {e}",
                    "account_state": "unknown",
                    "account_state_signal": None,
                    "proxy": None,
                    "summary": {},
                    "details": {},
                    "navigation_steps": [],
                    "duration_ms": 0,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                results.append(fail_result)
                await _save_single_check(fail_result, device_id, next_username)
                continue

            if not switched:
                logger.warning(f"[orchestrator] Account @{next_username} not found on device {device_id} — skipping")
                skip_result = {
                    "device_id": device_id,
                    "username": next_username,
                    "success": False,
                    "error": f"Account @{next_username} not found on device (not in IG dropdown)",
                    "account_state": "unknown",
                    "account_state_signal": None,
                    "proxy": None,
                    "summary": {},
                    "details": {},
                    "navigation_steps": ["SKIPPED:account_not_found"],
                    "duration_ms": 0,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                results.append(skip_result)
                await _save_single_check(skip_result, device_id, next_username)
                continue

            start = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    check_account(
                        device_id=device_id,
                        username=next_username,
                        include_details=include_details,
                        proxy_enabled=proxy_enabled,
                        _skip_proxy_lifecycle=True,
                        _skip_cleanup=True,  # always skip — orchestrator handles cleanup
                    ),
                    timeout=settings.check_timeout,
                )
            except asyncio.TimeoutError:
                logger.error(f"[orchestrator] Timed out for {device_id}/{next_username}")
                result = {
                    "device_id": device_id,
                    "username": next_username,
                    "success": False,
                    "error": f"Check timed out after {settings.check_timeout}s",
                    "account_state": "unknown",
                    "account_state_signal": None,
                    "proxy": None,
                    "summary": {},
                    "details": {},
                    "navigation_steps": [],
                }
            elapsed = int((time.monotonic() - start) * 1000)
            result["duration_ms"] = elapsed
            result["checked_at"] = datetime.now(timezone.utc).isoformat()
            await _save_single_check(result, device_id, next_username)
            results.append(result)

            # Proxy failure mid-loop → skip all remaining accounts
            result_error = result.get("error") or ""
            if "proxy_no_connection" in result_error:
                logger.warning(
                    f"[orchestrator] Proxy failed on {device_id}/{next_username} "
                    f"— skipping remaining accounts"
                )
                for skip_user in remaining[idx + 1:]:
                    skip_result = {
                        "device_id": device_id,
                        "username": skip_user,
                        "success": False,
                        "error": f"Skipped: proxy failed ({result_error})",
                        "account_state": "unknown",
                        "account_state_signal": None,
                        "proxy": None,
                        "summary": {},
                        "details": {},
                        "navigation_steps": ["SKIPPED:proxy_failed_on_device"],
                        "duration_ms": 0,
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    }
                    results.append(skip_result)
                    await _save_single_check(skip_result, device_id, skip_user)
                break

        # ── Switch back to primary account (while still inside IG) ──
        # Always switch back to the device's original primary, not the
        # reordered first_username (which may differ if we optimized order).
        if remaining:
            last_checked_username = None
            for r in reversed(results):
                if r.get("success") is not False:
                    last_checked_username = r.get("username")
                    break
            if last_checked_username and last_checked_username != primary_username:
                try:
                    logger.info(f"[orchestrator] Switching back to primary @{primary_username} on {device_id}")
                    switched_back = await switch_ig_account(device_id, primary_username)
                    if switched_back:
                        logger.info(f"[orchestrator] Switched back to primary @{primary_username} on {device_id}")
                    else:
                        logger.warning(f"[orchestrator] Could not switch back to @{primary_username} on {device_id}")
                except Exception as e:
                    logger.warning(f"[orchestrator] Switch-back to primary failed on {device_id}: {e}")

        # ── Orchestrator cleanup: back out of IG → HOME → stop proxy ──
        try:
            from shared.adb import press_back as _pb, press_home as _ph, force_portrait as _fp
            for _ in range(5):
                await _pb(device_id)
                await asyncio.sleep(0.4)
            await _ph(device_id)
            logger.debug(f"[orchestrator] Pressed HOME on {device_id} — IG minimized")

            if proxy_enabled:
                await asyncio.sleep(1.0)
                logger.info(f"[orchestrator] Stopping proxy on {device_id}")
                try:
                    from status.proxy import stop_proxy
                    stop_result = await asyncio.shield(stop_proxy(device_id))
                except asyncio.CancelledError:
                    logger.warning(f"[orchestrator] Task cancelled but proxy stop was shielded on {device_id}")
                    stop_result = {"success": False, "state": "error", "error": "Task cancelled during proxy stop"}
                if stop_result["success"]:
                    logger.info(f"[orchestrator] Proxy stopped on {device_id} — left on proxy screen")
                else:
                    logger.warning(f"[orchestrator] Failed to stop proxy on {device_id}: {stop_result.get('error')}")

            await _fp(device_id)
        except Exception as e:
            logger.warning(f"[orchestrator] Cleanup error on {device_id}: {e}")

        # Always stop scrcpy session after all accounts are checked
        _stop_scrcpy_session(device_id)

    return results


async def _save_single_check(result: dict, device_id: str, username: str) -> None:
    """Persist a single account's check result to the DB."""
    try:
        await save_check(result)
    except Exception as e:
        logger.error(f"[orchestrator] DB save failed for {device_id}/{username}: {e}")
        result["db_error"] = str(e)

    # Report to hub (no-op if HUB_URL not configured)
    try:
        from shared.hub_reporter import report as hub_report
        await hub_report("status", result)
    except Exception as e:
        logger.warning(f"[orchestrator] Hub report failed for {device_id}/{username}: {e}")


async def run_check_all(include_details: bool = True, monitored_only: bool = True) -> list[dict]:
    """
    Check devices in parallel (limited by semaphore).
    monitored_only=True: only devices with monitored=True (default for scheduled/force checks).
    monitored_only=False: all enabled devices.
    Also prunes old history records.
    """
    devices = get_monitored_devices() if monitored_only else get_enabled_devices()
    local_ids = {d.device_id for d in devices}

    # ── Dispatch remote worker devices FIRST (fire-and-forget) ──
    # Workers run independently, so dispatch immediately before starting
    # local checks.  This way workers and locals run in parallel instead
    # of sequentially.
    try:
        from shared.worker_registry import get_worker_devices, fire_and_forget_to_worker, register_remote_check
        remote_devices = get_worker_devices("status")
        remote_targets = []
        for rd in remote_devices:
            if rd.get("device_id") in local_ids:
                continue
            if not rd.get("enabled", False):
                continue
            if monitored_only and not rd.get("monitored", False):
                continue
            remote_targets.append(rd)

        if remote_targets:
            logger.info(f"[orchestrator] Dispatching status checks to {len(remote_targets)} remote worker devices")

            async def _dispatch_one(rd):
                dev_id = rd["device_id"]
                wid = rd.get("_worker_id", "?")
                try:
                    await fire_and_forget_to_worker(
                        dev_id, "POST",
                        f"/api/v1/status/check/force/{dev_id}?details={str(include_details).lower()}"
                    )
                    register_remote_check("status", dev_id, wid)
                    logger.info(f"[orchestrator] Dispatched remote status check for {dev_id} on worker {wid}")
                except (httpx.ReadTimeout, httpx.ConnectTimeout):
                    register_remote_check("status", dev_id, wid)
                    logger.info(f"[orchestrator] Remote status check accepted (timeout) for {dev_id} on worker {wid}")
                except Exception as e:
                    logger.warning(f"[orchestrator] Failed to dispatch remote status check for {dev_id}: {e}")

            await asyncio.gather(*[_dispatch_one(rd) for rd in remote_targets])
    except ImportError:
        pass

    logger.info(f"[orchestrator] Starting status check — {len(devices)} local devices")

    # Prune old history in background (non-blocking)
    asyncio.create_task(_safe_prune())

    # ── Local devices ──
    tasks = [
        check_device_accounts(d, include_details=include_details)
        for d in devices
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    clean_results = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.error(f"[orchestrator] Device {devices[i].device_id} failed: {r}")
            clean_results.append({
                "device_id": devices[i].device_id,
                "username": devices[i].username,
                "success": False,
                "error": str(r),
                "account_state": "unknown",
                "proxy": None,
                "summary": {},
                "details": {},
                "navigation_steps": [],
                "duration_ms": 0,
            })
        elif isinstance(r, list):
            clean_results.extend(r)
        else:
            clean_results.append(r)

    return clean_results


async def _safe_prune() -> None:
    """Prune old checks, swallowing exceptions."""
    try:
        await prune_old_checks()
    except Exception as e:
        logger.warning(f"[orchestrator] Prune failed: {e}")


# ── Task tracking ───
def get_active_tasks() -> list[dict]:
    """Return list of active/running check tasks."""
    _cleanup_finished_tasks()
    return [
        {
            "task_id": tid,
            "device_id": info.get("device_id", "all"),
            "status": "running" if not _active_tasks[tid].done() else "finished",
            "started_at": info.get("started_at"),
        }
        for tid, info in _task_status.items()
        if tid in _active_tasks
    ]


def get_activity_summary() -> dict:
    """Return current activity state for the global /api/v1/activity endpoint."""
    _cleanup_finished_tasks()
    active_devices = []
    for device_id, lock in _device_locks.items():
        if lock.locked():
            info = {}
            for tid, ts in _task_status.items():
                if ts.get("device_id") == device_id and tid in _active_tasks and not _active_tasks[tid].done():
                    info = ts
                    break
            # Include label for UI display
            dev = get_device_by_id(device_id)
            active_devices.append({
                "device_id": device_id,
                "label": dev.label if dev else "",
                "status": "checking",
                "started_at": info.get("started_at", ""),
            })

    if not active_devices:
        return {"active": False}

    running_tasks = [tid for tid, t in _active_tasks.items() if not t.done()]
    is_all = any(
        _task_status.get(tid, {}).get("device_id") == "all"
        for tid in running_tasks
    )
    total_devices = 0
    if is_all:
        try:
            total_devices = len(get_monitored_devices()) or len(get_enabled_devices())
        except Exception:
            total_devices = len(active_devices)

    return {
        "active": True,
        "mode": "all" if is_all else "single",
        "total_devices": total_devices if is_all else len(active_devices),
        "completed_devices": max(0, total_devices - len(active_devices)) if is_all else 0,
        "devices": active_devices,
    }


def cancel_task(task_id: str) -> bool:
    """Cancel a running check task. Returns True if cancelled."""
    task = _active_tasks.get(task_id)
    if not task or task.done():
        return False
    task.cancel()
    logger.info(f"[orchestrator] Task {task_id} cancelled")
    return True


def cancel_all_tasks() -> int:
    """Cancel all running check tasks. Returns count of cancelled tasks."""
    count = 0
    for tid, task in list(_active_tasks.items()):
        if not task.done():
            task.cancel()
            count += 1
    if count:
        logger.info(f"[orchestrator] Cancelled {count} active tasks")
    return count


def _cleanup_finished_tasks() -> None:
    """Remove finished tasks from tracking (keep last 50)."""
    finished = [tid for tid, t in _active_tasks.items() if t.done()]
    if len(finished) > 50:
        for tid in finished[:-50]:
            _active_tasks.pop(tid, None)
            _task_status.pop(tid, None)


def track_task(coro, device_id: str = "all") -> str:
    """Wrap a coroutine in a tracked asyncio.Task. Returns task_id."""
    task_id = str(uuid.uuid4())[:8]
    task = asyncio.create_task(coro)
    _active_tasks[task_id] = task
    _task_status[task_id] = {
        "device_id": device_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    return task_id
