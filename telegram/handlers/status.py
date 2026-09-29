"""
Status handler — ig_status_api commands via HTTP API.
All commands prefixed with /s_ in the bot.
Callback data prefixed with "s:" for routing.
"""

from telegram.api_client import api
import telegram.tg as tg
from logger import logger
from datetime import datetime

_SVC = "status"
_PAGE_SIZE = 6


# ── Formatting helpers ────────────────────────────────────

_STATE_ICONS = {
    "active": "🟢", "banned": "🔴", "expired": "🟠",
    "checkpoint": "🟡", "unknown": "⚪",
}
_SECTION_ICONS = {"ok": "✅", "problem": "🚨", "unknown": "❓"}


def _state_icon(state: str) -> str:
    return _STATE_ICONS.get(state, "⚪")


def _section_icon(status: str) -> str:
    return _SECTION_ICONS.get(status, "❓")


def _short(device_id: str) -> str:
    return device_id[:12] if len(device_id) > 12 else device_id


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
        "<b>🛡️ IG Status API</b>\n\n"
        "<b>📊 Monitoring</b>\n"
        "/s_status — Service status\n"
        "/s_devices — All devices\n"
        "/s_problems — Problem devices\n\n"
        "<b>🔄 Checks</b>\n"
        "/s_check — Check all\n"
        "/s_check &lt;device&gt; — Check one\n"
        "/s_history [device] — History\n"
        "/s_tasks — Active tasks\n\n"
        "<b>📱 Device Management</b>\n"
        "/s_discover — Scan ADB devices\n"
        "/s_adddevice &lt;id&gt; [user] — Add device\n"
        "/s_enable &lt;id&gt; — Enable device\n"
        "/s_disable &lt;id&gt; — Disable device\n"
        "/s_monitor &lt;id&gt; — Start monitoring\n"
        "/s_unmonitor &lt;id&gt; — Stop monitoring\n"
        "/s_setproxy &lt;id&gt; &lt;url&gt; — Set rotate URL\n"
        "/s_adduser &lt;id&gt; &lt;user&gt; — Add account\n"
        "/s_removeuser &lt;id&gt; &lt;user&gt; — Remove account\n\n"
        "<b>⚙️ Scheduler</b>\n"
        "/s_scheduler — Scheduler control\n"
    )
    keyboard = tg.kb([
        [tg.btn("📊 Status", "s:cmd:status"), tg.btn("📱 Devices", "s:cmd:devices")],
        [tg.btn("🔍 Discover", "s:cmd:discover"), tg.btn("🔄 Check All", "s:cmd:check_all")],
        [tg.btn("🚨 Problems", "s:cmd:problems")],
    ])
    await tg.send(text, chat_id, reply_markup=keyboard)


