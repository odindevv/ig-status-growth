"""
Telegram notification sender for viral alerts.
Sends formatted messages when content goes viral.

Configuration (in .env):
  TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
  TELEGRAM_CHAT_ID=-100123456789
  TELEGRAM_NOTIFY_MIN_SCORE=70
"""

import asyncio
import urllib.request
import urllib.parse
import json
from config import growth_settings as settings
from logger import logger


def _is_configured() -> bool:
    """Check if Telegram bot is configured."""
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def _format_alert(alert: dict) -> str:
    """Format a viral alert into a Telegram message."""
    score = alert.get("viral_score", 0)
    status = alert.get("status", "rising")

    if status == "viral":
        emoji = "\U0001F525"  # fire
    elif status == "trending":
        emoji = "\U0001F4C8"  # chart up
    else:
        emoji = "\U00002B06"  # up arrow

    username = alert.get("username", "unknown")
    content_type = alert.get("type", alert.get("content_type", "Content"))
    content_date = alert.get("date", alert.get("content_date", ""))
    rank = alert.get("rank", "-")
    views = alert.get("current_views", 0)
    prev_views = alert.get("previous_views")
    signals = alert.get("signals", [])

    # Format views with commas
    views_str = f"{views:,}" if isinstance(views, int) else str(views)

    lines = [
        f"{emoji} *{status.upper()}* — @{username}",
        f"",
        f"\U0001F3AC {content_type} · {content_date} · Rank #{rank}",
        f"\U0001F441 Views: *{views_str}*",
    ]

    if prev_views is not None:
        prev_str = f"{prev_views:,}" if isinstance(prev_views, int) else str(prev_views)
        if prev_views > 0:
            growth = ((views - prev_views) / prev_views) * 100
            lines.append(f"\U0001F4CA Previous: {prev_str} (+{growth:.0f}%)")
        else:
            lines.append(f"\U0001F4CA Previous: {prev_str}")
    else:
        lines.append(f"\U00002728 New content in top ranking")

    lines.append(f"\U0001F3AF Score: {score}/100")

    if signals:
        lines.append(f"")
        for s in signals:
            lines.append(f"  \U00002022 {s}")

    return "\n".join(lines)


async def send_viral_alert(alert: dict) -> bool:
    """Send a single viral alert via Telegram. Returns True on success."""
    if not _is_configured():
        return False

    text = _format_alert(alert)
    return await _send_message(text)


async def send_viral_alerts_batch(alerts: list[dict]) -> int:
    """
    Send multiple viral alerts. Returns count of successfully sent.
    Filters by minimum score from settings.
    """
    if not _is_configured():
        return 0

    min_score = settings.telegram_notify_min_score
    filtered = [a for a in alerts if a.get("viral_score", 0) >= min_score]

    if not filtered:
        return 0

    # If multiple alerts, send a summary first
    if len(filtered) > 1:
        summary = f"\U0001F6A8 *{len(filtered)} Viral Alerts Detected*\n"
        await _send_message(summary)

    sent = 0
    for alert in filtered:
        ok = await _send_message(_format_alert(alert))
        if ok:
            sent += 1
        # Small delay between messages to avoid rate limiting
        if sent < len(filtered):
            await asyncio.sleep(0.5)

    logger.info(f"[telegram] Sent {sent}/{len(filtered)} viral alerts")
    return sent


async def _send_message(text: str, parse_mode: str = "Markdown") -> bool:
    """Send a message via Telegram Bot API using urllib (no extra deps)."""
    token = settings.telegram_bot_token.strip()
    chat_id = settings.telegram_chat_id.strip()

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }).encode("utf-8")

    try:
        # Run in thread to not block the event loop
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _blocking_send, url, payload)
        return result
    except Exception as e:
        logger.error(f"[telegram] Failed to send message: {e}")
        return False


def _blocking_send(url: str, payload: bytes) -> bool:
    """Blocking HTTP POST to Telegram API."""
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                return True
            logger.warning(f"[telegram] API returned ok=false: {data}")
            return False
    except Exception as e:
        logger.error(f"[telegram] HTTP error: {e}")
        return False
