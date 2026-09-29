"""
ADB utility helpers.
All raw ADB calls live here — no ADB commands anywhere else.

Copied from ig_monitor and extended with:
  - tap(x, y) for UI navigation
  - swipe() for scrolling
  - get_screen_size() for coordinate scaling
  - Navigation to deep IG screens (Settings → Account Status)
"""

import asyncio
import subprocess
import sys
import shutil
import tempfile
import os
from datetime import datetime
from functools import partial
from pathlib import Path
from config import shared_settings as settings
from logger import logger


ADB = settings.adb_path

IG_PACKAGE = "com.instagram.android"
IG_MAIN_ACTIVITY = f"{IG_PACKAGE}/.activity.MainTabActivity"

PROXY_PACKAGE = "com.scheler.superproxy"

# ── Remote ADB host mapping ──────────────────────────────────
# Maps device_id → remote ADB host IP.  Devices not in this map
# are assumed local.  Populated by list_devices() when
# ADB_REMOTE_HOSTS is configured.
_device_host_map: dict[str, str] = {}

def _get_remote_hosts() -> list[str]:
    """Parse ADB_REMOTE_HOSTS from config (comma-separated IPs)."""
    raw = settings.adb_remote_hosts.strip()
    if not raw:
        return []
    return [h.strip() for h in raw.split(",") if h.strip()]


def _save_dump(device_id: str, ext: str, content: str | None = None, source_path: str | None = None) -> str | None:
    """Save a debug artifact to the dumps/ folder. Auto-rotates old files."""
    try:
        dumps_dir = Path(settings.debug_dumps_dir)
        if not dumps_dir.is_absolute():
            dumps_dir = Path(__file__).parent.parent / dumps_dir
        dumps_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_id = device_id.replace(":", "_").replace(".", "_")
        filename = f"{safe_id}_{ts}.{ext}"
        filepath = dumps_dir / filename
        if source_path:
            shutil.copy2(source_path, filepath)
        elif content:
            filepath.write_text(content, encoding="utf-8")
        else:
            return None
        logger.info(f"[dumps] Saved {ext.upper()} → {filepath}")

        # Rotate: prune oldest files if over max
        _rotate_dumps(dumps_dir)

        return str(filepath)
    except Exception as e:
        logger.warning(f"[dumps] Failed to save {ext}: {e}")
        return None


def save_failed_dump(device_id: str, xml: str, context: str = "") -> str | None:
    """Save a UI dump that was NOT recognized as Instagram to dumps_failed/.

    This helps diagnose what was on screen when _is_instagram_xml() returned
    False (e.g. app chooser, Chrome, launcher, etc.).
    """
    try:
        dumps_dir = Path(settings.debug_dumps_dir)
        if not dumps_dir.is_absolute():
            dumps_dir = Path(__file__).parent.parent / dumps_dir
        failed_dir = dumps_dir / "dumps_failed"
        failed_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_id = device_id.replace(":", "_").replace(".", "_")
        ctx = f"_{context}" if context else ""
        filename = f"{safe_id}_{ts}{ctx}.xml"
        filepath = failed_dir / filename
        filepath.write_text(xml, encoding="utf-8")
        logger.info(f"[dumps_failed] Saved → {filepath}")

        # Rotate: keep max 100 failed dumps
        all_files = sorted(
            [f for f in failed_dir.iterdir() if f.is_file()],
            key=lambda f: f.stat().st_mtime,
        )
        excess = len(all_files) - 100
        if excess > 0:
            for f in all_files[:excess]:
                f.unlink()

        return str(filepath)
    except Exception as e:
        logger.warning(f"[dumps_failed] Failed to save: {e}")
        return None


