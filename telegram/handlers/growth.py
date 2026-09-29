"""
Growth handler — ig_growth_api commands via HTTP API.
All commands prefixed with /g_ in the bot.
Callback data prefixed with "g:" for routing.
"""

from datetime import datetime

from telegram.api_client import api
import telegram.tg as tg
from logger import logger

_SVC = "growth"

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


def _short(device_id: str) -> str:
    return device_id[:10] if len(device_id) > 10 else device_id


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
        "<b>📊 IG Growth API</b>\n\n"
        "<b>🔄 Checks</b>\n"
        "/g_run &lt;device&gt; — Check single device\n"
        "/g_runall — Check all monitored\n"
        "/g_trials [device] — Trial reels\n\n"
        "<b>🎬 Content</b>\n"
        "/g_pending — Pending videos\n"
        "/g_unregistered — Missing URLs\n"
        "/g_register title | url — Register\n"
        "/g_resolve &lt;id&gt; &lt;url&gt; — Resolve\n"
        "/g_content [search] — Search content\n\n"
        "<b>📱 Devices</b>\n"
        "/g_devices — List devices\n"
        "/g_discover — Scan ADB\n"
        "/g_adddevice &lt;id&gt; [user] — Add\n"
        "/g_enable &lt;id&gt; — Enable\n"
        "/g_disable &lt;id&gt; — Disable\n"
        "/g_monitor &lt;id&gt; — Start monitoring\n"
        "/g_unmonitor &lt;id&gt; — Stop monitoring\n"
        "/g_setproxy &lt;id&gt; &lt;url&gt; — Set rotate URL\n"
        "/g_adduser &lt;id&gt; &lt;user&gt; — Add account\n"
        "/g_removeuser &lt;id&gt; &lt;user&gt; — Remove account\n\n"
        "<b>📈 Analytics</b>\n"
        "/g_7d — 7-day growth report\n\n"
        "<b>⚙️ System</b>\n"
        "/g_status — Status & tasks\n"
        "/g_schedule [HH:MM|on|off] — Scheduler\n"
    )
    keyboard = tg.kb([
        [tg.btn("📱 Devices", "g:cmd:devices"), tg.btn("📊 Status", "g:cmd:status")],
        [tg.btn("📈 7d Growth", "g:cmd:7d"), tg.btn("🔄 Run All", "g:cmd:runall")],
        [tg.btn("🎬 Trials", "g:cmd:trials")],
        [tg.btn("📋 Pending", "g:cmd:pending"), tg.btn("🔍 Unregistered", "g:cmd:unregistered")],
    ])
    await tg.send(text, chat_id, reply_markup=keyboard)


async def cmd_devices(chat_id: str, _args: str) -> None:
    data = await api.get(_SVC, "/devices")
    if not data or "error" in data:
        await tg.send(f"❌ Growth API error: {data.get('error', 'unreachable')}", chat_id)
        return

    devices = data.get("devices", [])
    if not devices:
        await tg.send("❌ No devices registered.", chat_id)
        return

    lines = [f"<b>📱 Devices ({len(devices)})</b>\n"]
    for d in devices:
        en = "✅" if d.get("enabled") else "❌"
        mon = " 📡" if d.get("monitored") else ""
        name = f"@{d['username']}" if d.get("username") else d.get("label") or "—"
        did = d.get("device_id", "?")
        state = d.get("account_state", "")
        st_str = f" {_state_icon(state)}{state}" if state else ""
        last = _time_ago(d.get("last_checked", d.get("checked_at", "")))
        last_str = f" · {last}" if last != "never" else ""
        lines.append(f"{en}{mon} <code>{_short(did)}</code>\n   {name}{st_str}{last_str}")

    # Action buttons per device
    dev_btns = []
    for d in devices:
        did = d.get("device_id", "?")
        dev_btns.append([
            tg.btn(f"🔍 {_short(did)}", f"g:run:{did}"),
            tg.btn("📡" if not d.get("monitored") else "🚫", f"g:toggle_mon:{did}"),
            tg.btn("✅" if not d.get("enabled") else "❌", f"g:toggle_en:{did}"),
        ])
    dev_btns.append([tg.btn("🔄 Run All", "g:cmd:runall"), tg.btn("🔍 Discover", "g:cmd:discover")])

    await tg.send("\n".join(lines), chat_id, reply_markup=tg.kb(dev_btns))


