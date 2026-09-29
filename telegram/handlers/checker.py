"""
Checker handler — ig_account_checker commands via HTTP API.
All commands prefixed with /c_ in the bot.
Callback data prefixed with "c:" for routing.
"""

from datetime import datetime

from telegram.api_client import api
import telegram.tg as tg
from logger import logger

_SVC = "checker"
_PAGE_SIZE = 8

# ── Formatting helpers ────────────────────────────────────

_STATE_ICONS = {
    "active": "🟢", "banned": "🔴", "expired": "🟠",
    "checkpoint": "🟡", "unknown": "⚪",
}


def _state_icon(state: str) -> str:
    return _STATE_ICONS.get(state, "⚪")


def _fmt(n) -> str:
    if n is None:
        return "—"
    return f"{n:,}" if isinstance(n, (int, float)) else str(n)


def _delta(d) -> str:
    if d is None or d == 0:
        return ""
    icon = "📈" if d > 0 else "📉"
    return f" {icon} {d:+,}"


def _time_ago(iso_str: str) -> str:
    if not iso_str:
        return "never"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.utcnow()
        diff = now - dt
        mins = int(diff.total_seconds() / 60)
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except Exception:
        return iso_str[:16]


# ── Commands ──────────────────────────────────────────────

async def cmd_help(chat_id: str, _args: str) -> None:
    text = (
        "<b>🔍 Account Checker</b>\n\n"
        "<b>📊 Monitoring</b>\n"
        "/c_status — Service status\n"
        "/c_list — Accounts list\n"
        "/c_groups — Groups\n\n"
        "<b>📈 Statistics</b>\n"
        "/c_stats — Global statistics\n"
        "/c_growth — Top movers (last run)\n"
        "/c_7d — 7-day growth %\n"
        "/c_top [N] — Top by followers\n\n"
        "<b>⚙️ Management</b>\n"
        "/c_add &lt;user&gt; [label] [group] — Add\n"
        "/c_remove &lt;user&gt; — Remove\n"
        "/c_enable &lt;user&gt; — Enable\n"
        "/c_disable &lt;user&gt; — Disable\n"
        "/c_settings — Alert thresholds\n\n"
        "<b>🔄 Checks</b>\n"
        "/c_check — Check all\n"
        "/c_check &lt;user&gt; — Check one\n"
        "/c_history &lt;user&gt; — History\n"
    )
    keyboard = tg.kb([
        [tg.btn("📊 Status", "c:cmd:status"), tg.btn("📋 List", "c:cmd:list")],
        [tg.btn("📈 Stats", "c:cmd:stats"), tg.btn("📊 7d Growth", "c:cmd:7d")],
        [tg.btn("🏆 Top", "c:cmd:top"), tg.btn("🔄 Check All", "c:cmd:check_all")],
        [tg.btn("🏠 Main", "menu:checker")],
    ])
    await tg.send(text, chat_id, reply_markup=keyboard)


