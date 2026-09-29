"""
Instagram account checker — ADB / UIAutomator approach.

Uses a real Android device with Instagram logged in.
Navigates the IG app via ADB to check profiles:
  1. Start proxy app (if enabled) — reuses growth/proxy.py
  2. Open Instagram
  3. Navigate to Explore/Search
  4. Type username → tap first user result → read profile metrics
  5. Back → clear input → repeat for next account

Profile data extracted via resource-id matching:
  - posts:     profile_header_familiar_post_count_value
  - followers: profile_header_familiar_followers_value
  - following: profile_header_familiar_following_value
  - full_name: profile_header_full_name_above_vanity
  - bio:       profile_user_info_compose_view (first text child)
  - private:   row_profile_header_empty_profile_notice_title == "This account is private"
  - username:  action_bar_title (on profile screen)
"""

import asyncio
import random
import re
import xml.etree.ElementTree as ET

from config import checker_settings as settings
from logger import logger
from shared.ig_popup import ensure_clean_ig_state as _shared_ensure_clean_ig_state


# ── Proxy — delegate to growth/proxy.py ───────────────────

async def _rotate_proxy_ip() -> None:
    """Rotate proxy IP via HTTP renew URL (if configured)."""
    if not settings.proxy_rotate_url:
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(settings.proxy_rotate_url)
            logger.info(f"[checker] Proxy IP rotated — status {resp.status_code}")
        await asyncio.sleep(2.5)  # Wait for IP to propagate
    except Exception as e:
        logger.warning(f"[checker] Proxy IP rotation failed: {e}")


# ── ADB helpers ───────────────────────────────────────────

# Reference resolution for coordinate scaling
_REF_W, _REF_H = 1080, 2160

# Cache screen size
_screen_cache: dict[str, tuple[int, int]] = {}


async def _get_screen(device_id: str) -> tuple[int, int]:
    """Get device screen size (cached)."""
    if device_id in _screen_cache:
        return _screen_cache[device_id]
    from shared.adb import get_screen_size
    w, h = await get_screen_size(device_id)
    _screen_cache[device_id] = (w, h)
    return w, h


def _scale(w: int, h: int, ref_x: int, ref_y: int) -> tuple[int, int]:
    """Scale reference coords (1080x2160) to actual screen."""
    return int(ref_x * w / _REF_W), int(ref_y * h / _REF_H)


def _human_delay(base: float, spread: float = 0.35) -> float:
    """Human-realistic delay with wide, non-uniform distribution.

    Uses a triangular distribution centred slightly below *base* so most
    waits are near the expected time but occasionally much shorter or longer.
    *spread* controls the ± range as a fraction of base (default 35%).

    Occasionally (10%) adds an extra "distraction pause" of 0.4-1.5s,
    simulating the user glancing at something on screen before continuing.
    """
    lo = base * (1 - spread)
    hi = base * (1 + spread)
    mode = base * random.uniform(0.85, 1.0)  # peak shifts slightly each call
    delay = random.triangular(lo, hi, mode)

    # Distraction pause — user looks at feed / notification / etc.
    if random.random() < 0.10:
        delay += random.uniform(0.4, 1.5)

    return max(delay, 0.05)


# ── Organic scroll behaviour ────────────────────────────

async def _maybe_scroll_feed(device_id: str, chance: float = 0.35) -> None:
    """Occasionally scroll the IG feed like a real user glancing at content.

    Called after opening IG / landing on home. A human doesn't immediately
    tap Search — they often glance at a post or two first.
    """
    if random.random() > chance:
        return
    from shared.adb import scroll_down, scroll_up

    scrolls = random.randint(1, 3)
    logger.debug(f"[checker] Organic feed scroll ({scrolls}x) on {device_id}")
    for _ in range(scrolls):
        amount = random.uniform(0.15, 0.40)
        await scroll_down(device_id, amount=amount)
        await asyncio.sleep(_human_delay(1.2, spread=0.50))

    # Sometimes scroll back up a bit — "wait, let me go back"
    if random.random() < 0.30:
        await scroll_up(device_id, amount=random.uniform(0.10, 0.25))
        await asyncio.sleep(_human_delay(0.8, spread=0.40))


async def _maybe_scroll_explore(device_id: str, chance: float = 0.25) -> None:
    """Occasionally scroll the Explore grid before tapping search.

    Simulates a user looking at explore thumbnails before searching.
    Always scrolls back to top afterwards so the search bar is visible.
    """
    if random.random() > chance:
        return
    from shared.adb import scroll_down, scroll_up

    scrolls = random.randint(1, 2)
    logger.debug(f"[checker] Organic explore scroll ({scrolls}x) on {device_id}")
    for _ in range(scrolls):
        amount = random.uniform(0.12, 0.30)
        await scroll_down(device_id, amount=amount)
        await asyncio.sleep(_human_delay(1.0, spread=0.45))

    # Scroll back to top so the search bar is visible again
    await asyncio.sleep(_human_delay(0.5, spread=0.30))
    for _ in range(scrolls + 1):  # +1 to guarantee we reach the top
        await scroll_up(device_id, amount=0.40)
        await asyncio.sleep(_human_delay(0.4, spread=0.35))


async def _maybe_scroll_profile(device_id: str, chance: float = 0.30) -> None:
    """Occasionally scroll down on a profile to glance at posts.

    A human checking a profile often scrolls a bit to see recent posts
    before going back.
    """
    if random.random() > chance:
        return
    from shared.adb import scroll_down, scroll_up

    logger.debug(f"[checker] Organic profile scroll on {device_id}")
    amount = random.uniform(0.15, 0.35)
    await scroll_down(device_id, amount=amount)
    await asyncio.sleep(_human_delay(1.5, spread=0.50))

    # Scroll back up so the profile header is visible (for the dump_ui)
    # — not always, sometimes the user just hits back from scrolled position
    if random.random() < 0.50:
        await scroll_up(device_id, amount=random.uniform(0.10, 0.25))
        await asyncio.sleep(_human_delay(0.6, spread=0.40))


