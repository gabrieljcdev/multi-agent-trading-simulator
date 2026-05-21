"""
tests/test_dashboard.py — Dashboard contract tests.

Render the dashboard against a mock bot and verify:
  - render() returns without exception
  - push API (add_log, add_signal, add_arb, add_insight) respects maxlen
  - update_exchange_health stores state
  - coordinator=None degrades gracefully
  - a broken bot attribute does not crash the whole layout (try/except shield)
"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from rich.console import Console
from rich.layout import Layout

from ui.dashboard import Dashboard


# ─────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────

def _mock_bot() -> SimpleNamespace:
    """Minimal CryptoBot-shaped object — only the attributes the dashboard
    actually reads."""
    cb_state = SimpleNamespace(
        current_equity=400.0,
        daily_pnl_pct=-0.5,
        consecutive_losses=0,
        drawdown_pct=0.0,
        halted=False,
        halt_reason=None,
    )

    sentiment = SimpleNamespace(latest={
        "fear_greed_value":      55,
        "fear_greed_label":      "greed",
        "composite_score":       30,
        "reddit_score":          15,
        "news_guard_active":     False,
        "btc_change_30m":        0.5,
        "top_headlines":         ["BTC ETF approved", "ETH upgrade landed"],
    })

    market = MagicMock()
    market.active_pairs.return_value = ["BTC/USDT", "ETH/USDT"]
    market.get_all_prices.return_value = {"binance": 100_000.0}
    market.get_price.return_value = 100_000.0

    pending = asyncio.Queue()

    # Mirror CryptoBot.peek_pending() — dashboard calls this instead of
    # reaching into the asyncio.Queue internals directly.
    def _peek():
        inner = getattr(pending, "_queue", None)
        if not inner:
            return None
        return inner[0] if len(inner) > 0 else None

    return SimpleNamespace(
        _cb_state=cb_state,
        _sentiment=sentiment,
        _market_data=market,
        _pending_signals=pending,
        peek_pending=_peek,
        _profile=SimpleNamespace(name="balanced"),
        _strategy=SimpleNamespace(name="default"),
    )


@pytest.fixture(autouse=True)
def _patch_db_queries(monkeypatch):
    """The dashboard's panels touch database/queries. Stub everything to
    avoid hitting SQLite during tests."""
    fake_q = MagicMock()
    fake_q.get_today_trades.return_value = []
    fake_q.get_open_trades.return_value = []
    fake_q.get_recent_closed_trades.return_value = []
    fake_q.get_signal_win_rate.return_value = {"total": 0, "win_rate": 0.0}
    fake_q.get_today_skipped_signals.return_value = []
    monkeypatch.setattr("database.queries.get_today_trades", fake_q.get_today_trades)
    monkeypatch.setattr("database.queries.get_open_trades", fake_q.get_open_trades)
    monkeypatch.setattr("database.queries.get_recent_closed_trades", fake_q.get_recent_closed_trades)
    monkeypatch.setattr("database.queries.get_signal_win_rate", fake_q.get_signal_win_rate)
    monkeypatch.setattr("database.queries.get_today_skipped_signals", fake_q.get_today_skipped_signals)


# ─────────────────────────────────────────────────────────────────────────
# render()
# ─────────────────────────────────────────────────────────────────────────

def test_render_returns_layout_without_error():
    dash = Dashboard(_mock_bot())
    layout = dash.render()
    assert isinstance(layout, Layout)


def test_render_can_be_drawn_to_console():
    """Rendering to a string-backed Console must not raise — this is the
    real proof a layout works, not just that render() returns something."""
    dash = Dashboard(_mock_bot())
    layout = dash.render()
    console = Console(file=open("/dev/null", "w"), width=200, height=80)
    console.print(layout)  # would raise on bad markup or malformed Renderables


def test_render_with_coordinator_none_degrades():
    dash = Dashboard(_mock_bot(), coordinator=None)
    dash.render()   # no exception


def test_render_survives_broken_bot_attribute():
    """If a panel raises (e.g. bot._cb_state vanishes), the dashboard still
    renders; the broken panel is replaced by an error placeholder."""
    bot = _mock_bot()
    del bot._cb_state
    dash = Dashboard(bot)
    layout = dash.render()
    assert isinstance(layout, Layout)


# ─────────────────────────────────────────────────────────────────────────
# Push API + buffer behaviour
# ─────────────────────────────────────────────────────────────────────────

def test_add_log_respects_maxlen():
    dash = Dashboard(_mock_bot())
    for i in range(dash.LOG_BUFFER_MAX + 10):
        dash.add_log(f"line {i}")
    assert len(dash._log_buffer) == dash.LOG_BUFFER_MAX
    # oldest entries dropped first
    assert dash._log_buffer[0]["msg"] == "line 10"


def test_add_signal_stores_fields():
    dash = Dashboard(_mock_bot())
    dash.add_signal("BTC/USDT", "momentum", 78, "trending", "exec", "arb_agent")
    entry = dash._signal_buffer[-1]
    assert entry["symbol"] == "BTC/USDT"
    assert entry["track"] == "momentum"
    assert entry["score"] == 78
    assert entry["regime"] == "trending"
    assert entry["action"] == "EXEC"
    assert entry["agent"] == "arb_agent"


def test_add_signal_respects_maxlen():
    dash = Dashboard(_mock_bot())
    for i in range(dash.SIGNAL_BUFFER_MAX + 5):
        dash.add_signal(f"X{i}", "arb", float(i), "ranging", "exec")
    assert len(dash._signal_buffer) == dash.SIGNAL_BUFFER_MAX


def test_add_arb_stores_and_caps():
    dash = Dashboard(_mock_bot())
    for i in range(dash.ARB_BUFFER_MAX + 3):
        dash.add_arb("BTC/USDT", "binance", "kraken", 0.5 + i, 0.001)
    assert len(dash._arb_buffer) == dash.ARB_BUFFER_MAX
    # most recent entry preserves the latest gap
    assert dash._arb_buffer[-1]["gap_pct"] == 0.5 + (dash.ARB_BUFFER_MAX + 2)


def test_add_insight_caps_at_three():
    dash = Dashboard(_mock_bot())
    for i in range(10):
        dash.add_insight(f"insight {i}")
    assert len(dash._insight_buffer) == dash.INSIGHT_BUFFER_MAX
    assert dash._insight_buffer[-1] == "insight 9"


def test_update_exchange_health_records_latest():
    dash = Dashboard(_mock_bot())
    dash.update_exchange_health("binance", latency_ms=42.0, connected=True)
    h = dash._exchange_health["binance"]
    assert h.connected is True
    assert h.latency_ms == 42.0
    assert h.last_seen is not None


# ─────────────────────────────────────────────────────────────────────────
# Approval peek
# ─────────────────────────────────────────────────────────────────────────

def test_approval_panel_idle_when_queue_empty():
    dash = Dashboard(_mock_bot())
    # Should not raise — empty queue means "Waiting for signals..."
    dash._panel_approval()


def test_approval_panel_shows_pending_signal():
    bot = _mock_bot()
    # Push a fake signal directly into the queue's underlying deque
    sig = SimpleNamespace(
        pair="BTC/USDT", direction="long", score=80,
        signal_type="momentum",
        suggested_entry=100.0, suggested_sl=99.0,
        suggested_tp=102.0, risk_reward=2.0,
        claude_reasoning="High-conviction breakout with sentiment tailwind.",
    )
    bot._pending_signals.put_nowait(sig)
    dash = Dashboard(bot)
    panel = dash._panel_approval()
    # Rendering against a Console proves the markup is well-formed
    Console(file=open("/dev/null", "w"), width=200).print(panel)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def test_session_classification():
    from datetime import datetime
    dash = Dashboard(_mock_bot())
    assert dash._current_session(datetime(2026, 5, 20, 8, 0))  == "LONDON"
    assert dash._current_session(datetime(2026, 5, 20, 14, 0)) == "NEW_YORK"
    assert dash._current_session(datetime(2026, 5, 20, 2, 0))  == "ASIA"
    assert dash._current_session(datetime(2026, 5, 20, 22, 0)) == "OFF_HOURS"


def test_signals_skipped_count_uses_new_query(monkeypatch):
    """_signals_skipped_count() calls queries.get_today_skipped_signals
    and reports its length."""
    fake = [object(), object(), object()]  # three skipped signal stubs
    monkeypatch.setattr(
        "database.queries.get_today_skipped_signals",
        lambda: fake,
    )
    dash = Dashboard(_mock_bot())
    assert dash._signals_skipped_count() == 3


def test_query_module_exports_get_today_skipped_signals():
    """The query is importable and callable. Smoke test only — doesn't hit DB."""
    from database import queries
    assert callable(queries.get_today_skipped_signals)


