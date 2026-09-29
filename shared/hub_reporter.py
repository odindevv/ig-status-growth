"""
Worker → Hub result reporter + heartbeat.

When HUB_URL is configured, this module:
  1. Sends check results to the hub after each local save
  2. Sends periodic heartbeats with the worker's device list

Results are queued and retried on failure so the worker never blocks
or loses data if the hub is temporarily unreachable.

When HUB_URL is empty (default), all functions are no-ops.
"""

import asyncio
from collections import deque
from datetime import datetime, timezone

import httpx

from config import shared_settings as settings
from logger import logger

# ── Configuration ─────────────────────────────────────────
_HUB_URL = settings.hub_url.strip().rstrip("/")
_WORKER_ID = settings.worker_id.strip() or "worker"
_ENABLED = bool(_HUB_URL)
_VIRAL_ENDPOINT = f"{_HUB_URL}/api/v1/worker/viral" if _ENABLED else ""
_REPORT_ENDPOINT = f"{_HUB_URL}/api/v1/worker/report" if _ENABLED else ""
_HEARTBEAT_ENDPOINT = f"{_HUB_URL}/api/v1/worker/heartbeat" if _ENABLED else ""
_API_KEY = settings.api_key.strip()

# ── Retry queue ───────────────────────────────────────────
_pending: deque[dict] = deque(maxlen=500)  # cap to avoid memory leak
_flush_task: asyncio.Task | None = None
_heartbeat_task: asyncio.Task | None = None
_RETRY_INTERVAL = 30   # seconds between flush attempts
_HEARTBEAT_INTERVAL = 60  # seconds between heartbeats
_TIMEOUT = 15           # HTTP timeout per request


def _headers() -> dict:
    """Build request headers with API key."""
    h = {"Content-Type": "application/json"}
    if _API_KEY:
        h["X-API-Key"] = _API_KEY
    return h


def is_enabled() -> bool:
    """Check if hub reporting is active."""
    return _ENABLED


# ── Result reporting ──────────────────────────────────────

