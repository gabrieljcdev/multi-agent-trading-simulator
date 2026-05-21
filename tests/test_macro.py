"""
tests/test_macro.py — coverage for the macro module.

  Regime computation — scenario priority, dimensional classifiers,
    score curves, hard block, signal modifier, confidence
  Calendar plugin — impact mapping, plugin compliance, pre-event pause
  Dashboard wiring — panels render with / without regime + events
  DB — save/get round-trips for macro_log + calendar_events

Everything that touches the DB uses a fresh tmp SQLite via the temp_db
fixture so tests don't share state across files.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from macro.regime import (
    DollarStrength, RateEnvironment, RiskAppetite, VolRegime,
    MacroScenario, MacroRegime,
)
from macro.signals import (
    CalendarEvent, EventImpact, PendingEvent, MacroSignal,
)
from macro.monitor import (
    MacroMonitor,
    _vix_score, _dollar_score, _yield_curve_score,
    _rate_env_score, _inflation_score, _score_to_modifier,
)
from macro.sources.base import BaseCalendarSource
from macro.sources.stub_calendar import StubCalendarSource
from macro.sources.fred_calendar import FREDCalendarSource


# ─────────────────────────────────────────────────────────────────────────
# Score curves — pin the bands
# ─────────────────────────────────────────────────────────────────────────

def test_vix_score_curve():
    assert _vix_score(None) == 0.0
    assert _vix_score(10.0) == 100.0    # < CALM (15)
    assert _vix_score(18.0) == 30.0     # CALM..ELEVATED
    assert _vix_score(28.0) == -60.0    # ELEVATED..CRISIS
    assert _vix_score(40.0) == -100.0   # >= CRISIS


def test_dollar_score_curve():
    assert _dollar_score(None) == 0.0
    assert _dollar_score(95.0) == 100.0    # WEAK
    assert _dollar_score(101.0) == 0.0     # NEUTRAL
    assert _dollar_score(110.0) == -100.0  # STRONG


def test_yield_curve_score():
    assert _yield_curve_score(None) == 0.0
    assert _yield_curve_score(-0.3) == -100.0  # inverted
    assert _yield_curve_score(0.2) == 0.0
    assert _yield_curve_score(1.5) == 60.0


def test_inflation_score():
    assert _inflation_score(None) == 0.0
    assert _inflation_score(2.0) == 100.0   # < LOW (2.5)
    assert _inflation_score(3.0) == 0.0     # mid band
    assert _inflation_score(5.0) == -100.0  # >= HIGH (4.0)


def test_score_to_modifier_step_ladder():
    """Step ladder mirrors sentiment._composite_to_modifier."""
    assert _score_to_modifier(70) == 15
    assert _score_to_modifier(50) == 10
    assert _score_to_modifier(25) == 5
    assert _score_to_modifier(0) == 0
    assert _score_to_modifier(-30) == -5
    assert _score_to_modifier(-50) == -10
    assert _score_to_modifier(-70) == -15


# ─────────────────────────────────────────────────────────────────────────
# Dimensional classifiers + scenario priority
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_crisis_fires_on_high_vix():
    """VIX past MACRO_VIX_CRISIS short-circuits the scenario ladder."""
    m = MacroMonitor(calendar_sources=[])
    with patch("data_sources.data_sources") as mock_ds, \
         patch("database.queries.save_macro_regime"), \
         patch("database.queries.get_data_at_time", return_value=None), \
         patch("database.queries.get_latest_data_point", return_value=None):
        mock_ds.frankfurter.get_dxy.return_value = 100.0
        mock_ds.fred.get_vix.return_value = 40.0           # CRISIS
        mock_ds.fred.get_10y_yield.return_value = 4.5
        mock_ds.fred.get_2y_yield.return_value = 4.0
        mock_ds.fred.get_fed_funds.return_value = 3.0
        mock_ds.fred.get_cpi_yoy.return_value = 3.0
        regime = await m.refresh()

    assert regime.scenario is MacroScenario.CRISIS
    assert regime.vol is VolRegime.CRISIS
    assert m.is_hard_blocked() is True


@pytest.mark.asyncio
async def test_goldilocks_fires_on_supportive_inputs():
    """Low vol + weak dollar + neutral rates + low inflation."""
    m = MacroMonitor(calendar_sources=[])
    with patch("data_sources.data_sources") as mock_ds, \
         patch("database.queries.save_macro_regime"), \
         patch("database.queries.get_data_at_time", return_value=None), \
         patch("database.queries.get_latest_data_point", return_value=None):
        mock_ds.frankfurter.get_dxy.return_value = 95.0   # weak
        mock_ds.fred.get_vix.return_value = 12.0          # calm
        mock_ds.fred.get_10y_yield.return_value = 4.0
        mock_ds.fred.get_2y_yield.return_value = 3.8
        mock_ds.fred.get_fed_funds.return_value = 3.0
        mock_ds.fred.get_cpi_yoy.return_value = 2.0       # low
        regime = await m.refresh()

    assert regime.scenario is MacroScenario.GOLDILOCKS
    assert regime.vol is VolRegime.CALM
    assert regime.dollar is DollarStrength.WEAK
    assert regime.risk is RiskAppetite.RISK_ON
    assert m.is_hard_blocked() is False
    # Heavy positive composite → +15 modifier
    assert m.get_signal_modifier() == 15


@pytest.mark.asyncio
async def test_risk_off_fires_on_inverted_curve_plus_elevated_vix():
    m = MacroMonitor(calendar_sources=[])
    with patch("data_sources.data_sources") as mock_ds, \
         patch("database.queries.save_macro_regime"), \
         patch("database.queries.get_data_at_time", return_value=None), \
         patch("database.queries.get_latest_data_point", return_value=None):
        mock_ds.frankfurter.get_dxy.return_value = 100.0
        mock_ds.fred.get_vix.return_value = 28.0          # elevated
        mock_ds.fred.get_10y_yield.return_value = 3.5
        mock_ds.fred.get_2y_yield.return_value = 4.5      # inverted
        mock_ds.fred.get_fed_funds.return_value = 4.0
        mock_ds.fred.get_cpi_yoy.return_value = 3.0
        regime = await m.refresh()

    assert regime.scenario is MacroScenario.RISK_OFF
    assert regime.vol is VolRegime.ELEVATED
    assert regime.yield_curve == pytest.approx(-1.0)


@pytest.mark.asyncio
async def test_confidence_degrades_with_missing_data():
    """Half the inputs None → confidence ≈ 0.5."""
    m = MacroMonitor(calendar_sources=[])
    with patch("data_sources.data_sources") as mock_ds, \
         patch("database.queries.save_macro_regime"), \
         patch("database.queries.get_data_at_time", return_value=None), \
         patch("database.queries.get_latest_data_point", return_value=None):
        # 3 of 6 inputs are None.
        mock_ds.frankfurter.get_dxy.return_value = 100.0
        mock_ds.fred.get_vix.return_value = 18.0
        mock_ds.fred.get_10y_yield.return_value = 4.5
        mock_ds.fred.get_2y_yield.return_value = None
        mock_ds.fred.get_fed_funds.return_value = None
        mock_ds.fred.get_cpi_yoy.return_value = None
        regime = await m.refresh()

    assert 0.4 < regime.confidence < 0.6


@pytest.mark.asyncio
async def test_missing_data_source_does_not_crash():
    """Whole data_sources singleton blowing up → regime still emitted
    with neutral defaults + zero confidence."""
    m = MacroMonitor(calendar_sources=[])
    # Force the import inside refresh() to raise so every safe() returns None.
    import sys as _sys
    _sys.modules.pop("data_sources", None)
    with patch("builtins.__import__", side_effect=lambda name, *a, **kw:
               (_ for _ in ()).throw(ImportError("offline"))
               if name == "data_sources"
               else __import__(name, *a, **kw)), \
         patch("database.queries.save_macro_regime"), \
         patch("database.queries.get_data_at_time", return_value=None), \
         patch("database.queries.get_latest_data_point", return_value=None):
        regime = await m.refresh()
    # Must not crash; everything degrades to neutral.
    assert regime is not None
    assert regime.confidence == 0.0


# ─────────────────────────────────────────────────────────────────────────
# Calendar plugin
# ─────────────────────────────────────────────────────────────────────────

def test_stub_calendar_is_unavailable():
    s = StubCalendarSource()
    assert s.is_available() is False


@pytest.mark.asyncio
async def test_stub_calendar_returns_empty():
    s = StubCalendarSource()
    assert await s.fetch_events() == []


def test_fred_calendar_requires_api_key(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    assert FREDCalendarSource().is_available() is False
    monkeypatch.setenv("FRED_API_KEY", "x")
    assert FREDCalendarSource().is_available() is True


def test_fred_calendar_impact_keyword_mapping():
    """Lowercase substring match — FOMC + CPI + NFP map HIGH; PPI +
    retail + claims + housing map MEDIUM; everything else LOW (and
    LOW events are dropped to keep the calendar focused)."""
    s = FREDCalendarSource()
    assert s._impact_for("FOMC Press Release") is EventImpact.HIGH
    assert s._impact_for("Consumer Price Index") is EventImpact.HIGH
    assert s._impact_for("Employment Situation") is EventImpact.HIGH
    assert s._impact_for("Producer Price Index") is EventImpact.MEDIUM
    assert s._impact_for("Retail Trade") is EventImpact.MEDIUM
    assert s._impact_for("Housing Starts") is EventImpact.MEDIUM
    assert s._impact_for("H.15 Selected Interest Rates") is EventImpact.LOW


def test_fred_calendar_parses_fomc_time_correctly():
    """FOMC releases at 14:00 ET = 18:00 UTC, not the 08:30 ET default."""
    s = FREDCalendarSource()
    ev = s._parse_entry({
        "date": "2026-06-10",
        "release_id": 101,
        "release_name": "FOMC Press Release",
    })
    assert ev is not None
    assert ev.scheduled_utc.hour == 18
    assert ev.scheduled_utc.minute == 0
    assert ev.impact is EventImpact.HIGH


def test_fred_calendar_drops_low_impact_releases():
    """Daily Treasury auctions etc would drown the panel."""
    s = FREDCalendarSource()
    ev = s._parse_entry({
        "date": "2026-06-10",
        "release_id": 86,
        "release_name": "Commercial Paper",
    })
    assert ev is None


def test_calendar_event_minutes_until():
    """minutes_until is +ve when in the future, -ve when past."""
    future = datetime.utcnow() + timedelta(minutes=45)
    ev = CalendarEvent(
        event_id="x", title="t", country="US",
        scheduled_utc=future, impact=EventImpact.HIGH,
        source_id="x",
    )
    assert 40 <= ev.minutes_until() <= 46


def test_new_calendar_source_picked_up_via_registry():
    """Drop-in plugin pattern — monitor instantiates from the list."""
    class _MySource(BaseCalendarSource):
        source_id = "my_source"
        async def fetch_events(self):
            return []

    m = MacroMonitor(calendar_sources=[_MySource()])
    assert any(s.source_id == "my_source" for s in m._calendar_sources)


# ─────────────────────────────────────────────────────────────────────────
# Pre-event pause window
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_pending_events_filters_past_and_orders_by_time():
    """Past events are excluded; future events come sorted soonest first."""
    now = datetime.utcnow()
    past = CalendarEvent("past", "Old", "US", now - timedelta(hours=1),
                         EventImpact.HIGH, "x")
    soon = CalendarEvent("soon", "Soon", "US", now + timedelta(minutes=20),
                         EventImpact.HIGH, "x")
    later = CalendarEvent("later", "Later", "US", now + timedelta(hours=4),
                          EventImpact.MEDIUM, "x")

    m = MacroMonitor(calendar_sources=[])
    m._events = [later, past, soon]
    pendings = m.get_pending_events(n=5)
    assert [p.title for p in pendings] == ["Soon", "Later"]
    assert pendings[0].minutes_until <= 25


# ─────────────────────────────────────────────────────────────────────────
# DB integration
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    """Fresh SQLite for each test that touches the DB."""
    db_file = tmp_path / "test_macro.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_file)

    import database.db
    importlib.reload(database.db)
    import database.queries as q
    importlib.reload(q)
    database.db.init_db()
    yield q


def _sample_regime() -> MacroRegime:
    return MacroRegime(
        scenario=MacroScenario.REFLATION,
        dollar=DollarStrength.NEUTRAL, risk=RiskAppetite.RISK_ON,
        rates=RateEnvironment.NEUTRAL, vol=VolRegime.CALM,
        macro_score=13.5,
        dxy=99.3, vix=18.1, yield_10y=4.67, yield_2y=4.13,
        yield_curve=0.54, fed_funds_rate=3.62, cpi_yoy=3.78,
        confidence=1.0,
    )


def test_save_macro_regime_round_trip(temp_db):
    q = temp_db
    q.save_macro_regime(_sample_regime())
    rows = q.get_macro_history(hours=1)
    assert len(rows) == 1
    r = rows[0]
    assert r.scenario == "REFLATION"
    assert r.dollar_strength == "NEUTRAL"
    assert r.vix == pytest.approx(18.1)
    assert r.cpi_yoy == pytest.approx(3.78)


def test_save_calendar_events_upserts_on_event_id(temp_db):
    """Same event_id on a re-fetch must overwrite, not append."""
    q = temp_db
    base = datetime.utcnow() + timedelta(hours=10)
    ev1 = CalendarEvent("fred:101:2026-06-10", "FOMC Press Release",
                        "US", base, EventImpact.HIGH, "fred_calendar")
    q.save_calendar_events([ev1])

    # Re-fetch with a forecast populated.
    ev1.forecast = 5.25
    q.save_calendar_events([ev1])

    rows = q.get_pending_events(hours_ahead=48)
    assert len(rows) == 1
    assert rows[0].forecast == 5.25


def test_get_pending_events_filters_window(temp_db):
    q = temp_db
    now = datetime.utcnow()
    near = CalendarEvent("a", "Near", "US", now + timedelta(hours=6),
                         EventImpact.HIGH, "x")
    far  = CalendarEvent("b", "Far",  "US", now + timedelta(hours=72),
                         EventImpact.HIGH, "x")
    q.save_calendar_events([near, far])
    assert len(q.get_pending_events(hours_ahead=24)) == 1
    assert len(q.get_pending_events(hours_ahead=100)) == 2


# ─────────────────────────────────────────────────────────────────────────
# Dashboard panels
# ─────────────────────────────────────────────────────────────────────────

def _mock_bot():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    cb = SimpleNamespace(current_equity=400.0, daily_pnl_pct=0.0,
                         consecutive_losses=0, drawdown_pct=0.0,
                         halted=False, halt_reason=None)
    market = MagicMock()
    market.active_pairs.return_value = []
    return SimpleNamespace(
        _cb_state=cb,
        _sentiment=SimpleNamespace(latest={}),
        _market_data=market,
        _pending_signals=asyncio.Queue(),
        peek_pending=lambda: None,
        _profile=SimpleNamespace(name="balanced"),
        _strategy=SimpleNamespace(name="default"),
    )


def test_macro_panel_renders_without_regime(monkeypatch):
    """No cached regime → dim panel with dashes, no exception."""
    from ui.dashboard import Dashboard
    from rich.console import Console

    monkeypatch.setattr("macro.macro_monitor.get_current_regime", lambda: None)
    monkeypatch.setattr("macro.macro_monitor.get_pending_events", lambda n=3: [])
    monkeypatch.setattr("database.queries.get_today_trades", lambda: [])
    monkeypatch.setattr("database.queries.get_open_trades", lambda: [])
    monkeypatch.setattr("database.queries.get_recent_closed_trades",
                        lambda limit=10: [])
    monkeypatch.setattr("database.queries.get_signal_win_rate",
                        lambda **kw: {"total": 0, "win_rate": 0.0})
    monkeypatch.setattr("database.queries.get_today_skipped_signals",
                        lambda: 0)

    dash = Dashboard(_mock_bot())
    panel = dash._panel_macro()
    Console(file=open("/dev/null", "w"), width=200).print(panel)


def test_macro_panel_renders_with_regime(monkeypatch):
    from ui.dashboard import Dashboard
    from rich.console import Console
    monkeypatch.setattr("macro.macro_monitor.get_current_regime",
                        lambda: _sample_regime())
    monkeypatch.setattr("macro.macro_monitor.get_pending_events", lambda n=3: [])
    monkeypatch.setattr("database.queries.get_today_trades", lambda: [])
    monkeypatch.setattr("database.queries.get_open_trades", lambda: [])
    monkeypatch.setattr("database.queries.get_recent_closed_trades",
                        lambda limit=10: [])
    monkeypatch.setattr("database.queries.get_signal_win_rate",
                        lambda **kw: {"total": 0, "win_rate": 0.0})
    monkeypatch.setattr("database.queries.get_today_skipped_signals",
                        lambda: 0)

    dash = Dashboard(_mock_bot())
    panel = dash._panel_macro()
    Console(file=open("/dev/null", "w"), width=200).print(panel)


def test_pending_events_panel_renders_with_high_impact_soon(monkeypatch):
    from ui.dashboard import Dashboard
    from rich.console import Console
    now = datetime.utcnow()
    pending = [
        PendingEvent("FOMC Press Release", "US",
                     now + timedelta(minutes=20),
                     20, EventImpact.HIGH),
        PendingEvent("Consumer Price Index", "US",
                     now + timedelta(hours=18),
                     18 * 60, EventImpact.HIGH),
    ]
    monkeypatch.setattr("macro.macro_monitor.get_pending_events",
                        lambda n=3: pending)
    monkeypatch.setattr("macro.macro_monitor.get_current_regime",
                        lambda: None)
    monkeypatch.setattr("database.queries.get_today_trades", lambda: [])
    monkeypatch.setattr("database.queries.get_open_trades", lambda: [])
    monkeypatch.setattr("database.queries.get_recent_closed_trades",
                        lambda limit=10: [])
    monkeypatch.setattr("database.queries.get_signal_win_rate",
                        lambda **kw: {"total": 0, "win_rate": 0.0})
    monkeypatch.setattr("database.queries.get_today_skipped_signals",
                        lambda: 0)

    dash = Dashboard(_mock_bot())
    panel = dash._panel_pending_events()
    Console(file=open("/dev/null", "w"), width=200).print(panel)
