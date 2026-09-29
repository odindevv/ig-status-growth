"""
Growth Metrics checker — navigates Instagram Professional Dashboard
to extract insights metrics (Views, Interactions, New Followers, Content Shared)
and optionally drills into Views for trending content detail.

Navigation flow:
  1. Pre-flight: verify proxy screen (if proxy_enabled)
  2. Start proxy (if proxy_enabled)
  3. Open IG → land on Profile (or navigate to Profile)
  4. Find "Professional dashboard" label → tap
  5. Wait for Professional Dashboard → find "Insights" section
  6. Parse summary metrics (Views, Interactions, New Followers, Content shared)
  7. Tap "Views" to drill into detail → extract trending content
  8. Press Back to return to dashboard
  9. Stop proxy → leave on proxy screen (if proxy_enabled)

Reuses proxy, VPN checkpoints, and ADB patterns from ig_status_api.
"""

import asyncio
import re
from datetime import datetime, timezone, timedelta
from config import growth_settings as settings
from logger import logger
from shared.adb import (
    dump_ui,
    tap,
    scroll_down,
    scroll_up,
    press_back,
    press_home,
    open_instagram,
    open_instagram_to_profile,
    profile_tab_coords,
    force_stop_instagram,
    is_instagram_foreground,
    wake_and_unlock,
    is_vpn_active,
    is_proxy_foreground,
    is_device_online,
    is_instagram_installed,
    get_screen_size,
    force_portrait,
    save_failed_dump,
    is_loading_spinner,
    test_device_connectivity,
)
from shared.ig_popup import ensure_clean_ig_state, dismiss_popup_if_present
# Reuse the account-state detector that status uses. The signals file
# is shared (account_status_signals.json) so any update applies to both.
from status.ui_parser import detect_account_state
from growth.ui_parser import (
    detect_screen,
    find_professional_dashboard,
    find_clickable_element,
    parse_insights_summary,
    parse_views_detail,
    parse_video_detail,
    parse_audience_demographics,
    detect_video_detail_screen,
    _get_all_nodes,
    _find_node_by_text,
    get_center_of_bounds,
)

import random as _random

SCROLL_DELAY = settings.scroll_delay
NAV_DELAY = settings.nav_step_delay
PAGE_LOAD_DELAY = settings.page_load_delay


def _human_delay(base: float) -> float:
    """Add ±20% random variation to a delay to mimic human timing."""
    jitter = base * 0.2
    return base + _random.uniform(-jitter, jitter)

# Reference resolution — all hardcoded fallback coords are based on 1080x2160
_REF_W, _REF_H = 1080, 2160

# Cache screen sizes per device to avoid repeated ADB calls
_screen_cache: dict[str, tuple[int, int, float]] = {}


async def _get_screen(device_id: str) -> tuple[int, int]:
    """Get screen size for device, cached with TTL."""
    import time as _time
    ttl = settings.screen_cache_ttl
    cached = _screen_cache.get(device_id)
    if cached:
        w, h, ts = cached
        if ttl <= 0 or (_time.monotonic() - ts) < ttl:
            return w, h
    w, h = await get_screen_size(device_id)
    _screen_cache[device_id] = (w, h, _time.monotonic())
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


async def _verify_still_in_ig(device_id: str, xml: str | None, context: str = "") -> str | None:
    """Mid-check verification: ensure we're still inside Instagram.

    Call this after important taps during a growth/status check.
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

    Instagram shows a loading spinner (swipe_refresh_animated_progressbar_container)
    when the profile content hasn't loaded yet (slow connection or no internet).

    Returns:
        (xml, error) — fresh XML if loaded, or (xml, error_string) if failed.
        error is None on success, or a string describing the problem.
    """
    if not is_loading_spinner(xml):
        return xml, None  # No spinner — profile loaded fine

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
    logger.warning(f"[loading] Profile did not load after 6 attempts on {device_id} — testing connectivity")
    connected = await test_device_connectivity(device_id)

    if connected:
        # Internet works but IG is just slow — give it one more try with longer wait
        logger.info(f"[loading] Internet OK on {device_id} but IG slow — one more attempt with 10s wait")
        await asyncio.sleep(10.0)
        xml = await dump_ui(device_id, safe_mode=True)
        if xml and not is_loading_spinner(xml):
            logger.info(f"[loading] Profile finally loaded on {device_id}")
            return xml, None
        return xml or "", "slow_connection"

    # No internet — try rotating proxy
    logger.warning(f"[loading] No internet on {device_id} — attempting proxy rotate")
    from growth.registry import get_device_by_id
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
        # Re-test after rotate
        connected = await test_device_connectivity(device_id)
        if connected:
            logger.info(f"[loading] Internet restored after proxy rotate on {device_id}")
            await asyncio.sleep(5.0)
            xml = await dump_ui(device_id, safe_mode=True)
            if xml and not is_loading_spinner(xml):
                return xml, None
            return xml or "", "slow_connection_after_rotate"

    return xml or "", "proxy_no_connection"


async def _recover_to_instagram(device_id: str, context: str = "") -> str | None:
    """Detect if we're NOT in Instagram and recover.
    Returns fresh XML if recovery succeeded, None if failed.

    Recovery chain (gentle → aggressive):
      1. dump_ui (maybe we're already in IG)
      2. open_to_profile (navigate to Profile — static screen, no reels)
      3. force_stop + open_to_profile (kill everything, reopen to Profile)
    """
    xml = await dump_ui(device_id, idle_escape="force_stop_cycle")
    if xml and _is_instagram_xml(xml):
        return xml

    # Not in IG — save failed dump for diagnosis, then recover
    ctx = f" during {context}" if context else ""
    logger.warning(f"[recovery] Not in Instagram{ctx} on {device_id} — recovering (xml_len={len(xml) if xml else 0})")
    if xml:
        save_failed_dump(device_id, xml, f"recovery_{context or 'unknown'}")
    await press_home(device_id)
    await asyncio.sleep(0.5)

    # Open to Profile — avoids home feed reels that block UIAutomator
    if await open_instagram_to_profile(device_id):
        await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY + 1.0))
        xml = await dump_ui(device_id, idle_escape="force_stop_cycle")
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
    await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY + 1.5))
    xml = await dump_ui(device_id)
    if xml and _is_instagram_xml(xml):
        logger.info(f"[recovery] Back in Instagram via force_stop + profile on {device_id}")
        return xml
    if xml:
        save_failed_dump(device_id, xml, f"recovery3_{context or 'unknown'}")

    logger.error(f"[recovery] Could not recover to Instagram on {device_id}")
    return None


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
    # when it doesn't see a tab_bar. On checkpoint/banned/expired screens
    # there IS no tab_bar, so it would destroy the original screen and we'd
    # lose the specific signal (e.g. checkpoint becomes login → expired).
    # Probe the screen first; if state is problematic, bail out NOW so the
    # caller's _check_account_state_or_abort sees the real screen.
    try:
        early_xml = await dump_ui(device_id, safe_mode=True)
        if early_xml:
            sr = detect_account_state(early_xml)
            early_state = sr.get("state") if isinstance(sr, dict) else sr
            if early_state in ("checkpoint", "banned", "expired"):
                logger.warning(
                    f"[ensure_ig_open] Account state '{early_state}' detected "
                    f"early on {device_id} — skipping ensure_clean_ig_state to "
                    f"preserve the screen for the abort flow"
                )
                return False
    except Exception as e:
        logger.debug(f"[ensure_ig_open] Early state probe failed on {device_id}: {e}")

    # ── Verify IG is in a clean state (no popups blocking) ──
    return await ensure_clean_ig_state(device_id, delay_fn=_human_delay)


# ── Account state guard (manual review trigger) ──────────────

async def _check_account_state_or_abort(
    device_id: str,
    username: str,
    profile_xml: str | None,
    proxy_enabled: bool,
    result: dict,
    nav: list,
) -> dict | None:
    """Check if IG is on a checkpoint / banned / expired screen.

    Reuses status.ui_parser.detect_account_state, which reads
    account_status_signals.json (shared with the status service).

    If the account is in a manual-review state:
      - Sets result['error'] = 'account_state:<state>:@<username>'
      - Performs full cleanup (back x5, HOME, stop proxy, force portrait)
      - Returns the populated `result` dict. Caller should `return` it.

    If the account looks active (or state is unknown — too ambiguous to
    abort), returns None and the caller continues with the normal flow.
    """
    xml = profile_xml
    if not xml:
        xml = await dump_ui(device_id, safe_mode=True)
    if not xml:
        return None  # no data to inspect — defer to the regular error handling

    try:
        state_result = detect_account_state(xml)
    except Exception as e:
        logger.warning(f"[growth] detect_account_state failed on {device_id}: {e}")
        return None

    # detect_account_state returns {"state": "...", "matched_signal": ..., "confidence": ...}
    # — unwrap defensively to a plain string.
    if isinstance(state_result, dict):
        state = state_result.get("state", "unknown")
    else:
        state = state_result or "unknown"

    if state not in ("checkpoint", "banned", "expired"):
        return None  # active or unknown — keep going

    logger.warning(
        f"[growth] Account state '{state}' detected for @{username} on {device_id} "
        f"— aborting check and flagging device for manual review"
    )
    nav.append(f"account_state:{state}")
    result["success"] = False
    result["error"] = f"account_state:{state}:@{username}"

    # No local cleanup here — check_growth's `finally` block performs the
    # full cleanup (back/HOME, stop proxy, force portrait) and leaves the
    # device on the proxy app screen for the next run. Doing cleanup here
    # too would short-circuit stop_proxy (VPN already down → no open of
    # the proxy app), leaving the phone in the launcher instead.
    return result


# ── Exceptions ────────────────────────────────────────────────

class ProxyDroppedError(Exception):
    """Raised when VPN/proxy drops during a check — abort immediately."""
    pass


# ── VPN Safety ────────────────────────────────────────────────