async def cmd_status(chat_id: str, _args: str) -> None:
    data = await api.get(_SVC, "/stats")
    if not data or "error" in data:
        await tg.send(f"❌ Checker API error: {data.get('error', 'unreachable')}", chat_id)
        return

    totals = await api.get(_SVC, "/stats/totals") or {}
    health = await api.get(_SVC, "/health") or {}
    proxy = health.get("proxy", {})

    # Fetch account states from latest history
    latest_data = await api.get(_SVC, "/history") or {}
    latest_list = latest_data.get("latest", [])
    state_counts: dict[str, int] = {}
    for acc in latest_list:
        st = acc.get("last_status", acc.get("status", "unknown")) or "unknown"
        # Map scraper statuses to state labels
        if st == "ok":
            st = "active"
        elif st == "not_found":
            st = "not_found"
        elif st == "rate_limited":
            st = "rate_limited"
        elif st == "error":
            st = "error"
        state_counts[st] = state_counts.get(st, 0) + 1

    lines = [
        "<b>📊 Account Checker Status</b>",
        "",
        f"👥 Accounts: {data.get('active_accounts', '?')} active / {data.get('total_accounts', '?')} total",
        f"✅ Checked (24h): {data.get('checked_last_24h', '?')}",
        f"⏱ Last run: {_time_ago(data.get('last_run')) if data.get('last_run') else 'never'}",
        f"🔄 Running: {'yes ⏳' if data.get('running') else 'no'}",
        f"🌐 Proxy: {'✅ ' + str(proxy.get('count', 1)) + 'x ' + proxy.get('rotation', '') + ' — ' + proxy.get('current', '?') if proxy.get('enabled') else '❌ off (direct IP)'}",
        *(
            [f"🔄 IP Renew: every {proxy['renew']['every']} reqs (renews: {proxy['renew']['total_renews']}, since last: {proxy['renew']['requests_since_renew']})"]
            if proxy.get('enabled') and proxy.get('renew') else []
        ),
    ]

    if state_counts:
        lines.extend(["", "<b>📋 Account States:</b>"])
        state_icons_map = {"active": "🟢", "not_found": "🔴", "rate_limited": "🟡", "error": "🟠", "unknown": "⚪"}
        for state, count in sorted(state_counts.items(), key=lambda x: -x[1]):
            icon = state_icons_map.get(state, "⚪")
            lines.append(f"  {icon} {state}: {count}")

    if totals.get("total_followers"):
        lines.extend([
            "",
            "<b>📈 Totals</b>",
            f"Followers: {_fmt(totals.get('total_followers'))}",
            f"Following: {_fmt(totals.get('total_following'))}",
            f"Posts: {_fmt(totals.get('total_posts'))}",
        ])

    keyboard = tg.kb([
        [tg.btn("🔄 Check All", "c:cmd:check_all"), tg.btn("📋 List", "c:cmd:list")],
        [tg.btn("📈 Stats", "c:cmd:stats"), tg.btn("🏆 Growth", "c:cmd:growth")],
    ])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def cmd_list(chat_id: str, _args: str, page: int = 0) -> None:
    data = await api.get(_SVC, "/accounts")
    if not data or "error" in data:
        await tg.send(f"❌ Error: {data.get('error', 'unreachable')}", chat_id)
        return

    accounts = data.get("accounts", [])
    if not accounts:
        await tg.send("No accounts registered.", chat_id)
        return

    total = len(accounts)
    total_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)
    page_accs = accounts[start:end]

    lines = [f"<b>📋 Accounts ({start+1}-{end} of {total})</b>"]
    for acc in page_accs:
        en_icon = "🟢" if acc.get("enabled") else "⚪"
        f = acc.get("followers")
        fg = acc.get("following")
        info = f" — {f:,}F / {fg:,}fg" if f is not None else ""
        label = f" [{acc['label']}]" if acc.get("label") else ""
        group = f" ({acc['group_name']})" if acc.get("group_name") else ""
        # Show last check status
        status = acc.get("last_status", "")
        priv = " 🔒" if acc.get("is_private") else ""
        st_icon = ""
        if status and status != "ok":
            st_map = {"not_found": "🔴", "rate_limited": "🟡", "error": "🟠"}
            st_icon = f" {st_map.get(status, '⚪')}{status}"
        lines.append(f"{en_icon} @{acc['username']}{label}{group}{info}{priv}{st_icon}")

    # Per-account buttons
    acc_btns = []
    for acc in page_accs:
        u = acc["username"]
        acc_btns.append([
            tg.btn(f"🔍 {u}", f"c:check:{u}"),
            tg.btn("📊", f"c:history:{u}:0"),
            tg.btn("🟢" if not acc.get("enabled") else "⚪", f"c:toggle:{u}"),
        ])

    # Pagination
    nav = []
    if page > 0:
        nav.append(tg.btn("⬅️ Prev", f"c:list_page:{page-1}"))
    nav.append(tg.btn(f"{page+1}/{total_pages}", "noop:0"))
    if page < total_pages - 1:
        nav.append(tg.btn("Next ➡️", f"c:list_page:{page+1}"))

    rows = acc_btns
    if total_pages > 1:
        rows.append(nav)
    rows.append([tg.btn("🔄 Check All", "c:cmd:check_all"), tg.btn("📈 Stats", "c:cmd:stats")])

    await tg.send("\n".join(lines), chat_id, reply_markup=tg.kb(rows))


