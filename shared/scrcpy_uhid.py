"""
Scrcpy UHID session manager — anti-detection touch injection.

Why: `adb shell input tap` goes through Android's Java InputManager which
has a detectable automation footprint.  Scrcpy-server with UHID registers
a virtual HID device on the Android kernel — the exact same path real
USB/Bluetooth peripherals take.  Apps like Instagram cannot distinguish
these from genuine hardware input.  Does NOT require root.

Architecture (Option 3 — Hybrid, direct server):
  Instead of running the scrcpy desktop client (which consumes the control
  socket for itself), we push scrcpy-server.jar to the device, start it
  via ADB, and connect to its control socket directly.  This makes US the
  sole client.

  1. Before a check starts, call `start_session(device_id)`.
  2. During the check, `uhid_tap()` / `uhid_swipe()` send touch events.
  3. After the check ends, call `stop_session(device_id)`.

Compatible with scrcpy v2.x and v3.x server (tested on v3.3.4).
Protocol: binary big-endian, INJECT_TOUCH_EVENT = type 2.

Fallback: if scrcpy-server is not found or the session fails,
adb.py falls back to `adb shell input tap/swipe` transparently.
"""

import asyncio
import struct
import socket
import subprocess
import sys
import time
import random
import os
from pathlib import Path
from functools import partial
from logger import logger

# ── Scrcpy control protocol constants ─────────────────────
SC_CONTROL_MSG_TYPE_INJECT_KEYCODE = 0
SC_CONTROL_MSG_TYPE_INJECT_TEXT = 1
SC_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT = 2

# KeyEvent action constants (Android)
AKEY_EVENT_ACTION_DOWN = 0
AKEY_EVENT_ACTION_UP = 1

# MotionEvent action constants (Android)
AMOTION_EVENT_ACTION_DOWN = 0
AMOTION_EVENT_ACTION_UP = 1
AMOTION_EVENT_ACTION_MOVE = 2

# Pointer ID for single-finger touch
POINTER_ID_FINGER = 0x0000000000000001

# Remote path on device where we push the server jar
REMOTE_SERVER_PATH = "/data/local/tmp/scrcpy-server.jar"

# ── Per-device session cache ──────────────────────────────


class ScrcpySession:
    """Manages a scrcpy-server process and its control socket for one device."""

    def __init__(self, device_id: str, server_process: subprocess.Popen,
                 control_socket: socket.socket, screen_w: int, screen_h: int,
                 forward_port: int):
        self.device_id = device_id
        self.server_process = server_process
        self.control_socket = control_socket
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.forward_port = forward_port
        self.started_at = time.monotonic()
        self._lock = asyncio.Lock()

    def is_alive(self) -> bool:
        """Check if the server process is still running."""
        return self.server_process.poll() is None

    def close(self) -> None:
        """Terminate the server process and close the socket."""
        try:
            self.control_socket.close()
        except Exception:
            pass
        try:
            self.server_process.terminate()
            self.server_process.wait(timeout=3)
        except Exception:
            try:
                self.server_process.kill()
            except Exception:
                pass


_sessions: dict[str, ScrcpySession] = {}

# Cache: True once we've verified scrcpy-server.jar exists on device
_server_pushed: dict[str, bool] = {}