async def cmd_status(chat_id: str, _args: str) -> None:
    devices_data = await api.get(_SVC, "/devices") or {}
    sched = await api.get(_SVC, "/scheduler/status") or {}
    tasks_data = await api.get(_SVC, "/check/tasks") or {}
    statuses = await api.get(_SVC, "/status/latest") or {}

    device_list = devices_data.get("devices", [])
    enabled = [d for d in device_list if d.get("enabled")]
    monitored = [d for d in device_list if d.get("monitored")]
    task_list = tasks_data.get("tasks", [])
    status_list = statuses.get("statuses", statuses.get("results", []))

    # Count states
    state_counts: dict[str, int] = {}
    problems = 0
    for s in status_list:
        st = s.get("account_state", "unknown")
        state_counts[st] = state_counts.get(st, 0) + 1
        sections = s.get("sections", s.get("summary", {}))
        for sec_data in sections.values():
            if isinstance(sec_data, dict) and sec_data.get("status") == "problem":
                problems += 1

    lines = [
        "<b>📊 IG Status API</b>",
        "",
        f"📱 Devices: {len(enabled)} enabled / {len(device_list)} total",
        f"👁 Monitored: {len(monitored)}",
        f"📋 Last checks: {len(status_list)}",
        f"🚨 Problems: {problems} section(s)",
    ]

    if state_counts:
        lines.extend(["", "<b>Account States:</b>"])
        for state, count in sorted(state_counts.items()):
            lines.append(f"  {_state_icon(state)} {state}: {count}")

    lines.extend([
        "",
        f"📅 Scheduler: {'on ✅' if sched.get('running') else 'off ❌'}",
        f"⏰ Daily: {sched.get('daily_time', '?')} ({sched.get('timezone', '?')})",
        f"⏱ Last run: {_time_ago(sched.get('last_run')) if sched.get('last_run') else 'never'}"
        + (f" ({sched.get('last_run_type')})" if sched.get('last_run_type') else ""),
    ])
    if sched.get("next_run"):
        lines.append(f"⏭ Next: {sched['next_run'][:16]}")
    if task_list:
        lines.append(f"\n🔄 Active tasks: {len(task_list)}")

    keyboard = tg.kb([
        [tg.btn("📱 Devices", "s:cmd:devices"), tg.btn("🚨 Problems", "s:cmd:problems")],
        [tg.btn("🔄 Check All", "s:cmd:check_all"), tg.btn("📅 Scheduler", "s:cmd:scheduler")],
    ])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def cmd_devices(chat_id: str, _args: str, page: int = 0) -> None:
    devices_data = await api.get(_SVC, "/devices") or {}
    statuses = await api.get(_SVC, "/status/latest") or {}

    device_list = devices_data.get("devices", [])
    status_list = statuses.get("statuses", statuses.get("results", []))
    status_map = {s.get("device_id"): s for s in status_list}

    if not device_list:
        await tg.send("No devices registered.", chat_id)
        return

    total = len(device_list)
    total_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)
    page_devs = device_list[start:end]

    lines = [f"<b>📱 Devices ({start+1}-{end} of {total})</b>"]
    for d in page_devs:
        did = d.get("device_id", "?")
        en_icon = "🟢" if d.get("enabled") else "⚪"
        mon_icon = "👁" if d.get("monitored") else ""
        s = status_map.get(did, {})
        state = s.get("account_state", "—")
        state_icon = _state_icon(state) if state != "—" else ""
        ago = _time_ago(s.get("checked_at", "")) if s.get("checked_at") else ""
        user = f"@{d.get('username')}" if d.get("username") else "no user"

        sections = s.get("sections", s.get("summary", {}))
        prob_count = sum(1 for v in sections.values() if isinstance(v, dict) and v.get("status") == "problem")
        prob_str = f" 🚨{prob_count}" if prob_count > 0 else ""

        lines.append(
            f"{en_icon}{mon_icon} <code>{_short(did)}</code> — {user}\n"
            f"    {state_icon} {state}{prob_str} {ago}"
        )

    # Per-device buttons
    dev_btns = []
    for d in page_devs:
        did = d.get("device_id", "?")
        dev_btns.append([
            tg.btn(f"🔍 {_short(did)}", f"s:check_dev:{did}"),
            tg.btn("📊", f"s:history_dev:{did}:0"),
            tg.btn("👁" if not d.get("monitored") else "👁‍🗨", f"s:toggle_mon:{did}"),
            tg.btn("✅" if not d.get("enabled") else "❌", f"s:toggle_en:{did}"),
        ])

    # Pagination
    nav = []
    if page > 0:
        nav.append(tg.btn("⬅️ Prev", f"s:dev_page:{page-1}"))
    nav.append(tg.btn(f"{page+1}/{total_pages}", "noop:0"))
    if page < total_pages - 1:
        nav.append(tg.btn("Next ➡️", f"s:dev_page:{page+1}"))

    rows = dev_btns
    if total_pages > 1:
        rows.append(nav)
    rows.append([tg.btn("🔄 Check All", "s:cmd:check_all"), tg.btn("🔍 Discover", "s:cmd:discover")])
    rows.append([tg.btn("📊 Status", "s:cmd:status")])
    await tg.send("\n".join(lines), chat_id, reply_markup=tg.kb(rows))


