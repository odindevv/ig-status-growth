"""
Raw kernel input injection via `sendevent`.

Why: `adb shell input tap` goes through Android's Java InputManager which
has a detectable automation footprint.  `sendevent` writes directly to the
Linux kernel input subsystem (/dev/input/eventX) — the exact same path real
hardware touch events take.  Apps like Instagram cannot distinguish these
from genuine human touches.

Usage is transparent — adb.py calls the helpers here when settings.use_sendevent
is True, and falls back to `input tap/swipe` when it's False or when detection
of the touch device fails.

Protocol reference (Linux input_event):
  EV_ABS  (0x03)  ABS_MT_TRACKING_ID (0x39)
  EV_ABS  (0x03)  ABS_MT_POSITION_X  (0x35)
  EV_ABS  (0x03)  ABS_MT_POSITION_Y  (0x36)
  EV_ABS  (0x03)  ABS_MT_PRESSURE    (0x3a)
  EV_KEY  (0x01)  BTN_TOUCH          (0x14a)  1=down, 0=up
  EV_SYN  (0x00)  SYN_REPORT         (0x00)
"""

import asyncio
import re
import random
from functools import partial
from logger import logger

# ── Constants for Linux input event types/codes ──────────────
EV_SYN = 0
EV_KEY = 1
EV_ABS = 3

SYN_REPORT = 0

BTN_TOUCH = 330  # 0x14a

ABS_MT_SLOT = 0x2f
ABS_MT_TRACKING_ID = 0x39
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_PRESSURE = 0x3a
ABS_MT_TOUCH_MAJOR = 0x30

# ── Per-device touchscreen info cache ────────────────────────
# { device_id: {"path": "/dev/input/event2", "max_x": 1080, "max_y": 2340} }
_touch_cache: dict[str, dict] = {}


async def detect_touchscreen(device_id: str, adb_run) -> dict | None:
    """
    Auto-detect the touchscreen input device and its coordinate ranges.

    Args:
        device_id: ADB device serial
        adb_run: the async _run(cmd, timeout) function from adb.py

    Returns:
        {"path": "/dev/input/eventX", "max_x": int, "max_y": int} or None
    """
    cached = _touch_cache.get(device_id)
    if cached:
        return cached

    from shared.adb import _base_cmd
    base = _base_cmd(device_id)

    # getevent -pl lists all input devices with their capabilities
    code, out, err = await adb_run(
        base + ["-s", device_id, "shell", "getevent", "-pl"],
        30,
    )
    if code != 0:
        logger.warning(f"[sendevent] getevent -pl failed on {device_id}: {err}")
        return None

    # Parse output to find the touchscreen device
    # Format:
    #   add device N: /dev/input/eventX
    #     name: "..."
    #     events:
    #       ABS (0003): ABS_MT_POSITION_X  value ... min ... max 1080
    device_path = None
    max_x = 0
    max_y = 0
    current_device = None

    for line in out.splitlines():
        dev_match = re.match(r"add device \d+: (.+)", line.strip())
        if dev_match:
            # Save previous device if it was a touchscreen
            if current_device and max_x > 0 and max_y > 0:
                break  # Already found one
            current_device = dev_match.group(1).strip()
            max_x = 0
            max_y = 0
            continue

        if current_device:
            # Look for ABS_MT_POSITION_X max value
            mx = re.search(r"ABS_MT_POSITION_X\s+.*max\s+(\d+)", line)
            if mx:
                max_x = int(mx.group(1))
                device_path = current_device

            my = re.search(r"ABS_MT_POSITION_Y\s+.*max\s+(\d+)", line)
            if my:
                max_y = int(my.group(1))

    # Check last device too
    if not device_path or max_x == 0 or max_y == 0:
        logger.warning(f"[sendevent] Could not detect touchscreen on {device_id}")
        return None

    info = {"path": device_path, "max_x": max_x, "max_y": max_y}
    _touch_cache[device_id] = info
    logger.info(f"[sendevent] Touchscreen detected on {device_id}: {device_path} (max {max_x}x{max_y})")
    return info


def _build_touch_down(dev: str, x: int, y: int, tracking_id: int = 0) -> str:
    """Build sendevent commands for a finger-down event."""
    pressure = random.randint(40, 80)
    touch_major = random.randint(4, 8)
    return (
        f"sendevent {dev} {EV_ABS} {ABS_MT_TRACKING_ID} {tracking_id};"
        f"sendevent {dev} {EV_ABS} {ABS_MT_POSITION_X} {x};"
        f"sendevent {dev} {EV_ABS} {ABS_MT_POSITION_Y} {y};"
        f"sendevent {dev} {EV_ABS} {ABS_MT_PRESSURE} {pressure};"
        f"sendevent {dev} {EV_ABS} {ABS_MT_TOUCH_MAJOR} {touch_major};"
        f"sendevent {dev} {EV_KEY} {BTN_TOUCH} 1;"
        f"sendevent {dev} {EV_SYN} {SYN_REPORT} 0"
    )


