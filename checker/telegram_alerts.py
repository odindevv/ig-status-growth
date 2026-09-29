"""
Telegram bot — notifications + interactive commands with inline keyboards.

Commands:
  /help         — Show help with navigation buttons
  /status       — Service status + stats
  /list         — Accounts with inline action buttons
  /add <user>   — Add account to monitor
  /remove <user>— Remove account
  /check        — Run check on all accounts
  /check <user> — Check single account + action buttons
  /history <u>  — Last checks with pagination
  /groups       — List groups with filter buttons
  /stats        — Global statistics summary
  /growth       — Top movers (gainers & losers)
  /top [N]      — Top N accounts by followers
  /settings     — View/edit alert thresholds
"""

import asyncio
import json
import urllib.request
import urllib.parse
from datetime import datetime

from config import checker_settings as settings
from logger import logger

_BASE = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
_POLL_TIMEOUT = 10  # shorter so Ctrl+C doesn't hang
_shutdown = False

# Pagination config
_PAGE_SIZE = 8


def stop_bot():
    """Signal the bot loop to stop."""
    global _shutdown
    _shutdown = True


# ── Telegram API helpers ────────────────────────────────────

def _inline_keyboard(rows: list[list[dict]]) -> dict:
    """
    Build inline keyboard markup.
    rows = [[{"text": "Label", "callback_data": "action:data"}, ...], ...]
    """
    return {"inline_keyboard": rows}


def _btn(text: str, callback_data: str) -> dict:
    """Shorthand for an inline button."""
    return {"text": text, "callback_data": callback_data}