async def cmd_check(chat_id: str, args: str) -> None:
    did = args.strip()
    if did:
        await _do_single_check(chat_id, did)
    else:
        await tg.send("🔄 Starting check on all monitored devices...", chat_id)
        data = await api.post(_SVC, "/check/force", timeout=600)
        if not data or data.get("error"):
            await tg.send(f"❌ {data.get('error', data.get('detail', 'Failed'))}", chat_id)
            return

        results = data.get("results", [])
        if not results:
            await tg.send("No devices checked.", chat_id)
            return

        ok = sum(1 for r in results if r.get("success"))
        failed = len(results) - ok
        lines = [f"<b>✅ Check Complete — {len(results)} devices</b>", f"Success: {ok}  |  Failed: {failed}", ""]

        states: dict[str, list] = {}
        for r in results:
            st = r.get("account_state", "unknown")
            states.setdefault(st, []).append(r)

        for state in ["active", "banned", "checkpoint", "expired", "unknown"]:
            devs = states.get(state, [])
            if devs:
                lines.append(f"{_state_icon(state)} <b>{state}</b> ({len(devs)}):")
                for r in devs[:5]:
                    user = f"@{r.get('username')}" if r.get("username") else _short(r.get("device_id", "?"))
                    lines.append(f"  • {user}")
                if len(devs) > 5:
                    lines.append(f"  ... +{len(devs)-5} more")

        keyboard = tg.kb([[tg.btn("📱 Devices", "s:cmd:devices"), tg.btn("🚨 Problems", "s:cmd:problems")]])
        await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def _do_single_check(chat_id: str, device_id: str) -> None:
    await tg.send(f"🔄 Checking <code>{_short(device_id)}</code>...", chat_id)
    data = await api.post(_SVC, f"/check/force/{device_id}", timeout=600)
    if not data or data.get("error"):
        await tg.send(f"❌ {data.get('error', data.get('detail', 'Failed'))}", chat_id)
        return

    result = data.get("result", data)
    user = f"@{result.get('username')}" if result.get("username") else _short(device_id)
    state = result.get("account_state", "unknown")
    success = result.get("success", False)
    dur = result.get("duration_ms", 0) // 1000 if result.get("duration_ms") else 0

    lines = [
        f"<b>📊 {user}</b> ({_short(device_id)})",
        "",
        f"{'✅ Success' if success else '❌ Failed'} — {dur}s",
        f"State: {_state_icon(state)} {state}",
    ]
    if result.get("error"):
        lines.append(f"Error: {result['error']}")

    summary = result.get("summary", result.get("sections", {}))
    if summary:
        lines.extend(["", "<b>Sections:</b>"])
        for section, section_data in summary.items():
            if isinstance(section_data, dict):
                s_status = section_data.get("status", "unknown")
                label = section_data.get("label", section)
                detail = section_data.get("detail", "")
                detail_str = f" — {detail[:120]}" if detail and s_status == "problem" else ""
                lines.append(f"  {_section_icon(s_status)} {label}{detail_str}")

    keyboard = tg.kb([
        [tg.btn("🔄 Recheck", f"s:check_dev:{device_id}"), tg.btn("📊 History", f"s:history_dev:{device_id}:0")],
        [tg.btn("📱 Devices", "s:cmd:devices"), tg.btn("📊 Status", "s:cmd:status")],
    ])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def cmd_history(chat_id: str, args: str, page: int = 0) -> None:
    did = args.strip() or None
    per_page = 5
    params = {"limit": per_page + 1, "offset": page * per_page}
    if did:
        params["device_id"] = did

    data = await api.get(_SVC, "/history", **params)
    if not data or "error" in data:
        await tg.send(f"❌ {data.get('error', 'Failed')}", chat_id)
        return

    rows = data.get("history", data.get("results", []))
    has_more = len(rows) > per_page
    rows = rows[:per_page]

    if not rows:
        label = f"<code>{did}</code>" if did else "any device"
        await tg.send(f"No history for {label}.", chat_id)
        return

    title = f"<code>{_short(did)}</code>" if did else "All Devices"
    lines = [f"<b>📋 History — {title}</b> (page {page+1})"]
    for r in rows:
        ts = _time_ago(r.get("checked_at", ""))
        user = f"@{r.get('username')}" if r.get("username") else _short(r.get("device_id", "?"))
        state = r.get("account_state", "unknown")
        success = r.get("success", 0)
        icon = "✅" if success else "❌"
        dur = f" {r.get('duration_ms', 0) // 1000}s" if r.get("duration_ms") else ""
        lines.append(f"{icon} {ts}{dur} — {user} {_state_icon(state)}{state}")

    nav = []
    if page > 0:
        cb = f"s:history_dev:{did}:{page-1}" if did else f"s:history_all:{page-1}"
        nav.append(tg.btn("⬅️ Newer", cb))
    nav.append(tg.btn(f"Page {page+1}", "noop:0"))
    if has_more:
        cb = f"s:history_dev:{did}:{page+1}" if did else f"s:history_all:{page+1}"
        nav.append(tg.btn("Older ➡️", cb))

    btn_rows = []
    if nav:
        btn_rows.append(nav)
    btn_rows.append([tg.btn("📱 Devices", "s:cmd:devices"), tg.btn("📊 Status", "s:cmd:status")])
    await tg.send("\n".join(lines), chat_id, reply_markup=tg.kb(btn_rows))