def _find_scrcpy_server() -> str | None:
    """
    Find the scrcpy-server.jar file on the host machine.

    The server jar is bundled with scrcpy installations:
      - Windows: same directory as scrcpy.exe (e.g. C:\\scrcpy\\scrcpy-server)
      - Linux: /usr/share/scrcpy/scrcpy-server or alongside the binary
      - macOS: similar to Linux, or in Homebrew prefix
    """
    import shutil

    # Method 1: Check SCRCPY_SERVER_PATH env var
    env_path = os.environ.get("SCRCPY_SERVER_PATH", "")
    if env_path and Path(env_path).exists():
        return env_path

    # Method 2: Look next to the scrcpy binary
    scrcpy_bin = shutil.which("scrcpy")
    if scrcpy_bin:
        scrcpy_dir = Path(scrcpy_bin).parent
        # scrcpy bundles the server as "scrcpy-server" (no .jar extension) in the same dir
        for name in ["scrcpy-server", "scrcpy-server.jar"]:
            candidate = scrcpy_dir / name
            if candidate.exists():
                return str(candidate)

        # On Linux, also check /usr/share/scrcpy/
        for share_dir in ["/usr/share/scrcpy", "/usr/local/share/scrcpy"]:
            for name in ["scrcpy-server", "scrcpy-server.jar"]:
                candidate = Path(share_dir) / name
                if candidate.exists():
                    return str(candidate)

    # Method 3: Check from config
    from config import shared_settings as settings
    scrcpy_path = getattr(settings, "scrcpy_path", "")
    if scrcpy_path:
        scrcpy_dir = Path(scrcpy_path).parent
        for name in ["scrcpy-server", "scrcpy-server.jar"]:
            candidate = scrcpy_dir / name
            if candidate.exists():
                return str(candidate)

    return None


def _get_scrcpy_version() -> str:
    """Get scrcpy version string from the binary."""
    import shutil
    scrcpy_bin = shutil.which("scrcpy")
    if not scrcpy_bin:
        return "3.3.4"  # Default assumption
    try:
        result = subprocess.run(
            [scrcpy_bin, "--version"],
            capture_output=True, timeout=5,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32" else 0
            ),
        )
        out = result.stdout.decode("utf-8", errors="ignore")
        # Output: "scrcpy 3.3.4 <https://...>"
        for line in out.splitlines():
            if line.startswith("scrcpy "):
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]
    except Exception:
        pass
    return "3.3.4"


async def _push_server(device_id: str) -> bool:
    """Push scrcpy-server.jar to the device (skips if already done)."""
    if _server_pushed.get(device_id):
        return True

    server_path = _find_scrcpy_server()
    if not server_path:
        logger.warning("[scrcpy] scrcpy-server not found — install scrcpy or set SCRCPY_SERVER_PATH env var")
        return False
    # Log which server JAR we're using (helps debug version mismatches)
    try:
        jar_size = Path(server_path).stat().st_size
        logger.debug(f"[scrcpy] Using server jar: {server_path} ({jar_size} bytes)")
    except Exception:
        pass

    from shared.adb import _run, _base_cmd

    base = _base_cmd(device_id)

    # Check if already on device (avoid redundant pushes)
    code, out, _ = await _run(
        base + ["-s", device_id, "shell", "ls", "-l", REMOTE_SERVER_PATH],
        timeout=5,
    )
    if code == 0 and "No such file" not in out:
        _server_pushed[device_id] = True
        logger.debug(f"[scrcpy] Server jar already on {device_id}")
        return True

    # Push it
    code, _, err = await _run(
        base + ["-s", device_id, "push", server_path, REMOTE_SERVER_PATH],
        timeout=15,
    )
    if code != 0:
        logger.warning(f"[scrcpy] Failed to push server to {device_id}: {err}")
        return False

    _server_pushed[device_id] = True
    logger.info(f"[scrcpy] Server jar pushed to {device_id}")
    return True


