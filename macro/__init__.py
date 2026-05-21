"""
macro/ — regime monitor + economic calendar.

Public surface:
    MacroMonitor      — orchestrator class
    macro_monitor     — module-level singleton (CLAUDE.md pattern)
    MacroRegime       — one regime snapshot
    MacroScenario     — scenario enum (CRISIS, GOLDILOCKS, …)
    CalendarEvent     — fetched by BaseCalendarSource subclasses
    PendingEvent      — render-friendly view for the dashboard
    EventImpact       — HIGH | MEDIUM | LOW
    MacroSignal       — discrete event for the future MacroAgent
    BaseCalendarSource — subclass to add a new calendar source

See macro/sources/__init__.py for the docstring on adding a new
calendar source — same 5-step plugin pattern as sentiment + data_sources.
"""

from macro.monitor import MacroMonitor
from macro.regime import (
    DollarStrength, RiskAppetite, RateEnvironment, VolRegime,
    MacroScenario, MacroRegime,
)
from macro.signals import (
    CalendarEvent, EventImpact, PendingEvent, MacroSignal,
)
from macro.sources.base import BaseCalendarSource


# Module-level singleton — CLAUDE.md singleton pattern.
macro_monitor = MacroMonitor()


__all__ = [
    "MacroMonitor",
    "macro_monitor",
    "MacroRegime",
    "MacroScenario",
    "DollarStrength",
    "RiskAppetite",
    "RateEnvironment",
    "VolRegime",
    "CalendarEvent",
    "PendingEvent",
    "EventImpact",
    "MacroSignal",
    "BaseCalendarSource",
]
