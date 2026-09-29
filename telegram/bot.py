"""
Bot core — polling loop, command dispatch, main menu.
Routes commands to service-specific handlers.
"""

import asyncio
from config import telegram_settings as settings
from logger import logger
import telegram.tg as tg


_shutdown = False


def stop():
    """Signal bot loop to stop."""
    global _shutdown
    _shutdown = True


# ── Main menu ──────────────────────────────────────────────

async def _cmd_start(chat_id: str, _args: str) -> None:
    text = (
        "<b>🤖 IG Bot Gateway</b>\n\n"
        "Unified bot for all IG services.\n\n"
        "<b>🔍 Account Checker</b>\n"
        "/c_status — Checker status\n"
        "/c_list — Monitored accounts\n"
        "/c_check [user] — Check accounts\n"
        "/c_stats — Statistics\n"
        "/c_growth — Top movers\n"
        "/c_7d — 7-day growth %\n"
        "/c_top — Top by followers\n"
        "/c_groups — Account groups\n"
        "/c_add &lt;user&gt; — Add account\n"
        "/c_remove &lt;user&gt; — Remove account\n"
        "/c_history &lt;user&gt; — Check history\n"
        "/c_settings — Alert thresholds\n\n"
        "<b>📊 Growth API</b>\n"
        "/g_status — Scheduler & tasks\n"
        "/g_devices — Registered devices\n"
        "/g_discover — Scan ADB\n"
        "/g_adddevice &lt;id&gt; [user] — Add device\n"
        "/g_enable &lt;id&gt; — Enable device\n"
        "/g_disable &lt;id&gt; — Disable device\n"
        "/g_monitor &lt;id&gt; — Start monitoring\n"
        "/g_unmonitor &lt;id&gt; — Stop monitoring\n"
        "/g_setproxy &lt;id&gt; &lt;url&gt; — Set rotate URL\n"
        "/g_adduser &lt;id&gt; &lt;user&gt; — Add account\n"
        "/g_removeuser &lt;id&gt; &lt;user&gt; — Remove account\n"
        "/g_run &lt;device&gt; — Check device\n"
        "/g_runall — Check all monitored\n"
        "/g_7d — 7-day growth report\n"
        "/g_trials — Trial reels data\n"
        "/g_pending — Videos awaiting URL\n"
        "/g_unregistered — Missing URLs\n"
        "/g_register title | url — Register video\n"
        "/g_resolve &lt;id&gt; &lt;url&gt; — Resolve pending\n"
        "/g_content — Search content\n"
        "/g_schedule [HH:MM|on|off] — Scheduler\n\n"
        "<b>🛡️ Status API</b>\n"
        "/s_status — Status overview\n"
        "/s_devices — All devices\n"
        "/s_discover — Scan ADB\n"
        "/s_adddevice &lt;id&gt; [user] — Add device\n"
        "/s_enable &lt;id&gt; — Enable device\n"
        "/s_disable &lt;id&gt; — Disable device\n"
        "/s_monitor &lt;id&gt; — Start monitoring\n"
        "/s_unmonitor &lt;id&gt; — Stop monitoring\n"
        "/s_setproxy &lt;id&gt; &lt;url&gt; — Set rotate URL\n"
        "/s_adduser &lt;id&gt; &lt;user&gt; — Add account\n"
        "/s_removeuser &lt;id&gt; &lt;user&gt; — Remove account\n"
        "/s_check [device] — Check devices\n"
        "/s_history [device] — History\n"
        "/s_problems — Problem devices\n"
        "/s_scheduler — Scheduler control\n"
        "/s_tasks — Active tasks\n\n"
        "<b>🏥 System</b>\n"
        "/health — Check all APIs\n"
        "/whoami — Show your chat ID\n"
    )
    keyboard = tg.kb([
        [tg.btn("🔍 Checker", "menu:checker"), tg.btn("📊 Growth", "menu:growth"), tg.btn("🛡️ Status", "menu:status")],
        [tg.btn("🏥 Health", "sys:health")],
    ])
    await tg.send(text, chat_id, reply_markup=keyboard)


async def _cmd_health(chat_id: str, _args: str) -> None:
    """Check connectivity to all 3 APIs."""
    from telegram.api_client import api

    await tg.send("🔍 Checking API connectivity...", chat_id)

    results = await asyncio.gather(
        api.health("checker"),
        api.health("growth"),
        api.health("status"),
    )

    names = ["🔍 Checker", "📊 Growth", "🛡️ Status"]
    lines = ["<b>🏥 API Health</b>", ""]
    for name, r in zip(names, results):
        if r["ok"]:
            lines.append(f"✅ {name} — online")
        else:
            lines.append(f"❌ {name} — {r.get('error', 'offline')}")

    keyboard = tg.kb([
        [tg.btn("🔍 Checker", "menu:checker"), tg.btn("📊 Growth", "menu:growth"), tg.btn("🛡️ Status", "menu:status")],
    ])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def _cmd_whoami(chat_id: str, _args: str, msg: dict | None = None) -> None:
    """Show user/chat ID (works without auth)."""
    msg = msg or {}
    from_user = msg.get("from", {})
    first = from_user.get("first_name", "?")
    uname = from_user.get("username", "?")
    user_id = from_user.get("id", "?")
    chat_type = msg.get("chat", {}).get("type", "?")
    await tg.send(
        f"<b>🔍 Who Am I</b>\n\n"
        f"👤 User: {first} (@{uname})\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"💬 Chat ID: <code>{chat_id}</code>\n"
        f"📋 Chat type: {chat_type}\n\n"
        f"Add to .env:\n<code>TELEGRAM_CHAT_ID={user_id}</code>",
        chat_id,
    )