async def cmd_problems(chat_id: str, _args: str) -> None:
    statuses = await api.get(_SVC, "/status/latest") or {}
    status_list = statuses.get("statuses", statuses.get("results", []))

    if not status_list:
        await tg.send("No check data available.", chat_id)
        return

    problem_devices = []
    problem_sections = []

    for s in status_list:
        user = f"@{s.get('username')}" if s.get("username") else _short(s.get("device_id", "?"))
        state = s.get("account_state", "unknown")

        if state not in ("active", "unknown"):
            problem_devices.append(f"{_state_icon(state)} {user} — <b>{state}</b>")

        if not s.get("success") and s.get("error"):
            err = s['error'][:100] + ("..." if len(s['error']) > 100 else "")
            problem_devices.append(f"❌ {user} — {err}")

        sections = s.get("sections", s.get("summary", {}))
        for sec_key, sec_data in sections.items():
            if isinstance(sec_data, dict) and sec_data.get("status") == "problem":
                label = sec_data.get("label", sec_key)
                problem_sections.append(f"🚨 {user} → {label}")

    if not problem_devices and not problem_sections:
        keyboard = tg.kb([[tg.btn("📊 Status", "s:cmd:status"), tg.btn("📱 Devices", "s:cmd:devices")]])
        await tg.send("✅ <b>All clear!</b> No problems.", chat_id, reply_markup=keyboard)
        return

    lines = ["<b>🚨 Problems</b>"]
    if problem_devices:
        lines.extend(["", "<b>Account Issues:</b>"] + problem_devices[:15])
    if problem_sections:
        lines.extend(["", "<b>Section Problems:</b>"] + problem_sections[:15])

    keyboard = tg.kb([[tg.btn("🔄 Check All", "s:cmd:check_all"), tg.btn("📱 Devices", "s:cmd:devices")]])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def cmd_scheduler(chat_id: str, _args: str) -> None:
    sched = await api.get(_SVC, "/scheduler/status") or {}

    lines = [
        "<b>📅 Scheduler</b>",
        "",
        f"Status: {'Running ✅' if sched.get('running') else 'Stopped ❌'}",
        f"Daily: {sched.get('daily_time', '?')} ({sched.get('timezone', '?')})",
        f"Last run: {_time_ago(sched.get('last_run')) if sched.get('last_run') else 'never'}"
        + (f" ({sched.get('last_run_type')})" if sched.get('last_run_type') else ""),
    ]
    if sched.get("next_run"):
        lines.append(f"Next: {sched['next_run'][:16]}")

    if sched.get("running"):
        keyboard = tg.kb([
            [tg.btn("⏹ Stop", "s:sched:stop")],
            [tg.btn("📊 Status", "s:cmd:status"), tg.btn("🔄 Check Now", "s:cmd:check_all")],
        ])
    else:
        keyboard = tg.kb([
            [tg.btn("▶️ Start", "s:sched:start")],
            [tg.btn("📊 Status", "s:cmd:status"), tg.btn("🔄 Check Now", "s:cmd:check_all")],
        ])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


async def cmd_tasks(chat_id: str, _args: str) -> None:
    data = await api.get(_SVC, "/check/tasks") or {}
    tasks = data.get("tasks", [])
    if not tasks:
        keyboard = tg.kb([[tg.btn("📊 Status", "s:cmd:status"), tg.btn("🔄 Check All", "s:cmd:check_all")]])
        await tg.send("🟢 No active tasks.", chat_id, reply_markup=keyboard)
        return

    lines = [f"<b>🔄 Active Tasks ({len(tasks)})</b>"]
    for t in tasks:
        did = t.get("device_id", "all")
        status = t.get("status", "unknown")
        uname = f"@{t.get('username')}" if t.get("username") else _short(did)
        started = _time_ago(t.get("started_at", t.get("created_at", "")))
        started_str = f" · {started}" if started != "never" else ""
        lines.append(f"  • <code>{t.get('task_id', '?')}</code> — {uname} ({status}){started_str}")

    keyboard = tg.kb([[tg.btn("📊 Status", "s:cmd:status"), tg.btn("📱 Devices", "s:cmd:devices")]])
    await tg.send("\n".join(lines), chat_id, reply_markup=keyboard)


