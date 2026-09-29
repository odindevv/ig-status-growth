"""
Check runner — orchestrates ADB-based scraping + DB persistence + alerts.

Uses scrape_batch() for multi-account checks (single ADB session) and
supports a live queue: individual checks requested while a batch is running
get appended to the current session instead of being rejected.
"""

import asyncio
from datetime import datetime, timezone

from logger import logger
from checker.database import (
    list_accounts, save_check, get_previous, get_latest,
    cleanup_old_history,
)
from checker.scraper import scrape_profile, scrape_batch
from shared.device_lock import acquire_device_lock

_run_lock = asyncio.Lock()  # prevents concurrent batch runs on same device
_running = False
_last_run: str = ""
_last_results: list[dict] = []
_active_usernames: list[str] = []  # accounts being checked in current batch

# ── Activity tracking for /api/v1/activity ────────────────────
_trigger: str = ""          # "scheduler" | "manual" | "api"
_mode: str = ""             # "all" | "single" | "queued"
_total_count: int = 0
_completed_count: int = 0
_current_username: str = ""
_started_at: str = ""

# ── Proxy error state ─────────────────────────────────────────
_proxy_error: bool = False
_proxy_error_message: str = ""
_proxy_error_at: str = ""

# ── Live queue for mid-session additions ────────────────────
_queue: asyncio.Queue[str] = asyncio.Queue()
_queued_usernames: list[str] = []  # ordered snapshot for position lookups


def is_running() -> bool:
    return _running


def last_run_info() -> dict:
    info = {
        "running": _running,
        "last_run": _last_run,
        "last_count": len(_last_results),
        "active_usernames": list(_active_usernames),
        "queued": list(_queued_usernames),
    }
    if _proxy_error:
        info["proxy_error"] = True
        info["proxy_error_message"] = _proxy_error_message
        info["proxy_error_at"] = _proxy_error_at
    else:
        info["proxy_error"] = False
    return info


def get_activity_summary() -> dict:
    """Return current activity state for the global /api/v1/activity endpoint."""
    if not _running:
        return {"active": False}
    # Current = first in active list (being checked now), rest = pending
    current = _active_usernames[0] if _active_usernames else ""
    pending = _active_usernames[1:] if len(_active_usernames) > 1 else []
    return {
        "active": True,
        "mode": _mode,
        "trigger": _trigger,
        "total": _total_count,
        "completed": _completed_count,
        "current": current,
        "pending": list(pending),
        "started_at": _started_at,
    }


def clear_proxy_error() -> dict:
    """Clear the proxy error state manually."""
    global _proxy_error, _proxy_error_message, _proxy_error_at
    _proxy_error = False
    _proxy_error_message = ""
    _proxy_error_at = ""
    logger.info("[runner] Proxy error state cleared")
    return {"cleared": True}


def _set_proxy_error(message: str) -> None:
    """Set the proxy error state."""
    global _proxy_error, _proxy_error_message, _proxy_error_at
    _proxy_error = True
    _proxy_error_message = message
    _proxy_error_at = datetime.now(timezone.utc).isoformat()
    logger.error(f"[runner] PROXY ERROR flagged: {message}")


def queue_check(username: str) -> dict:
    """Enqueue a username to be checked by the running batch.

    Returns queue status dict. Safe to call from sync context
    (Queue.put_nowait is non-blocking).
    """
    clean = username.lower().strip().lstrip("@")
    if clean in _queued_usernames:
        pos = _queued_usernames.index(clean) + 1
        logger.info(f"[runner] @{clean} already queued at position {pos}")
        return {"queued": True, "username": clean, "position": pos, "duplicate": True}

    _queue.put_nowait(clean)
    _queued_usernames.append(clean)
    _active_usernames.append(clean)
    pos = len(_queued_usernames)
    logger.info(f"[runner] Queued @{clean} at position {pos}")
    return {"queued": True, "username": clean, "position": pos, "duplicate": False}


