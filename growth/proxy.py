"""
Proxy management — start/stop Super Proxy app before/after IG checks.

Flow:
  Before IG check:  open proxy → tap Start → if ad: Home → reopen → navigate to profile → verify
  After IG check:   open proxy → tap Stop → leave on proxy screen

The proxy app (com.scheler.superproxy) uses content-desc="Start"/"Stop" on its main button.
Ads may appear after tapping Start — pressing Home and reopening the app auto-activates
the proxy and dismisses the ad. After reopen, the app may land on the profile list screen
(identifiable by "Buy proxy" button) instead of the Start/Stop screen — tap "Default Profile"
to get back to the main view.
"""

import asyncio
from config import growth_settings as settings
from logger import logger
from shared.adb import (
    dump_ui,
    tap,
    press_back,
    press_home,
    open_proxy_app,
    is_proxy_foreground,
    force_portrait,
    get_screen_size,
    is_vpn_active,
)
from growth.ui_parser import _get_all_nodes, get_center_of_bounds


# Devices known to have the ad WebView that blocks UIAutomator.
# Set during start_proxy when VPN fallback is used, cleared when dump_ui works normally.
_ad_devices: set[str] = set()

# Per-device cache of the Start/Stop button coordinates.
# Populated when start_proxy finds the button via dump_ui.
# Re-used by stop_proxy so we don't need dump_ui (which fails due to ad WebView).
_button_coords: dict[str, tuple[int, int]] = {}

# Reference screen for blind tap scaling (Super Proxy layout on 1080x1920)
# Start/Stop button: bounds [32,1334][1048,1434] → center (540, 1384)
_REF_W, _REF_H = 1080, 1920
_REF_BTN_X, _REF_BTN_Y = 540, 1384


async def _get_blind_tap_coords(device_id: str) -> tuple[int, int]:
    """Get Start/Stop button coordinates.
    Prefers cached coords from a previous dump_ui (exact).
    Falls back to scaling with nav bar correction (wm size includes nav bar,
    but the app renders in the smaller visible area).
    """
    # Best: cached from a real dump_ui during start_proxy
    if device_id in _button_coords:
        logger.debug(f"[proxy] Using cached button coords {_button_coords[device_id]} for {device_id}")
        return _button_coords[device_id]

    # Fallback: scale using wm size but correct for nav bar.
    # wm size returns physical resolution (includes software nav bar area).
    # The proxy app renders in the visible area which is ~6% shorter.
    # Example: wm size = 1080x2220, visible = 1080x2088, nav bar = 132px
    w, h = await get_screen_size(device_id)
    visible_h = int(h * 0.94)  # ~6% nav bar correction
    x = int(_REF_BTN_X * w / _REF_W)
    y = int(_REF_BTN_Y * visible_h / _REF_H)
    logger.info(f"[proxy] Blind tap coords: ({x}, {y}) — wm={w}x{h}, visible_h≈{visible_h} on {device_id}")
    return x, y


# ── Helpers ───────────────────────────────────────────────

def _find_proxy_button(xml: str) -> dict | None:
    """Find the Start or Stop button in proxy app UI.
    Checks both content_desc and text attributes.
    Note: _get_all_nodes returns keys with underscores (content_desc, resource_id)
    and clickable as a boolean (True/False), not a string.
    Returns {"state": "start"|"stop", "coords": (x, y)} or None.
    """
    nodes = _get_all_nodes(xml)

    # Pass 1: strict — content_desc + clickable=True
    for node in nodes:
        desc = (node.get("content_desc", "") or "").strip().lower()
        if desc in ("start", "stop") and node.get("clickable") is True:
            bounds = node.get("bounds", "")
            if bounds:
                coords = get_center_of_bounds(bounds)
                if coords:
                    return {"state": desc, "coords": coords}

    # Pass 2: also check text attribute + clickable=True
    for node in nodes:
        text = (node.get("text", "") or "").strip().lower()
        if text in ("start", "stop") and node.get("clickable") is True:
            bounds = node.get("bounds", "")
            if bounds:
                coords = get_center_of_bounds(bounds)
                if coords:
                    return {"state": text, "coords": coords}

    # Pass 3: relaxed — content_desc or text without requiring clickable
    for node in nodes:
        desc = (node.get("content_desc", "") or "").strip().lower()
        text = (node.get("text", "") or "").strip().lower()
        matched = None
        if desc in ("start", "stop"):
            matched = desc
        elif text in ("start", "stop"):
            matched = text
        if matched:
            bounds = node.get("bounds", "")
            if bounds:
                coords = get_center_of_bounds(bounds)
                if coords:
                    logger.info(f"[proxy] Found button via relaxed match: {matched} (clickable={node.get('clickable')})")
                    return {"state": matched, "coords": coords}

    # Debug: log all node texts/descs to help diagnose
    all_labels = []
    for node in nodes:
        t = (node.get("text", "") or "").strip()
        d = (node.get("content_desc", "") or "").strip()
        if t or d:
            all_labels.append(f"text={t!r} desc={d!r} click={node.get('clickable')}")
    logger.debug(f"[proxy] Button not found. All nodes with text/desc ({len(all_labels)}): {all_labels[:30]}")

    return None