# ── Sub-menus (shortcut buttons) ──────────────────────────

async def _menu_checker(chat_id: str) -> None:
    from telegram.handlers.checker import cmd_help
    await cmd_help(chat_id, "")


async def _menu_growth(chat_id: str) -> None:
    from telegram.handlers.growth import cmd_help
    await cmd_help(chat_id, "")


async def _menu_status(chat_id: str) -> None:
    from telegram.handlers.status import cmd_help
    await cmd_help(chat_id, "")


# ── Command routing ────────────────────────────────────────

def _build_command_map() -> dict:
    """Build the full command map from all handlers."""
    from telegram.handlers.checker import COMMANDS as c_cmds
    from telegram.handlers.growth import COMMANDS as g_cmds
    from telegram.handlers.status import COMMANDS as s_cmds

    cmd_map = {
        "/start": _cmd_start,
        "/help": _cmd_start,
        "/health": _cmd_health,
    }
    # Checker commands: /c_xxx
    for cmd, handler in c_cmds.items():
        cmd_map[f"/c_{cmd}"] = handler
    # Growth commands: /g_xxx
    for cmd, handler in g_cmds.items():
        cmd_map[f"/g_{cmd}"] = handler
    # Status commands: /s_xxx
    for cmd, handler in s_cmds.items():
        cmd_map[f"/s_{cmd}"] = handler

    return cmd_map


def _build_callback_map() -> dict:
    """Build callback dispatch from all handlers."""
    from telegram.handlers.checker import handle_callback as c_cb
    from telegram.handlers.growth import handle_callback as g_cb
    from telegram.handlers.status import handle_callback as s_cb
    return {"c": c_cb, "g": g_cb, "s": s_cb}


# ── Message handler ────────────────────────────────────────

async def _handle_message(msg: dict, cmd_map: dict) -> None:
    text = (msg.get("text") or "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not text or not chat_id:
        return

    # /whoami works without auth
    if text.lower().startswith("/whoami"):
        await _cmd_whoami(chat_id, "", msg)
        return

    # Auth check
    if settings.chat_id and chat_id != settings.chat_id:
        if text.startswith("/"):
            await tg.send("Unauthorized.", chat_id)
        return

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]
    args = parts[1] if len(parts) > 1 else ""

    handler = cmd_map.get(cmd)
    if handler:
        try:
            await handler(chat_id, args)
        except Exception as e:
            logger.error(f"[bot] Command error {cmd}: {e}", exc_info=True)
            await tg.send(f"❌ Error: {e}", chat_id)
    elif text.startswith("/"):
        await tg.send(
            f"❓ Unknown command: <code>{cmd}</code>\nType /help for available commands.",
            chat_id,
        )


# ── Callback handler ──────────────────────────────────────

async def _handle_callback(callback_query: dict, cb_map: dict) -> None:
    cb_id = callback_query.get("id", "")
    data = callback_query.get("data", "")
    msg = callback_query.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))

    if not data or not chat_id:
        await tg.answer_cb(cb_id)
        return

    # Auth check
    if settings.chat_id and chat_id != settings.chat_id:
        await tg.answer_cb(cb_id, "Unauthorized")
        return

    try:
        # Format: "prefix:action:arg1:arg2"
        # prefix = c (checker), g (growth), s (status), menu, sys, noop
        parts = data.split(":", maxsplit=1)
        prefix = parts[0]
        rest = parts[1] if len(parts) > 1 else ""

        if prefix == "noop":
            await tg.answer_cb(cb_id)
            return

        elif prefix == "menu":
            await tg.answer_cb(cb_id)
            menus = {"checker": _menu_checker, "growth": _menu_growth, "status": _menu_status}
            handler = menus.get(rest)
            if handler:
                await handler(chat_id)

        elif prefix == "sys":
            await tg.answer_cb(cb_id)
            if rest == "health":
                await _cmd_health(chat_id, "")

        elif prefix in cb_map:
            handler = cb_map[prefix]
            await handler(cb_id, chat_id, rest)

        else:
            await tg.answer_cb(cb_id, "Unknown action")

    except Exception as e:
        logger.error(f"[bot] Callback error: {e}", exc_info=True)
        await tg.answer_cb(cb_id, f"Error: {e}")


# ── Polling loop ──────────────────────────────────────────

async def run() -> None:
    """Main bot loop — long-polling for Telegram updates."""
    global _shutdown
    _shutdown = False

    # Verify token
    me = await tg.get_me()
    if not me:
        logger.error("[bot] Cannot connect to Telegram API")
        return
    logger.info(f"[bot] Connected as @{me.get('username', '?')} — building command map")

    cmd_map = _build_command_map()
    cb_map = _build_callback_map()

    logger.info(f"[bot] Ready — {len(cmd_map)} commands, polling started")

    offset = 0
    while not _shutdown:
        try:
            updates = await tg.get_updates(offset)
            for u in updates:
                if _shutdown:
                    break
                offset = u["update_id"] + 1

                msg = u.get("message")
                if msg:
                    await _handle_message(msg, cmd_map)

                callback = u.get("callback_query")
                if callback:
                    await _handle_callback(callback, cb_map)

        except asyncio.CancelledError:
            logger.info("[bot] Polling cancelled")
            break
        except Exception as e:
            if _shutdown:
                break
            logger.error(f"[bot] Poll loop error: {e}")
            await asyncio.sleep(3)

    logger.info("[bot] Polling stopped")
