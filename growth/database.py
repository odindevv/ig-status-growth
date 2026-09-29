"""
SQLite persistence for growth metrics history and content registry.
Uses aiosqlite with WAL mode.
  - Writer: single connection protected by asyncio.Lock
  - Reader: separate connection for concurrent reads
  - Retention: auto-prune old checks beyond configured max days

Tables:
  - metrics: full history of every extraction (1 row per device per run)
  - latest_metrics: last known metrics per device (upsert)
  - views_detail: trending content items from Views drill-down
  - trial_reels: trial reel metrics per device per check
  - content_videos: video title → IG URL registry
  - content_pending: pending videos awaiting URL registration
"""

import asyncio
import json
import aiosqlite
from datetime import datetime, timezone, timedelta
from pathlib import Path
from config import growth_settings as settings
from logger import logger


_writer: aiosqlite.Connection | None = None
_reader: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()


def _db_path() -> Path:
    """Resolve database path relative to project root."""
    p = Path(settings.db_path)
    if not p.is_absolute():
        p = Path(__file__).parent.parent / p
    return p


async def _open_connection(readonly: bool = False) -> aiosqlite.Connection:
    """Open an aiosqlite connection with shared WAL pragmas."""
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{db_path}?mode=ro" if readonly else str(db_path)
    conn = await aiosqlite.connect(uri, uri=readonly)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=10000")
    if readonly:
        await conn.execute("PRAGMA query_only=ON")
    return conn


