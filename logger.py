"""
Unified logger — single Loguru instance for all services.
Each service module gets the same logger; service name appears in the log messages.
"""

import sys
from pathlib import Path
from loguru import logger as _loguru_logger

from config import shared_settings as _settings

# Remove default handler
_loguru_logger.remove()

# Console handler
_loguru_logger.add(
    sys.stderr,
    level=_settings.log_level.upper(),
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>",
)

# Rotating file handler
_log_dir = Path(__file__).parent / "logs"
_log_dir.mkdir(exist_ok=True)

_loguru_logger.add(
    str(_log_dir / "ig_unified_{time:YYYY-MM-DD}.log"),
    rotation="00:00",
    retention="30 days",
    level=_settings.log_level.upper(),
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} — {message}",
)

logger = _loguru_logger

__all__ = ["logger"]