async def start_session(device_id: str, screen_w: int = 0, screen_h: int = 0) -> bool:
    """
    Start a scrcpy UHID session for a device.

    Pushes scrcpy-server.jar, starts the server process on the device,
    connects to its control socket, and verifies the connection works.

    Args:
        device_id: ADB device serial
        screen_w: Screen width (0 = auto-detect)
        screen_h: Screen height (0 = auto-detect)

    Returns:
        True if session started successfully, False otherwise.
    """
    # If already have a live session, reuse it
    existing = _sessions.get(device_id)
    if existing and existing.is_alive():
        logger.debug(f"[scrcpy] Reusing existing session for {device_id}")
        return True

    # Clean up dead session
    if existing:
        existing.close()
        _sessions.pop(device_id, None)

    # Step 1: Push server jar to device
    if not await _push_server(device_id):
        return False

    from shared.adb import _run, _base_cmd, get_screen_size

    base = _base_cmd(device_id)

    # Auto-detect screen size if not provided
    if screen_w == 0 or screen_h == 0:
        screen_w, screen_h = await get_screen_size(device_id)

    # Step 2: Kill any existing scrcpy-server on the device and wait for it to die
    await _run(
        base + ["-s", device_id, "shell", "pkill", "-f", "scrcpy"],
        timeout=5,
    )
    # Verify the old process is actually gone (race condition fix)
    for _kill_wait in range(6):  # up to 3s
        await asyncio.sleep(0.5)
        code, out, _ = await _run(
            base + ["-s", device_id, "shell", "pgrep", "-f", "scrcpy"],
            timeout=3,
        )
        if code != 0 or not out.strip():
            break  # no scrcpy processes left
        if _kill_wait >= 3:
            # Force kill if still alive after 1.5s
            await _run(
                base + ["-s", device_id, "shell", "kill", "-9", out.strip().split()[0]],
                timeout=3,
            )
    else:
        logger.warning(f"[scrcpy] Old scrcpy process still alive on {device_id} after 3s — proceeding anyway")

    # Remove stale ADB forwards for scrcpy ports (27100-27999 range)
    code, fwd_out, _ = await _run(
        base + ["-s", device_id, "forward", "--list"],
        timeout=5,
    )
    if code == 0 and fwd_out:
        for line in fwd_out.splitlines():
            if "localabstract:scrcpy" in line:
                parts = line.split()
                # Format: "SERIAL tcp:PORT localabstract:scrcpy"
                for p in parts:
                    if p.startswith("tcp:"):
                        await _run(
                            base + ["-s", device_id, "forward", "--remove", p],
                            timeout=3,
                        )
                        break

    # Step 3: Start scrcpy-server on the device
    version = _get_scrcpy_version()
    logger.debug(f"[scrcpy] Starting server with version={version} on {device_id}")
    server_cmd = (
        f"CLASSPATH={REMOTE_SERVER_PATH} "
        f"app_process / com.genymobile.scrcpy.Server {version} "
        f"tunnel_forward=true "
        f"video=false "
        f"audio=false "
        f"control=true "
        f"send_device_meta=false "
        f"raw_stream=true "
        f"cleanup=true "
        f"power_off_on_close=false"
    )

    server_process = None
    local_port = None
    try:
        # Start server as a background ADB shell process
        server_process = subprocess.Popen(
            base + ["-s", device_id, "shell", server_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32" else 0
            ),
        )

        # Wait for server to start listening — 3s for slower devices (Pixel SELinux)
        await asyncio.sleep(3.0)

        if server_process.poll() is not None:
            _, stderr = server_process.communicate(timeout=2)
            err_msg = stderr.decode("utf-8", errors="ignore").strip() if stderr else "unknown"
            logger.warning(f"[scrcpy] Server exited during startup on {device_id}: {err_msg}")
            # Invalidate push cache — JAR might be stale/corrupt/version mismatch
            _server_pushed.pop(device_id, None)
            server_process = None  # already exited, no cleanup needed
            return False

        # Verify the abstract socket exists before attempting forward
        code, sock_out, _ = await _run(
            base + ["-s", device_id, "shell", "cat /proc/net/unix | grep scrcpy"],
            timeout=3,
        )
        if code != 0 or "scrcpy" not in (sock_out or ""):
            logger.warning(f"[scrcpy] Server running but socket not ready on {device_id} — waiting 2s more")
            await asyncio.sleep(2.0)
            # Check again
            if server_process.poll() is not None:
                _, stderr = server_process.communicate(timeout=2)
                err_msg = stderr.decode("utf-8", errors="ignore").strip() if stderr else "unknown"
                logger.warning(f"[scrcpy] Server died while waiting for socket on {device_id}: {err_msg}")
                _server_pushed.pop(device_id, None)
                server_process = None
                return False

        # Step 4: Set up ADB forward to the server's socket
        local_port = random.randint(27100, 27999)
        code, _, err = await _run(
            base + ["-s", device_id, "forward",
                    f"tcp:{local_port}", "localabstract:scrcpy"],
            timeout=5,
        )
        if code != 0:
            logger.warning(f"[scrcpy] ADB forward failed for {device_id}: {err}")
            server_process.terminate()
            return False

        # Step 5: Connect to the control socket
        control_sock = None
        for attempt in range(3):
            try:
                loop = asyncio.get_event_loop()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                await loop.run_in_executor(None, sock.connect, ("127.0.0.1", local_port))
                sock.settimeout(5)
                control_sock = sock
                break
            except Exception as e:
                logger.debug(f"[scrcpy] Socket connect attempt {attempt + 1}/3: {e}")
                try:
                    sock.close()
                except Exception:
                    pass
                if attempt < 2:
                    await asyncio.sleep(1.0)

        if not control_sock:
            logger.warning(f"[scrcpy] Could not connect to server socket for {device_id}")
            server_process.terminate()
            await _run(
                base + ["-s", device_id, "forward", "--remove", f"tcp:{local_port}"],
                timeout=5,
            )
            return False

        # Step 6: Drain any initial data (some versions send a small header)
        try:
            control_sock.setblocking(False)
            try:
                while True:
                    chunk = control_sock.recv(4096)
                    if not chunk:
                        break
            except (BlockingIOError, socket.error):
                pass
            control_sock.setblocking(True)
            control_sock.settimeout(5)
        except Exception:
            pass

        # Build session
        session = ScrcpySession(
            device_id=device_id,
            server_process=server_process,
            control_socket=control_sock,
            screen_w=screen_w,
            screen_h=screen_h,
            forward_port=local_port,
        )
        _sessions[device_id] = session
        server_process = None  # ownership transferred to session — don't kill in except

        # Step 7: Verify with a test touch
        test_ok = await _send_control_msg(session, _build_touch_event(
            AMOTION_EVENT_ACTION_DOWN, 0, 0, screen_w, screen_h, 0.0,
        ))
        if test_ok:
            await _send_control_msg(session, _build_touch_event(
                AMOTION_EVENT_ACTION_UP, 0, 0, screen_w, screen_h, 0.0,
            ))

        if not test_ok:
            logger.warning(f"[scrcpy] Test touch failed — server not accepting control messages on {device_id}")
            session.close()
            _sessions.pop(device_id, None)
            await _run(
                base + ["-s", device_id, "forward", "--remove", f"tcp:{local_port}"],
                timeout=5,
            )
            return False

        logger.info(f"[scrcpy] UHID session started for {device_id} ({screen_w}x{screen_h}) via direct server")
        return True

    except Exception as e:
        logger.warning(f"[scrcpy] Failed to start session for {device_id}: {e}")
        _server_pushed.pop(device_id, None)  # force re-push on next attempt
        # Kill orphan server process if it was created but not transferred to a session
        if server_process is not None:
            try:
                server_process.terminate()
                server_process.wait(timeout=3)
            except Exception:
                try:
                    server_process.kill()
                except Exception:
                    pass
            # Clean up ADB forward if port was allocated
            if local_port is not None:
                await _run(
                    base + ["-s", device_id, "forward", "--remove", f"tcp:{local_port}"],
                    timeout=5,
                )
        return False