async def init_db() -> None:
    """Initialize SQLite database with schema (writer + reader)."""
    global _writer, _reader

    _writer = await _open_connection(readonly=False)

    await _writer.executescript("""
        CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            success INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            date_range TEXT DEFAULT '',
            views INTEGER,
            interactions INTEGER,
            new_followers INTEGER,
            accounts_reached INTEGER,
            content_shared INTEGER,
            ui_model TEXT DEFAULT '',
            views_detail_json TEXT,
            navigation_steps TEXT,
            duration_ms INTEGER DEFAULT 0,
            checked_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_metrics_device ON metrics(device_id);
        CREATE INDEX IF NOT EXISTS idx_metrics_checked_at ON metrics(checked_at DESC);
        CREATE INDEX IF NOT EXISTS idx_metrics_username ON metrics(username);

        CREATE TABLE IF NOT EXISTS latest_metrics (
            device_id TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            success INTEGER NOT NULL DEFAULT 0,
            date_range TEXT DEFAULT '',
            views INTEGER,
            interactions INTEGER,
            new_followers INTEGER,
            accounts_reached INTEGER,
            content_shared INTEGER,
            ui_model TEXT DEFAULT '',
            views_detail_json TEXT,
            error TEXT,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (device_id, username)
        );

        CREATE TABLE IF NOT EXISTS viral_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            content_id TEXT NOT NULL DEFAULT '',
            content_type TEXT DEFAULT '',
            content_date TEXT DEFAULT '',
            rank INTEGER DEFAULT 0,
            current_views INTEGER DEFAULT 0,
            previous_views INTEGER,
            viral_score INTEGER DEFAULT 0,
            status TEXT DEFAULT '',
            signals_json TEXT,
            checked_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_viral_device ON viral_alerts(device_id);
        CREATE INDEX IF NOT EXISTS idx_viral_score ON viral_alerts(viral_score DESC);
        CREATE INDEX IF NOT EXISTS idx_viral_created ON viral_alerts(created_at DESC);

        CREATE TABLE IF NOT EXISTS trial_reels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            reel_index INTEGER NOT NULL DEFAULT 0,
            grid_views INTEGER,
            views INTEGER,
            watch_time TEXT,
            likes INTEGER,
            comments INTEGER,
            shares INTEGER,
            interactions INTEGER,
            profile_activity INTEGER,
            title TEXT DEFAULT '',
            reel_date TEXT DEFAULT '',
            duration TEXT DEFAULT '',
            is_available INTEGER NOT NULL DEFAULT 1,
            ig_url TEXT DEFAULT '',
            checked_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_trial_device ON trial_reels(device_id);
        CREATE INDEX IF NOT EXISTS idx_trial_checked ON trial_reels(checked_at DESC);

        CREATE TABLE IF NOT EXISTS content_videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL UNIQUE,
            url TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_cv_title ON content_videos(title);
        CREATE INDEX IF NOT EXISTS idx_cv_username ON content_videos(username);

        CREATE TABLE IF NOT EXISTS content_pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(title)
        );
        CREATE INDEX IF NOT EXISTS idx_cp_title ON content_pending(title);
    """)
    await _writer.commit()

    # Migration: upgrade latest_metrics PK from device_id-only to composite (device_id, username)
    try:
        async with _writer.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='latest_metrics'") as cursor:
            row = await cursor.fetchone()
        if row and 'PRIMARY KEY (device_id, username)' not in (row[0] or ''):
            await _writer.execute("DROP TABLE IF EXISTS latest_metrics")
            await _writer.execute("""CREATE TABLE latest_metrics (
                device_id TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                success INTEGER NOT NULL DEFAULT 0,
                date_range TEXT DEFAULT '',
                views INTEGER,
                interactions INTEGER,
                new_followers INTEGER,
                accounts_reached INTEGER,
                content_shared INTEGER,
                ui_model TEXT DEFAULT '',
                views_detail_json TEXT,
                extended_json TEXT,
                error TEXT,
                checked_at TEXT NOT NULL,
                PRIMARY KEY (device_id, username)
            )""")
            await _writer.commit()
            logger.info("[db] Migrated latest_metrics to composite PK (device_id, username)")
    except Exception as e:
        logger.warning(f"[db] Migration check failed: {e}")

    # ── Auto-migrate: add new columns if missing (safe for existing DBs) ──
    for table in ("metrics", "latest_metrics"):
        existing = {
            row[1]
            for row in await (await _writer.execute(f"PRAGMA table_info({table})")).fetchall()
        }
        for col, col_type in [("accounts_reached", "INTEGER"), ("ui_model", "TEXT DEFAULT ''"), ("extended_json", "TEXT")]:
            if col not in existing:
                await _writer.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                logger.info(f"[db] Migrated: added '{col}' to {table}")

    # Migrate trial_reels: add ig_url if missing
    try:
        trial_cols = {
            row[1]
            for row in await (await _writer.execute("PRAGMA table_info(trial_reels)")).fetchall()
        }
        if trial_cols and "ig_url" not in trial_cols:
            await _writer.execute("ALTER TABLE trial_reels ADD COLUMN ig_url TEXT DEFAULT ''")
            logger.info("[db] Migrated: added 'ig_url' to trial_reels")
    except Exception:
        pass  # Table may not exist yet on fresh DB

    await _writer.commit()

    _reader = await _open_connection(readonly=True)

    logger.info(f"[db] Database initialized at {_db_path()} (writer + reader)")


async def close_db() -> None:
    global _writer, _reader
    if _reader:
        await _reader.close()
        _reader = None
    if _writer:
        await _writer.close()
        _writer = None
    logger.info("[db] Database closed")


# ── Write operations (use _writer under lock) ──────────────

async def save_metric(result: dict) -> None:
    """Save a metric extraction result to history and update latest."""
    if not _writer:
        logger.error("[db] save_metric called but database not initialized — data NOT persisted")
        return
    async with _write_lock:
        try:
            await _save_metric_inner(result)
        except Exception as e:
            logger.error(f"[db] Failed to save metric for {result.get('device_id', '?')}: {e}")
            raise  # let caller know persistence failed