async def cmd_discover(chat_id: str, _args: str) -> None:
    await tg.send("🔍 Scanning ADB devices...", chat_id)
    data = await api.get(_SVC, "/devices/discover")
    if not data or "error" in data:
        await tg.send(f"❌ Discover failed: {data.get('error', '?')}", chat_id)
        return

    # API returns: online_registered, offline_registered, unregistered, summary
    online = data.get("online_registered", [])
    offline = data.get("offline_registered", [])
    new_devices = data.get("unregistered", [])
    summary = data.get("summary", {})
    total_adb = summary.get("total_adb", len(online) + len(new_devices))

    if total_adb == 0 and not online and not offline and not new_devices:
        await tg.send("📭 No ADB devices found.", chat_id)
        return

    lines = [f"<b>🔍 ADB Scan</b> — {total_adb} ADB device(s)\n"]

    for d in online:
        did = d.get("device_id", "?")
        model = d.get("model", "?")
        user = f"@{d['username']}" if d.get("username") else d.get("label", "—")
        lines.append(f"✅ <code>{_short(did)}</code> — {model} ({user})")

    for d in offline:
        did = d.get("device_id", "?")
        state = d.get("state", "offline")
        user = f"@{d['username']}" if d.get("username") else d.get("label", "—")
        lines.append(f"⚠️ <code>{_short(did)}</code> — {state} ({user})")

    for d in new_devices:
        did = d.get("device_id", "?")
        model = d.get("model", "?")
        lines.append(f"🆕 <code>{_short(did)}</code> — {model} (<b>not registered</b>)")

    btn_rows = []
    for nd in new_devices[:4]:
        did = nd.get("device_id", "?")
        btn_rows.append([tg.btn(f"➕ Add {_short(did)}", f"g:adddev:{did}")])
    btn_rows.append([tg.btn("📱 Devices", "g:cmd:devices"), tg.btn("📊 Status", "g:cmd:status")])

    await tg.send("\n".join(lines), chat_id, reply_markup=tg.kb(btn_rows))


async def cmd_adddevice(chat_id: str, args: str) -> None:
    parts = args.strip().split(None, 1)
    if not parts:
        await tg.send("⚠️ Usage: /g_adddevice &lt;device_id&gt; [username]", chat_id)
        return
    device_id = parts[0]
    username = parts[1] if len(parts) > 1 else ""

    data = await api.post(_SVC, "/devices", json={
        "device_id": device_id, "username": username, "label": "Added via Telegram", "enabled": True,
    })
    if data and not data.get("error"):
        uname_str = f"@{username}" if username else "no username"
        keyboard = tg.kb([
            [tg.btn("📡 Monitor", f"g:toggle_mon:{device_id}"), tg.btn("🔍 Run", f"g:run:{device_id}")],
            [tg.btn("📱 Devices", "g:cmd:devices")],
        ])
        await tg.send(
            f"✅ Device added!\n\n📱 <code>{device_id}</code>\n👤 {uname_str}",
            chat_id, reply_markup=keyboard,
        )
    else:
        await tg.send(f"❌ {data.get('detail', data.get('error', 'Failed'))}", chat_id)


async def cmd_enable(chat_id: str, args: str) -> None:
    did = args.strip()
    if not did:
        await tg.send("⚠️ Usage: /g_enable &lt;device_id&gt;", chat_id)
        return
    data = await api.post(_SVC, f"/devices/{did}/enable")
    if data and not data.get("error"):
        keyboard = tg.kb([[tg.btn("📱 Devices", "g:cmd:devices")]])
        await tg.send(f"✅ <code>{did}</code> <b>enabled</b>.", chat_id, reply_markup=keyboard)
    else:
        await tg.send(f"❌ {data.get('detail', 'Not found')}", chat_id)


async def cmd_disable(chat_id: str, args: str) -> None:
    did = args.strip()
    if not did:
        await tg.send("⚠️ Usage: /g_disable &lt;device_id&gt;", chat_id)
        return
    data = await api.post(_SVC, f"/devices/{did}/disable")
    if data and not data.get("error"):
        keyboard = tg.kb([[tg.btn("📱 Devices", "g:cmd:devices")]])
        await tg.send(f"❌ <code>{did}</code> <b>disabled</b>.", chat_id, reply_markup=keyboard)
    else:
        await tg.send(f"❌ {data.get('detail', 'Not found')}", chat_id)


async def cmd_monitor(chat_id: str, args: str) -> None:
    did = args.strip()
    if not did:
        await tg.send("⚠️ Usage: /g_monitor &lt;device_id&gt;", chat_id)
        return
    data = await api.post(_SVC, f"/devices/{did}/monitor")
    if data and not data.get("error"):
        keyboard = tg.kb([[tg.btn("🔍 Run", f"g:run:{did}"), tg.btn("📱 Devices", "g:cmd:devices")]])
        await tg.send(f"📡 <code>{did}</code> monitoring <b>enabled</b>.", chat_id, reply_markup=keyboard)
    else:
        await tg.send(f"❌ {data.get('detail', 'Not found')}", chat_id)