# ── Scrcpy session lifecycle ──────────────────────────────

async def _start_scrcpy(device_id: str, max_attempts: int = 3) -> None:
    """Start scrcpy UHID session for anti-detection input.

    Retries up to *max_attempts* times, killing any stale scrcpy process
    on the device between attempts.  Falls back to ADB input if all fail.
    """
    if not settings.use_scrcpy:
        return
    from shared.scrcpy_uhid import start_session

    for attempt in range(1, max_attempts + 1):
        # Kill stale scrcpy on this device before each attempt
        _stop_scrcpy(device_id)
        if attempt > 1:
            await asyncio.sleep(1.5)  # extra breathing room after a failed attempt

        try:
            ok = await start_session(device_id)
            if ok:
                logger.info(f"[checker] Scrcpy UHID session started for {device_id} (attempt {attempt}/{max_attempts})")
                return
            else:
                logger.warning(f"[checker] Scrcpy UHID attempt {attempt}/{max_attempts} failed on {device_id}")
        except Exception as e:
            logger.warning(f"[checker] Scrcpy UHID attempt {attempt}/{max_attempts} error on {device_id}: {e}")

    logger.info(f"[checker] Scrcpy UHID unavailable after {max_attempts} attempts on {device_id} — will use ADB input fallback")


def _stop_scrcpy(device_id: str) -> None:
    """Stop scrcpy UHID session for a device."""
    try:
        from shared.scrcpy_uhid import stop_session
        stop_session(device_id)
    except Exception as e:
        logger.debug(f"[checker] Scrcpy UHID stop error for {device_id}: {e}")


# ── Human-like typing patterns ──────────────────────────

# Adjacent keys on a QWERTY layout — used for realistic typos
_NEARBY_KEYS: dict[str, str] = {
    "a": "sq", "b": "vn", "c": "xv", "d": "sf", "e": "wr",
    "f": "dg", "g": "fh", "h": "gj", "i": "uo", "j": "hk",
    "k": "jl", "l": "k", "m": "n", "n": "bm", "o": "ip",
    "p": "o", "q": "w", "r": "et", "s": "ad", "t": "ry",
    "u": "yi", "v": "cb", "w": "qe", "x": "zc", "y": "tu",
    "z": "x", ".": "",  "_": "",
}


def _pick_typing_pattern() -> str:
    """Randomly choose a typing behaviour for this input session.

    Patterns (weights designed so no single one dominates):
      steady   – consistent ~120-180ms per char, no drama
      rusher   – fast 50-100ms bursts of 2-4 chars then micro-pause
      cautious – slow 160-280ms, occasional long think-pauses
      natural  – accelerates mid-word, slows at punctuation / dots
    """
    return random.choices(
        ["steady", "rusher", "cautious", "natural"],
        weights=[30, 25, 20, 25],
        k=1,
    )[0]


def _char_delay(pattern: str, idx: int, char: str, length: int) -> float:
    """Return the delay (seconds) to wait *before* typing char at idx."""
    if idx == 0:
        # Small initial pause — finger finding the first key
        return random.uniform(0.12, 0.30)

    if pattern == "steady":
        return random.uniform(0.10, 0.19)

    if pattern == "rusher":
        # Bursts of 2-4 fast chars, then a micro-pause
        if idx % random.choice([2, 3, 4]) == 0:
            return random.uniform(0.22, 0.40)  # pause between bursts
        return random.uniform(0.04, 0.10)  # burst speed

    if pattern == "cautious":
        base = random.uniform(0.16, 0.28)
        # 15% chance of a "think pause"
        if random.random() < 0.15:
            base += random.uniform(0.30, 0.60)
        return base

    # natural — speed ramps up then slows at end/punctuation
    progress = idx / max(length - 1, 1)  # 0→1
    if char in "._-":
        return random.uniform(0.20, 0.38)  # punctuation pause
    if progress < 0.25:
        return random.uniform(0.14, 0.24)  # warmup
    if progress > 0.80:
        return random.uniform(0.12, 0.22)  # slight slowdown at end
    return random.uniform(0.06, 0.14)  # fast in the middle


def _should_typo(idx: int, length: int) -> bool:
    """Decide whether to introduce a typo at this position.
    ~8% chance, never on first or last char, max once per word."""
    if idx < 1 or idx >= length - 1:
        return False
    return random.random() < 0.08


def _get_typo_char(char: str) -> str:
    """Return a plausible wrong character near *char* on the keyboard."""
    neighbours = _NEARBY_KEYS.get(char.lower(), "")
    if not neighbours:
        return char  # no good typo candidate — skip
    return random.choice(neighbours)


# ── Text input (scrcpy UHID → ADB fallback) ──────────────