def _build_touch_move(dev: str, x: int, y: int) -> str:
    """Build sendevent commands for a finger-move event."""
    pressure = random.randint(40, 80)
    return (
        f"sendevent {dev} {EV_ABS} {ABS_MT_POSITION_X} {x};"
        f"sendevent {dev} {EV_ABS} {ABS_MT_POSITION_Y} {y};"
        f"sendevent {dev} {EV_ABS} {ABS_MT_PRESSURE} {pressure};"
        f"sendevent {dev} {EV_SYN} {SYN_REPORT} 0"
    )


def _build_touch_up(dev: str) -> str:
    """Build sendevent commands for a finger-up event."""
    return (
        f"sendevent {dev} {EV_ABS} {ABS_MT_TRACKING_ID} -1;"
        f"sendevent {dev} {EV_KEY} {BTN_TOUCH} 0;"
        f"sendevent {dev} {EV_SYN} {SYN_REPORT} 0"
    )


def scale_coord(screen_val: int, screen_max: int, touch_max: int) -> int:
    """Scale a screen pixel coordinate to the touch device's coordinate range."""
    if screen_max == touch_max or screen_max == 0:
        return screen_val
    return int(screen_val * touch_max / screen_max)


async def sendevent_tap(
    device_id: str,
    x: int,
    y: int,
    screen_w: int,
    screen_h: int,
    touch_info: dict,
    adb_run,
) -> bool:
    """
    Inject a tap via raw kernel sendevent.

    Simulates a realistic tap: finger-down, brief hold (~50-100ms), finger-up.
    Coordinates are scaled from screen pixels to the touch device's range.
    """
    dev = touch_info["path"]
    tx = scale_coord(x, screen_w, touch_info["max_x"])
    ty = scale_coord(y, screen_h, touch_info["max_y"])
    tracking_id = random.randint(1000, 60000)

    # Build the full command: down → small delay → up
    hold_ms = random.randint(50, 120)
    cmd_str = (
        f"{_build_touch_down(dev, tx, ty, tracking_id)};"
        f"sleep 0.{hold_ms:03d};"
        f"{_build_touch_up(dev)}"
    )

    from shared.adb import _base_cmd
    base = _base_cmd(device_id)

    code, _, err = await adb_run(
        base + ["-s", device_id, "shell", cmd_str],
        10,
    )
    if code != 0:
        logger.warning(f"[sendevent] Tap failed at ({x},{y}) → touch ({tx},{ty}) on {device_id}: {err}")
        return False
    return True


async def sendevent_swipe(
    device_id: str,
    x1: int, y1: int,
    x2: int, y2: int,
    duration_ms: int,
    screen_w: int,
    screen_h: int,
    touch_info: dict,
    adb_run,
) -> bool:
    """
    Inject a swipe via raw kernel sendevent.

    Generates intermediate move events with realistic timing to simulate
    a human finger drag.
    """
    dev = touch_info["path"]
    tx1 = scale_coord(x1, screen_w, touch_info["max_x"])
    ty1 = scale_coord(y1, screen_h, touch_info["max_y"])
    tx2 = scale_coord(x2, screen_w, touch_info["max_x"])
    ty2 = scale_coord(y2, screen_h, touch_info["max_y"])
    tracking_id = random.randint(1000, 60000)

    # Number of intermediate steps — more steps = smoother swipe
    steps = max(8, duration_ms // 30)
    step_delay_ms = max(10, duration_ms // steps)

    # Build command: down → series of moves with delays → up
    parts = [_build_touch_down(dev, tx1, ty1, tracking_id)]

    for i in range(1, steps + 1):
        t = i / steps
        # Add slight curve/jitter for realism
        jitter_x = random.randint(-2, 2)
        jitter_y = random.randint(-2, 2)
        cx = int(tx1 + (tx2 - tx1) * t) + jitter_x
        cy = int(ty1 + (ty2 - ty1) * t) + jitter_y
        parts.append(f"sleep 0.{step_delay_ms:03d}")
        parts.append(_build_touch_move(dev, cx, cy))

    parts.append(f"sleep 0.{random.randint(20, 50):03d}")
    parts.append(_build_touch_up(dev))

    cmd_str = ";".join(parts)

    from shared.adb import _base_cmd
    base = _base_cmd(device_id)

    code, _, err = await adb_run(
        base + ["-s", device_id, "shell", cmd_str],
        max(15, duration_ms // 1000 + 10),
    )
    if code != 0:
        logger.warning(f"[sendevent] Swipe failed ({x1},{y1})→({x2},{y2}) on {device_id}: {err}")
        return False
    return True


def clear_cache(device_id: str | None = None) -> None:
    """Clear cached touchscreen info. Call if device is reconnected."""
    if device_id:
        _touch_cache.pop(device_id, None)
    else:
        _touch_cache.clear()
