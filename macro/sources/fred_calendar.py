"""
macro/sources/fred_calendar.py

Economic calendar from FRED's /fred/releases/dates endpoint. Maps
release names to human-readable event titles + impact ratings.

Caveats:
  - FRED publishes dates only, no time-of-day. We default to 13:30 UTC
    (08:30 ET, the standard US release window) and override to 18:00
    UTC for FOMC Press Releases (14:00 ET).
  - "Include scheduled" via include_release_dates_with_no_data=true.
    FRED's scheduled-date precision is loose; treat the calendar as
    "release days ahead" rather than minute-precise.
  - All FRED releases are US-coverage; country is hard-coded "US".

For tighter calendars, swap in a Trading Economics / Investing.com /
Forex Factory source via the same plugin interface.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

from config import settings
from macro.signals import CalendarEvent, EventImpact
from macro.sources.base import BaseCalendarSource

logger = logging.getLogger(__name__)

RELEASES_ENDPOINT = "https://api.stlouisfed.org/fred/releases/dates"

# Lower-case substring match against FRED's release_name. First match
# wins; check HIGH before MEDIUM so a Fed-adjacent release isn't
# downgraded by a coincidental keyword.
_HIGH_KEYWORDS = (
    "fomc",
    "consumer price index",
    "employment situation",
    "personal consumption expenditures",
    "gross domestic product",
)
_MEDIUM_KEYWORDS = (
    "producer price index",
    "retail",
    "initial claims",
    "unemployment insurance",
    "housing starts",
    "existing home sales",
    "new residential sales",
    "industrial production",
    "ism manufacturing",
    "consumer sentiment",
)

# Default US release time when FRED doesn't tell us. 13:30 UTC = 08:30
# ET, the canonical "morning release" window. FOMC overrides to 18:00.
_DEFAULT_HOUR_UTC = 13
_DEFAULT_MINUTE_UTC = 30
_FOMC_HOUR_UTC      = 18


class FREDCalendarSource(BaseCalendarSource):
    source_id        = "fred_calendar"
    display_name     = "FRED Release Calendar"
    refresh_interval = 3600                  # hourly is plenty
    optional         = True
    requires_api_key = True
    api_key_env_var  = "FRED_API_KEY"

    def __init__(self, lookahead_days: int = 14):
        self._lookahead_days = lookahead_days

    async def fetch_events(self) -> list[CalendarEvent]:
        api_key = os.getenv(self.api_key_env_var, "")
        if not api_key:
            logger.debug("FRED_API_KEY missing — skipping FRED calendar")
            return []

        today = datetime.utcnow().date()
        end = today + timedelta(days=self._lookahead_days)
        params = {
            "api_key":   api_key,
            "file_type": "json",
            "sort_order":     "asc",
            "include_release_dates_with_no_data": "true",
            "realtime_start": today.isoformat(),
            "realtime_end":   end.isoformat(),
            "limit": 100,
        }

        try:
            timeout = aiohttp.ClientTimeout(
                total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC,
            )
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(RELEASES_ENDPOINT, params=params) as r:
                    payload = await r.json(content_type=None)
        except Exception as e:
            logger.warning(f"fred calendar fetch failed: {e}")
            return []

        events: list[CalendarEvent] = []
        for entry in payload.get("release_dates") or []:
            try:
                ev = self._parse_entry(entry)
            except Exception as e:
                logger.debug(f"fred calendar parse skipped: {e} — {entry}")
                continue
            if ev:
                events.append(ev)
        return events

    # ── Internals ────────────────────────────────────────────────────────

    def _parse_entry(self, entry: dict) -> Optional[CalendarEvent]:
        date_str = entry.get("date")
        release_id = entry.get("release_id")
        name = entry.get("release_name") or ""
        if not date_str or release_id is None:
            return None

        impact = self._impact_for(name)
        # We drop LOW-impact entries to avoid drowning the calendar in
        # bond auctions, daily Treasury data, etc. Pre-event pause and
        # the dashboard's pending-events panel both care primarily
        # about HIGH/MEDIUM.
        if impact is EventImpact.LOW:
            return None

        hour, minute = self._time_for(name)
        try:
            sched = datetime.strptime(date_str, "%Y-%m-%d").replace(
                hour=hour, minute=minute,
            )
        except ValueError:
            return None

        return CalendarEvent(
            event_id=f"fred:{release_id}:{date_str}",
            title=name,
            country="US",
            scheduled_utc=sched,
            impact=impact,
            source_id=self.source_id,
        )

    def _impact_for(self, name: str) -> EventImpact:
        low = name.lower()
        for kw in _HIGH_KEYWORDS:
            if kw in low:
                return EventImpact.HIGH
        for kw in _MEDIUM_KEYWORDS:
            if kw in low:
                return EventImpact.MEDIUM
        return EventImpact.LOW

    def _time_for(self, name: str) -> tuple[int, int]:
        if "fomc" in name.lower():
            return _FOMC_HOUR_UTC, 0
        return _DEFAULT_HOUR_UTC, _DEFAULT_MINUTE_UTC
