"""
Combined Account Checker — checks:
  1. Account State: active / banned / expired / checkpoint (from UI on open)
  2. Account Status: N sections from Settings → Account Status screen
     (sections vary by account type — personal vs business/creator)

Flow (ADB only):
  Pre-flight: device online, IG installed
  Proxy: start if enabled (abort if fails)
  Open IG → detect account state from first screen →
  if active: Profile → Options → Settings → scroll → Account Status
  → dynamically detect sections → tap each → read detail → determine ok/problem
  Cleanup: back to IG home → press Home → stop proxy
"""

import asyncio
import random as _random
import time
from config import status_settings as settings
from logger import logger
from status.proxy import start_proxy, stop_proxy
from shared.adb import (
    wake_and_unlock,
    is_instagram_foreground,
    open_instagram,
    open_instagram_to_profile,
    profile_tab_coords,
    force_stop_instagram,
    dump_ui,
    tap,
    scroll_down,
    scroll_up,
    press_back,
    press_home,
    is_device_online,
    is_instagram_installed,
    get_screen_size,
    force_portrait,
    save_failed_dump,
    is_loading_spinner,
    test_device_connectivity,
)
from shared.ig_popup import ensure_clean_ig_state
from status.ui_parser import (
    detect_screen,
    detect_account_state,
    parse_account_status_summary,
    parse_account_status_detail,
    find_clickable_element,
)


NAV_DELAY = settings.nav_step_delay
SCROLL_DELAY = settings.scroll_delay
PAGE_LOAD_DELAY = settings.page_load_delay


def _human_delay(base: float) -> float:
    """Add ±20% random variation to a delay to mimic human timing."""
    jitter = base * 0.2
    return base + _random.uniform(-jitter, jitter)


class ProxyDroppedError(Exception):
    """Raised when VPN/proxy drops during an active check — triggers immediate abort."""
    pass


async def _assert_vpn(device_id: str, stage: str) -> bool:
    """Check VPN is still active.  If dropped, attempt recovery.

    Recovery: force-stop IG → HOME → re-start proxy → verify VPN → re-open IG.
    Returns False if VPN was fine, True if recovered (caller should re-navigate).
    Raises ProxyDroppedError only if recovery fails.
    """
    from shared.adb import is_vpn_active
    if await is_vpn_active(device_id):
        return False

    logger.warning(f"[proxy] VPN DROPPED during '{stage}' on {device_id} — attempting recovery")
    await force_stop_instagram(device_id)
    await press_home(device_id)

    try:
        from growth.proxy import start_proxy
        proxy_result = await start_proxy(device_id)
        if not proxy_result["success"]:
            logger.error(f"[proxy] Recovery failed — proxy start error: {proxy_result.get('error')} on {device_id}")
            raise ProxyDroppedError(f"Proxy VPN dropped during {stage} — recovery failed (proxy start)")

        if not await is_vpn_active(device_id):
            await asyncio.sleep(2.0)
            if not await is_vpn_active(device_id):
                logger.error(f"[proxy] VPN still not active after proxy restart on {device_id}")
                raise ProxyDroppedError(f"Proxy VPN dropped during {stage} — recovery failed (vpn still down)")

        logger.info(f"[proxy] VPN recovered on {device_id} during '{stage}'")

        await press_home(device_id)
        await asyncio.sleep(1.0)
        if not await _ensure_instagram_open(device_id):
            logger.error(f"[proxy] Could not reopen IG after VPN recovery on {device_id}")
            raise ProxyDroppedError(f"Proxy VPN dropped during {stage} — recovery failed (IG reopen)")

        await asyncio.sleep(2.0)
        return True

    except ProxyDroppedError:
        raise
    except Exception as e:
        logger.error(f"[proxy] Unexpected error during VPN recovery on {device_id}: {e}", exc_info=True)
        raise ProxyDroppedError(f"Proxy VPN dropped during {stage} — recovery error: {e}")

# Reference resolution — all hardcoded fallback coords are based on 1080x2160
_REF_W, _REF_H = 1080, 2160

# Cache screen sizes per device to avoid repeated ADB calls
_screen_cache: dict[str, tuple[int, int, float]] = {}  # device_id → (w, h, timestamp)


async def _get_screen(device_id: str) -> tuple[int, int]:
    """Get screen size for device, cached with TTL."""
    ttl = settings.screen_cache_ttl
    cached = _screen_cache.get(device_id)
    if cached:
        w, h, ts = cached
        if ttl <= 0 or (time.monotonic() - ts) < ttl:
            return w, h
    w, h = await get_screen_size(device_id)
    _screen_cache[device_id] = (w, h, time.monotonic())
    return w, h


def _scale_coords(w: int, h: int, ref_x: int, ref_y: int) -> tuple[int, int]:
    """Scale reference coordinates (based on 1080x2160) to actual screen size."""
    return (
        int(ref_x * w / _REF_W),
        int(ref_y * h / _REF_H),
    )


def _is_instagram_xml(xml: str) -> bool:
    """Check if a UI dump XML belongs to the Instagram app.
    Returns False if XML is from another app (camera, launcher, etc.)."""
    if not xml or len(xml) < 2000:
        return False
    return "com.instagram.android" in xml


async def _recover_to_instagram(device_id: str, context: str = "") -> str | None:
    """Detect if we're NOT in Instagram and recover.
    Returns fresh XML if recovery succeeded, None if failed.

    Recovery chain (gentle → aggressive):
      1. dump_ui (maybe we're already in IG)
      2. open_to_profile (navigate to Profile — static screen, no reels)
      3. force_stop + open_to_profile (kill everything, reopen to Profile)
    """
    xml = await dump_ui(device_id, safe_mode=True)
    if xml and _is_instagram_xml(xml):
        return xml

    # Not in IG — save failed dump for diagnosis, then recover
    ctx = f" during {context}" if context else ""
    logger.warning(f"[recovery] Not in Instagram{ctx} on {device_id} — recovering (xml_len={len(xml) if xml else 0})")
    if xml:
        save_failed_dump(device_id, xml, f"recovery_{context or 'unknown'}")
    await press_home(device_id)
    await asyncio.sleep(0.5)

    # Open IG + tap Profile — avoids home feed reels blocking UIAutomator
    if await open_instagram_to_profile(device_id):
        await asyncio.sleep(PAGE_LOAD_DELAY + 1.0)
        xml = await dump_ui(device_id, safe_mode=True)
        if xml and _is_instagram_xml(xml):
            logger.info(f"[recovery] Back in Instagram via open_to_profile on {device_id}")
            return xml
        if xml:
            save_failed_dump(device_id, xml, f"recovery2_{context or 'unknown'}")

    # ── Last resort: force-stop + reopen to Profile (guaranteed clean state) ──
    logger.warning(f"[recovery] Normal recovery failed on {device_id} — force-stopping + opening to Profile")
    await force_stop_instagram(device_id)
    await asyncio.sleep(0.5)
    await open_instagram_to_profile(device_id)
    await asyncio.sleep(PAGE_LOAD_DELAY + 1.5)
    xml = await dump_ui(device_id, safe_mode=True)
    if xml and _is_instagram_xml(xml):
        logger.info(f"[recovery] Back in Instagram via force_stop + profile on {device_id}")
        return xml
    if xml:
        save_failed_dump(device_id, xml, f"recovery3_{context or 'unknown'}")

    logger.error(f"[recovery] Could not recover to Instagram on {device_id}")
    return None


async def _verify_still_in_ig(device_id: str, xml: str | None, context: str = "") -> str | None:
    """Mid-check verification: ensure we're still inside Instagram.

    Call this after important taps during a status check.
    If the XML doesn't belong to IG (or is suspiciously small), triggers
    recovery and returns fresh IG XML. Returns None if recovery fails.

    If XML is valid IG, returns it unchanged (no extra work).
    """
    if xml and _is_instagram_xml(xml) and len(xml) > 5000:
        return xml  # Looks fine — still in IG

    # Not in IG or suspiciously small dump
    logger.warning(
        f"[mid-check] Lost Instagram on {device_id} during {context} "
        f"(xml_len={len(xml) if xml else 0}) — recovering"
    )
    if xml:
        save_failed_dump(device_id, xml, f"midcheck_{context}")

    # Try recovery
    recovered_xml = await _recover_to_instagram(device_id, context=f"midcheck_{context}")
    return recovered_xml


