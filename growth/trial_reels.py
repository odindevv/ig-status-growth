"""
Trial Reels checker — navigates from Professional Dashboard into
the Trial Reels grid, taps each reel, and extracts full insights.

Called after the main growth check while still on the dashboard.
Detects if "Trial reels" button exists; if not, skips silently.

Navigation flow:
  1. Scan dashboard XML for "Trial reels" button
  2. Tap → Trial Reels grid (list of thumbnails with view counts)
  3. Parse grid: extract up to 5 reels from first visible row
  4. For each reel:
     a. Tap thumbnail → reel player
     b. Tap "Trial reel · View insights" → popup
     c. If unavailable ("take longer to start getting views") → tap Close, skip
     d. Tap "View insights" in popup → Reel Insights detail screen
     e. Parse full metrics (views, watch_time, likes, comments, shares, interactions, profile_activity)
     f. Press back 3x to return to grid
  5. Press back to return to Professional Dashboard
"""

import asyncio
import random as _random

from config import growth_settings as settings
from logger import logger
from shared.adb import dump_ui, tap, press_back, scroll_down
from growth.ui_parser import (
    detect_trial_reels_button,
    detect_trial_reels_screen,
    parse_trial_grid,
    detect_trial_reel_player,
    find_trial_view_insights_button,
    parse_trial_popup,
    find_trial_popup_view_insights,
    detect_trial_unavailable,
    find_trial_close_button,
    parse_trial_insights,
    detect_reel_insights_screen,
)

NAV_DELAY = settings.nav_step_delay
PAGE_LOAD_DELAY = settings.page_load_delay
MAX_TRIAL_REELS = 5


def _human_delay(base: float) -> float:
    """Add ±20% random variation to a delay."""
    jitter = base * 0.2
    return base + _random.uniform(-jitter, jitter)