async def cmd_add(chat_id: str, args: str) -> None:
    parts = args.strip().split(maxsplit=2)
    if not parts:
        await tg.send("Usage: /c_add &lt;username&gt; [label] [group]", chat_id)
        return
    username = parts[0].lstrip("@").lower()
    label = parts[1] if len(parts) > 1 else ""
    group = parts[2] if len(parts) > 2 else ""

    data = await api.post(_SVC, "/accounts", json={
        "username": username, "label": label, "group_name": group,
    })
    if data and data.get("added"):
        keyboard = tg.kb([
            [tg.btn("🔍 Check Now", f"c:check:{username}"), tg.btn("📋 List", "c:cmd:list")],
        ])
        await tg.send(f"✅ Added @{username} to monitoring.", chat_id, reply_markup=keyboard)
    else:
        await tg.send(f"❌ {data.get('detail', data.get('error', 'Failed'))}", chat_id)


async def cmd_remove(chat_id: str, args: str) -> None:
    username = args.strip().lstrip("@").lower()
    if not username:
        await tg.send("Usage: /c_remove &lt;username&gt;", chat_id)
        return
    data = await api.delete(_SVC, f"/accounts/{username}")
    if data and data.get("deleted"):
        await tg.send(f"🗑 Removed @{username}.", chat_id)
    else:
        await tg.send(f"❌ {data.get('detail', 'Not found')}", chat_id)


async def cmd_enable(chat_id: str, args: str) -> None:
    username = args.strip().lstrip("@").lower()
    if not username:
        await tg.send("Usage: /c_enable &lt;username&gt;", chat_id)
        return
    data = await api.put(_SVC, f"/accounts/{username}", json={"enabled": True})
    if data and data.get("updated"):
        keyboard = tg.kb([[tg.btn("🔍 Check", f"c:check:{username}"), tg.btn("📋 List", "c:cmd:list")]])
        await tg.send(f"🟢 Enabled @{username}.", chat_id, reply_markup=keyboard)
    else:
        await tg.send(f"❌ {data.get('detail', 'Failed')}", chat_id)


async def cmd_disable(chat_id: str, args: str) -> None:
    username = args.strip().lstrip("@").lower()
    if not username:
        await tg.send("Usage: /c_disable &lt;username&gt;", chat_id)
        return
    data = await api.put(_SVC, f"/accounts/{username}", json={"enabled": False})
    if data and data.get("updated"):
        await tg.send(f"⚪ Disabled @{username}.", chat_id)
    else:
        await tg.send(f"❌ {data.get('detail', 'Failed')}", chat_id)


async def cmd_check(chat_id: str, args: str) -> None:
    username = args.strip().lstrip("@").lower()
    if username:
        await tg.send(f"🔄 Checking @{username}...", chat_id)
        data = await api.post(_SVC, f"/check/{username}", timeout=120)
        logger.info(f"[checker] /check/{username} response: {data}")

        if not data:
            await tg.send("❌ Check failed: no response from API", chat_id)
            return

        # Check for error conditions
        status = data.get("status", "")
        error_msg = data.get("error", "") or data.get("detail", "")
        if status and status != "ok":
            await tg.send(f"❌ Check failed: {error_msg or status}", chat_id)
            return

        # Format result
        f = data.get("followers")
        fg = data.get("following")
        p = data.get("posts")
        df = data.get("delta_followers")
        priv = " 🔒" if data.get("is_private") else ""
        title = f"<b>📊 @{username}</b>{priv}"
        if data.get("full_name"):
            title = f"<b>📊 {data['full_name']}</b> (@{username}){priv}"
        lines = [
            title,
            "",
            f"👥 Followers: {_fmt(f)}{_delta(df)}",
            f"👤 Following: {_fmt(fg)}{_delta(data.get('delta_following'))}",
            f"📝 Posts: {_fmt(p)}{_delta(data.get('delta_posts'))}",
        ]
        if f and fg:
            ratio = round(f / fg, 1) if fg else "∞"
            lines.append(f"📊 Ratio: {ratio}")
        if data.get("bio"):
            bio = data["bio"][:150]
            lines.extend(["", f"📝 <i>{bio}</i>"])
        keyboard = tg.kb([
            [tg.btn("🔄 Recheck", f"c:check:{username}"), tg.btn("📊 History", f"c:history:{username}:0")],
            [tg.btn("📋 List", "c:cmd:list")],
        ])
        await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)
    else:
        data = await api.post(_SVC, "/check/all", json=None)
        if data and data.get("started"):
            keyboard = tg.kb([[tg.btn("📊 Status", "c:cmd:status")]])
            await tg.send("🔄 Check started on all accounts.", chat_id, reply_markup=keyboard)
        else:
            await tg.send(f"❌ {data.get('detail', 'Failed')}", chat_id)


