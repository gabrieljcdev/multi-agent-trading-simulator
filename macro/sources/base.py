"""
macro/sources/base.py

BaseCalendarSource ABC — same plugin shape as BaseSentimentSource and
BaseDataSource. Concrete sources need only set the class attrs and
implement fetch_events().

Cache + staleness management is intentionally not in this base class:
calendar sources publish discrete events (not continuous metrics), so
they're queried on a slow timer from MacroMonitor and upserted into
the DB rather than into a per-source cache. Keeps the surface small.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class BaseCalendarSource(ABC):
    """Subclass + set the class attrs, then implement fetch_events()."""

    # Override in subclasses
    source_id:        str  = "base_calendar"
    display_name:     str  = "Base Calendar"
    refresh_interval: int  = 3600          # seconds between fetch_events
    optional:         bool = True
    requires_api_key: bool = False
    api_key_env_var:  str  = ""

    @abstractmethod
    async def fetch_events(self) -> list:
        """Return a list of macro.signals.CalendarEvent. Must never
        raise — return [] on any fetch failure and log the cause."""
        ...

    def is_available(self) -> bool:
        """True if the source can run. Default checks the API key env
        var when requires_api_key is set; otherwise always True.
        Subclasses can override for richer checks."""
        if not self.requires_api_key:
            return True
        return bool(os.getenv(self.api_key_env_var, ""))