def is_loading_spinner(xml: str) -> bool:
    """Check if IG is showing a loading spinner AND profile content has NOT loaded.

    The swipe_refresh_animated_progressbar_container element is ALWAYS present
    in the profile XML layout — even when the profile is fully loaded. It's only
    a real "loading" state when that element exists but no actual profile content
    (followers, grid, etc.) has rendered yet.
    """
    if "swipe_refresh_animated_progressbar_container" not in xml:
        return False
    # Profile content indicators — if ANY of these exist, the profile loaded fine
    profile_loaded_indicators = (
        "profile_header_familiar_followers_value",
        "profile_header_familiar_followers_label",
        "profile_header_followers_stacked_familiar",
        "media_set_row_content_identifier",
        "row_profile_header",
    )
    for indicator in profile_loaded_indicators:
        if indicator in xml:
            return False  # Profile content present — not truly loading
    return True  # Spinner present but no profile content — truly loading


async def test_device_connectivity(device_id: str) -> bool:
    """Test if a device has working internet by pinging graph.instagram.com.

    Returns True if the device can reach Instagram's servers, False otherwise.
    Uses ADB shell to run the ping from the phone itself (through its proxy).
    Routes through the correct ADB host for remote devices.
    """
    base = _base_cmd(device_id)
    cmd = base + ["-s", device_id, "shell",
                  "ping", "-c", "1", "-W", "3", "graph.instagram.com"]
    code, out, err = await _run(cmd, timeout=10)
    connected = code == 0 and ("1 received" in out or "1 packets received" in out)
    logger.info(f"[connectivity] Ping graph.instagram.com on {device_id}: {'OK' if connected else 'FAILED'}")
    return connected


def _rotate_dumps(dumps_dir: Path) -> None:
    """Remove oldest dump files if count exceeds debug_dumps_max_files."""
    try:
        max_files = settings.debug_dumps_max_files
        if max_files <= 0:
            return
        all_files = sorted(dumps_dir.iterdir(), key=lambda f: f.stat().st_mtime)
        # Only count actual files (not directories)
        all_files = [f for f in all_files if f.is_file()]
        excess = len(all_files) - max_files
        if excess > 0:
            for f in all_files[:excess]:
                f.unlink()
            logger.debug(f"[dumps] Rotated {excess} old dump files (max={max_files})")
    except Exception as e:
        logger.warning(f"[dumps] Rotation failed: {e}")


def _base_cmd(device_id: str | None = None) -> list[str]:
    """Build the ADB base command with optional host/port.

    If *device_id* is given and it lives on a remote ADB host (populated
    by list_devices), the command is routed to that host automatically.
    When ADB_REMOTE_HOSTS is not configured, behaviour is identical to before.
    """
    cmd = [ADB]
    # Check if this device lives on a remote ADB server
    remote_host = _device_host_map.get(device_id) if device_id else None
    if remote_host:
        cmd.extend(["-H", remote_host, "-P", "5037"])
    else:
        # Fallback to global config (existing behaviour)
        if settings.adb_host:
            cmd.extend(["-H", settings.adb_host])
        if settings.adb_port:
            cmd.extend(["-P", str(settings.adb_port)])
    return cmd


def _run_sync(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """Synchronous subprocess call."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32" else 0
            ),
        )
        stdout = result.stdout.decode("utf-8", errors="ignore")
        stderr = result.stderr.decode("utf-8", errors="ignore")
        return result.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        logger.warning(f"[ADB] Timeout ({timeout}s): {' '.join(cmd)}")
        return -1, "", "timeout"
    except FileNotFoundError:
        logger.error(f"[ADB] Binary not found: {cmd[0]} — check ADB_PATH in .env")
        return -1, "", f"binary_not_found: {cmd[0]}"
    except Exception as e:
        logger.error(f"[ADB] Exec error ({type(e).__name__}): {e}")
        return -1, "", str(e)


async def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """Run an ADB command asynchronously via thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, partial(_run_sync, cmd, timeout)
    )


async def _run_with_retry(
    cmd: list[str],
    timeout: int = 15,
    retries: int | None = None,
) -> tuple[int, str, str]:
    """Run ADB command with retry logic for flaky connections."""
    max_retries = retries if retries is not None else settings.adb_retry_count
    for attempt in range(max_retries + 1):
        code, out, err = await _run(cmd, timeout=timeout)
        if code == 0:
            return code, out, err
        retryable = any(s in err for s in (
            "device not found", "device offline", "timeout",
            "closed", "protocol fault", "connection refused", "cannot connect",
        ))
        if not retryable or attempt >= max_retries:
            return code, out, err
        logger.warning(f"[ADB] Retry {attempt + 1}/{max_retries} — err: {err.strip()}")
        await asyncio.sleep(settings.adb_retry_delay)
    return code, out, err