async def cmd_history(chat_id: str, args: str, page: int = 0) -> None:
    username = args.strip().lstrip("@").lower()
    if not username:
        await tg.send("Usage: /c_history &lt;username&gt;", chat_id)
        return

    data = await api.get(_SVC, f"/history/{username}", days=30, limit=50)
    if not data or "error" in data:
        await tg.send(f"❌ {data.get('detail', data.get('error', 'Failed'))}", chat_id)
        return

    rows = data.get("history", [])
    if not rows:
        await tg.send(f"No history for @{username}.", chat_id)
        return

    total = len(rows)
    per_page = 5
    total_pages = (total + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, total)

    lines = [f"<b>📊 History — @{username}</b> ({start+1}-{end} of {total})"]
    for i in range(start, end):
        r = rows[i]
        f = r.get("followers")
        ts = _time_ago(r.get("checked_at", ""))
        status = r.get("status", "ok")
        icon = "✅" if status == "ok" else "❌"
        delta_str = ""
        if i + 1 < total and f is not None:
            of = rows[i + 1].get("followers")
            if of is not None:
                diff = f - of
                if diff != 0:
                    delta_str = f" ({diff:+,})"
        fg = r.get("following")
        if f is not None:
            lines.append(f"{icon} {ts} — {f:,}F{delta_str} / {fg:,}fg")
        else:
            err = r.get("error", status)
            lines.append(f"{icon} {ts} — {err}")

    nav = []
    if page > 0:
        nav.append(tg.btn("⬅️ Newer", f"c:history:{username}:{page-1}"))
    nav.append(tg.btn(f"{page+1}/{total_pages}", "noop:0"))
    if page < total_pages - 1:
        nav.append(tg.btn("Older ➡️", f"c:history:{username}:{page+1}"))

    kb_rows = []
    if total_pages > 1:
        kb_rows.append(nav)
    kb_rows.append([tg.btn("🔍 Check", f"c:check:{username}"), tg.btn("📋 List", "c:cmd:list")])
    await tg.send("\n".join(lines), chat_id, reply_markup=tg.kb(kb_rows))


async def cmd_groups(chat_id: str, _args: str) -> None:
    data = await api.get(_SVC, "/groups")
    if not data or "error" in data:
        await tg.send(f"❌ {data.get('error', 'Failed')}", chat_id)
        return
    groups = data.get("groups", [])
    if not groups:
        await tg.send("No groups defined.", chat_id)
        return

    lines = ["<b>📁 Groups</b>"]
    for g in groups:
        lines.append(f"• {g}")

    btn_rows = []
    row = []
    for g in groups:
        row.append(tg.btn(f"📋 {g}", f"c:list_group:{g}"))
        if len(row) == 2:
            btn_rows.append(row)
            row = []
    if row:
        btn_rows.append(row)
    btn_rows.append([tg.btn("📋 All Accounts", "c:cmd:list")])

    await tg.send("\n".join(lines), chat_id, reply_markup=tg.kb(btn_rows))


