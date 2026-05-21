"""
macro/signals.py

Dataclasses for calendar events + macro signal generation.

  EventImpact      — HIGH | MEDIUM | LOW
  CalendarEvent    — what BaseCalendarSource.fetch_events returns
  PendingEvent     — render-friendly view for the dashboard (computed
                     minutes_until + same fields)
  MacroSignal      — derived from MacroRegime for the future MacroAgent

CalendarEvent lives in the macro module rather than database/models so
plugins don't need a SQLAlchemy dependency. database/queries persists
them via the SQLAlchemy CalendarEvent ORM model.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


class EventImpact(enum.Enum):
    HIGH   = "HIGH"
    MEDIUM = "MEDIUM"
    LOW    = "LOW"


@dataclass
class CalendarEvent:
    """One scheduled economic-calendar item (from FRED, Trading
    Economics, Forex Factory, etc).

    `event_id` must be stable across refreshes so DB upserts work — use
    something like "fred:release_<id>:2026-05-21" instead of a random
    UUID. Post-release fields (actual/forecast/previous) are populated
    later by the source, in place.
    """
    event_id:      str
    title:         str
    country:       str
    scheduled_utc: datetime
    impact:        EventImpact
    source_id:     str
    actual:        Optional[float] = None
    forecast:      Optional[float] = None
    previous:      Optional[float] = None

    def minutes_until(self, now: Optional[datetime] = None) -> int:
        """Whole minutes until scheduled_utc. Negative if already past."""
        now = now or datetime.utcnow()
        # Treat both as naive UTC for arithmetic — the source guarantees
        # scheduled_utc is in UTC by convention.
        if self.scheduled_utc.tzinfo is not None:
            sched = self.scheduled_utc.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            sched = self.scheduled_utc
        delta = (sched - now).total_seconds()
        return int(delta // 60)


@dataclass
class PendingEvent:
    """Dashboard view of an upcoming event. `minutes_until` is computed
    at the point of view, so the dashboard never has to do timezone
    math itself."""
    title:         str
    country:       str
    scheduled_utc: datetime
    minutes_until: int
    impact:        EventImpact

    @classmethod
    def from_calendar_event(
        cls,
        ev: CalendarEvent,
        now: Optional[datetime] = None,
    ) -> "PendingEvent":
        return cls(
            title=ev.title,
            country=ev.country,
            scheduled_utc=ev.scheduled_utc,
            minutes_until=ev.minutes_until(now),
            impact=ev.impact,
        )


@dataclass
class MacroSignal:
    """Discrete event the future MacroAgent can react to.

    These are *generated* by MacroMonitor (e.g. "VIX just crossed 25
    from below"), not consumed yet — the agent is a placeholder. Stored
    in MacroRegime.raw_data so they're queryable from macro_log JSON.
    """
    signal_id:      str
    signal_type:    str     # "VIX_ELEVATED", "YIELD_CURVE_INVERSION", …
    direction:      str     # "RISK_ON" | "RISK_OFF"
    strength:       float   # 0..1
    description:    str
    source_metrics: dict    = field(default_factory=dict)
    timestamp:      float   = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()
        self.strength = max(0.0, min(1.0, float(self.strength)))