async def _wait_for_profile_load(device_id: str, xml: str) -> tuple[str, str | None]:
    """Wait for the profile to finish loading if a spinner is detected.

    Returns (xml, error_reason). error_reason is None if profile loaded OK,
    or a string like 'proxy_no_connection' / 'slow_connection' if it never loaded.
    """
    if not is_loading_spinner(xml):
        return xml, None

    logger.warning(f"[loading] Spinner detected on {device_id} — profile still loading, retrying")

    # Retry up to 5 more times (6 total including the original dump) with 7s waits
    for attempt in range(2, 7):
        await asyncio.sleep(7.0)
        xml = await dump_ui(device_id, safe_mode=True)
        if not xml:
            continue
        if not is_loading_spinner(xml):
            logger.info(f"[loading] Profile loaded on attempt {attempt} on {device_id}")
            return xml, None
        logger.warning(f"[loading] Still loading on attempt {attempt} on {device_id}")

    # After 6 attempts (~35s total), test connectivity
    connected = await test_device_connectivity(device_id)
    if connected:
        logger.info(f"[loading] Internet OK on {device_id} but IG slow — one more attempt with 10s wait")
        await asyncio.sleep(10.0)
        xml = await dump_ui(device_id, safe_mode=True)
        if xml and not is_loading_spinner(xml):
            return xml, None
        return xml or "", "slow_connection"

    # No internet — try rotating proxy
    logger.warning(f"[loading] No internet on {device_id} — attempting proxy rotate")
    from status.registry import get_device_by_id
    dev = get_device_by_id(device_id)
    if dev and dev.proxy_rotate_url:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as hc:
                rot = await hc.get(dev.proxy_rotate_url)
                logger.info(f"[loading] Proxy rotated on {device_id}: {rot.status_code}")
        except Exception as e:
            logger.warning(f"[loading] Proxy rotate failed on {device_id}: {e}")
        await asyncio.sleep(3.0)
        connected = await test_device_connectivity(device_id)
        if connected:
            logger.info(f"[loading] Internet restored after proxy rotate on {device_id}")
            await asyncio.sleep(5.0)
            xml = await dump_ui(device_id, safe_mode=True)
            if xml and not is_loading_spinner(xml):
                return xml, None
            return xml or "", "slow_connection_after_rotate"

    return xml or "", "proxy_no_connection"


# ── Multi-Account Helpers ────────────────────────────────────

# Words that appear in IG UI elements but are NOT usernames.
# Used to filter false positives in read_current_ig_username (all tiers).
_COMMON_WORDS = {
    "instagram", "profile", "search", "home", "reels", "explore", "share",
    "settings", "edit", "follow", "following", "followers", "posts", "saved",
    "archive", "close", "back", "menu", "more", "camera", "portrait",
    "landscape", "gmail", "email", "phone", "photo", "video", "story",
    "stories", "create", "discover", "activity", "notifications", "direct",
    "message", "send", "cancel", "done", "next", "save", "delete", "report",
    "block", "mute", "hide", "show", "open", "link", "copy", "text",
    "manila", "london", "paris", "tokyo", "york", "angeles", "chicago",
    "threads", "suggested", "recent", "tagged", "clips", "live", "shop",
    "surfshark", "vpn", "connected", "disconnected", "secure",
}

async def read_current_ig_username(device_id: str) -> str | None:
    """
    Read the currently active IG username from the profile screen.
    Returns lowercase username or None if not found.
    """
    from status.ui_parser import _get_all_nodes
    import re as _re

    xml = await dump_ui(device_id, safe_mode=True)
    if not xml:
        return None
    nodes = _get_all_nodes(xml)

    # Tier 1: resource-id for the username in the action bar
    # NOTE: _get_all_nodes returns keys with underscores (resource_id, content_desc)
    for rid in [
        "com.instagram.android:id/action_bar_large_title_auto_size",
        "com.instagram.android:id/action_bar_title",
        "com.instagram.android:id/action_bar_textview_title",
    ]:
        for n in nodes:
            if n.get("resource_id") == rid and n.get("text"):
                uname = n["text"].strip().lstrip("@").lower()
                if uname and len(uname) <= 30 and " " not in uname:
                    if uname in _COMMON_WORDS:
                        # Likely on a sub-screen (e.g. "Posts" grid) — back should
                        # return to the profile where the real username is visible.
                        logger.info(f"[multi-account] Tier 1 found common word '{uname}' in action bar on {device_id} — pressing back to recover")
                        await press_back(device_id)
                        await asyncio.sleep(1.5)
                        xml = await dump_ui(device_id, safe_mode=True)
                        if xml:
                            nodes = _get_all_nodes(xml)
                            for rid2 in [
                                "com.instagram.android:id/action_bar_large_title_auto_size",
                                "com.instagram.android:id/action_bar_title",
                                "com.instagram.android:id/action_bar_textview_title",
                            ]:
                                for n2 in nodes:
                                    if n2.get("resource_id") == rid2 and n2.get("text"):
                                        uname2 = n2["text"].strip().lstrip("@").lower()
                                        if uname2 and len(uname2) <= 30 and " " not in uname2 and uname2 not in _COMMON_WORDS:
                                            logger.info(f"[multi-account] Current IG username after back: {uname2} (via {rid2})")
                                            return uname2
                        # back didn't help — fall through to Tier 2/3
                        break
                    logger.info(f"[multi-account] Current IG username: {uname} (via {rid})")
                    return uname

    # Tier 2: content-desc on profile tab
    for n in nodes:
        cd = (n.get("content_desc") or "").lower()
        if "profile" in cd and "@" in cd:
            m = _re.search(r"@([a-z0-9._]+)", cd)
            if m:
                logger.info(f"[multi-account] Current IG username: {m.group(1)} (via content-desc)")
                return m.group(1)

    # Tier 3: top-zone text matching username pattern
    w, h = await _get_screen(device_id)
    top_zone = int(h * 0.20)
    for n in nodes:
        text = (n.get("text") or "").strip()
        bounds = n.get("bounds", "")
        if not text or not bounds:
            continue
        bm = _re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not bm:
            continue
        y1 = int(bm.group(2))
        if y1 < top_zone and 3 <= len(text) <= 30 and " " not in text:
            candidate = text.lower()
            if candidate in _COMMON_WORDS:
                continue
            if _re.match(r"^[a-z0-9._]+$", candidate):
                logger.info(f"[multi-account] Possible IG username: {candidate} (via top-zone text)")
                return candidate

    logger.warning(f"[multi-account] Could not read current IG username on {device_id}")
    return None


