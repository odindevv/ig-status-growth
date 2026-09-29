"""
Pydantic models for API request/response schemas.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class SectionStatus(BaseModel):
    status: str = "unknown"          # ok | problem | unknown
    label: str = ""
    raw_text: str = ""


class CheckResult(BaseModel):
    device_id: str
    username: str = ""
    success: bool = False
    error: Optional[str] = None
    account_state: str = "unknown"
    account_state_signal: Optional[str] = None
    web_check: Optional[dict] = None
    summary: dict = {}              # Dynamic sections — keys vary by account type
    details: dict = {}
    navigation_steps: list[str] = []
    duration_ms: int = 0
    checked_at: Optional[datetime] = None


class CheckResponse(BaseModel):
    total: int
    results: list[CheckResult]
    duration_ms: int = 0


class SchedulerStatus(BaseModel):
    running: bool = False
    daily_time: str = ""
    next_run: Optional[str] = None
    last_run: Optional[str] = None
    last_run_type: Optional[str] = None