async def cmd_unmonitor(chat_id: str, args: str) -> None:
    did = args.strip()
    if not did:
        await tg.send("⚠️ Usage: /g_unmonitor &lt;device_id&gt;", chat_id)
        return
    data = await api.delete(_SVC, f"/devices/{did}/monitor")
    if data and not data.get("error"):
        keyboard = tg.kb([[tg.btn("📱 Devices", "g:cmd:devices")]])
        await tg.send(f"🚫 <code>{did}</code> monitoring <b>disabled</b>.", chat_id, reply_markup=keyboard)
    else:
        await tg.send(f"❌ {data.get('detail', 'Not found')}", chat_id)


async def cmd_run(chat_id: str, args: str) -> None:
    did = args.strip()
    if not did:
        await tg.send("⚠️ Usage: /g_run &lt;device_id&gt;", chat_id)
        return

    await tg.send(f"🔄 Starting check on <code>{_short(did)}</code>...", chat_id)
    data = await api.post(_SVC, f"/check/force/{did}", timeout=600)
    if not data or data.get("error"):
        await tg.send(f"❌ Check failed: {data.get('error', data.get('detail', '?'))}", chat_id)
        return

    keyboard = tg.kb([
        [tg.btn("🔄 Recheck", f"g:run:{did}"), tg.btn("🎬 Trials", f"g:trials:{did}")],
        [tg.btn("📱 Devices", "g:cmd:devices"), tg.btn("📊 Status", "g:cmd:status")],
    ])

    # Multi-account response has "results" (plural); single-account is the result itself
    results = data.get("results")
    if results and isinstance(results, list):
        lines = [f"<b>✅ Check Complete</b> — <code>{_short(did)}</code> ({len(results)} accounts)\n"]
        for r in results:
            lines.append(_format_check_result(r))
        await tg.send("\n\n".join(lines), chat_id, reply_markup=keyboard)
    else:
        result = data.get("result", data)
        await tg.send(_format_check_result(result), chat_id, reply_markup=keyboard)


async def cmd_runall(chat_id: str, _args: str) -> None:
    await tg.send("🔄 Starting check on all monitored devices...", chat_id)
    data = await api.post(_SVC, "/check/force", timeout=600)
    if not data or data.get("error"):
        await tg.send(f"❌ {data.get('error', data.get('detail', 'Failed'))}", chat_id)
        return

    results = data.get("results", [])
    if not results:
        await tg.send("No devices checked.", chat_id)
        return

    lines = [f"<b>✅ Check Complete</b> — {len(results)} devices\n"]
    for r in results:
        lines.append(_format_check_summary(r))

    btn_rows = []
    row = []
    for r in results:
        did = r.get("device_id", "?")
        row.append(tg.btn(f"📊 {_short(did)}", f"g:run:{did}"))
        if len(row) == 3:
            btn_rows.append(row)
            row = []
    if row:
        btn_rows.append(row)
    btn_rows.append([tg.btn("🎬 Trials", "g:cmd:trials"), tg.btn("📱 Devices", "g:cmd:devices")])
    await tg.send("\n".join(lines), chat_id, reply_markup=tg.kb(btn_rows))