async def _assert_vpn(device_id: str, stage: str) -> bool:
    """Check VPN is still active.  If dropped, attempt recovery.

    Recovery flow:
      1. Force-stop IG (prevent un-proxied traffic)
      2. Press HOME
      3. Re-start proxy via start_proxy()
      4. Verify VPN tunnel is back (with one 2 s retry)
      5. Re-open Instagram

    Returns False if VPN was fine (no recovery needed).
    Returns True  if VPN dropped but was recovered — caller should
                  re-navigate because IG was restarted.
    Raises ProxyDroppedError only if recovery fails.
    """
    if await is_vpn_active(device_id):
        return False  # all good, no recovery

    # ── VPN is down — try to recover ──
    logger.warning(f"[proxy] VPN DROPPED during '{stage}' on {device_id} — attempting recovery")
    await force_stop_instagram(device_id)
    await press_home(device_id)

    try:
        from growth.proxy import start_proxy
        proxy_result = await start_proxy(device_id)
        if not proxy_result["success"]:
            logger.error(f"[proxy] Recovery failed — proxy start error: {proxy_result.get('error')} on {device_id}")
            raise ProxyDroppedError(f"Proxy VPN dropped during {stage} — recovery failed (proxy start)")

        # Verify VPN is back (with one retry after 2 s)
        if not await is_vpn_active(device_id):
            await asyncio.sleep(2.0)
            if not await is_vpn_active(device_id):
                logger.error(f"[proxy] VPN still not active after proxy restart on {device_id}")
                raise ProxyDroppedError(f"Proxy VPN dropped during {stage} — recovery failed (vpn still down)")

        logger.info(f"[proxy] VPN recovered on {device_id} during '{stage}'")

        # Re-open Instagram so caller can continue
        await press_home(device_id)
        await asyncio.sleep(1.0)
        if not await _ensure_instagram_open(device_id):
            logger.error(f"[proxy] Could not reopen IG after VPN recovery on {device_id}")
            raise ProxyDroppedError(f"Proxy VPN dropped during {stage} — recovery failed (IG reopen)")

        await asyncio.sleep(2.0)  # let IG settle
        return True  # recovered — caller should re-navigate

    except ProxyDroppedError:
        raise  # already formatted
    except Exception as e:
        logger.error(f"[proxy] Unexpected error during VPN recovery on {device_id}: {e}", exc_info=True)
        raise ProxyDroppedError(f"Proxy VPN dropped during {stage} — recovery error: {e}")


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
    Looks for the action_bar_title or username text at the top of the profile tab.
    Returns lowercase username or None if not found.
    """
    xml = await dump_ui(device_id, idle_escape="force_stop_cycle")
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
                        xml = await dump_ui(device_id, idle_escape="force_stop_cycle")
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

    # Tier 2: content-desc on profile tab that shows the username
    for n in nodes:
        cd = (n.get("content_desc") or "").lower()
        if "profile" in cd and "@" in cd:
            # content-desc like "Profile for @username"
            import re as _re
            m = _re.search(r"@([a-z0-9._]+)", cd)
            if m:
                logger.info(f"[multi-account] Current IG username: {m.group(1)} (via content-desc)")
                return m.group(1)

    # Tier 3: look for text that matches a known username pattern in the top 20% of screen
    # Only works if we're actually on the profile screen
    w, h = await _get_screen(device_id)
    top_zone = int(h * 0.20)
    for n in nodes:
        text = (n.get("text") or "").strip()
        bounds = n.get("bounds", "")
        if not text or not bounds:
            continue
        # Parse y coordinate from bounds "[x1,y1][x2,y2]"
        import re as _re
        bm = _re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not bm:
            continue
        y1 = int(bm.group(2))
        if y1 < top_zone and 3 <= len(text) <= 30 and " " not in text:
            candidate = text.lower()
            # Skip common UI words that aren't usernames
            if candidate in _COMMON_WORDS:
                continue
            # Must look like a username — lowercase alphanumeric + dots + underscores
            if _re.match(r"^[a-z0-9._]+$", candidate):
                logger.info(f"[multi-account] Possible IG username: {candidate} (via top-zone text)")
                return candidate

    logger.warning(f"[multi-account] Could not read current IG username on {device_id}")
    return None


async def switch_ig_account(device_id: str, target_username: str) -> bool:
    """
    Switch IG to a different account by tapping the username dropdown on the profile tab.

    Flow:
      1. Navigate to profile tab
      2. Read current username → if already correct, return True
      3. Tap username in action bar to open account switcher dropdown
      4. Find target username in the dropdown list
      5. Tap it → wait for IG to reload
      6. Verify the switch by reading username again

    Returns True if switch was successful (or already on correct account).
    """
    target = target_username.strip().lstrip("@").lower()
    logger.info(f"[multi-account] Switching to @{target} on {device_id}")

    # ── Step 1: Check if IG is already on profile BEFORE disrupting state ──
    # This avoids the blind tap in open_instagram_to_profile which can hit
    # profile thumbnails and open a post/reel (causing idle-state failures).
    already_on_profile = False
    xml = None

    if await is_instagram_foreground(device_id):
        xml = await dump_ui(device_id, idle_escape="force_stop_cycle")
        if xml and _is_instagram_xml(xml):
            screen = detect_screen(xml)
            if screen == "profile":
                already_on_profile = True
                profile_xml = xml
                logger.info(f"[multi-account] IG already on profile — skipping reopen on {device_id}")
            else:
                # Also check fallback text hints
                hints = _find_node_by_text(_get_all_nodes(xml), [
                    "Edit profile", "Editar perfil", "Share profile", "Compartir perfil",
                    "Professional dashboard", "Panel profesional",
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
        xml = await dump_ui(device_id, idle_escape="force_stop_cycle")
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
            hints = _find_node_by_text(_get_all_nodes(xml), [
                "Edit profile", "Editar perfil", "Share profile", "Compartir perfil",
                "Professional dashboard", "Panel profesional",
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

    # Check current username (with retry — profile may still be loading)
    current = await read_current_ig_username(device_id)
    if not current:
        for _cur_retry in range(3):
            logger.info(f"[multi-account] Username read returned None on {device_id}, retry {_cur_retry + 1}/3 (waiting 2s)")
            await asyncio.sleep(2.0)
            current = await read_current_ig_username(device_id)
            if current:
                break
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
        profile_xml = await dump_ui(device_id)
        if not profile_xml:
            profile_xml = ""
        nodes = _get_all_nodes(profile_xml)

        # NOTE: _get_all_nodes returns keys with underscores (resource_id)
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

        # Retry: wait and re-dump in case profile wasn't fully loaded
        logger.info(f"[multi-account] Switcher element not found (attempt {attempt + 1}), waiting 2s to retry")
        await asyncio.sleep(2.0)

    # Fallback: tap the top-left area where username typically is
    if not switcher_tapped:
        w, h = await _get_screen(device_id)
        fallback_x = int(w * 0.15)
        fallback_y = int(h * 0.045)
        logger.info(f"[multi-account] Username element not found after retries, tapping fallback ({fallback_x}, {fallback_y})")
        await tap(device_id, fallback_x, fallback_y)

    # Wait for dropdown to appear
    await asyncio.sleep(_human_delay(1.5))

    # Dump UI and find target username in the dropdown
    xml = await dump_ui(device_id)
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
        # Also check content_desc (_get_all_nodes uses underscores)
        cd = (n.get("content_desc") or "").strip().lstrip("@").lower()
        if cd == target and n.get("bounds"):
            target_node = n
            break

    if not target_node:
        logger.error(f"[multi-account] Username @{target} not found in dropdown on {device_id}")
        # Press back to close the dropdown
        await press_back(device_id)
        await asyncio.sleep(0.5)
        return False

    # Tap the target account
    coords = get_center_of_bounds(target_node["bounds"])
    if not coords:
        logger.error(f"[multi-account] Could not get bounds for @{target} on {device_id}")
        await press_back(device_id)
        await asyncio.sleep(0.5)
        return False

    await tap(device_id, coords[0], coords[1])
    logger.info(f"[multi-account] Tapped @{target} in dropdown on {device_id}")

    # Wait for IG to switch accounts (this involves a reload)
    await asyncio.sleep(_human_delay(3.5))

    # ── Dismiss any popup that appears after account switch ──
    # Instagram often shows "Save your login info?" after switching.
    from shared.ig_popup import find_dismiss_button, has_tab_bar
    for _popup_try in range(3):
        xml_check = await dump_ui(device_id, safe_mode=True)
        if not xml_check:
            break
        if has_tab_bar(xml_check):
            break  # No popup blocking — proceed to verify
        btn = find_dismiss_button(xml_check)
        if btn:
            (bx, by), label = btn
            logger.info(f"[multi-account] Dismissing post-switch popup '{label}' at ({bx},{by}) on {device_id}")
            await tap(device_id, bx, by, jitter=3)
            await asyncio.sleep(_human_delay(2.0))
        else:
            logger.debug(f"[multi-account] No tab bar and no dismiss button — pressing back on {device_id}")
            await press_back(device_id)
            await asyncio.sleep(_human_delay(1.5))

    # Verify switch by reading username again
    verify = await read_current_ig_username(device_id)
    if verify and verify == target:
        logger.info(f"[multi-account] Successfully switched to @{target} on {device_id}")
        return True

    logger.warning(f"[multi-account] Switch verification failed on {device_id}: expected @{target}, got @{verify}")
    # Retry verification once more after extra wait
    await asyncio.sleep(2.0)
    verify2 = await read_current_ig_username(device_id)
    if verify2 and verify2 == target:
        logger.info(f"[multi-account] Switch to @{target} confirmed on retry")
        return True

    logger.error(f"[multi-account] Failed to switch to @{target} on {device_id} (current: @{verify2})")
    return False


# ── Navigation Helpers ────────────────────────────────────────


def _find_profile_tab_in_navbar(nodes: list[dict], min_y: int) -> tuple[int, int] | None:
    """
    Find the "Profile" / "Perfil" tab but ONLY in the bottom navigation bar zone.

    This prevents false matches on Reel captions, post text, or user bios
    that contain the word "profile".  The bottom nav bar on IG sits in
    roughly the last 10-12% of the screen.

    Args:
        nodes: parsed UI nodes
        min_y: minimum Y coordinate — only nodes below this are considered

    Returns:
        (x, y) center of the matching nav-bar node, or None
    """
    profile_patterns = ["profile", "perfil"]
    for node in nodes:
        text_lower = (node["text"] or "").lower()
        desc_lower = (node["content_desc"] or "").lower()

        # Must match profile text
        if not any(p == text_lower or p == desc_lower for p in profile_patterns):
            continue

        # Must be in the nav bar zone (bottom of screen)
        coords = get_center_of_bounds(node["bounds"])
        if coords and coords[1] >= min_y:
            return coords

    # Broader match — "contains" instead of exact, but still zone-restricted
    for node in nodes:
        text_lower = (node["text"] or "").lower()
        desc_lower = (node["content_desc"] or "").lower()

        if not any(p in text_lower or p in desc_lower for p in profile_patterns):
            continue

        coords = get_center_of_bounds(node["bounds"])
        if coords and coords[1] >= min_y:
            logger.debug(f"[nav] Profile tab broad match in navbar: text='{node['text']}' desc='{node['content_desc']}' at {coords}")
            return coords

    return None

async def _xml_after_profile_popup_check(device_id: str, xml: str) -> str:
    """Defensive popup check for successful profile-navigation exits.

    Wraps ``dismiss_popup_if_present``: if a popup like "Save your login info?"
    is overlaying the profile screen, dismiss it and re-dump fresh XML so the
    caller's next step (e.g., finding "Professional dashboard") sees the real
    profile, not the popup.

    Best-effort: any failure is non-fatal and returns the original XML so the
    caller behaves exactly like before this defense existed.
    """
    try:
        dismissed = await dismiss_popup_if_present(device_id, xml)
        if not dismissed:
            return xml
        await asyncio.sleep(0.5)
        new_xml = await dump_ui(device_id, safe_mode=True)
        if not new_xml:
            return xml
        # If after dismiss we're no longer on profile, still return the new XML —
        # caller has its own validation (text hints, screen detector) and will
        # handle the navigation failure path that already exists today.
        return new_xml
    except Exception as e:
        logger.debug(f"[nav] Popup check after profile failed (non-fatal) on {device_id}: {e}")
        return xml


async def _navigate_to_profile(device_id: str) -> str | None:
    """
    Navigate to the profile tab.
    Uses 3-tier detection: resource-id → text match → scaled coordinate fallback.

    IMPORTANT: Unlike ig_status_api, we ALWAYS dump fresh XML after tapping profile.
    The status API can return stale XML because its next step (find Options button)
    works from any screen.  But our next step needs "Professional dashboard" which
    ONLY exists on the profile screen — stale XML would miss it.

    Returns fresh profile XML or None on failure.
    """
    from growth.ui_parser import _find_node_by_resource_id

    xml = await dump_ui(device_id, idle_escape="force_stop_cycle")
    if not xml:
        return None

    # ── Guard: verify we're actually in Instagram ──
    if not _is_instagram_xml(xml):
        logger.warning(f"[nav] Not in Instagram on {device_id} (xml_len={len(xml)}) — recovering")
        xml = await _recover_to_instagram(device_id, context="navigate_to_profile")
        if not xml:
            return None

    screen = detect_screen(xml)
    logger.info(f"[nav] Current screen: {screen} on {device_id}")

    if screen == "profile":
        return await _xml_after_profile_popup_check(device_id, xml)

    # Get screen size for validation
    w, h = await _get_screen(device_id)

    # ── Tier 1: Find profile_tab by resource-id in the bottom nav bar ──
    # IMPORTANT: "profile_tab" partial-matches profile_tab_layout, profile_tab_icon_view, etc.
    # which are in the MID-screen area (profile tabs for Grid/Reels/Tagged).
    # We ONLY want the bottom navbar profile_tab, so validate Y coordinate.
    nodes = _get_all_nodes(xml)
    nav_bar_min_y = int(h * 0.88)  # Nav bar is in the bottom ~12% of the screen
    coords = None

    for n in nodes:
        rid = n.get("resource_id", "")
        if "com.instagram.android:id/profile_tab" == rid and n.get("bounds"):
            c = get_center_of_bounds(n["bounds"])
            if c and c[1] >= nav_bar_min_y:
                coords = c
                logger.info(f"[nav] Found profile_tab by resource-id at {coords} (in navbar zone)")
                break
    if not coords:
        # ── Tier 2: Find by text/content_desc ──
        # IMPORTANT: Only accept matches in the bottom nav bar zone (last ~10% of screen).
        # "Profile"/"Perfil" can appear in Reel captions, post text, etc.
        # If we tap those, we open a Reel instead of going to profile.
        coords = _find_profile_tab_in_navbar(nodes, nav_bar_min_y)
        if coords:
            logger.info(f"[nav] Found profile tab by text in nav bar zone at {coords}")

    if not coords:
        # ── Tier 3: Scaled coordinate fallback ──
        # Uses the shared helper which handles Pixel edge-to-edge layouts
        # (where the profile_tab is ~51px lower than the linear scale would
        # predict). All blind taps to the profile tab go through this.
        coords = profile_tab_coords(w, h)
        logger.info(f"[nav] Profile tab not found in XML, using scaled fallback {coords} (screen {w}x{h})")

    await tap(device_id, coords[0], coords[1])
    await asyncio.sleep(NAV_DELAY + 1.0)  # Extra wait for profile to load

    # ── ALWAYS dump fresh XML — we need the actual profile screen content ──
    # (caller needs to find "Professional dashboard" which only exists on profile)
    xml = await dump_ui(device_id)
    if not xml:
        return None

    # ── Guard: verify we're still in Instagram after tap ──
    if not _is_instagram_xml(xml):
        logger.warning(f"[nav] Left Instagram after profile tap on {device_id} — recovering")
        xml = await _recover_to_instagram(device_id, context="after_profile_tap")
        if not xml:
            return None
        # After recovery, try navigating to profile again (one retry)
        screen = detect_screen(xml)
        if screen != "profile":
            nodes2 = _get_all_nodes(xml)
            for n in nodes2:
                rid = n.get("resource_id", "")
                if "com.instagram.android:id/profile_tab" == rid and n.get("bounds"):
                    c = get_center_of_bounds(n["bounds"])
                    if c and c[1] >= nav_bar_min_y:
                        await tap(device_id, c[0], c[1])
                        await asyncio.sleep(NAV_DELAY + 1.0)
                        xml = await dump_ui(device_id)
                        break

    if not xml:
        return None

    screen = detect_screen(xml)
    if screen == "profile":
        logger.info("[nav] Profile screen reached successfully")
        return await _xml_after_profile_popup_check(device_id, xml)

    # Fallback text match for profile (some IG versions)
    if screen != "home":
        has_profile_hints = _find_node_by_text(_get_all_nodes(xml), [
            "Edit profile", "Editar perfil", "Share profile", "Compartir perfil",
            "Posts", "Followers", "Following", "Publicaciones", "Seguidores", "Siguiendo",
            "Professional dashboard", "Your dashboard", "Panel profesional",
        ])
        if has_profile_hints:
            logger.info("[nav] Profile screen detected via fallback text match")
            return await _xml_after_profile_popup_check(device_id, xml)

    # ── "other" screen — only proceed if XML is actually from IG ──
    if screen == "other" and _is_instagram_xml(xml):
        logger.info("[nav] Screen is 'other' but in IG — proceeding optimistically")
        return await _xml_after_profile_popup_check(device_id, xml)

    # ── Last resort: retry once — maybe profile was still loading ──
    logger.info(f"[nav] Profile not detected (screen={screen}), waiting 3s and retrying dump")
    await asyncio.sleep(3.0)
    xml = await dump_ui(device_id)
    if not xml:
        return None

    screen = detect_screen(xml)
    if screen == "profile":
        return await _xml_after_profile_popup_check(device_id, xml)

    has_profile_hints = _find_node_by_text(_get_all_nodes(xml), [
        "Edit profile", "Editar perfil", "Share profile", "Compartir perfil",
        "Professional dashboard", "Your dashboard", "Panel profesional",
    ])
    if has_profile_hints:
        logger.info("[nav] Profile detected on retry via text match")
        return await _xml_after_profile_popup_check(device_id, xml)

    logger.warning(f"[nav] Failed to navigate to profile on {device_id} (screen={screen})")
    return None


def _is_personal_account(xml: str) -> bool:
    """Detect if this is a personal (non-business) IG account.
    Personal accounts have 'Edit profile' or 'Edit your profile' but no
    'Professional dashboard', 'Switch to professional' or 'Insights' nearby.
    They also show 'Threads' button and lack business-specific elements.
    """
    nodes = _get_all_nodes(xml)
    texts_lower = set()
    for n in nodes:
        t = (n.get("text", "") or "").strip().lower()
        d = (n.get("content_desc", "") or "").strip().lower()
        if t:
            texts_lower.add(t)
        if d:
            texts_lower.add(d)

    has_edit_profile = any("edit profile" in t for t in texts_lower)
    has_professional = any(
        kw in t
        for t in texts_lower
        for kw in ("professional dashboard", "professional", "insights", "switch to professional")
    )

    # Personal account: has Edit profile but no professional indicators
    if has_edit_profile and not has_professional:
        return True
    return False


async def _find_and_tap_professional_dashboard(device_id: str, profile_xml: str) -> str | None:
    """
    Find "Professional dashboard" on profile and tap it.
    The button is always visible in the profile header (never below fold).
    If not found on profile, falls back to hamburger menu → Insights.
    Returns XML of the Professional Dashboard/Insights screen, or None.
    Returns early if the account is detected as personal (non-business).
    """
    xml = profile_xml
    detected_personal = _is_personal_account(xml)

    # Phase 1: Check current screen (no extra dump)
    coords = find_professional_dashboard(xml)
    if coords:
        logger.info(f"[nav] Found 'Professional dashboard' at {coords} on {device_id}")
        await tap(device_id, coords[0], coords[1])
        await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY + 1.0))
        xml = await dump_ui(device_id)
        if xml:
            # Mid-check: verify we're still in IG after tapping dashboard
            xml = await _verify_still_in_ig(device_id, xml, context="dashboard_tap")
            if not xml:
                return None
            screen = detect_screen(xml)
            if screen in ("professional_dashboard", "insights"):
                return xml
            logger.debug(f"[nav] After tapping dashboard, screen={screen}")

            # Pixel / Bloks layout: dashboard loaded but no tab_bar so
            # detect_screen returns "other".  Check action_bar_title or
            # known Insights texts to confirm we're actually on the dashboard.
            # Retry up to 3 times total — the dashboard may still be loading
            # (especially on physical devices over VPN/proxy).
            if screen == "other":
                for _retry in range(3):
                    nodes = _get_all_nodes(xml)
                    ab_title = _find_node_by_text(nodes, [
                        "Professional dashboard", "Professional Dashboard",
                        "Panel profesional",
                    ])
                    insights_hints = _find_node_by_text(nodes, [
                        "Views", "Interactions", "New followers",
                        "Content you shared",
                    ])
                    if ab_title or insights_hints:
                        logger.info(
                            f"[nav] Dashboard detected via Bloks layout "
                            f"(action_bar={'yes' if ab_title else 'no'}, "
                            f"insights_text={'yes' if insights_hints else 'no'}) "
                            f"on {device_id}"
                        )
                        return xml

                    # Also check if detect_screen now sees it properly
                    detected = detect_screen(xml)
                    if detected in ("professional_dashboard", "insights"):
                        logger.info(f"[nav] Dashboard loaded on retry {_retry + 1} on {device_id}")
                        return xml

                    if _retry < 2:
                        logger.debug(f"[nav] Dashboard not loaded yet (retry {_retry + 1}/3), waiting 3s on {device_id}")
                        await asyncio.sleep(3.0)
                        xml = await dump_ui(device_id)
                        if not xml:
                            break

    # Phase 2: Fallback — open hamburger menu and look for "Insights" label.
    # The dashboard button is always in the profile header (never below fold),
    # so scrolling the profile is pointless — if not found, try the menu.
    logger.info(f"[nav] 'Professional dashboard' not on profile — trying menu → Insights on {device_id}")

    # Tap the hamburger/Options icon (top-right of profile)
    xml = await dump_ui(device_id)
    menu_coords = None
    if xml:
        menu_coords = find_clickable_element(xml, ["Options", "Opciones"])
    if not menu_coords:
        w, h = await _get_screen(device_id)
        # Scaled fallback: Options icon at ~(992, 143) on 1080×2160
        menu_coords = (int(992 * w / 1080), int(143 * h / 2160))
        logger.info(f"[nav] Menu icon not found in XML, using scaled fallback {menu_coords}")

    # Tap menu with retry — scrcpy UHID taps can silently fail
    menu_opened = False
    for _menu_attempt in range(3):
        await tap(device_id, menu_coords[0], menu_coords[1])
        # IG side drawer + menu items can take 10-12s to fully render under
        # slow proxy/VPN conditions. PAGE_LOAD_DELAY (~3s) was too short and
        # caused false 'Personal account detected' when Insights hadn't
        # rendered yet in the menu drawer (see kcmelons/georgia1am cases).
        await asyncio.sleep(_human_delay(15.0))
        xml = await dump_ui(device_id)
        if xml:
            screen_after = detect_screen(xml)
            if screen_after != "profile":
                menu_opened = True
                break
            # Still on profile — tap didn't register
            if _menu_attempt < 2:
                logger.info(f"[nav] Menu tap didn't change screen (attempt {_menu_attempt + 1}/3), retrying on {device_id}")
        else:
            break  # dump failed, don't retry

    # Look for "Insights" in the menu
    if menu_opened and xml:
        insights_coords = find_clickable_element(xml, ["Insights", "Estadísticas"])
        if insights_coords:
            logger.info(f"[nav] Found 'Insights' in menu at {insights_coords} on {device_id}")
            await tap(device_id, insights_coords[0], insights_coords[1])
            await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY + 1.0))
            xml = await dump_ui(device_id)
            if xml and detect_screen(xml) in ("professional_dashboard", "insights"):
                return xml
            # Screen might not be detected perfectly — check for known insights texts
            if xml:
                nodes = _get_all_nodes(xml)
                insights_hints = _find_node_by_text(nodes, [
                    "Views", "Interactions", "New followers", "Content you shared",
                    "Accounts reached", "Cuentas alcanzadas",
                ])
                if insights_hints:
                    logger.info(f"[nav] Insights screen confirmed via text hints on {device_id}")
                    return xml

    if detected_personal:
        logger.warning(f"[nav] Personal account detected on {device_id} — no Professional Dashboard or Insights available")
    else:
        logger.warning(f"[nav] Could not find 'Professional dashboard' or 'Insights' on {device_id}")
    return None


async def _navigate_to_insights(device_id: str, dashboard_xml: str) -> str | None:
    """
    From Professional Dashboard, navigate to the Insights section.
    The dashboard may already show Insights directly, or we may need
    to tap an "Insights" entry.
    Returns XML of the Insights screen, or None.
    """
    # Check if we're already on the Insights screen
    screen = detect_screen(dashboard_xml)
    if screen == "insights":
        return dashboard_xml

    # Look for "Insights" or "See all insights" to tap
    xml = dashboard_xml
    insights_patterns = [
        "Insights", "Estadísticas",
        "See all insights", "Ver todas las estadísticas",
        "See all", "Ver todo",
    ]
    insights_coords = find_clickable_element(xml, insights_patterns)
    if insights_coords:
        logger.info(f"[nav] Tapping Insights at {insights_coords}")
        await tap(device_id, insights_coords[0], insights_coords[1])
        await asyncio.sleep(PAGE_LOAD_DELAY + 0.5)
        xml = await dump_ui(device_id)
        if xml:
            scr = detect_screen(xml)
            if scr == "insights":
                return xml
            # Model B: after tapping Insights, we land on Overview tab
            # detect_screen may not fully recognize it yet — check for "Overview" text
            _nodes = _get_all_nodes(xml)
            _texts = " ".join((n["text"] or "").lower() for n in _nodes)
            if "overview" in _texts or "all content" in _texts:
                logger.info("[nav] Model B Insights screen detected via Overview/All content text")
                return xml

    # Scroll down to find it
    for attempt in range(5):
        await scroll_down(device_id, amount=0.3)
        await asyncio.sleep(_human_delay(SCROLL_DELAY))
        xml = await dump_ui(device_id)
        if not xml:
            continue
        if detect_screen(xml) == "insights":
            return xml
        insights_coords = find_clickable_element(xml, insights_patterns)
        if insights_coords:
            await tap(device_id, insights_coords[0], insights_coords[1])
            await asyncio.sleep(PAGE_LOAD_DELAY + 0.5)
            xml = await dump_ui(device_id)
            if xml and detect_screen(xml) == "insights":
                return xml

    # The dashboard might show metrics directly without a separate Insights page
    # Try parsing from what we have
    logger.info(f"[nav] Could not navigate to separate Insights screen — trying to parse dashboard directly")
    return dashboard_xml


async def _extract_views_detail(
    device_id: str,
    insights_xml: str,
    include_video_details: bool = False,
    nav: list[str] | None = None,
    username: str = "",
) -> dict:
    """
    Tap into the Views row on the Insights screen to get detailed breakdown.
    Returns dict with:
      - "items": list of content items with view counts + content_id
      - "accounts_reached": int or None
      - "views_extended": dict with followers_pct, non_followers_pct, content_type, profile_visits, etc.
      - "screenshot": filename of top content screenshot (or None)
      - "video_details": list of individual video detail dicts (if include_video_details=True)
    """
    from growth.ui_parser import parse_accounts_reached, parse_views_extended

    result = {"items": [], "accounts_reached": None, "views_extended": {}}

    # Find "Views" / "Vistas" to tap
    coords = find_clickable_element(insights_xml, ["Views", "Vistas"])
    if not coords:
        logger.warning(f"[views] Could not find Views row to tap on {device_id}")
        return result

    logger.info(f"[views] Tapping Views at {coords} on {device_id}")
    await tap(device_id, coords[0], coords[1])
    await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY + 1.0))

    xml = await dump_ui(device_id)
    if not xml:
        logger.warning(f"[views] dump_ui failed after tapping Views on {device_id}")
        return result

    # Mid-check: verify we're still in IG after tapping Views
    xml = await _verify_still_in_ig(device_id, xml, context="views_tap")
    if not xml:
        logger.error(f"[views] Lost IG after tapping Views on {device_id} — aborting views detail")
        return result

    screen = detect_screen(xml)
    logger.debug(f"[views] After tapping Views, screen={screen}")

    # ── Parse first visible screen (top of Views detail) ──
    accounts = parse_accounts_reached(xml)
    if accounts is not None:
        result["accounts_reached"] = accounts
        logger.info(f"[views] Found accounts_reached={accounts} on Views detail screen")

    views_ext = parse_views_extended(xml)
    items = parse_views_detail(xml)

    # ── Scroll down to find more data (content type, profile activity, etc.) ──
    for scroll_attempt in range(4):
        await scroll_down(device_id, amount=0.3)
        await asyncio.sleep(_human_delay(SCROLL_DELAY))
        xml = await dump_ui(device_id)
        if not xml:
            continue

        # Keep accumulating data from each scroll position
        scroll_ext = parse_views_extended(xml)
        for k, v in scroll_ext.items():
            if v is not None and (k not in views_ext or views_ext[k] is None or views_ext[k] == {}):
                views_ext[k] = v
            elif isinstance(v, dict) and v:  # Merge content_type dict
                if k == "content_type":
                    for ck, cv in v.items():
                        if cv is not None and ck not in views_ext.get("content_type", {}):
                            views_ext.setdefault("content_type", {})[ck] = cv

        if not items:
            items = parse_views_detail(xml)

        if result["accounts_reached"] is None:
            acc = parse_accounts_reached(xml)
            if acc is not None:
                result["accounts_reached"] = acc
                logger.info(f"[views] Found accounts_reached={acc} after scroll {scroll_attempt + 1}")

    # Generate content_id for each item (type + date + views + rank)
    for item in items:
        ctype = (item.get("type", "Unknown") or "Unknown").replace(" ", "")
        cdate = (item.get("date", "") or "").replace(" ", "")
        cviews = item.get("views", 0)
        crank = item.get("rank", 0)
        item["content_id"] = f"{ctype}_{cdate}_{cviews}_r{crank}"

    result["items"] = items
    result["views_extended"] = views_ext

    # ── Optional: extract individual video details via carrusel ──
    result["video_details"] = []
    if include_video_details and items:
        _nav = nav if nav is not None else []
        # Scroll back to top first
        for _ in range(4):
            await scroll_up(device_id, amount=0.3)
            await asyncio.sleep(_human_delay(SCROLL_DELAY * 0.5))

        # The "Top content" section with video thumbnails is NOT at the very top
        # of the Views detail page — it's below the chart, followers breakdown,
        # accounts reached, and content type sections.  We must scroll DOWN
        # incrementally until we find "Reels media" Button elements.
        thumbnails_xml = None

        def _has_reels_media(xml_str: str) -> bool:
            """Check if XML contains Reels media buttons (Top Content thumbnails)."""
            for n in _get_all_nodes(xml_str):
                if ("media" in (n.get("content_desc", "") or "").lower()
                        and n.get("class", "") == "android.widget.Button"):
                    return True
            return False

        # First pass: scroll down up to 6 times looking for thumbnails
        for scroll_try in range(6):
            xml_check = await dump_ui(device_id)
            if xml_check and _has_reels_media(xml_check):
                thumbnails_xml = xml_check
                logger.info(f"[views] Found Top Content thumbnails after {scroll_try} scroll(s) on {device_id}")
                break
            await scroll_down(device_id, amount=0.25)
            await asyncio.sleep(_human_delay(SCROLL_DELAY))

        # ── FIX #3: Scroll-back recovery if Top Content was missed ──
        # If we scrolled too far past the section, scroll back up and retry
        if not thumbnails_xml:
            logger.info(f"[views] Top Content not found after 6 scrolls — scrolling back to retry on {device_id}")
            # Scroll back up to near-top
            for _ in range(5):
                await scroll_up(device_id, amount=0.3)
                await asyncio.sleep(_human_delay(SCROLL_DELAY * 0.5))
            # Second pass with smaller scroll increments
            for scroll_try in range(10):
                xml_check = await dump_ui(device_id)
                if xml_check and _has_reels_media(xml_check):
                    thumbnails_xml = xml_check
                    logger.info(f"[views] Found Top Content thumbnails on retry pass after {scroll_try} scroll(s) on {device_id}")
                    break
                await scroll_down(device_id, amount=0.15)
                await asyncio.sleep(_human_delay(SCROLL_DELAY))

        if thumbnails_xml:
            # ── FIX #4: Global timeout for video detail extraction ──
            # Wrap the entire carousel extraction in a timeout to prevent
            # getting stuck indefinitely on slow-loading screens.
            VIDEO_DETAIL_TIMEOUT = 120  # seconds
            try:
                result["video_details"] = await asyncio.wait_for(
                    _extract_video_details(
                        device_id, thumbnails_xml, _nav, max_videos=5, username=username
                    ),
                    timeout=VIDEO_DETAIL_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[views] Video detail extraction timed out after {VIDEO_DETAIL_TIMEOUT}s on {device_id}"
                )
                _nav.append(f"video_detail:global_timeout_{VIDEO_DETAIL_TIMEOUT}s")
                # Try to get back to a safe screen after timeout
                for _ in range(2):
                    await press_back(device_id)
                    await asyncio.sleep(_human_delay(NAV_DELAY))
        else:
            logger.warning(f"[views] Could not find Top Content thumbnails after scrolling on {device_id}")
            _nav.append("video_detail:top_content_not_found")

    # ── Audience demographics extraction (best-effort) ──
    # Captures Top cities (with %) and Top countries (names + rank) from
    # the Audience section of the Views detail screen. Two-phase probe:
    # first dump as-is, then if no audience found do one scroll-down and
    # retry. Returns silently with no audience when:
    #   - Account doesn't meet IG's demographics threshold ("Currently
    #     unavailable due to a lack of data")
    #   - Audience section is off-screen after scroll
    #   - Any error in dump or parsing
    # The scroll runs just before the back-to-dashboard press, so the
    # final position inside Views detail doesn't matter — press_back
    # exits the whole screen regardless.
    try:
        aud_xml = await dump_ui(device_id, safe_mode=True)
        audience = parse_audience_demographics(aud_xml) if aud_xml else None

        if not audience:
            # Phase 2: scroll once and retry.
            # 0.5 (50% of viewport ≈ 900px) is generous enough to bring the
            # full Audience section into view — header "Top cities" + 4-5
            # cities + 4-5 countries fit in ~600px below it. Overshooting
            # is safe here: the only section below Audience is Profile
            # activity, which we don't extract. The press_back after this
            # block exits Views detail entirely, so scroll position doesn't
            # affect any subsequent step.
            try:
                await scroll_down(device_id, amount=0.5)
                await asyncio.sleep(_human_delay(SCROLL_DELAY))
                aud_xml = await dump_ui(device_id, safe_mode=True)
                if aud_xml:
                    audience = parse_audience_demographics(aud_xml)
            except Exception as _aud_scroll_e:
                logger.debug(f"[audience] scroll retry failed (non-fatal): {_aud_scroll_e}")

        if audience:
            result["audience"] = audience
            _nav.append("audience:extracted")
            logger.info(
                f"[audience] device={device_id} username={username} "
                f"cities={len(audience.get('top_cities', []))} "
                f"countries={len(audience.get('top_countries', []))}"
            )
        else:
            logger.debug(f"[audience] no demographics available for {device_id} ({username})")
    except Exception as _aud_e:
        logger.debug(f"[audience] extraction failed (non-fatal) on {device_id}: {_aud_e}")

    # Go back to dashboard
    await press_back(device_id)
    await asyncio.sleep(_human_delay(NAV_DELAY))

    return result


async def _extract_interactions_detail(device_id: str, insights_xml: str) -> dict:
    """
    Tap into the Interactions row to get interaction breakdown.
    Returns dict with followers_pct, non_followers_pct, content_type (reels/posts %).
    Reuses parse_views_extended since the Interactions screen has the same layout.
    """
    from growth.ui_parser import parse_views_extended

    result = {}

    # Find "Interactions" / "Interacciones" row to tap
    coords = find_clickable_element(insights_xml, ["Interactions", "Interacciones"])
    if not coords:
        logger.warning(f"[interactions] Could not find Interactions row to tap on {device_id}")
        return result

    logger.info(f"[interactions] Tapping Interactions at {coords} on {device_id}")
    await tap(device_id, coords[0], coords[1])
    await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY + 1.0))

    xml = await dump_ui(device_id)
    if not xml:
        logger.warning(f"[interactions] dump_ui failed after tapping Interactions on {device_id}")
        return result

    # Mid-check: verify we're still in IG after tapping Interactions
    xml = await _verify_still_in_ig(device_id, xml, context="interactions_tap")
    if not xml:
        logger.error(f"[interactions] Lost IG after tapping Interactions on {device_id} — aborting")
        return result

    logger.debug(f"[interactions] After tapping Interactions, screen={detect_screen(xml)}")

    # Parse first visible screen — same layout as Views detail
    combined = parse_views_extended(xml)

    # Scroll down to capture content type breakdown (Reels %, Posts %)
    for scroll_attempt in range(3):
        await scroll_down(device_id, amount=0.35)
        await asyncio.sleep(_human_delay(SCROLL_DELAY))
        xml = await dump_ui(device_id)
        if not xml:
            continue

        scroll_data = parse_views_extended(xml)
        for k, v in scroll_data.items():
            if v is None:
                continue
            if isinstance(v, dict) and v:
                if k == "content_type":
                    for ck, cv in v.items():
                        if cv is not None and ck not in combined.get("content_type", {}):
                            combined.setdefault("content_type", {})[ck] = cv
                elif not combined.get(k):
                    combined[k] = v
            elif combined.get(k) is None:
                combined[k] = v

    # Go back to dashboard
    await press_back(device_id)
    await asyncio.sleep(_human_delay(NAV_DELAY))

    logger.info(f"[interactions] {device_id} interaction detail: followers={combined.get('followers_pct')}%, "
                f"non_followers={combined.get('non_followers_pct')}%, content={combined.get('content_type', {})}")

    return combined


# ── Video Detail Extraction (carrusel) ──────────────────────


async def _extract_video_details(
    device_id: str,
    views_detail_xml: str,
    nav: list[str],
    max_videos: int = 5,
    username: str = "",
) -> list[dict]:
    """
    Tap into first video from top content, parse detail screen, then
    navigate the horizontal carrusel to extract remaining videos.

    Flow:
      1. Find first video thumbnail on the Views detail screen → tap it
      2. Wait for detail screen → parse with parse_video_detail()
      3. Find next carrusel thumbnail ("Reels Media" buttons) → tap (4 times)
      4. Parse each subsequent video detail
      5. Press back to return to Views detail screen

    Returns list of dicts, each with title, date, duration, engagement, overview metrics.
    Also calls content lookup API for each video to get the IG URL.
    """
    from config import growth_settings as settings

    video_details = []

    # ── Step 1: Find first video thumbnail to tap ──
    # On the Views detail screen, videos appear as clickable items with
    # content_desc like "Reels Media" or class "ImageView" near the top content area.
    # Look for the first clickable Reels Media element.
    nodes = _get_all_nodes(views_detail_xml)
    first_video_coords = None

    for node in nodes:
        desc = (node.get("content_desc", "") or "").strip().lower()
        if "reels media" in desc or "reel media" in desc:
            coords = get_center_of_bounds(node.get("bounds", ""))
            if coords:
                first_video_coords = coords
                break

    # Fallback: look for any clickable ImageView in the top content area
    # IMPORTANT: exclude elements in the action bar (y < 250) to avoid
    # hitting the Back button or other nav elements.
    if not first_video_coords:
        for node in nodes:
            if (node.get("class", "") == "android.widget.ImageView"
                    and node.get("clickable")
                    and node.get("bounds", "")):
                bounds = node.get("bounds", "")
                coords = get_center_of_bounds(bounds)
                if coords and 300 < coords[1] < 1800:
                    # Also check the element is reasonably sized (thumbnail, not icon)
                    bm = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                    if bm:
                        w = int(bm.group(3)) - int(bm.group(1))
                        h = int(bm.group(4)) - int(bm.group(2))
                        if w >= 80 and h >= 80:  # thumbnail should be at least 80x80
                            first_video_coords = coords
                            break

    if not first_video_coords:
        logger.warning(f"[video_detail] No video thumbnail found on Views detail screen for {device_id}")
        nav.append("video_detail:no_thumbnail_found")
        return video_details

    # ── Tap first video (with back+retry if it doesn't load) ──
    xml = None
    for tap_attempt in range(2):
        logger.info(f"[video_detail] Tapping first video at {first_video_coords} on {device_id} (attempt {tap_attempt + 1})")
        await tap(device_id, first_video_coords[0], first_video_coords[1])
        # Video detail/insights screens load charts & stats — needs extra wait
        await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY + 4.0))

        # Try dump with retries (safe_mode avoids pressing Home)
        for dump_try in range(3):
            xml = await dump_ui(device_id, safe_mode=True)
            if xml and detect_video_detail_screen(xml):
                break
            if xml:
                logger.info(f"[video_detail] Dump {dump_try + 1}: not video detail screen yet on {device_id}")
            else:
                logger.info(f"[video_detail] Dump {dump_try + 1}: dump_ui returned None on {device_id}")
            if dump_try < 2:
                await asyncio.sleep(3.0)
                xml = None

        if xml and detect_video_detail_screen(xml):
            break  # Success — got the video detail screen

        # Didn't load → go back and retry the tap
        logger.warning(f"[video_detail] Video detail didn't load (attempt {tap_attempt + 1}) on {device_id} — going back to retry")
        await press_back(device_id)
        await asyncio.sleep(_human_delay(NAV_DELAY + 1.0))

    if not xml or not detect_video_detail_screen(xml):
        logger.warning(f"[video_detail] Failed to reach video detail screen after retries on {device_id}")
        nav.append("video_detail:detail_screen_not_reached")
        await press_back(device_id)
        await asyncio.sleep(_human_delay(NAV_DELAY))
        return video_details

    # ── Parse first video ──
    detail = parse_video_detail(xml)
    detail["position"] = 1
    video_details.append(detail)
    nav.append(f"video_detail:parsed_video_1(title={detail.get('title', '')!r})")
    logger.info(f"[video_detail] Video 1: {detail.get('title', 'N/A')} — views={detail.get('views')}")

    # ── Navigate carrusel for remaining videos ──
    # IG carousel behaviour (confirmed from real XML):
    #   - The SELECTED (current) thumbnail is rendered ~276-279px wide
    #   - Non-selected thumbnails are ~250px wide
    #   - After tapping a new thumbnail it becomes "selected" and the carousel
    #     scrolls so that it sits in the centre-left, revealing more items to the right
    # Strategy: find the LARGEST button (= current), then tap the first one
    # immediately to its RIGHT.
    entered_detail = True  # we entered at least 1 video detail screen

    for video_num in range(2, max_videos + 1):
        await asyncio.sleep(_human_delay(1.5))

        xml = await dump_ui(device_id, safe_mode=True)
        if not xml:
            logger.warning(f"[video_detail] dump_ui failed before video {video_num} on {device_id}")
            nav.append(f"video_detail:dump_failed_video_{video_num}")
            break

        # ── FIX #1: Verify we're still on an Insights-related screen ──
        if not detect_video_detail_screen(xml):
            screen = detect_screen(xml)
            if screen in ("home", "other", "unknown"):
                logger.warning(f"[video_detail] Left Insights (screen={screen}) during carousel on {device_id} — aborting")
                nav.append(f"video_detail:left_insights_screen={screen}")
                entered_detail = False  # we got kicked out, no extra back needed
                break
            # Could be Views detail or Insights — not fatal, but we can't navigate carousel
            logger.warning(f"[video_detail] Not on video detail screen (screen={screen}) for video {video_num} on {device_id}")
            nav.append(f"video_detail:wrong_screen_{video_num}={screen}")
            break

        # Collect all carousel "Reels media" buttons with their bounds
        carousel_items: list[dict] = []
        for node in _get_all_nodes(xml):
            desc = (node.get("content_desc", "") or "").strip().lower()
            if ("reels media" in desc or "reel media" in desc) and node.get("bounds"):
                bm = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
                if bm:
                    x1, y1, x2, y2 = int(bm.group(1)), int(bm.group(2)), int(bm.group(3)), int(bm.group(4))
                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                    w = x2 - x1
                    carousel_items.append({"cx": cx, "cy": cy, "w": w, "x1": x1})

        carousel_items.sort(key=lambda c: c["cx"])  # sort left→right

        if not carousel_items:
            logger.warning(f"[video_detail] No carrusel items found for video {video_num} on {device_id}")
            nav.append(f"video_detail:no_carousel_video_{video_num}")
            break

        # Find the selected (largest width) item
        selected_idx = max(range(len(carousel_items)), key=lambda i: carousel_items[i]["w"])
        sel = carousel_items[selected_idx]

        logger.debug(
            f"[video_detail] Carousel items: {[(c['cx'], c['w']) for c in carousel_items]}, "
            f"selected_idx={selected_idx} (cx={sel['cx']}, w={sel['w']})"
        )

        # The next video is the one immediately to the RIGHT of the selected
        next_idx = selected_idx + 1
        if next_idx >= len(carousel_items):
            logger.warning(f"[video_detail] No carousel item to the right of selected for video {video_num} on {device_id}")
            nav.append(f"video_detail:no_next_carousel_video_{video_num}")
            break

        nxt = carousel_items[next_idx]
        next_coords = (nxt["cx"], nxt["cy"])

        logger.info(f"[video_detail] Tapping carrusel video {video_num} at {next_coords} (w={nxt['w']}) on {device_id}")
        await tap(device_id, next_coords[0], next_coords[1])
        await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY + 3.0))

        xml = await dump_ui(device_id, safe_mode=True)
        if not xml:
            logger.warning(f"[video_detail] dump_ui failed after tapping video {video_num} on {device_id}")
            nav.append(f"video_detail:dump_failed_after_tap_{video_num}")
            continue

        if not detect_video_detail_screen(xml):
            # Check if we left IG entirely
            screen = detect_screen(xml)
            if screen in ("home", "other", "unknown"):
                logger.warning(f"[video_detail] Left Insights after tap {video_num} (screen={screen}) on {device_id} — aborting")
                nav.append(f"video_detail:left_insights_after_tap_{video_num}={screen}")
                entered_detail = False
                break
            logger.warning(f"[video_detail] Video {video_num} screen not detected (screen={screen}) on {device_id}")
            nav.append(f"video_detail:screen_not_detected_{video_num}")
            continue

        detail = parse_video_detail(xml)
        detail["position"] = video_num

        # Dedup: skip if we already parsed a video with the same views+likes+shares
        dedup_key = (detail.get("views"), detail.get("likes"), detail.get("shares"))
        already_seen = any(
            (vd.get("views"), vd.get("likes"), vd.get("shares")) == dedup_key
            for vd in video_details
        )
        if already_seen:
            logger.info(f"[video_detail] Video {video_num} is duplicate (views={dedup_key[0]}) — skipping")
            nav.append(f"video_detail:duplicate_video_{video_num}")
            continue

        video_details.append(detail)
        nav.append(f"video_detail:parsed_video_{video_num}(title={detail.get('title', '')!r})")
        logger.info(f"[video_detail] Video {video_num}: {detail.get('title', 'N/A')} — views={detail.get('views')}")

    # ── Content lookup API: resolve IG URLs by content_id ──
    # content_id is built from type+date+views in the top content parser,
    # matched to video_details by position. Build a content_id for each video.
    if video_details:
        for vd in video_details:
            # Build a stable content_id (no views — they change between checks)
            vd_date = vd.get("date", "")
            vd_pos = vd.get("position", 0)
            content_id = f"Reel_{vd_date}_r{vd_pos}_{username}" if vd_date else ""
            vd["content_id"] = content_id
            title = vd.get("title", "") or ""
            # Lookup key: "VideoTitle Reel_March 24_r1_username"
            lookup_key = f"{title} {content_id}".strip() if content_id else title
            if not lookup_key:
                continue
            try:
                ig_url = await _lookup_content_url("", lookup_key, username=username, device_id=device_id)
                if ig_url:
                    vd["ig_url"] = ig_url
                    logger.info(f"[video_detail] Resolved URL for '{lookup_key}': {ig_url}")
            except Exception as e:
                logger.warning(f"[video_detail] Content lookup failed for '{lookup_key}': {e}")

    # ── FIX #2: Smart back — press back ONCE to exit video detail ──
    # We only need to go back ONE level: from video detail → views_detail.
    # The caller (_extract_views_detail) will handle the next back to dashboard.
    # If we got kicked out (home/other), pressing back could make things worse — skip.
    if entered_detail:
        await press_back(device_id)
        await asyncio.sleep(_human_delay(NAV_DELAY))
        check_xml = await dump_ui(device_id, safe_mode=True)
        if check_xml:
            scr = detect_screen(check_xml)
            # If we're STILL on a video detail screen, press back one more time
            if detect_video_detail_screen(check_xml):
                logger.info(f"[video_detail] Still on video detail after 1st back — pressing back again on {device_id}")
                await press_back(device_id)
                await asyncio.sleep(_human_delay(NAV_DELAY))
            else:
                logger.info(f"[video_detail] Left video detail → screen={scr} after 1 back on {device_id}")

    nav.append(f"video_detail:total_parsed={len(video_details)}")
    logger.info(f"[video_detail] Extracted {len(video_details)} video details for {device_id}")

    return video_details


async def _lookup_content_url(
    base_url: str,
    title: str,
    username: str = "",
    device_id: str = "",
) -> str | None:
    """
    Look up a video title in the local content registry (SQLite).
    If not found, adds it to the pending queue.

    Returns the URL string or None if not found.
    """
    from growth.database import content_get_by_title, content_add_pending

    try:
        video = await content_get_by_title(title)
        if video:
            return video.get("url") or None

        # Not registered — add to pending queue
        logger.info(f"[content_lookup] '{title}' not registered — adding to pending")
        await content_add_pending(title, username=username, device_id=device_id)
        return None
    except Exception as e:
        logger.debug(f"[content_lookup] Lookup failed for '{title}': {e}")
        return None


# ── Main Check Function ──────────────────────────────────────

async def check_growth(
    device_id: str,
    username: str = "",
    proxy_enabled: bool = True,
    include_views_detail: bool = True,
    _skip_proxy_lifecycle: bool = False,
    _skip_cleanup: bool = False,
) -> dict:
    """
    Full growth metrics extraction for a single device.

    Flow:
      1. Pre-flight proxy check
      2. Start proxy
      3. Navigate to Profile → Professional Dashboard → Insights
      4. Extract summary metrics
      5. Drill into Views detail (optional)
      6. Cleanup: stop proxy, leave on proxy screen

    Returns dict with all metrics + navigation metadata.
    """
    result = {
        "device_id": device_id,
        "username": username,
        "success": False,
        "error": None,
        "date_range": "",
        "views": None,
        "interactions": None,
        "new_followers": None,
        "accounts_reached": None,
        "content_shared": None,
        "ui_model": "",
        "views_detail": [],
        "navigation_steps": [],
        "duration_ms": 0,
    }

    nav = result["navigation_steps"]
    # Tracks whether we successfully opened Instagram. Used in the finally
    # cleanup to decide whether to do IG-specific cleanup (press_back × 5 +
    # press_home). If proxy fails before we open IG, we should NOT do IG
    # cleanup — it would just take us off the proxy app screen to home for no
    # reason (and the misleading "left on proxy screen" log).
    ig_was_opened = False

    try:
        # ── Pre-flight: proxy screen check (disabled by default — start_proxy handles it) ──
        if proxy_enabled and not _skip_proxy_lifecycle and settings.preflight_proxy_check:
            if not await is_proxy_foreground(device_id):
                logger.error(f"[pre-flight] Device {device_id} is NOT on proxy app screen — skipping")
                result["error"] = "Device not on proxy screen — phone may need manual attention"
                return result
            nav.append("pre-flight:proxy_screen_ok")

        # ── Wake screen ──
        if settings.wake_screen_before_check and not _skip_proxy_lifecycle:
            await wake_and_unlock(device_id)
            await asyncio.sleep(3.0)
            nav.append("wake_screen")

        # ── Rotate proxy IP (per-device URL) ──
        if proxy_enabled and not _skip_proxy_lifecycle:
            from growth.registry import get_device_by_id as _get_dev
            _dev = _get_dev(device_id)
            if _dev and _dev.proxy_rotate_url:
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=15) as _hc:
                        _rot = await _hc.get(_dev.proxy_rotate_url)
                        logger.info(f"[proxy-rotate] {device_id} → {_rot.status_code} ({_dev.proxy_rotate_url})")
                    nav.append(f"proxy_rotate:{_rot.status_code}")
                    await asyncio.sleep(2.5)  # Wait for IP to propagate
                except Exception as _re:
                    logger.warning(f"[proxy-rotate] {device_id} rotate failed: {_re}")
                    nav.append("proxy_rotate:failed")

        # ── Start proxy ──
        if proxy_enabled and not _skip_proxy_lifecycle:
            from growth.proxy import start_proxy
            proxy_result = await start_proxy(device_id)
            nav.append(f"proxy_start:{proxy_result['state']}")
            if not proxy_result["success"]:
                result["error"] = f"Proxy failed: {proxy_result.get('error', 'unknown')}"
                return result
            # Go to home screen before opening IG
            await press_home(device_id)
            await asyncio.sleep(1.0)

        # ── VPN checkpoint 1 ──
        if proxy_enabled and not _skip_proxy_lifecycle:
            recovered = await _assert_vpn(device_id, "after_proxy_start")
            nav.append("vpn_check:after_proxy_start" + (":recovered" if recovered else ""))

        # ── Pre-flight: device online + IG installed ──
        if not await is_device_online(device_id):
            result["error"] = "Device is offline"
            return result
        nav.append("pre-flight:device_online")

        if not await is_instagram_installed(device_id):
            result["error"] = "Instagram not installed on device"
            return result
        nav.append("pre-flight:ig_installed")

        # ── Open IG (ensure foreground) ──
        if not await _ensure_instagram_open(device_id):
            # ensure_clean_ig_state failed: usually means no tab_bar visible.
            # Most common real-world cause: account is on a checkpoint /
            # banned / expired screen (no tab bar there). Check before
            # bailing with a generic error.
            aborted = await _check_account_state_or_abort(
                device_id, username, None, proxy_enabled, result, nav,
            )
            if aborted:
                return aborted
            result["error"] = "Failed to open Instagram"
            return result
        nav.append("open_instagram")
        ig_was_opened = True  # IG is now in foreground — finally can do full cleanup

        # ── IG settle time — replicate what status API gets via _detect_state_from_ui ──
        # Status API does: sleep(1) + dump_ui + if unknown: sleep(4) + dump_ui = ~6s total
        # We replicate this to give IG enough time to fully render the nav bar.
        await asyncio.sleep(1.0)
        settle_xml = await dump_ui(device_id)
        if settle_xml:
            # ── Guard: verify we're actually in Instagram, not camera/launcher ──
            if not _is_instagram_xml(settle_xml):
                logger.warning(f"[growth] Not in Instagram after open on {device_id} (xml_len={len(settle_xml)}) — recovering")
                nav.append("ig_settle:wrong_app_recovery")
                settle_xml = await _recover_to_instagram(device_id, context="ig_settle")
                if not settle_xml:
                    result["error"] = "Not in Instagram — recovery failed (camera/other app)"
                    return result

            settle_screen = detect_screen(settle_xml)
            nav.append(f"ig_settle:screen={settle_screen}")
            logger.debug(f"[growth] IG settle pass 1: screen={settle_screen}")
            # If IG is still loading (unknown/other), wait more — just like status API
            if settle_screen in ("unknown", "other"):
                logger.info(f"[growth] IG still loading on {device_id} — waiting 4s for retry")
                await asyncio.sleep(4.0)
                settle_xml2 = await dump_ui(device_id)
                if settle_xml2:
                    # Check again for wrong app after retry
                    if not _is_instagram_xml(settle_xml2):
                        logger.warning(f"[growth] Still not in Instagram on {device_id} after settle retry — recovering")
                        settle_xml2 = await _recover_to_instagram(device_id, context="ig_settle_retry")
                        if not settle_xml2:
                            result["error"] = "Not in Instagram — recovery failed after retry"
                            return result
                    settle_screen2 = detect_screen(settle_xml2)
                    nav.append(f"ig_settle_retry:screen={settle_screen2}")
                    logger.debug(f"[growth] IG settle pass 2: screen={settle_screen2}")
        else:
            # No XML at all — IG probably still opening, give it extra time
            await asyncio.sleep(4.0)
            nav.append("ig_settle:no_xml_extra_wait")

        profile_xml = await _navigate_to_profile(device_id)
        if not profile_xml:
            # Maybe the screen is checkpoint / banned / expired — check before
            # falling back to the generic "could not navigate" error.
            aborted = await _check_account_state_or_abort(
                device_id, username, None, proxy_enabled, result, nav,
            )
            if aborted:
                return aborted
            result["error"] = "Could not navigate to Profile screen"
            return result
        nav.append("navigate:profile")

        # ── Account state guard ──
        # If IG is showing checkpoint / banned / expired screen, abort early.
        # The whole device will be flagged for manual review by the orchestrator.
        aborted = await _check_account_state_or_abort(
            device_id, username, profile_xml, proxy_enabled, result, nav,
        )
        if aborted:
            return aborted

        # ── First-account username validation ──
        # Read the currently logged-in username and compare with expected.
        # If mismatch, switch to the correct account before proceeding.
        if username:
            try:
                expected = username.strip().lstrip("@").lower()
                current_user = await read_current_ig_username(device_id)
                # Retry if username couldn't be read — profile may still be loading
                if not current_user:
                    for _uname_retry in range(3):
                        logger.info(f"[validation] Username read returned None on {device_id}, retry {_uname_retry + 1}/3 (waiting 2s)")
                        nav.append(f"username_read_retry:{_uname_retry + 1}")
                        await asyncio.sleep(2.0)
                        current_user = await read_current_ig_username(device_id)
                        if current_user:
                            break
                if current_user and current_user != expected:
                    logger.warning(
                        f"[validation] Expected @{expected} but found @{current_user} on {device_id} — switching account"
                    )
                    nav.append(f"username_mismatch:{current_user}→{expected}")
                    switched = await switch_ig_account(device_id, expected)
                    if switched:
                        logger.info(f"[validation] Switched to @{expected} on {device_id}")
                        nav.append("username_switched")
                        # Re-navigate to profile after switch (switch leaves us on profile already)
                        # Pixel / Material You animations need more settle time than Samsung
                        await asyncio.sleep(3.0)
                        profile_xml = await _navigate_to_profile(device_id)
                        if not profile_xml:
                            result["error"] = "Failed to navigate to profile after account switch"
                            return result
                    else:
                        logger.error(f"[validation] Could not switch to @{expected} on {device_id} — account not found")
                        nav.append("username_switch_failed")
                        result["error"] = f"Account @{expected} not found on device (active: @{current_user})"
                        result["success"] = False
                        return result
                elif current_user:
                    logger.info(f"[validation] Username confirmed: @{current_user} on {device_id}")
                    nav.append(f"username_confirmed:{current_user}")
                else:
                    logger.warning(f"[validation] Could not read username on {device_id} — proceeding with expected @{expected}")
                    nav.append("username_read_failed")
            except Exception as e:
                logger.warning(f"[validation] Username validation error on {device_id}: {e} — proceeding anyway")
                nav.append(f"username_validation_error:{e}")

        # ── VPN checkpoint 2 ──
        if proxy_enabled:
            recovered = await _assert_vpn(device_id, "after_profile")
            nav.append("vpn_check:after_profile" + (":recovered" if recovered else ""))
            if recovered:
                profile_xml = await _navigate_to_profile(device_id)
                if not profile_xml:
                    result["error"] = "Failed to re-navigate to profile after VPN recovery"
                    return result
                nav.append("vpn_recovery:re_navigate_profile")

        # ── Spinner / connectivity check ──
        profile_xml, load_error = await _wait_for_profile_load(device_id, profile_xml)
        if load_error:
            result["error"] = f"Profile did not load: {load_error}"
            nav.append(f"loading_error:{load_error}")
            return result

        # ── Profile → Professional Dashboard ──
        # Check for personal account early (before wasting time scrolling)
        is_personal = _is_personal_account(profile_xml)

        dashboard_xml = await _find_and_tap_professional_dashboard(device_id, profile_xml)
        if not dashboard_xml:
            if is_personal:
                result["error"] = "Personal account — not a Business/Creator profile"
                nav.append("SKIP:personal_account")
                logger.warning(f"[growth] {device_id} is a personal account — skipping metrics extraction")
            else:
                result["error"] = "Could not find Professional Dashboard on profile"
            return result
        nav.append("navigate:professional_dashboard")

        # ── Detect UI model BEFORE navigating further ──
        # Model A: dashboard already shows all metrics — no need to tap "Insights"
        # Model B: need to tap "Insights" to reach Overview tab with cards
        from growth.ui_parser import detect_insights_model, parse_insights_model_b
        ui_model = detect_insights_model(dashboard_xml)

        if ui_model == "A":
            # Model A: Professional Dashboard screen already has the metrics rows
            # (Views, Interactions, New followers, Content you shared)
            # Do NOT tap "Insights" — it would open a drill-down we don't want
            insights_xml = dashboard_xml
            nav.append("model_a:dashboard_has_metrics")
            logger.info(f"[growth] Model A detected — using dashboard XML directly (no Insights tap)")
        else:
            # Model B: need to tap into Insights to get Overview tab
            insights_xml = await _navigate_to_insights(device_id, dashboard_xml)
            if not insights_xml:
                result["error"] = "Could not navigate to Insights"
                return result
            nav.append("navigate:insights")

        # ── VPN checkpoint 3 ──
        if proxy_enabled:
            recovered = await _assert_vpn(device_id, "before_extraction")
            nav.append("vpn_check:before_extraction" + (":recovered" if recovered else ""))
            if recovered:
                # Full re-navigation: profile → dashboard → insights
                profile_xml = await _navigate_to_profile(device_id)
                if not profile_xml:
                    result["error"] = "Failed to re-navigate to profile after VPN recovery (before_extraction)"
                    return result
                dashboard_xml = await _find_and_tap_professional_dashboard(device_id, profile_xml)
                if not dashboard_xml:
                    result["error"] = "Failed to re-navigate to dashboard after VPN recovery"
                    return result
                ui_model = detect_insights_model(dashboard_xml)
                if ui_model == "A":
                    insights_xml = dashboard_xml
                else:
                    insights_xml = await _navigate_to_insights(device_id, dashboard_xml)
                    if not insights_xml:
                        result["error"] = "Failed to re-navigate to insights after VPN recovery"
                        return result
                nav.append("vpn_recovery:re_navigate_insights")

        ui_model = detect_insights_model(insights_xml)
        result["ui_model"] = ui_model
        nav.append(f"ui_model:{ui_model}")
        logger.info(f"[growth] Detected UI model: {ui_model} on {device_id}")

        if ui_model == "B":
            # ── Model B: cards on Overview tab ──
            # Retry up to 3 times if cards aren't captured (IG may still be loading)
            from growth.ui_parser import parse_accounts_reached
            summary = None
            current_xml = insights_xml
            for card_attempt in range(3):
                summary = parse_insights_model_b(current_xml)
                has_cards = summary and any(
                    summary.get(k) is not None for k in ("views", "interactions", "new_followers")
                )
                if has_cards:
                    nav.append(f"parse:model_b_visible(attempt={card_attempt + 1})")
                    break
                # Cards not found — wait and re-dump
                logger.info(f"[growth] Model B cards not found on attempt {card_attempt + 1}, retrying...")
                await asyncio.sleep(1.5)
                current_xml = await dump_ui(device_id) or current_xml
                nav.append(f"parse:model_b_retry_{card_attempt + 1}")

            if summary:
                raw_range = summary.get("date_range", "")
                # Model B shows "30 days" instead of actual dates.
                # Generate a proper range from system date: (today - N) → today
                if raw_range and "days" in raw_range.lower():
                    import re as _re
                    days_match = _re.search(r"(\d+)", raw_range)
                    n_days = int(days_match.group(1)) if days_match else 30
                    today = datetime.now(timezone.utc)
                    start = today - timedelta(days=n_days)
                    result["date_range"] = f"{start.strftime('%b %d')} - {today.strftime('%b %d')}"
                elif raw_range:
                    result["date_range"] = raw_range
                result["views"] = summary.get("views")
                result["interactions"] = summary.get("interactions")
                result["new_followers"] = summary.get("new_followers")

            # ── Scroll down to find Accounts reached (up to 3 scrolls) ──
            for scroll_attempt in range(3):
                await scroll_down(device_id, amount=0.35)
                await asyncio.sleep(_human_delay(SCROLL_DELAY))
                scrolled_xml = await dump_ui(device_id)
                if scrolled_xml:
                    accounts = parse_accounts_reached(scrolled_xml)
                    if accounts is not None:
                        result["accounts_reached"] = accounts
                        nav.append(f"parse:accounts_reached={accounts}(scroll={scroll_attempt + 1})")
                        break
                    # Also try to pick up any cards we missed on the first pass
                    if not summary or result["views"] is None:
                        fallback = parse_insights_model_b(scrolled_xml)
                        if fallback:
                            for k in ("views", "interactions", "new_followers"):
                                if result[k] is None and fallback.get(k) is not None:
                                    result[k] = fallback[k]
                                    nav.append(f"parse:model_b_late_{k}={fallback[k]}")
                            if not result["date_range"] and fallback.get("date_range"):
                                result["date_range"] = fallback["date_range"]

            logger.info(
                f"[growth] {device_id} Model B metrics: views={result['views']}, "
                f"interactions={result['interactions']}, net_followers={result['new_followers']}, "
                f"accounts_reached={result['accounts_reached']}, range='{result['date_range']}'"
            )
        else:
            # ── Model A: traditional rows (Views, Interactions, New Followers, Content shared) ──
            summary = parse_insights_summary(insights_xml)
            if summary:
                result["date_range"] = summary.get("date_range", "")
                result["views"] = summary.get("views")
                result["interactions"] = summary.get("interactions")
                result["new_followers"] = summary.get("new_followers")
                result["content_shared"] = summary.get("content_shared")
                nav.append("parse:model_a_summary")
                logger.info(
                    f"[growth] {device_id} Model A metrics: views={result['views']}, "
                    f"interactions={result['interactions']}, followers={result['new_followers']}, "
                    f"shared={result['content_shared']}, range='{result['date_range']}'"
                )
            else:
                logger.warning(f"[growth] Failed to parse insights summary on {device_id}")
                nav.append("parse:insights_summary_failed")

            # Views detail drill-down (Model A only)
            # Also captures accounts_reached from the Views detail screen
            extended_data = {}
            if include_views_detail and insights_xml:
                if proxy_enabled:
                    recovered = await _assert_vpn(device_id, "before_views_detail")
                    if recovered:
                        # Re-navigate: profile → dashboard → insights
                        profile_xml = await _navigate_to_profile(device_id)
                        dashboard_xml = await _find_and_tap_professional_dashboard(device_id, profile_xml) if profile_xml else None
                        if dashboard_xml:
                            _um = detect_insights_model(dashboard_xml)
                            insights_xml = dashboard_xml if _um == "A" else await _navigate_to_insights(device_id, dashboard_xml)
                        nav.append("vpn_recovery:re_navigate_views_detail")
                views_result = await _extract_views_detail(
                    device_id, insights_xml,
                    include_video_details=include_views_detail,
                    nav=nav,
                    username=username,
                )
                result["views_detail"] = views_result["items"]
                nav.append(f"parse:views_detail({len(views_result['items'])} items)")
                if views_result["accounts_reached"] is not None:
                    result["accounts_reached"] = views_result["accounts_reached"]
                    nav.append(f"parse:accounts_reached={views_result['accounts_reached']}")
                # Attach video details to the result
                if views_result.get("video_details"):
                    extended_data["video_details"] = views_result["video_details"]
                    nav.append(f"parse:video_details_individual({len(views_result['video_details'])} videos)")
                # Tier 1: Views extended data
                if views_result.get("views_extended"):
                    extended_data["views"] = views_result["views_extended"]
                    nav.append("parse:tier1_views_extended")
                    logger.info(f"[growth] {device_id} Tier 1 views_extended: {list(views_result['views_extended'].keys())}")
                # Audience demographics (top cities / countries) — optional
                if views_result.get("audience"):
                    extended_data["audience"] = views_result["audience"]
                    nav.append("parse:audience_demographics")

            # VPN check before interactions
            if proxy_enabled:
                recovered = await _assert_vpn(device_id, "before_interactions_detail")
                if recovered:
                    profile_xml = await _navigate_to_profile(device_id)
                    dashboard_xml = await _find_and_tap_professional_dashboard(device_id, profile_xml) if profile_xml else None
                    if dashboard_xml:
                        _um = detect_insights_model(dashboard_xml)
                        insights_xml = dashboard_xml if _um == "A" else await _navigate_to_insights(device_id, dashboard_xml)
                    nav.append("vpn_recovery:re_navigate_interactions_detail")

            # Interactions detail drill-down (Model A — Tier 2)
            # _extract_views_detail already pressed back to insights dashboard
            if insights_xml:
                interactions_detail = await _extract_interactions_detail(device_id, insights_xml)
                if interactions_detail:
                    extended_data["interactions"] = interactions_detail
                    nav.append("parse:tier2_interactions_detail")
                    logger.info(f"[growth] {device_id} Tier 2 interactions_detail: {list(interactions_detail.keys())}")

            # Store extended data in result
            if extended_data:
                result["extended"] = extended_data

        # ── Navigate back to Professional Dashboard for Trial Reels ──
        # After Insights drill-downs, press_back lands on Profile (not dashboard).
        # We need to: back → Profile → tap "Professional dashboard" → dashboard.
        await press_back(device_id)
        await asyncio.sleep(_human_delay(NAV_DELAY))
        nav.append("nav:back_to_profile_for_trial")

        # VPN check before trial reels
        if proxy_enabled:
            recovered = await _assert_vpn(device_id, "before_trial_reels")
            if recovered:
                nav.append("vpn_recovery:re_navigate_trial_reels")
                # IG was restarted — navigate back to profile
                _trial_profile = await _navigate_to_profile(device_id)
                if not _trial_profile:
                    nav.append("vpn_recovery:trial_profile_failed")

        # ── Trial Reels check ──
        try:
            from growth.trial_reels import check_trial_reels

            # We're on Profile now — find and tap "Professional dashboard" again
            profile_xml = await dump_ui(device_id, safe_mode=True)
            dashboard_coords = find_professional_dashboard(profile_xml) if profile_xml else None

            if dashboard_coords:
                await tap(device_id, dashboard_coords[0], dashboard_coords[1])
                await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY))
                nav.append("nav:tap_dashboard_for_trial")

                trial_dashboard_xml = await dump_ui(device_id, safe_mode=True)
                trial_result = await check_trial_reels(device_id, dashboard_xml=trial_dashboard_xml, username=username)
                result["trial_reels"] = trial_result
                for tn in trial_result.get("nav", []):
                    nav.append(tn)
                if trial_result.get("found"):
                    logger.info(f"[growth] {device_id} trial reels: {len(trial_result.get('reels', []))} extracted")

                # Back to profile after trial reels check
                await press_back(device_id)
                await asyncio.sleep(_human_delay(NAV_DELAY))
            else:
                # Fallback: menu → Insights (same as _find_and_tap_professional_dashboard Phase 2)
                logger.info(f"[growth] Dashboard button not found for trial reels — trying menu → Insights on {device_id}")
                nav.append("trial:menu_insights_fallback")

                # ── B': Pre-check screen before blind menu tap ──
                # The blind menu coord (992, 158) is only reliable on the profile
                # screen (where the rightmost action-bar button = hamburger menu).
                # On other screens (e.g. professional_dashboard) that point lands
                # on a different button, which can open the Create (+) panel and
                # cascade into "could not get idle state" → back×2 → leaves IG.
                pre_menu_xml = await dump_ui(device_id, safe_mode=True)
                pre_screen = detect_screen(pre_menu_xml) if pre_menu_xml else "unknown"
                if pre_screen != "profile":
                    logger.warning(f"[growth] Not on profile (screen={pre_screen}) — skipping menu fallback for trial reels on {device_id}")
                    nav.append(f"trial:skipped_not_profile:{pre_screen}")
                    result["trial_reels"] = {"found": False, "reels": [], "nav": ["trial:not_on_profile"]}
                else:
                    # Scroll to top so hamburger icon is visible
                    for _ in range(3):
                        await scroll_up(device_id, amount=0.4)
                        await asyncio.sleep(0.3)

                    menu_xml = await dump_ui(device_id, safe_mode=True)
                    menu_coords = None
                    if menu_xml:
                        menu_coords = find_clickable_element(menu_xml, ["Options", "Opciones"])
                    if not menu_coords:
                        w, h = await _get_screen(device_id)
                        menu_coords = (int(992 * w / 1080), int(143 * h / 2160))
                        logger.info(f"[growth] Menu icon not found, using scaled fallback {menu_coords}")

                    await tap(device_id, menu_coords[0], menu_coords[1])
                    await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY))

                    menu_xml = await dump_ui(device_id, safe_mode=True)

                    # ── A: Guard against accidentally landing on Create (+) bottom sheet ──
                    # The Create panel is a bottom_sheet_container with options
                    # "Create new reel", "Create new post", etc. If we tapped the
                    # wrong button (e.g. on Pixel where action-bar buttons lack
                    # resource-ids), this panel opens. Recover by pressing back
                    # and skipping trial reels for this account.
                    if menu_xml and "bottom_sheet_container" in menu_xml and (
                        "Create new reel" in menu_xml
                        or "Create new post" in menu_xml
                        or "Create new story" in menu_xml
                    ):
                        logger.warning(f"[growth] Menu tap landed on Create (+) panel — recovering on {device_id}")
                        nav.append("trial:landed_on_create_panel")
                        await press_back(device_id)
                        await asyncio.sleep(_human_delay(NAV_DELAY))
                        result["trial_reels"] = {"found": False, "reels": [], "nav": ["trial:create_panel"]}
                    else:
                        insights_coords = None
                        if menu_xml:
                            insights_coords = find_clickable_element(menu_xml, ["Insights", "Estadísticas"])

                        if insights_coords:
                            logger.info(f"[growth] Found 'Insights' in menu at {insights_coords} for trial reels on {device_id}")
                            await tap(device_id, insights_coords[0], insights_coords[1])
                            await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY))
                            nav.append("trial:tap_insights_menu")

                            trial_dashboard_xml = await dump_ui(device_id, safe_mode=True)
                            trial_result = await check_trial_reels(device_id, dashboard_xml=trial_dashboard_xml, username=username)
                            result["trial_reels"] = trial_result
                            for tn in trial_result.get("nav", []):
                                nav.append(tn)
                            if trial_result.get("found"):
                                logger.info(f"[growth] {device_id} trial reels (via menu): {len(trial_result.get('reels', []))} extracted")

                            await press_back(device_id)
                            await asyncio.sleep(_human_delay(NAV_DELAY))
                        else:
                            nav.append("trial:no_dashboard_button")
                            logger.warning(f"[growth] Could not find Professional dashboard or Insights for trial reels on {device_id}")
                            result["trial_reels"] = {"found": False, "reels": [], "nav": ["trial:dashboard_not_found"]}
        except Exception as e:
            logger.warning(f"[growth] Trial reels check failed on {device_id}: {e}")
            nav.append(f"trial:error={type(e).__name__}")

        # ── Mark success ──
        has_any_metric = any(
            result[k] is not None
            for k in ("views", "interactions", "new_followers", "accounts_reached", "content_shared")
        )
        if has_any_metric:
            result["success"] = True
        else:
            result["error"] = "No metrics could be extracted"

    except ProxyDroppedError as e:
        logger.error(f"[checker] PROXY DROPPED on {device_id}: {e} — aborting check")
        result["error"] = str(e)
        # Still mark as success=True so it saves to DB (proxy issue, not a code bug)
        result["success"] = True
        nav.append(f"ABORT:proxy_dropped")

    except Exception as e:
        logger.error(f"[checker] Unexpected error on {device_id}: {e}", exc_info=True)
        result["error"] = f"Unexpected: {type(e).__name__}: {e}"
        nav.append(f"ERROR:{type(e).__name__}")

    finally:
        if _skip_cleanup:
            # Multi-account mode: navigate back to profile and STAY in IG.
            # Do NOT press HOME — the orchestrator needs IG open on the
            # profile screen so switch_ig_account can access the dropdown.
            reached_profile = False
            try:
                MAX_BACKS = 6
                # Press back 2 times first, then check
                for i in range(1, MAX_BACKS + 1):
                    await press_back(device_id)
                    await asyncio.sleep(0.4)
                    # Check after every 2 backs (2, 4, 6)
                    if i % 2 == 0:
                        xml = await dump_ui(device_id, safe_mode=True, idle_escape="force_stop_cycle")
                        if xml and _is_instagram_xml(xml):
                            screen = detect_screen(xml)
                            if screen == "profile":
                                logger.info(f"[cleanup] Multi-account: reached profile after {i} backs on {device_id}")
                                nav.append(f"multi_account:profile_after_{i}_backs")
                                reached_profile = True
                                break
                            elif screen == "home":
                                # On IG home feed — tap profile tab via shared helper
                                # (handles Pixel edge-to-edge coord compensation)
                                w, h = await _get_screen(device_id)
                                px, py = profile_tab_coords(w, h)
                                logger.info(f"[cleanup] Multi-account: on home feed after {i} backs — tapping profile tab at ({px},{py}) on {device_id}")
                                await tap(device_id, px, py)
                                await asyncio.sleep(1.5)
                                nav.append(f"multi_account:profile_via_tab_after_{i}_backs")
                                reached_profile = True
                                break
                            else:
                                logger.debug(f"[cleanup] Multi-account: screen={screen} after {i} backs on {device_id} — continuing")
                        elif xml and not _is_instagram_xml(xml):
                            # Left IG entirely — need to reopen
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
                    xml = await dump_ui(device_id, safe_mode=True, idle_escape="force_stop_cycle")
                    if xml and _is_instagram_xml(xml):
                        screen = detect_screen(xml)
                        if screen == "profile":
                            nav.append("multi_account:profile_via_reopen")
                            reached_profile = True
                        elif screen in ("home", "other"):
                            # Tap profile tab via shared helper (Pixel-aware)
                            w, h = await _get_screen(device_id)
                            px, py = profile_tab_coords(w, h)
                            await tap(device_id, px, py)
                            await asyncio.sleep(1.5)
                            nav.append("multi_account:profile_via_reopen_tab")
                            reached_profile = True
                    if not reached_profile:
                        logger.warning(f"[cleanup] Multi-account: could not reach profile on {device_id} — proceeding anyway")
                        nav.append("multi_account:profile_fallback_failed")
                except Exception as e:
                    logger.warning(f"[cleanup] Multi-account reopen failed on {device_id}: {e}")
                    nav.append("multi_account:reopen_failed")
        else:
            # ── Cleanup: back out of IG, press Home, stop proxy ──
            try:
                # IG-specific cleanup only if IG was actually opened. If
                # proxy failed early, we're still on the proxy app screen
                # and don't need to press back/home — those operations would
                # just kick us off the proxy app onto Android home for no
                # reason, leaving stop_proxy to early-return on inactive VPN.
                if ig_was_opened:
                    # Press back a few times to exit deep navigation
                    for _ in range(5):
                        await press_back(device_id)
                        await asyncio.sleep(0.4)

                    # Press Android Home — IG goes to background intact
                    await press_home(device_id)
                    logger.debug(f"[nav] Pressed HOME on {device_id} — IG minimized to background")
                else:
                    logger.debug(f"[nav] Skipping IG cleanup on {device_id} — IG never opened (likely proxy failed)")

                # ── Stop proxy (shielded from cancellation) ──
                if proxy_enabled:
                    await asyncio.sleep(1.0)
                    logger.info(f"[proxy] Stopping proxy on {device_id}")
                    try:
                        from growth.proxy import stop_proxy
                        stop_result = await asyncio.shield(stop_proxy(device_id))
                    except asyncio.CancelledError:
                        logger.warning(f"[proxy] Task cancelled but proxy stop was shielded on {device_id}")
                        stop_result = {"success": False, "state": "error", "error": "Task cancelled during proxy stop"}
                    nav.append(f"proxy_stop:{stop_result['state']}")
                    if stop_result["success"]:
                        logger.info(f"[proxy] Proxy stopped on {device_id} — left on proxy screen")
                    else:
                        logger.warning(f"[proxy] Failed to stop proxy on {device_id}: {stop_result.get('error')}")

                # Always force portrait after cleanup — proxy/ads may have left screen in landscape
                await force_portrait(device_id)

            except Exception as e:
                logger.warning(f"[cleanup] Error during cleanup on {device_id}: {e}")
                nav.append(f"cleanup_error:{e}")

    # ── Ensure date_range always reflects "Last 30 days" ──
    if not result.get("date_range") and result.get("success"):
        today = datetime.now(timezone.utc)
        start = today - timedelta(days=30)
        result["date_range"] = f"{start.strftime('%b %d')} - {today.strftime('%b %d')}"

    return result