async def _save_metric_inner(result: dict) -> None:
    """Inner save logic, called under _write_lock."""
    now = datetime.now(timezone.utc).isoformat()
    views_detail = result.get("views_detail", [])
    nav_steps = result.get("navigation_steps", [])
    extended = result.get("extended", {})

    await _writer.execute(
        """INSERT INTO metrics
           (device_id, username, success, error, date_range,
            views, interactions, new_followers, accounts_reached, content_shared,
            ui_model, views_detail_json, extended_json, navigation_steps, duration_ms, checked_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            result["device_id"],
            result.get("username", ""),
            1 if result.get("success") else 0,
            result.get("error"),
            result.get("date_range", ""),
            result.get("views"),
            result.get("interactions"),
            result.get("new_followers"),
            result.get("accounts_reached"),
            result.get("content_shared"),
            result.get("ui_model", ""),
            json.dumps(views_detail) if views_detail else None,
            json.dumps(extended) if extended else None,
            json.dumps(nav_steps),
            result.get("duration_ms", 0),
            now,
        ),
    )

    # Update latest metrics
    await _writer.execute(
        """INSERT OR REPLACE INTO latest_metrics
           (device_id, username, success, date_range,
            views, interactions, new_followers, accounts_reached, content_shared,
            ui_model, views_detail_json, extended_json, error, checked_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            result["device_id"],
            result.get("username", ""),
            1 if result.get("success") else 0,
            result.get("date_range", ""),
            result.get("views"),
            result.get("interactions"),
            result.get("new_followers"),
            result.get("accounts_reached"),
            result.get("content_shared"),
            result.get("ui_model", ""),
            json.dumps(views_detail) if views_detail else None,
            json.dumps(extended) if extended else None,
            result.get("error"),
            now,
        ),
    )
    await _writer.commit()


async def prune_old_metrics(max_days: int = 0) -> int:
    """Delete metrics older than max_days. Returns count deleted."""
    if not _writer:
        return 0
    days = max_days or settings.db_retention_days
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with _write_lock:
        cursor = await _writer.execute(
            "DELETE FROM metrics WHERE checked_at < ?", (cutoff,)
        )
        count = cursor.rowcount
        if count > 0:
            await _writer.commit()
            logger.info(f"[db] Pruned {count} metrics older than {days} days")
    return count


# ── Read operations (use _reader, no lock needed) ──────────

async def get_latest_metrics() -> list[dict]:
    """Return latest metrics for all devices."""
    if not _reader:
        return []
    cursor = await _reader.execute(
        "SELECT * FROM latest_metrics ORDER BY checked_at DESC"
    )
    rows = await cursor.fetchall()
    results = []
    for r in rows:
        row_dict = dict(r)
        if row_dict.get("views_detail_json"):
            try:
                row_dict["views_detail"] = json.loads(row_dict["views_detail_json"])
            except (json.JSONDecodeError, TypeError):
                row_dict["views_detail"] = []
        else:
            row_dict["views_detail"] = []
        if row_dict.get("extended_json"):
            try:
                row_dict["extended"] = json.loads(row_dict["extended_json"])
            except (json.JSONDecodeError, TypeError):
                row_dict["extended"] = {}
        else:
            row_dict["extended"] = {}
        results.append(row_dict)
    return results