def _is_profile_list_screen(xml: str) -> bool:
    """Detect if we're on the proxy profile list screen (not the Start/Stop screen).
    This screen shows profiles and has a 'Buy proxy' button.
    """
    nodes = _get_all_nodes(xml)
    for node in nodes:
        desc = (node.get("content_desc", "") or "").lower()
        if "buy proxy" in desc:
            return True
    return False


def _find_default_profile(xml: str) -> tuple[int, int] | None:
    """Find the 'Default Profile' entry on the profile list screen to tap into it."""
    nodes = _get_all_nodes(xml)
    for node in nodes:
        desc = (node.get("content_desc", "") or "")
        if "Default Profile" in desc and node.get("clickable") is True:
            bounds = node.get("bounds", "")
            if bounds:
                return get_center_of_bounds(bounds)
    return None


async def _ensure_proxy_open(device_id: str) -> bool:
    """Make sure proxy app is open and in foreground.
    Gives extra time for slow proxy app launches.
    Always forces portrait after opening — ads/rotation can leave screen in landscape.
    """
    if await is_proxy_foreground(device_id):
        return True

    if not await open_proxy_app(device_id):
        return False

    # Wait longer for proxy app — it can be slow to render
    await asyncio.sleep(settings.page_load_delay + 1.5)

    # Force portrait every time the app is opened/reopened
    await force_portrait(device_id)

    if await is_proxy_foreground(device_id):
        return True

    # Retry once — sometimes monkey launch needs a second try
    logger.info(f"[proxy] Proxy not in foreground after first launch, retrying on {device_id}")
    await open_proxy_app(device_id)
    await asyncio.sleep(settings.page_load_delay + 1.0)
    await force_portrait(device_id)
    return await is_proxy_foreground(device_id)


async def _navigate_to_proxy_main(device_id: str) -> bool:
    """If we're on the profile list screen, tap Default Profile to get to the Start/Stop view.
    Returns True if we're on the main screen (Start/Stop button visible).
    """
    xml = await dump_ui(device_id)
    if not xml:
        return False

    # Already on the main screen?
    if _find_proxy_button(xml):
        return True

    # On profile list screen? Tap Default Profile
    if _is_profile_list_screen(xml):
        coords = _find_default_profile(xml)
        if coords:
            logger.info(f"[proxy] On profile list screen, tapping Default Profile at {coords}")
            await tap(device_id, coords[0], coords[1])
            await asyncio.sleep(settings.page_load_delay)

            # Verify we're now on the main screen
            xml = await dump_ui(device_id)
            if xml and _find_proxy_button(xml):
                return True

    logger.warning(f"[proxy] Could not navigate to proxy main screen on {device_id}")
    return False


async def _recover_from_ad(device_id: str) -> bool:
    """Recover from ad that appears after tapping Start.
    Strategy: press Home → reopen proxy → the proxy auto-activates and ad is gone.
    After reopen, may land on profile list → tap Default Profile.
    Returns True if we can see the Start/Stop button again.
    """
    logger.info(f"[proxy] Ad recovery: pressing Home and reopening proxy on {device_id}")
    await press_home(device_id)
    await asyncio.sleep(1.0)

    # _ensure_proxy_open handles force_portrait automatically
    if not await _ensure_proxy_open(device_id):
        logger.warning(f"[proxy] Ad recovery: failed to reopen proxy on {device_id}")
        return False

    await asyncio.sleep(1.0)

    # Navigate to main screen (may be on profile list after reopen)
    return await _navigate_to_proxy_main(device_id)


