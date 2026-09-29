"""
Unified configuration — single .env file, per-service settings via env prefixes.

Shared settings (ADB, security, telegram, logging) have no prefix.
Service-specific settings use prefixes: GROWTH_, STATUS_, CHECKER_, TG_.

Usage in service modules:
    from config import growth_settings as settings   # growth files
    from config import status_settings as settings   # status files
    from config import checker_settings as settings  # checker files
    from config import shared_settings as settings   # shared files (adb, scrcpy)
"""

from pathlib import Path
from pydantic_settings import BaseSettings

_ENV = Path(__file__).parent / ".env"


# ── Shared (no prefix) ───────────────────────────────────────

class _Shared(BaseSettings):
    # ADB
    adb_path: str = "adb"
    adb_host: str = ""
    adb_port: int = 0
    adb_remote_hosts: str = ""  # comma-separated remote ADB server IPs (optional)

    # Worker→Hub: if set, this instance reports results to the hub
    hub_url: str = ""           # e.g. http://192.168.1.10:8001
    worker_id: str = ""         # name for this worker (e.g. "rdp2")
    worker_url: str = ""        # public URL where hub can reach this worker (e.g. http://PUBLIC_IP:8001)
    use_scrcpy: bool = True
    scrcpy_path: str = ""
    adb_retry_count: int = 2
    adb_retry_delay: float = 1.0
    wake_screen_before_check: bool = True
    preflight_proxy_check: bool = False  # If True, abort check when phone isn't on proxy screen (fast-fail). If False, let start_proxy() handle it.

    # Navigation timing
    nav_step_delay: float = 1.5
    scroll_delay: float = 1.0
    page_load_delay: float = 2.0
    max_scrolls_to_find: int = 12

    # Proxy (Super Proxy app)
    proxy_package: str = "com.scheler.superproxy"
    proxy_ad_dismiss_attempts: int = 3

    # Security
    api_key: str = ""
    admin_key: str = ""
    api_keys_file: str = "data/api_keys.json"
    cors_origins: str = "*"
    rate_limit_force: int = 20
    rate_limit_whitelist: str = ""

    # Debug / dumps
    debug_dumps: bool = True
    debug_dumps_dir: str = "dumps"
    debug_dumps_max_files: int = 500
    screen_cache_ttl: int = 3600

    # Telegram (shared token + chat)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Logging
    log_level: str = "DEBUG"

    model_config = {"env_file": str(_ENV), "extra": "ignore"}


# ── Growth (GROWTH_ prefix) ──────────────────────────────────

class _Growth(BaseSettings):
    db_path: str = "data/ig_growth.db"
    check_timeout: float = 360
    max_parallel_devices: int = 5
    db_retention_days: int = 365
    scheduler_enabled: bool = False
    scheduler_daily_hour: int = 8
    scheduler_daily_minute: int = 0
    scheduler_timezone: str = ""
    telegram_notify_min_score: int = 70
    telegram_bot_enabled: bool = False
    viral_retention_days: int = 90
    viral_dedup_hours: int = 6
    content_lookup_url: str = ""

    model_config = {"env_prefix": "GROWTH_", "env_file": str(_ENV), "extra": "ignore"}


# ── Status (STATUS_ prefix) ──────────────────────────────────

class _Status(BaseSettings):
    db_path: str = "data/ig_status.db"
    check_timeout: float = 180
    max_parallel_devices: int = 5
    db_retention_days: int = 90
    scheduler_enabled: bool = False
    scheduler_daily_hour: int = 7
    scheduler_daily_minute: int = 0
    scheduler_timezone: str = ""
    telegram_bot_enabled: bool = False
    telegram_alert_on_problem: bool = True
    telegram_alert_on_state_change: bool = True

    model_config = {"env_prefix": "STATUS_", "env_file": str(_ENV), "extra": "ignore"}


# ── Checker (CHECKER_ prefix) ────────────────────────────────

class _Checker(BaseSettings):
    db_path: str = "data/ig_accounts.db"
    device_id: str = ""                   # ADB device serial (required)
    proxy_enabled: bool = False           # Use Super Proxy app before IG
    proxy_rotate_url: str = ""            # HTTP URL to rotate proxy IP
    delay_min: float = 3.0               # Min delay between accounts
    delay_max: float = 8.0               # Max delay between accounts
    max_retries: int = 3                  # Retries per account on failure
    check_timeout: float = 120            # Max seconds per account check
    scheduler_enabled: bool = False
    scheduler_hour: int = 8
    scheduler_minute: int = 0
    scheduler_interval_hours: int = 0
    telegram_bot_enabled: bool = False
    telegram_alert_drop_pct: float = 5.0
    telegram_alert_gain_pct: float = 20.0
    history_retention_days: int = 365
    cors_origins: str = "*"
    log_level: str = "DEBUG"

    model_config = {"env_prefix": "CHECKER_", "env_file": str(_ENV), "extra": "ignore"}


# ── Telegram Bot (TG_ prefix) ────────────────────────────────

class _Telegram(BaseSettings):
    bot_token: str = ""
    chat_id: str = ""
    api_base_url: str = "http://localhost:8000"
    api_key: str = ""
    alert_poll_interval: int = 60
    log_level: str = "INFO"

    model_config = {"env_prefix": "TG_", "env_file": str(_ENV), "extra": "ignore"}


# ── Merged proxy: service attrs first, then shared ────────────

class _Merged:
    """Transparent proxy — looks up service-specific settings first, shared second."""

    def __init__(self, svc, shared):
        object.__setattr__(self, "_svc", svc)
        object.__setattr__(self, "_shared", shared)

    def __getattr__(self, name):
        svc = object.__getattribute__(self, "_svc")
        shared = object.__getattribute__(self, "_shared")
        try:
            return getattr(svc, name)
        except AttributeError:
            return getattr(shared, name)


# ── Singletons ────────────────────────────────────────────────

_shared = _Shared()
_growth = _Growth()
_status = _Status()
_checker = _Checker()
_telegram = _Telegram()

shared_settings = _shared
growth_settings = _Merged(_growth, _shared)
status_settings = _Merged(_status, _shared)
checker_settings = _Merged(_checker, _shared)
telegram_settings = _telegram