async def cmd_trials(chat_id: str, args: str) -> None:
    did = args.strip() or None
    path = f"/metrics/trials/{did}" if did else "/metrics/trials"
    data = await api.get(_SVC, path)
    if not data or "error" in data:
        await tg.send("📭 No trial reels data found.", chat_id)
        return

    reels = data.get("reels", data.get("trials", []))
    if not reels:
        await tg.send("📭 No trial reels data found.", chat_id)
        return

    # Group by device + check time
    groups: dict[str, list] = {}
    for r in reels:
        key = f"{r.get('device_id', '?')}|{r.get('checked_at', '')}"
        groups.setdefault(key, []).append(r)

    lines = [f"<b>🎬 Trial Reels</b> ({len(reels)} reels)\n"]
    for key, grp in list(groups.items())[:5]:
        first = grp[0]
        uname = f"@{first['username']}" if first.get("username") else _short(first.get("device_id", "?"))
        ts = first.get("checked_at", "")[:16].replace("T", " ")
        lines.append(f"📱 <b>{uname}</b> — {ts}")
        for r in grp:
            avail = r.get("is_available", 1)
            idx = r.get("reel_index", 0) + 1
            title = r.get("title", "") or f"Reel #{idx}"
            if not avail:
                lines.append(f"  {idx}. <i>{title}</i> — not available")
                continue
            views = r.get("views")
            v_str = f"{views:,}" if isinstance(views, int) else "—"
            likes = r.get("likes")
            l_str = f"{likes:,}" if isinstance(likes, int) else "—"
            lines.append(f"  {idx}. <b>{title}</b>\n     👁 {v_str}  ❤️ {l_str}")
        lines.append("")

    keyboard = tg.kb([[tg.btn("🔄 Run All", "g:cmd:runall"), tg.btn("📱 Devices", "g:cmd:devices")]])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def cmd_7d(chat_id: str, _args: str) -> None:
    """7-day growth report for all devices."""
    data = await api.get(_SVC, "/metrics/growth-7d") or {}
    devices = data.get("devices", [])
    if not devices:
        await tg.send("No 7-day data available. Need checks spanning several days.", chat_id)
        return

    lines = ["<b>📊 7-Day Growth Report</b>", ""]

    for d in devices:
        username = d.get("username", "?")
        score = d.get("score", 50)
        label = d.get("score_label", "neutral")
        checks = d.get("checks_in_period", 0)
        deltas = d.get("deltas", {})

        # Score emoji
        if score >= 70:
            icon = "🟢"
        elif score >= 55:
            icon = "🟡"
        elif score >= 40:
            icon = "🟠"
        else:
            icon = "🔴"

        lines.append(f"{icon} <b>@{username}</b> — {label} ({score}/100) · {checks} checks")

        # Metric deltas
        metric_labels = {
            "views": ("👁", "Views"),
            "interactions": ("❤️", "Interactions"),
            "new_followers": ("👥", "New Followers"),
            "accounts_reached": ("📣", "Reached"),
            "content_shared": ("📝", "Content"),
        }
        for key, (emoji, name) in metric_labels.items():
            delta = deltas.get(key, {})
            val = delta.get("value")
            pct = delta.get("pct")
            if val is not None:
                sign = "+" if val >= 0 else ""
                pct_str = f" ({sign}{pct}%)" if pct is not None else ""
                curr = d.get("current", {}).get(key)
                curr_str = f" → {_fmt(curr)}" if curr is not None else ""
                lines.append(f"  {emoji} {name}: {sign}{_fmt(val)}{pct_str}{curr_str}")

        lines.append("")

    keyboard = tg.kb([
        [tg.btn("📊 Status", "g:cmd:status"), tg.btn("📱 Devices", "g:cmd:devices")],
        [tg.btn("🔄 Run All", "g:cmd:runall")],
    ])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def cmd_status(chat_id: str, _args: str) -> None:
    sched = await api.get(_SVC, "/scheduler/status") or {}
    tasks = await api.get(_SVC, "/check/tasks") or {}
    devices = await api.get(_SVC, "/devices") or {}

    device_list = devices.get("devices", [])
    enabled = [d for d in device_list if d.get("enabled")]
    monitored = [d for d in device_list if d.get("monitored")]
    task_list = tasks.get("tasks", [])

    sched_icon = "✅" if sched.get("running") else "❌"
    lines = [
        "<b>📊 Growth System Status</b>\n",
        f"{sched_icon} Scheduler: {'ON' if sched.get('running') else 'OFF'}",
        f"⏰ Daily: {sched.get('daily_time', '?')} ({sched.get('timezone', '?')})",
    ]
    if sched.get("next_run"):
        lines.append(f"➡️ Next: {sched['next_run'][:16].replace('T', ' ')}")
    if sched.get("last_run"):
        ago = _time_ago(sched["last_run"])
        lines.append(f"🔄 Last: {ago} ({sched.get('last_run_type', '?')})")

    lines.append(f"\n📱 Devices: {len(enabled)} enabled, {len(monitored)} monitored")

    # Show account state summary from devices
    state_counts: dict[str, int] = {}
    for d in device_list:
        st = d.get("account_state", "")
        if st:
            state_counts[st] = state_counts.get(st, 0) + 1
    if state_counts:
        parts = [f"{_state_icon(s)}{s}: {c}" for s, c in sorted(state_counts.items(), key=lambda x: -x[1])]
        lines.append(f"📋 States: {' | '.join(parts)}")

    if task_list:
        lines.append(f"\n⏳ <b>Active Tasks</b> ({len(task_list)})")
        for t in task_list:
            did = t.get("device_id", "?")
            uname = f"@{t.get('username')}" if t.get("username") else _short(did)
            lines.append(f"  • <code>{t.get('task_id', '?')}</code> — {uname} ({t.get('status', '?')})")
    else:
        lines.append("\n🟢 No active tasks")

    if sched.get("running"):
        keyboard = tg.kb([
            [tg.btn("⏹ Sched Off", "g:sched:off"), tg.btn("🔄 Run All", "g:cmd:runall")],
            [tg.btn("📱 Devices", "g:cmd:devices"), tg.btn("🎬 Trials", "g:cmd:trials")],
        ])
    else:
        keyboard = tg.kb([
            [tg.btn("▶️ Sched On", "g:sched:on"), tg.btn("🔄 Run All", "g:cmd:runall")],
            [tg.btn("📱 Devices", "g:cmd:devices"), tg.btn("🎬 Trials", "g:cmd:trials")],
        ])

    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def cmd_schedule(chat_id: str, args: str) -> None:
    args = args.strip().lower()
    if not args:
        await cmd_status(chat_id, "")
        return

    if args == "off":
        await api.post(_SVC, "/scheduler/stop")
        keyboard = tg.kb([[tg.btn("▶️ Turn On", "g:sched:on"), tg.btn("📊 Status", "g:cmd:status")]])
        await tg.send("❌ Scheduler <b>disabled</b>.", chat_id, reply_markup=keyboard)
        return

    if args == "on":
        await api.post(_SVC, "/scheduler/start")
        sched = await api.get(_SVC, "/scheduler/status") or {}
        keyboard = tg.kb([[tg.btn("⏹ Turn Off", "g:sched:off"), tg.btn("📊 Status", "g:cmd:status")]])
        await tg.send(f"✅ Scheduler <b>enabled</b> — daily at {sched.get('daily_time', '?')}", chat_id, reply_markup=keyboard)
        return

    # Parse HH:MM
    try:
        parts = args.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, IndexError):
        await tg.send("⚠️ Usage: /g_schedule HH:MM or /g_schedule on|off", chat_id)
        return

    await api.post(_SVC, "/scheduler/start", json={"hour": hour, "minute": minute})
    sched = await api.get(_SVC, "/scheduler/status") or {}
    keyboard = tg.kb([[tg.btn("📊 Status", "g:cmd:status")]])
    await tg.send(f"✅ Schedule updated — daily at <b>{sched.get('daily_time', '?')}</b>", chat_id, reply_markup=keyboard)