async def _input_text(device_id: str, text: str) -> bool:
    """Type text into the currently focused field, character by character
    with human-like timing and occasional typo→backspace corrections.

    Priority:
      1. Scrcpy UHID char-by-char (undetectable — kernel HID events)
      2. Fallback: adb shell input text (single shot, detectable)
    """
    # ── Try scrcpy UHID — human-like char-by-char ──
    if settings.use_scrcpy:
        try:
            from shared.scrcpy_uhid import has_active_session, uhid_text
            if has_active_session(device_id):
                ok = await _human_type(device_id, text, uhid_text)
                if ok:
                    return True
                logger.debug(f"[checker] scrcpy human-type failed on {device_id}, falling back")
        except Exception as e:
            logger.debug(f"[checker] scrcpy text error on {device_id}: {e} — falling back")

    # ── Fallback: adb shell input text (single shot) ──
    from shared.adb import _base_cmd, _run
    escaped = text.replace(" ", "%s").replace("&", "\\&").replace("(", "\\(").replace(")", "\\)")
    base = _base_cmd()
    code, _, err = await _run(
        base + ["-s", device_id, "shell", "input", "text", escaped],
    )
    if code != 0:
        logger.warning(f"[checker] input text failed on {device_id}: {err}")
        return False
    return True


async def _human_type(
    device_id: str,
    text: str,
    type_fn,
) -> bool:
    """Type *text* one character at a time with human-realistic timing.

    Randomly selects a typing pattern (steady / rusher / cautious / natural)
    and occasionally introduces a typo followed by a backspace correction.
    """
    pattern = _pick_typing_pattern()
    length = len(text)
    typo_done = False  # max 1 typo per input to keep it subtle

    logger.debug(f"[checker] Human-typing '{text}' pattern={pattern} on {device_id}")

    for idx, char in enumerate(text):
        # ── Delay before this character ──
        delay = _char_delay(pattern, idx, char, length)
        await asyncio.sleep(delay)

        # ── Occasional typo (once per input at most) ──
        if not typo_done and _should_typo(idx, length):
            wrong = _get_typo_char(char)
            if wrong != char:
                typo_done = True
                # Type wrong char
                ok = await type_fn(device_id, wrong)
                if not ok:
                    return False
                # Brief pause — realize the mistake
                await asyncio.sleep(random.uniform(0.25, 0.55))
                # Backspace
                await _press_del(device_id)
                await asyncio.sleep(random.uniform(0.08, 0.18))
                # Now type the correct char (fall through below)

        # ── Type the actual character ──
        ok = await type_fn(device_id, char)
        if not ok:
            return False

    logger.debug(
        f"[checker] Typed '{text}' ({length} chars, pattern={pattern}, "
        f"typo={'yes' if typo_done else 'no'}) on {device_id}"
    )
    return True


async def _press_del(device_id: str) -> bool:
    """Press KEYCODE_DEL (backspace) via scrcpy UHID or ADB fallback."""
    if settings.use_scrcpy:
        try:
            from shared.scrcpy_uhid import has_active_session, uhid_keycode
            if has_active_session(device_id):
                return await uhid_keycode(device_id, 67)  # KEYCODE_DEL
        except Exception:
            pass
    from shared.adb import _base_cmd, _run
    base = _base_cmd()
    code, _, _ = await _run(base + ["-s", device_id, "shell", "input", "keyevent", "67"])
    return code == 0


async def _clear_search_input(device_id: str) -> bool:
    """Clear the search input field.

    Strategy (in order):
      1. Try tapping the "Clear text" button (X icon inside search field)
      2. Fallback: brute-force delete with repeated KEYCODE_DEL (via scrcpy or ADB)
    """
    from shared.adb import dump_ui, tap

    # ── Strategy 1: Tap the "Clear text" button ──
    xml = await dump_ui(device_id, safe_mode=True)
    if xml:
        match = re.search(
            r'content-desc="Clear text"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml,
        )
        if match:
            x = (int(match.group(1)) + int(match.group(3))) // 2
            y = (int(match.group(2)) + int(match.group(4))) // 2
            await tap(device_id, x, y, jitter=3)
            logger.info(f"[checker] Cleared search via 'Clear text' button at ({x},{y})")
            await asyncio.sleep(_human_delay(0.5, spread=0.35))
            return True

    # ── Strategy 2: Brute-force delete (scrcpy UHID or ADB) ──
    for _ in range(35):
        await _press_del(device_id)
    await asyncio.sleep(_human_delay(0.3, spread=0.40))
    logger.info(f"[checker] Cleared search via brute-force delete on {device_id}")

    return True


# ── XML parsing helpers ──────────────────────────────────

def _find_by_resource_id(xml_str: str, partial_id: str) -> list[dict]:
    """Find all nodes whose resource-id contains partial_id.
    Returns list of {text, bounds, resource_id, content_desc}."""
    results = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return results

    for elem in root.iter("node"):
        rid = elem.get("resource-id", "")
        if partial_id in rid:
            results.append({
                "text": elem.get("text", ""),
                "bounds": elem.get("bounds", ""),
                "resource_id": rid,
                "content_desc": elem.get("content-desc", ""),
                "clickable": elem.get("clickable", "false") == "true",
            })
    return results


def _parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
    """Parse '[x1,y1][x2,y2]' → (x1, y1, x2, y2)."""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if m:
        return tuple(int(m.group(i)) for i in range(1, 5))
    return None


def _center_of(bounds_str: str) -> tuple[int, int] | None:
    """Get center point of a bounds string."""
    b = _parse_bounds(bounds_str)
    if b:
        return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
    return None


def _find_dismiss_button(xml_str: str) -> tuple[tuple[int, int], str] | None:
    """Find a popup dismiss button in UI dump.

    Scans for common IG popup dismiss texts (Not Now, Skip, etc.).
    Returns ((cx, cy), label) or None.
    """
    # Order matters — most specific first to avoid false positives
    _DISMISS_TEXTS = [
        "not now", "skip", "no thanks", "i agree", "cancel",
    ]

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