async def get_last_two_successful(device_id: str, username: str | None = None) -> list[dict]:
    """Return the last 2 successful checks for a device+username (newest first).
    If username is provided, filters by that account; otherwise any on the device.
    """
    if not _reader:
        return []
    if username:
        cursor = await _reader.execute(
            """SELECT * FROM metrics
               WHERE device_id = ? AND username = ? AND success = 1
               ORDER BY checked_at DESC LIMIT 2""",
            (device_id, username),
        )
    else:
        cursor = await _reader.execute(
            """SELECT * FROM metrics
               WHERE device_id = ? AND success = 1
               ORDER BY checked_at DESC LIMIT 2""",
            (device_id,),
        )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_successful_history(device_id: str, days: int = 30, username: str | None = None) -> list[dict]:
    """Return all successful checks for a device (optionally filtered by username) within N days (oldest first)."""
    if not _reader:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    if username:
        cursor = await _reader.execute(
            """SELECT * FROM metrics
               WHERE device_id = ? AND username = ? AND success = 1 AND checked_at >= ?
               ORDER BY checked_at ASC""",
            (device_id, username, cutoff),
        )
    else:
        cursor = await _reader.execute(
            """SELECT * FROM metrics
               WHERE device_id = ? AND success = 1 AND checked_at >= ?
               ORDER BY checked_at ASC""",
            (device_id, cutoff),
        )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_history(
    device_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Query metric history with optional device filter."""
    if not _reader:
        return []

    query = "SELECT * FROM metrics"
    params = []
    if device_id:
        query += " WHERE device_id = ?"
        params.append(device_id)
    query += " ORDER BY checked_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor = await _reader.execute(query, params)
    rows = await cursor.fetchall()

    results = []
    for r in rows:
        row_dict = dict(r)
        if row_dict.get("views_detail_json"):
            try:
                row_dict["views_detail"] = json.loads(row_dict["views_detail_json"])
            except (json.JSONDecodeError, TypeError):
                row_dict["views_detail"] = []
        if row_dict.get("navigation_steps"):
            try:
                row_dict["navigation_steps"] = json.loads(row_dict["navigation_steps"])
            except (json.JSONDecodeError, TypeError):
                row_dict["navigation_steps"] = []
        if row_dict.get("extended_json"):
            try:
                row_dict["extended"] = json.loads(row_dict["extended_json"])
            except (json.JSONDecodeError, TypeError):
                row_dict["extended"] = {}
        results.append(row_dict)

    return results


# ── Viral Alerts persistence ─────────────────────────────

async def save_viral_alerts(alerts: list[dict]) -> int:
    """
    Persist viral alert records with deduplication.
    Skips alerts where the same device_id + content_id was saved within
    the configured dedup window (default 6 hours).
    Returns count saved.
    """
    if not _writer or not alerts:
        return 0
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    dedup_hours = settings.viral_dedup_hours if settings.viral_dedup_hours > 0 else 6
    dedup_cutoff = (now - timedelta(hours=dedup_hours)).isoformat()
    count = 0
    skipped = 0
    async with _write_lock:
        for a in alerts:
            try:
                device_id = a.get("device_id", "")
                content_id = a.get("content_id", "")

                # ── Dedup check: skip if recent duplicate exists ──
                if device_id and content_id:
                    cursor = await _writer.execute(
                        """SELECT COUNT(*) FROM viral_alerts
                           WHERE device_id = ? AND content_id = ? AND created_at > ?""",
                        (device_id, content_id, dedup_cutoff),
                    )
                    row = await cursor.fetchone()
                    if row and row[0] > 0:
                        skipped += 1
                        continue

                await _writer.execute(
                    """INSERT INTO viral_alerts
                       (device_id, username, content_id, content_type, content_date,
                        rank, current_views, previous_views, viral_score, status,
                        signals_json, checked_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        device_id,
                        a.get("username", ""),
                        content_id,
                        a.get("type", ""),
                        a.get("date", ""),
                        a.get("rank", 0),
                        a.get("current_views", 0),
                        a.get("previous_views"),
                        a.get("viral_score", 0),
                        a.get("status", ""),
                        json.dumps(a.get("signals", [])),
                        a.get("checked_at", now_iso),
                        now_iso,
                    ),
                )
                count += 1
            except Exception as e:
                logger.error(f"[db] Failed to save viral alert: {e}")
        await _writer.commit()
    if count:
        logger.info(f"[db] Saved {count} viral alerts (skipped {skipped} duplicates)")
    elif skipped:
        logger.debug(f"[db] All {skipped} viral alerts were duplicates — nothing saved")
    return count


async def clear_all_viral_alerts() -> int:
    """Delete ALL viral alerts. Returns count deleted."""
    if not _writer:
        return 0
    async with _write_lock:
        cursor = await _writer.execute("DELETE FROM viral_alerts")
        count = cursor.rowcount
        if count > 0:
            await _writer.commit()
            logger.info(f"[db] Cleared all {count} viral alerts")
    return count


async def prune_old_viral_alerts(max_days: int = 0) -> int:
    """Delete viral alerts older than max_days. Returns count deleted."""
    if not _writer:
        return 0
    days = max_days or settings.viral_retention_days
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with _write_lock:
        cursor = await _writer.execute(
            "DELETE FROM viral_alerts WHERE created_at < ?", (cutoff,)
        )
        count = cursor.rowcount
        if count > 0:
            await _writer.commit()
            logger.info(f"[db] Pruned {count} viral alerts older than {days} days")
    return count


