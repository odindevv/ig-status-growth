"""
Global per-device lock shared across all services (Growth, Status, Checker).

Prevents two services from sending ADB commands to the same physical device
simultaneously — which would cause taps, navigation, and dumps to collide.

Usage in orchestrators:
    from shared.device_lock import acquire_device_lock

    async with acquire_device_lock(device_id, "status", timeout=300):
        # ... entire check flow for this device ...

Per-service locks (semaphores, internal device locks) remain as-is.
This module adds a layer ABOVE them to coordinate between services.
"""

import asyncio
from contextlib import asynccontextmanager

from logger import logger

# ── Global lock registry (one asyncio.Lock per device_id) ──
_locks: dict[str, asyncio.Lock] = {}

# Track which service currently holds each lock (for logging/debugging)
_holders: dict[str, str] = {}


def _get_lock(device_id: str) -> asyncio.Lock:
    """Get or create the global lock for a device."""
    if device_id not in _locks:
        _locks[device_id] = asyncio.Lock()
    return _locks[device_id]


def current_holder(device_id: str) -> str | None:
    """Return the service name holding the lock, or None if free."""
    return _holders.get(device_id)


@asynccontextmanager
async def acquire_device_lock(device_id: str, service: str, timeout: float = 300.0):
    """
    Async context manager to acquire the global device lock.

    Args:
        device_id: ADB device serial (e.g. "R5CR712QAXF").
        service: Name of the calling service ("growth", "status", "checker").
        timeout: Max seconds to wait for the lock. 0 = no wait (try-acquire).
                 Default 300s (5 min) — enough for any single check to finish.

    Raises:
        TimeoutError: If the lock could not be acquired within the timeout.

    Example:
        async with acquire_device_lock("R5CR712QAXF", "status"):
            await check_device_accounts(...)
    """
    lock = _get_lock(device_id)
    holder = _holders.get(device_id)

    if lock.locked() and holder:
        logger.info(
            f"[device_lock] {service} waiting for {device_id} "
            f"(held by {holder}) — timeout {timeout}s"
        )

    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        holder = _holders.get(device_id, "unknown")
        logger.error(
            f"[device_lock] {service} timed out waiting for {device_id} "
            f"(held by {holder} for >{timeout}s)"
        )
        raise TimeoutError(
            f"Device {device_id} busy (held by {holder})"
        )

    _holders[device_id] = service
    logger.info(f"[device_lock] {service} acquired {device_id}")
    try:
        yield
    finally:
        _holders.pop(device_id, None)
        lock.release()
        logger.info(f"[device_lock] {service} released {device_id}")