async def switch_ig_account(device_id: str, target_username: str) -> bool:
    """
    Switch IG to a different account via the profile username dropdown.
    Returns True if switch was successful (or already on correct account).
    """
    from status.ui_parser import _get_all_nodes, get_center_of_bounds

    target = target_username.strip().lstrip("@").lower()
    logger.info(f"[multi-account] Switching to @{target} on {device_id}")

    # ── Step 1: Check if IG is already on profile BEFORE disrupting state ──
    # This avoids the blind tap in open_instagram_to_profile which can hit
    # profile thumbnails and open a post/reel (causing idle-state failures).
    already_on_profile = False
    xml = None

    if await is_instagram_foreground(device_id):
        xml = await dump_ui(device_id, safe_mode=True)
        if xml and _is_instagram_xml(xml):
            screen = detect_screen(xml)
            if screen == "profile":
                already_on_profile = True
                profile_xml = xml
                logger.info(f"[multi-account] IG already on profile — skipping reopen on {device_id}")
            else:
                from status.ui_parser import _find_node_by_text
                hints = _find_node_by_text(_get_all_nodes(xml), [
                    "Edit profile", "Editar perfil", "Share profile", "Compartir perfil",
                    "Professional dashboard", "Your dashboard", "Panel profesional",
                    "Posts", "Followers", "Following",
                ])
                if hints:
                    already_on_profile = True
                    profile_xml = xml
                    logger.info(f"[multi-account] Profile detected via text hints — skipping reopen on {device_id}")

    # ── Step 2: If not on profile, do the full reopen cycle ──
    if not already_on_profile:
        await press_home(device_id)
        await asyncio.sleep(1.0)

        await open_instagram_to_profile(device_id)
        await asyncio.sleep(2.0)

        # Guard: verify we're actually in Instagram
        xml = await dump_ui(device_id, safe_mode=True)
        if not xml or not _is_instagram_xml(xml):
            logger.warning(f"[multi-account] Not in Instagram after open_to_profile on {device_id} — recovering")
            if xml:
                save_failed_dump(device_id, xml, "switch_account")
            xml = await _recover_to_instagram(device_id, context="switch_ig_account")
            if not xml:
                logger.error(f"[multi-account] Cannot recover to Instagram on {device_id}")
                return False

        # Check if we landed on profile after reopen
        screen = detect_screen(xml)
        if screen == "profile":
            already_on_profile = True
            profile_xml = xml
            logger.info(f"[multi-account] On profile after reopen on {device_id}")
        else:
            from status.ui_parser import _find_node_by_text
            hints = _find_node_by_text(_get_all_nodes(xml), [
                "Edit profile", "Editar perfil", "Share profile", "Compartir perfil",
                "Professional dashboard", "Your dashboard", "Panel profesional",
                "Posts", "Followers", "Following",
            ])
            if hints:
                already_on_profile = True
                profile_xml = xml
                logger.info(f"[multi-account] Profile detected via hints after reopen on {device_id}")

    # ── Step 3: If still not on profile, navigate using resource-id ──
    if not already_on_profile:
        profile_xml = await _navigate_to_profile(device_id)
        if not profile_xml:
            logger.error(f"[multi-account] Cannot navigate to profile on {device_id}")
            return False

    # Check current username
    current = await read_current_ig_username(device_id)
    if current and current == target:
        logger.info(f"[multi-account] Already on @{target} — no switch needed")
        return True

    # Find and tap the username switcher element (with retry)
    switcher_tapped = False
    switcher_rids = [
        "com.instagram.android:id/action_bar_new_title_container",
        "com.instagram.android:id/action_bar_username_container",
        "com.instagram.android:id/action_bar_large_title_auto_size",
        "com.instagram.android:id/action_bar_title",
        "com.instagram.android:id/action_bar_textview_title",
    ]

    for attempt in range(2):
        profile_xml = await dump_ui(device_id, safe_mode=True)
        if not profile_xml:
            profile_xml = ""
        nodes = _get_all_nodes(profile_xml)

        for rid in switcher_rids:
            for n in nodes:
                if n.get("resource_id") == rid and n.get("bounds"):
                    coords = get_center_of_bounds(n["bounds"])
                    if coords:
                        await tap(device_id, coords[0], coords[1])
                        switcher_tapped = True
                        logger.info(f"[multi-account] Tapped username element ({rid}) to open switcher (attempt {attempt + 1})")
                        break
            if switcher_tapped:
                break
        if switcher_tapped:
            break

        logger.info(f"[multi-account] Switcher element not found (attempt {attempt + 1}), waiting 2s to retry")
        await asyncio.sleep(2.0)

    if not switcher_tapped:
        w, h = await _get_screen(device_id)
        fx, fy = int(w * 0.15), int(h * 0.045)
        logger.info(f"[multi-account] Username not found after retries, tapping fallback ({fx}, {fy})")
        await tap(device_id, fx, fy)

    await asyncio.sleep(_human_delay(1.5))

    # Find target in dropdown
    xml = await dump_ui(device_id, safe_mode=True)
    if not xml:
        logger.error(f"[multi-account] UI dump failed after opening switcher on {device_id}")
        return False

    nodes = _get_all_nodes(xml)
    target_node = None
    for n in nodes:
        text = (n.get("text") or "").strip().lstrip("@").lower()
        if text == target and n.get("bounds"):
            target_node = n
            break
        cd = (n.get("content_desc") or "").strip().lstrip("@").lower()
        if cd == target and n.get("bounds"):
            target_node = n
            break

    if not target_node:
        logger.error(f"[multi-account] @{target} not found in dropdown on {device_id}")
        await press_back(device_id)
        await asyncio.sleep(0.5)
        return False

    coords = get_center_of_bounds(target_node["bounds"])
    if not coords:
        await press_back(device_id)
        await asyncio.sleep(0.5)
        return False

    await tap(device_id, coords[0], coords[1])
    logger.info(f"[multi-account] Tapped @{target} in dropdown on {device_id}")
    await asyncio.sleep(_human_delay(3.5))

    # ── Dismiss popup if present (e.g. "Save your login info?") ──
    from shared.ig_popup import find_dismiss_button
    popup_xml = await dump_ui(device_id, safe_mode=True)
    if popup_xml:
        btn = find_dismiss_button(popup_xml)
        if btn:
            (bx, by), label = btn
            logger.info(f"[multi-account] Popup detected after switch — tapping '{label}' at ({bx},{by}) on {device_id}")
            await tap(device_id, bx, by, jitter=3)
            await asyncio.sleep(_human_delay(2.0))

    # Verify
    verify = await read_current_ig_username(device_id)
    if verify and verify == target:
        logger.info(f"[multi-account] Successfully switched to @{target} on {device_id}")
        return True

    await asyncio.sleep(2.0)
    verify2 = await read_current_ig_username(device_id)
    if verify2 and verify2 == target:
        logger.info(f"[multi-account] Switch to @{target} confirmed on retry")
        return True

    logger.error(f"[multi-account] Failed to switch to @{target} on {device_id} (current: @{verify2})")
    return False


async def _ensure_instagram_open(device_id: str) -> bool:
    """Make sure Instagram is open and in foreground.
    Opens IG and immediately taps Profile tab (avoids home feed reels
    that block UIAutomator).
    After confirming IG is in foreground, checks for popups (e.g.
    "Save your login info?") and dismisses them if needed."""
    ig_open = False

    if await is_instagram_foreground(device_id):
        ig_open = True
    else:
        # Open IG + tap Profile — avoids home feed reels blocking UIAutomator
        if await open_instagram_to_profile(device_id):
            await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY + 1.0))
            if await is_instagram_foreground(device_id):
                ig_open = True

    if not ig_open:
        return False

    # ── Early account-state probe ─────────────────────────────────
    # ensure_clean_ig_state below does press_back + double-back + reopen
    # when no tab_bar is visible. On checkpoint/banned/expired screens
    # there IS no tab_bar, and those press_backs end up closing the IG
    # session entirely — making the parser see "log in" (expired) instead
    # of the real "Enter your mobile number" (checkpoint).
    # Probe first; if state is problematic, bail out NOW so check_account's
    # downstream logic detects the real state on the untouched screen.
    try:
        early_xml = await dump_ui(device_id, safe_mode=True)
        if early_xml:
            sr = detect_account_state(early_xml)
            early_state = sr.get("state") if isinstance(sr, dict) else sr
            if early_state in ("checkpoint", "banned", "expired"):
                logger.warning(
                    f"[ensure_ig_open] Account state '{early_state}' detected "
                    f"early on {device_id} — skipping ensure_clean_ig_state to "
                    f"preserve the screen for state detection"
                )
                return False
    except Exception as e:
        logger.debug(f"[ensure_ig_open] Early state probe failed on {device_id}: {e}")

    # ── Verify IG is in a clean state (no popups blocking) ──
    return await ensure_clean_ig_state(device_id, delay_fn=_human_delay)


async def _detect_state_from_ui(device_id: str) -> dict:
    """
    Dump UI and detect account state (banned/checkpoint/expired/active).
    If state is unknown (IG still loading), retry once after a short wait.
    Returns detect_account_state() result.
    """
    xml = await dump_ui(device_id, safe_mode=True, idle_escape="force_stop_cycle")
    if not xml:
        return {"state": "unknown", "matched_signal": None, "confidence": "low"}

    state = detect_account_state(xml)
    if state["state"] != "unknown":
        return state

    # IG may not have finished loading — retry once
    logger.info(f"[state] State unknown on {device_id} — waiting 4s and retrying")
    await asyncio.sleep(_human_delay(4.0))
    xml = await dump_ui(device_id, safe_mode=True, idle_escape="force_stop_cycle")
    if not xml:
        return state  # Return original unknown
    return detect_account_state(xml)