async def cmd_stats(chat_id: str, _args: str) -> None:
    stats = await api.get(_SVC, "/stats") or {}
    totals = await api.get(_SVC, "/stats/totals") or {}
    growth_data = await api.get(_SVC, "/stats/growth") or {}

    growth = growth_data.get("growth", [])
    total_delta_f = sum(g.get("delta_followers") or 0 for g in growth)
    gainers = [g for g in growth if (g.get("delta_followers") or 0) > 0]
    losers = [g for g in growth if (g.get("delta_followers") or 0) < 0]
    with_data = [g for g in growth if g.get("followers") is not None]
    unchanged = len(with_data) - len(gainers) - len(losers)
    avg_f = totals.get("total_followers", 0) // max(totals.get("account_count", 1), 1)

    lines = [
        "<b>📈 Global Statistics</b>",
        "",
        f"👥 Total Followers: {_fmt(totals.get('total_followers'))}{_delta(total_delta_f)}",
        f"👤 Total Following: {_fmt(totals.get('total_following'))}",
        f"📝 Total Posts: {_fmt(totals.get('total_posts'))}",
        "",
        f"📊 Avg Followers/Account: {_fmt(avg_f)}",
        f"🔢 Accounts: {stats.get('active_accounts', '?')} active / {stats.get('total_accounts', '?')} total",
        "",
        f"📈 Growing: {len(gainers)}  |  📉 Declining: {len(losers)}  |  ➡️ Stable: {unchanged}",
    ]

    keyboard = tg.kb([
        [tg.btn("🏆 Top Movers", "c:cmd:growth"), tg.btn("👑 Top Accounts", "c:cmd:top")],
        [tg.btn("📊 Status", "c:cmd:status"), tg.btn("📋 List", "c:cmd:list")],
    ])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def cmd_growth(chat_id: str, _args: str) -> None:
    data = await api.get(_SVC, "/stats/growth") or {}
    growth = data.get("growth", [])
    if not growth:
        await tg.send("No growth data available. Run a check first.", chat_id)
        return

    with_delta = [g for g in growth if g.get("delta_followers") is not None]
    if not with_delta:
        await tg.send("Need at least 2 checks to show growth data.", chat_id)
        return

    gainers = sorted(with_delta, key=lambda x: x.get("delta_followers", 0), reverse=True)[:5]
    losers = sorted(with_delta, key=lambda x: x.get("delta_followers", 0))[:5]

    lines = ["<b>🏆 Top Movers</b>", "", "<b>📈 Top Gainers</b>"]
    for i, g in enumerate(gainers, 1):
        df = g.get("delta_followers", 0)
        if df <= 0:
            break
        pct = g.get("pct_followers")
        pct_str = f" ({pct:+.1f}%)" if pct is not None else ""
        lines.append(f"{i}. @{g['username']} +{df:,}{pct_str} → {_fmt(g.get('followers'))}")

    lines.extend(["", "<b>📉 Top Losers</b>"])
    for i, g in enumerate(losers, 1):
        df = g.get("delta_followers", 0)
        if df >= 0:
            break
        pct = g.get("pct_followers")
        pct_str = f" ({pct:+.1f}%)" if pct is not None else ""
        lines.append(f"{i}. @{g['username']} {df:,}{pct_str} → {_fmt(g.get('followers'))}")

    btn_rows = []
    row = []
    for g in (gainers[:3] + losers[:3]):
        if g.get("delta_followers", 0) == 0:
            continue
        row.append(tg.btn(f"📊 {g['username']}", f"c:check:{g['username']}"))
        if len(row) == 3:
            btn_rows.append(row)
            row = []
    if row:
        btn_rows.append(row)
    btn_rows.append([tg.btn("📈 Stats", "c:cmd:stats"), tg.btn("📋 List", "c:cmd:list")])
    await tg.send("\n".join(lines), chat_id, reply_markup=tg.kb(btn_rows))