async def cmd_pending(chat_id: str, _args: str) -> None:
    data = await api.get(_SVC, "/content/pending")
    if not data or "error" in data:
        await tg.send("❌ Error fetching pending.", chat_id)
        return

    pending = data.get("pending", [])
    if not pending:
        keyboard = tg.kb([[tg.btn("🎬 Trials", "g:cmd:trials"), tg.btn("📊 Status", "g:cmd:status")]])
        await tg.send("✅ No pending videos!", chat_id, reply_markup=keyboard)
        return

    lines = [f"<b>📋 Pending Videos</b> ({len(pending)})\n"]
    for p in pending[:20]:
        pid = p.get("id", "?")
        title = p.get("title", "?")
        username = p.get("username", "")
        uname = f"@{username}" if username else ""
        ts = p.get("created_at", "")[:10]
        lines.append(f"<code>{pid}</code> — {title}\n   {uname} · {ts}")
    if len(pending) > 20:
        lines.append(f"\n<i>...and {len(pending) - 20} more</i>")
    lines.append("\n📝 /g_resolve &lt;id&gt; &lt;url&gt;")

    keyboard = tg.kb([[tg.btn("🔍 Unregistered", "g:cmd:unregistered"), tg.btn("🎬 Content", "g:cmd:content")]])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def cmd_register(chat_id: str, args: str) -> None:
    if "|" not in args:
        await tg.send("⚠️ Usage: /g_register title | url [| username]", chat_id)
        return

    parts = [p.strip() for p in args.split("|")]
    title = parts[0]
    url = parts[1] if len(parts) > 1 else ""
    username = parts[2] if len(parts) > 2 else ""

    if not title or not url:
        await tg.send("❌ Both title and url required.", chat_id)
        return
    if not url.startswith("http"):
        await tg.send("❌ URL must start with http:// or https://", chat_id)
        return

    data = await api.post(_SVC, "/content/videos", json={
        "title": title, "url": url, "username": username,
    })
    if data and not data.get("error"):
        keyboard = tg.kb([[tg.btn("📋 Pending", "g:cmd:pending"), tg.btn("🎬 Content", "g:cmd:content")]])
        await tg.send(f"✅ Registered!\n\n🎬 <b>{title}</b>\n🔗 {url}", chat_id, reply_markup=keyboard)
    else:
        await tg.send(f"❌ {data.get('detail', data.get('error', 'Failed'))}", chat_id)