async def _navigate_to_profile(device_id: str) -> str | None:
    """Navigate to the profile tab.
    Returns the last XML dump (for reuse by next step) or None on failure."""
    from status.ui_parser import _get_all_nodes, _find_node_by_resource_id, _find_node_by_text, get_center_of_bounds

    xml = await dump_ui(device_id, safe_mode=True, idle_escape="force_stop_cycle")  # DUMP 1
    if not xml:
        return None

    # ── Guard: verify we're actually in Instagram ──
    if not _is_instagram_xml(xml):
        logger.warning(f"[nav] Not in Instagram on {device_id} (xml_len={len(xml)}) — recovering")
        xml = await _recover_to_instagram(device_id, context="navigate_to_profile")
        if not xml:
            return None

    screen = detect_screen(xml)
    logger.info(f"[nav] Current screen: {screen}")

    if screen == "profile":
        return xml

    nodes = _get_all_nodes(xml)
    w, h = await _get_screen(device_id)
    nav_bar_min_y = int(h * 0.88)  # Nav bar is in the bottom ~12% of the screen

    # Find profile_tab by resource-id but ONLY in the bottom navbar zone.
    # "profile_tab" partial-matches profile_tab_layout, profile_tab_icon_view, etc.
    # which are in the MID-screen area (profile tabs for Grid/Reels/Tagged).
    profile_node = None
    coords = None
    for n in nodes:
        rid = n.get("resource_id", "")
        if "com.instagram.android:id/profile_tab" == rid and n.get("bounds"):
            c = get_center_of_bounds(n["bounds"])
            if c and c[1] >= nav_bar_min_y:
                profile_node = n
                coords = c
                logger.info(f"[nav] Found profile_tab by resource-id at {coords} (in navbar zone)")
                break

    if not coords:
        coords = find_clickable_element(xml, ["Profile", "Perfil"])

    if not coords:
        # Tier 3 fallback — Pixel-aware via shared helper
        coords = profile_tab_coords(w, h)
        logger.info(f"[nav] Profile tab not found in XML, using scaled fallback {coords} (screen {w}x{h})")

    await tap(device_id, coords[0], coords[1])
    await asyncio.sleep(_human_delay(NAV_DELAY + 0.5))

    # OPT: Skip verification dump if we found profile_tab by resource-id
    # (reliable indicator — the tap will land on the right place)
    if profile_node and profile_node["bounds"]:
        logger.info("[nav] Trusting profile_tab resource-id tap — skipping verification dump")
        return xml  # Return stale XML (caller only needs Options button which is on profile)

    xml = await dump_ui(device_id, safe_mode=True)                     # DUMP 2 (only if no resource-id)
    if not xml:
        return None

    screen = detect_screen(xml)
    if screen == "profile":
        return xml

    if screen != "home":
        profile_nodes = _get_all_nodes(xml)
        has_profile_hints = _find_node_by_text(profile_nodes, [
            "Edit profile", "Editar perfil", "Share profile", "Compartir perfil",
            "Professional dashboard", "Your dashboard", "Panel profesional",
            "Posts", "Followers", "Following", "Publicaciones", "Seguidores", "Siguiendo"
        ])
        if has_profile_hints:
            logger.info("[nav] Profile screen detected via fallback text match")
            return xml

    # ── "other" screen — only proceed if XML is actually from IG ──
    if screen == "other" and _is_instagram_xml(xml):
        logger.info("[nav] Screen is 'other' but in IG — proceeding optimistically")
        return xml

    logger.warning(f"[nav] Failed to navigate to profile on {device_id} (screen={screen})")
    return None


async def _navigate_to_settings(device_id: str, profile_xml: str | None = None) -> str | None:
    """From profile, tap Options — this goes DIRECTLY to Settings (no intermediate menu).
    Retries once if the first tap doesn't land on Settings.
    Returns the last XML dump (for reuse) or None on failure."""
    settings_texts = [
        "Settings and activity", "Settings and privacy",
        "Your account", "Tu cuenta",
        "Accounts Center", "Centro de cuentas",
        "Notifications", "Notificaciones",
    ]

    for attempt in range(2):
        # OPT: Reuse XML from _navigate_to_profile on first attempt
        if attempt == 0 and profile_xml:
            xml = profile_xml
            logger.info("[nav] Reusing profile XML to find Options button — skipping dump")
        else:
            xml = await dump_ui(device_id, safe_mode=True)
            if not xml:
                return None

        coords = find_clickable_element(xml, ["Options", "Opciones"])

        if not coords:
            w, h = await _get_screen(device_id)
            coords = _scale_coords(w, h, 992, 143)
            logger.info(f"[nav] Menu icon not found in XML, using scaled fallback {coords} (screen {w}x{h})")

        await tap(device_id, coords[0], coords[1])
        # IG side drawer + menu items can take 10-12s to fully render under slow
        # proxy conditions. PAGE_LOAD_DELAY (~3s) was too short — _is_compose_settings
        # gave false positives on partial dumps (empty_views=16, readable=0 matches
        # both real Compose Settings AND a drawer mid-animation), causing
        # _navigate_to_settings to return prematurely with stale XML. Wait 15s
        # before the first dump so detectors run against a fully-rendered screen.
        wait = 15.0
        for check in range(2):
            await asyncio.sleep(wait)
            xml = await dump_ui(device_id, safe_mode=True)
            if not xml:
                return None

            if detect_screen(xml) == "settings":
                logger.info("[nav] Settings screen reached via Options tap")
                return xml

            from status.ui_parser import _get_all_nodes, _find_node_by_text, _is_compose_settings
            nodes = _get_all_nodes(xml)
            if _find_node_by_text(nodes, settings_texts):
                logger.info("[nav] Detected settings screen via fallback text match")
                return xml

            # Compose-based Settings (newer IG) — UIAutomator can't read text
            if _is_compose_settings(xml):
                logger.info("[nav] Detected Compose-based Settings screen (text not readable)")
                return xml

            if check == 0:
                # Settings may still be loading — wait 15s more before re-checking
                logger.info(f"[nav] Settings not loaded yet on {device_id} — waiting 15s extra")
                wait = 15.0

        if attempt == 0:
            logger.info(f"[nav] Settings not detected on {device_id} — retrying Options tap")
            await press_back(device_id)
            await asyncio.sleep(_human_delay(NAV_DELAY))

    logger.warning(f"[nav] Failed to navigate to settings on {device_id} after 2 attempts")
    return None


async def _navigate_to_account_status(device_id: str, settings_xml: str | None = None) -> bool:
    """From Settings, scroll down and tap Account Status.
    Handles Compose-based Settings where UIAutomator can't read text —
    falls back to blind scroll + tap at estimated positions.

    OPT: Account Status is always near the bottom of Settings.  Instead of
    scroll→dump→check on every scroll, we:
      1. Check the reused settings XML first (free — no dump).
      2. If not found, do 3 blind scrolls (no dumps) then dump once.
      3. Only if that fails, fall back to scroll+dump per iteration.
    This saves 2-3 dump_ui calls in the common case.
    """
    from status.ui_parser import _is_compose_settings
    max_scrolls = settings.max_scrolls_to_find
    BLIND_SCROLLS = 3  # Number of scrolls before first dump

    _acct_status_texts = ["Account Status", "Estado de la cuenta", "Account status"]

    # ── Phase 0: Check reused settings XML (no dump) ──
    if settings_xml:
        logger.info("[nav] Reusing settings XML for Account Status search — skipping dump")

        if _is_compose_settings(settings_xml):
            logger.info("[nav] Compose Settings detected — using blind navigation for Account Status")
            return await _navigate_account_status_compose(device_id)

        coords = find_clickable_element(settings_xml, _acct_status_texts)
        if coords:
            logger.info(f"[nav] Found Account Status in reused XML at ({coords[0]}, {coords[1]})")
            if await _tap_and_verify_account_status(device_id, coords):
                return True
            return False

    # ── Phase 1: Blind scrolls — scroll 3x without dumping ──
    logger.info(f"[nav] Account Status not visible yet — blind-scrolling {BLIND_SCROLLS}x to bottom area")
    for i in range(BLIND_SCROLLS):
        await scroll_down(device_id, amount=0.35)
        await asyncio.sleep(_human_delay(SCROLL_DELAY))

    # Single dump after blind scrolls
    xml = await dump_ui(device_id, safe_mode=True)
    if not xml:
        return False

    if _is_compose_settings(xml):
        logger.info("[nav] Compose Settings detected after scrolling — using blind navigation")
        return await _navigate_account_status_compose(device_id)

    coords = find_clickable_element(xml, _acct_status_texts)
    if coords:
        logger.info(f"[nav] Found Account Status at ({coords[0]}, {coords[1]}) after {BLIND_SCROLLS} blind scrolls")
        if await _tap_and_verify_account_status(device_id, coords):
            return True
        return False

    # ── Phase 2: Fallback — scroll+dump one at a time for remaining attempts ──
    remaining = max_scrolls - BLIND_SCROLLS
    for scroll_attempt in range(remaining):
        logger.debug(f"[nav] Account Status not visible, scrolling down (fallback {scroll_attempt + 1}/{remaining})")
        await scroll_down(device_id, amount=0.35)
        await asyncio.sleep(_human_delay(SCROLL_DELAY))

        xml = await dump_ui(device_id, safe_mode=True)
        if not xml:
            return False

        if _is_compose_settings(xml):
            logger.info("[nav] Compose Settings detected in fallback — using blind navigation")
            return await _navigate_account_status_compose(device_id)

        coords = find_clickable_element(xml, _acct_status_texts)
        if coords:
            total_scrolls = BLIND_SCROLLS + scroll_attempt + 1
            logger.info(f"[nav] Found Account Status at ({coords[0]}, {coords[1]}) after {total_scrolls} total scrolls")
            if await _tap_and_verify_account_status(device_id, coords):
                return True
            return False

    logger.warning(f"[nav] Account Status not found after {max_scrolls} scrolls on {device_id}")
    return False


