"""
SQLite persistence for combined check history.
Uses aiosqlite with WAL mode.
  - Writer: single connection protected by asyncio.Lock
  - Reader: separate connection for concurrent reads
  - Retention: auto-prune old checks beyond configured max days
"""

import asyncio
import json
import aiosqlite
from datetime import datetime, timezone, timedelta
from pathlib import Path
from config import status_settings as settings
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
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            success INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            account_state TEXT DEFAULT 'unknown',
            account_state_signal TEXT,
            summary_json TEXT,
            details_json TEXT,
            proxy_json TEXT,
            navigation_steps TEXT,
            duration_ms INTEGER DEFAULT 0,
            checked_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_checks_device ON checks(device_id);
        CREATE INDEX IF NOT EXISTS idx_checks_checked_at ON checks(checked_at DESC);
        CREATE INDEX IF NOT EXISTS idx_checks_username ON checks(username);

        CREATE TABLE IF NOT EXISTS latest_status (
            device_id TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            success INTEGER NOT NULL DEFAULT 0,
            account_state TEXT DEFAULT 'unknown',
            account_state_signal TEXT,
            sections_json TEXT DEFAULT '{}',
            proxy_json TEXT,
            error TEXT,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (device_id, username)
        );
    """)
    await _writer.commit()

    # Migration: upgrade latest_status PK from device_id-only to composite (device_id, username)
    try:
        async with _writer.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='latest_status'") as cursor:
            row = await cursor.fetchone()
        if row and 'PRIMARY KEY (device_id, username)' not in (row[0] or ''):
            await _writer.execute("DROP TABLE IF EXISTS latest_status")
            await _writer.execute("""CREATE TABLE latest_status (
                device_id TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                success INTEGER NOT NULL DEFAULT 0,
                account_state TEXT DEFAULT 'unknown',
                account_state_signal TEXT,
                sections_json TEXT DEFAULT '{}',
                proxy_json TEXT,
                error TEXT,
                checked_at TEXT NOT NULL,
                PRIMARY KEY (device_id, username)
            )""")
            await _writer.commit()
            logger.info("[db] Migrated latest_status to composite PK (device_id, username)")
    except Exception as e:
        logger.warning(f"[db] Migration check failed: {e}")

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

async def save_check(result: dict) -> None:
    """Save a check result to history and update latest status."""
    if not _writer:
        logger.error("[db] save_check called but database not initialized — data NOT persisted")
        return
    async with _write_lock:
        try:
            await _save_check_inner(result)
        except Exception as e:
            logger.error(f"[db] Failed to save check for {result.get('device_id', '?')}: {e}")
            raise  # re-raise so caller knows persistence failed


async def _save_check_inner(result: dict) -> None:
    """Inner save logic, called under _write_lock."""
    now = datetime.now(timezone.utc).isoformat()
    summary = result.get("summary", {})
    details = result.get("details", {})
    nav_steps = result.get("navigation_steps", [])
    proxy_data = result.get("proxy")

    await _writer.execute(
        """INSERT INTO checks
           (device_id, username, success, error, account_state, account_state_signal,
            summary_json, details_json, proxy_json, navigation_steps, duration_ms, checked_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            result["device_id"],
            result.get("username", ""),
            1 if result.get("success") else 0,
            result.get("error"),
            result.get("account_state", "unknown"),
            result.get("account_state_signal"),
            json.dumps(summary),
            json.dumps(details) if details else None,
            json.dumps(proxy_data) if proxy_data else None,
            json.dumps(nav_steps),
            result.get("duration_ms", 0),
            now,
        ),
    )

    # Update latest status
    sections_summary = {}
    for section_key, section_data in summary.items():
        if isinstance(section_data, dict):
            sections_summary[section_key] = {
                "status": section_data.get("status", "unknown"),
                "label": section_data.get("label", section_key),
            }

    await _writer.execute(
        """INSERT OR REPLACE INTO latest_status
           (device_id, username, success, account_state, account_state_signal,
            sections_json, proxy_json, error, checked_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            result["device_id"],
            result.get("username", ""),
            1 if result.get("success") else 0,
            result.get("account_state", "unknown"),
            result.get("account_state_signal"),
            json.dumps(sections_summary),
            json.dumps(proxy_data) if proxy_data else None,
            result.get("error"),
            now,
        ),
    )
    await _writer.commit()


async def prune_old_checks(max_days: int = 0) -> int:
    """Delete checks older than max_days. Returns count deleted."""
    if not _writer:
        return 0
    days = max_days or settings.db_retention_days
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with _write_lock:
        cursor = await _writer.execute(
            "DELETE FROM checks WHERE checked_at < ?", (cutoff,)
        )
        count = cursor.rowcount
        if count > 0:
            await _writer.commit()
            logger.info(f"[db] Pruned {count} checks older than {days} days")
    return count


# ── Read operations (use _reader, no lock needed) ──────────

async def get_latest_statuses() -> list[dict]:
    """Return latest status for all devices."""
    if not _reader:
        return []
    cursor = await _reader.execute(
        "SELECT * FROM latest_status ORDER BY checked_at DESC"
    )
    rows = await cursor.fetchall()
    results = []
    for r in rows:
        row_dict = dict(r)
        if row_dict.get("sections_json"):
            try:
                row_dict["sections"] = json.loads(row_dict["sections_json"])
            except (json.JSONDecodeError, TypeError):
                row_dict["sections"] = {}
        else:
            row_dict["sections"] = {}
        if row_dict.get("proxy_json"):
            try:
                row_dict["proxy"] = json.loads(row_dict["proxy_json"])
            except (json.JSONDecodeError, TypeError):
                row_dict["proxy"] = None
        else:
            row_dict["proxy"] = None
        results.append(row_dict)
    return results


async def get_history(
    device_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Query check history with optional device filter."""
    if not _reader:
        return []

    query = "SELECT * FROM checks"
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
        if row_dict.get("summary_json"):
            row_dict["summary"] = json.loads(row_dict["summary_json"])
        if row_dict.get("details_json"):
            row_dict["details"] = json.loads(row_dict["details_json"])
        if row_dict.get("proxy_json"):
            try:
                row_dict["proxy"] = json.loads(row_dict["proxy_json"])
            except (json.JSONDecodeError, TypeError):
                row_dict["proxy"] = None
        if row_dict.get("navigation_steps"):
            row_dict["navigation_steps"] = json.loads(row_dict["navigation_steps"])
        results.append(row_dict)

    return results
