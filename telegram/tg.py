"""
Telegram API helpers — send messages, inline keyboards, polling.
Shared by all handlers.
"""

import asyncio
import json
import urllib.request
import urllib.parse

from config import telegram_settings as settings
from logger import logger

_BASE = f"https://api.telegram.org/bot{settings.bot_token}"
_POLL_TIMEOUT = 10


# ── Inline keyboard helpers ────────────────────────────────

def kb(rows: list[list[dict]]) -> dict:
    """Build inline keyboard markup."""
    return {"inline_keyboard": rows}


def btn(text: str, callback_data: str) -> dict:
    """Shorthand for an inline button."""
    return {"text": text, "callback_data": callback_data}


# ── Send / edit / answer ──────────────────────────────────

async def send(
    text: str,
    chat_id: str = "",
    reply_markup: dict | None = None,
) -> dict | None:
    """Send a Telegram message with optional inline keyboard."""
    cid = chat_id or settings.chat_id
    if not cid or not settings.bot_token:
        return None
    url = f"{_BASE}/sendMessage"
    payload = {
        "chat_id": cid,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10))
        return json.loads(resp.read())
    except Exception as e:
        logger.error(f"[tg] Send failed: {e}")
        return None


async def edit(
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    """Edit an existing message."""
    url = f"{_BASE}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10))
    except Exception as e:
        logger.debug(f"[tg] Edit failed: {e}")


async def answer_cb(callback_query_id: str, text: str = "") -> None:
    """Answer a callback query (dismiss loading indicator)."""
    url = f"{_BASE}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10))
    except Exception as e:
        logger.debug(f"[tg] Answer callback failed: {e}")


async def get_updates(offset: int) -> list:
    """Long-poll for updates."""
    allowed = urllib.parse.quote(json.dumps(["message", "callback_query"]))
    url = f"{_BASE}/getUpdates?timeout={_POLL_TIMEOUT}&offset={offset}&allowed_updates={allowed}"
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None, lambda: urllib.request.urlopen(url, timeout=_POLL_TIMEOUT + 5)
        )
        data = json.loads(resp.read())
        return data.get("result", [])
    except Exception as e:
        logger.debug(f"[tg] Poll error: {e}")
        return []


async def get_me() -> dict | None:
    """Verify bot token and get bot info."""
    url = f"{_BASE}/getMe"
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None, lambda: urllib.request.urlopen(url, timeout=10)
        )
        data = json.loads(resp.read())
        if data.get("ok"):
            return data["result"]
    except Exception as e:
        logger.error(f"[tg] getMe failed: {e}")
    return None
