"""
Worker registry — Hub-side storage for remote worker state.

Stores device lists received from worker heartbeats.  The data lives
in-memory only (no persistence needed — workers re-send on startup).

When no workers are connected, all functions return empty results.
"""

import time
import httpx
from logger import logger
from config import shared_settings as _settings


# worker_id → {url, devices, last_seen, services}
_workers: dict[str, dict] = {}

# Timeout: if a worker hasn't sent a heartbeat in this many seconds,
# consider it offline.
_HEARTBEAT_TIMEOUT = 120  # 2 minutes

# ── Remote activity tracking ─────────────────────────────────
# Tracks checks dispatched to workers so the hub UI can show
# "CHECK IN PROGRESS" even though the check runs remotely.
# Key: (service, device_id) → {worker_id, started_at}
# Each /worker/report call from the worker RENEWS the timestamp (see
# main.py). Sliding-window semantics: the device stays "busy" as long as
# new reports keep arriving; expires N seconds after the last one.
# Calibrated to the observed gap between accounts (2-3.5 min for a real
# multi-account run), so 5 min won't expire mid-run, but releases shortly
# after the last account so the UI doesn't stay stuck.
_remote_activity: dict[tuple[str, str], dict] = {}
_REMOTE_ACTIVITY_TIMEOUT = 300  # 5 min sliding window after last report


def register_heartbeat(worker_id: str, worker_url: str, services: dict) -> None:
    """Update worker state from a heartbeat.

    Args:
        worker_id: unique name for the worker (e.g. "rdp2")
        worker_url: base URL of the worker API (e.g. "http://192.168.1.50:8001")
        services: dict with keys "growth", "status", "checker", each containing
                  a list of device dicts (same format as registry Device.model_dump())
    """
    _workers[worker_id] = {
        "url": worker_url.rstrip("/"),
        "services": services,
        "last_seen": time.time(),
    }
    total = sum(len(devs) for devs in services.values())
    logger.info(f"[worker_registry] Heartbeat from '{worker_id}' @ {worker_url} — {total} device(s)")


def get_worker(worker_id: str) -> dict | None:
    """Get a single worker's info, or None if not registered/offline."""
    w = _workers.get(worker_id)
    if not w:
        return None
    if time.time() - w["last_seen"] > _HEARTBEAT_TIMEOUT:
        return None
    return w


def get_all_workers() -> dict[str, dict]:
    """Return all online workers."""
    now = time.time()
    return {
        wid: w for wid, w in _workers.items()
        if now - w["last_seen"] <= _HEARTBEAT_TIMEOUT
    }


def get_worker_devices(service: str) -> list[dict]:
    """Get all devices from all online workers for a given service.

    Each device dict gets extra fields: _worker_id, _worker_url, _remote=True
    so the caller can distinguish them from local devices.
    """
    now = time.time()
    devices = []
    for wid, w in _workers.items():
        if now - w["last_seen"] > _HEARTBEAT_TIMEOUT:
            continue
        for dev in w["services"].get(service, []):
            dev_copy = dict(dev)
            dev_copy["_worker_id"] = wid
            dev_copy["_worker_url"] = w["url"]
            dev_copy["_remote"] = True
            devices.append(dev_copy)
    return devices


def update_worker_device_cache(device_id: str, service: str, **fields) -> bool:
    """Update fields on a cached worker device after a successful proxy write.

    This prevents the UI from showing stale data while waiting for the
    next heartbeat.  Only touches the in-memory cache — the worker holds
    the source of truth and will overwrite on the next heartbeat.

    Returns True if the device was found and updated.
    """
    now = time.time()
    for wid, w in _workers.items():
        if now - w["last_seen"] > _HEARTBEAT_TIMEOUT:
            continue
        for dev in w["services"].get(service, []):
            if dev.get("device_id") == device_id:
                dev.update(fields)
                logger.debug(
                    f"[worker_registry] Cache updated for {device_id} "
                    f"({service}): {fields}"
                )
                return True
    return False


def find_worker_for_device(device_id: str) -> tuple[str, str] | None:
    """Find which worker owns a device_id.

    Returns (worker_id, worker_url) or None.
    """
    now = time.time()
    for wid, w in _workers.items():
        if now - w["last_seen"] > _HEARTBEAT_TIMEOUT:
            continue
        for service_devs in w["services"].values():
            for dev in service_devs:
                if dev.get("device_id") == device_id:
                    return wid, w["url"]
    return None