def _has_tab_bar(xml_str: str) -> bool:
    """Check if IG's bottom tab bar is present in the UI dump.

    The tab bar is visible on any normal IG screen (feed, profile, reels,
    explore, etc.) but hidden when a popup/dialog is blocking.
    """
    return "tab_bar" in xml_str


def _find_search_results(xml_str: str) -> list[dict]:
    """Parse search results from UI dump.
    Returns list of user results (skipping keyword suggestions).

    Each result: {username, full_name, bounds, center}
    """
    results = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return results

    # Find all row_search_user_container elements (actual user results)
    for elem in root.iter("node"):
        rid = elem.get("resource-id", "")
        if "row_search_user_container" not in rid:
            continue

        bounds = elem.get("bounds", "")
        center = _center_of(bounds)
        if not center:
            continue

        # Extract username and full_name from children
        username = ""
        full_name = ""
        for child in elem.iter("node"):
            child_rid = child.get("resource-id", "")
            if "row_search_user_username" in child_rid:
                username = child.get("text", "").strip()
            elif "row_search_user_fullname" in child_rid:
                full_name = child.get("text", "").strip()

        results.append({
            "username": username,
            "full_name": full_name,
            "bounds": bounds,
            "center": center,
        })

    return results


def _parse_profile_data(xml_str: str) -> dict | None:
    """Parse profile screen XML to extract metrics.

    Resource-IDs used:
      - action_bar_title → username on profile
      - profile_header_familiar_post_count_value → posts count
      - profile_header_familiar_followers_value → followers count
      - profile_header_familiar_following_value → following count
      - profile_header_full_name_above_vanity → full name
      - profile_user_info_compose_view → bio (first text child)
      - row_profile_header_empty_profile_notice_title → "This account is private"
      - profile_header_follow_button → content-desc has "Follow <name>" (confirms we're on profile)
    """
    if not xml_str:
        return None

    data = {
        "followers": None,
        "following": None,
        "posts": None,
        "bio": "",
        "full_name": "",
        "is_private": False,
        "profile_pic": "",
    }

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None

    # Verify we're on a profile screen (action_bar_title or profile_header present)
    found_profile = False

    for elem in root.iter("node"):
        rid = elem.get("resource-id", "")
        text = elem.get("text", "").strip()

        # ── Posts ──
        if "profile_header_familiar_post_count_value" in rid and text:
            data["posts"] = _parse_count(text)
            found_profile = True

        # ── Followers ──
        elif "profile_header_familiar_followers_value" in rid and text:
            data["followers"] = _parse_count(text)
            found_profile = True

        # ── Following ──
        elif "profile_header_familiar_following_value" in rid and text:
            data["following"] = _parse_count(text)
            found_profile = True

        # ── Full name ──
        elif "profile_header_full_name_above_vanity" in rid and text:
            data["full_name"] = text

        # ── Private account ──
        elif "row_profile_header_empty_profile_notice_title" in rid:
            if "private" in text.lower():
                data["is_private"] = True

        # ── Bio (Compose view — first text child) ──
        elif "profile_user_info_compose_view" in rid:
            # Walk children to find first non-empty text
            for child in elem.iter("node"):
                child_text = child.get("text", "").strip()
                if child_text:
                    data["bio"] = child_text
                    break

    if not found_profile:
        return None

    return data


def _parse_count(text: str) -> int | None:
    """Parse count strings like '184', '1,234', '12.5K', '1.2M'."""
    if not text:
        return None
    text = text.strip().replace(",", "")
    multipliers = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    lower = text.lower()
    for suffix, mult in multipliers.items():
        if lower.endswith(suffix):
            try:
                return int(float(lower[:-1]) * mult)
            except ValueError:
                return None
    try:
        return int(text)
    except ValueError:
        return None


# ── Navigation ────────────────────────────────────────────

async def _open_explore(device_id: str) -> bool:
    """Navigate to Instagram's Explore/Search screen.

    Strategy:
      1. Try deep link — am start -a VIEW -d "instagram://explore"
      2. Try dump_ui → find search_tab with VALID bounds → tap it
      3. Fallback: coordinate-based tap (2nd icon in bottom nav)
    """
    from shared.adb import dump_ui, tap, _base_cmd, _run

    # ── Strategy 1: deep link ──
    base = _base_cmd()
    code, out, err = await _run(
        base + ["-s", device_id, "shell",
                "am", "start", "-a", "android.intent.action.VIEW",
                "-d", "instagram://explore",
                "com.instagram.android"],
    )
    if code == 0:
        logger.info(f"[checker] Opened Explore via deep link on {device_id}")
        return True

    logger.warning(f"[checker] Deep link failed ({err.strip()}), trying UI tap...")

    # ── Strategy 2: find search_tab via dump_ui ──
    xml = await dump_ui(device_id, safe_mode=True)
    if xml:
        match = re.search(
            r'resource-id="[^"]*search_tab"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml,
        )
        if match:
            x1, y1, x2, y2 = (int(match.group(i)) for i in range(1, 5))
            if x2 > 0 and y2 > 0:
                x = (x1 + x2) // 2
                y = (y1 + y2) // 2
                await tap(device_id, x, y)
                logger.info(f"[checker] Tapped search_tab via resource-id ({x},{y}) on {device_id}")
                return True

        match = re.search(
            r'content-desc="Search and explore"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml,
        )
        if match:
            x1, y1, x2, y2 = (int(match.group(i)) for i in range(1, 5))
            if x2 > 0 and y2 > 0:
                x = (x1 + x2) // 2
                y = (y1 + y2) // 2
                await tap(device_id, x, y)
                logger.info(f"[checker] Tapped Search via content-desc ({x},{y}) on {device_id}")
                return True

    # ── Strategy 3: coordinate-based fallback ──
    w, h = await _get_screen(device_id)
    sx, sy = _scale(w, h, 270, 2100)
    await tap(device_id, sx, sy)
    logger.info(f"[checker] Tapped Search icon via coords ({sx},{sy}) on {device_id}")
    return True