async def get_viral_alerts(
    device_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    min_score: int = 0,
    days: int = 0,
) -> list[dict]:
    """Query viral alert history, newest first.

    Filters:
      device_id  — only alerts for this device.
      min_score  — only alerts with viral_score >= min_score.
      days       — only alerts whose checked_at falls within the last N days.
                   0 disables the filter. Uses checked_at (the real detection
                   moment) when present, falling back to created_at.
    """
    if not _reader:
        return []
    conditions = []
    params = []
    if device_id:
        conditions.append("device_id = ?")
        params.append(device_id)
    if min_score > 0:
        conditions.append("viral_score >= ?")
        params.append(min_score)
    if days and days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        conditions.append("COALESCE(NULLIF(checked_at, ''), created_at) >= ?")
        params.append(cutoff)

    query = "SELECT * FROM viral_alerts"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor = await _reader.execute(query, params)
    rows = await cursor.fetchall()
    results = []
    for r in rows:
        row_dict = dict(r)
        if row_dict.get("signals_json"):
            try:
                row_dict["signals"] = json.loads(row_dict["signals_json"])
            except (json.JSONDecodeError, TypeError):
                row_dict["signals"] = []
        else:
            row_dict["signals"] = []
        results.append(row_dict)
    return results


async def get_viral_alerts_global_top(limit: int = 7, days: int = 0) -> list[dict]:
    """Return top N viral alerts by score across all devices (latest per content_id).

    If `days` > 0, only alerts whose checked_at falls within that window are
    considered. Uses checked_at when present, otherwise created_at.
    """
    if not _reader:
        return []
    if days and days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = await _reader.execute(
            """SELECT * FROM viral_alerts
               WHERE id IN (
                   SELECT MAX(id) FROM viral_alerts
                   WHERE COALESCE(NULLIF(checked_at, ''), created_at) >= ?
                   GROUP BY device_id, content_id
               )
               ORDER BY viral_score DESC LIMIT ?""",
            (cutoff, limit),
        )
    else:
        cursor = await _reader.execute(
            """SELECT * FROM viral_alerts
               WHERE id IN (
                   SELECT MAX(id) FROM viral_alerts GROUP BY device_id, content_id
               )
               ORDER BY viral_score DESC LIMIT ?""",
            (limit,),
        )
    rows = await cursor.fetchall()
    results = []
    for r in rows:
        row_dict = dict(r)
        if row_dict.get("signals_json"):
            try:
                row_dict["signals"] = json.loads(row_dict["signals_json"])
            except (json.JSONDecodeError, TypeError):
                row_dict["signals"] = []
        else:
            row_dict["signals"] = []
        results.append(row_dict)
    return results


# ── Trial Reels persistence ──────────────────────────────