# ── Device management ────────────────────────────────────

async def cmd_discover(chat_id: str, _args: str) -> None:
    await tg.send("🔍 Scanning ADB devices...", chat_id)
    data = await api.get(_SVC, "/devices/discover")
    if not data or "error" in data:
        await tg.send(f"❌ Discover failed: {data.get('error', data.get('detail', '?'))}", chat_id)
        return

    # API returns: online_registered, offline_registered, unregistered, summary
    online = data.get("online_registered", [])
    offline = data.get("offline_registered", [])
    new_devices = data.get("unregistered", [])
    summary = data.get("summary", {})
    total_adb = summary.get("total_adb", len(online) + len(new_devices))

    if total_adb == 0 and not online and not offline and not new_devices:
        keyboard = tg.kb([[tg.btn("📱 Devices", "s:cmd:devices"), tg.btn("📊 Status", "s:cmd:status")]])
        await tg.send("📭 No ADB devices found.", chat_id, reply_markup=keyboard)
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
        btn_rows.append([tg.btn(f"➕ Add {_short(did)}", f"s:adddev:{did}")])
    btn_rows.append([tg.btn("📱 Devices", "s:cmd:devices"), tg.btn("📊 Status", "s:cmd:status")])

    await tg.send("\n".join(lines), chat_id, reply_markup=tg.kb(btn_rows))


