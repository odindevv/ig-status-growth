"""
UI Parser for Account Status screens.

Parses UIAutomator XML dumps to:
  1. Detect which IG screen we're on (navigation aid)
  2. Parse the Account Status summary (dynamic N sections, varies by account type)
  3. Parse detail screens for each section
  4. Detect account state (banned/checkpoint/expired/active)

Signal patterns are loaded from account_status_signals.json
for easy editing without touching code.
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from logger import logger


SIGNALS_FILE = Path(__file__).parent.parent / "account_status_signals.json"

# Loaded at startup, hot-reloadable
_signals: dict = {}


def _load_signals() -> None:
    """Load detection signals from JSON config."""
    global _signals
    try:
        if SIGNALS_FILE.exists():
            _signals = json.loads(SIGNALS_FILE.read_text(encoding="utf-8"))
            logger.info(f"[ui_parser] Loaded signals from {SIGNALS_FILE}")
        else:
            logger.warning(f"[ui_parser] Signals file not found: {SIGNALS_FILE}")
            _signals = {}
    except Exception as e:
        logger.error(f"[ui_parser] Failed to load signals: {e}")
        _signals = {}


def reload_signals() -> None:
    """Hot-reload signals from JSON file."""
    _load_signals()


# Load on import
_load_signals()


def _get_all_nodes(xml_str: str) -> list[dict]:
    """Parse XML and extract all nodes with text and resource-id."""
    nodes = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        logger.warning(f"[ui_parser] XML parse error: {e}")
        return nodes

    for elem in root.iter():
        text = (elem.get("text") or "").strip()
        resource_id = (elem.get("resource-id") or "").strip()
        content_desc = (elem.get("content-desc") or "").strip()
        class_name = (elem.get("class") or "").strip()
        bounds = (elem.get("bounds") or "").strip()
        clickable = elem.get("clickable", "false") == "true"

        if text or resource_id or content_desc:
            nodes.append({
                "text": text,
                "resource_id": resource_id,
                "content_desc": content_desc,
                "class": class_name,
                "bounds": bounds,
                "clickable": clickable,
            })
    return nodes


def _text_contains_any(text: str, patterns: list[str]) -> bool:
    """Check if text contains any of the patterns (case-insensitive)."""
    text_lower = text.lower()
    return any(p.lower() in text_lower for p in patterns)


def _find_node_by_text(nodes: list[dict], patterns: list[str]) -> dict | None:
    """Find the first node whose text matches any pattern."""
    for node in nodes:
        if _text_contains_any(node["text"], patterns):
            return node
        if _text_contains_any(node["content_desc"], patterns):
            return node
    return None


def _find_node_by_resource_id(nodes: list[dict], resource_ids: list[str]) -> dict | None:
    """Find the first node matching any resource ID."""
    for node in nodes:
        for rid in resource_ids:
            if rid in node["resource_id"]:
                return node
    return None


def parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
    """
    Parse UIAutomator bounds string like '[0,0][1080,1920]'
    Returns (left, top, right, bottom) or None.
    """
    try:
        # Format: [left,top][right,bottom]
        parts = bounds_str.replace("][", ",").strip("[]")
        nums = [int(x) for x in parts.split(",")]
        if len(nums) == 4:
            return tuple(nums)
    except Exception:
        pass
    return None


def get_center_of_bounds(bounds_str: str) -> tuple[int, int] | None:
    """Get center point (x, y) from a bounds string."""
    b = parse_bounds(bounds_str)
    if b:
        return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
    return None


# ── Screen detection ───────────────────────────────────────

def _is_compose_settings(xml_str: str) -> bool:
    """Detect Compose-based Settings screen (newer IG versions).

    Jetpack Compose renders Settings with empty View nodes — UIAutomator can't
    read any text.  We identify it by structure:
      1. Contains ComposeView class
      2. Has a "Back" button (content-desc)
      3. Does NOT look like profile (no Edit profile, no profile_tab, no tab_bar)
      4. Has multiple empty android.view.View children (the grid of setting items)
      5. Does NOT have enough readable text items — if items have readable text,
         the normal text-based navigation (with scroll) works fine.
    """
    has_compose = "ComposeView" in xml_str
    if not has_compose:
        return False

    has_back = False
    has_profile_hints = False
    empty_view_count = 0
    readable_text_count = 0

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return False

    for elem in root.iter():
        cd = (elem.get("content-desc") or "").strip().lower()
        text = (elem.get("text") or "").strip().lower()
        rid = (elem.get("resource-id") or "").lower()
        cls = (elem.get("class") or "")

        if cd == "back":
            has_back = True
        # Profile indicators — if present, this is NOT settings
        if any(p in text or p in cd for p in ["edit profile", "editar perfil", "share profile", "compartir perfil"]):
            has_profile_hints = True
        if any(p in rid for p in ["profile_tab_layout", "tab_bar", "profile_header"]):
            has_profile_hints = True
        # Count empty View nodes (Compose settings items with no text)
        if cls == "android.view.View" and not text and not cd:
            empty_view_count += 1
        # Count readable text items (TextView or View with actual text/content-desc)
        # Exclude generic labels like "back", "navigate up"
        if text and text not in ("back", "navigate up", "settings", "configuración"):
            if cls in ("android.widget.TextView", "android.view.View"):
                readable_text_count += 1

    # If there are enough readable text items, the normal text-based navigation
    # with scroll will work — don't flag as compose even if empty views are high.
    if readable_text_count >= 5:
        logger.info(
            f"[ui_parser] ComposeView present but text IS readable "
            f"(readable={readable_text_count}, empty_views={empty_view_count}) "
            f"— using normal text-based navigation"
        )
        return False

    # Compose Settings: has ComposeView + Back button + many empty views + NOT profile
    if has_back and not has_profile_hints and empty_view_count >= 8:
        logger.info(f"[ui_parser] Detected Compose-based Settings screen (empty_views={empty_view_count}, readable={readable_text_count})")
        return True

    return False


def detect_screen(xml_str: str) -> str:
    """
    Detect which Instagram screen is currently shown.
    Returns: 'home', 'profile', 'settings', 'account_status',
             'account_status_detail', 'other', 'unknown'
    """
    nodes = _get_all_nodes(xml_str)
    if not nodes:
        return "unknown"

    # ── Account Status (highest priority) ──
    # The action_bar_title has text="Account Status" and resource-id action_bar_title
    title_node = _find_node_by_resource_id(nodes, ["com.instagram.android:id/action_bar_title"])
    if title_node and _text_contains_any(title_node["text"], ["Account Status", "Estado de la cuenta"]):
        return "account_status"

    # ── Account Status Detail ──
    # Detail screens have "Nothing to worry about" or "Learn more" etc.
    screen_signals = _signals.get("screens", {})
    detail_sigs = screen_signals.get("account_status_detail", {})
    detail_texts = detail_sigs.get("texts", [])
    if detail_texts and _find_node_by_text(nodes, detail_texts):
        # Make sure we're NOT on the account_status summary (which also has "Learn more")
        # Detail screen won't have multiple section buttons visible.
        # Count how many known section labels appear — summary has 3+, detail has 0-1.
        sections_config = _signals.get("sections", {})
        all_label_texts = []
        for sk, sd in sections_config.items():
            if sk.startswith("_") or not isinstance(sd, dict):
                continue
            all_label_texts.extend(sd.get("label_texts", []))

        section_count = 0
        for node in nodes:
            cd = node["content_desc"]
            txt = node["text"]
            for label in all_label_texts:
                if label.lower() in (cd.lower() if cd else "") or label.lower() in (txt.lower() if txt else ""):
                    section_count += 1
                    break
        if section_count < 2:
            return "account_status_detail"

    # ── Other screens (standard priority) ──
    for screen_name in ["settings", "profile", "home"]:
        sigs = screen_signals.get(screen_name, {})
        texts = sigs.get("texts", [])
        resource_ids = sigs.get("resource_ids", [])

        if resource_ids and _find_node_by_resource_id(nodes, resource_ids):
            return screen_name
        if texts and _find_node_by_text(nodes, texts):
            return screen_name

    # ── Compose-based Settings fallback ──
    # Newer IG versions render Settings with Jetpack Compose — UIAutomator can't
    # read any text.  Signature: ComposeView present + "Back" content-desc +
    # NO profile elements (no tab_bar, no Edit profile).
    if _is_compose_settings(xml_str):
        return "settings"

    return "other"


# ── Account Status Summary Parser ──────────────────────────

def parse_account_status_summary(xml_str: str) -> dict:
    """
    Parse the Account Status summary screen DYNAMICALLY.

    Sections vary by account type (personal vs business/creator).
    First tries to match known sections from signals config,
    then discovers unknown sections from clickable elements.

    Status icons (check/X) are ImageViews with NO text — status is determined
    later by reading each section's detail screen.

    Returns dict with N sections (variable):
    {
        "content_removed": {"status": "unknown", "label": "...", "found": true},
        "monetization": {"status": "unknown", "label": "...", "found": true},
        ...
    }
    """
    nodes = _get_all_nodes(xml_str)
    if not nodes:
        return {}

    sections_config = _signals.get("sections", {})
    result = {}
    matched_node_texts = set()  # Track what we already matched

    # Pass 1: Match known sections from config
    for section_key, section_def in sections_config.items():
        if section_key.startswith("_") or not isinstance(section_def, dict):
            continue
        label_patterns = section_def.get("label_texts", [])

        section_data = {
            "status": "unknown",
            "label": section_def.get("display_name", section_key),
            "found": False,
            "raw_text": "",
        }

        label_node = _find_node_by_text(nodes, label_patterns)
        if label_node:
            section_data["found"] = True
            section_data["raw_text"] = label_node["text"] or label_node["content_desc"]
            matched_node_texts.add(section_data["raw_text"].lower())
            result[section_key] = section_data

    # Pass 2: Discover unknown sections from clickable elements on Account Status screen
    # Sections appear as clickable items with content-desc text that describes the section.
    # We look for clickable nodes that we didn't already match and that look like section names.
    # Skip common non-section elements like "Learn more", back buttons, nav tabs, etc.
    skip_patterns = [
        "learn more", "mas informacion", "back", "atras", "options", "opciones",
        "account status", "estado de la cuenta", "profile", "perfil",
        "home", "inicio", "search", "buscar", "reels", "notifications",
        "notificaciones", "close", "cerrar", "settings", "configuracion",
        "help center", "centro de ayuda", "about", "acerca de",
        "thank you", "gracias", "nothing to worry", "no hay nada",
    ]

    for node in nodes:
        text = (node["text"] or node["content_desc"]).strip()
        if not text or len(text) < 5:
            continue
        text_lower = text.lower()
        if text_lower in matched_node_texts:
            continue
        # Skip if text contains any skip pattern
        if any(sp in text_lower for sp in skip_patterns):
            continue
        # Only consider clickable elements in the middle area of the screen (not nav bars)
        bounds = parse_bounds(node["bounds"])
        if not bounds:
            continue
        # Section items are typically in the main content area (y between 300 and 1800)
        center_y = (bounds[1] + bounds[3]) // 2
        if center_y < 200 or center_y > 1900:
            continue
        # Must be clickable or have a clickable-looking class
        if not node["clickable"] and "Button" not in node["class"] and "ViewGroup" not in node["class"]:
            continue
        # Check if this looks like a section name (not a generic UI element)
        # Sections tend to be medium-length descriptive text
        if len(text) < 8 or len(text) > 80:
            continue

        # Create a key from the text
        section_key = _text_to_section_key(text)
        if section_key not in result:
            result[section_key] = {
                "status": "unknown",
                "label": text,
                "found": True,
                "raw_text": text,
                "discovered": True,  # Flag that this was auto-discovered
            }
            logger.info(f"[ui_parser] Discovered unknown section: '{text}' → key='{section_key}'")

    return result


def _text_to_section_key(text: str) -> str:
    """Convert section display text to a snake_case key."""
    key = text.lower().strip()
    key = re.sub(r"[^a-z0-9\s]", "", key)
    key = re.sub(r"\s+", "_", key)
    # Truncate to reasonable length
    if len(key) > 40:
        key = key[:40].rstrip("_")
    return key


# ── Account Status Detail Parser ──────────────────────────

def parse_account_status_detail(xml_str: str, section_key: str = "") -> dict:
    """
    Parse a detail screen opened from one of the 3 Account Status sections.
    Determines the actual ok/problem status from the detail text content.

    Returns:
    {
        "status": "ok" | "problem" | "unknown",
        "texts": [...all visible text...],
        "node_count": int,
    }
    """
    nodes = _get_all_nodes(xml_str)
    if not nodes:
        return {"status": "unknown", "texts": [], "node_count": 0}

    texts = []
    for node in nodes:
        t = node["text"].strip()
        if t and len(t) > 1:
            texts.append(t)

    # Determine status from detail screen text
    status = "unknown"
    sections_config = _signals.get("sections", {})

    if section_key and section_key in sections_config:
        section_def = sections_config[section_key]
        ok_indicators = section_def.get("detail_ok_indicators", [])
        problem_indicators = section_def.get("detail_problem_indicators", [])
    else:
        # Generic indicators — used for dynamically discovered sections
        generic = sections_config.get("_generic_indicators", {})
        ok_indicators = generic.get("detail_ok_indicators", [
            "not affected right now", "not at risk", "Nothing to worry about",
            "No hay nada de que preocuparse", "No issues",
        ])
        problem_indicators = generic.get("detail_problem_indicators", [
            "goes against", "violation", "infraccion", "strike",
            "restricted", "restringido", "limited", "limitado",
            "not eligible", "no elegible",
        ])

    combined_text = " ".join(texts).lower()

    found_ok = any(ind.lower() in combined_text for ind in ok_indicators)
    found_problem = any(ind.lower() in combined_text for ind in problem_indicators)

    matched_ok = [ind for ind in ok_indicators if ind.lower() in combined_text]
    matched_problem = [ind for ind in problem_indicators if ind.lower() in combined_text]

    if found_ok:
        # OK indicators are specific ("not affected right now", "age appropriate guidelines")
        # so if found, trust them — detail screens for ok accounts still contain
        # informational text with words like "warning" or "take down" that are NOT problems.
        status = "ok"
    elif found_problem:
        status = "problem"

    logger.info(f"[detail] section='{section_key}' status={status} ok_matched={matched_ok} problem_matched={matched_problem} texts={texts[:10]}")

    return {
        "status": status,
        "texts": texts,
        "node_count": len(nodes),
    }


# ── Account State Detection (banned/checkpoint/expired/active) ─

def detect_account_state(xml_str: str) -> dict:
    """
    Detect IG account state from UI dump when app opens.
    Priority: banned > checkpoint > expired > active > unknown.

    Returns:
        {"state": "active"|"banned"|"expired"|"checkpoint"|"unknown",
         "matched_signal": str|None, "confidence": "high"|"low"}
    """
    if not xml_str or len(xml_str) < 50:
        return {"state": "unknown", "matched_signal": None, "confidence": "low"}

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return {"state": "unknown", "matched_signal": None, "confidence": "low"}

    # Collect all text and resource-ids
    all_texts = []
    all_ids = []
    for elem in root.iter():
        t = (elem.get("text") or "").strip().lower()
        cd = (elem.get("content-desc") or "").strip().lower()
        rid = (elem.get("resource-id") or "").lower()
        if t:
            all_texts.append(t)
        if cd:
            all_texts.append(cd)
        if rid:
            all_ids.append(rid)

    ids_str = " ".join(all_ids)
    account_state_signals = _signals.get("account_state", {})

    # Check in priority order: banned > checkpoint > expired > active
    for state_name in ["banned", "checkpoint", "expired", "active"]:
        state_def = account_state_signals.get(state_name, {})
        if not isinstance(state_def, dict):
            continue

        for rid_pattern in state_def.get("resource_ids", []):
            if rid_pattern.lower() in ids_str:
                logger.info(f"[state] {state_name.upper()} detected via resource-id: {rid_pattern}")
                return {"state": state_name, "matched_signal": rid_pattern, "confidence": "high"}

        for text_pattern in state_def.get("texts", []):
            if any(text_pattern.lower() in t for t in all_texts):
                logger.info(f"[state] {state_name.upper()} detected via text: '{text_pattern}'")
                return {"state": state_name, "matched_signal": text_pattern, "confidence": "high"}

    logger.warning("[state] No known account state signals found in XML")
    return {"state": "unknown", "matched_signal": None, "confidence": "low"}


def find_clickable_element(xml_str: str, text_patterns: list[str]) -> tuple[int, int] | None:
    """
    Find a clickable element matching text patterns and return its center coords.
    Used for navigation (tapping menu items, settings options, etc).

    Strategy:
      1. Walk the XML tree to find text nodes matching patterns
      2. If the node itself is clickable, use its center
      3. If not, walk up to the nearest clickable ancestor and use ITS center
         (the ancestor spans the full row width → more reliable tap target)
      4. Last resort: use the text node's own center
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None

    # Build child→parent map for ancestor walking
    parent_map = {child: parent for parent in root.iter() for child in parent}

    for elem in root.iter():
        text = (elem.get("text") or "").strip()
        content_desc = (elem.get("content-desc") or "").strip()

        if not (_text_contains_any(text, text_patterns) or _text_contains_any(content_desc, text_patterns)):
            continue

        bounds = (elem.get("bounds") or "").strip()
        clickable = elem.get("clickable", "false") == "true"

        if clickable and bounds:
            coords = get_center_of_bounds(bounds)
            if coords:
                return coords

        # Walk up to nearest clickable ancestor
        ancestor = parent_map.get(elem)
        while ancestor is not None:
            if ancestor.get("clickable", "false") == "true":
                anc_bounds = (ancestor.get("bounds") or "").strip()
                if anc_bounds:
                    coords = get_center_of_bounds(anc_bounds)
                    if coords:
                        logger.debug(f"[ui_parser] Using clickable ancestor bounds for '{text_patterns}'")
                        return coords
            ancestor = parent_map.get(ancestor)

        # No clickable ancestor — use the text node's own bounds
        if bounds:
            coords = get_center_of_bounds(bounds)
            if coords:
                logger.debug(f"[ui_parser] Using text node bounds (no clickable ancestor) for '{text_patterns}'")
                return coords

    return None