def _build_touch_event(action: int, x: int, y: int, w: int, h: int,
                       pressure: float = 1.0) -> bytes:
    """
    Build a scrcpy INJECT_TOUCH_EVENT control message.

    Binary format (scrcpy v2.x / v3.x — unchanged):
      type:       u8   (2 = INJECT_TOUCH_EVENT)
      action:     u8   (0=DOWN, 1=UP, 2=MOVE)
      pointer_id: i64  (big-endian)
      x:          i32  (screen coordinate, big-endian)
      y:          i32  (screen coordinate, big-endian)
      width:      u16  (screen width, big-endian)
      height:     u16  (screen height, big-endian)
      pressure:   u16  (0xFFFF = max, big-endian)
      action_btn: u32  (0 for touch, big-endian)
      buttons:    u32  (0 for touch, big-endian)
    """
    pressure_u16 = int(pressure * 0xFFFF)

    return struct.pack(
        ">BBqiiHHHII",
        SC_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT,  # type (u8)
        action,                                   # action (u8)
        POINTER_ID_FINGER,                       # pointer_id (i64)
        x,                                        # x (i32)
        y,                                        # y (i32)
        w,                                        # width (u16)
        h,                                        # height (u16)
        pressure_u16,                             # pressure (u16)
        0,                                        # action_button (u32)
        0,                                        # buttons (u32)
    )