def _drain_queue() -> list[str]:
    """Drain all pending usernames from the queue (non-blocking)."""
    drained = []
    while not _queue.empty():
        try:
            drained.append(_queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return drained


def _clear_queue() -> None:
    """Clear the queue and snapshot."""
    while not _queue.empty():
        try:
            _queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    _queued_usernames.clear()


async def _process_result(data: dict, username_fallback: str) -> dict:
    """Process a single scrape result: save to DB, calculate deltas."""
    username = data.get("username", username_fallback)

    prev = await get_previous(username)
    await save_check(username, data)

    # Report to hub (no-op if HUB_URL not configured)
    try:
        from shared.hub_reporter import report as hub_report
        await hub_report("checker", {**data, "username": username})
    except Exception as e:
        from logger import logger as _log
        _log.debug(f"[runner] Hub report failed for {username}: {e}")

    check_result = {
        "username": username,
        "status": data["status"],
        "followers": data.get("followers"),
        "following": data.get("following"),
        "posts": data.get("posts"),
        "bio": data.get("bio", ""),
        "full_name": data.get("full_name", ""),
        "is_private": data.get("is_private", False),
        "error": data.get("error", ""),
        "delta_followers": None,
        "delta_following": None,
        "delta_posts": None,
    }

    if prev and data["status"] == "ok":
        pf = prev.get("followers")
        cf = data.get("followers")
        if pf is not None and cf is not None:
            check_result["delta_followers"] = cf - pf

        pfg = prev.get("following")
        cfg = data.get("following")
        if pfg is not None and cfg is not None:
            check_result["delta_following"] = cfg - pfg

        pp = prev.get("posts")
        cp = data.get("posts")
        if pp is not None and cp is not None:
            check_result["delta_posts"] = cp - pp

    return check_result


async def run_check(
    usernames: list[str] | None = None,
    notify: bool = True,
    trigger: str = "manual",
) -> list[dict]:
    """
    Run a full check cycle using ADB batch mode.

    Opens Instagram once, navigates to search, then checks each account
    sequentially: type username -> tap result -> read profile -> back -> clear -> next.

    The batch consumes from _queue between accounts -- any checks queued
    while the batch is running get picked up without restarting the session.

    Args:
        usernames: Specific accounts to check. None = all enabled accounts.
        notify: Send Telegram alerts for anomalies.

    Returns:
        List of check results with deltas.
    """
    global _running, _last_run, _last_results, _active_usernames

    # Use lock to prevent concurrent batch runs (scheduler + API race)
    if _run_lock.locked():
        logger.warning("[runner] Check already in progress -- skipping")
        return []

    async with _run_lock:
        _running = True
        results = []
        accounts = []

        # Set activity tracking
        global _trigger, _mode, _total_count, _completed_count, _current_username, _started_at
        _trigger = trigger
        _mode = "single" if usernames and len(usernames) == 1 else ("all" if not usernames else "manual")
        _started_at = datetime.now(timezone.utc).isoformat()
        _completed_count = 0
        _current_username = ""

        # Clear proxy error at start of new batch (fresh attempt)
        global _proxy_error, _proxy_error_message, _proxy_error_at
        _proxy_error = False
        _proxy_error_message = ""
        _proxy_error_at = ""

        try:
            # Resolve which accounts to check
            if usernames:
                accounts = [u.lower().strip().lstrip("@") for u in usernames]
            else:
                rows = await list_accounts(enabled_only=True)
                accounts = [r["username"] for r in rows]

            if not accounts:
                logger.info("[runner] No accounts to check")
                return []

            _active_usernames = list(accounts)
            _total_count = len(accounts)
            logger.info(f"[runner] Starting ADB batch check for {len(accounts)} accounts: {accounts}")

            # -- Callback: save each result to DB as soon as it's scraped --
            _result_counter = 0

            async def _on_scrape_result(data: dict) -> None:
                nonlocal _result_counter
                _result_counter += 1
                username = data.get("username", "unknown")

                check_result = await _process_result(data, username)
                results.append(check_result)

                # Update activity tracking
                _completed_count = _result_counter

                # Remove from active list so frontend knows it's done
                if username in _active_usernames:
                    _active_usernames.remove(username)

                status_msg = data["status"]
                error_msg = data.get("error", "")

                # Detect VPN/proxy failure -> flag for UI
                if "VPN recovery failed" in error_msg or "Proxy failed" in error_msg:
                    _set_proxy_error(
                        f"Proxy/VPN failed and could not recover -- needs technical support. "
                        f"Last account attempted: @{username}"
                    )

                if status_msg == "ok":
                    logger.info(
                        f"[runner] [{_result_counter}] {username} -> "
                        f"{data.get('followers', '?')} followers, "
                        f"{data.get('following', '?')} following, "
                        f"{data.get('posts', '?')} posts"
                    )
                else:
                    logger.info(
                        f"[runner] [{_result_counter}] {username} -> "
                        f"{status_msg} (err: {error_msg})"
                    )

            # -- Use batch mode with live queue + immediate DB saves --
            # Global cross-service lock (prevents Growth/Status collision)
            from config import checker_settings as _cs
            _checker_device_id = _cs.device_id or "checker_default"
            try:
                async with acquire_device_lock(_checker_device_id, "checker"):
                    await scrape_batch(accounts, queue=_queue, on_result=_on_scrape_result)
            except TimeoutError:
                logger.error(f"[runner] Device {_checker_device_id} busy (another service) — aborting batch")
                return []

            _last_run = datetime.now(timezone.utc).isoformat()
            _last_results = results

            # Summary
            ok_count = sum(1 for r in results if r["status"] == "ok")
            err_count = len(results) - ok_count
            logger.info(f"[runner] Check complete: {len(results)} processed -- {ok_count} ok, {err_count} errors")
            if err_count:
                failed = [r["username"] for r in results if r["status"] != "ok"]
                logger.warning(f"[runner] Failed accounts: {failed}")

            # Send alerts if enabled
            if notify:
                await _send_alerts(results)

            # Cleanup old history
            await cleanup_old_history()

        except Exception as e:
            logger.error(f"[runner] FATAL error during check after {len(results)} accounts: {e}", exc_info=True)
        finally:
            _running = False
            _active_usernames = []
            _current_username = ""
            _clear_queue()

        return results


async def run_single_check(username: str, notify: bool = False) -> dict:
    """Check a single account via ADB.

    If a batch is already running, queues the username and returns
    a queued status instead of blocking or failing.
    """
    if _run_lock.locked():
        return queue_check(username)

    results = await run_check(usernames=[username], notify=notify)
    return results[0] if results else {"username": username, "status": "error", "error": "Check failed"}


async def _send_alerts(results: list[dict]) -> None:
    """Send Telegram alerts for significant changes."""
    from config import checker_settings as settings

    if not settings.telegram_bot_enabled or not settings.telegram_chat_id:
        return

    alerts = []
    for r in results:
        if r["status"] != "ok":
            if r["status"] == "not_found":
                alerts.append(f"Account @{r['username']} not found (deleted/banned?)")
            continue

        df = r.get("delta_followers")
        if df is not None and r.get("followers"):
            prev_f = r["followers"] - df
            if prev_f > 0:
                pct = (df / prev_f) * 100
                if pct <= -settings.telegram_alert_drop_pct:
                    alerts.append(
                        f"@{r['username']} lost {abs(df)} followers ({pct:+.1f}%) "
                        f"-- now {r['followers']:,}"
                    )
                elif pct >= settings.telegram_alert_gain_pct:
                    alerts.append(
                        f"@{r['username']} gained {df} followers ({pct:+.1f}%) "
                        f"-- now {r['followers']:,}"
                    )

    if not alerts:
        return

    try:
        from checker.telegram_alerts import send_message
        sep = chr(9472) * 30
        header = f"Account Checker Alert ({len(alerts)} events)\n{sep}"
        msg = header + "\n" + "\n".join("* " + a for a in alerts)
        await send_message(msg)
        logger.info(f"[runner] Sent {len(alerts)} alert(s) via Telegram")
    except Exception as e:
        logger.error(f"[runner] Failed to send Telegram alerts: {e}")