async def check_trial_reels(
    device_id: str,
    dashboard_xml: str | None = None,
    username: str = "",
) -> dict:
    """
    Extract trial reel metrics from the Professional Dashboard.

    Args:
        device_id: ADB device serial
        dashboard_xml: Optional XML of current screen (dashboard).
                       If None, will dump_ui.
        username: IG username for content lookup API registration.

    Returns:
        {
            "found": bool,        # whether Trial reels button was found
            "reels": [...],       # list of reel data dicts
            "nav": [...],         # navigation log
        }
    """
    nav = []
    reels_data = []
    result = {"found": False, "reels": [], "nav": nav}

    try:
        # ── Step 1: Detect Trial Reels button on dashboard ──
        xml = dashboard_xml
        if not xml:
            xml = await dump_ui(device_id, safe_mode=True)
        if not xml:
            nav.append("trial:no_xml")
            return result

        trial_btn = detect_trial_reels_button(xml)
        if not trial_btn:
            # Button may be below the fold — scroll down and retry (up to 2 scrolls)
            for scroll_attempt in range(2):
                await scroll_down(device_id, amount=0.35)
                await asyncio.sleep(_human_delay(NAV_DELAY))
                xml = await dump_ui(device_id, safe_mode=True)
                if xml:
                    trial_btn = detect_trial_reels_button(xml)
                    if trial_btn:
                        nav.append(f"trial:button_found_after_scroll={scroll_attempt + 1}")
                        break
        if not trial_btn:
            nav.append("trial:no_button")
            logger.debug(f"[trial] No Trial reels button found on {device_id} — skipping")
            return result

        result["found"] = True
        nav.append("trial:button_found")

        # ── Step 2: Tap into Trial Reels grid (with retry for unexpected screens) ──
        GRID_MAX_RETRIES = 3
        grid_xml = None
        grid_ok = False

        await tap(device_id, trial_btn[0], trial_btn[1])
        await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY))

        for grid_attempt in range(GRID_MAX_RETRIES):
            grid_xml = await dump_ui(device_id, safe_mode=True)
            if grid_xml and detect_trial_reels_screen(grid_xml):
                grid_ok = True
                break

            # Not on grid — could be loading, web view, popup, etc.
            if grid_attempt < GRID_MAX_RETRIES - 1:
                logger.warning(
                    f"[trial] Grid not detected on {device_id} (attempt {grid_attempt + 1}/{GRID_MAX_RETRIES}) "
                    f"— pressing back and re-tapping"
                )
                nav.append(f"trial:grid_retry_{grid_attempt + 1}")
                # Dismiss whatever unexpected screen appeared
                await press_back(device_id)
                await asyncio.sleep(_human_delay(1.0))

                # Re-check: we might now be back on dashboard or some other screen
                check_xml = await dump_ui(device_id, safe_mode=True)
                if check_xml and detect_trial_reels_screen(check_xml):
                    grid_xml = check_xml
                    grid_ok = True
                    break

                # If we see the trial button again, re-tap it
                if check_xml:
                    retry_btn = detect_trial_reels_button(check_xml)
                    if retry_btn:
                        logger.info(f"[trial] Found trial button again after back — re-tapping on {device_id}")
                        await tap(device_id, retry_btn[0], retry_btn[1])
                        await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY))
                    else:
                        # Not on dashboard either — press back once more to try to get there
                        await press_back(device_id)
                        await asyncio.sleep(_human_delay(1.0))
                        check_xml2 = await dump_ui(device_id, safe_mode=True)
                        if check_xml2:
                            retry_btn2 = detect_trial_reels_button(check_xml2)
                            if retry_btn2:
                                logger.info(f"[trial] Found trial button after 2 backs — re-tapping on {device_id}")
                                await tap(device_id, retry_btn2[0], retry_btn2[1])
                                await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY))
            else:
                # Last attempt — just wait longer in case it's still loading
                await asyncio.sleep(2.0)
                grid_xml = await dump_ui(device_id, safe_mode=True)
                if grid_xml and detect_trial_reels_screen(grid_xml):
                    grid_ok = True

        if not grid_ok:
            nav.append("trial:grid_not_loaded")
            logger.warning(f"[trial] Grid screen not detected after {GRID_MAX_RETRIES} attempts on {device_id}")
            await press_back(device_id)
            return result

        nav.append("trial:grid_loaded")

        # ── Step 3: Parse grid thumbnails ──
        grid_items = parse_trial_grid(grid_xml)
        if not grid_items:
            nav.append("trial:grid_empty")
            logger.info(f"[trial] No trial reels in grid on {device_id}")
            await press_back(device_id)
            return result

        nav.append(f"trial:grid_count={len(grid_items)}")
        logger.info(f"[trial] Found {len(grid_items)} trial reels on {device_id}")

        # ── Step 4: Iterate reels (max 5) ──
        for reel_info in grid_items[:MAX_TRIAL_REELS]:
            reel_idx = reel_info["reel_index"]
            reel_result = {
                "reel_index": reel_idx,
                "grid_views": reel_info.get("grid_views"),
                "views": None,
                "watch_time": "",
                "likes": None,
                "comments": None,
                "shares": None,
                "interactions": None,
                "profile_activity": None,
                "title": "",
                "reel_date": "",
                "duration": "",
                "is_available": True,
                "ig_url": "",
            }

            try:
                # ── 4a: Tap thumbnail → reel player ──
                await tap(device_id, reel_info["cx"], reel_info["cy"])
                await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY))

                player_xml = await dump_ui(device_id, safe_mode=True)
                if not player_xml:
                    nav.append(f"trial:reel{reel_idx}_no_xml")
                    await press_back(device_id)
                    await asyncio.sleep(_human_delay(NAV_DELAY))
                    reels_data.append(reel_result)
                    continue

                # ── 4b: Find and tap "Trial reel · View insights" button ──
                insights_btn = find_trial_view_insights_button(player_xml)
                if not insights_btn:
                    nav.append(f"trial:reel{reel_idx}_no_insights_btn")
                    await press_back(device_id)
                    await asyncio.sleep(_human_delay(NAV_DELAY))
                    reels_data.append(reel_result)
                    continue

                await tap(device_id, insights_btn[0], insights_btn[1])
                await asyncio.sleep(_human_delay(NAV_DELAY))

                popup_xml = await dump_ui(device_id, safe_mode=True)
                if not popup_xml:
                    nav.append(f"trial:reel{reel_idx}_popup_no_xml")
                    await press_back(device_id)
                    await asyncio.sleep(_human_delay(NAV_DELAY))
                    await press_back(device_id)
                    await asyncio.sleep(_human_delay(NAV_DELAY))
                    reels_data.append(reel_result)
                    continue

                # ── 4c: Check if unavailable ──
                if detect_trial_unavailable(popup_xml):
                    nav.append(f"trial:reel{reel_idx}_unavailable")
                    logger.info(f"[trial] Reel {reel_idx} not available yet on {device_id}")
                    reel_result["is_available"] = False
                    # Tap Close button
                    close_btn = find_trial_close_button(popup_xml)
                    if close_btn:
                        await tap(device_id, close_btn[0], close_btn[1])
                    else:
                        await press_back(device_id)
                    await asyncio.sleep(_human_delay(NAV_DELAY))
                    # Back to grid from player
                    await press_back(device_id)
                    await asyncio.sleep(_human_delay(NAV_DELAY))
                    reels_data.append(reel_result)
                    continue

                # ── 4d: Parse popup quick metrics ──
                popup_data = parse_trial_popup(popup_xml)
                if popup_data:
                    nav.append(f"trial:reel{reel_idx}_popup_parsed")

                # ── 4e: Tap "View insights" for full detail ──
                view_btn = find_trial_popup_view_insights(popup_xml)
                if not view_btn:
                    nav.append(f"trial:reel{reel_idx}_no_view_insights_btn")
                    # Use popup data if available
                    if popup_data:
                        reel_result.update({
                            "views": popup_data.get("views"),
                            "likes": popup_data.get("likes"),
                            "comments": popup_data.get("comments"),
                            "shares": popup_data.get("shares"),
                        })
                    await press_back(device_id)
                    await asyncio.sleep(_human_delay(NAV_DELAY))
                    await press_back(device_id)
                    await asyncio.sleep(_human_delay(NAV_DELAY))
                    reels_data.append(reel_result)
                    continue

                await tap(device_id, view_btn[0], view_btn[1])
                await asyncio.sleep(_human_delay(PAGE_LOAD_DELAY))

                # ── 4f: Parse Reel Insights detail screen ──
                detail_xml = await dump_ui(device_id, safe_mode=True)
                if not detail_xml:
                    # Retry once
                    await asyncio.sleep(2.0)
                    detail_xml = await dump_ui(device_id, safe_mode=True)

                if detail_xml and detect_reel_insights_screen(detail_xml):
                    insights = parse_trial_insights(detail_xml)
                    reel_result.update({
                        "title": insights.get("title", ""),
                        "reel_date": insights.get("reel_date", ""),
                        "duration": insights.get("duration", ""),
                        "views": insights.get("views"),
                        "watch_time": insights.get("watch_time", ""),
                        "likes": insights.get("likes"),
                        "comments": insights.get("comments"),
                        "shares": insights.get("shares"),
                        "interactions": insights.get("interactions"),
                        "profile_activity": insights.get("profile_activity"),
                    })
                    nav.append(f"trial:reel{reel_idx}_detail_parsed")
                    logger.info(f"[trial] Reel {reel_idx}: views={insights.get('views')}, "
                                f"interactions={insights.get('interactions')} on {device_id}")
                elif popup_data:
                    # Fallback to popup data
                    reel_result.update({
                        "views": popup_data.get("views"),
                        "likes": popup_data.get("likes"),
                        "comments": popup_data.get("comments"),
                        "shares": popup_data.get("shares"),
                    })
                    nav.append(f"trial:reel{reel_idx}_detail_failed_popup_fallback")
                else:
                    nav.append(f"trial:reel{reel_idx}_detail_failed")

                # ── Navigate back to grid: back from insights → back from player ──
                await press_back(device_id)
                await asyncio.sleep(_human_delay(NAV_DELAY))
                # May land on popup or player — press back again
                await press_back(device_id)
                await asyncio.sleep(_human_delay(NAV_DELAY))
                # One more back to be safe (popup → player → grid)
                check_xml = await dump_ui(device_id, safe_mode=True)
                if check_xml and not detect_trial_reels_screen(check_xml):
                    await press_back(device_id)
                    await asyncio.sleep(_human_delay(NAV_DELAY))

            except Exception as e:
                logger.error(f"[trial] Error on reel {reel_idx} for {device_id}: {e}")
                nav.append(f"trial:reel{reel_idx}_error={type(e).__name__}")
                # Try to recover to grid
                for _ in range(3):
                    await press_back(device_id)
                    await asyncio.sleep(0.5)

            reels_data.append(reel_result)

        # ── Step 4.5: Content lookup — resolve IG URLs (local DB) ──
        if reels_data:
            from growth.growth_metrics import _lookup_content_url
            for rd in reels_data:
                if not rd.get("is_available"):
                    continue
                title = rd.get("title", "") or ""
                rd_date = rd.get("reel_date", "") or ""
                rd_idx = rd.get("reel_index", 0)
                # Stable key: no views (they change). Use date+index+username
                content_id = f"Trial_{rd_date}_t{rd_idx}_{username}" if rd_date else ""
                lookup_key = f"{title} {content_id}".strip() if content_id else title
                if not lookup_key:
                    continue
                try:
                    ig_url = await _lookup_content_url(
                        "", lookup_key,
                        username=username, device_id=device_id,
                    )
                    if ig_url:
                        rd["ig_url"] = ig_url
                        logger.info(f"[trial] Resolved URL for reel {rd_idx}: {ig_url}")
                    else:
                        nav.append(f"trial:reel{rd_idx}_url_pending")
                except Exception as e:
                    logger.warning(f"[trial] Content lookup failed for reel {rd_idx}: {e}")

        # ── Step 5: Back to Professional Dashboard ──
        await press_back(device_id)
        await asyncio.sleep(_human_delay(NAV_DELAY))
        nav.append(f"trial:done({len(reels_data)} reels)")

    except Exception as e:
        logger.error(f"[trial] Unexpected error on {device_id}: {e}", exc_info=True)
        nav.append(f"trial:fatal_error={type(e).__name__}")
        # Try to recover
        for _ in range(3):
            await press_back(device_id)
            await asyncio.sleep(0.5)

    result["reels"] = reels_data
    return result