async def fire_and_forget_to_worker(device_id: str, method: str, path: str,
                                    json_body: dict | None = None,
                                    timeout: int = 10) -> dict | None:
    """Send a request to the worker and return immediately once accepted.

    Unlike proxy_to_worker, this uses a SHORT timeout — it only waits
    long enough to confirm the worker received the request (not for it
    to finish).  Ideal for check/force which can take minutes.

    Returns {worker_id, worker_url} on success, or None if device not found.
    Raises on network error (worker unreachable).
    """
    found = find_worker_for_device(device_id)
    if not found:
        return None

    worker_id, worker_url = found
    target = f"{worker_url}{path}"

    headers = {}
    api_key = _settings.api_key.strip() if _settings.api_key else ""
    admin_key = _settings.admin_key.strip() if _settings.admin_key else ""
    key = admin_key or api_key
    if key:
        headers["X-API-Key"] = key

    logger.info(f"[worker_proxy] FIRE {method} {target} (device={device_id}, worker={worker_id})")

    # Use a short timeout — we only care that the worker accepted the job
    async with httpx.AsyncClient(timeout=timeout) as client:
        if method.upper() == "POST":
            resp = await client.post(target, json=json_body, headers=headers)
        else:
            resp = await client.get(target, headers=headers)

    # If worker returned quickly (e.g. 409 conflict, 400 error), relay it
    if resp.status_code != 200:
        return {"status_code": resp.status_code, "body": resp.json(), "worker_id": worker_id}

    # 200 means the check finished within the short timeout (unlikely for check/force)
    return {"status_code": resp.status_code, "body": resp.json(), "worker_id": worker_id}


async def proxy_to_worker(device_id: str, method: str, path: str,
                          json_body: dict | None = None, timeout: int = 120) -> dict | None:
    """Proxy an API request to the worker that owns the given device.

    Args:
        device_id: the device to look up
        method: "GET", "POST", "PUT", or "DELETE"
        path: the API path on the worker (e.g. "/api/v1/growth/check/force/DEVICE")
        json_body: optional JSON body for POST requests
        timeout: HTTP timeout in seconds

    Returns a dict with {status_code, body, worker_id}, or None if device not found.
    Raises httpx.HTTPError on network failures.
    """
    found = find_worker_for_device(device_id)
    if not found:
        return None

    worker_id, worker_url = found
    target = f"{worker_url}{path}"

    headers = {}
    api_key = _settings.api_key.strip() if _settings.api_key else ""
    admin_key = _settings.admin_key.strip() if _settings.admin_key else ""
    key = admin_key or api_key
    if key:
        headers["X-API-Key"] = key

    logger.info(f"[worker_proxy] {method} {target} (device={device_id}, worker={worker_id})")

    async with httpx.AsyncClient(timeout=timeout) as client:
        if method.upper() == "POST":
            resp = await client.post(target, json=json_body, headers=headers)
        elif method.upper() == "PUT":
            resp = await client.put(target, json=json_body, headers=headers)
        elif method.upper() == "DELETE":
            resp = await client.delete(target, headers=headers)
        else:
            resp = await client.get(target, headers=headers)

    return {"status_code": resp.status_code, "body": resp.json(), "worker_id": worker_id}


# ── Remote activity helpers ──────────────────────────────────

def register_remote_check(service: str, device_id: str, worker_id: str) -> None:
    """Mark a device as being checked on a remote worker."""
    from datetime import datetime, timezone
    _remote_activity[(service, device_id)] = {
        "worker_id": worker_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(f"[worker_registry] Remote activity registered: {service}/{device_id} on worker '{worker_id}'")


def clear_remote_check(service: str, device_id: str) -> None:
    """Remove remote activity entry (called when worker reports results)."""
    key = (service, device_id)
    if key in _remote_activity:
        _remote_activity.pop(key, None)
        logger.debug(f"[worker_registry] Remote activity cleared: {service}/{device_id}")


def get_remote_activity(service: str | None = None) -> list[dict]:
    """Return active remote checks, optionally filtered by service.

    Entries older than _REMOTE_ACTIVITY_TIMEOUT are auto-pruned.
    """
    now = time.time()
    from datetime import datetime, timezone
    expired = []
    results = []

    for (svc, dev_id), info in _remote_activity.items():
        # Auto-expire stale entries
        try:
            started = datetime.fromisoformat(info["started_at"])
            age = now - started.timestamp()
            if age > _REMOTE_ACTIVITY_TIMEOUT:
                expired.append((svc, dev_id))
                continue
        except Exception:
            expired.append((svc, dev_id))
            continue

        if service and svc != service:
            continue

        results.append({
            "device_id": dev_id,
            "service": svc,
            "worker_id": info["worker_id"],
            "started_at": info["started_at"],
            "status": "checking",
            "_remote": True,
        })

    # Prune expired
    for k in expired:
        _remote_activity.pop(k, None)

    return results