async def _send_control_msg(session: ScrcpySession, data: bytes) -> bool:
    """Send a control message through the scrcpy socket (thread-safe)."""
    async with session._lock:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, session.control_socket.sendall, data)
            return True
        except Exception as e:
            logger.warning(f"[scrcpy] Send failed for {session.device_id}: {e}")
            return False


async def uhid_tap(device_id: str, x: int, y: int) -> bool:
    """
    Inject a tap via scrcpy UHID.

    Simulates a realistic tap: finger-down, brief hold (~50-100ms), finger-up.
    """
    session = _sessions.get(device_id)
    if not session or not session.is_alive():
        return False

    w = session.screen_w
    h = session.screen_h
    pressure = random.uniform(0.4, 0.9)

    # Finger down
    down_msg = _build_touch_event(AMOTION_EVENT_ACTION_DOWN, x, y, w, h, pressure)
    if not await _send_control_msg(session, down_msg):
        return False

    # Brief hold (realistic timing)
    hold_ms = random.randint(50, 120)
    await asyncio.sleep(hold_ms / 1000.0)

    # Finger up
    up_msg = _build_touch_event(AMOTION_EVENT_ACTION_UP, x, y, w, h, 0.0)
    if not await _send_control_msg(session, up_msg):
        return False

    return True


