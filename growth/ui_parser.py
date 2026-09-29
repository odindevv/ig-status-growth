"""
UI Parser for Professional Dashboard / Insights screens.

Parses UIAutomator XML dumps to:
  1. Detect which IG screen we're on (navigation aid)
  2. Find the "Professional dashboard" entry on profile
  3. Parse Insights summary metrics (Views, Interactions, New Followers, Content shared)
  4. Parse Views detail screen (trending content breakdown)
"""

import re
import xml.etree.ElementTree as ET
from logger import logger


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
    """Parse UIAutomator bounds string like '[0,0][1080,1920]'."""
    try:
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


def find_clickable_element(xml_str: str, text_patterns: list[str]) -> tuple[int, int] | None:
    """
    Find a clickable element matching text patterns and return its center coords.
    Used for navigation (tapping menu items, dashboard entries, etc).

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

    # Find all elements matching the text patterns
    for elem in root.iter():
        text = (elem.get("text") or "").strip()
        content_desc = (elem.get("content-desc") or "").strip()

        if not (_text_contains_any(text, text_patterns) or _text_contains_any(content_desc, text_patterns)):
            continue

        # Found a matching element
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


# ── Screen Detection ──────────────────────────────────────────

def detect_screen(xml_str: str) -> str:
    """
    Detect which Instagram screen is currently shown.
    Returns: 'profile', 'professional_dashboard', 'insights',
             'views_detail', 'home', 'other', 'unknown'
    """
    nodes = _get_all_nodes(xml_str)
    if not nodes:
        return "unknown"

    # Collect all text for analysis
    all_text = []
    all_descs = []
    all_rids = []
    for node in nodes:
        if node["text"]:
            all_text.append(node["text"].lower())
        if node["content_desc"]:
            all_descs.append(node["content_desc"].lower())
        if node["resource_id"]:
            all_rids.append(node["resource_id"].lower())

    combined_text = " ".join(all_text)
    combined_descs = " ".join(all_descs)
    combined_rids = " ".join(all_rids)

    # ── Views Detail ──
    # When tapped into Views section, shows content breakdown
    # Look for "Views" as title/header + individual content items with view counts
    if _is_views_detail_screen(nodes, combined_text, combined_descs):
        return "views_detail"

    # ── Insights (Professional Dashboard main content) ──
    # Has "Insights" header + date range + metric rows (Views, Interactions, etc.)
    if _is_insights_screen(nodes, combined_text, combined_descs):
        return "insights"

    # ── Professional Dashboard entry ──
    # This is the transitional screen or the dashboard landing with "Professional dashboard" title
    if "professional dashboard" in combined_text or "your dashboard" in combined_text or "panel profesional" in combined_text:
        return "professional_dashboard"

    # ── Home / Feed ──
    # IMPORTANT: Check home BEFORE profile! The bottom nav bar has "Profile" text on
    # EVERY screen, so we must rule out Home first.  Home feed shows: Like, Comment,
    # Send post, and post content.  Profile never shows these.
    home_rids = ["feed_tab", "action_bar_inbox"]
    home_feed_signals = ["like", "comment", "send post", "turn sound on",
                         "me gusta", "comentar", "enviar publicación", "activar sonido"]
    is_home_by_rid = any(rid in combined_rids for rid in home_rids)
    home_signal_count = sum(1 for s in home_feed_signals if s in combined_descs or s in combined_text)
    if is_home_by_rid or home_signal_count >= 2:
        return "home"

    # ── Profile ──
    # Profile-SPECIFIC signals — these only appear on the profile screen, not on home/feed.
    # NOTE: "tab_bar" and "profile_tab_layout" are bottom-nav elements present on ALL screens
    #       so they MUST NOT be used as profile indicators.
    profile_rids = ["profile_header", "profile_header_container", "user_detail_fragment"]
    if any(rid in combined_rids for rid in profile_rids):
        return "profile"
    profile_texts = ["edit profile", "editar perfil", "share profile", "compartir perfil",
                     "professional dashboard", "panel profesional"]
    if any(t in combined_text or t in combined_descs for t in profile_texts):
        return "profile"
    # Secondary: Posts/Followers/Following row is profile-specific
    profile_stats = ["posts", "followers", "following",
                     "publicaciones", "seguidores", "siguiendo"]
    stats_count = sum(1 for s in profile_stats if s in combined_text or s in combined_descs)
    if stats_count >= 2:
        return "profile"

    return "other"


def _is_insights_screen(nodes: list[dict], combined_text: str, combined_descs: str) -> bool:
    """Detect the Insights screen inside Professional Dashboard (both Model A and B)."""
    has_insights = "insights" in combined_text or "estadísticas" in combined_text
    if not has_insights:
        return False

    # ── Model B: has Overview/Content/Audience tabs ──
    model_b_tabs = ["overview", "content", "audience"]
    tab_count = sum(1 for t in model_b_tabs if t in combined_text or t in combined_descs)
    if tab_count >= 2:
        return True

    # ── Model B alternate: "All content" + metric cards ──
    if "all content" in combined_text:
        model_b_metrics = ["views", "net followers", "interactions",
                           "vistas", "seguidores netos", "interacciones"]
        b_count = sum(1 for m in model_b_metrics if m in combined_text or m in combined_descs)
        if b_count >= 2:
            return True

    # ── Model A: traditional row-based metrics ──
    metric_labels = ["views", "interactions", "new followers", "content you shared",
                     "vistas", "interacciones", "nuevos seguidores", "contenido compartido"]
    metric_count = sum(1 for label in metric_labels if label in combined_text or label in combined_descs)

    # Insights screen typically has 2+ metric labels
    return metric_count >= 2


def _is_views_detail_screen(nodes: list[dict], combined_text: str, combined_descs: str) -> bool:
    """Detect the Views detail drill-down screen."""
    # Views detail has a "Views" or "Vistas" title
    # Plus individual content items with view counts
    has_views_title = False
    has_content_items = False

    for node in nodes:
        t = node["text"].lower()
        d = node["content_desc"].lower()
        # Title check — "Views" or "Vistas" as a prominent element
        if t in ("views", "vistas") or d in ("views", "vistas"):
            has_views_title = True
        # Content items typically have view count numbers
        if re.search(r"\d+[kKmM]?\s*(views|vistas|plays)", t) or re.search(r"\d+[kKmM]?\s*(views|vistas|plays)", d):
            has_content_items = True

    return has_views_title and has_content_items


# ── Professional Dashboard Parsers ───────────────────────────

def find_professional_dashboard(xml_str: str) -> tuple[int, int] | None:
    """
    Find the "Professional dashboard" entry on the profile screen.
    It shows as a label with views summary below it (e.g. "2.8k views in the last 30 days").
    Returns center coordinates to tap, or None if not found.
    """
    return find_clickable_element(xml_str, [
        "Professional dashboard",
        "Your dashboard",
        "Panel profesional",
        "Professional Dashboard",
        "Creator experience entry point",
    ])


def parse_insights_summary(xml_str: str) -> dict:
    """
    Parse the Insights screen to extract summary metrics.

    Expected UI structure:
        "Insights   Mar 20 - Apr 18"   (header with date range)
        "Views"                         → number on right
        "Interactions"                  → number on right
        "New followers"                 → number on right
        "Content you shared"            → number on right

    Returns:
    {
        "date_range": "Mar 20 - Apr 18",
        "views": 2800,
        "interactions": 150,
        "new_followers": 42,
        "content_shared": 7,
        "raw_texts": [...],
    }
    """
    nodes = _get_all_nodes(xml_str)
    if not nodes:
        return {}

    result = {
        "date_range": "",
        "views": None,
        "interactions": None,
        "new_followers": None,
        "content_shared": None,
        "raw_texts": [],
    }

    # Collect all text for debugging
    for node in nodes:
        t = node["text"].strip()
        if t:
            result["raw_texts"].append(t)

    # ── Extract date range ──
    # Look for patterns like "Mar 20 - Apr 18" or "Insights   Mar 20 - Apr 18"
    for node in nodes:
        text = node["text"].strip()
        desc = node["content_desc"].strip()
        for t in [text, desc]:
            # Match date range pattern: "Mon DD - Mon DD" or with year
            match = re.search(
                r"([A-Za-z]{3}\s+\d{1,2}\s*[-–]\s*[A-Za-z]{3}\s+\d{1,2}(?:\s*,?\s*\d{4})?)",
                t,
            )
            if match:
                result["date_range"] = match.group(1).strip()
                break
        if result["date_range"]:
            break

    # ── Extract metrics ──
    # Strategy: find metric labels, then look for the nearest number
    # Numbers can be in the same node (content_desc) or in adjacent nodes
    _extract_metrics_from_nodes(nodes, result)

    return result


def _extract_metrics_from_nodes(nodes: list[dict], result: dict) -> None:
    """
    Extract metric values by finding label→value pairs.

    IG Professional Dashboard shows metrics as rows. Each row has:
    - A label (text: "Views", "Interactions", etc.)
    - A value (text: "2.8K", "150", etc.) — either in same node or next to it

    We try two strategies:
    1. content_desc may contain "Views 2,800" (label + value together)
    2. Sequential nodes: label node followed by value node
    """
    metric_map = {
        "views": ["views", "vistas"],
        "interactions": ["interactions", "interacciones"],
        "new_followers": ["new followers", "nuevos seguidores", "followers"],
        "content_shared": ["content you shared", "contenido compartido", "content shared"],
    }

    # Strategy 1: content_desc or text has label + number together
    for node in nodes:
        for check_field in [node["content_desc"], node["text"]]:
            if not check_field:
                continue
            check_lower = check_field.lower()
            for metric_key, patterns in metric_map.items():
                if result[metric_key] is not None:
                    continue
                for pattern in patterns:
                    if pattern in check_lower:
                        # Try to find a number in the same string
                        num = _extract_number_from_text(check_field)
                        if num is not None:
                            result[metric_key] = num
                            logger.debug(f"[insights] {metric_key}={num} (from combined text)")
                        break

    # Strategy 2: Sequential nodes — label at position i, value near position i±1..3
    for i, node in enumerate(nodes):
        text_lower = (node["text"] or node["content_desc"]).lower()
        for metric_key, patterns in metric_map.items():
            if result[metric_key] is not None:
                continue
            if any(p in text_lower for p in patterns):
                # Look at nearby nodes (forward and backward) for a number
                for offset in [1, -1, 2, -2, 3]:
                    j = i + offset
                    if 0 <= j < len(nodes):
                        neighbor = nodes[j]
                        for field in [neighbor["text"], neighbor["content_desc"]]:
                            if field:
                                num = _extract_number_from_text(field)
                                if num is not None:
                                    # Make sure this number node isn't itself a metric label
                                    field_lower = field.lower()
                                    is_label = any(
                                        any(p in field_lower for p in pats)
                                        for pats in metric_map.values()
                                    )
                                    if not is_label:
                                        result[metric_key] = num
                                        logger.debug(f"[insights] {metric_key}={num} (from neighbor node offset={offset})")
                                        break
                        if result[metric_key] is not None:
                            break


def _extract_number_from_text(text: str) -> int | None:
    """
    Extract a number from text, supporting formats like:
    - "2,800" → 2800
    - "2.8K"  → 2800
    - "1.2M"  → 1200000
    - "150"   → 150
    - "2.8k views in the last 30 days" → 2800
    """
    if not text:
        return None

    # Remove commas in numbers
    clean = text.replace(",", "")

    # Match number with optional K/M suffix
    match = re.search(r"(\d+(?:\.\d+)?)\s*([kKmM])?", clean)
    if not match:
        return None

    num_str = match.group(1)
    suffix = (match.group(2) or "").upper()

    try:
        num = float(num_str)
        if suffix == "K":
            num *= 1000
        elif suffix == "M":
            num *= 1000000
        result = int(num)
        if result < 0 or result > 1_000_000_000:  # cap at 1B — sanity guard
            return None
        return result
    except (ValueError, OverflowError):
        return None


# ── Views Detail Parser ──────────────────────────────────────

def _parse_view_count(text: str) -> int | None:
    """Parse a compact view count like '6.5K', '2K', '488', '1.2M' into an integer."""
    if not text:
        return None
    m = re.match(r"^(\d+(?:[.,]\d+)?)\s*([kKmM])?$", text.strip())
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
        suffix = (m.group(2) or "").upper()
        if suffix == "K":
            num *= 1000
        elif suffix == "M":
            num *= 1000000
        return int(num)
    except (ValueError, OverflowError):
        return None


def parse_views_detail(xml_str: str) -> list[dict]:
    """
    Parse the Views detail "By top content" section.

    Real XML structure per content item:
      Button[content-desc="Reels media"]  ← type (Reels/Posts/Stories)
        └─ ViewGroup
            └─ View[text="6.5K"]          ← view count
      View[text="Mar 24"]                 ← date (sibling after the Button's parent)

    Returns list of dicts sorted by views desc:
    [
        {"views": 6500, "type": "Reel", "date": "Mar 24", "rank": 1},
        ...
    ]
    """
    import xml.etree.ElementTree as ET
    items = []

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        logger.warning("[views_detail] Failed to parse XML")
        return items

    # Strategy: find all Button nodes with content-desc containing "media"
    # These are the clickable thumbnails in the "By top content" carousel
    media_type_map = {
        "reels media": "Reel",
        "reel media": "Reel",
        "posts media": "Post",
        "post media": "Post",
        "stories media": "Story",
        "story media": "Story",
        "video media": "Video",
        "carousel media": "Carousel",
        "igtv media": "IGTV",
    }

    for btn in root.iter("node"):
        desc = (btn.get("content-desc", "") or "").strip().lower()
        if "media" not in desc or btn.get("class") != "android.widget.Button":
            continue

        # Determine content type
        content_type = "Unknown"
        for pattern, ctype in media_type_map.items():
            if pattern in desc:
                content_type = ctype
                break

        # Find view count — it's a descendant View/android.view.View with a numeric text
        view_count = None
        for child in btn.iter("node"):
            child_text = (child.get("text", "") or "").strip()
            if child_text:
                vc = _parse_view_count(child_text)
                if vc is not None:
                    view_count = vc
                    break

        if view_count is None:
            continue

        # Find date — it's a sibling View node right after the Button's parent ViewGroup
        # The parent ViewGroup contains: Button (thumbnail) + View (date)
        date_text = ""
        parent_vg = btn.find("..")  # ET doesn't support parent, we'll use the flat approach

        item = {
            "views": view_count,
            "type": content_type,
            "date": date_text,
            "rank": 0,
        }
        items.append(item)

    # Now find dates using flat node approach
    # Dates appear as "Mon DD" pattern right after each thumbnail group
    nodes = _get_all_nodes(xml_str)
    date_pattern = re.compile(
        r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}$", re.IGNORECASE
    )

    date_nodes = []
    for node in nodes:
        text = (node.get("text", "") or "").strip()
        if date_pattern.match(text):
            # Check it's in the right Y range (below 1000 — top content area)
            bounds = node.get("bounds", "")
            bm = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
            if bm:
                y = int(bm.group(2))
                if y > 900:  # Top content items are in the lower part of the screen
                    date_nodes.append(text)

    # Assign dates to items in order
    for idx, item in enumerate(items):
        if idx < len(date_nodes):
            item["date"] = date_nodes[idx]
        item["rank"] = idx + 1

    # Sort by views descending (should already be sorted, but ensure)
    items.sort(key=lambda x: x["views"], reverse=True)
    for idx, item in enumerate(items):
        item["rank"] = idx + 1

    logger.info(f"[views_detail] Extracted {len(items)} content items: "
                + ", ".join(f"#{i['rank']} {i['type']} {i['views']} ({i['date']})" for i in items))
    return items


# ── Model B Detection & Parsing ─────────────────────────────


def detect_insights_model(xml_str: str) -> str:
    """
    Detect which Insights UI model is shown.

    Model B (new): Has tabs "Overview" / "Content" / "Audience" at the top.
                   Metrics shown as horizontal cards (Views, Net followers, Interactions).
                   "Accounts reached" appears on scroll.

    Model A (old): Metrics listed as rows directly (Views, Interactions, New followers, Content shared).
                   No tab bar.

    Returns "A" or "B".
    """
    nodes = _get_all_nodes(xml_str)
    if not nodes:
        return "A"  # Default to A if we can't parse

    all_text_lower = []
    all_desc_lower = []
    for node in nodes:
        if node["text"]:
            all_text_lower.append(node["text"].lower())
        if node["content_desc"]:
            all_desc_lower.append(node["content_desc"].lower())

    combined = " ".join(all_text_lower + all_desc_lower)

    # Model B has Overview/Content/Audience tabs — each as a standalone text node.
    # Use exact match against individual node texts to avoid false positives
    # from phrases like "Content you shared" or "reach new audiences".
    model_b_tabs = {"overview", "content", "audience"}
    all_individual = set(all_text_lower + all_desc_lower)
    tab_count = len(model_b_tabs & all_individual)
    if tab_count >= 2:
        logger.info(f"[ui_model] Detected Model B (found {tab_count}/3 tabs as exact node text)")
        return "B"

    # Model B also has "All content" + "30 days" dropdown
    if "all content" in combined and "days" in combined:
        logger.info("[ui_model] Detected Model B (found 'All content' + 'days')")
        return "B"

    # Model B has "Net followers" (vs Model A which has "New followers")
    if "net followers" in combined:
        logger.info("[ui_model] Detected Model B (found 'Net followers')")
        return "B"

    logger.info("[ui_model] Detected Model A (no Model B indicators found)")
    return "A"


def parse_insights_model_b(xml_str: str) -> dict:
    """
    Parse the Model B Insights Overview screen.

    Expected UI structure (horizontal cards):
        "Views"          → number below (e.g. "0", "2.8K")
        "Net followers"  → number below (e.g. "--", "42")
        "Interactions"   → number below (e.g. "0", "150")

    Also looks for:
        "All content" + "30 days" header
        Date range if present

    Returns:
    {
        "date_range": "30 days",
        "views": 0,
        "interactions": 0,
        "new_followers": None,  (None if "--")
        "raw_texts": [...],
    }
    """
    nodes = _get_all_nodes(xml_str)
    if not nodes:
        return {}

    result = {
        "date_range": "",
        "views": None,
        "interactions": None,
        "new_followers": None,
        "raw_texts": [],
    }

    # Collect raw texts for debugging
    for node in nodes:
        t = node["text"].strip()
        if t:
            result["raw_texts"].append(t)

    # ── Extract date range ──
    # Model B shows "30 days" dropdown or a date range
    for node in nodes:
        text = node["text"].strip()
        desc = node["content_desc"].strip()
        for t in [text, desc]:
            # Match "30 days", "7 days", "90 days" etc.
            if re.search(r"\d+\s*days", t, re.IGNORECASE):
                result["date_range"] = t.strip()
                break
            # Also check for traditional date range format
            match = re.search(
                r"([A-Za-z]{3}\s+\d{1,2}\s*[-–]\s*[A-Za-z]{3}\s+\d{1,2}(?:\s*,?\s*\d{4})?)",
                t,
            )
            if match:
                result["date_range"] = match.group(1).strip()
                break
        if result["date_range"]:
            break

    # ── Extract card metrics ──
    # Model B cards: label text on one node, value on a nearby node (usually next)
    # Labels: "Views", "Net followers", "Interactions" (may be truncated as "Inter...")
    metric_map = {
        "views": ["views", "vistas"],
        "new_followers": ["net followers", "net follower", "seguidores netos"],
        "interactions": ["interactions", "inter…", "inter...", "interacciones"],
    }

    _extract_model_b_cards(nodes, result, metric_map)

    return result


def _extract_model_b_cards(nodes: list[dict], result: dict, metric_map: dict) -> None:
    """
    Extract metric values from Model B horizontal cards.

    Strategy:
    1. Find the label node (e.g. "Views")
    2. Look at nearby nodes (forward) for a number value
    3. Handle "--" as None (no data available)
    """
    for i, node in enumerate(nodes):
        text_lower = (node["text"] or "").strip().lower()
        desc_lower = (node["content_desc"] or "").strip().lower()

        for metric_key, patterns in metric_map.items():
            if result[metric_key] is not None:
                continue

            matched = False
            for p in patterns:
                if p in text_lower or p in desc_lower:
                    matched = True
                    break

            if not matched:
                continue

            # Found a label — look at nearby nodes for the value
            # In Model B, the number is usually directly below (next node or within 1-3 nodes)
            for offset in [1, -1, 2, -2, 3]:
                j = i + offset
                if 0 <= j < len(nodes):
                    neighbor = nodes[j]
                    for field in [neighbor["text"], neighbor["content_desc"]]:
                        if not field or not field.strip():
                            continue
                        field = field.strip()

                        # Handle "--" as None (no data)
                        if field in ("--", "—", "-", "- -"):
                            result[metric_key] = None
                            logger.debug(f"[model_b] {metric_key}=None (displayed as '{field}')")
                            # Mark as "checked" so we don't keep looking
                            # Use a sentinel to distinguish "no data" from "not found"
                            break

                        # Skip if this is another metric label
                        field_lower = field.lower()
                        is_label = any(
                            any(p in field_lower for p in pats)
                            for pats in metric_map.values()
                        )
                        if is_label:
                            continue

                        # Skip non-metric text (e.g. "All content", "Overview", etc.)
                        skip_texts = ["all content", "overview", "content", "audience",
                                      "days", "followers", "non-followers"]
                        if any(s in field_lower for s in skip_texts):
                            continue

                        # Try to extract a number
                        num = _extract_number_from_text(field)
                        if num is not None:
                            result[metric_key] = num
                            logger.debug(f"[model_b] {metric_key}={num} (from '{field}')")
                            break

                    if result[metric_key] is not None:
                        break


def parse_accounts_reached(xml_str: str) -> int | None:
    """
    Parse the "Accounts reached" metric from a scrolled Insights screen.

    The UI shows:
        "Accounts reached" (section header, bold)
        "Accounts reached"  0    (icon + label + number on the right)

    Returns the integer value or None if not found.
    """
    nodes = _get_all_nodes(xml_str)
    if not nodes:
        return None

    for i, node in enumerate(nodes):
        text_lower = (node["text"] or "").strip().lower()
        desc_lower = (node["content_desc"] or "").strip().lower()

        if "accounts reached" not in text_lower and "accounts reached" not in desc_lower:
            if "cuentas alcanzadas" not in text_lower and "cuentas alcanzadas" not in desc_lower:
                continue

        # Found label — look for a number nearby
        # First check if number is in the same node (content_desc may have "Accounts reached 0")
        for field in [node["text"], node["content_desc"]]:
            if field:
                num = _extract_number_from_text(field)
                if num is not None:
                    logger.info(f"[accounts_reached] Found value {num} in same node")
                    return num

        # Check neighboring nodes for the number
        for offset in [1, 2, -1, 3]:
            j = i + offset
            if 0 <= j < len(nodes):
                neighbor = nodes[j]
                for field in [neighbor["text"], neighbor["content_desc"]]:
                    if not field or not field.strip():
                        continue
                    field = field.strip()

                    # Skip if this is another section label
                    if "accounts reached" in field.lower() or "cuentas alcanzadas" in field.lower():
                        continue
                    if "interactions" in field.lower() or "views" in field.lower():
                        continue

                    num = _extract_number_from_text(field)
                    if num is not None:
                        logger.info(f"[accounts_reached] Found value {num} from neighbor (offset={offset})")
                        return num

    logger.debug("[accounts_reached] Not found in XML")
    return None


# ── Tier 1: Views Detail Parser ──────────────────────────────

def _extract_pct_from_text(text: str) -> float | None:
    """Extract percentage from text like '6.6%', '93.4%', '99.6%'."""
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if match:
        return float(match.group(1))
    return None


def parse_views_extended(xml: str) -> dict:
    """
    Parse extended data from the Views detail drill-down screen.
    Captures:
      - followers_pct / non_followers_pct
      - content_type: {reels_pct, posts_pct, stories_pct}
      - accounts_reached (+ change_pct)
      - profile_activity, profile_visits, external_link_taps
    """
    nodes = _get_all_nodes(xml)
    result = {
        "followers_pct": None,
        "non_followers_pct": None,
        "content_type": {},
        "profile_visits": None,
        "external_link_taps": None,
        "profile_activity": None,
    }

    for i, node in enumerate(nodes):
        text = (node.get("text", "") or "").strip()
        text_lower = text.lower()

        # ── Followers / Non-followers percentages ──
        if text_lower in ("followers", "seguidores") and i + 1 < len(nodes):
            # Check if the NEXT node on the same line has a percentage
            for offset in range(1, 4):
                if i + offset >= len(nodes):
                    break
                neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                pct = _extract_pct_from_text(neighbor_text)
                if pct is not None and result["followers_pct"] is None:
                    result["followers_pct"] = pct
                    break

        if text_lower.startswith("non-follower") or text_lower.startswith("no seguidores"):
            for offset in range(1, 4):
                if i + offset >= len(nodes):
                    break
                neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                pct = _extract_pct_from_text(neighbor_text)
                if pct is not None and result["non_followers_pct"] is None:
                    result["non_followers_pct"] = pct
                    break

        # ── Content type breakdown (Reels, Posts, Stories) ──
        if text_lower in ("reels",):
            for offset in range(1, 4):
                if i + offset >= len(nodes):
                    break
                neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                pct = _extract_pct_from_text(neighbor_text)
                if pct is not None:
                    result["content_type"]["reels_pct"] = pct
                    break

        if text_lower in ("posts", "publicaciones"):
            for offset in range(1, 4):
                if i + offset >= len(nodes):
                    break
                neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                pct = _extract_pct_from_text(neighbor_text)
                if pct is not None:
                    result["content_type"]["posts_pct"] = pct
                    break

        if text_lower in ("stories", "historias"):
            for offset in range(1, 4):
                if i + offset >= len(nodes):
                    break
                neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                pct = _extract_pct_from_text(neighbor_text)
                if pct is not None:
                    result["content_type"]["stories_pct"] = pct
                    break

        # ── Profile activity ──
        if "profile activity" in text_lower or "actividad del perfil" in text_lower:
            for offset in range(1, 4):
                if i + offset >= len(nodes):
                    break
                neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                num = _extract_number_from_text(neighbor_text)
                if num is not None:
                    result["profile_activity"] = num
                    break

        # ── Profile visits ──
        if text_lower in ("profile visits", "visitas al perfil"):
            for offset in range(1, 4):
                if i + offset >= len(nodes):
                    break
                neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                num = _extract_number_from_text(neighbor_text)
                if num is not None:
                    result["profile_visits"] = num
                    break

        # ── External link taps ──
        if "external link" in text_lower or "enlace externo" in text_lower:
            for offset in range(1, 4):
                if i + offset >= len(nodes):
                    break
                neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                num = _extract_number_from_text(neighbor_text)
                if num is not None:
                    result["external_link_taps"] = num
                    break

    has_data = any(v is not None for k, v in result.items() if k != "content_type") or result["content_type"]
    if has_data:
        logger.info(f"[views_ext] Parsed: followers={result['followers_pct']}%, non_followers={result['non_followers_pct']}%, "
                     f"content={result['content_type']}, profile_visits={result['profile_visits']}, link_taps={result['external_link_taps']}")
    return result


# ── Audience demographics (Views detail screen, peek mode) ───
# Different from parse_followers_detail (which parses the Followers
# drill-down screen). This parses the "Audience" widget that appears
# below "By top content" in the Views detail screen — Top cities with
# percentages on the left, Top countries with names only on the right.

def parse_audience_demographics(xml_str: str) -> dict | None:
    """Extract Top cities (with %) and Top countries (names + rank).

    Returns None when:
      - IG explicitly marks the section as "Currently unavailable due to
        a lack of data" (small accounts that don't meet the threshold)
      - Neither section is found in the XML (audience block off-screen)
      - XML parsing fails

    Returns dict with shape:
      {
        "top_cities": [{"name": "Melbourne", "pct": 0.7}, ...],
        "top_countries": [{"name": "United States", "rank": 1}, ...]
      }

    The right-side countries column in IG's "peek mode" renders names
    but not absolute percentages (no horizontal real-estate). For an
    absolute % per country, IG requires a horizontal swipe — outside
    scope of this parser. We capture ranking only.
    """
    if not xml_str:
        return None

    # IG explicit "no data" marker — account doesn't meet demographics threshold
    if "Currently unavailable due to a lack of data" in xml_str:
        return None

    try:
        nodes = _get_all_nodes(xml_str)
    except Exception:
        return None

    # Extract text nodes in document order (skip empties)
    texts: list[str] = []
    for n in nodes:
        t = (n.get("text", "") or "").strip()
        if t:
            texts.append(t)

    if not texts:
        return None

    top_cities: list[dict] = []
    top_countries: list[dict] = []
    pct_re = re.compile(r"^(\d+(?:\.\d+)?)%$")

    state: str | None = None  # 'cities' | 'countries' | None
    pending_name: str | None = None

    for t in texts:
        tl = t.lower()
        # Section boundaries
        if tl in ("top cities", "ciudades principales", "ciudades"):
            state = "cities"
            pending_name = None
            continue
        if tl in ("top countries", "países principales", "paises principales",
                  "países", "paises"):
            state = "countries"
            pending_name = None
            continue
        # Stop if we hit the next major section
        if tl in ("profile activity", "actividad del perfil"):
            break

        if state == "cities":
            m = pct_re.match(t)
            if m and pending_name:
                try:
                    top_cities.append({"name": pending_name, "pct": float(m.group(1))})
                except ValueError:
                    pass
                pending_name = None
            elif not m:
                # Treat as city name (overwrite any orphan pending name)
                pending_name = t

        elif state == "countries":
            # Countries column shows only names in peek mode; no pct text
            if not pct_re.match(t) and len(top_countries) < 5:
                top_countries.append({"name": t, "rank": len(top_countries) + 1})

    # Cap cities at 5 (IG usually shows top 4-5)
    top_cities = top_cities[:5]

    if not top_cities and not top_countries:
        return None

    return {"top_cities": top_cities, "top_countries": top_countries}


# ── Tier 2: Followers Detail Parser ──────────────────────────

def parse_followers_detail(xml: str) -> dict:
    """
    Parse extended data from the Followers detail drill-down screen.
    Captures:
      - total_followers
      - follows, unfollows, net_growth (Overall)
      - top_countries: [{name, pct}, ...]
      - top_cities: [{name, pct}, ...]
      - gender: {men_pct, women_pct}
      - top_age_range: {range, pct}  (the highest percentage age bracket)
    """
    nodes = _get_all_nodes(xml)
    result = {
        "total_followers": None,
        "follows": None,
        "unfollows": None,
        "net_growth": None,
        "top_countries": [],
        "top_cities": [],
        "gender": {},
        "age_ranges": [],
    }

    # Collect all text values for section detection
    section = None  # Track which section we're in

    for i, node in enumerate(nodes):
        text = (node.get("text", "") or "").strip()
        text_lower = text.lower()

        # ── Total followers (big number at top) ──
        # Pattern: "7,466" then "Followers" label below
        if text_lower == "followers" or text_lower == "seguidores":
            # Check the node BEFORE for the total number
            if i > 0:
                prev_text = (nodes[i - 1].get("text", "") or "").strip()
                num = _extract_number_from_text(prev_text)
                if num is not None:
                    result["total_followers"] = num

        # ── Section markers ──
        if text_lower == "growth" or text_lower == "crecimiento":
            section = "growth"
            continue
        if "top cities" in text_lower or "ciudades" in text_lower:
            section = "cities"
            continue
        if "top countries" in text_lower or "países" in text_lower or "top c" in text_lower:
            # "Top c..." might be truncated "Top countries"
            if section == "cities" or "countr" in text_lower or "país" in text_lower:
                section = "countries"
                continue
        if "age range" in text_lower or "rango de edad" in text_lower:
            section = "age"
            continue
        if text_lower == "gender" or text_lower == "género":
            section = "gender"
            continue
        if "follower details" in text_lower or "top content" in text_lower or "top locations" in text_lower:
            # Reset section on new headers
            if "top locations" in text_lower:
                section = "cities"  # Top locations starts with cities
            continue

        # ── Growth section: Overall, Follows, Unfollows ──
        if section == "growth":
            if text_lower in ("overall", "general"):
                for offset in range(1, 3):
                    if i + offset >= len(nodes):
                        break
                    neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                    # Can be negative like "-72"
                    match = re.search(r"(-?\d[\d,]*)", neighbor_text.replace(",", ""))
                    if match:
                        result["net_growth"] = int(match.group(1))
                        break

            if text_lower in ("follows", "seguimientos"):
                for offset in range(1, 3):
                    if i + offset >= len(nodes):
                        break
                    neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                    num = _extract_number_from_text(neighbor_text)
                    if num is not None:
                        result["follows"] = num
                        break

            if text_lower in ("unfollows", "dejaron de seguir"):
                for offset in range(1, 3):
                    if i + offset >= len(nodes):
                        break
                    neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                    num = _extract_number_from_text(neighbor_text)
                    if num is not None:
                        result["unfollows"] = num
                        break

        # ── Cities section ──
        if section == "cities" and _extract_pct_from_text(text) is None:
            # This might be a city name — check if next node has a percentage
            if text and not text.startswith(("Top", "Cities", "Countries")) and len(text) > 1:
                for offset in range(1, 3):
                    if i + offset >= len(nodes):
                        break
                    neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                    pct = _extract_pct_from_text(neighbor_text)
                    if pct is not None and len(result["top_cities"]) < 5:
                        result["top_cities"].append({"name": text, "pct": pct})
                        break

        # ── Countries section ──
        if section == "countries" and _extract_pct_from_text(text) is None:
            if text and not text.startswith(("Top", "Cities", "Countries")) and len(text) > 1:
                for offset in range(1, 3):
                    if i + offset >= len(nodes):
                        break
                    neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                    pct = _extract_pct_from_text(neighbor_text)
                    if pct is not None and len(result["top_countries"]) < 5:
                        result["top_countries"].append({"name": text, "pct": pct})
                        break

        # ── Age ranges ──
        if section == "age":
            # Age brackets look like "13-17", "18-24", "25-34", "35-44", "45-54", "55-64", "65+"
            age_match = re.match(r"^(\d{2}[-–]\d{2}|\d{2}\+)$", text)
            if age_match:
                for offset in range(1, 3):
                    if i + offset >= len(nodes):
                        break
                    neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                    pct = _extract_pct_from_text(neighbor_text)
                    if pct is not None:
                        result["age_ranges"].append({"range": text, "pct": pct})
                        break

        # ── Gender ──
        if section == "gender":
            if text_lower in ("men", "hombres"):
                for offset in range(1, 3):
                    if i + offset >= len(nodes):
                        break
                    neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                    pct = _extract_pct_from_text(neighbor_text)
                    if pct is not None:
                        result["gender"]["men_pct"] = pct
                        break
            if text_lower in ("women", "mujeres"):
                for offset in range(1, 3):
                    if i + offset >= len(nodes):
                        break
                    neighbor_text = (nodes[i + offset].get("text", "") or "").strip()
                    pct = _extract_pct_from_text(neighbor_text)
                    if pct is not None:
                        result["gender"]["women_pct"] = pct
                        break

    # Derive top age range
    if result["age_ranges"]:
        top = max(result["age_ranges"], key=lambda x: x["pct"])
        result["top_age_range"] = top

    has_data = (result["total_followers"] is not None or result["follows"] is not None
                or result["top_countries"] or result["gender"])
    if has_data:
        logger.info(f"[followers_ext] Parsed: total={result['total_followers']}, follows={result['follows']}, "
                     f"unfollows={result['unfollows']}, net={result['net_growth']}, "
                     f"countries={len(result['top_countries'])}, gender={result['gender']}, "
                     f"age_ranges={len(result['age_ranges'])}")
    return result


# ── Video Detail Parser ──────────────────────────────────────


def parse_video_detail(xml_str: str) -> dict:
    """
    Parse a single video detail / Insights screen.

    Expected UI structure (from tapping a video in top content):
      - Title text node (e.g. "Untitled" or a custom creator title)
      - Date + Duration text node (e.g. "March 24 · Duration 0:07")
      - Engagement content-desc nodes: "71 Likes", "0 Comments", "27 Shares", etc.
      - Overview section with text nodes: Views count, Watch time, Interactions, Profile activity

    Returns dict with keys:
      title, date, duration, likes, comments, shares, reposts, saves,
      views, watch_time, interactions, profile_activity
    """
    result = {
        "title": "",
        "date": "",
        "duration": "",
        "likes": None,
        "comments": None,
        "shares": None,
        "reposts": None,
        "saves": None,
        "views": None,
        "watch_time": "",
        "interactions": None,
        "profile_activity": None,
    }

    nodes = _get_all_nodes(xml_str)
    if not nodes:
        return result

    # ── 1. Engagement metrics from content-desc ("71 Likes", "0 Comments", etc.) ──
    engagement_map = {
        "likes": re.compile(r"^([\d,\.]+)\s+likes?$", re.IGNORECASE),
        "comments": re.compile(r"^([\d,\.]+)\s+comments?$", re.IGNORECASE),
        "shares": re.compile(r"^([\d,\.]+)\s+shares?$", re.IGNORECASE),
        "reposts": re.compile(r"^([\d,\.]+)\s+reposts?$", re.IGNORECASE),
        "saves": re.compile(r"^([\d,\.]+)\s+saves?$", re.IGNORECASE),
    }

    for node in nodes:
        desc = node.get("content_desc", "")
        if not desc:
            continue
        for key, pattern in engagement_map.items():
            m = pattern.match(desc.strip())
            if m:
                result[key] = _extract_number_from_text(m.group(1))
                break

    # ── 2. Title — text node near the top, not a metric/date/navigation label ──
    # The title appears after the carrusel thumbnails and before the date line.
    # Strategy: find the "Month DD · Duration" node, then look backwards for the title.
    date_duration_idx = None
    date_duration_re = re.compile(
        r"^([A-Za-z]+\s+\d{1,2})\s*·\s*Duration\s+(.+)$", re.IGNORECASE
    )

    for i, node in enumerate(nodes):
        text = node.get("text", "")
        m = date_duration_re.match(text.strip())
        if m:
            result["date"] = m.group(1).strip()
            result["duration"] = m.group(2).strip()
            date_duration_idx = i
            break

    # Title: look backwards from date line for a non-empty text that isn't a known label
    _skip_labels = {
        "insights", "overview", "reels media", "back", "share",
        "boost reel", "view more insights", "",
    }
    if date_duration_idx is not None:
        for j in range(date_duration_idx - 1, max(date_duration_idx - 6, -1), -1):
            if j < 0:
                break
            candidate = (nodes[j].get("text", "") or "").strip()
            if candidate and candidate.lower() not in _skip_labels:
                # Skip if it looks like a number-only node or engagement metric
                if re.match(r"^[\d,\.]+$", candidate):
                    continue
                result["title"] = candidate
                break

    # ── 3. Overview section — Views, Watch time, Interactions, Profile activity ──
    # These appear as label → value pairs in sequence.
    overview_labels = {
        "views": "views",
        "watch time": "watch_time",
        "interactions": "interactions",
        "profile activity": "profile_activity",
    }

    for i, node in enumerate(nodes):
        text_lower = (node.get("text", "") or "").strip().lower()
        if text_lower in overview_labels:
            field = overview_labels[text_lower]
            # Look at next 1-2 nodes for the value
            for offset in range(1, 3):
                if i + offset >= len(nodes):
                    break
                val_text = (nodes[i + offset].get("text", "") or "").strip()
                if not val_text:
                    continue
                # Watch time is a string like "6h 50m 33s"
                if field == "watch_time":
                    result["watch_time"] = val_text
                    break
                # Numeric fields
                num = _extract_number_from_text(val_text)
                if num is not None:
                    result[field] = num
                    break

    has_data = (
        result["title"]
        or result["views"] is not None
        or result["likes"] is not None
    )
    if has_data:
        logger.info(
            f"[video_detail] Parsed: title={result['title']!r}, "
            f"views={result['views']}, likes={result['likes']}, "
            f"comments={result['comments']}, shares={result['shares']}, "
            f"duration={result['duration']}"
        )

    return result


def detect_video_detail_screen(xml_str: str) -> bool:
    """Check if current screen is a video detail / Insights screen.

    IG varies the header text across account types and devices:
      - Small accounts / Samsung often show "Insights"
      - Large accounts / Pixel often show "Ad insights" (when account
        has ad-related metrics history)
      - Future variants like "Reel insights" or "Post insights" should
        also match.

    Use substring match with a length guard to avoid false positives on
    longer text where "insights" might appear inside descriptive copy.
    The `has_overview OR has_engagement` second condition still ensures
    we're actually on a metrics screen, not just any screen mentioning
    the word.
    """
    nodes = _get_all_nodes(xml_str)
    has_insights_title = False
    has_overview = False
    has_engagement = False

    for node in nodes:
        text = (node.get("text", "") or "").strip().lower()
        desc = (node.get("content_desc", "") or "").strip().lower()

        if "insights" in text and len(text) <= 30:
            has_insights_title = True
        if text in ("overview", "watch time", "profile activity"):
            has_overview = True
        if re.match(r"^\d[\d,]*\s+(likes?|comments?|shares?|saves?)$", desc):
            has_engagement = True

    return has_insights_title and (has_overview or has_engagement)


# ── Trial Reels Parsers ──────────────────────────────────────


def detect_trial_reels_button(xml_str: str) -> tuple[int, int] | None:
    """
    Detect 'Trial reels' button on Professional Dashboard.
    Returns center (x, y) or None if not found.

    First tries clickable nodes, then falls back to any matching node
    (text labels in IG are often not marked clickable themselves).
    """
    nodes = _get_all_nodes(xml_str)
    fallback_center = None
    for node in nodes:
        text = (node.get("text", "") or "").strip().lower()
        desc = (node.get("content_desc", "") or "").strip().lower()
        if "trial reel" in text or "trial reel" in desc:
            bounds = node.get("bounds", "")
            if not bounds:
                continue
            center = get_center_of_bounds(bounds)
            if not center:
                continue
            clickable = node.get("clickable", False)
            if clickable:
                return center
            if fallback_center is None:
                fallback_center = center
    return fallback_center


def detect_trial_reels_screen(xml_str: str) -> bool:
    """Check if we're on the Trial Reels grid screen."""
    nodes = _get_all_nodes(xml_str)
    for node in nodes:
        rid = node.get("resource_id", "")
        text = (node.get("text", "") or "").strip()
        if "trials_fragment_container" in rid:
            return True
        if "trials_list" in rid:
            return True
        if rid.endswith("action_bar_title") and text == "Trial reels":
            return True
    return False


def parse_trial_grid(xml_str: str) -> list[dict]:
    """
    Parse the trial reels grid to extract thumbnails with their view counts.
    Returns list of {reel_index, grid_views, bounds, cx, cy} — max 5 from first row.
    """
    nodes = _get_all_nodes(xml_str)
    reels = []

    # Find all draft_entrypoint_container (each is a reel thumbnail)
    containers = []
    for node in nodes:
        rid = node.get("resource_id", "")
        if "draft_entrypoint_container" in rid and node.get("clickable", False):
            bounds = node.get("bounds", "")
            center = get_center_of_bounds(bounds)
            parsed = parse_bounds(bounds)
            if center and parsed:
                containers.append({
                    "cx": center[0],
                    "cy": center[1],
                    "bounds": bounds,
                    "x1": parsed[0],
                    "y1": parsed[1],
                })

    if not containers:
        return []

    # Sort by position: left to right, top to bottom
    containers.sort(key=lambda c: (c["y1"], c["x1"]))

    # Only first row (same y1 range, within 50px tolerance)
    first_y = containers[0]["y1"]
    first_row = [c for c in containers if abs(c["y1"] - first_y) < 50]

    # Find play counts for each container
    play_counts = []
    for node in nodes:
        rid = node.get("resource_id", "")
        if "preview_clip_play_count" in rid:
            text = (node.get("text", "") or "").strip()
            bounds = node.get("bounds", "")
            center = get_center_of_bounds(bounds)
            if text and center:
                views = _parse_view_count(text)
                play_counts.append({"views": views, "cx": center[0], "cy": center[1]})

    # Match play counts to containers by proximity
    for idx, container in enumerate(first_row[:5]):
        grid_views = None
        # Find closest play count to this container
        best_dist = 9999
        for pc in play_counts:
            dist = abs(pc["cx"] - container["cx"]) + abs(pc["cy"] - container["cy"])
            if dist < best_dist:
                best_dist = dist
                grid_views = pc["views"]

        reels.append({
            "reel_index": idx,
            "grid_views": grid_views,
            "cx": container["cx"],
            "cy": container["cy"],
            "bounds": container["bounds"],
        })

    return reels


def detect_trial_reel_player(xml_str: str) -> bool:
    """Check if we're on a trial reel video player screen."""
    nodes = _get_all_nodes(xml_str)
    for node in nodes:
        text = (node.get("text", "") or "").strip()
        if "Trial reel" in text and "View insights" in text:
            return True
    return False


def find_trial_view_insights_button(xml_str: str) -> tuple[int, int] | None:
    """Find the 'Trial reel · View insights' button on the reel player."""
    nodes = _get_all_nodes(xml_str)
    for node in nodes:
        text = (node.get("text", "") or "").strip()
        if "Trial reel" in text and "View insights" in text:
            bounds = node.get("bounds", "")
            if bounds:
                return get_center_of_bounds(bounds)
    return None


def parse_trial_popup(xml_str: str) -> dict | None:
    """
    Parse the 'Trial reel insights' popup.
    Returns {views, likes, comments, shares} or None if not found.
    """
    nodes = _get_all_nodes(xml_str)
    has_popup = False
    metric_values = []

    for node in nodes:
        text = (node.get("text", "") or "").strip()
        rid = node.get("resource_id", "")

        if text == "Trial reel insights":
            has_popup = True
        if "trial_metric_value" in rid and text:
            val = _parse_view_count(text)
            metric_values.append(val)

    if not has_popup or len(metric_values) < 4:
        return None

    # Order from XML: views, likes, comments, shares
    return {
        "views": metric_values[0],
        "likes": metric_values[1],
        "comments": metric_values[2],
        "shares": metric_values[3],
    }


def find_trial_popup_view_insights(xml_str: str) -> tuple[int, int] | None:
    """Find the 'View insights' button inside the trial popup."""
    nodes = _get_all_nodes(xml_str)
    for node in nodes:
        text = (node.get("text", "") or "").strip()
        if text == "View insights":
            bounds = node.get("bounds", "")
            if bounds:
                return get_center_of_bounds(bounds)
    return None


def detect_trial_unavailable(xml_str: str) -> bool:
    """Detect if trial reel shows 'take longer to start getting views' message."""
    nodes = _get_all_nodes(xml_str)
    for node in nodes:
        text = (node.get("text", "") or "").strip().lower()
        rid = node.get("resource_id", "")
        if "take longer" in text and "views" in text:
            return True
        if "unrevealed_message" in rid:
            return True
    return False


def find_trial_close_button(xml_str: str) -> tuple[int, int] | None:
    """Find the 'Close' button on the unavailable trial popup."""
    nodes = _get_all_nodes(xml_str)
    for node in nodes:
        text = (node.get("text", "") or "").strip()
        if text == "Close":
            bounds = node.get("bounds", "")
            if bounds:
                return get_center_of_bounds(bounds)
    return None


def parse_trial_insights(xml_str: str) -> dict:
    """
    Parse the full 'Reel insights' screen for a trial reel.
    Returns {title, reel_date, duration, views, watch_time,
             likes, comments, shares, interactions, profile_activity, is_available}.
    """
    nodes = _get_all_nodes(xml_str)
    result = {
        "title": "",
        "reel_date": "",
        "duration": "",
        "views": None,
        "watch_time": "",
        "likes": None,
        "comments": None,
        "shares": None,
        "interactions": None,
        "profile_activity": None,
        "is_available": True,
    }

    is_trial = False
    texts_with_bounds = []

    for node in nodes:
        text = (node.get("text", "") or "").strip()
        rid = node.get("resource_id", "")
        bounds = node.get("bounds", "")

        if not text:
            continue

        text_lower = text.lower()
        texts_with_bounds.append({"text": text, "bounds": bounds})

        # Detect trial reel marker
        if "this reel is a trial reel" in text_lower:
            is_trial = True

        # Title (usually "Untitled" or actual title)
        # Located near the top, above the date line
        if "·" in text and ("duration" in text_lower or re.search(r"\d{1,2}:\d{2}", text)):
            # e.g. "March 23 · Duration 0:07"
            parts = text.split("·")
            result["reel_date"] = parts[0].strip()
            dur_part = parts[1].strip() if len(parts) > 1 else ""
            dur_match = re.search(r"(\d{1,2}:\d{2})", dur_part)
            if dur_match:
                result["duration"] = dur_match.group(1)

    # Extract metric rows: "Views" → value, "Watch time" → value, etc.
    # The pattern is: label on left, value on right, same y-level
    metric_pairs = []
    for i, item in enumerate(texts_with_bounds):
        text = item["text"]
        text_lower = text.lower()
        bounds = item["bounds"]
        parsed = parse_bounds(bounds) if bounds else None

        if text_lower == "views" and parsed and parsed[0] < 200:
            # Look for numeric value at same y-level
            for other in texts_with_bounds:
                other_parsed = parse_bounds(other["bounds"]) if other.get("bounds") else None
                if other_parsed and other_parsed[0] > 500:
                    if abs(other_parsed[1] - parsed[1]) < 30:
                        val = _parse_view_count(other["text"].replace(",", ""))
                        if val is not None:
                            result["views"] = val
                            break

        elif text_lower == "watch time" and parsed and parsed[0] < 200:
            for other in texts_with_bounds:
                other_parsed = parse_bounds(other["bounds"]) if other.get("bounds") else None
                if other_parsed and other_parsed[0] > 500:
                    if abs(other_parsed[1] - parsed[1]) < 30:
                        result["watch_time"] = other["text"]
                        break

        elif text_lower == "interactions" and parsed and parsed[0] < 200:
            for other in texts_with_bounds:
                other_parsed = parse_bounds(other["bounds"]) if other.get("bounds") else None
                if other_parsed and other_parsed[0] > 500:
                    if abs(other_parsed[1] - parsed[1]) < 30:
                        val = _parse_view_count(other["text"].replace(",", ""))
                        if val is not None:
                            result["interactions"] = val
                            break

        elif text_lower == "profile activity" and parsed and parsed[0] < 200:
            for other in texts_with_bounds:
                other_parsed = parse_bounds(other["bounds"]) if other.get("bounds") else None
                if other_parsed and other_parsed[0] > 500:
                    if abs(other_parsed[1] - parsed[1]) < 30:
                        val = _parse_view_count(other["text"].replace(",", ""))
                        if val is not None:
                            result["profile_activity"] = val
                            break

    # Extract title: text above the date line, usually standalone
    for item in texts_with_bounds:
        text = item["text"]
        parsed = parse_bounds(item["bounds"]) if item.get("bounds") else None
        if parsed and 700 < parsed[1] < 830 and parsed[0] > 300:
            # Title region (centered, above date)
            if text not in ("Overview", "Reel insights") and "·" not in text:
                result["title"] = text
                break

    # Also try to extract likes/comments/shares from top row (screen 4 has them as standalone numbers)
    # The row with 5 numbers: likes, comments, shares, saves, ??? at y~930
    number_row = []
    for item in texts_with_bounds:
        parsed = parse_bounds(item["bounds"]) if item.get("bounds") else None
        if parsed and 890 < parsed[1] < 980:
            val = _parse_view_count(item["text"].replace(",", ""))
            if val is not None:
                number_row.append({"val": val, "x": parsed[0]})

    number_row.sort(key=lambda n: n["x"])
    if len(number_row) >= 4:
        result["likes"] = number_row[0]["val"]
        result["comments"] = number_row[1]["val"]
        result["shares"] = number_row[2]["val"]
        # index 3 could be saves, index 4 could be something else

    return result


def detect_reel_insights_screen(xml_str: str) -> bool:
    """Check if we're on the 'Reel insights' detail screen."""
    nodes = _get_all_nodes(xml_str)
    for node in nodes:
        rid = node.get("resource_id", "")
        text = (node.get("text", "") or "").strip()
        if rid.endswith("action_bar_title") and text == "Reel insights":
            return True
    return False