async def cmd_7d(chat_id: str, _args: str) -> None:
    """7-day growth percentage for all accounts."""
    data = await api.get(_SVC, "/stats/growth-7d") or {}
    growth = data.get("growth", [])
    if not growth:
        await tg.send("No 7-day data available. Need checks spanning at least a few days.", chat_id)
        return

    with_pct = [g for g in growth if g.get("pct_followers") is not None]
    if not with_pct:
        await tg.send("Not enough history to compute 7-day growth.", chat_id)
        return

    # Split into gainers and losers
    gainers = [g for g in with_pct if (g.get("delta_followers") or 0) > 0]
    losers = [g for g in with_pct if (g.get("delta_followers") or 0) < 0]
    flat = [g for g in with_pct if (g.get("delta_followers") or 0) == 0]

    lines = ["<b>📊 7-Day Growth Report</b>", ""]

    if gainers:
        lines.append("<b>📈 Growing</b>")
        for g in gainers:
            pct = g.get("pct_followers", 0)
            df = g.get("delta_followers", 0)
            lines.append(
                f"  @{g['username']}  <b>+{pct:.1f}%</b>  (+{df:,}) → {_fmt(g.get('followers'))}"
            )
        lines.append("")

    if losers:
        lines.append("<b>📉 Declining</b>")
        for g in losers:
            pct = g.get("pct_followers", 0)
            df = g.get("delta_followers", 0)
            lines.append(
                f"  @{g['username']}  <b>{pct:.1f}%</b>  ({df:,}) → {_fmt(g.get('followers'))}"
            )
        lines.append("")

    if flat:
        lines.append(f"<b>➡️ No change:</b> {', '.join('@' + g['username'] for g in flat)}")
        lines.append("")

    # Summary line
    total_start = sum((g.get("followers", 0) or 0) - (g.get("delta_followers", 0) or 0) for g in with_pct)
    total_now = sum(g.get("followers", 0) or 0 for g in with_pct)
    if total_start > 0:
        total_pct = ((total_now - total_start) / total_start) * 100
        lines.append(f"<b>🏠 Combined:</b> {_fmt(total_now)} followers ({total_pct:+.2f}% / 7d)")

    keyboard = tg.kb([
        [tg.btn("📈 Last-Run Growth", "c:cmd:growth"), tg.btn("📊 Stats", "c:cmd:stats")],
        [tg.btn("🏆 Top", "c:cmd:top"), tg.btn("📋 List", "c:cmd:list")],
    ])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def cmd_top(chat_id: str, args: str) -> None:
    try:
        n = int(args.strip()) if args.strip() else 10
        n = max(1, min(n, 50))
    except ValueError:
        n = 10

    data = await api.get(_SVC, "/history")  # gets latest for all
    if not data or "error" in data:
        await tg.send(f"❌ {data.get('error', 'Failed')}", chat_id)
        return

    latest = data.get("latest", [])
    with_f = [r for r in latest if r.get("followers") is not None]
    with_f.sort(key=lambda x: x.get("followers", 0), reverse=True)
    top = with_f[:n]

    if not top:
        await tg.send("No data yet. Run a check first.", chat_id)
        return

    lines = [f"<b>👑 Top {len(top)} by Followers</b>", ""]
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(top):
        medal = medals[i] if i < 3 else f"{i+1}."
        f = r.get("followers", 0)
        fg = r.get("following", 0)
        ratio = round(f / fg, 1) if fg else "∞"
        lines.append(f"{medal} @{r['username']} — {f:,}F (ratio {ratio})")

    btn_rows = []
    row = []
    for r in top[:6]:
        row.append(tg.btn(f"📊 {r['username']}", f"c:check:{r['username']}"))
        if len(row) == 3:
            btn_rows.append(row)
            row = []
    if row:
        btn_rows.append(row)
    btn_rows.append([tg.btn("🏆 Growth", "c:cmd:growth"), tg.btn("📈 Stats", "c:cmd:stats")])
    await tg.send("\n".join(lines), chat_id, reply_markup=tg.kb(btn_rows))


async def cmd_settings(chat_id: str, _args: str) -> None:
    health = await api.get(_SVC, "/health") or {}
    stats = await api.get(_SVC, "/stats") or {}
    proxy = health.get("proxy", {})

    lines = [
        "<b>⚙️ Checker Settings</b>",
        "",
        f"👥 Accounts: {stats.get('active_accounts', '?')} active / {stats.get('total_accounts', '?')} total",
        f"🌐 Proxy: {'enabled' if proxy.get('enabled') else 'disabled'}",
    ]
    if proxy.get("enabled"):
        lines.extend([
            f"   Proxies: {proxy.get('count', '?')} ({proxy.get('rotation', '?')})",
            f"   Source: {proxy.get('source', '?')}",
            f"   Current: {proxy.get('current', '?')}",
        ])
        if proxy.get("renew"):
            r = proxy["renew"]
            lines.append(f"   Renew: every {r.get('every', '?')} reqs (total: {r.get('total_renews', 0)})")

    lines.extend([
        "",
        "<i>Edit .env in Checker API to change config.</i>",
    ])
    keyboard = tg.kb([
        [tg.btn("📊 Status", "c:cmd:status"), tg.btn("🏠 Help", "c:cmd:help")],
    ])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