async def cmd_resolve(chat_id: str, args: str) -> None:
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        await tg.send("⚠️ Usage: /g_resolve &lt;id&gt; &lt;url&gt;", chat_id)
        return

    pending_id = parts[0]
    url = parts[1].strip()
    if not url.startswith("http"):
        await tg.send("❌ URL must start with http:// or https://", chat_id)
        return

    data = await api.post(_SVC, f"/content/pending/{pending_id}/resolve", json={"url": url})
    if data and not data.get("error"):
        keyboard = tg.kb([[tg.btn("📋 Pending", "g:cmd:pending")]])
        await tg.send(f"✅ Resolved #{pending_id}!\n🔗 {url}", chat_id, reply_markup=keyboard)
    else:
        await tg.send(f"❌ {data.get('detail', data.get('error', 'Failed'))}", chat_id)


async def cmd_content(chat_id: str, args: str) -> None:
    search = args.strip()
    params = {}
    if search:
        params["search"] = search

    data = await api.get(_SVC, "/content/videos", **params)
    if not data or "error" in data:
        await tg.send("❌ Error.", chat_id)
        return

    videos = data.get("videos", [])
    total = data.get("total", len(videos))
    if not videos:
        msg = f"No content found for '{search}'." if search else "No registered content."
        await tg.send(f"📭 {msg}", chat_id)
        return

    label = f"matching '{search}'" if search else "registered"
    lines = [f"<b>🎬 Content</b> — {total} {label}\n"]
    for v in videos[:15]:
        vid = v.get("id", "?")
        title = v.get("title", "?")
        url = v.get("url", "")
        lines.append(f"<code>{vid}</code> <b>{title}</b>\n   🔗 {url}")
    if total > 15:
        lines.append(f"\n<i>...{total - 15} more</i>")

    keyboard = tg.kb([[tg.btn("📋 Pending", "g:cmd:pending"), tg.btn("🔍 Unregistered", "g:cmd:unregistered")]])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def cmd_unregistered(chat_id: str, _args: str) -> None:
    """Show content without URLs — checks trials + pending."""
    lines = ["<b>🔍 Unregistered Content</b>\n"]
    found_any = False

    # Check pending
    pend_data = await api.get(_SVC, "/content/pending") or {}
    pending = pend_data.get("pending", [])
    if pending:
        found_any = True
        lines.append(f"<b>📋 Pending Videos</b> ({len(pending)})")
        for p in pending[:10]:
            pid = p.get("id", "?")
            title = p.get("title", "?")
            lines.append(f"  <code>{pid}</code> {title}")
        if len(pending) > 10:
            lines.append(f"  <i>...{len(pending) - 10} more</i>")
        lines.append(f"\n📝 /g_resolve &lt;id&gt; &lt;url&gt;")

    # Check trials without URL
    trial_data = await api.get(_SVC, "/metrics/trials") or {}
    reels = trial_data.get("reels", trial_data.get("trials", []))
    seen_titles: set[str] = set()
    missing = []
    for r in reels:
        if not r.get("is_available", 1):
            continue
        title = r.get("title", "") or ""
        ig_url = r.get("ig_url", "") or ""
        if not ig_url and title and title not in seen_titles:
            seen_titles.add(title)
            missing.append(r)

    if missing:
        found_any = True
        lines.append(f"\n<b>🧪 Trials without URL</b> ({len(missing)})")
        for r in missing[:10]:
            title = r.get("title", "?")
            views = r.get("views")
            v_str = f"{views:,}" if isinstance(views, int) else "—"
            lines.append(f"  • {title} — {v_str} views")
        if len(missing) > 10:
            lines.append(f"  <i>...{len(missing) - 10} more</i>")

    if not found_any:
        keyboard = tg.kb([[tg.btn("🎬 Content", "g:cmd:content"), tg.btn("📊 Status", "g:cmd:status")]])
        await tg.send("✅ All content has URLs!", chat_id, reply_markup=keyboard)
    else:
        keyboard = tg.kb([[tg.btn("📋 Pending", "g:cmd:pending"), tg.btn("🎬 Content", "g:cmd:content")]])
        await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


# ── Result formatting ────────────────────────────────────

