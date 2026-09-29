"""
SQLite database — accounts registry + check history.
"""

import asyncio
import aiosqlite
from pathlib import Path
from datetime import datetime, timedelta, timezone

from config import checker_settings as settings
from logger import logger

_write_lock = asyncio.Lock()  # serialize all DB writes to prevent corruption

def _db_path() -> Path:
    """Resolve DB path relative to project root (where main.py lives)."""
    p = Path(settings.db_path)
    if not p.is_absolute():
        p = Path(__file__).parent.parent / p
    return p
_writer: aiosqlite.Connection | None = None
_reader: aiosqlite.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL UNIQUE,
    label       TEXT    NOT NULL DEFAULT '',
    group_name  TEXT    NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    added_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    last_check  TEXT
);

CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL,
    followers   INTEGER,
    following   INTEGER,
    posts       INTEGER,
    bio         TEXT    NOT NULL DEFAULT '',
    is_private  INTEGER NOT NULL DEFAULT 0,
    full_name   TEXT    NOT NULL DEFAULT '',
    profile_pic TEXT    NOT NULL DEFAULT '',
    checked_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    status      TEXT    NOT NULL DEFAULT 'ok',
    error       TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_history_user   ON history(username, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_date   ON history(checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_accounts_user  ON accounts(username);
"""


async def init_db():
    """Create DB file + tables."""
    global _writer, _reader
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)

    _writer = await aiosqlite.connect(str(db))
    _writer.row_factory = aiosqlite.Row
    await _writer.executescript(_SCHEMA)
    await _writer.execute("PRAGMA journal_mode=WAL")
    await _writer.execute("PRAGMA busy_timeout=5000")
    await _writer.commit()

    _reader = await aiosqlite.connect(str(db))
    _reader.row_factory = aiosqlite.Row
    await _reader.execute("PRAGMA journal_mode=WAL")

    logger.info(f"Database ready: {db}")


async def close_db():
    """Close database connections."""
    global _writer, _reader
    if _writer:
        await _writer.close()
        _writer = None
    if _reader:
        await _reader.close()
        _reader = None
    logger.info("Checker database closed")


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    return dict(row)


# ── Accounts CRUD ───────────────────────────────────────────

async def add_account(username: str, label: str = "", group_name: str = "") -> dict:
    username = username.lower().strip().lstrip("@")
    async with _write_lock:
        await _writer.execute(
            "INSERT INTO accounts (username, label, group_name) VALUES (?, ?, ?)",
            (username, label, group_name),
        )
        await _writer.commit()
    return {"username": username, "label": label, "group_name": group_name}


async def get_account(username: str) -> dict:
    username = username.lower().strip().lstrip("@")
    cursor = await _reader.execute(
        "SELECT * FROM accounts WHERE username = ?", (username,)
    )
    row = await cursor.fetchone()
    return _row_to_dict(row)


async def list_accounts(enabled_only: bool = False, group_name: str = "") -> list[dict]:
    sql = "SELECT * FROM accounts"
    conditions, params = [], []
    if enabled_only:
        conditions.append("enabled = 1")
    if group_name:
        conditions.append("group_name = ?")
        params.append(group_name)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY username"
    cursor = await _reader.execute(sql, params)
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def update_account(username: str, **fields) -> bool:
    username = username.lower().strip().lstrip("@")
    allowed = {"label", "group_name", "enabled"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    sets = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [username]
    async with _write_lock:
        await _writer.execute(f"UPDATE accounts SET {sets} WHERE username = ?", vals)
        await _writer.commit()
        return _writer.total_changes > 0


async def delete_account(username: str) -> bool:
    username = username.lower().strip().lstrip("@")
    async with _write_lock:
        await _writer.execute("DELETE FROM accounts WHERE username = ?", (username,))
        await _writer.commit()
        return _writer.total_changes > 0


async def count_accounts() -> int:
    cursor = await _reader.execute("SELECT COUNT(*) FROM accounts")
    row = await cursor.fetchone()
    return row[0] if row else 0


async def list_groups() -> list[str]:
    cursor = await _reader.execute(
        "SELECT DISTINCT group_name FROM accounts WHERE group_name != '' ORDER BY group_name"
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows]


# ── History ─────────────────────────────────────────────────

async def save_check(username: str, data: dict) -> None:
    """Insert a check result into history."""
    async with _write_lock:
        await _writer.execute(
            """INSERT INTO history
               (username, followers, following, posts, bio, is_private, full_name, profile_pic, status, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                username,
                data.get("followers"),
                data.get("following"),
                data.get("posts"),
                data.get("bio", ""),
                1 if data.get("is_private") else 0,
                data.get("full_name", ""),
                data.get("profile_pic", ""),
                data.get("status", "ok"),
                data.get("error", ""),
            ),
        )
        # Update last_check on account
        await _writer.execute(
            "UPDATE accounts SET last_check = datetime('now') WHERE username = ?",
            (username,),
        )
        await _writer.commit()


async def get_latest(username: str) -> dict:
    cursor = await _reader.execute(
        "SELECT * FROM history WHERE username = ? ORDER BY checked_at DESC LIMIT 1",
        (username,),
    )
    row = await cursor.fetchone()
    return _row_to_dict(row)


async def get_history(username: str, days: int = 30, limit: int = 100) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cursor = await _reader.execute(
        """SELECT * FROM history
           WHERE username = ? AND checked_at >= ?
           ORDER BY checked_at DESC LIMIT ?""",
        (username, since, limit),
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_all_latest() -> list[dict]:
    """Latest check for every account."""
    cursor = await _reader.execute(
        """SELECT h.* FROM history h
           INNER JOIN (
               SELECT username, MAX(checked_at) as max_check
               FROM history GROUP BY username
           ) latest ON h.username = latest.username AND h.checked_at = latest.max_check
           ORDER BY h.username"""
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_previous(username: str) -> dict:
    """Second-to-last check (for delta comparison)."""
    cursor = await _reader.execute(
        "SELECT * FROM history WHERE username = ? ORDER BY checked_at DESC LIMIT 1 OFFSET 1",
        (username,),
    )
    row = await cursor.fetchone()
    return _row_to_dict(row)


async def get_stats_summary() -> dict:
    """Global summary stats."""
    cursor = await _reader.execute("SELECT COUNT(*) FROM accounts WHERE enabled = 1")
    active = (await cursor.fetchone())[0]
    cursor = await _reader.execute("SELECT COUNT(*) FROM accounts")
    total = (await cursor.fetchone())[0]
    cursor = await _reader.execute(
        "SELECT COUNT(DISTINCT username) FROM history WHERE checked_at >= datetime('now', '-1 day')"
    )
    checked_24h = (await cursor.fetchone())[0]
    return {"total_accounts": total, "active_accounts": active, "checked_last_24h": checked_24h}


async def get_growth_data() -> list[dict]:
    """
    Growth deltas for every account: compare latest vs previous check.
    Returns list of {username, followers, following, posts, delta_followers,
    delta_following, delta_posts, pct_followers, checked_at}.
    """
    # Get latest two checks per username
    cursor = await _reader.execute(
        """WITH ranked AS (
               SELECT *, ROW_NUMBER() OVER (PARTITION BY username ORDER BY checked_at DESC) AS rn
               FROM history WHERE status = 'ok'
           )
           SELECT * FROM ranked WHERE rn <= 2 ORDER BY username, rn"""
    )
    rows = await cursor.fetchall()

    # Group by username
    by_user: dict[str, list[dict]] = {}
    for r in rows:
        d = _row_to_dict(r)
        by_user.setdefault(d["username"], []).append(d)

    results = []
    for username, checks in by_user.items():
        latest = checks[0]
        entry = {
            "username": username,
            "followers": latest.get("followers"),
            "following": latest.get("following"),
            "posts": latest.get("posts"),
            "checked_at": latest.get("checked_at", ""),
            "delta_followers": None,
            "delta_following": None,
            "delta_posts": None,
            "pct_followers": None,
        }
        if len(checks) > 1:
            prev = checks[1]
            pf = prev.get("followers")
            cf = latest.get("followers")
            if pf is not None and cf is not None:
                entry["delta_followers"] = cf - pf
                entry["pct_followers"] = round(((cf - pf) / pf) * 100, 2) if pf else 0
            pfg = prev.get("following")
            cfg = latest.get("following")
            if pfg is not None and cfg is not None:
                entry["delta_following"] = cfg - pfg
            pp = prev.get("posts")
            cp = latest.get("posts")
            if pp is not None and cp is not None:
                entry["delta_posts"] = cp - pp
        results.append(entry)

    return results


async def get_7d_growth() -> list[dict]:
    """
    7-day growth for every account: compare latest check vs oldest check
    within last 7 days. Returns list with delta and percentage for
    followers, following, posts.
    """
    cursor = await _reader.execute(
        """WITH latest AS (
               SELECT username, followers, following, posts, checked_at,
                      ROW_NUMBER() OVER (PARTITION BY username ORDER BY checked_at DESC) AS rn
               FROM history WHERE status = 'ok'
                 AND checked_at >= datetime('now', '-7 days')
           ),
           oldest AS (
               SELECT username, followers, following, posts, checked_at,
                      ROW_NUMBER() OVER (PARTITION BY username ORDER BY checked_at ASC) AS rn
               FROM history WHERE status = 'ok'
                 AND checked_at >= datetime('now', '-7 days')
           )
           SELECT
               l.username,
               l.followers, l.following, l.posts,
               l.checked_at as latest_at,
               o.followers as old_followers, o.following as old_following, o.posts as old_posts,
               o.checked_at as oldest_at
           FROM latest l
           JOIN oldest o ON l.username = o.username AND o.rn = 1
           WHERE l.rn = 1
           ORDER BY l.username"""
    )
    rows = await cursor.fetchall()

    results = []
    for r in rows:
        d = _row_to_dict(r)
        cf = d.get("followers")
        of = d.get("old_followers")
        cfg = d.get("following")
        ofg = d.get("old_following")
        cp = d.get("posts")
        op = d.get("old_posts")

        entry = {
            "username": d["username"],
            "followers": cf,
            "following": cfg,
            "posts": cp,
            "latest_at": d.get("latest_at", ""),
            "oldest_at": d.get("oldest_at", ""),
            "delta_followers": (cf - of) if cf is not None and of is not None else None,
            "delta_following": (cfg - ofg) if cfg is not None and ofg is not None else None,
            "delta_posts": (cp - op) if cp is not None and op is not None else None,
            "pct_followers": round(((cf - of) / of) * 100, 2) if cf is not None and of and of > 0 else None,
            "pct_following": round(((cfg - ofg) / ofg) * 100, 2) if cfg is not None and ofg and ofg > 0 else None,
            "pct_posts": round(((cp - op) / op) * 100, 2) if cp is not None and op and op > 0 else None,
        }
        results.append(entry)

    return results


async def get_global_totals() -> dict:
    """Aggregate totals across all accounts from latest checks."""
    cursor = await _reader.execute(
        """SELECT
               SUM(h.followers) as total_followers,
               SUM(h.following) as total_following,
               SUM(h.posts) as total_posts,
               COUNT(*) as account_count
           FROM history h
           INNER JOIN (
               SELECT username, MAX(checked_at) as max_check
               FROM history WHERE status = 'ok' GROUP BY username
           ) latest ON h.username = latest.username AND h.checked_at = latest.max_check"""
    )
    row = await cursor.fetchone()
    if not row:
        return {"total_followers": 0, "total_following": 0, "total_posts": 0, "account_count": 0}
    return _row_to_dict(row)


async def get_global_history(days: int = 30) -> list[dict]:
    """
    Daily aggregate followers/following across all accounts.
    Returns [{date, total_followers, total_following, account_count}].
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cursor = await _reader.execute(
        """WITH daily AS (
               SELECT username,
                      DATE(checked_at) as check_date,
                      followers, following, posts,
                      ROW_NUMBER() OVER (PARTITION BY username, DATE(checked_at) ORDER BY checked_at DESC) as rn
               FROM history
               WHERE status = 'ok' AND checked_at >= ?
           )
           SELECT check_date as date,
                  SUM(followers) as total_followers,
                  SUM(following) as total_following,
                  SUM(posts) as total_posts,
                  COUNT(*) as account_count
           FROM daily WHERE rn = 1
           GROUP BY check_date
           ORDER BY check_date""",
        (since,),
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def reset_history(username: str) -> int:
    """Delete all history for a single account."""
    username = username.lower().strip().lstrip("@")
    async with _write_lock:
        await _writer.execute("DELETE FROM history WHERE username = ?", (username,))
        await _writer.execute(
            "UPDATE accounts SET last_check = NULL WHERE username = ?", (username,),
        )
        await _writer.commit()
        deleted = _writer.total_changes
    logger.info(f"Reset history for @{username}: {deleted} rows deleted")
    return deleted


async def reset_all_history() -> int:
    """Delete all history for every account."""
    async with _write_lock:
        await _writer.execute("DELETE FROM history")
        await _writer.execute("UPDATE accounts SET last_check = NULL")
        await _writer.commit()
        deleted = _writer.total_changes
    logger.info(f"Reset all history: {deleted} rows deleted")
    return deleted


async def cleanup_old_history() -> int:
    """Remove history older than retention days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.history_retention_days)).isoformat()
    async with _write_lock:
        await _writer.execute("DELETE FROM history WHERE checked_at < ?", (cutoff,))
        await _writer.commit()
        deleted = _writer.total_changes
    if deleted:
        logger.info(f"Cleaned up {deleted} old history rows")
    return deleted