# ── Command map ────────────────────────────────────────────

COMMANDS = {
    "help": cmd_help,
    "start": cmd_help,
    "status": cmd_status,
    "list": cmd_list,
    "add": cmd_add,
    "remove": cmd_remove,
    "enable": cmd_enable,
    "disable": cmd_disable,
    "check": cmd_check,
    "history": cmd_history,
    "groups": cmd_groups,
    "stats": cmd_stats,
    "growth": cmd_growth,
    "7d": cmd_7d,
    "top": cmd_top,
    "settings": cmd_settings,
    "check_all": lambda cid, _: cmd_check(cid, ""),
}


# ── Callback handler ──────────────────────────────────────

async def handle_callback(cb_id: str, chat_id: str, data: str) -> None:
    """Handle callback queries prefixed with 'c:'."""
    parts = data.split(":", maxsplit=2)
    action = parts[0]
    arg1 = parts[1] if len(parts) > 1 else ""
    arg2 = parts[2] if len(parts) > 2 else ""

    if action == "cmd":
        await tg.answer_cb(cb_id)
        handler = COMMANDS.get(arg1)
        if handler:
            await handler(chat_id, "")

    elif action == "check":
        username = arg1
        await tg.answer_cb(cb_id, f"Checking @{username}...")
        await cmd_check(chat_id, username)

    elif action == "history":
        username = arg1
        page = int(arg2) if arg2 else 0
        await tg.answer_cb(cb_id)
        await cmd_history(chat_id, username, page=page)

    elif action == "toggle":
        username = arg1
        # Get current state, then toggle
        acc_data = await api.get(_SVC, f"/accounts/{username}")
        if acc_data and "latest" in acc_data:
            current = acc_data.get("enabled", True)
            await api.put(_SVC, f"/accounts/{username}", json={"enabled": not current})
            state = "disabled ⚪" if current else "enabled 🟢"
            await tg.answer_cb(cb_id, f"@{username} {state}")
            await cmd_list(chat_id, "")
        else:
            await tg.answer_cb(cb_id, f"@{username} not found")

    elif action == "list_page":
        page = int(arg1) if arg1 else 0
        await tg.answer_cb(cb_id)
        await cmd_list(chat_id, "", page=page)

    elif action == "list_group":
        group = arg1
        await tg.answer_cb(cb_id)
        # Fetch filtered accounts
        data = await api.get(_SVC, "/accounts", group=group)
        accounts = data.get("accounts", []) if data else []
        if not accounts:
            await tg.send(f"No accounts in group '{group}'.", chat_id)
            return
        lines = [f"<b>📁 Group: {group} ({len(accounts)})</b>"]
        for acc in accounts:
            en_icon = "🟢" if acc.get("enabled") else "⚪"
            f = acc.get("followers")
            fg = acc.get("following")
            info = f" — {f:,}F / {fg:,}fg" if f is not None else ""
            priv = " 🔒" if acc.get("is_private") else ""
            status = acc.get("last_status", "")
            st_str = ""
            if status and status != "ok":
                st_map = {"not_found": "🔴", "rate_limited": "🟡", "error": "🟠"}
                st_str = f" {st_map.get(status, '⚪')}"
            lines.append(f"{en_icon} @{acc['username']}{info}{priv}{st_str}")
        btn_rows = []
        row = []
        for acc in accounts[:6]:
            row.append(tg.btn(f"🔍 {acc['username']}", f"c:check:{acc['username']}"))
            if len(row) == 3:
                btn_rows.append(row)
                row = []
        if row:
            btn_rows.append(row)
        btn_rows.append([tg.btn("📁 Groups", "c:cmd:groups"), tg.btn("📋 All", "c:cmd:list")])
        await tg.send("\n".join(lines), chat_id, reply_markup=tg.kb(btn_rows))

    else:
        await tg.answer_cb(cb_id, "Unknown action")