async def _tap_search_input(device_id: str) -> bool:
    """Tap the search input field on the Explore screen."""
    from shared.adb import dump_ui, tap

    xml = await dump_ui(device_id, safe_mode=True)
    if not xml:
        logger.warning(f"[checker] No UI dump for search input on {device_id}")
        return False

    match = re.search(
        r'resource-id="[^"]*action_bar_search_edit_text"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        xml,
    )
    if match:
        x = (int(match.group(1)) + int(match.group(3))) // 2
        y = (int(match.group(2)) + int(match.group(4))) // 2
        await tap(device_id, x, y)
        logger.info(f"[checker] Tapped search input ({x},{y}) on {device_id}")
        return True

    logger.warning(f"[checker] Search input not found in UI dump on {device_id}")
    return False


async def _is_on_explore(device_id: str) -> bool:
    """Check if we're on the Explore/Search screen."""
    from shared.adb import dump_ui

    xml = await dump_ui(device_id, safe_mode=True)
    if not xml:
        return False

    if 'resource-id="com.instagram.android:id/search_tab"' in xml and 'selected="true"' in xml:
        match = re.search(r'resource-id="[^"]*search_tab"[^>]*selected="true"', xml)
        if match:
            return True

    if 'action_bar_search_edit_text' in xml or 'explore_action_bar' in xml:
        return True

    return False


async def _ensure_instagram_open(device_id: str) -> bool:
    """Make sure Instagram is open and in foreground."""
    from shared.adb import (
        open_instagram, open_instagram_to_profile, is_instagram_foreground,
        is_device_online, is_instagram_installed,
    )

    if not await is_device_online(device_id):
        logger.error(f"[checker] Device {device_id} is offline")
        return False

    if not await is_instagram_installed(device_id):
        logger.error(f"[checker] Instagram not installed on {device_id}")
        return False

    if not await is_instagram_foreground(device_id):
        # Open IG + tap Profile — avoids reel on home feed
        await open_instagram_to_profile(device_id)
        await asyncio.sleep(_human_delay(2.0, spread=0.3))

        if not await is_instagram_foreground(device_id):
            await asyncio.sleep(_human_delay(3.0, spread=0.25))
            if not await is_instagram_foreground(device_id):
                return False

    # ── Verify IG is in a clean state (no popups blocking) ──
    return await _ensure_clean_ig_state(device_id)


async def _ensure_clean_ig_state(device_id: str, max_retries: int = 2) -> bool:
    """Verify IG is usable — delegates to shared.ig_popup."""
    return await _shared_ensure_clean_ig_state(
        device_id,
        delay_fn=lambda base: _human_delay(base, spread=0.30),
        max_retries=max_retries,
    )


async def _navigate_to_search_ready(device_id: str) -> bool:
    """Full navigation to get the search input focused and ready for typing.
    Returns True if search input is focused and ready.
    """
    # Open Explore
    await _open_explore(device_id)
    await asyncio.sleep(_human_delay(2.0))

    # Verify we're on Explore screen
    if not await _is_on_explore(device_id):
        logger.warning(f"[checker] Not on Explore screen, retrying...")
        await _open_explore(device_id)
        await asyncio.sleep(_human_delay(2.0))

    # Organic: sometimes scroll explore thumbnails before searching
    await _maybe_scroll_explore(device_id)

    # Tap search input to focus it
    if not await _tap_search_input(device_id):
        logger.error(f"[checker] Could not focus search input on {device_id}")
        return False

    await asyncio.sleep(_human_delay(1.0))
    return True


# ── Search + Profile read ────────────────────────────────

