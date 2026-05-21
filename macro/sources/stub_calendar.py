"""
macro/sources/stub_calendar.py

Placeholder source — always reports unavailable, fetches nothing. Lives
in the registry to remind future implementers what slots are available
(Trading Economics, Investing.com economic calendar, Forex Factory,
econoday, MarketAux, etc).

═══════════════════════════════════════════════════════════════════════
To implement a real source against this slot
═══════════════════════════════════════════════════════════════════════

1. Subclass BaseCalendarSource in your own file
2. Set the class attrs:
       source_id        = "trading_economics"        # or whichever
       display_name     = "Trading Economics"
       refresh_interval = 3600                       # seconds
       requires_api_key = True
       api_key_env_var  = "TRADING_ECONOMICS_API_KEY"
3. Implement async def fetch_events() returning list[CalendarEvent].
   - Build a stable event_id ("te:<source_id>:<scheduled_iso>") so
     DB upserts replace rather than accumulate
   - Set country to ISO-2 ("US", "EU", "UK", "JP", …)
   - Set impact via the provider's own rating or a keyword map
4. is_available() defaults to checking the env var — override only
   for richer dependency checks (module imports, network reachability)
5. Append your class to REGISTERED_CALENDAR_SOURCES in
   macro/sources/__init__.py

The monitor instantiates, polls, and persists each registered source
without any further wiring.
"""

from __future__ import annotations

from macro.signals import CalendarEvent
from macro.sources.base import BaseCalendarSource


class StubCalendarSource(BaseCalendarSource):
    source_id        = "stub_calendar"
    display_name     = "Stub Calendar (placeholder)"
    refresh_interval = 86400
    optional         = True

    def is_available(self) -> bool:
        # Never runs — exists only as a documented slot.
        return False

    async def fetch_events(self) -> list[CalendarEvent]:
        return []
