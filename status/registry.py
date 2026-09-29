"""
Device registry — loaded from devices.json.
Same format as ig_monitor for consistency.

Supports:
  - load/save from JSON
  - dict-based O(1) lookup by device_id
  - enable/disable individual devices
  - sync with live ADB devices (auto-add new, mark disconnected)
  - asyncio.Lock for concurrent write protection
"""

import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path
from pydantic import BaseModel, field_validator
from logger import logger


def _devices_file() -> Path:
    """Resolve devices.json relative to the project root (where main.py lives)."""
    p = Path("devices_status.json")
    if not p.is_absolute():
        p = Path(__file__).parent.parent / p
    return p


def _normalize_usernames(raw: list) -> list[dict]:
    """Convert mixed list of str / dict to uniform [{username, enabled}].

    Backward compatible: old format ["user1", "user2"] auto-migrates to
    [{"username": "user1", "enabled": true}, ...].
    """
    result = []
    for item in raw:
        if isinstance(item, str):
            result.append({"username": item, "enabled": True})
        elif isinstance(item, dict) and "username" in item:
            result.append({
                "username": item["username"],
                "enabled": item.get("enabled", True),
            })
    return result


class Device(BaseModel):
    device_id: str
    username: str
    label: str = ""
    enabled: bool = True
    proxy_enabled: bool = True
    monitored: bool = False
    proxy_rotate_url: str = ""
    usernames: list[dict] = []
    notes: str = ""

    @field_validator("usernames", mode="before")
    @classmethod
    def _coerce_usernames(cls, v):
        if isinstance(v, list):
            return _normalize_usernames(v)
        return v

    def get_enabled_usernames(self) -> list[str]:
        """Return only enabled usernames as a plain list."""
        return [u["username"] for u in self.usernames if u.get("enabled", True)]

    def get_all_usernames(self) -> list[str]:
        """Return all usernames regardless of enabled state."""
        return [u["username"] for u in self.usernames]

    def set_username_enabled(self, username: str, enabled: bool) -> bool:
        """Toggle enabled state for a single username. Returns True if found."""
        for u in self.usernames:
            if u["username"] == username:
                u["enabled"] = enabled
                return True
        return False


_devices: list[Device] = []
_devices_map: dict[str, Device] = {}  # O(1) lookup
_registry_lock = asyncio.Lock()       # async lock for sync_with_adb
_sync_lock = threading.Lock()         # thread-safe lock for sync add/update/remove


def _rebuild_map() -> None:
    """Rebuild the device_id → Device lookup dict."""
    global _devices_map
    _devices_map = {d.device_id: d for d in _devices}


def load_devices() -> list[Device]:
    """Load devices from JSON file. Called at startup."""
    global _devices
    devices_file = _devices_file()
    if not devices_file.exists():
        logger.warning(f"[registry] {devices_file} not found — no devices loaded")
        _devices = []
        _rebuild_map()
        return _devices
    try:
        raw = json.loads(devices_file.read_text(encoding="utf-8"))
        _devices = [Device(**d) for d in raw]
        _rebuild_map()
        logger.info(f"[registry] Loaded {len(_devices)} devices from {devices_file}")
    except Exception as e:
        logger.error(f"[registry] Failed to load devices: {e}")
        _devices = []
        _rebuild_map()
    return _devices


def save_devices() -> None:
    """Persist current device list to JSON file (atomic write)."""
    devices_file = _devices_file()
    try:
        devices_file.parent.mkdir(parents=True, exist_ok=True)
        data = [d.model_dump() for d in _devices]
        content = json.dumps(data, indent=2, ensure_ascii=False)
        # Atomic write: write to temp file then rename — prevents corruption on crash
        fd, tmp_path = tempfile.mkstemp(
            dir=str(devices_file.parent), suffix=".tmp", prefix=".devices_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(devices_file))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.info(f"[registry] Saved {len(_devices)} devices to {devices_file}")
    except Exception as e:
        logger.error(f"[registry] Failed to save devices: {e}")


def get_devices() -> list[Device]:
    return _devices


def get_enabled_devices() -> list[Device]:
    return [d for d in _devices if d.enabled]


def get_monitored_devices() -> list[Device]:
    """Return devices that are both enabled AND monitored."""
    return [d for d in _devices if d.enabled and d.monitored]


def get_device_by_id(device_id: str) -> Device | None:
    return _devices_map.get(device_id)


def add_device(device_id: str, username: str = "", label: str = "", enabled: bool = True) -> Device:
    """Add a new device or return existing one. Thread-safe."""
    with _sync_lock:
        existing = get_device_by_id(device_id)
        if existing:
            return existing
        dev = Device(device_id=device_id, username=username, label=label, enabled=enabled)
        _devices.append(dev)
        _rebuild_map()
        return dev


def update_device(device_id: str, **kwargs) -> Device | None:
    """Update fields on an existing device. Thread-safe. Returns updated device or None."""
    with _sync_lock:
        dev = get_device_by_id(device_id)
        if not dev:
            return None
        for key, value in kwargs.items():
            if hasattr(dev, key) and value is not None:
                setattr(dev, key, value)
        _rebuild_map()
        return dev


def remove_device(device_id: str) -> bool:
    """Remove a device from the registry. Thread-safe."""
    global _devices
    with _sync_lock:
        dev = get_device_by_id(device_id)
        if not dev:
            return False
        _devices = [d for d in _devices if d.device_id != device_id]
        _rebuild_map()
        return True


async def sync_with_adb(adb_devices: list[dict], auto_add: bool = False) -> dict:
    """
    Sync registry with live ADB device list.
    Protected by _registry_lock for concurrent access safety.

    - Marks registered devices as online/offline based on ADB state
    - Optionally auto-adds new unregistered devices
    - NEVER touches disabled devices (skips them for checks)

    Returns sync report:
    {
        "online": [...], "offline": [...], "new_added": [...],
        "unregistered": [...], "disabled_skipped": [...]
    }
    """
    async with _registry_lock:
        adb_ids = {d["device_id"]: d for d in adb_devices if d["state"] == "device"}
        report = {
            "online": [],
            "offline": [],
            "new_added": [],
            "unregistered": [],
            "disabled_skipped": [],
        }

        # Check registered devices against ADB
        for dev in _devices:
            if not dev.enabled:
                report["disabled_skipped"].append(dev.device_id)
                continue
            if dev.device_id in adb_ids:
                report["online"].append(dev.device_id)
            else:
                report["offline"].append(dev.device_id)

        # Detect unregistered ADB devices
        for device_id in adb_ids:
            if device_id not in _devices_map:
                if auto_add:
                    model = adb_ids[device_id].get("model", "unknown")
                    new_dev = add_device(
                        device_id=device_id,
                        username="",
                        label=f"Auto-added ({model})",
                        enabled=True,
                    )
                    report["new_added"].append(device_id)
                    logger.info(f"[registry] Auto-added device: {device_id} ({model})")
                else:
                    report["unregistered"].append(device_id)

        if report["new_added"]:
            save_devices()

    return report