async def send_message(
    text: str,
    chat_id: str = "",
    reply_markup: dict | None = None,
) -> dict | None:
    """Send a Telegram message with optional inline keyboard."""
    cid = chat_id or settings.telegram_chat_id
    if not cid or not settings.telegram_bot_token:
        return None
    url = f"{_BASE}/sendMessage"
    payload = {
        "chat_id": cid,
        "text": text,
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
        logger.error(f"[telegram] Send failed: {e}")
        return None


async def edit_message(
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    """Edit an existing message (for inline keyboard updates)."""
    url = f"{_BASE}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
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
        logger.debug(f"[telegram] Edit failed: {e}")


async def answer_callback(callback_query_id: str, text: str = "") -> None:
    """Answer a callback query (dismiss the loading indicator)."""
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
        logger.debug(f"[telegram] Answer callback failed: {e}")


async def _get_updates(offset: int) -> list:
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
        logger.debug(f"[telegram] Poll error: {e}")
        return []


# ── Formatting helpers ─────────────────────────────────────

def _fmt_number(n: int | None) -> str:
    """Format number with commas or '—' if None."""
    if n is None:
        return "—"
    return f"{n:,}"


def _fmt_delta(d: int | None, show_sign: bool = True) -> str:
    """Format delta with sign and color emoji."""
    if d is None:
        return ""
    if d > 0:
        return f" 📈 +{d:,}" if show_sign else f"+{d:,}"
    elif d < 0:
        return f" 📉 {d:,}" if show_sign else f"{d:,}"
    return " ➡️ 0" if show_sign else "0"


def _fmt_pct(pct: float | None) -> str:
    if pct is None:
        return ""
    return f" ({pct:+.1f}%)"


# ── Command handlers ────────────────────────────────────────

async def _cmd_help(chat_id: str, _args: str) -> None:
    text = (
        "<b>🤖 IG Account Checker Bot</b>\n\n"
        "<b>📊 Monitoring</b>\n"
        "/status — Service status & stats\n"
        "/list — All monitored accounts\n"
        "/groups — List groups\n\n"
        "<b>📈 Statistics</b>\n"
        "/stats — Global statistics summary\n"
        "/growth — Top movers (gainers & losers)\n"
        "/top — Top accounts by followers\n\n"
        "<b>⚙️ Management</b>\n"
        "/add &lt;username&gt; [label] [group] — Add account\n"
        "/remove &lt;username&gt; — Remove account\n"
        "/enable &lt;username&gt; — Enable account\n"
        "/disable &lt;username&gt; — Disable account\n"
        "/settings — Alert thresholds\n\n"
        "<b>🔍 Checks</b>\n"
        "/check — Check all enabled accounts\n"
        "/check &lt;username&gt; — Check single account\n"
        "/history &lt;username&gt; — Recent checks\n"
    )
    keyboard = _inline_keyboard([
        [_btn("📊 Status", "cmd:status"), _btn("📋 List", "cmd:list")],
        [_btn("📈 Stats", "cmd:stats"), _btn("🏆 Top", "cmd:top")],
        [_btn("🔄 Check All", "cmd:check_all")],
    ])
    await send_message(text, chat_id, reply_markup=keyboard)


async def _cmd_status(chat_id: str, _args: str) -> None:
    from checker.database import get_stats_summary, get_global_totals
    from checker.runner import last_run_info
    from checker.scheduler import get_status as sched_status

    stats = await get_stats_summary()
    run = last_run_info()
    sched = sched_status()
    totals = await get_global_totals()

    lines = [
        "<b>📊 Account Checker Status</b>",
        "",
        f"👥 Accounts: {stats['active_accounts']} active / {stats['total_accounts']} total",
        f"✅ Checked (24h): {stats['checked_last_24h']}",
        f"⏱ Last run: {run['last_run'] or 'never'}",
        f"🔄 Running: {'yes ⏳' if run['running'] else 'no'}",
        f"📅 Scheduler: {'on ✅' if sched.get('running') else 'off ❌'}",
    ]
    if sched.get("jobs"):
        for j in sched["jobs"]:
            lines.append(f"  ⏰ Next: {j['next_run'] or '—'} ({j['name']})")

    if totals.get("total_followers"):
        lines.extend([
            "",
            "<b>📈 Totals</b>",
            f"Followers: {_fmt_number(totals.get('total_followers'))}",
            f"Following: {_fmt_number(totals.get('total_following'))}",
            f"Posts: {_fmt_number(totals.get('total_posts'))}",
        ])

    keyboard = _inline_keyboard([
        [_btn("🔄 Check All", "cmd:check_all"), _btn("📋 List", "cmd:list")],
        [_btn("📈 Stats", "cmd:stats"), _btn("🏆 Growth", "cmd:growth")],
    ])
    await send_message("\n".join(lines), chat_id, reply_markup=keyboard)


async def _cmd_list(chat_id: str, _args: str, page: int = 0) -> None:
    from checker.database import list_accounts, get_all_latest

    accounts = await list_accounts()
    if not accounts:
        await send_message("No accounts registered.", chat_id)
        return

    latest_map = {r["username"]: r for r in await get_all_latest()}
    total = len(accounts)
    total_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)
    page_accounts = accounts[start:end]

    lines = [f"<b>📋 Accounts ({start+1}-{end} of {total})</b>"]
    for acc in page_accounts:
        status_icon = "🟢" if acc["enabled"] else "⚪"
        latest = latest_map.get(acc["username"], {})
        f = latest.get("followers")
        fg = latest.get("following")
        info = f" — {f:,}F / {fg:,}fg" if f is not None else ""
        label = f" [{acc['label']}]" if acc.get("label") else ""
        group = f" ({acc['group_name']})" if acc.get("group_name") else ""
        lines.append(f"{status_icon} @{acc['username']}{label}{group}{info}")

    # Action buttons for each account on this page
    account_buttons = []
    for acc in page_accounts:
        u = acc["username"]
        account_buttons.append([
            _btn(f"🔍 {u}", f"check:{u}"),
            _btn("📊", f"history:{u}"),
            _btn("🟢" if not acc["enabled"] else "⚪", f"toggle:{u}"),
        ])

    # Pagination row
    nav_buttons = []
    if page > 0:
        nav_buttons.append(_btn("⬅️ Prev", f"list_page:{page-1}"))
    nav_buttons.append(_btn(f"{page+1}/{total_pages}", "noop:0"))
    if page < total_pages - 1:
        nav_buttons.append(_btn("Next ➡️", f"list_page:{page+1}"))

    all_rows = account_buttons
    if total_pages > 1:
        all_rows.append(nav_buttons)
    all_rows.append([_btn("🔄 Check All", "cmd:check_all"), _btn("📈 Stats", "cmd:stats")])

    keyboard = _inline_keyboard(all_rows)
    await send_message("\n".join(lines), chat_id, reply_markup=keyboard)


async def _cmd_add(chat_id: str, args: str) -> None:
    parts = args.strip().split(maxsplit=2)
    if not parts:
        await send_message("Usage: /add <username> [label] [group]", chat_id)
        return

    from checker.database import add_account, get_account

    username = parts[0].lstrip("@").lower()
    label = parts[1] if len(parts) > 1 else ""
    group = parts[2] if len(parts) > 2 else ""

    existing = await get_account(username)
    if existing:
        await send_message(f"@{username} already exists.", chat_id)
        return

    await add_account(username, label, group)
    keyboard = _inline_keyboard([
        [_btn("🔍 Check Now", f"check:{username}"), _btn("📋 List", "cmd:list")],
    ])
    await send_message(f"✅ Added @{username} to monitoring.", chat_id, reply_markup=keyboard)


async def _cmd_remove(chat_id: str, args: str) -> None:
    username = args.strip().lstrip("@").lower()
    if not username:
        await send_message("Usage: /remove <username>", chat_id)
        return

    from checker.database import delete_account
    deleted = await delete_account(username)
    if deleted:
        await send_message(f"🗑 Removed @{username}.", chat_id)
    else:
        await send_message(f"@{username} not found.", chat_id)


async def _cmd_enable(chat_id: str, args: str) -> None:
    username = args.strip().lstrip("@").lower()
    if not username:
        await send_message("Usage: /enable <username>", chat_id)
        return
    from checker.database import update_account
    await update_account(username, enabled=1)
    keyboard = _inline_keyboard([
        [_btn("🔍 Check", f"check:{username}"), _btn("📋 List", "cmd:list")],
    ])
    await send_message(f"🟢 Enabled @{username}.", chat_id, reply_markup=keyboard)


async def _cmd_disable(chat_id: str, args: str) -> None:
    username = args.strip().lstrip("@").lower()
    if not username:
        await send_message("Usage: /disable <username>", chat_id)
        return
    from checker.database import update_account
    await update_account(username, enabled=0)
    await send_message(f"⚪ Disabled @{username}.", chat_id)


async def _cmd_check(chat_id: str, args: str) -> None:
    username = args.strip().lstrip("@").lower()

    if username:
        await _do_single_check(chat_id, username)
    else:
        from checker.runner import run_check, is_running
        if is_running():
            await send_message("⏳ Check already running.", chat_id)
            return
        keyboard = _inline_keyboard([
            [_btn("📊 Status", "cmd:status")],
        ])
        await send_message("🔄 Starting check on all accounts...", chat_id, reply_markup=keyboard)
        asyncio.create_task(run_check(notify=True))


async def _do_single_check(chat_id: str, username: str, message_id: int | None = None) -> None:
    """Check a single account and send/edit result with buttons."""
    from checker.database import get_account
    acc = await get_account(username)
    if not acc:
        text = f"@{username} not found."
        if message_id:
            await edit_message(chat_id, message_id, text)
        else:
            await send_message(text, chat_id)
        return

    loading_text = f"🔄 Checking @{username}..."
    if message_id:
        await edit_message(chat_id, message_id, loading_text)
    else:
        await send_message(loading_text, chat_id)

    from checker.runner import run_single_check
    result = await run_single_check(username)

    if result["status"] == "ok":
        f = result.get("followers")
        fg = result.get("following")
        p = result.get("posts")
        df = result.get("delta_followers")
        dfg = result.get("delta_following")
        dp = result.get("delta_posts")

        text = (
            f"<b>📊 @{username}</b>\n\n"
            f"👥 Followers: {_fmt_number(f)}{_fmt_delta(df)}\n"
            f"👤 Following: {_fmt_number(fg)}{_fmt_delta(dfg)}\n"
            f"📝 Posts: {_fmt_number(p)}{_fmt_delta(dp)}\n"
        )
        if result.get("full_name"):
            text = f"<b>📊 {result['full_name']}</b> (@{username})\n\n" + text.split("\n\n", 1)[1]
    else:
        text = f"❌ @{username}: {result['status']} — {result.get('error', '')}"

    keyboard = _inline_keyboard([
        [_btn("🔄 Recheck", f"check:{username}"), _btn("📊 History", f"history:{username}:0")],
        [
            _btn("🟢 Enable" if not acc.get("enabled") else "⚪ Disable", f"toggle:{username}"),
            _btn("📋 List", "cmd:list"),
        ],
    ])
    await send_message(text, chat_id, reply_markup=keyboard)


async def _cmd_history(chat_id: str, args: str, page: int = 0) -> None:
    username = args.strip().lstrip("@").lower()
    if not username:
        await send_message("Usage: /history <username>", chat_id)
        return

    from checker.database import get_history
    all_rows = await get_history(username, days=30, limit=50)
    if not all_rows:
        await send_message(f"No history for @{username}.", chat_id)
        return

    total = len(all_rows)
    per_page = 5
    total_pages = (total + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, total)
    rows = all_rows[start:end]

    lines = [f"<b>📊 History — @{username}</b>  ({start+1}-{end} of {total})"]
    prev_row = None
    for i, r in enumerate(rows):
        f = r.get("followers")
        fg = r.get("following")
        ts = r.get("checked_at", "")[:16]
        status = r.get("status", "ok")
        icon = "✅" if status == "ok" else "❌"

        # Calculate delta from next row (which is the older check)
        delta_str = ""
        next_idx = start + i + 1
        if next_idx < total and f is not None:
            older = all_rows[next_idx]
            of = older.get("followers")
            if of is not None:
                diff = f - of
                if diff != 0:
                    delta_str = f" ({diff:+,})"

        if f is not None:
            lines.append(f"{icon} {ts} — {f:,}F{delta_str} / {fg:,}fg")
        else:
            lines.append(f"{icon} {ts} — {status}")

    # Navigation buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(_btn("⬅️ Newer", f"history:{username}:{page-1}"))
    nav_buttons.append(_btn(f"{page+1}/{total_pages}", "noop:0"))
    if page < total_pages - 1:
        nav_buttons.append(_btn("Older ➡️", f"history:{username}:{page+1}"))

    rows_kb = []
    if total_pages > 1:
        rows_kb.append(nav_buttons)
    rows_kb.append([
        _btn("🔍 Check", f"check:{username}"),
        _btn("📋 List", "cmd:list"),
    ])

    keyboard = _inline_keyboard(rows_kb)
    await send_message("\n".join(lines), chat_id, reply_markup=keyboard)


async def _cmd_groups(chat_id: str, _args: str) -> None:
    from checker.database import list_groups
    groups = await list_groups()
    if not groups:
        await send_message("No groups defined.", chat_id)
        return

    lines = ["<b>📁 Groups</b>"]
    for g in groups:
        lines.append(f"• {g}")

    # Button per group to filter /list
    group_buttons = []
    row = []
    for g in groups:
        row.append(_btn(f"📋 {g}", f"list_group:{g}"))
        if len(row) == 2:
            group_buttons.append(row)
            row = []
    if row:
        group_buttons.append(row)
    group_buttons.append([_btn("📋 All Accounts", "cmd:list")])

    keyboard = _inline_keyboard(group_buttons)
    await send_message("\n".join(lines), chat_id, reply_markup=keyboard)


# ── New commands ───────────────────────────────────────────

async def _cmd_stats(chat_id: str, _args: str) -> None:
    """Global statistics summary."""
    from checker.database import get_stats_summary, get_global_totals, get_growth_data

    stats = await get_stats_summary()
    totals = await get_global_totals()
    growth = await get_growth_data()

    # Calculate aggregate deltas
    total_delta_f = sum(g.get("delta_followers") or 0 for g in growth)
    total_delta_fg = sum(g.get("delta_following") or 0 for g in growth)
    total_delta_p = sum(g.get("delta_posts") or 0 for g in growth)

    # Accounts with data
    with_data = [g for g in growth if g.get("followers") is not None]
    gainers = [g for g in growth if (g.get("delta_followers") or 0) > 0]
    losers = [g for g in growth if (g.get("delta_followers") or 0) < 0]
    unchanged = len(with_data) - len(gainers) - len(losers)

    # Average followers
    avg_f = totals.get("total_followers", 0) // max(totals.get("account_count", 1), 1)

    lines = [
        "<b>📈 Global Statistics</b>",
        "",
        f"👥 Total Followers: {_fmt_number(totals.get('total_followers'))}{_fmt_delta(total_delta_f)}",
        f"👤 Total Following: {_fmt_number(totals.get('total_following'))}{_fmt_delta(total_delta_fg)}",
        f"📝 Total Posts: {_fmt_number(totals.get('total_posts'))}{_fmt_delta(total_delta_p)}",
        "",
        f"📊 Avg Followers/Account: {_fmt_number(avg_f)}",
        f"🔢 Accounts: {stats['active_accounts']} active / {stats['total_accounts']} total",
        "",
        f"📈 Growing: {len(gainers)}  |  📉 Declining: {len(losers)}  |  ➡️ Stable: {unchanged}",
    ]

    keyboard = _inline_keyboard([
        [_btn("🏆 Top Movers", "cmd:growth"), _btn("👑 Top Accounts", "cmd:top")],
        [_btn("📊 Status", "cmd:status"), _btn("📋 List", "cmd:list")],
    ])
    await send_message("\n".join(lines), chat_id, reply_markup=keyboard)


async def _cmd_growth(chat_id: str, _args: str) -> None:
    """Top movers — biggest gainers and losers."""
    from checker.database import get_growth_data

    growth = await get_growth_data()
    if not growth:
        await send_message("No growth data available yet. Run a check first.", chat_id)
        return

    # Sort by delta_followers
    with_delta = [g for g in growth if g.get("delta_followers") is not None]
    if not with_delta:
        await send_message("Need at least 2 checks to show growth data.", chat_id)
        return

    # Top gainers
    gainers = sorted(with_delta, key=lambda x: x.get("delta_followers", 0), reverse=True)[:5]
    # Top losers
    losers = sorted(with_delta, key=lambda x: x.get("delta_followers", 0))[:5]

    lines = ["<b>🏆 Top Movers</b>", ""]

    # Gainers
    lines.append("<b>📈 Top Gainers</b>")
    for i, g in enumerate(gainers, 1):
        df = g.get("delta_followers", 0)
        if df <= 0:
            break
        pct = g.get("pct_followers")
        pct_str = f" ({pct:+.1f}%)" if pct is not None else ""
        lines.append(f"{i}. @{g['username']} +{df:,}{pct_str} → {_fmt_number(g.get('followers'))}")

    lines.append("")

    # Losers
    lines.append("<b>📉 Top Losers</b>")
    for i, g in enumerate(losers, 1):
        df = g.get("delta_followers", 0)
        if df >= 0:
            break
        pct = g.get("pct_followers")
        pct_str = f" ({pct:+.1f}%)" if pct is not None else ""
        lines.append(f"{i}. @{g['username']} {df:,}{pct_str} → {_fmt_number(g.get('followers'))}")

    # Buttons for individual accounts
    top_accounts = gainers[:3] + losers[:3]
    btn_rows = []
    row = []
    for g in top_accounts:
        if g.get("delta_followers", 0) == 0:
            continue
        row.append(_btn(f"📊 {g['username']}", f"check:{g['username']}"))
        if len(row) == 3:
            btn_rows.append(row)
            row = []
    if row:
        btn_rows.append(row)
    btn_rows.append([_btn("📈 Stats", "cmd:stats"), _btn("📋 List", "cmd:list")])

    keyboard = _inline_keyboard(btn_rows)
    await send_message("\n".join(lines), chat_id, reply_markup=keyboard)


async def _cmd_top(chat_id: str, args: str) -> None:
    """Top N accounts by followers."""
    from checker.database import get_all_latest, list_accounts

    try:
        n = int(args.strip()) if args.strip() else 10
        n = max(1, min(n, 50))
    except ValueError:
        n = 10

    latest = await get_all_latest()
    accounts = {a["username"]: a for a in await list_accounts()}

    # Sort by followers descending
    with_followers = [r for r in latest if r.get("followers") is not None]
    with_followers.sort(key=lambda x: x.get("followers", 0), reverse=True)
    top = with_followers[:n]

    if not top:
        await send_message("No data yet. Run a check first.", chat_id)
        return

    lines = [f"<b>👑 Top {len(top)} by Followers</b>", ""]
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(top):
        medal = medals[i] if i < 3 else f"{i+1}."
        acc = accounts.get(r["username"], {})
        label = f" [{acc['label']}]" if acc.get("label") else ""
        f = r.get("followers", 0)
        fg = r.get("following", 0)
        ratio = round(f / fg, 1) if fg else "∞"
        lines.append(f"{medal} @{r['username']}{label} — {f:,}F (ratio {ratio})")

    btn_rows = []
    row = []
    for r in top[:6]:
        row.append(_btn(f"📊 {r['username']}", f"check:{r['username']}"))
        if len(row) == 3:
            btn_rows.append(row)
            row = []
    if row:
        btn_rows.append(row)
    btn_rows.append([_btn("🏆 Growth", "cmd:growth"), _btn("📈 Stats", "cmd:stats")])

    keyboard = _inline_keyboard(btn_rows)
    await send_message("\n".join(lines), chat_id, reply_markup=keyboard)


async def _cmd_settings(chat_id: str, _args: str) -> None:
    """Show current alert settings."""
    lines = [
        "<b>⚙️ Alert Settings</b>",
        "",
        f"📉 Drop alert: ≥{settings.telegram_alert_drop_pct}% loss",
        f"📈 Gain alert: ≥{settings.telegram_alert_gain_pct}% gain",
        f"⏱ Check interval: {settings.scheduler_interval_hours}h",
        f"📅 Daily check: {settings.scheduler_hour:02d}:{settings.scheduler_minute:02d} UTC",
        f"🕐 Delay range: {settings.delay_min}-{settings.delay_max}s",
        f"🔄 Max retries: {settings.max_retries}",
        f"📡 Proxy: {'enabled' if settings.proxy_enabled else 'disabled'}",
        f"🗄 Retention: {settings.history_retention_days} days",
    ]

    keyboard = _inline_keyboard([
        [_btn("📉 Drop 3%", "set:drop:3"), _btn("📉 Drop 5%", "set:drop:5"), _btn("📉 Drop 10%", "set:drop:10")],
        [_btn("📈 Gain 10%", "set:gain:10"), _btn("📈 Gain 20%", "set:gain:20"), _btn("📈 Gain 50%", "set:gain:50")],
        [_btn("📊 Status", "cmd:status"), _btn("🏠 Help", "cmd:help")],
    ])
    await send_message("\n".join(lines), chat_id, reply_markup=keyboard)


# ── Callback query handler ─────────────────────────────────

async def _handle_callback(callback_query: dict) -> None:
    """Handle inline keyboard button presses."""
    cb_id = callback_query.get("id", "")
    data = callback_query.get("data", "")
    msg = callback_query.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    message_id = msg.get("message_id")

    if not data or not chat_id:
        await answer_callback(cb_id)
        return

    # Auth check
    from_user = callback_query.get("from", {})
    from_id = str(from_user.get("id", ""))
    if settings.telegram_chat_id and chat_id != settings.telegram_chat_id:
        await answer_callback(cb_id, "Unauthorized")
        return

    try:
        parts = data.split(":", maxsplit=2)
        action = parts[0]
        arg1 = parts[1] if len(parts) > 1 else ""
        arg2 = parts[2] if len(parts) > 2 else ""

        if action == "noop":
            await answer_callback(cb_id)
            return

        elif action == "cmd":
            await answer_callback(cb_id)
            cmd_map = {
                "help": _cmd_help,
                "status": _cmd_status,
                "list": _cmd_list,
                "stats": _cmd_stats,
                "growth": _cmd_growth,
                "top": _cmd_top,
                "groups": _cmd_groups,
                "settings": _cmd_settings,
                "check_all": _cmd_check,
            }
            handler = cmd_map.get(arg1)
            if handler:
                await handler(chat_id, "")

        elif action == "check":
            username = arg1
            await answer_callback(cb_id, f"Checking @{username}...")
            await _do_single_check(chat_id, username)

        elif action == "history":
            username = arg1
            page = int(arg2) if arg2 else 0
            await answer_callback(cb_id)
            await _cmd_history(chat_id, username, page=page)

        elif action == "toggle":
            username = arg1
            from checker.database import get_account, update_account
            acc = await get_account(username)
            if acc:
                new_state = 0 if acc.get("enabled") else 1
                await update_account(username, enabled=new_state)
                state_text = "enabled 🟢" if new_state else "disabled ⚪"
                await answer_callback(cb_id, f"@{username} {state_text}")
                # Refresh the list
                await _cmd_list(chat_id, "")
            else:
                await answer_callback(cb_id, f"@{username} not found")

        elif action == "list_page":
            page = int(arg1) if arg1 else 0
            await answer_callback(cb_id)
            await _cmd_list(chat_id, "", page=page)

        elif action == "list_group":
            group = arg1
            await answer_callback(cb_id)
            await _send_group_list(chat_id, group)

        elif action == "set":
            param = arg1
            value = arg2
            await answer_callback(cb_id, f"Setting {param} to {value}%")
            await _apply_setting(chat_id, param, value, message_id)

        else:
            await answer_callback(cb_id, "Unknown action")

    except Exception as e:
        logger.error(f"[telegram] Callback error: {e}", exc_info=True)
        await answer_callback(cb_id, f"Error: {e}")


async def _send_group_list(chat_id: str, group_name: str) -> None:
    """Send filtered account list for a specific group."""
    from checker.database import list_accounts, get_all_latest

    accounts = await list_accounts(group_name=group_name)
    if not accounts:
        await send_message(f"No accounts in group '{group_name}'.", chat_id)
        return

    latest_map = {r["username"]: r for r in await get_all_latest()}

    lines = [f"<b>📁 Group: {group_name} ({len(accounts)})</b>"]
    for acc in accounts:
        status_icon = "🟢" if acc["enabled"] else "⚪"
        latest = latest_map.get(acc["username"], {})
        f = latest.get("followers")
        fg = latest.get("following")
        info = f" — {f:,}F / {fg:,}fg" if f is not None else ""
        label = f" [{acc['label']}]" if acc.get("label") else ""
        lines.append(f"{status_icon} @{acc['username']}{label}{info}")

    btn_rows = []
    row = []
    for acc in accounts[:6]:
        row.append(_btn(f"🔍 {acc['username']}", f"check:{acc['username']}"))
        if len(row) == 3:
            btn_rows.append(row)
            row = []
    if row:
        btn_rows.append(row)
    btn_rows.append([_btn("📁 Groups", "cmd:groups"), _btn("📋 All", "cmd:list")])

    keyboard = _inline_keyboard(btn_rows)
    await send_message("\n".join(lines), chat_id, reply_markup=keyboard)


async def _apply_setting(chat_id: str, param: str, value: str, message_id: int | None) -> None:
    """Apply a settings change (runtime only)."""
    try:
        val = float(value)
        if param == "drop":
            settings.telegram_alert_drop_pct = val
            text = f"✅ Drop alert threshold set to {val}%"
        elif param == "gain":
            settings.telegram_alert_gain_pct = val
            text = f"✅ Gain alert threshold set to {val}%"
        else:
            text = f"Unknown setting: {param}"

        # Refresh settings view
        await _cmd_settings(chat_id, "")
    except ValueError:
        await send_message(f"Invalid value: {value}", chat_id)


# ── Command dispatch ────────────────────────────────────────

_COMMANDS = {
    "/help": _cmd_help,
    "/start": _cmd_help,
    "/status": _cmd_status,
    "/list": _cmd_list,
    "/add": _cmd_add,
    "/remove": _cmd_remove,
    "/enable": _cmd_enable,
    "/disable": _cmd_disable,
    "/check": _cmd_check,
    "/history": _cmd_history,
    "/groups": _cmd_groups,
    "/stats": _cmd_stats,
    "/growth": _cmd_growth,
    "/top": _cmd_top,
    "/settings": _cmd_settings,
}


async def _handle_message(msg: dict) -> None:
    text = msg.get("text", "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not text or not chat_id:
        return

    # Auth check
    if settings.telegram_chat_id and chat_id != settings.telegram_chat_id:
        if text.startswith("/"):
            await send_message("Unauthorized.", chat_id)
        return

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]  # Strip @botname
    args = parts[1] if len(parts) > 1 else ""

    handler = _COMMANDS.get(cmd)
    if handler:
        try:
            await handler(chat_id, args)
        except Exception as e:
            logger.error(f"[telegram] Command error {cmd}: {e}", exc_info=True)
            await send_message(f"Error: {e}", chat_id)


# ── Bot loop ────────────────────────────────────────────────

async def start_bot() -> None:
    """Long-poll loop for Telegram updates."""
    global _shutdown
    _shutdown = False
    logger.info("[telegram] Bot polling started (inline keyboards enabled)")
    offset = 0
    while not _shutdown:
        try:
            updates = await _get_updates(offset)
            for u in updates:
                if _shutdown:
                    break
                offset = u["update_id"] + 1

                # Handle text messages
                msg = u.get("message")
                if msg:
                    await _handle_message(msg)

                # Handle callback queries (inline keyboard presses)
                callback = u.get("callback_query")
                if callback:
                    await _handle_callback(callback)

        except asyncio.CancelledError:
            logger.info("[telegram] Bot polling cancelled")
            break
        except Exception as e:
            if _shutdown:
                break
            logger.error(f"[telegram] Poll loop error: {e}")
            await asyncio.sleep(3)
    logger.info("[telegram] Bot polling stopped")