async def _search_and_read_profile(device_id: str, username: str) -> dict:
    """Type a username in the search bar, tap the exact match result,
    read the profile data, and return.

    Assumes search input is already focused and empty.
    After reading, presses back to return to search results, then clears input.

    Returns dict with profile data or error info.
    """
    from shared.adb import dump_ui, tap, press_back

    result = {
        "username": username,
        "followers": None,
        "following": None,
        "posts": None,
        "bio": "",
        "full_name": "",
        "is_private": False,
        "profile_pic": "",
        "status": "error",
        "error": "",
    }

    target = username.lower().strip().lstrip("@")

    # ── Step 1: Type username ──
    logger.info(f"[checker] Typing '{target}' in search on {device_id}")
    if not await _input_text(device_id, target):
        result["error"] = "Failed to type username in search"
        return result

    # Wait for search results to load (initial wait)
    await asyncio.sleep(_human_delay(4.0))

    # ── Step 2: Find the exact match — polling with retries ──
    search_results = []
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        xml = await dump_ui(device_id, safe_mode=True)
        if xml:
            search_results = _find_search_results(xml)
        if search_results:
            break
        if attempt < max_retries:
            logger.info(
                f"[checker] No search results for '{target}' — "
                f"retry {attempt}/{max_retries - 1}, waiting 1.5s"
            )
            await asyncio.sleep(_human_delay(1.5, spread=0.30))

    if not search_results:
        logger.warning(f"[checker] No results for '{target}' after {max_retries} attempts")
        result["error"] = f"No search results found for '{target}'"
        result["status"] = "not_found"
        result["_vpn_suspect"] = True  # signal to batch loop for VPN retry
        return result

    # The first user result (index 0 in our list, which is index 1 in recycler
    # since index 0 is the keyword suggestion) is always the exact match
    first_result = search_results[0]

    # Validate username matches
    result_username = first_result["username"].lower().strip()
    if result_username != target:
        logger.warning(
            f"[checker] First result '{result_username}' doesn't match target '{target}' — tapping anyway"
        )

    # Tap the first user result
    cx, cy = first_result["center"]
    logger.info(f"[checker] Tapping search result '{first_result['username']}' at ({cx},{cy}) on {device_id}")
    await tap(device_id, cx, cy, jitter=5)

    # Wait for profile to fully load (counters render asynchronously;
    # profiles with music widgets or highlights need extra time)
    await asyncio.sleep(_human_delay(5.0, spread=0.30))

    # ── Step 3: Read profile data ──
    xml = await dump_ui(device_id, safe_mode=True)
    if not xml:
        result["error"] = "Failed to dump UI on profile screen"
        return result

    profile = _parse_profile_data(xml)
    if not profile:
        # Retry once — profile may still be loading
        logger.info(f"[checker] Profile not parsed for '{target}' — waiting and retrying")
        await asyncio.sleep(_human_delay(2.0, spread=0.30))
        xml = await dump_ui(device_id, safe_mode=True)
        if xml:
            profile = _parse_profile_data(xml)

    if not profile:
        result["error"] = "Failed to parse profile data from UI"
        # Still try to detect if we're on the profile screen
        if xml and "action_bar_title" in xml:
            result["error"] = "On profile screen but could not read metrics"
        return result

    # Verify the profile matches the username we searched for
    # Check action_bar_title for username confirmation
    if xml:
        title_nodes = _find_by_resource_id(xml, "action_bar_title")
        if title_nodes:
            profile_username = title_nodes[0]["text"].lower().strip()
            if profile_username and profile_username != target:
                logger.warning(
                    f"[checker] Profile shows '{profile_username}' but expected '{target}'"
                )
                # Update username to actual profile username
                result["username"] = profile_username

    result.update(profile)
    result["status"] = "ok"
    result["error"] = ""

    logger.info(
        f"[checker] {target}: followers={profile['followers']}, "
        f"following={profile['following']}, posts={profile['posts']}, "
        f"private={profile['is_private']}"
    )

    # Organic: sometimes scroll the profile (look at posts) before leaving
    await _maybe_scroll_profile(device_id)

    return result


async def _cleanup_after_profile(device_id: str) -> None:
    """After reading a profile, go back to search and clear the input
    so the next search can begin.
    """
    from shared.adb import press_back

    # Press back to return to search results
    await press_back(device_id)
    await asyncio.sleep(_human_delay(1.0))

    # Clear the search input for the next query
    await _clear_search_input(device_id)
    await asyncio.sleep(_human_delay(0.5))


# ── VPN drop detection & recovery ────────────────────────


async def _check_vpn_and_recover(device_id: str, proxy_enabled: bool) -> bool:
    """Check if VPN is still active; if not, recover the proxy session.

    Recovery flow:
      1. Detect VPN down → press HOME
      2. Re-start proxy via growth/proxy.py (which opens the proxy app)
      3. Verify VPN is back
      4. Press HOME → re-open Instagram
      5. Check if search input is already focused (can continue immediately)
      6. If not, call _navigate_to_search_ready()

    Returns True if VPN is active (or was recovered), False if recovery failed.
    """
    if not proxy_enabled:
        return True

    from shared.adb import is_vpn_active, press_home, dump_ui

    if await is_vpn_active(device_id):
        return True

    # ── VPN is down — start recovery ──
    logger.warning(f"[checker] VPN dropped on {device_id} — starting recovery")

    try:
        # Step 1: Go to HOME first (leave IG)
        await press_home(device_id)
        await asyncio.sleep(_human_delay(1.0, spread=0.25))

        # Step 2: Re-start proxy (this opens proxy app + connects)
        from growth.proxy import start_proxy
        proxy_result = await start_proxy(device_id)
        if not proxy_result["success"]:
            logger.error(
                f"[checker] VPN recovery failed — proxy start error: "
                f"{proxy_result.get('error')} on {device_id}"
            )
            return False
        logger.info(f"[checker] Proxy re-started on {device_id}: {proxy_result['state']}")

        # Step 3: Verify VPN is back
        if not await is_vpn_active(device_id):
            await asyncio.sleep(2.0)
            if not await is_vpn_active(device_id):
                logger.error(f"[checker] VPN still not active after proxy restart on {device_id}")
                return False

        logger.info(f"[checker] VPN recovered on {device_id}")

        # Step 4: Go HOME → re-open Instagram
        await press_home(device_id)
        await asyncio.sleep(_human_delay(1.0, spread=0.25))

        if not await _ensure_instagram_open(device_id):
            logger.error(f"[checker] Could not reopen Instagram after VPN recovery on {device_id}")
            return False
        await asyncio.sleep(_human_delay(2.0, spread=0.30))

        # Step 5: Check if search input is already focused (IG may resume
        # right where we left off — on the search screen with input ready)
        xml = await dump_ui(device_id, safe_mode=True)
        if xml and "action_bar_search_edit_text" in xml:
            logger.info(f"[checker] Search input already focused after recovery on {device_id} — continuing")
            # Clear any leftover text in search
            await _clear_search_input(device_id)
            await asyncio.sleep(_human_delay(0.5))
            return True

        # Step 6: Not on search — navigate there
        logger.info(f"[checker] Navigating back to search after VPN recovery on {device_id}")
        if not await _navigate_to_search_ready(device_id):
            logger.error(f"[checker] Could not navigate to search after VPN recovery on {device_id}")
            return False

        return True

    except Exception as e:
        logger.error(f"[checker] VPN recovery error on {device_id}: {e}", exc_info=True)
        return False