# ── Device discovery ───────────────────────────────────────

def _parse_device_lines(output: str) -> list[dict]:
    """Parse 'adb devices -l' output into a list of device dicts."""
    devices = []
    for line in output.strip().splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        device_id = parts[0]
        state = parts[1]
        info = {}
        for part in parts[2:]:
            if ":" in part:
                k, v = part.split(":", 1)
                info[k] = v
        devices.append({
            "device_id": device_id,
            "state": state,
            "model": info.get("model", "unknown"),
            "product": info.get("product", "unknown"),
            "transport_id": info.get("transport_id", ""),
        })
    return devices


async def list_devices() -> list[dict]:
    """Return list of connected ADB devices with their state.

    When ADB_REMOTE_HOSTS is configured, also queries each remote ADB
    server and merges the results.  The _device_host_map is updated so
    subsequent commands for remote devices are routed correctly.
    """
    # ── Local devices ──
    base = _base_cmd()
    code, out, err = await _run(base + ["devices", "-l"])
    if code != 0:
        logger.error(f"[ADB] Failed to list local devices: {err}")
        devices = []
    else:
        devices = _parse_device_lines(out)

    local_ids = {d["device_id"] for d in devices}

    # ── Remote devices (optional) ──
    remote_hosts = _get_remote_hosts()
    for host in remote_hosts:
        remote_cmd = [ADB, "-H", host, "-P", "5037", "devices", "-l"]
        r_code, r_out, r_err = await _run(remote_cmd, timeout=10)
        if r_code != 0:
            logger.warning(f"[ADB] Failed to list devices on remote host {host}: {r_err}")
            continue
        remote_devs = _parse_device_lines(r_out)
        for rd in remote_devs:
            did = rd["device_id"]
            if did in local_ids:
                # Same serial on local and remote — skip remote duplicate
                logger.debug(f"[ADB] Device {did} exists locally, skipping remote duplicate from {host}")
                continue
            rd["remote_host"] = host
            devices.append(rd)
            _device_host_map[did] = host
            local_ids.add(did)
        if remote_devs:
            remote_ids = [d["device_id"] for d in remote_devs]
            logger.info(f"[ADB] Found {len(remote_devs)} device(s) on remote host {host}: {remote_ids}")

    # Clean up stale entries: remove devices from map that no longer appear
    current_ids = {d["device_id"] for d in devices}
    stale = [did for did in _device_host_map if did not in current_ids]
    for did in stale:
        del _device_host_map[did]

    return devices


async def get_online_device_ids() -> list[str]:
    """Return only device_ids that are online and ready."""
    devices = await list_devices()
    return [d["device_id"] for d in devices if d["state"] == "device"]


async def is_device_online(device_id: str) -> bool:
    """Check if a specific device is connected and responsive."""
    base = _base_cmd(device_id)
    code, out, _ = await _run_with_retry(
        base + ["-s", device_id, "shell", "echo", "ok"],
        timeout=5, retries=1,
    )
    return code == 0 and "ok" in out


# ── Screen management ──────────────────────────────────────

async def wake_and_unlock(device_id: str) -> None:
    """Wake the screen and dismiss lock screen. Swipe coords scale to actual screen size."""
    base = _base_cmd(device_id)
    await _run(base + ["-s", device_id, "shell", "input", "keyevent", "224"])
    await asyncio.sleep(0.3)
    await _run(base + ["-s", device_id, "shell", "input", "keyevent", "82"])
    await asyncio.sleep(0.3)
    # Scale swipe to actual screen size instead of hardcoded 1080x1920
    w, h = await get_screen_size(device_id)
    cx = w // 2
    y_start = int(h * 0.83)   # ~83% from top (was 1800/2160)
    y_end = int(h * 0.37)     # ~37% from top (was 800/2160)
    await _run(base + [
        "-s", device_id, "shell", "input", "swipe",
        str(cx), str(y_start), str(cx), str(y_end), "300",
    ])
    await asyncio.sleep(0.3)