def _format_check_result(r: dict) -> str:
    success = r.get("success", False)
    device = r.get("device_id", "?")
    username = r.get("username", "")
    uname = f"@{username}" if username else _short(device)
    state = r.get("account_state", "")
    state_str = f"\n{_state_icon(state)} Account: {state}" if state else ""

    if not success:
        error = r.get("error", "Unknown error")
        return f"❌ <b>Check Failed</b> — {uname}{state_str}\n\n{error}"

    dur = r.get("duration_ms", 0)
    dur_s = f"{dur / 1000:.1f}s" if dur else "—"
    date_range = r.get("date_range", "—")

    lines = [f"✅ <b>Check Complete</b> — {uname}{state_str}", f"📅 {date_range}  ⏱ {dur_s}\n"]

    for label, key, icon in [
        ("Views", "views", "👁"), ("Interactions", "interactions", "💬"),
        ("New Followers", "new_followers", "👥"), ("Content Shared", "content_shared", "📤"),
        ("Accounts Reached", "accounts_reached", "🌍"),
    ]:
        val = r.get(key)
        if val is not None:
            v = f"{val:,}" if isinstance(val, int) else str(val)
            lines.append(f"{icon} {label}: <b>{v}</b>")

    views_detail = r.get("views_detail", [])
    if views_detail:
        lines.append(f"\n<b>🎬 Top Content</b> ({len(views_detail)} items)")
        for i, v in enumerate(views_detail[:5], 1):
            t = v.get("type", "?")
            d = v.get("date", "")
            vw = v.get("views", 0)
            vw_str = f"{vw:,}" if isinstance(vw, int) else str(vw)
            lines.append(f"  {i}. {t} · {d} — {vw_str} views")

    # Only show trial reels on success
    trial = r.get("trial_reels", {})
    trial_reels = trial.get("reels", [])
    if trial_reels:
        lines.append(f"\n<b>🧪 Trial Reels</b> ({len(trial_reels)})")
        for tr in trial_reels[:5]:
            idx = tr.get("reel_index", 0) + 1
            title = tr.get("title", "") or f"Reel #{idx}"
            avail = tr.get("is_available", True)
            if not avail:
                lines.append(f"  {idx}. <i>{title}</i> — not available")
                continue
            tv = tr.get("views")
            tv_str = f"{tv:,}" if isinstance(tv, int) else "—"
            lines.append(f"  {idx}. {title} — {tv_str} views")

    return "\n".join(lines)


def _format_check_summary(r: dict) -> str:
    device = r.get("device_id", "?")
    username = r.get("username", "")
    uname = f"@{username}" if username else _short(device)
    success = r.get("success", False)
    state = r.get("account_state", "")
    st = f" {_state_icon(state)}" if state else ""
    if not success:
        err = r.get("error", "unknown")
        if len(err) > 80:
            err = err[:77] + "..."
        return f"❌ {uname}{st} — {err}"
    views = r.get("views")
    v_str = f"{views:,}" if isinstance(views, int) else "—"
    inter = r.get("interactions")
    i_str = f"{inter:,}" if isinstance(inter, int) else "—"
    return f"✅ {uname}{st} — 👁 {v_str} 💬 {i_str}"


async def cmd_setproxy(chat_id: str, args: str) -> None:
    """Set proxy rotate URL for a device. Usage: /g_setproxy <device_id> <url> (or 'off' to clear)"""
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2:
        await tg.send("⚠️ Usage: /g_setproxy &lt;device_id&gt; &lt;url|off&gt;\n\nExample:\n<code>/g_setproxy R5CT415H8HJ https://proxy.example.com/rotate</code>\n<code>/g_setproxy R5CT415H8HJ off</code>", chat_id)
        return
    did, url = parts[0], parts[1]
    if url.lower() == "off":
        url = ""
    data = await api.put(_SVC, f"/devices/{did}", params={"proxy_rotate_url": url})
    if data and not data.get("error"):
        if url:
            await tg.send(f"🔄 <code>{did}</code> proxy rotate URL set:\n<code>{url}</code>", chat_id)
        else:
            await tg.send(f"🚫 <code>{did}</code> proxy rotate URL <b>cleared</b>.", chat_id)
    else:
        await tg.send(f"❌ {data.get('detail', data.get('error', 'Failed'))}", chat_id)


async def cmd_adduser(chat_id: str, args: str) -> None:
    """Add IG username to a device's multi-account list."""
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2:
        await tg.send("⚠️ Usage: /g_adduser &lt;device_id&gt; &lt;username&gt;", chat_id)
        return
    did, user = parts[0], parts[1].strip().lstrip("@").lower()
    # Get current usernames
    data = await api.get(_SVC, f"/devices/{did}")
    if not data or data.get("error"):
        await tg.send(f"❌ Device not found", chat_id)
        return
    current = data.get("usernames", [])
    if user in current:
        await tg.send(f"ℹ️ @{user} already in device list", chat_id)
        return
    current.append(user)
    result = await api.put(_SVC, f"/devices/{did}", params={"usernames": ",".join(current)})
    if result and not result.get("error"):
        await tg.send(f"✅ @{user} added to <code>{did}</code>\n📋 Accounts: {', '.join(current)}", chat_id)
    else:
        await tg.send(f"❌ {result.get('detail', result.get('error', 'Failed'))}", chat_id)