def test_duration_format():
    from datetime import timedelta
    dash = Dashboard(_mock_bot())
    assert dash._fmt_duration(timedelta(seconds=30))  == "30s"
    assert dash._fmt_duration(timedelta(minutes=5))   == "5m00s"
    assert dash._fmt_duration(timedelta(hours=2, minutes=30)) == "2h30m"


# ─────────────────────────────────────────────────────────────────────────
# Async coordinator integration
# ─────────────────────────────────────────────────────────────────────────

def test_panels_do_not_call_async_coordinator_directly():
    """Regression: portfolio + agents panels used to call async coordinator
    methods directly from the sync render path, yielding a coroutine object
    that crashed `.get(...)` with 'coroutine object has no attribute get'.

    Render must touch only the cached data, not the coordinator methods."""
    coord = MagicMock()
    coord.get_portfolio_stats = MagicMock(
        side_effect=AssertionError("must not be called from sync render"))
    coord.get_agent_stats = MagicMock(
        side_effect=AssertionError("must not be called from sync render"))

    dash = Dashboard(_mock_bot(), coordinator=coord)
    layout = dash.render()
    # Force the layout to materialise — markup errors only surface on print
    Console(file=open("/dev/null", "w"), width=200, height=80).print(layout)


def test_refresh_coordinator_data_caches_async_results():
    """The async run() helper must await coordinator getters and reshape
    AgentStats dataclasses into the dict keys the agents panel expects."""
    from agents.base import AgentStats, RUNNING

    async def fake_portfolio():
        return {
            "total_equity":        500.0,
            "total_daily_pnl_pct": 1.25,
            "total_exposure_pct":  10.0,
        }

    async def fake_agents():
        return [
            AgentStats(
                agent_id="signal", status=RUNNING,
                capital_allocated=400.0, capital_deployed=50.0,
                daily_pnl=5.0, daily_pnl_pct=1.25,
                total_pnl=20.0, trades_today=3,
                win_rate_today=0.66, win_rate_alltime=0.55,
                consecutive_losses=0, last_trade_time=None, error=None,
            ),
        ]

    coord = SimpleNamespace(
        get_portfolio_stats=fake_portfolio,
        get_agent_stats=fake_agents,
    )
    dash = Dashboard(_mock_bot(), coordinator=coord)
    asyncio.run(dash._refresh_coordinator_data())

    assert dash._portfolio_cache == {
        "total_equity":  500.0,
        "daily_pnl_pct": 1.25,
    }
    assert dash._agent_stats_cache == [{
        "name":    "Signal",
        "status":  RUNNING,
        "capital": 400.0,
        "pnl_pct": 1.25,
        "trades":  3,
    }]

    # Panels now render against populated caches without raising
    layout = dash.render()
    Console(file=open("/dev/null", "w"), width=200, height=80).print(layout)


def test_refresh_coordinator_data_survives_async_errors():
    """If a coordinator getter raises, the cache stays at its prior value
    and refresh does not propagate the exception to the live loop."""
    async def boom():
        raise RuntimeError("coordinator offline")

    coord = SimpleNamespace(get_portfolio_stats=boom, get_agent_stats=boom)
    dash = Dashboard(_mock_bot(), coordinator=coord)
    # Must not raise
    asyncio.run(dash._refresh_coordinator_data())
    assert dash._portfolio_cache   is None
    assert dash._agent_stats_cache == []