async def is_screen_on(device_id: str) -> bool:
    """Check if device screen is currently on."""
    base = _base_cmd(device_id)
    code, out, _ = await _run(
        base + ["-s", device_id, "shell", "dumpsys", "power"], timeout=5,
    )
    if code != 0:
        return False
    return "mHoldingDisplaySuspendBlocker=true" in out


_screen_size_cache: dict[str, tuple[int, int]] = {}


async def get_screen_size(device_id: str) -> tuple[int, int]:
    """Get device screen resolution (width, height). Cached per device. Defaults to 1080x1920."""
    cached = _screen_size_cache.get(device_id)
    if cached:
        return cached

    base = _base_cmd(device_id)
    code, out, _ = await _run(
        base + ["-s", device_id, "shell", "wm", "size"], timeout=5,
    )
    if code == 0 and "x" in out:
        try:
            # Output: "Physical size: 1080x1920" or "Override size: ..."
            last_line = [l for l in out.strip().splitlines() if "x" in l][-1]
            size_str = last_line.split(":")[-1].strip()
            w, h = size_str.split("x")
            result = (int(w), int(h))
            _screen_size_cache[device_id] = result
            logger.info(f"[ADB] Screen size for {device_id}: {result[0]}x{result[1]}")
            return result
        except Exception:
            pass
    default = (1080, 1920)
    _screen_size_cache[device_id] = default
    return default


# ── UI interaction ─────────────────────────────────────────

async def tap(device_id: str, x: int, y: int, jitter: int = 12) -> bool:
    """Tap a coordinate with slight random offset to avoid automation detection.

    Priority chain:
      1. Scrcpy UHID (if session active) — undetectable, no root needed
      2. adb shell input tap — fallback

    Args:
        jitter: max pixel offset in each direction (default ±12px).
                Set to 0 for exact taps (e.g. small buttons).
    """
    import random
    jx = x + random.randint(-jitter, jitter)
    jy = y + random.randint(-jitter, jitter)
    jx = max(5, jx)
    jy = max(5, jy)

    # ── Try scrcpy UHID (anti-detection, no root) ──
    if settings.use_scrcpy:
        try:
            from shared.scrcpy_uhid import has_active_session, uhid_tap
            if has_active_session(device_id):
                ok = await uhid_tap(device_id, jx, jy)
                if ok:
                    if jitter > 0 and (jx != x or jy != y):
                        logger.debug(f"[ADB] Tap ({x},{y}) → jittered to ({jx},{jy}) on {device_id} [scrcpy-uhid]")
                    return True
                logger.debug(f"[ADB] scrcpy UHID tap failed, falling back to input tap on {device_id}")
        except Exception as e:
            logger.debug(f"[ADB] scrcpy UHID error on {device_id}: {e} — falling back to input tap")

    # ── Fallback: adb shell input tap ──
    base = _base_cmd(device_id)
    code, _, err = await _run(
        base + ["-s", device_id, "shell", "input", "tap", str(jx), str(jy)],
    )
    if code != 0:
        logger.warning(f"[ADB] Tap failed at ({jx},{jy}) on {device_id}: {err}")
        return False
    if jitter > 0 and (jx != x or jy != y):
        logger.debug(f"[ADB] Tap ({x},{y}) → jittered to ({jx},{jy}) on {device_id}")
    return True