async def _tap_and_verify_account_status(device_id: str, coords: tuple[int, int]) -> bool:
    """Tap Account Status and verify the screen loaded, with escalating retries.

    Retry strategy:
      1. Tap → wait PAGE_LOAD_DELAY → verify
      2. If fail: wait 3s more → verify again (screen may still be loading)
      3. If fail: retry tap → wait PAGE_LOAD_DELAY + 2s → verify
    """
    # Attempt 1: tap and verify
    await tap(device_id, coords[0], coords[1])
    await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY))
    if await _verify_account_status_screen(device_id):
        return True

    # Attempt 2: wait longer — screen might still be loading
    logger.info(f"[nav] Account Status not loaded yet — waiting 3s extra on {device_id}")
    await asyncio.sleep(3.0)
    if await _verify_account_status_screen(device_id):
        return True

    # Attempt 3: retry the tap entirely
    logger.info(f"[nav] Retrying Account Status tap at ({coords[0]}, {coords[1]}) on {device_id}")
    await tap(device_id, coords[0], coords[1])
    await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY + 2.0))
    if await _verify_account_status_screen(device_id):
        return True

    logger.warning(f"[nav] Account Status screen did not load after 3 attempts on {device_id}")
    return False


async def _verify_account_status_screen(device_id: str) -> bool:
    """Verify we're on the Account Status screen (works for both normal and Compose UIs)."""
    xml = await dump_ui(device_id, safe_mode=True)
    if not xml:
        return False

    if detect_screen(xml) == "account_status":
        return True

    from status.ui_parser import _get_all_nodes, _find_node_by_text, _signals
    nodes = _get_all_nodes(xml)
    sections_config = _signals.get("sections", {})
    all_labels = []
    for sk, sd in sections_config.items():
        if sk.startswith("_") or not isinstance(sd, dict):
            continue
        all_labels.extend(sd.get("label_texts", []))
    if all_labels and _find_node_by_text(nodes, all_labels):
        logger.info("[nav] Detected Account Status screen via section text match")
        return True

    return False


async def _navigate_account_status_compose(device_id: str) -> bool:
    """Blind navigation to Account Status from Compose-based Settings.

    Strategy: scroll to the very bottom of Settings (25+ items), then extract
    clickable View node positions from the XML dump and try tapping them from
    bottom to top.  Even though Compose text is unreadable, the View nodes
    have valid bounds we can use to tap real items (not random positions).

    Account Status is near the very end of Settings, just above
    'More info and support' / 'Log out' / 'Add account'.
    """
    w, h = await _get_screen(device_id)

    # Phase 1: Scroll to the very bottom of Settings
    # Settings has 25+ items, ~15 visible per screen → need ~6 scrolls
    for i in range(6):
        await scroll_down(device_id, amount=0.5)
        await asyncio.sleep(_human_delay(0.5))

    # Phase 2: Get the current XML and extract clickable item positions
    xml = await dump_ui(device_id, safe_mode=True)
    if not xml:
        return False

    # First: check if text became readable after scrolling
    coords = find_clickable_element(xml, [
        "Account Status", "Estado de la cuenta", "Account status"
    ])
    if coords:
        logger.info(f"[nav] Found Account Status text after scrolling at {coords}")
        await tap(device_id, coords[0], coords[1])
        await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY))
        if await _verify_account_status_screen(device_id):
            return True

    # Extract clickable item positions from the Compose View tree.
    # Items are clickable android.view.View with consistent row heights (~130px).
    # We skip the header area (y < 200) and bottom nav (y > h - 150).
    item_centers = _extract_compose_item_centers(xml, min_y=200, max_y=h - 150)
    if not item_centers:
        logger.warning(f"[nav] No Compose items found in XML on {device_id}")
        return False

    # Try items from BOTTOM to TOP (Account Status is near the end)
    item_centers.sort(key=lambda c: c[1], reverse=True)
    # Limit to 8 items max to avoid spending too long
    item_centers = item_centers[:8]

    logger.info(f"[nav] Compose: trying {len(item_centers)} item positions bottom-to-top")

    for i, (cx, cy) in enumerate(item_centers):
        logger.info(f"[nav] Compose blind tap {i + 1}/{len(item_centers)} at ({cx}, {cy})")
        await tap(device_id, cx, cy)
        await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY))

        if await _verify_account_status_screen(device_id):
            logger.info(f"[nav] Account Status reached via Compose item at ({cx}, {cy})")
            return True

        # Not Account Status — go back (scroll position is usually preserved)
        await press_back(device_id)
        await asyncio.sleep(_human_delay(NAV_DELAY))

    logger.warning(f"[nav] Failed to find Account Status via Compose navigation on {device_id}")
    return False


def _extract_compose_item_centers(
    xml_str: str, min_y: int = 200, max_y: int = 2000
) -> list[tuple[int, int]]:
    """Extract center coordinates of clickable items from a Compose-based Settings XML.

    Compose Settings items are clickable android.view.View with consistent
    heights (~100-140px).  We filter to full-width items in the content area
    to avoid tapping headers, dividers, or nav elements.
    """
    import xml.etree.ElementTree as ET
    from status.ui_parser import parse_bounds
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return []

    items = []
    seen_ys = set()  # Avoid duplicate rows

    for elem in root.iter():
        cls = elem.get("class", "")
        clickable = elem.get("clickable", "false") == "true"
        bounds_str = elem.get("bounds", "")

        if not clickable or "View" not in cls:
            continue

        b = parse_bounds(bounds_str)
        if not b:
            continue

        left, top, right, bottom = b
        item_h = bottom - top
        item_w = right - left
        center_y = (top + bottom) // 2
        center_x = (left + right) // 2

        # Filter: full-width row items in content area with reasonable height
        if item_w < 800:  # Must be nearly full-width (not an icon or small element)
            continue
        if item_h < 60 or item_h > 200:  # Row-sized items only
            continue
        if center_y < min_y or center_y > max_y:
            continue

        # Avoid tapping the same row twice (items may overlap slightly)
        row_key = center_y // 50  # Group nearby y-coords
        if row_key in seen_ys:
            continue
        seen_ys.add(row_key)

        items.append((center_x, center_y))

    logger.debug(f"[nav] Extracted {len(items)} Compose item positions: {items}")
    return items


async def _read_summary(device_id: str) -> dict:
    """Read the Account Status summary screen.
    Scrolls down once if known sections from config are missing (below fold)."""
    from status.ui_parser import _signals

    xml = await dump_ui(device_id, safe_mode=True)
    if not xml:
        return parse_account_status_summary("")

    summary = parse_account_status_summary(xml)

    # Check if any known config sections are missing — they might be below the fold
    sections_config = _signals.get("sections", {})
    known_keys = [k for k in sections_config if not k.startswith("_") and isinstance(sections_config[k], dict)]
    found_keys = [k for k, v in summary.items() if v.get("found")]

    missing_known = [k for k in known_keys if k not in found_keys]
    if missing_known and len(found_keys) >= 1:
        # We found some sections but not all known ones — scroll to reveal more
        logger.info(f"[summary] Missing sections {missing_known} — scrolling down to check below fold")
        await scroll_down(device_id, amount=0.3)
        await asyncio.sleep(_human_delay(SCROLL_DELAY))

        xml2 = await dump_ui(device_id, safe_mode=True)
        if xml2:
            summary2 = parse_account_status_summary(xml2)
            # Merge: add newly found sections that weren't in the first pass
            for key, data in summary2.items():
                if key not in summary and data.get("found"):
                    summary[key] = data
                    logger.info(f"[summary] Found '{key}' after scrolling")

        # Scroll back up so detail taps can start from the top
        await scroll_up(device_id, amount=0.3)
        await asyncio.sleep(_human_delay(SCROLL_DELAY))

    return summary


def _get_action_bar_title(xml: str) -> str:
    """Extract the action_bar_title text from an XML dump."""
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        for elem in root.iter():
            if (elem.get("resource-id") or "") == "com.instagram.android:id/action_bar_title":
                return (elem.get("text") or elem.get("content-desc") or "").strip()
    except Exception:
        pass
    return ""


def _is_on_expected_detail(xml: str, section_key: str, section_texts: list[str]) -> bool:
    """Check if the detail page we navigated to matches the expected section.

    Returns True if:
      - The action_bar_title contains any of the section_texts (flexible match), OR
      - The title is 'Account Status' and we already verified we're not on the main page
        (content_removed detail keeps 'Account Status' as title), OR
      - We can't determine the title (assume correct).
    """
    title = _get_action_bar_title(xml)
    if not title:
        return True  # can't verify — assume correct

    # Check if any section label appears in the title
    title_lower = title.lower()
    for text in section_texts:
        if text.lower() in title_lower:
            return True

    # content_removed detail shows "Account Status" as title (same as main page)
    # Since caller already verified _is_still_on_main_page == False, accept it
    if title_lower == "account status":
        return True

    logger.warning(
        f"[detail] Landed on wrong section! Expected '{section_key}' "
        f"(texts={section_texts[:2]}), got title='{title}'"
    )
    return False