async def report(service: str, result: dict) -> None:
    """Send a check result to the hub.

    Args:
        service: "growth", "status", or "checker"
        result: the full result dict (same structure as saved locally)

    This function returns immediately.  If the hub is unreachable, the
    payload is queued and retried in the background.
    """
    if not _ENABLED:
        return

    payload = {
        "worker_id": _WORKER_ID,
        "service": service,
        "result": result,
        "reported_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(_REPORT_ENDPOINT, json=payload, headers=_headers())
            if resp.status_code == 200:
                logger.info(f"[hub] Reported {service} result for {result.get('device_id', '?')} to hub")
                return
            else:
                logger.warning(f"[hub] Hub returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"[hub] Failed to report to hub: {e}")

    # Queue for retry
    _pending.append(payload)
    _ensure_flush_task()


async def report_viral(alerts: list[dict]) -> None:
    """Forward viral alerts to the hub so the central dashboard sees them.

    The hub keeps a `viral_alerts` table; workers also keep their own. Without
    this call, only locally-detected alerts (i.e. devices wired directly to the
    hub) appear in the hub UI. With this call, alerts from every worker land
    in the hub's table and stay deduped by (device_id, content_id, time).

    No-op if HUB_URL is not configured or `alerts` is empty.
    """
    if not _ENABLED or not alerts:
        return
    payload = {
        "worker_id": _WORKER_ID,
        "alerts": alerts,
        "reported_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(_VIRAL_ENDPOINT, json=payload, headers=_headers())
            if resp.status_code == 200:
                logger.info(f"[hub] Reported {len(alerts)} viral alert(s) to hub")
                return
            logger.warning(
                f"[hub] Hub returned {resp.status_code} for viral report: {resp.text[:200]}"
            )
    except Exception as e:
        logger.warning(f"[hub] Failed to report viral alerts: {e}")


def _ensure_flush_task() -> None:
    """Start the background flush loop if not already running."""
    global _flush_task
    if _flush_task is None or _flush_task.done():
        try:
            _flush_task = asyncio.create_task(_flush_loop())
        except RuntimeError:
            pass  # no event loop yet


async def _flush_loop() -> None:
    """Background loop that retries sending queued payloads to the hub."""
    while _pending:
        await asyncio.sleep(_RETRY_INTERVAL)
        if not _pending:
            break

        payload = _pending[0]
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(_REPORT_ENDPOINT, json=payload, headers=_headers())
                if resp.status_code == 200:
                    _pending.popleft()
                    logger.info(f"[hub] Flushed queued {payload['service']} result to hub ({len(_pending)} remaining)")
                else:
                    logger.warning(f"[hub] Flush failed — hub returned {resp.status_code}")
        except Exception as e:
            logger.warning(f"[hub] Flush failed — {e} ({len(_pending)} queued)")

    logger.debug("[hub] Flush queue empty — background task exiting")


# ── Heartbeat ─────────────────────────────────────────────

def _collect_devices() -> dict:
    """Collect device lists from all three local registries/services."""
    services = {}

    # Growth devices
    try:
        from growth.registry import get_devices as growth_devs
        services["growth"] = [d.model_dump() for d in growth_devs()]
    except Exception:
        services["growth"] = []

    # Status devices
    try:
        from status.registry import get_devices as status_devs
        services["status"] = [d.model_dump() for d in status_devs()]
    except Exception:
        services["status"] = []

    # Checker doesn't have a device registry — it uses usernames
    # We send an empty list; checker results are reported via report()
    services["checker"] = []

    return services


async def _send_heartbeat() -> bool:
    """Send a single heartbeat to the hub. Returns True on success."""
    services = _collect_devices()

    # Worker URL: where the hub can reach us back
    # Prefer explicit WORKER_URL from .env, otherwise auto-detect
    _configured_url = settings.worker_url.strip() if settings.worker_url else ""
    if _configured_url:
        worker_url = _configured_url.rstrip("/")
    else:
        worker_port = 8001
        try:
            import socket
            local_ip = socket.gethostbyname(socket.gethostname())
            worker_url = f"http://{local_ip}:{worker_port}"
        except Exception:
            worker_url = f"http://127.0.0.1:{worker_port}"

    payload = {
        "worker_id": _WORKER_ID,
        "worker_url": worker_url,
        "services": services,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(_HEARTBEAT_ENDPOINT, json=payload, headers=_headers())
            if resp.status_code == 200:
                total = sum(len(devs) for devs in services.values())
                logger.info(f"[hub] Heartbeat sent — {total} device(s) reported to hub")
                return True
            else:
                logger.warning(f"[hub] Heartbeat failed — hub returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"[hub] Heartbeat failed — {e}")

    return False


async def _heartbeat_loop() -> None:
    """Background loop that sends heartbeats to the hub periodically."""
    # Initial heartbeat with a small delay (let registries load first)
    await asyncio.sleep(5)
    await _send_heartbeat()

    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL)
        await _send_heartbeat()


def start_heartbeat() -> None:
    """Start the periodic heartbeat background task.

    Should be called once during app startup (in lifespan).
    No-op if HUB_URL is not configured.
    """
    global _heartbeat_task
    if not _ENABLED:
        return
    if _heartbeat_task is not None and not _heartbeat_task.done():
        return  # already running
    _heartbeat_task = asyncio.create_task(_heartbeat_loop())
    logger.info(f"[hub] Heartbeat started — reporting to {_HUB_URL} every {_HEARTBEAT_INTERVAL}s as '{_WORKER_ID}'")


def stop_heartbeat() -> None:
    """Stop the heartbeat background task."""
    global _heartbeat_task
    if _heartbeat_task and not _heartbeat_task.done():
        _heartbeat_task.cancel()
        _heartbeat_task = None