# ── Public API ────────────────────────────────────────────

async def start_proxy(device_id: str) -> dict:
    """
    Open proxy app and tap Start.
    Devices with known ad WebView (in _ad_devices) take the fast path:
      1 dump attempt → VPN check → blind tap if needed.
    Devices without ad: normal dump_ui flow with full retries.

    Returns {"success": bool, "state": "running"|"already_running"|"failed", "error": str|None}
    """
    result = {"success": False, "state": "error", "error": None}
    has_ad = device_id in _ad_devices

    # Open proxy app
    if not await _ensure_proxy_open(device_id):
        result["error"] = "Failed to open proxy app"
        return result

    # Navigate to main screen if needed (may be on profile list)
    # Skip for ad devices — dump_ui will fail with idle state anyway, wasting ~20s
    if not has_ad:
        await _navigate_to_proxy_main(device_id)

    # ── Find button ──
    # Ad devices: 1 attempt only (dump_ui will likely fail)
    # Normal devices: up to 3 attempts
    max_attempts = 1 if has_ad else 3
    button = None
    for attempt in range(max_attempts):
        xml = await dump_ui(device_id)
        if not xml:
            if not has_ad:
                await asyncio.sleep(1.5)
            continue
        button = _find_proxy_button(xml)
        if button:
            # Cache exact button coords for stop_proxy (avoids dump_ui issues later)
            _button_coords[device_id] = button["coords"]
            logger.debug(f"[proxy] Cached button coords {button['coords']} for {device_id}")
            # dump_ui worked — clear ad flag if it was set (ad may have gone away)
            if has_ad:
                _ad_devices.discard(device_id)
                has_ad = False
                logger.info(f"[proxy] dump_ui worked on {device_id} — cleared ad flag")
            break
        if not has_ad:
            logger.info(f"[proxy] Button not found on {device_id}, waiting (attempt {attempt + 1}/{max_attempts})")
            await asyncio.sleep(2.0)

    # ── No button found — fallback path ──
    if not button:
        # Check if VPN already active (proxy might already be running)
        if await is_vpn_active(device_id):
            _ad_devices.add(device_id)
            logger.info(f"[proxy] dump_ui failed but VPN active — proxy already running on {device_id} (flagged as ad device)")
            result["success"] = True
            result["state"] = "already_running"
            return result

        # VPN not active — blind tap Start
        bx, by = await _get_blind_tap_coords(device_id)
        logger.info(f"[proxy] Blind-tapping Start at ({bx}, {by}) on {device_id}")
        await tap(device_id, bx, by)
        await asyncio.sleep(3.0)

        if await is_vpn_active(device_id):
            _ad_devices.add(device_id)
            logger.info(f"[proxy] Proxy started via blind tap on {device_id} (VPN confirmed, flagged as ad device)")
            result["success"] = True
            result["state"] = "running"
            return result

        # Blind tap didn't work — maybe on profile list screen
        # Try full recovery: Home + reopen + navigate to main (covers profile list case)
        logger.info(f"[proxy] Blind tap failed on {device_id} — trying full recovery (profile list?)")
        await _recover_from_ad(device_id)
        await asyncio.sleep(2.0)
        if await is_vpn_active(device_id):
            _ad_devices.add(device_id)
            logger.info(f"[proxy] Proxy started on {device_id} after recovery (VPN confirmed)")
            result["success"] = True
            result["state"] = "running"
            return result

        result["error"] = "Proxy failed to start — VPN not active after all attempts"
        result["state"] = "failed"
        return result

    # ── Button found — normal flow ──

    # Already running?
    if button["state"] == "stop":
        logger.info(f"[proxy] Proxy already running on {device_id}")
        result["success"] = True
        result["state"] = "already_running"
        return result

    # Tap Start
    result["state"] = "failed"
    logger.info(f"[proxy] Tapping Start at {button['coords']} on {device_id}")
    await tap(device_id, button["coords"][0], button["coords"][1])
    await asyncio.sleep(3.0)

    # Verify via VPN — fast and reliable, no dump_ui needed
    if await is_vpn_active(device_id):
        logger.info(f"[proxy] Proxy started successfully on {device_id} (VPN confirmed)")
        result["success"] = True
        result["state"] = "running"
        return result

    # VPN not active — try ad recovery (Home + reopen)
    await _recover_from_ad(device_id)
    await asyncio.sleep(2.0)
    if await is_vpn_active(device_id):
        _ad_devices.add(device_id)
        logger.info(f"[proxy] Proxy started on {device_id} after recovery (VPN confirmed)")
        result["success"] = True
        result["state"] = "running"
        return result

    result["error"] = "Proxy failed to start — VPN not active"
    result["state"] = "failed"
    return result


