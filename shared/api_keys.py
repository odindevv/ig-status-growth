"""
API Key store — JSON-file-backed key management.

Keys are loaded into memory at startup and persisted on create/delete.
The admin key (from .env) is NOT stored here — it's checked separately.
"""

import json
import os
import secrets
import hmac
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from config import shared_settings
from logger import logger

_keys: list[dict] = []
_keys_path: Path | None = None


def _resolve_path() -> Path:
    p = Path(shared_settings.api_keys_file)
    if not p.is_absolute():
        p = Path(__file__).parent.parent / p
    return p


def load_keys() -> None:
    """Load keys from JSON file into memory."""
    global _keys, _keys_path
    _keys_path = _resolve_path()
    if _keys_path.exists():
        try:
            _keys = json.loads(_keys_path.read_text(encoding="utf-8"))
            logger.info(f"Loaded {len(_keys)} API key(s) from {_keys_path}")
        except Exception as e:
            logger.error(f"Failed to load API keys: {e}")
            _keys = []
    else:
        _keys = []
        logger.info("No API keys file found — starting with empty key store")


def _save() -> None:
    """Persist keys to disk (atomic write)."""
    if _keys_path is None:
        return
    _keys_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(_keys, indent=2)
    # Atomic write: write to temp file then rename — prevents corruption on crash
    fd, tmp_path = tempfile.mkstemp(
        dir=str(_keys_path.parent), suffix=".tmp", prefix=".apikeys_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(_keys_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def list_keys() -> list[dict]:
    """Return all keys (with key partially masked for display)."""
    result = []
    for k in _keys:
        result.append({
            "id": k["id"],
            "label": k.get("label", ""),
            "key_preview": k["key"][:8] + "..." + k["key"][-4:],
            "created_at": k.get("created_at", ""),
        })
    return result


def create_key(label: str = "") -> dict:
    """Generate a new API key. Returns the full key (only time it's shown)."""
    key = secrets.token_urlsafe(32)
    key_id = secrets.token_hex(4)
    entry = {
        "id": key_id,
        "key": key,
        "label": label or f"key-{key_id}",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _keys.append(entry)
    _save()
    logger.info(f"API key created: id={key_id}, label={entry['label']}")
    return entry


def delete_key(key_id: str) -> bool:
    """Delete a key by ID. Returns True if found and removed."""
    global _keys
    before = len(_keys)
    _keys = [k for k in _keys if k["id"] != key_id]
    if len(_keys) < before:
        _save()
        logger.info(f"API key deleted: id={key_id}")
        return True
    return False


def is_valid_key(key: str) -> bool:
    """Check if key matches any stored API key (constant-time)."""
    for k in _keys:
        if hmac.compare_digest(k["key"], key):
            return True
    return False