# ── Main scrape function (single account) ────────────────

async def scrape_profile(username: str) -> dict:
    """
    Check a single Instagram account via ADB.

    Opens IG, navigates to search, types username, reads profile.
    Full lifecycle: proxy start → IG open → search → read → cleanup → proxy stop.

    Returns dict compatible with checker DB schema:
      {username, followers, following, posts, bio, is_private,
       full_name, profile_pic, status, error}
    """
    device_id = settings.device_id
    if not device_id:
        return {
            "username": username,
            "followers": None,
            "following": None,
            "posts": None,
            "bio": "",
            "full_name": "",
            "is_private": False,
            "profile_pic": "",
            "status": "error",
            "error": "No device_id configured — set CHECKER_DEVICE_ID in .env",
        }

    proxy_enabled = settings.proxy_enabled

    try:
        from shared.adb import (
            wake_and_unlock, press_home, force_portrait,
            is_proxy_foreground, is_vpn_active,
        )

        # ── Pre-flight: proxy screen check (disabled by default — start_proxy handles it) ──
        if proxy_enabled and settings.preflight_proxy_check:
            if not await is_proxy_foreground(device_id):
                logger.error(f"[checker] Device {device_id} not on proxy screen — skipping")
                return _error_result(username, "Device not on proxy screen")
            logger.info(f"[checker] Pre-flight: proxy screen OK on {device_id}")

        # ── Wake screen ──
        if settings.wake_screen_before_check:
            await wake_and_unlock(device_id)
            await asyncio.sleep(_human_delay(0.6, spread=0.40))

        # ── Rotate proxy IP ──
        if proxy_enabled:
            await _rotate_proxy_ip()

        # ── Start proxy ──
        if proxy_enabled:
            from growth.proxy import start_proxy
            proxy_result = await start_proxy(device_id)
            logger.info(f"[checker] Proxy start result: {proxy_result['state']}")
            if not proxy_result["success"]:
                return _error_result(username, f"Proxy failed: {proxy_result.get('error', 'unknown')}")
            await press_home(device_id)
            await asyncio.sleep(_human_delay(1.0, spread=0.35))

        # ── VPN checkpoint ──
        if proxy_enabled:
            if not await is_vpn_active(device_id):
                logger.warning(f"[checker] VPN not active after proxy start on {device_id}")

        # ── Start scrcpy UHID session ──
        _stop_scrcpy(device_id)   # kill any leftover session
        await _start_scrcpy(device_id)

        # ── Open Instagram ──
        if not await _ensure_instagram_open(device_id):
            return _error_result(username, "Failed to open Instagram")
        await asyncio.sleep(_human_delay(2.0, spread=0.35))

        # ── Navigate to search ──
        if not await _navigate_to_search_ready(device_id):
            return _error_result(username, "Failed to navigate to search")

        # ── Search and read profile ──
        result = await _search_and_read_profile(device_id, username)

        # ── Cleanup: back to search ──
        await _cleanup_after_profile(device_id)

        return result

    except Exception as e:
        logger.error(f"[checker] Error checking {username}: {e}", exc_info=True)
        return _error_result(username, str(e))

    finally:
        # ── Stop scrcpy UHID session ──
        _stop_scrcpy(device_id)

        # ── Cleanup: stop proxy, leave on proxy screen ──
        if proxy_enabled:
            try:
                from growth.proxy import stop_proxy
                stop_result = await stop_proxy(device_id)
                logger.info(f"[checker] Proxy stop result: {stop_result['state']}")
            except Exception as e:
                logger.warning(f"[checker] Cleanup error: {e}")


# ── Batch scrape (multiple accounts, single session) ─────