def _is_still_on_main_page(xml: str) -> bool:
    """Detect if we're still on the Account Status main page (not a detail page).

    The main page shows multiple section titles at once (e.g. "Content and
    messages", "Features", "Reach", "Under 18").  A detail page may contain
    text that *substring-matches* labels from other sections (e.g. the
    limits_to_reach detail page mentions "People under 18" which matches
    the short label "Under 18").

    To avoid false positives we count **distinct sections** that have at
    least one label match — not individual label strings.  The main page
    has 3+ distinct sections visible; a detail page typically matches only
    1-2 section keys.
    """
    from status.ui_parser import _signals, _get_all_nodes
    sections_config = _signals.get("sections", {})

    # Build label → section_key mapping (many labels can map to one section)
    label_to_section: dict[str, str] = {}
    for sec_key, sec_def in sections_config.items():
        if sec_key.startswith("_"):
            continue
        if isinstance(sec_def, dict):
            for lbl in sec_def.get("label_texts", []):
                label_to_section[lbl.lower()] = sec_key

    # Count how many distinct sections have a label visible in the XML
    nodes = _get_all_nodes(xml)
    found_sections: set[str] = set()
    for node in nodes:
        text = (node.get("text", "") or "").strip().lower()
        desc = (node.get("content_desc", "") or "").strip().lower()
        for lbl, sec_key in label_to_section.items():
            if lbl in text or lbl in desc:
                found_sections.add(sec_key)
    return len(found_sections) >= 3


async def _read_section_detail(
    device_id: str,
    section_key: str,
    section_texts: list[str],
    main_page_xml: str | None = None,
) -> tuple[dict, str | None]:
    """Tap into a section, read detail, determine ok/problem, go back.
    Verifies we actually navigated to the detail page. If still on main page,
    retries the tap once (page may have been loading).

    Args:
        main_page_xml: Optional XML of the Account Status main page (reuse from
                       previous iteration to skip the 'find' dump).

    Returns:
        (detail_result, main_page_xml_after_back) — the second element is the
        XML after pressing back (for reuse by the next section).
    """
    # OPT: Reuse main page XML from previous iteration if available
    if main_page_xml:
        xml = main_page_xml
        logger.info(f"[detail] Reusing main page XML for '{section_key}' — skipping find dump")
    else:
        xml = await dump_ui(device_id, safe_mode=True)
        if not xml:
            return {"status": "unknown", "error": "Failed to dump UI", "texts": []}, None

    coords = find_clickable_element(xml, section_texts)
    if not coords:
        # Section might be below the visible area — scroll down once and retry
        logger.info(f"[detail] Section '{section_key}' not visible — scrolling down to find it")
        await scroll_down(device_id, amount=0.3)
        await asyncio.sleep(_human_delay(SCROLL_DELAY))
        xml = await dump_ui(device_id, safe_mode=True)
        if xml:
            coords = find_clickable_element(xml, section_texts)
    if not coords:
        return {"status": "unknown", "error": f"Section '{section_key}' not found on screen", "texts": []}, None

    await tap(device_id, coords[0], coords[1])
    await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY))

    xml = await dump_ui(device_id, safe_mode=True)                     # DUMP: read detail
    if not xml:
        await press_back(device_id)
        await asyncio.sleep(_human_delay(NAV_DELAY))
        return {"status": "unknown", "texts": [], "error": "Failed to dump detail"}, None

    # Verify we left the main page — if still there, WAIT first (don't re-tap
    # immediately — Bloks UI may queue taps and process them with delay, causing
    # navigation to the wrong section if multiple taps stack up).
    if _is_still_on_main_page(xml):
        navigated = False

        # Phase 1: wait-only (the original tap may still be in Bloks queue)
        for wait_idx, wait_secs in enumerate([3.0, 4.0], 1):
            logger.info(f"[detail] Still on main page after tapping '{section_key}' — waiting {wait_secs}s ({wait_idx}/2)")
            await asyncio.sleep(wait_secs)
            xml = await dump_ui(device_id, safe_mode=True)
            if xml and not _is_still_on_main_page(xml):
                navigated = True
                break

        # Phase 2: if still stuck, scroll slightly to force Bloks re-render,
        # re-dump for fresh clickable ancestors, then tap once more.
        if not navigated:
            logger.info(f"[detail] Tap didn't register for '{section_key}' — scroll nudge + retap")
            # Small scroll up then down to force Bloks to refresh element tree
            await scroll_down(device_id, amount=0.15)
            await asyncio.sleep(0.5)
            await scroll_up(device_id, amount=0.15)
            await asyncio.sleep(1.0)
            xml = await dump_ui(device_id, safe_mode=True)
            if xml:
                coords = find_clickable_element(xml, section_texts)
                if coords:
                    await tap(device_id, coords[0], coords[1])
                    await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY + 5.0))
                    xml = await dump_ui(device_id, safe_mode=True)
                    if xml and not _is_still_on_main_page(xml):
                        navigated = True

        if not navigated:
            logger.warning(f"[detail] Still on main page after retries for '{section_key}'")
            await press_back(device_id)
            await asyncio.sleep(_human_delay(NAV_DELAY))
            return {"status": "unknown", "texts": [], "error": f"Could not navigate into {section_key} detail"}, None

    # Verify we landed on the CORRECT detail page (not a different section
    # due to a delayed/stacked tap from a previous iteration).
    if not _is_on_expected_detail(xml, section_key, section_texts):
        logger.warning(f"[detail] Wrong detail page for '{section_key}' — pressing back to retry")
        await press_back(device_id)
        await asyncio.sleep(_human_delay(NAV_DELAY))
        # Re-dump and try ONE more time
        xml = await dump_ui(device_id, safe_mode=True)
        if xml and _is_still_on_main_page(xml):
            coords = find_clickable_element(xml, section_texts)
            if coords:
                await tap(device_id, coords[0], coords[1])
                await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY + 3.0))
                xml = await dump_ui(device_id, safe_mode=True)
                if not xml or _is_still_on_main_page(xml) or not _is_on_expected_detail(xml, section_key, section_texts):
                    logger.warning(f"[detail] Could not reach correct detail for '{section_key}' after wrong-page recovery")
                    if xml and not _is_still_on_main_page(xml):
                        await press_back(device_id)
                        await asyncio.sleep(_human_delay(NAV_DELAY))
                    return {"status": "unknown", "texts": [], "error": f"Landed on wrong section for {section_key}"}, None
        else:
            return {"status": "unknown", "texts": [], "error": f"Landed on wrong section for {section_key}"}, None

    detail = parse_account_status_detail(xml, section_key=section_key)

    await press_back(device_id)
    await asyncio.sleep(_human_delay(NAV_DELAY))

    # OPT: After pressing back we're on the main page again.
    # Don't dump here — the NEXT section call will reuse the main page XML
    # from _read_summary (sections don't move). But if the screen scrolled
    # during detail reading, the next section may not be visible.
    # We return None and let the caller decide — if the next section was
    # in the original summary XML, the coords are still valid.
    return detail, None


# ── Public API ─────────────────────────────────────────────

