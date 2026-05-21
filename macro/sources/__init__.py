"""
macro/sources/__init__.py

Registry of calendar sources MacroMonitor polls by default.

═══════════════════════════════════════════════════════════════════════
To add a new calendar source
═══════════════════════════════════════════════════════════════════════

1. Create macro/sources/my_calendar.py
2. Subclass BaseCalendarSource (from macro.sources.base)
3. Set the class attrs:
       source_id        = "my_calendar"
       display_name     = "My Calendar"
       refresh_interval = 3600                     # seconds
       optional         = True
       requires_api_key = True                     # or False
       api_key_env_var  = "MY_CALENDAR_API_KEY"
4. Implement:
       async def fetch_events(self) -> list[CalendarEvent]
       def is_available(self) -> bool                # only override if non-default
5. Append your class to REGISTERED_CALENDAR_SOURCES below

Nothing else needs to change. MacroMonitor instantiates each entry,
skips any whose is_available() returns False, persists events via
database.queries.save_calendar_events, and folds them into
get_pending_events() output.

Sources never have to manage retries, dedup, or persistence — the
monitor handles those.
"""

from macro.sources.fred_calendar import FREDCalendarSource
from macro.sources.stub_calendar import StubCalendarSource


# Ordered registry — the monitor instantiates each class in order. Stub
# stays in the list so its docstring is visible from a single grep.
REGISTERED_CALENDAR_SOURCES: list[type] = [
    FREDCalendarSource,
    StubCalendarSource,
]