async def scrape_batch(
    usernames: list[str],
    queue: asyncio.Queue[str] | None = None,
    on_result=None,
) -> list[dict]:
    """
    Check multiple Instagram accounts in a single ADB session.

    Opens IG once, navigates to search, then for each account:
      type username → tap result → read profile → back → clear → next

    If *queue* is provided, the loop drains it after finishing the initial
    list — any usernames added while the batch was running get processed
    in the same IG/scrcpy/proxy session instead of requiring a new one.

    If *on_result* is provided, it is called with each result dict
    immediately after the account is scraped (before moving to the next).
    Signature: ``await on_result(data)``

    Returns list of result dicts (initial + queued).
    """
    device_id = settings.device_id
    if not device_id:
        return [_error_result(u, "No device_id configured") for u in usernames]

    if not usernames:
        return []

    proxy_enabled = settings.proxy_enabled
    results = []
    checked: set[str] = set()  # track already-checked to avoid duplicates

    try:
        from shared.adb import (
            wake_and_unlock, press_back, press_home, force_portrait,
            is_proxy_foreground, is_vpn_active,
        )

        # ── Pre-flight (disabled by default — start_proxy handles it) ──
        if proxy_enabled and settings.preflight_proxy_check:
            if not await is_proxy_foreground(device_id):
                logger.error(f"[checker] Device {device_id} not on proxy screen")
                return [_error_result(u, "Device not on proxy screen") for u in usernames]

        if settings.wake_screen_before_check:
            await wake_and_unlock(device_id)
            await asyncio.sleep(_human_delay(0.6, spread=0.40))

        if proxy_enabled:
            await _rotate_proxy_ip()

        # ── Start proxy (once for entire batch) ──
        if proxy_enabled:
            from growth.proxy import start_proxy
            proxy_result = await start_proxy(device_id)
            logger.info(f"[checker] Proxy start: {proxy_result['state']}")
            if not proxy_result["success"]:
                return [_error_result(u, f"Proxy failed: {proxy_result.get('error')}") for u in usernames]
            await press_home(device_id)
            await asyncio.sleep(_human_delay(1.0, spread=0.35))

        if proxy_enabled:
            if not await is_vpn_active(device_id):
                logger.warning(f"[checker] VPN not active on {device_id}")

        # ── Start scrcpy UHID session (once for entire batch) ──
        _stop_scrcpy(device_id)   # kill any leftover session
        await _start_scrcpy(device_id)

        # ── Open Instagram (once) ──
        if not await _ensure_instagram_open(device_id):
            return [_error_result(u, "Failed to open Instagram") for u in usernames]
        await asyncio.sleep(_human_delay(2.0, spread=0.35))

        # ── Navigate to search (once) ──
        if not await _navigate_to_search_ready(device_id):
            return [_error_result(u, "Failed to navigate to search") for u in usernames]

        # ── Process accounts (initial list + any queued additions) ──
        pending = list(usernames)
        counter = 0

        while pending:
            total_so_far = counter + len(pending)

            for i, username in enumerate(pending):
                counter += 1
                logger.info(f"[checker] [{counter}/{total_so_far}] Checking @{username} on {device_id}")

                if username.lower() in checked:
                    logger.info(f"[checker] @{username} already checked this session — skipping")
                    continue

                result = await _search_and_read_profile(device_id, username)

                # ── VPN suspect: if search returned nothing, check VPN & retry ──
                if result.get("_vpn_suspect") and proxy_enabled:
                    logger.info(f"[checker] Search failed for @{username} — checking VPN health")
                    # Clean up current search state first
                    await _clear_search_input(device_id)
                    await asyncio.sleep(_human_delay(0.5))

                    vpn_ok = await _check_vpn_and_recover(device_id, proxy_enabled)
                    if vpn_ok:
                        logger.info(f"[checker] VPN recovered — retrying @{username}")
                        result = await _search_and_read_profile(device_id, username)
                        result.pop("_vpn_suspect", None)
                    else:
                        logger.error(f"[checker] VPN recovery failed during search for @{username}")
                        result["error"] = "VPN dropped and recovery failed"
                        result["status"] = "error"

                result.pop("_vpn_suspect", None)
                results.append(result)
                checked.add(username.lower())

                # Notify caller immediately (DB save, UI update, etc.)
                if on_result:
                    try:
                        await on_result(result)
                    except Exception as cb_err:
                        logger.warning(f"[checker] on_result callback error for @{username}: {cb_err}")

                # Cleanup after each profile (back + clear input)
                await _cleanup_after_profile(device_id)

                # VPN health check between accounts
                has_more = (i < len(pending) - 1) or (queue and not queue.empty())
                if has_more:
                    vpn_ok = await _check_vpn_and_recover(device_id, proxy_enabled)
                    if not vpn_ok:
                        logger.error(f"[checker] VPN recovery failed — aborting remaining accounts on {device_id}")
                        for remaining_u in pending[i + 1:]:
                            if remaining_u.lower() not in checked:
                                err = _error_result(remaining_u, "Batch aborted: VPN recovery failed")
                                results.append(err)
                                if on_result:
                                    try:
                                        await on_result(err)
                                    except Exception:
                                        pass
                        pending = []  # break outer while
                        break

                    # Random delay between accounts
                    delay = random.uniform(settings.delay_min, settings.delay_max)
                    logger.debug(f"[checker] Waiting {delay:.1f}s before next account")
                    await asyncio.sleep(delay)

            # ── Drain queue for any new additions ──
            pending = []
            if queue:
                while not queue.empty():
                    try:
                        queued_user = queue.get_nowait()
                        if queued_user.lower() not in checked:
                            pending.append(queued_user)
                            logger.info(f"[checker] Picked up queued account: @{queued_user}")
                        else:
                            logger.debug(f"[checker] Queued @{queued_user} already checked — skipping")
                    except asyncio.QueueEmpty:
                        break

            if pending:
                logger.info(f"[checker] Processing {len(pending)} queued account(s): {pending}")

        return results

    except Exception as e:
        logger.error(f"[checker] Batch error after {len(results)}/{len(usernames)} accounts: {e}", exc_info=True)
        # Fill remaining with errors
        for u in usernames:
            if u.lower() not in checked:
                results.append(_error_result(u, f"Batch aborted: {e}"))
        return results

    finally:
        # ── Cleanup: back out of search → IG home, stop scrcpy, stop proxy ──
        try:
            await press_back(device_id)   # search results → explore
            await asyncio.sleep(_human_delay(0.5, spread=0.40))
            await press_back(device_id)   # explore → IG home
            await asyncio.sleep(_human_delay(0.4, spread=0.45))
            await press_back(device_id)   # extra back to ensure IG home
            await asyncio.sleep(_human_delay(0.5, spread=0.35))
            await press_home(device_id)   # device home
        except Exception:
            pass

        # ── Stop scrcpy UHID session ──
        _stop_scrcpy(device_id)

        if proxy_enabled:
            try:
                from growth.proxy import stop_proxy
                stop_result = await stop_proxy(device_id)
                logger.info(f"[checker] Proxy stop: {stop_result['state']}")
            except Exception as e:
                logger.warning(f"[checker] Cleanup error: {e}")


def _error_result(username: str, error: str) -> dict:
    """Create a standardized error result."""
    return {
        "username": username,
        "followers": None,
        "following": None,
        "posts": None,
        "bio": "",
        "full_name": "",
        "is_private": False,
        "profile_pic": "",
        "status": "error",
        "error": error,
    }
