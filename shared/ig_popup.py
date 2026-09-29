"""
Instagram popup detection and dismissal — shared across services.

After opening IG, dialogs like "Save your login info?" can block the UI.
This module detects such popups by checking for the bottom tab bar
and dismisses them via known button texts.
"""

import asyncio
import re
import xml.etree.ElementTree as ET

from logger import logger

# ── Popup dismiss button texts (lowercase) ──
_DISMISS_TEXTS = [
    "not now", "skip", "no thanks", "i agree", "cancel",
]


def _parse_bounds(bounds_str: str):
    """Parse '[x1,y1][x2,y2]' → (x1, y1, x2, y2) or None."""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if m:
        return tuple(int(m.group(i)) for i in range(1, 5))
    return None


def _center_of(bounds_str: str):
    """Get center point of a bounds string."""
    b = _parse_bounds(bounds_str)
    if b:
        return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
    return None


def has_tab_bar(xml_str: str) -> bool:
    """Check if IG's bottom tab bar is present in the UI dump.

    The tab bar is visible on any normal IG screen (feed, profile, reels,
    explore, etc.) but hidden when a popup/dialog is blocking.
    """
    return "tab_bar" in xml_str


def find_dismiss_button(xml_str: str):
    """Find a popup dismiss button in UI dump.

    Scans for common IG popup dismiss texts (Not Now, Skip, etc.).
    Returns ((cx, cy), label) or None.
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None

    for elem in root.iter("node"):
        text = elem.get("text", "").strip().lower()
        content_desc = elem.get("content-desc", "").strip().lower()

        matched_label = None
        if text in _DISMISS_TEXTS:
            matched_label = elem.get("text", "").strip()
        elif content_desc in _DISMISS_TEXTS:
            matched_label = elem.get("content-desc", "").strip()
        elif content_desc == "close":
            matched_label = "Close (X)"

        if matched_label:
            bounds = elem.get("bounds", "")
            center = _center_of(bounds)
            if center:
                return center, matched_label

    return None


async def dismiss_popup_if_present(device_id: str, xml: str | None = None) -> bool:
    """Lightweight mid-flow popup dismissal.

    Unlike ``ensure_clean_ig_state`` (which is heavy: back-cycle + reopen IG when
    state is unrecoverable), this helper just checks the current XML for a known
    dismiss button and taps it if found. Intended for use after intermediate
    navigation steps (e.g., after tapping Profile tab) where a popup like
    "Save your login info?" may appear and block subsequent actions.

    Args:
        device_id: ADB device serial.
        xml: Optional UI dump. If None, a fresh dump is taken.

    Returns:
        True if a popup was detected and a dismiss tap was issued.
        False if no popup was found (or XML couldn't be obtained).
    """
    from shared.adb import dump_ui, tap

    if xml is None:
        xml = await dump_ui(device_id, safe_mode=True)
    if not xml:
        return False

    btn = find_dismiss_button(xml)
    if not btn:
        return False

    (cx, cy), label = btn
    logger.info(f"[ig_popup] Dismissing mid-flow popup '{label}' at ({cx},{cy}) on {device_id}")
    await tap(device_id, cx, cy, jitter=3)
    await asyncio.sleep(1.0)
    return True


async def ensure_clean_ig_state(device_id: str, delay_fn=None, max_retries: int = 2) -> bool:
    """Verify IG is usable — bottom tab bar visible, no popup blocking.

    After opening IG, a dialog like "Save your login info?" may appear.
    If the tab bar is NOT visible, this function:
      1. Tries to dismiss the popup (tap "Not Now" / "Skip" / etc.)
      2. Double-backs to exit IG
      3. Reopens IG and checks again
    Retries up to *max_retries* times.

    Args:
        device_id: ADB device serial.
        delay_fn: Optional callable(base) -> float for human-like delays.
                  Falls back to plain asyncio.sleep if not provided.
        max_retries: Max dismiss + reopen attempts.
    """
    from shared.adb import dump_ui, tap, press_back, open_instagram

    def _delay(base):
        if delay_fn:
            return delay_fn(base)
        return base

    for attempt in range(max_retries + 1):
        await asyncio.sleep(_delay(1.5))

        xml = await dump_ui(device_id, safe_mode=True, idle_escape="force_stop_cycle")

        if xml and has_tab_bar(xml):
            if attempt > 0:
                logger.info(f"[ig_popup] IG clean after {attempt} popup dismiss(es) on {device_id}")
            else:
                logger.debug(f"[ig_popup] IG tab bar visible — clean state on {device_id}")
            return True

        # No tab bar → popup or unexpected state
        if attempt >= max_retries:
            logger.error(
                f"[ig_popup] IG not in valid state after {max_retries} retries on {device_id}"
            )
            return False

        logger.warning(
            f"[ig_popup] No tab bar found on {device_id} — popup detected "
            f"(attempt {attempt + 1}/{max_retries})"
        )

        # Try to find and tap a dismiss button
        dismissed = False
        if xml:
            btn = find_dismiss_button(xml)
            if btn:
                (cx, cy), label = btn
                logger.info(f"[ig_popup] Tapping popup dismiss '{label}' at ({cx},{cy}) on {device_id}")
                await tap(device_id, cx, cy, jitter=3)
                dismissed = True

        if not dismissed:
            logger.info(f"[ig_popup] No known dismiss button — pressing back on {device_id}")
            await press_back(device_id)

        await asyncio.sleep(_delay(1.0))

        # Double back to fully exit IG
        logger.info(f"[ig_popup] Double-back to exit IG on {device_id}")
        await press_back(device_id)
        await asyncio.sleep(_delay(0.5))
        await press_back(device_id)
        await asyncio.sleep(_delay(1.0))

        # Reopen IG fresh
        await open_instagram(device_id)
        await asyncio.sleep(_delay(2.5))

    return False