async def cmd_adddevice(chat_id: str, args: str) -> None:
    parts = args.strip().split(None, 1)
    if not parts:
        await tg.send("⚠️ Usage: /s_adddevice &lt;device_id&gt; [username]", chat_id)
        return
    device_id = parts[0]
    username = parts[1] if len(parts) > 1 else ""

    data = await api.post(_SVC, "/devices", json={
        "device_id": device_id, "username": username, "label": "Added via Telegram", "enabled": True,
    })
    if data and not data.get("error"):
        uname_str = f"@{username}" if username else "no username"
        keyboard = tg.kb([
            [tg.btn("📡 Monitor", f"s:toggle_mon:{device_id}"), tg.btn("🔍 Check", f"s:check_dev:{device_id}")],
            [tg.btn("📱 Devices", "s:cmd:devices")],
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
        await tg.send("⚠️ Usage: /s_enable &lt;device_id&gt;", chat_id)
        return
    data = await api.post(_SVC, f"/devices/{did}/enable")
    if data and not data.get("error"):
        keyboard = tg.kb([[tg.btn("📱 Devices", "s:cmd:devices")]])
        await tg.send(f"✅ <code>{_short(did)}</code> <b>enabled</b>.", chat_id, reply_markup=keyboard)
    else:
        await tg.send(f"❌ {data.get('detail', data.get('error', 'Not found'))}", chat_id)


async def cmd_disable(chat_id: str, args: str) -> None:
    did = args.strip()
    if not did:
        await tg.send("⚠️ Usage: /s_disable &lt;device_id&gt;", chat_id)
        return
    data = await api.post(_SVC, f"/devices/{did}/disable")
    if data and not data.get("error"):
        keyboard = tg.kb([[tg.btn("📱 Devices", "s:cmd:devices")]])
        await tg.send(f"❌ <code>{_short(did)}</code> <b>disabled</b>.", chat_id, reply_markup=keyboard)
    else:
        await tg.send(f"❌ {data.get('detail', data.get('error', 'Not found'))}", chat_id)


async def cmd_monitor(chat_id: str, args: str) -> None:
    did = args.strip()
    if not did:
        await tg.send("⚠️ Usage: /s_monitor &lt;device_id&gt;", chat_id)
        return
    data = await api.post(_SVC, f"/devices/{did}/monitor")
    if data and not data.get("error"):
        keyboard = tg.kb([[tg.btn("🔍 Check", f"s:check_dev:{did}"), tg.btn("📱 Devices", "s:cmd:devices")]])
        await tg.send(f"👁 <code>{_short(did)}</code> monitoring <b>enabled</b>.", chat_id, reply_markup=keyboard)
    else:
        await tg.send(f"❌ {data.get('detail', data.get('error', 'Not found'))}", chat_id)


async def cmd_unmonitor(chat_id: str, args: str) -> None:
    did = args.strip()
    if not did:
        await tg.send("⚠️ Usage: /s_unmonitor &lt;device_id&gt;", chat_id)
        return
    data = await api.delete(_SVC, f"/devices/{did}/monitor")
    if data and not data.get("error"):
        keyboard = tg.kb([[tg.btn("📱 Devices", "s:cmd:devices")]])
        await tg.send(f"🚫 <code>{_short(did)}</code> monitoring <b>disabled</b>.", chat_id, reply_markup=keyboard)
    else:
        await tg.send(f"❌ {data.get('detail', data.get('error', 'Not found'))}", chat_id)


async def cmd_setproxy(chat_id: str, args: str) -> None:
    """Set proxy rotate URL for a device. Usage: /s_setproxy <device_id> <url> (or 'off' to clear)"""
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2:
        await tg.send("⚠️ Usage: /s_setproxy &lt;device_id&gt; &lt;url|off&gt;\n\nExample:\n<code>/s_setproxy R5CT415H8HJ https://proxy.example.com/rotate</code>\n<code>/s_setproxy R5CT415H8HJ off</code>", chat_id)
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
        await tg.send("⚠️ Usage: /s_adduser &lt;device_id&gt; &lt;username&gt;", chat_id)
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
        await tg.send("⚠️ Usage: /s_removeuser &lt;device_id&gt; &lt;username&gt;", chat_id)
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
    "status": cmd_status,
    "devices": cmd_devices,
    "discover": cmd_discover,
    "adddevice": cmd_adddevice,
    "enable": cmd_enable,
    "disable": cmd_disable,
    "monitor": cmd_monitor,
    "unmonitor": cmd_unmonitor,
    "check": cmd_check,
    "history": cmd_history,
    "problems": cmd_problems,
    "scheduler": cmd_scheduler,
    "tasks": cmd_tasks,
    "check_all": lambda cid, _: cmd_check(cid, ""),
    "setproxy": cmd_setproxy,
    "adduser": cmd_adduser,
    "removeuser": cmd_removeuser,
}


# ── Callback handler ──────────────────────────────────────

async def handle_callback(cb_id: str, chat_id: str, data: str) -> None:
    """Handle callback queries prefixed with 's:'."""
    parts = data.split(":", maxsplit=2)
    action = parts[0]
    arg1 = parts[1] if len(parts) > 1 else ""
    arg2 = parts[2] if len(parts) > 2 else ""

    if action == "cmd":
        await tg.answer_cb(cb_id)
        handler = COMMANDS.get(arg1)
        if handler:
            await handler(chat_id, "")

    elif action == "check_dev":
        did = arg1
        await tg.answer_cb(cb_id, f"Checking {_short(did)}...")
        await _do_single_check(chat_id, did)

    elif action == "history_dev":
        did = arg1
        page = int(arg2) if arg2 else 0
        await tg.answer_cb(cb_id)
        await cmd_history(chat_id, did, page=page)

    elif action == "history_all":
        page = int(arg1) if arg1 else 0
        await tg.answer_cb(cb_id)
        await cmd_history(chat_id, "", page=page)

    elif action == "dev_page":
        page = int(arg1) if arg1 else 0
        await tg.answer_cb(cb_id)
        await cmd_devices(chat_id, "", page=page)

    elif action == "toggle_mon":
        did = arg1
        dev_data = await api.get(_SVC, "/devices")
        devices = dev_data.get("devices", []) if dev_data else []
        dev = next((d for d in devices if d.get("device_id") == did), None)
        if dev:
            if dev.get("monitored"):
                await api.delete(_SVC, f"/devices/{did}/monitor")
                label = "unmonitored 🚫"
            else:
                await api.post(_SVC, f"/devices/{did}/monitor")
                label = "monitored 👁"
            await tg.answer_cb(cb_id, f"{_short(did)} → {label}")
            await cmd_devices(chat_id, "")
        else:
            await tg.answer_cb(cb_id, "Device not found")

    elif action == "toggle_en":
        did = arg1
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
        did = arg1
        await tg.answer_cb(cb_id, f"Adding {_short(did)}...")
        await cmd_adddevice(chat_id, did)

    elif action == "sched":
        if arg1 == "start":
            await api.post(_SVC, "/scheduler/start")
            await tg.answer_cb(cb_id, "Scheduler started ✅")
            await cmd_scheduler(chat_id, "")
        elif arg1 == "stop":
            await api.post(_SVC, "/scheduler/stop")
            await tg.answer_cb(cb_id, "Scheduler stopped ❌")
            await cmd_scheduler(chat_id, "")

    else:
        await tg.answer_cb(cb_id, "Unknown action")
