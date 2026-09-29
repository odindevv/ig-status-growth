"""
Pydantic models for Growth Metrics API request/response schemas.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, computed_field


# ── Extended data sub-models (Tier 1 + Tier 2) ──────────────

class ViewsExtended(BaseModel):
    """Tier 1: Views detail breakdown."""
    followers_pct: Optional[float] = None
    non_followers_pct: Optional[float] = None
    content_type: Optional[dict] = None       # {"reels": 85.0, "posts": 10.0, "stories": 5.0}
    profile_activity: Optional[dict] = None   # {"profile_visits": 123, "external_link_taps": 45}
    profile_visits: Optional[int] = None
    external_link_taps: Optional[int] = None


class InteractionsExtended(BaseModel):
    """Tier 2: Interactions detail breakdown."""
    followers_pct: Optional[float] = None
    non_followers_pct: Optional[float] = None
    content_type: Optional[dict] = None       # {"reels_pct": 91.1, "posts_pct": 8.9}
    profile_activity: Optional[dict] = None
    profile_visits: Optional[int] = None
    external_link_taps: Optional[int] = None


class ExtendedMetrics(BaseModel):
    """Container for all extended (Tier 1 + 2) data."""
    views: Optional[ViewsExtended] = None
    interactions: Optional[InteractionsExtended] = None


# ── Main metric snapshot ─────────────────────────────────────

class MetricSnapshot(BaseModel):
    """Single metric extraction from Professional Dashboard."""
    device_id: str
    username: str = ""
    success: bool = False
    error: Optional[str] = None

    # Summary metrics from Insights
    date_range: str = ""                    # e.g. "Mar 20 - Apr 18"
    views: Optional[int] = None
    interactions: Optional[int] = None
    new_followers: Optional[int] = None
    accounts_reached: Optional[int] = None
    content_shared: Optional[int] = None    # Model A only

    # Which UI model was detected
    ui_model: str = ""                      # "A" or "B"

    # Views detail (trending content breakdown) — Model A only
    views_detail: list[dict] = []           # [{title, views, type}, ...]

    # Extended metrics (Tier 1 + Tier 2)
    extended: Optional[dict] = None

    # Navigation metadata
    navigation_steps: list[str] = []
    duration_ms: int = 0
    checked_at: Optional[datetime] = None

    # ── Computed fields ──────────────────────────────────────

    @computed_field
    @property
    def engagement_rate(self) -> Optional[float]:
        """Engagement rate = interactions / accounts_reached * 100."""
        if self.interactions is not None and self.accounts_reached and self.accounts_reached > 0:
            return round((self.interactions / self.accounts_reached) * 100, 2)
        return None

    @computed_field
    @property
    def follower_conversion_rate(self) -> Optional[float]:
        """Follower conversion = new_followers / accounts_reached * 100."""
        if self.new_followers is not None and self.accounts_reached and self.accounts_reached > 0:
            return round((self.new_followers / self.accounts_reached) * 100, 2)
        return None

    @computed_field
    @property
    def views_per_content(self) -> Optional[float]:
        """Average views per content piece shared."""
        if self.views is not None and self.content_shared and self.content_shared > 0:
            return round(self.views / self.content_shared, 1)
        return None


class MetricResponse(BaseModel):
    total: int
    results: list[MetricSnapshot]
    duration_ms: int = 0


class SchedulerStatus(BaseModel):
    running: bool = False
    daily_time: str = ""
    next_run: Optional[str] = None
    last_run: Optional[str] = None
    last_run_type: Optional[str] = None