async def check_account(
    device_id: str,
    username: str = "",
    include_details: bool = True,
    proxy_enabled: bool = True,
    _skip_proxy_lifecycle: bool = False,
    _skip_cleanup: bool = False,
) -> dict:
    """
    Full combined check for a device:

    1. Pre-flight: device online, IG installed
    2. Proxy: start if enabled (abort if fails)
    3. Wake screen, open IG
    4. Detect account state from UI (banned/checkpoint/expired/active)
    5. If NOT active → stop, return state (no point navigating further)
    6. If active → navigate to Account Status, read sections

    Returns:
    {
        "device_id": "...",
        "username": "...",
        "success": true/false,
        "error": null or "description",
        "account_state": "active" | "banned" | "expired" | "checkpoint" | "unknown",
        "account_state_signal": "...",
        "proxy": { "start": {...}, "stop": {...} },
        "summary": { ... sections ... },
        "details": { ... section details ... },
        "navigation_steps": [...],
    }
    """
    result = {
        "device_id": device_id,
        "username": username,
        "success": False,
        "error": None,
        "account_state": "unknown",
        "account_state_signal": None,
        "proxy": None,
        "summary": {},
        "details": {},
        "navigation_steps": [],
    }

    # ── Pre-flight ──
    if not await is_device_online(device_id):
        result["error"] = "Device offline or not responding"
        return result

    if not await is_instagram_installed(device_id):
        result["error"] = "Instagram not installed on device"
        return result

    # Wake screen (skip for multi-account secondary checks — screen already active)
    if settings.wake_screen_before_check and not _skip_proxy_lifecycle:
        await wake_and_unlock(device_id)
        await asyncio.sleep(3.0)

    # ── Pre-flight: verify phone is on proxy screen (disabled by default — start_proxy handles it) ──
    if proxy_enabled and not _skip_proxy_lifecycle and settings.preflight_proxy_check:
        from shared.adb import is_proxy_foreground
        if not await is_proxy_foreground(device_id):
            logger.error(f"[pre-flight] Device {device_id} is NOT on proxy app screen — skipping (phone may need attention)")
            result["error"] = "Device not on proxy screen — phone may need manual attention"
            return result
        logger.info(f"[pre-flight] Device {device_id} confirmed on proxy screen — proceeding")

    # ── Rotate proxy IP (per-device URL) ──
    if proxy_enabled and not _skip_proxy_lifecycle:
        from status.registry import get_device_by_id as _get_dev
        _dev = _get_dev(device_id)
        if _dev and _dev.proxy_rotate_url:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=15) as _hc:
                    _rot = await _hc.get(_dev.proxy_rotate_url)
                    logger.info(f"[proxy-rotate] {device_id} → {_rot.status_code} ({_dev.proxy_rotate_url})")
                result["navigation_steps"].append(f"proxy_rotate:{_rot.status_code}")
                await asyncio.sleep(2.5)  # Wait for IP to propagate
            except Exception as _re:
                logger.warning(f"[proxy-rotate] {device_id} rotate failed: {_re}")
                result["navigation_steps"].append("proxy_rotate:failed")

    # ── Start proxy before IG check ──
    if proxy_enabled and not _skip_proxy_lifecycle:
        logger.info(f"[proxy] Starting proxy on {device_id}")
        proxy_result = await start_proxy(device_id)
        result["proxy"] = {"start": proxy_result}
        if proxy_result["success"]:
            result["navigation_steps"].append("proxy_started")
            # Go to home screen before opening IG
            await press_home(device_id)
            await asyncio.sleep(_human_delay(1.0))
        else:
            # Proxy failed — DO NOT proceed with IG check to protect the account
            logger.error(f"[proxy] Proxy failed on {device_id}: {proxy_result['error']} — aborting IG check")
            result["error"] = f"Proxy failed to start: {proxy_result['error']}"
            return result

    # ── Final VPN check before opening IG ──
    if proxy_enabled and not _skip_proxy_lifecycle:
        from shared.adb import is_vpn_active
        if not await is_vpn_active(device_id):
            logger.error(f"[proxy] VPN dropped before opening IG on {device_id} — aborting to protect account")
            result["error"] = "Proxy VPN dropped before opening Instagram — aborted for safety"
            return result
        logger.info(f"[proxy] VPN confirmed active on {device_id} — safe to open IG")

    # Open IG
    if not await _ensure_instagram_open(device_id):
        # ensure_clean_ig_state failed: most common cause is the account
        # being on a checkpoint/banned/expired screen (no tab_bar there).
        # Try to capture the real state from the current screen before
        # bailing with a generic error.
        try:
            fallback_state = await _detect_state_from_ui(device_id)
            if fallback_state and fallback_state.get("state") not in (None, "active", "unknown"):
                result["account_state"] = fallback_state["state"]
                result["account_state_signal"] = fallback_state.get("matched_signal")
                result["success"] = True  # check completed: state captured
                result["navigation_steps"].append(f"account_state:{fallback_state['state']}")
                logger.info(
                    f"[state] Captured '{fallback_state['state']}' on {device_id} "
                    f"despite IG-open failure (signal: {fallback_state.get('matched_signal')})"
                )
                return result
        except Exception as e:
            logger.debug(f"[state] Fallback state probe failed on {device_id}: {e}")
        result["error"] = "Failed to open Instagram"
        return result
    result["navigation_steps"].append("instagram_open")

    # ── Detect account state from first screen ──
    await asyncio.sleep(_human_delay(1.0))  # Let IG settle
    state_result = await _detect_state_from_ui(device_id)
    result["account_state"] = state_result["state"]
    result["account_state_signal"] = state_result["matched_signal"]

    logger.info(f"[state] Account state for {device_id}: {state_result['state']} (signal: {state_result['matched_signal']})")

    try:
        # If not active, stop here — can't navigate further
        # (inside try so finally cleanup always runs: back, home, stop proxy, force_portrait)
        if state_result["state"] != "active":
            result["success"] = True  # Check completed, just not active
            return result

        # ── Navigate to Account Status ──
        # OPT: Each nav function returns its last XML for the next step to reuse
        profile_xml = await _navigate_to_profile(device_id)
        if not profile_xml:
            result["error"] = "Failed to navigate to profile tab"
            result["success"] = True  # State was detected, nav failed
            return result
        result["navigation_steps"].append("profile")

        # ── First-account username validation ──
        # Read the currently logged-in username and compare with expected.
        # If mismatch, switch to the correct account before proceeding.
        if username:
            try:
                current_user = await read_current_ig_username(device_id)
                expected = username.strip().lstrip("@").lower()
                if current_user and current_user != expected:
                    logger.warning(
                        f"[validation] Expected @{expected} but found @{current_user} on {device_id} — switching account"
                    )
                    result["navigation_steps"].append(f"username_mismatch:{current_user}→{expected}")
                    switched = await switch_ig_account(device_id, expected)
                    if switched:
                        logger.info(f"[validation] Switched to @{expected} on {device_id}")
                        result["navigation_steps"].append("username_switched")
                        # Re-navigate to profile after switch (switch leaves us on profile already)
                        await asyncio.sleep(_human_delay(1.0))
                        profile_xml = await _navigate_to_profile(device_id)
                        if not profile_xml:
                            result["error"] = "Failed to navigate to profile after account switch"
                            result["success"] = True
                            return result
                    else:
                        logger.error(f"[validation] Could not switch to @{expected} on {device_id} — account not found")
                        result["navigation_steps"].append("username_switch_failed")
                        result["error"] = f"Account @{expected} not found on device (active: @{current_user})"
                        result["success"] = False
                        return result
                elif current_user:
                    logger.info(f"[validation] Username confirmed: @{current_user} on {device_id}")
                    result["navigation_steps"].append(f"username_confirmed:{current_user}")
                else:
                    logger.warning(f"[validation] Could not read username on {device_id} — proceeding with expected @{expected}")
                    result["navigation_steps"].append("username_read_failed")
            except Exception as e:
                logger.warning(f"[validation] Username validation error on {device_id}: {e} — proceeding anyway")
                result["navigation_steps"].append(f"username_validation_error:{e}")

        # ── Mid-check: verify still in IG after profile navigation ──
        profile_xml = await _verify_still_in_ig(device_id, profile_xml, context="after_profile")
        if not profile_xml:
            result["error"] = "Lost Instagram after profile navigation — recovery failed"
            result["success"] = True
            return result

        # ── Spinner / connectivity check ──
        profile_xml, load_error = await _wait_for_profile_load(device_id, profile_xml)
        if load_error:
            result["error"] = f"Profile did not load: {load_error}"
            result["navigation_steps"].append(f"loading_error:{load_error}")
            return result

        # ── VPN checkpoint: after profile ──
        if proxy_enabled:
            recovered = await _assert_vpn(device_id, "after_profile")
            if recovered:
                profile_xml = await _navigate_to_profile(device_id)
                if not profile_xml:
                    result["error"] = "Failed to re-navigate to profile after VPN recovery"
                    result["success"] = True
                    return result
                result["navigation_steps"].append("vpn_recovery:re_navigate_profile")

        settings_xml = await _navigate_to_settings(device_id, profile_xml=profile_xml)
        if not settings_xml:
            result["error"] = "Failed to open Settings"
            result["success"] = True
            return result
        result["navigation_steps"].append("settings")

        # ── Mid-check: verify still in IG after settings navigation ──
        settings_xml = await _verify_still_in_ig(device_id, settings_xml, context="after_settings")
        if not settings_xml:
            result["error"] = "Lost Instagram after settings navigation — recovery failed"
            result["success"] = True
            return result

        # ── VPN checkpoint: after settings (about to enter Account Status) ──
        if proxy_enabled:
            recovered = await _assert_vpn(device_id, "after_settings")
            if recovered:
                # Full re-navigation: profile → settings
                profile_xml = await _navigate_to_profile(device_id)
                if not profile_xml:
                    result["error"] = "Failed to re-navigate to profile after VPN recovery (after_settings)"
                    result["success"] = True
                    return result
                settings_xml = await _navigate_to_settings(device_id, profile_xml=profile_xml)
                if not settings_xml:
                    result["error"] = "Failed to re-navigate to settings after VPN recovery"
                    result["success"] = True
                    return result
                result["navigation_steps"].append("vpn_recovery:re_navigate_settings")

        if not await _navigate_to_account_status(device_id, settings_xml=settings_xml):
            result["error"] = "Failed to find Account Status in Settings"
            result["success"] = True
            return result
        result["navigation_steps"].append("account_status")

        # ── Mid-check: verify still in IG before reading summary ──
        midcheck_xml = await dump_ui(device_id, safe_mode=True)
        midcheck_xml = await _verify_still_in_ig(device_id, midcheck_xml, context="before_summary")
        if not midcheck_xml:
            result["error"] = "Lost Instagram before reading Account Status summary — recovery failed"
            result["success"] = True
            return result

        # Read summary
        summary = await _read_summary(device_id)
        result["summary"] = summary

        if not summary:
            logger.warning(f"[checker] Account Status summary is empty on {device_id} — no sections detected")

        # ── VPN checkpoint: before reading section details ──
        if proxy_enabled and include_details and summary:
            recovered = await _assert_vpn(device_id, "before_details")
            if recovered:
                # Full re-navigation: profile → settings → account status → re-read summary
                profile_xml = await _navigate_to_profile(device_id)
                if profile_xml:
                    settings_xml = await _navigate_to_settings(device_id, profile_xml=profile_xml)
                    if settings_xml and await _navigate_to_account_status(device_id, settings_xml=settings_xml):
                        summary = await _read_summary(device_id)
                        result["summary"] = summary
                        result["navigation_steps"].append("vpn_recovery:re_navigate_account_status")
                    else:
                        result["error"] = "Failed to re-navigate to Account Status after VPN recovery"
                        result["success"] = True
                        return result
                else:
                    result["error"] = "Failed to re-navigate to profile after VPN recovery (before_details)"
                    result["success"] = True
                    return result

        # Read details for each section found in the summary (dynamic)
        if include_details and summary:
            details = {}
            _section_idx = 0

            for section_key, section_data in summary.items():
                if not section_data.get("found"):
                    continue

                # ── Bloks settle delay: after back-navigating from a previous
                # section detail, Bloks may still be re-registering touch
                # handlers even though UIAutomator already reports correct
                # bounds.  A short pause prevents taps from being swallowed. ──
                if _section_idx > 0:
                    await asyncio.sleep(_human_delay(2.0))

                _section_idx += 1

                # ── VPN checkpoint: before each section drill-down ──
                if proxy_enabled:
                    recovered = await _assert_vpn(device_id, f"before_section_{section_key}")
                    if recovered:
                        # Re-navigate: profile → settings → account status
                        _rp = await _navigate_to_profile(device_id)
                        if _rp:
                            _rs = await _navigate_to_settings(device_id, profile_xml=_rp)
                            if _rs:
                                await _navigate_to_account_status(device_id, settings_xml=_rs)
                        result["navigation_steps"].append(f"vpn_recovery:re_navigate_section_{section_key}")

                # Use label_texts from config if known, otherwise use the raw text
                from status.ui_parser import _signals
                sections_config = _signals.get("sections", {})
                section_def = sections_config.get(section_key, {})
                if isinstance(section_def, dict) and section_def.get("label_texts"):
                    label_texts = section_def["label_texts"]
                else:
                    # Dynamically discovered section — use its raw label
                    label_texts = [section_data.get("raw_text", section_data.get("label", ""))]
                    label_texts = [t for t in label_texts if t]

                if label_texts:
                    # Each section gets a fresh dump — do NOT reuse stale XML.
                    # Bloks re-renders between navigate+back cycles, which moves
                    # element positions and can remove clickable ancestors.
                    detail, _ = await _read_section_detail(
                        device_id, section_key, label_texts,
                        main_page_xml=None,
                    )
                    details[section_key] = detail
                    result["summary"][section_key]["status"] = detail.get("status", "unknown")

            result["details"] = details

        result["success"] = True

    except ProxyDroppedError as e:
        logger.error(f"[checker] PROXY DROPPED on {device_id}: {e} — aborting check")
        result["error"] = str(e)
        result["success"] = True  # Check was running fine, proxy failed externally

    except Exception as e:
        logger.error(f"[checker] Unexpected error on {device_id}: {e}")
        result["error"] = f"Unexpected error: {str(e)}"

    finally:
        if _skip_cleanup:
            # Multi-account mode: navigate back to profile and STAY in IG.
            # Do NOT press HOME — the orchestrator needs IG open on the
            # profile screen so switch_ig_account can access the dropdown.
            reached_profile = False
            try:
                MAX_BACKS = 6
                for i in range(1, MAX_BACKS + 1):
                    await press_back(device_id)
                    await asyncio.sleep(0.4)
                    # Check after every 2 backs (2, 4, 6)
                    if i % 2 == 0:
                        xml = await dump_ui(device_id, safe_mode=True)
                        if xml and _is_instagram_xml(xml):
                            screen = detect_screen(xml)
                            if screen == "profile":
                                logger.info(f"[cleanup] Multi-account: reached profile after {i} backs on {device_id}")
                                result["navigation_steps"].append(f"multi_account:profile_after_{i}_backs")
                                reached_profile = True
                                break
                            elif screen == "home":
                                # Tap profile tab via shared helper (Pixel-aware)
                                w, h = await _get_screen(device_id)
                                px, py = profile_tab_coords(w, h)
                                logger.info(f"[cleanup] Multi-account: on home feed after {i} backs — tapping profile tab at ({px},{py}) on {device_id}")
                                await tap(device_id, px, py)
                                await asyncio.sleep(1.5)
                                result["navigation_steps"].append(f"multi_account:profile_via_tab_after_{i}_backs")
                                reached_profile = True
                                break
                            else:
                                logger.debug(f"[cleanup] Multi-account: screen={screen} after {i} backs on {device_id} — continuing")
                        elif xml and not _is_instagram_xml(xml):
                            logger.warning(f"[cleanup] Multi-account: left IG after {i} backs on {device_id}")
                            break
            except Exception as e:
                logger.warning(f"[cleanup] Multi-account back-to-profile failed on {device_id}: {e}")
            # Fallback: reopen IG and navigate to profile
            if not reached_profile:
                try:
                    logger.info(f"[cleanup] Multi-account: reopening IG to Profile on {device_id}")
                    await open_instagram_to_profile(device_id)
                    await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY + 1.0))
                    xml = await dump_ui(device_id, safe_mode=True)
                    if xml and _is_instagram_xml(xml):
                        screen = detect_screen(xml)
                        if screen == "profile":
                            result["navigation_steps"].append("multi_account:profile_via_reopen")
                            reached_profile = True
                        elif screen in ("home", "other"):
                            # Tap profile tab via shared helper (Pixel-aware)
                            w, h = await _get_screen(device_id)
                            px, py = profile_tab_coords(w, h)
                            await tap(device_id, px, py)
                            await asyncio.sleep(1.5)
                            result["navigation_steps"].append("multi_account:profile_via_reopen_tab")
                            reached_profile = True
                    if not reached_profile:
                        logger.warning(f"[cleanup] Multi-account: could not reach profile on {device_id} — proceeding anyway")
                        result["navigation_steps"].append("multi_account:profile_fallback_failed")
                except Exception as e:
                    logger.warning(f"[cleanup] Multi-account reopen failed on {device_id}: {e}")
                    result["navigation_steps"].append("multi_account:reopen_failed")
        else:
            # Navigate back to IG home, then press Android Home to minimize.
            nav_depth = len([s for s in result["navigation_steps"] if s in ("profile", "settings", "account_status")])
            back_presses = max(nav_depth, 1)
            logger.debug(f"[nav] Pressing back {back_presses} times (nav_depth={nav_depth})")
            for _ in range(back_presses):
                await press_back(device_id)
                await asyncio.sleep(_human_delay(0.5))

            # Press Android Home button — IG goes to background intact
            await press_home(device_id)
            logger.debug(f"[nav] Pressed HOME on {device_id} — IG minimized to background")

            # ── Stop proxy after IG check (shielded from cancellation) ──
            if proxy_enabled:
                await asyncio.sleep(_human_delay(1.0))
                logger.info(f"[proxy] Stopping proxy on {device_id}")
                try:
                    proxy_stop = await asyncio.shield(stop_proxy(device_id))
                except asyncio.CancelledError:
                    logger.warning(f"[proxy] Task cancelled but proxy stop was shielded on {device_id}")
                    proxy_stop = {"success": False, "state": "error", "error": "Task cancelled during proxy stop"}
                if result.get("proxy") is None:
                    result["proxy"] = {}
                result["proxy"]["stop"] = proxy_stop
                if proxy_stop["success"]:
                    result["navigation_steps"].append("proxy_stopped")
                    logger.info(f"[proxy] Proxy stopped on {device_id} — left on proxy screen")
                else:
                    logger.warning(f"[proxy] Failed to stop proxy on {device_id}: {proxy_stop['error']}")

            # Always force portrait after cleanup — proxy/ads may have left screen in landscape
            await force_portrait(device_id)

    return result 