async def save_trial_reels(device_id: str, username: str, reels: list[dict]) -> int:
    """Save trial reel data from a single check. Returns count saved."""
    if not _writer or not reels:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    async with _write_lock:
        for reel in reels:
            try:
                await _writer.execute(
                    """INSERT INTO trial_reels
                       (device_id, username, reel_index, grid_views,
                        views, watch_time, likes, comments, shares,
                        interactions, profile_activity, title, reel_date,
                        duration, is_available, ig_url, checked_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        device_id,
                        username,
                        reel.get("reel_index", 0),
                        reel.get("grid_views"),
                        reel.get("views"),
                        reel.get("watch_time", ""),
                        reel.get("likes"),
                        reel.get("comments"),
                        reel.get("shares"),
                        reel.get("interactions"),
                        reel.get("profile_activity"),
                        reel.get("title", ""),
                        reel.get("reel_date", ""),
                        reel.get("duration", ""),
                        1 if reel.get("is_available", True) else 0,
                        reel.get("ig_url", ""),
                        now,
                    ),
                )
                count += 1
            except Exception as e:
                logger.error(f"[db] Failed to save trial reel: {e}")
        await _writer.commit()
    if count:
        logger.info(f"[db] Saved {count} trial reels for {device_id}")
    return count


async def get_trial_reels(
    device_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return trial reels from the latest check per device+username (not all history)."""
    if not _reader:
        return []

    if device_id:
        # Single device — delegate to get_latest_trial_reels
        return await get_latest_trial_reels(device_id)

    # All devices: get the latest checked_at per device+username pair
    cursor = await _reader.execute(
        "SELECT device_id, username, MAX(checked_at) as last_check FROM trial_reels GROUP BY device_id, username"
    )
    latest = await cursor.fetchall()
    if not latest:
        return []

    # Build query to fetch only the latest check rows per device+username
    conditions = []
    params = []
    for row in latest:
        conditions.append("(device_id = ? AND username = ? AND checked_at = ?)")
        params.append(row["device_id"])
        params.append(row["username"])
        params.append(row["last_check"])

    where = " OR ".join(conditions)
    cursor = await _reader.execute(
        f"SELECT * FROM trial_reels WHERE {where} ORDER BY checked_at DESC, reel_index ASC LIMIT ?",
        params + [limit],
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_latest_trial_reels(device_id: str, username: str | None = None) -> list[dict]:
    """Return trial reels from the most recent check for a device (optionally per username)."""
    if not _reader:
        return []

    if username:
        # Single account on device
        cursor = await _reader.execute(
            "SELECT MAX(checked_at) as last_check FROM trial_reels WHERE device_id = ? AND username = ?",
            (device_id, username),
        )
        row = await cursor.fetchone()
        if not row or not row["last_check"]:
            return []
        cursor = await _reader.execute(
            """SELECT * FROM trial_reels
               WHERE device_id = ? AND username = ? AND checked_at = ?
               ORDER BY reel_index ASC""",
            (device_id, username, row["last_check"]),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # All accounts on device: get latest per username
    cursor = await _reader.execute(
        "SELECT username, MAX(checked_at) as last_check FROM trial_reels WHERE device_id = ? GROUP BY username",
        (device_id,),
    )
    latest = await cursor.fetchall()
    if not latest:
        return []

    conditions = []
    params = []
    for row in latest:
        conditions.append("(device_id = ? AND username = ? AND checked_at = ?)")
        params.append(device_id)
        params.append(row["username"])
        params.append(row["last_check"])

    where = " OR ".join(conditions)
    cursor = await _reader.execute(
        f"SELECT * FROM trial_reels WHERE {where} ORDER BY checked_at DESC, reel_index ASC",
        params,
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ── Content Registry (videos + pending) ───────────────────

async def content_add_video(title: str, url: str, username: str = "") -> dict:
    """Register a video title → URL mapping. Returns the row dict."""
    if not _writer:
        return {}
    async with _write_lock:
        await _writer.execute(
            "INSERT OR REPLACE INTO content_videos (title, url, username) VALUES (?, ?, ?)",
            (title.strip(), url.strip(), username.strip()),
        )
        await _writer.commit()
    cursor = await _reader.execute(
        "SELECT * FROM content_videos WHERE title = ?", (title.strip(),)
    )
    row = await cursor.fetchone()
    return dict(row) if row else {"title": title, "url": url, "username": username}


async def content_get_by_title(title: str) -> dict | None:
    """Exact title lookup."""
    if not _reader:
        return None
    cursor = await _reader.execute(
        "SELECT * FROM content_videos WHERE title = ?", (title.strip(),)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def content_search(query: str) -> list[dict]:
    """Search videos by partial title match."""
    if not _reader:
        return []
    cursor = await _reader.execute(
        "SELECT * FROM content_videos WHERE title LIKE ? ORDER BY created_at DESC",
        (f"%{query.strip()}%",),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def content_list_videos(
    limit: int = 200, offset: int = 0, username: str | None = None,
) -> list[dict]:
    """List registered videos, newest first."""
    if not _reader:
        return []
    if username:
        cursor = await _reader.execute(
            "SELECT * FROM content_videos WHERE username = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (username.strip(), limit, offset),
        )
    else:
        cursor = await _reader.execute(
            "SELECT * FROM content_videos ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    return [dict(r) for r in await cursor.fetchall()]


async def content_get_usernames() -> list[str]:
    """Return distinct non-empty usernames from content_videos."""
    if not _reader:
        return []
    cursor = await _reader.execute(
        "SELECT DISTINCT username FROM content_videos WHERE username != '' ORDER BY username ASC"
    )
    return [r[0] for r in await cursor.fetchall()]


async def content_update_video(
    video_id: int, title: str | None = None, url: str | None = None, username: str | None = None,
) -> dict | None:
    """Partial update of a video. Returns updated row or None."""
    if not _writer:
        return None
    async with _write_lock:
        if title:
            await _writer.execute("UPDATE content_videos SET title = ? WHERE id = ?", (title.strip(), video_id))
        if url:
            await _writer.execute("UPDATE content_videos SET url = ? WHERE id = ?", (url.strip(), video_id))
        if username is not None:
            await _writer.execute("UPDATE content_videos SET username = ? WHERE id = ?", (username.strip(), video_id))
        await _writer.commit()
    cursor = await _reader.execute("SELECT * FROM content_videos WHERE id = ?", (video_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def content_delete_video(video_id: int) -> bool:
    """Delete a registered video."""
    if not _writer:
        return False
    async with _write_lock:
        cursor = await _writer.execute("DELETE FROM content_videos WHERE id = ?", (video_id,))
        await _writer.commit()
    return cursor.rowcount > 0


async def content_count_videos() -> int:
    """Total count of registered videos."""
    if not _reader:
        return 0
    cursor = await _reader.execute("SELECT COUNT(*) FROM content_videos")
    row = await cursor.fetchone()
    return row[0] if row else 0


# ── Content Pending ───────────────────────────────────────

async def content_add_pending(title: str, username: str = "", device_id: str = "") -> dict | None:
    """Add a pending video. Skips if already registered or already pending."""
    if not _writer:
        return None
    # Skip if already registered
    existing = await content_get_by_title(title)
    if existing:
        return None
    async with _write_lock:
        try:
            await _writer.execute(
                "INSERT OR IGNORE INTO content_pending (title, username, device_id) VALUES (?, ?, ?)",
                (title.strip(), username.strip(), device_id.strip()),
            )
            await _writer.commit()
        except Exception as e:
            if "UNIQUE" in str(e):
                return None
            raise
    cursor = await _reader.execute(
        "SELECT * FROM content_pending WHERE title = ?", (title.strip(),)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def content_list_pending(limit: int = 100) -> list[dict]:
    """List pending videos, newest first."""
    if not _reader:
        return []
    cursor = await _reader.execute(
        "SELECT * FROM content_pending ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in await cursor.fetchall()]


async def content_count_pending() -> int:
    """Total count of pending videos."""
    if not _reader:
        return 0
    cursor = await _reader.execute("SELECT COUNT(*) FROM content_pending")
    row = await cursor.fetchone()
    return row[0] if row else 0


async def content_delete_pending(pending_id: int) -> bool:
    """Delete a pending item."""
    if not _writer:
        return False
    async with _write_lock:
        cursor = await _writer.execute("DELETE FROM content_pending WHERE id = ?", (pending_id,))
        await _writer.commit()
    return cursor.rowcount > 0


async def content_clear_all_pending() -> int:
    """Delete ALL pending items. Returns count deleted."""
    if not _writer:
        return 0
    async with _write_lock:
        cursor = await _writer.execute("DELETE FROM content_pending")
        count = cursor.rowcount
        if count > 0:
            await _writer.commit()
            logger.info(f"[db] Cleared all {count} pending videos")
    return count


async def content_resolve_pending(pending_id: int, url: str) -> dict | None:
    """Resolve a pending video: register it with the URL and remove from pending."""
    if not _writer or not _reader:
        return None
    cursor = await _reader.execute("SELECT * FROM content_pending WHERE id = ?", (pending_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    p = dict(row)
    video = await content_add_video(p["title"], url, p.get("username", ""))
    async with _write_lock:
        await _writer.execute("DELETE FROM content_pending WHERE id = ?", (pending_id,))
        await _writer.commit()
    return video