async def uhid_swipe(
    device_id: str,
    x1: int, y1: int,
    x2: int, y2: int,
    duration_ms: int = 300,
) -> bool:
    """
    Inject a swipe via scrcpy UHID.

    Generates intermediate move events with realistic timing and jitter
    to simulate a human finger drag.
    """
    session = _sessions.get(device_id)
    if not session or not session.is_alive():
        return False

    w = session.screen_w
    h = session.screen_h

    # Number of intermediate steps
    steps = max(8, duration_ms // 30)
    step_delay = max(0.010, duration_ms / 1000.0 / steps)

    # Finger down at start position
    pressure = random.uniform(0.5, 0.9)
    down_msg = _build_touch_event(AMOTION_EVENT_ACTION_DOWN, x1, y1, w, h, pressure)
    if not await _send_control_msg(session, down_msg):
        return False

    # Move events with interpolation + jitter
    for i in range(1, steps + 1):
        t = i / steps
        jitter_x = random.randint(-2, 2)
        jitter_y = random.randint(-2, 2)
        cx = int(x1 + (x2 - x1) * t) + jitter_x
        cy = int(y1 + (y2 - y1) * t) + jitter_y
        cx = max(0, min(cx, w - 1))
        cy = max(0, min(cy, h - 1))

        pressure = random.uniform(0.4, 0.8)
        move_msg = _build_touch_event(AMOTION_EVENT_ACTION_MOVE, cx, cy, w, h, pressure)
        if not await _send_control_msg(session, move_msg):
            return False

        await asyncio.sleep(step_delay)

    # Brief pause before lift
    await asyncio.sleep(random.uniform(0.02, 0.05))

    # Finger up at end position
    up_msg = _build_touch_event(AMOTION_EVENT_ACTION_UP, x2, y2, w, h, 0.0)
    if not await _send_control_msg(session, up_msg):
        return False

    return True


def _build_inject_text(text: str) -> bytes:
    """
    Build a scrcpy INJECT_TEXT control message.

    Binary format:
      type:   u8   (1 = INJECT_TEXT)
      length: u32  (big-endian, byte length of UTF-8 text)
      text:   bytes (UTF-8 encoded)
    """
    text_bytes = text.encode("utf-8")
    return struct.pack(">BI", SC_CONTROL_MSG_TYPE_INJECT_TEXT, len(text_bytes)) + text_bytes


def _build_inject_keycode(action: int, keycode: int, repeat: int = 0, metastate: int = 0) -> bytes:
    """
    Build a scrcpy INJECT_KEYCODE control message.

    Binary format:
      type:      u8   (0 = INJECT_KEYCODE)
      action:    u8   (0=DOWN, 1=UP)
      keycode:   u32  (Android KEYCODE_*, big-endian)
      repeat:    u32  (big-endian)
      metastate: u32  (big-endian)
    """
    return struct.pack(
        ">BBIII",
        SC_CONTROL_MSG_TYPE_INJECT_KEYCODE,
        action,
        keycode,
        repeat,
        metastate,
    )


async def uhid_text(device_id: str, text: str) -> bool:
    """
    Inject text via scrcpy UHID — equivalent to typing on a virtual keyboard.
    Undetectable by apps (same path as real HID devices).

    For usernames with special chars like dots/underscores, this is much more
    reliable than adb shell input text which requires escaping.
    """
    session = _sessions.get(device_id)
    if not session or not session.is_alive():
        return False

    msg = _build_inject_text(text)
    return await _send_control_msg(session, msg)


async def uhid_keycode(device_id: str, keycode: int, metastate: int = 0) -> bool:
    """
    Inject a single key press (down + up) via scrcpy UHID.

    Common keycodes:
      4  = KEYCODE_BACK
      3  = KEYCODE_HOME
      67 = KEYCODE_DEL (backspace)
      66 = KEYCODE_ENTER
      123 = KEYCODE_MOVE_END
    """
    session = _sessions.get(device_id)
    if not session or not session.is_alive():
        return False

    # Key down
    down_msg = _build_inject_keycode(AKEY_EVENT_ACTION_DOWN, keycode, metastate=metastate)
    if not await _send_control_msg(session, down_msg):
        return False

    # Brief hold
    await asyncio.sleep(random.uniform(0.03, 0.08))

    # Key up
    up_msg = _build_inject_keycode(AKEY_EVENT_ACTION_UP, keycode, metastate=metastate)
    return await _send_control_msg(session, up_msg)


def stop_session(device_id: str) -> None:
    """Stop and clean up a scrcpy session for a device."""
    session = _sessions.pop(device_id, None)
    if not session:
        return

    session.close()

    # Clean up ADB forward
    try:
        from shared.adb import _base_cmd
        base = _base_cmd(device_id)
        subprocess.run(
            base + ["-s", device_id, "forward", "--remove", f"tcp:{session.forward_port}"],
            timeout=5, capture_output=True,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32" else 0
            ),
        )
    except Exception:
        pass

    # Kill server on device
    try:
        from shared.adb import _base_cmd as _bc
        base = _bc(device_id)
        subprocess.run(
            base + ["-s", device_id, "shell", "pkill", "-f", "scrcpy"],
            timeout=5, capture_output=True,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32" else 0
            ),
        )
    except Exception:
        pass

    logger.info(f"[scrcpy] Session stopped for {device_id}")


def stop_all_sessions() -> None:
    """Stop all active scrcpy sessions."""
    for device_id in list(_sessions.keys()):
        stop_session(device_id)


def get_session(device_id: str) -> ScrcpySession | None:
    """Get the active session for a device (if any)."""
    session = _sessions.get(device_id)
    if session and session.is_alive():
        return session
    if session:
        session.close()
        _sessions.pop(device_id, None)
    return None


def has_active_session(device_id: str) -> bool:
    """Check if a device has an active scrcpy UHID session."""
    return get_session(device_id) is not None


def clear_server_cache(device_id: str | None = None) -> None:
    """Clear cached server push state. Call if device is reconnected."""
    if device_id:
        _server_pushed.pop(device_id, None)
    else:
        _server_pushed.clear()