async def stop_proxy(device_id: str) -> dict:
    """
    Open proxy app and tap Stop.

    Strategy: if VPN is active we KNOW the proxy is running, so skip dump_ui
    (which triggers 'idle state' errors from the ad WebView) and go straight
    to blind tap.  Only fall back to dump_ui when VPN is already off (to
    confirm the button shows "Start").

    Returns {"success": bool, "state": "stopped"|"already_stopped"|"failed", "error": str|None}
    """
    result = {"success": False, "state": "error", "error": None}

    # ── Quick check: is proxy even running? ──
    vpn_active = await is_vpn_active(device_id)
    if not vpn_active:
        logger.info(f"[proxy] Stop: VPN already inactive on {device_id} — proxy already stopped")
        result["success"] = True
        result["state"] = "already_stopped"
        return result

    # ── VPN active → proxy is running. Open app and blind-tap Stop. ──
    # We intentionally skip dump_ui here because the Super Proxy ad WebView
    # causes 'could not get idle state', which triggers HOME recovery and
    # kicks us out of the app.  Blind tap is faster and more reliable.
    bx, by = await _get_blind_tap_coords(device_id)

    for attempt in range(3):
        # (Re)open proxy app
        if not await _ensure_proxy_open(device_id):
            logger.warning(f"[proxy] Stop: failed to open proxy app on {device_id} (attempt {attempt + 1})")
            await asyncio.sleep(1.0)
            continue

        # Give the app a moment to render (but don't dump_ui)
        await asyncio.sleep(1.5)

        # Blind tap the Stop button
        logger.info(f"[proxy] Stop: blind tap at ({bx}, {by}) on {device_id} (attempt {attempt + 1}/3)")
        await tap(device_id, bx, by)
        await asyncio.sleep(3.0)

        # Verify via VPN
        if not await is_vpn_active(device_id):
            logger.info(f"[proxy] Proxy stopped via blind tap on {device_id} (VPN confirmed down)")
            result["success"] = True
            result["state"] = "stopped"
            return result

        # Still active — might have hit the ad or wrong spot. Press Home and retry.
        logger.info(f"[proxy] Stop: VPN still active after blind tap attempt {attempt + 1} on {device_id}")
        await press_home(device_id)
        await asyncio.sleep(1.0)

    # ── All blind tap attempts failed — last resort: try dump_ui approach ──
    logger.warning(f"[proxy] Stop: all blind tap attempts failed on {device_id}, trying dump_ui fallback")
    if not await _ensure_proxy_open(device_id):
        result["error"] = "Failed to open proxy app for stop"
        return result

    await _navigate_to_proxy_main(device_id)

    xml = await dump_ui(device_id)
    if xml:
        button = _find_proxy_button(xml)
        if button and button["state"] == "stop":
            logger.info(f"[proxy] Stop: tapping Stop button at {button['coords']} on {device_id}")
            await tap(device_id, button["coords"][0], button["coords"][1])
            await asyncio.sleep(3.0)
            if not await is_vpn_active(device_id):
                logger.info(f"[proxy] Proxy stopped via dump_ui fallback on {device_id}")
                result["success"] = True
                result["state"] = "stopped"
                return result
        elif button and button["state"] == "start":
            logger.info(f"[proxy] Stop: button shows Start — proxy already stopped on {device_id}")
            result["success"] = True
            result["state"] = "already_stopped"
            return result

    logger.warning(f"[proxy] Could not stop proxy on {device_id} — VPN still active after all attempts")
    _ad_devices.add(device_id)
    result["error"] = "Proxy VPN still active after all stop attempts"
    result["state"] = "failed"
    return result