async def cmd_removeuser(chat_id: str, args: str) -> None:
    """Remove IG username from a device's multi-account list."""
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2:
        await tg.send("⚠️ Usage: /g_removeuser &lt;device_id&gt; &lt;username&gt;", chat_id)
        return
    did, user = parts[0], parts[1].strip().lstrip("@").lower()
    data = await api.get(_SVC, f"/devices/{did}")
    if not data or data.get("error"):
        await tg.send(f"❌ Device not found", chat_id)
        return
    current = data.get("usernames", [])
    if user not in current:
        await tg.send(f"ℹ️ @{user} not in device list", chat_id)
        return
    current.remove(user)
    result = await api.put(_SVC, f"/devices/{did}", params={"usernames": ",".join(current)})
    if result and not result.get("error"):
        remaining = ', '.join(current) if current else 'none'
        await tg.send(f"🗑 @{user} removed from <code>{did}</code>\n📋 Accounts: {remaining}", chat_id)
    else:
        await tg.send(f"❌ {result.get('detail', result.get('error', 'Failed'))}", chat_id)


# ── Command map ────────────────────────────────────────────

COMMANDS = {
    "help": cmd_help,
    "start": cmd_help,
    "devices": cmd_devices,
    "discover": cmd_discover,
    "adddevice": cmd_adddevice,
    "enable": cmd_enable,
    "disable": cmd_disable,
    "monitor": cmd_monitor,
    "unmonitor": cmd_unmonitor,
    "run": cmd_run,
    "runall": cmd_runall,
    "trials": cmd_trials,
    "7d": cmd_7d,
    "status": cmd_status,
    "schedule": cmd_schedule,
    "pending": cmd_pending,
    "register": cmd_register,
    "resolve": cmd_resolve,
    "content": cmd_content,
    "unregistered": cmd_unregistered,
    "setproxy": cmd_setproxy,
    "adduser": cmd_adduser,
    "removeuser": cmd_removeuser,
}


# ── Callback handler ──────────────────────────────────────

async def handle_callback(cb_id: str, chat_id: str, data: str) -> None:
    """Handle callback queries prefixed with 'g:'."""
    parts = data.split(":", maxsplit=1)
    action = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    if action == "cmd":
        await tg.answer_cb(cb_id)
        handler = COMMANDS.get(arg)
        if handler:
            await handler(chat_id, "")

    elif action == "run":
        await tg.answer_cb(cb_id, f"Checking {_short(arg)}...")
        await cmd_run(chat_id, arg)

    elif action == "trials":
        await tg.answer_cb(cb_id)
        await cmd_trials(chat_id, arg)

    elif action == "toggle_mon":
        did = arg
        # Get current state
        dev_data = await api.get(_SVC, "/devices")
        devices = dev_data.get("devices", []) if dev_data else []
        dev = next((d for d in devices if d.get("device_id") == did), None)
        if dev:
            if dev.get("monitored"):
                await api.delete(_SVC, f"/devices/{did}/monitor")
                label = "unmonitored 🚫"
            else:
                await api.post(_SVC, f"/devices/{did}/monitor")
                label = "monitored 📡"
            await tg.answer_cb(cb_id, f"{_short(did)} → {label}")
            await cmd_devices(chat_id, "")
        else:
            await tg.answer_cb(cb_id, "Device not found")

    elif action == "toggle_en":
        did = arg
        dev_data = await api.get(_SVC, "/devices")
        devices = dev_data.get("devices", []) if dev_data else []
        dev = next((d for d in devices if d.get("device_id") == did), None)
        if dev:
            if dev.get("enabled"):
                await api.post(_SVC, f"/devices/{did}/disable")
                label = "disabled ❌"
            else:
                await api.post(_SVC, f"/devices/{did}/enable")
                label = "enabled ✅"
            await tg.answer_cb(cb_id, f"{_short(did)} → {label}")
            await cmd_devices(chat_id, "")
        else:
            await tg.answer_cb(cb_id, "Device not found")

    elif action == "adddev":
        did = arg
        await tg.answer_cb(cb_id, f"Adding {_short(did)}...")
        await cmd_adddevice(chat_id, did)

    elif action == "sched":
        await tg.answer_cb(cb_id)
        await cmd_schedule(chat_id, arg)

    else:
        await tg.answer_cb(cb_id, "Unknown action")