async def swipe(device_id: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> bool:
    """Swipe from (x1,y1) to (x2,y2) with given duration.

    Uses scrcpy UHID when session is active for anti-detection.
    """
    # ── Try scrcpy UHID ──
    if settings.use_scrcpy:
        try:
            from shared.scrcpy_uhid import has_active_session, uhid_swipe
            if has_active_session(device_id):
                ok = await uhid_swipe(device_id, x1, y1, x2, y2, duration_ms)
                if ok:
                    return True
                logger.debug(f"[ADB] scrcpy UHID swipe failed, falling back to input swipe on {device_id}")
        except Exception as e:
            logger.debug(f"[ADB] scrcpy UHID swipe error on {device_id}: {e} — falling back")

    # ── Fallback: adb shell input swipe ──
    base = _base_cmd(device_id)
    code, _, err = await _run(
        base + ["-s", device_id, "shell", "input", "swipe",
                str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
    )
    if code != 0:
        logger.warning(f"[ADB] Swipe failed on {device_id}: {err}")
        return False
    return True


async def scroll_down(device_id: str, amount: float = 0.4) -> bool:
    """
    Scroll down on the device screen with slight randomization.
    amount: fraction of screen height to scroll (0.0 to 1.0).
    """
    import random
    w, h = await get_screen_size(device_id)
    cx = w // 2 + random.randint(-20, 20)
    jitter_amt = amount + random.uniform(-0.03, 0.03)
    y_start = int(h * 0.7) + random.randint(-15, 15)
    y_end = int(h * (0.7 - jitter_amt)) + random.randint(-15, 15)
    duration = 400 + random.randint(-80, 120)
    return await swipe(device_id, cx, y_start, cx, y_end, duration_ms=duration)


async def scroll_up(device_id: str, amount: float = 0.4) -> bool:
    """
    Scroll up on the device screen with slight randomization.
    amount: fraction of screen height to scroll (0.0 to 1.0).
    """
    import random
    w, h = await get_screen_size(device_id)
    cx = w // 2 + random.randint(-20, 20)
    jitter_amt = amount + random.uniform(-0.03, 0.03)
    y_start = int(h * 0.3) + random.randint(-15, 15)
    y_end = int(h * (0.3 + jitter_amt)) + random.randint(-15, 15)
    return await swipe(device_id, cx, y_start, cx, y_end, duration_ms=400)


async def press_back(device_id: str) -> bool:
    """Press the Android back button."""
    base = _base_cmd(device_id)
    code, _, _ = await _run(
        base + ["-s", device_id, "shell", "input", "keyevent", "4"],
    )
    return code == 0


async def press_home(device_id: str) -> bool:
    """Press the Android home button — minimizes current app without killing it."""
    base = _base_cmd(device_id)
    code, _, _ = await _run(
        base + ["-s", device_id, "shell", "input", "keyevent", "3"],
    )
    return code == 0


# ── Instagram operations ───────────────────────────────────

async def open_instagram(device_id: str) -> bool:
    """Launch Instagram main activity on device."""
    base = _base_cmd(device_id)
    code, _, err = await _run_with_retry(
        base + ["-s", device_id, "shell", "am", "start", "-n", IG_MAIN_ACTIVITY],
    )
    if code != 0:
        logger.warning(f"[ADB] Failed to open IG on {device_id}: {err}")
        return False
    return True


def profile_tab_coords(w: int, h: int) -> tuple[int, int]:
    """Compute blind-tap coords for the IG profile tab (5th icon, rightmost).

    Reference layout is 1080x2160 with profile_tab centre at (972, 2028).
    For most devices we scale linearly. On Pixel devices with gesture
    navigation (h=2400 area), the IG navbar sits against the gesture bar
    at the very bottom of the usable area, and the scaled coord lands
    ~51px above the real button (XML dump confirms y=2304 on Pixel 1080x2400).
    The scaled-from-2160 math assumes screen real-estate below the navbar
    that doesn't exist on edge-to-edge layouts.

    Centralised so every call site (open_instagram_to_profile, the Tier-3
    fallback in _navigate_to_profile, the cleanup branch in check_growth)
    stays in sync. Any future device-specific tweak only needs to happen
    here.
    """
    tx = int(972 * w / 1080)
    if h >= 2360 and w == 1080:
        ty = h - 96  # → 2304 on Pixel 1080x2400 (matches real profile_tab)
    else:
        ty = int(2028 * h / 2160)
    return tx, ty


async def open_instagram_to_profile(device_id: str) -> bool:
    """Open Instagram and immediately navigate to the Profile tab.

    The home feed may have reels/videos playing that prevent UIAutomator
    from reaching idle state. The Profile tab is static (no video), so
    tapping it right after open avoids the idle-state problem.

    Profile tab coords come from `profile_tab_coords(w, h)` which handles
    the Pixel edge-to-edge compensation.

    If IG is already in foreground (warm start), the blind tap is skipped
    to avoid hitting content on screen (e.g. a reel thumbnail on profile).
    """
    was_foreground = await is_instagram_foreground(device_id)
    if not await open_instagram(device_id):
        return False
    if was_foreground:
        # IG already open — skip blind tap to avoid hitting content
        logger.info(f"[ADB] IG already foreground on {device_id} — skipping blind profile tap")
        return True
    # Cold start — safe to tap profile tab (home feed is showing)
    await asyncio.sleep(1.5)
    w, h = await get_screen_size(device_id)
    tap_x, tap_y = profile_tab_coords(w, h)
    await tap(device_id, tap_x, tap_y, jitter=8)
    logger.info(f"[ADB] Opened IG + tapped Profile tab at ({tap_x},{tap_y}) on {device_id} (screen {w}x{h})")
    await asyncio.sleep(1.0)
    return True


async def force_stop_instagram(device_id: str) -> None:
    """Force stop IG app to reset state."""
    base = _base_cmd(device_id)
    await _run_with_retry(
        base + ["-s", device_id, "shell", "am", "force-stop", IG_PACKAGE],
    )


async def is_instagram_foreground(device_id: str) -> bool:
    """Check if Instagram is the current foreground activity.
    Supports multiple Android versions — Samsung/Android 12+ use different
    dumpsys keys than older versions.
    """
    base = _base_cmd(device_id)
    code, out, _ = await _run(
        base + ["-s", device_id, "shell", "dumpsys", "activity", "activities"],
        timeout=10,
    )
    if code != 0:
        return False
    # Different Android versions use different keys for the focused/resumed activity
    fg_keywords = [
        "mResumedActivity",      # Android ≤11
        "mFocusedActivity",      # Android ≤11
        "topResumedActivity",    # Android 12+
        "ResumedActivity",       # Android 12+ (Samsung)
        "mFocusedApp",           # Android 12+ (Samsung)
    ]
    for line in out.splitlines():
        if IG_PACKAGE in line and any(kw in line for kw in fg_keywords):
            return True
    return False


async def bring_instagram_to_front(device_id: str) -> bool:
    """Bring Instagram to foreground without killing it."""
    base = _base_cmd(device_id)
    code, _, err = await _run_with_retry(
        base + ["-s", device_id, "shell",
                "am", "start", "-n", IG_MAIN_ACTIVITY,
                "-f", "0x20000000"],
    )
    if code != 0:
        logger.warning(f"[ADB] Failed to bring IG to front on {device_id}: {err}")
        return False
    return True


async def is_instagram_installed(device_id: str) -> bool:
    """Check if Instagram APK is installed on device."""
    base = _base_cmd(device_id)
    code, out, _ = await _run(
        base + ["-s", device_id, "shell", "pm", "list", "packages", IG_PACKAGE],
        timeout=10,
    )
    return code == 0 and IG_PACKAGE in out


# ── UI Dump ────────────────────────────────────────────────

async def dump_ui(device_id: str, max_retries: int = 2, safe_mode: bool = False,
                  idle_escape: str | None = None) -> str | None:
    """
    Dump UIAutomator XML from device with retry logic.
    UIAutomator dumps are flaky — retrying improves success rate significantly.
    Returns XML string or None on failure.

    Args:
        safe_mode: If True, do NOT press Home on "could not get idle state" errors.
                   Use when dumping screens that load slowly (video detail insights)
                   where pressing Home would break the navigation flow.
        idle_escape: Recovery strategy when "could not get idle state" occurs.
                     "background_cycle" → press Home to pause animations, then
                     bring IG back to front and retry. Kills video playback
                     that prevents UIAutomator from reaching idle state.
                     "force_stop_cycle" → like background_cycle on attempt 1,
                     but escalates to force-stop + reopen to Profile on attempt 2.
                     Guaranteed to kill persistent reels that survive
                     background_cycle.
                     None → default behavior (wait / press Home).
    """
    base = _base_cmd(device_id)
    remote_paths = [
        "/data/local/tmp/ig_status_dump.xml",
        "/sdcard/ig_status_dump.xml",
    ]

    for attempt in range(max_retries + 1):
        for remote_path in remote_paths:
            code, out, err = await _run_with_retry(
                base + ["-s", device_id, "shell", "uiautomator", "dump", remote_path],
                timeout=20,
            )
            if code != 0:
                logger.debug(f"[ADB] uiautomator dump to {remote_path} failed on {device_id}: {err}")
                continue

            combined = (out + err).lower()
            if "could not get idle state" in combined:
                if idle_escape in ("background_cycle", "force_stop_cycle") and attempt < 2:
                    if idle_escape == "force_stop_cycle" and attempt >= 1:
                        # Escalate: background_cycle didn't work — force-stop
                        # and reopen IG to DMs (static screen, no video)
                        logger.warning(
                            f"[ADB] 'could not get idle state' on {device_id} "
                            f"(attempt {attempt + 1}/{max_retries + 1}) — "
                            f"ESCALATING: force-stop + reopen to Profile"
                        )
                        await force_stop_instagram(device_id)
                        await asyncio.sleep(0.5)
                        await open_instagram_to_profile(device_id)
                        await asyncio.sleep(1.5)
                    else:
                        # First attempt: Home + reopen to Profile (static screen)
                        logger.warning(
                            f"[ADB] 'could not get idle state' on {device_id} "
                            f"(attempt {attempt + 1}/{max_retries + 1}) — "
                            f"reopening to Profile to kill animations"
                        )
                        await press_home(device_id)
                        await asyncio.sleep(1.0)
                        await open_instagram_to_profile(device_id)
                elif attempt < 1:
                    # First attempt: just wait and retry, don't press anything
                    logger.warning(f"[ADB] 'could not get idle state' on {device_id} (attempt {attempt + 1}/{max_retries + 1}) — waiting to retry")
                    await asyncio.sleep(3.0)
                elif safe_mode:
                    # Safe mode: never press Home, just wait longer
                    logger.warning(f"[ADB] 'could not get idle state' on {device_id} (attempt {attempt + 1}/{max_retries + 1}) — safe_mode, waiting longer")
                    await asyncio.sleep(3.0)
                else:
                    # After 2 failed attempts: press Home to escape the stuck UI
                    logger.warning(f"[ADB] 'could not get idle state' on {device_id} (attempt {attempt + 1}/{max_retries + 1}) — pressing Home to recover")
                    await _run(base + ["-s", device_id, "shell", "input", "keyevent", "3"])  # Home
                    await asyncio.sleep(3.0)
                break  # Break inner loop → retry outer loop

            if "error" in combined or "null root node" in combined:
                logger.warning(f"[ADB] uiautomator dump empty on {device_id} (attempt {attempt + 1}): {out.strip()}")
                break  # Break inner loop — retry with delay

            with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
                local_path = tmp.name
            try:
                code, _, err = await _run_with_retry(
                    base + ["-s", device_id, "pull", remote_path, local_path],
                )
                if code != 0:
                    continue
                with open(local_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if not content or len(content) < 50:
                    continue
                logger.info(f"[ADB] UI dump OK from {remote_path} on {device_id} ({len(content)} bytes)")
                if settings.debug_dumps:
                    _save_dump(device_id, "xml", content)
                return content
            finally:
                if os.path.exists(local_path):
                    os.unlink(local_path)
                await _run(base + ["-s", device_id, "shell", "rm", "-f", remote_path])

        # Retry after a short delay — UIAutomator often works on second attempt
        if attempt < max_retries:
            logger.info(f"[ADB] UI dump retry {attempt + 1}/{max_retries} on {device_id}")
            await asyncio.sleep(1.0)

    logger.warning(f"[ADB] All dump attempts failed on {device_id} after {max_retries + 1} tries")
    return None


async def take_screenshot(device_id: str) -> str | None:
    """Capture screenshot from device. Returns local file path or None."""
    base = _base_cmd(device_id)
    remote_path = "/sdcard/ig_status_screen.png"
    code, _, err = await _run_with_retry(
        base + ["-s", device_id, "shell", "screencap", "-p", remote_path],
        timeout=15,
    )
    if code != 0:
        logger.warning(f"[ADB] screencap failed on {device_id}: {err}")
        return None

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        local_path = tmp.name

    code, _, err = await _run_with_retry(
        base + ["-s", device_id, "pull", remote_path, local_path],
    )
    if code != 0:
        if os.path.exists(local_path):
            os.unlink(local_path)
        return None

    await _run(base + ["-s", device_id, "shell", "rm", "-f", remote_path])

    file_size = os.path.getsize(local_path)
    if file_size < 1000:
        os.unlink(local_path)
        return None

    if settings.debug_dumps:
        _save_dump(device_id, "png", None, source_path=local_path)

    return local_path


# ── Device info ────────────────────────────────────────────

async def get_device_model(device_id: str) -> str:
    base = _base_cmd(device_id)
    code, out, _ = await _run(
        base + ["-s", device_id, "shell", "getprop", "ro.product.model"], timeout=5,
    )
    return out.strip() if code == 0 else "unknown"


async def get_android_version(device_id: str) -> str:
    base = _base_cmd(device_id)
    code, out, _ = await _run(
        base + ["-s", device_id, "shell", "getprop", "ro.build.version.release"], timeout=5,
    )
    return out.strip() if code == 0 else "unknown"


# ── Proxy app (Super Proxy) ──────────────────────────────

async def force_portrait(device_id: str) -> bool:
    """Force device into portrait orientation.
    Disables auto-rotate and sets user rotation to 0 (portrait).
    Uses 'content insert' which works reliably on Samsung/Panda setups.
    """
    base = _base_cmd(device_id)
    # Disable auto-rotate
    c1, _, _ = await _run(
        base + ["-s", device_id, "shell", "content", "insert",
                "--uri", "content://settings/system",
                "--bind", "name:s:accelerometer_rotation",
                "--bind", "value:i:0"],
        timeout=5,
    )
    # Force portrait (0=portrait, 1=landscape)
    c2, _, _ = await _run(
        base + ["-s", device_id, "shell", "content", "insert",
                "--uri", "content://settings/system",
                "--bind", "name:s:user_rotation",
                "--bind", "value:i:0"],
        timeout=5,
    )
    ok = c1 == 0 and c2 == 0
    if ok:
        logger.debug(f"[ADB] Forced portrait on {device_id}")
    else:
        logger.warning(f"[ADB] Failed to force portrait on {device_id}")
    return ok


async def open_proxy_app(device_id: str) -> bool:
    """Launch Super Proxy app via monkey (no specific activity needed)."""
    base = _base_cmd(device_id)
    code, _, err = await _run_with_retry(
        base + ["-s", device_id, "shell", "monkey", "-p", PROXY_PACKAGE,
                "-c", "android.intent.category.LAUNCHER", "1"],
    )
    if code != 0:
        logger.warning(f"[ADB] Failed to open proxy app on {device_id}: {err}")
        return False
    return True


async def is_proxy_foreground(device_id: str) -> bool:
    """Check if Super Proxy is the current foreground app."""
    base = _base_cmd(device_id)
    code, out, _ = await _run(
        base + ["-s", device_id, "shell", "dumpsys", "activity", "activities"],
        timeout=10,
    )
    if code != 0:
        return False
    fg_keywords = [
        "mResumedActivity", "mFocusedActivity",
        "topResumedActivity", "ResumedActivity", "mFocusedApp",
    ]
    for line in out.splitlines():
        if PROXY_PACKAGE in line and any(kw in line for kw in fg_keywords):
            return True
    return False


async def is_vpn_active(device_id: str) -> bool:
    """Check if a VPN tunnel is active by looking for tun0 interface.
    This is a system-level check that doesn't depend on UIAutomator.
    """
    base = _base_cmd(device_id)
    code, out, _ = await _run(
        base + ["-s", device_id, "shell", "ifconfig", "tun0"],
        timeout=5,
    )
    active = code == 0 and "tun0" in out
    logger.debug(f"[ADB] VPN active on {device_id}: {active}")
    return active
