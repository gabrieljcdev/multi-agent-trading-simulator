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
    fake_q.get_today_skipped_signals.return_value = 0
    fake_q.get_arb_opportunity_stats.return_value = {
        "total_detected": 0, "total_executed": 0,
        "execution_rate_pct": 0.0, "avg_gap_pct": 0.0,
        "max_gap_pct": 0.0, "top_pairs": [],
    }
    monkeypatch.setattr("database.queries.get_today_trades", fake_q.get_today_trades)
    monkeypatch.setattr("database.queries.get_open_trades", fake_q.get_open_trades)
    monkeypatch.setattr("database.queries.get_recent_closed_trades", fake_q.get_recent_closed_trades)
    monkeypatch.setattr("database.queries.get_signal_win_rate", fake_q.get_signal_win_rate)
    monkeypatch.setattr("database.queries.get_today_skipped_signals", fake_q.get_today_skipped_signals)
    monkeypatch.setattr("database.queries.get_arb_opportunity_stats",
                        fake_q.get_arb_opportunity_stats)


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
    """_signals_skipped_count() delegates to queries.get_today_skipped_signals,
    which now returns an int directly (was a list pre-WIRE-3)."""
    monkeypatch.setattr(
        "database.queries.get_today_skipped_signals",
        lambda: 3,
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
        "name":     "SIGNAL",
        "status":   RUNNING,
        "capital":  400.0,
        "equity":   405.0,      # capital 400 + daily_pnl 5.0
        "pnl_pct":  1.25,
        "pnl_usd":  5.0,
        "win_rate": 0.66,
        "trades":   3,
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


# ─────────────────────────────────────────────────────────────────────────
# FIX 4 — macro panel no raw markup leak
# FIX 2 — approval panel surfaces the command vocabulary
# ─────────────────────────────────────────────────────────────────────────

def _render_to_string(panel) -> str:
    """Render a Panel to a plain string so we can assert on content."""
    import io
    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=False, color_system=None).print(panel)
    return buf.getvalue()


def test_macro_panel_renders_no_raw_markup_tags(monkeypatch):
    """VIX cell used to render `[dim]—[/dim]` and `[bright_green]18.1[/bright_green]`
    as literal text — FIX 4 switches to explicit `style=` args."""
    from macro.regime import (
        MacroRegime, MacroScenario, DollarStrength,
        RiskAppetite, RateEnvironment, VolRegime,
    )
    regime = MacroRegime(
        scenario=MacroScenario.REFLATION,
        dollar=DollarStrength.NEUTRAL, risk=RiskAppetite.RISK_ON,
        rates=RateEnvironment.NEUTRAL, vol=VolRegime.CALM,
        macro_score=15.0,
        dxy=99.3, vix=18.1, yield_10y=4.6, yield_2y=4.1,
        yield_curve=0.5, fed_funds_rate=3.6, cpi_yoy=3.7,
        confidence=1.0,
    )
    monkeypatch.setattr("macro.macro_monitor.get_current_regime",
                        lambda: regime)
    dash = Dashboard(_mock_bot())
    text = _render_to_string(dash._panel_macro())

    # No raw markup tags should appear as literal characters.
    assert "[dim]" not in text
    assert "[bright_green]" not in text
    assert "[/" not in text
    # And the VIX value itself should be rendered.
    assert "18.1" in text


def test_approval_panel_idle_shows_command_vocab():
    """Idle panel: spec wants the keystroke help visible so users know
    what to type. FIX 2 keystrokes are a/s/k/q."""
    dash = Dashboard(_mock_bot())
    text = _render_to_string(dash._panel_approval())
    assert "a=approve" in text
    assert "s=skip" in text
    assert "k=kill" in text
    assert "q=quit" in text


def test_approval_panel_pending_shows_pair_score_and_commands():
    """Pending panel: shows the pair + score + same keystroke help."""
    bot = _mock_bot()
    sig = SimpleNamespace(
        pair="BTC/USDT", direction="long", score=80,
        signal_type="momentum",
        suggested_entry=100.0, suggested_sl=99.0,
        suggested_tp=102.0, risk_reward=2.0,
        claude_reasoning="High-conviction breakout",
    )
    bot._pending_signals.put_nowait(sig)
    dash = Dashboard(bot)
    text = _render_to_string(dash._panel_approval())
    assert "BTC/USDT" in text
    assert "score=80" in text
    assert "a=approve" in text
    assert "q=quit" in text


# ─────────────────────────────────────────────────────────────────────────
# Command bar — bottom-row input echo panel
# ─────────────────────────────────────────────────────────────────────────

def test_cmd_bar_idle_shows_hint():
    """No command typed yet → 'Type command below ↓' prompt + the full
    hint strip. Bot may not have the cmd-state attrs at all (older
    constructions) and the panel still renders."""
    bot = _mock_bot()
    # Strip any cmd state to simulate a fresh bot before the handler runs.
    for attr in ("_cmd_current", "_cmd_last_result", "_cmd_last_ts"):
        if hasattr(bot, attr):
            delattr(bot, attr)
    dash = Dashboard(bot)
    text = _render_to_string(dash._panel_cmd_bar())
    assert "Type command below" in text
    assert "a=approve" in text
    assert "q=quit" in text


def test_cmd_bar_echoes_current_input_and_last():
    """After the input handler writes state, the panel echoes
    'CMD > <current>' and 'Last: <result>  [HH:MM:SS]'."""
    bot = _mock_bot()
    bot._cmd_current     = "approve"
    bot._cmd_last_result = "skip"
    bot._cmd_last_ts     = "14:32:01"
    dash = Dashboard(bot)
    text = _render_to_string(dash._panel_cmd_bar())
    assert "approve" in text
    assert "Last: skip" in text
    assert "14:32:01" in text


def test_full_dashboard_renders_with_cmd_bar(monkeypatch):
    """Smoke: full render() includes row12 without raising."""
    monkeypatch.setattr("database.queries.get_today_trades", lambda: [])
    monkeypatch.setattr("database.queries.get_open_trades", lambda: [])
    monkeypatch.setattr("database.queries.get_recent_closed_trades",
                        lambda limit=10: [])
    monkeypatch.setattr("database.queries.get_signal_win_rate",
                        lambda **kw: {"total": 0, "win_rate": 0.0})
    monkeypatch.setattr("database.queries.get_today_skipped_signals", lambda: 0)
    monkeypatch.setattr("database.queries.get_arb_opportunity_stats",
                        lambda: {"total_detected": 0, "total_executed": 0,
                                 "execution_rate_pct": 0.0,
                                 "avg_gap_pct": 0.0, "max_gap_pct": 0.0,
                                 "top_pairs": []})
    dash = Dashboard(_mock_bot())
    layout = dash.render()
    _render_to_string(layout)   # would raise on bad markup or missing panel


# ─────────────────────────────────────────────────────────────────────────
# ARB OPPORTUNITY panel — detection/execution funnel
# ─────────────────────────────────────────────────────────────────────────

def _populated_opportunity_stats(executed=21, above=38, detected=142,
                                 avg=0.07, mx=0.31, top=("ETH/USDT", 9)):
    return {
        "total_detected":     detected,
        "total_executed":     executed,
        "execution_rate_pct": (executed / above * 100.0) if above else 0.0,
        "avg_gap_pct":        avg,
        "max_gap_pct":        mx,
        "top_pairs":          [top] if top else [],
    }


def test_arb_opportunity_panel_renders_with_stats(monkeypatch):
    """Populated stats → numbers + top pair appear in the rendered text."""
    monkeypatch.setattr("database.queries.get_arb_opportunity_stats",
                        lambda: _populated_opportunity_stats())
    dash = Dashboard(_mock_bot())
    text = _render_to_string(dash._panel_arb_opportunities())
    assert "142" in text                         # detected
    assert "38"  in text                         # above threshold (derived)
    assert "21"  in text                         # executed
    assert "0.07" in text                        # avg gap
    assert "0.31" in text                        # max gap
    assert "ETH/USDT" in text                    # top pair
    assert "(9 detections)" in text


def test_arb_opportunity_panel_placeholder_on_empty_stats(monkeypatch):
    """Empty stats dict → placeholder text, no crash, dim border."""
    monkeypatch.setattr(
        "database.queries.get_arb_opportunity_stats",
        lambda: {"total_detected": 0, "total_executed": 0,
                 "execution_rate_pct": 0.0, "avg_gap_pct": 0.0,
                 "max_gap_pct": 0.0, "top_pairs": []},
    )
    dash = Dashboard(_mock_bot())
    text = _render_to_string(dash._panel_arb_opportunities())
    assert "No opportunities detected yet" in text


def test_arb_opportunity_panel_amber_when_exec_rate_below_50(monkeypatch):
    """20% ≤ exec_rate < 50% → amber (yellow) styling on executed line."""
    # 8 / 27 ≈ 30% execution rate
    monkeypatch.setattr(
        "database.queries.get_arb_opportunity_stats",
        lambda: _populated_opportunity_stats(executed=8, above=27, detected=80),
    )
    dash = Dashboard(_mock_bot())
    # Render with color_system so styles materialise as ANSI codes.
    import io
    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=True,
            color_system="truecolor").print(dash._panel_arb_opportunities())
    out = buf.getvalue()
    # Yellow (ANSI 33) is what Rich uses for the "yellow" style.
    assert "33" in out  # ANSI yellow attribute appears
    assert "8" in out


def test_arb_opportunity_panel_red_when_exec_rate_below_20(monkeypatch):
    """exec_rate < 20% → red styling on executed line."""
    # 1 / 25 = 4%
    monkeypatch.setattr(
        "database.queries.get_arb_opportunity_stats",
        lambda: _populated_opportunity_stats(executed=1, above=25, detected=100),
    )
    dash = Dashboard(_mock_bot())
    import io
    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=True,
            color_system="truecolor").print(dash._panel_arb_opportunities())
    out = buf.getvalue()
    # ANSI 31 is the foreground red.
    assert "31" in out


def test_dashboard_does_not_crash_on_stats_query_failure(monkeypatch):
    """If the query raises, the panel must render the placeholder."""
    def boom():
        raise RuntimeError("db offline")
    monkeypatch.setattr("database.queries.get_arb_opportunity_stats", boom)
    dash = Dashboard(_mock_bot())
    text = _render_to_string(dash._panel_arb_opportunities())
    assert "No opportunities detected yet" in text


# ─────────────────────────────────────────────────────────────────────────
# ARB FEED — inline "Missed (balance)" capital-gate counter
# ─────────────────────────────────────────────────────────────────────────

def test_arb_panel_shows_missed_balance_checks_count():
    """The arb feed panel surfaces missed_balance_checks pulled from the
    cached engine stats."""
    dash = Dashboard(_mock_bot())
    dash._arb_engine_stats_cache = {"missed_balance_checks": 4}
    text = _render_to_string(dash._panel_arb_feed())
    assert "Missed (balance):" in text
    assert "4" in text


def test_arb_panel_balance_miss_amber_at_threshold():
    """At 1 ≤ N < 6 the counter renders in amber (yellow). Threshold value
    comes from settings — we use DASHBOARD_BALANCE_MISS_AMBER = 1."""
    from config import settings as s
    dash = Dashboard(_mock_bot())
    dash._arb_engine_stats_cache = {
        "missed_balance_checks": s.DASHBOARD_BALANCE_MISS_AMBER,
    }
    import io
    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=True,
            color_system="truecolor").print(dash._panel_arb_feed())
    out = buf.getvalue()
    assert "33" in out  # ANSI yellow
    assert "blocked today" in out


def test_arb_panel_balance_miss_red_at_threshold():
    """At N ≥ DASHBOARD_BALANCE_MISS_RED the counter renders red."""
    from config import settings as s
    dash = Dashboard(_mock_bot())
    dash._arb_engine_stats_cache = {
        "missed_balance_checks": s.DASHBOARD_BALANCE_MISS_RED,
    }
    import io
    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=True,
            color_system="truecolor").print(dash._panel_arb_feed())
    out = buf.getvalue()
    assert "31" in out  # ANSI red
    assert "blocked today" in out


def test_arb_panel_handles_missing_missed_balance_attr():
    """No cached engine stats → defaults to 0 missed, dim styling, panel
    still renders without raising."""
    dash = Dashboard(_mock_bot())
    # Don't populate _arb_engine_stats_cache — leaves it as {}
    text = _render_to_string(dash._panel_arb_feed())
    assert "Missed (balance):" in text
    assert "0" in text


# ─────────────────────────────────────────────────────────────────────────
# SCALP FEED panel — header, OFI strip, positions, recent closed, stats
# ─────────────────────────────────────────────────────────────────────────

def test_scalp_panel_placeholder_when_no_agent():
    """No scalp agent registered (default _scalp_data_cache shape) → dim
    placeholder panel, never raises."""
    dash = Dashboard(_mock_bot())
    text = _render_to_string(dash._panel_scalp_feed())
    assert "Scalp agent not registered" in text


def test_scalp_panel_renders_header_and_sections_when_available():
    """When _scalp_data_cache is populated, the panel renders every
    section: header (with venue ticks), OFI strip, positions table,
    closed observations, stats bar."""
    dash = Dashboard(_mock_bot())
    dash._scalp_data_cache = {
        "available": True,
        "live":      False,                  # obs-mode
        "fee_viability": {
            "mexc":   {"viable": True},
            "bitget": {"viable": False},
        },
        "ofi_top": [
            {"symbol": "BTC/USDT", "exchange": "mexc",
             "z": 2.3, "direction": "LONG", "strength": "strong", "stale": False},
            {"symbol": "ETH/USDT", "exchange": "bitget",
             "z": -1.6, "direction": "SHORT", "strength": "moderate", "stale": False},
        ],
        "open_positions": [
            {"symbol": "BTC/USDT", "exchange": "mexc", "direction": "LONG",
             "entry": 100000.0, "tp": 100500.0, "sl": 99700.0,
             "unrealised_bps": 4.2, "hold_sec": 42.0},
        ],
        "recent_closed": [
            {"symbol": "BTC/USDT", "direction": "LONG", "exit_reason": "TP_HIT",
             "gross_bps": 8.0, "net_bps": 6.0, "hold_sec": 35.0},
            {"symbol": "ETH/USDT", "direction": "SHORT", "exit_reason": "OFI_EXHAUSTED",
             "gross_bps": -3.0, "net_bps": -5.0, "hold_sec": 50.0},
        ],
        "stats": {
            "total_evaluated":   142,
            "would_enter":       18,
            "closed":            12,
            "win_rate":          0.58,
            "avg_pnl_net_bps":   3.4,
            "daily_loss_usd":    0.0,
            "fee_viability":     {},
        },
    }
    text = _render_to_string(dash._panel_scalp_feed())

    # Header bits — scalp is MEXC-only now (STRATEGY_EXCHANGE_MAP["scalp"]).
    assert "SCALP"       in text
    assert "OFI-Primary" in text
    assert "MEXC"        in text
    assert "obs-mode"    in text

    # OFI strip — top 5 by |z|
    assert "BTC/USDT" in text
    assert "ETH/USDT" in text
    assert "+2.30"    in text          # signed format for the LONG row
    assert "-1.60"    in text          # SHORT row

    # Open positions
    assert "100000"   in text or "100,000" in text
    assert "LONG"     in text

    # Recent closed
    assert "TP_HIT"        in text
    assert "OFI_EXHAUSTED" in text

    # Stats bar
    assert "evaluated"   in text
    assert "would-enter" in text
    assert "wr"          in text
    assert "58%"         in text
    assert "avg-net"     in text


def test_scalp_panel_shows_live_label_when_capital_positive():
    """SCALP_CAPITAL > 0 → header shows LIVE (red), not obs-mode."""
    dash = Dashboard(_mock_bot())
    dash._scalp_data_cache = {
        "available": True, "live": True,
        "fee_viability": {}, "ofi_top": [],
        "open_positions": [], "recent_closed": [], "stats": {},
    }
    text = _render_to_string(dash._panel_scalp_feed())
    assert "LIVE"     in text
    assert "obs-mode" not in text


def test_scalp_panel_handles_missing_agent_state(monkeypatch):
    """Coordinator present but no scalp agent registered — _snapshot_scalp_agent
    must return the empty shape, panel renders the placeholder."""
    coord = SimpleNamespace(_agents=[])
    dash = Dashboard(_mock_bot(), coordinator=coord)
    snap = dash._snapshot_scalp_agent()
    assert snap["available"] is False
    text = _render_to_string(dash._panel_scalp_feed())
    assert "Scalp agent not registered" in text


def test_scalp_snapshot_handles_observation_summary_failure():
    """If get_observation_summary raises, the snapshot still returns
    'available' True with zeroed stats — the panel must still render."""
    class _BoomAgent:
        agent_id = "scalp"
        _capital = 0.0
        _positions = {}
        _observations = []
        _ofi_engine = None
        def get_observation_summary(self):
            raise RuntimeError("synthetic failure")

    coord = SimpleNamespace(_agents=[_BoomAgent()])
    dash = Dashboard(_mock_bot(), coordinator=coord)
    snap = dash._snapshot_scalp_agent()
    assert snap["available"] is True
    assert snap["stats"] == {}
    # Panel must still render without raising
    dash._scalp_data_cache = snap
    text = _render_to_string(dash._panel_scalp_feed())
    assert "SCALP" in text


def test_scalp_snapshot_sorts_ofi_by_abs_z():
    """The snapshot helper ranks OFI entries by absolute z, descending —
    biggest conviction first regardless of sign. (The panel renders
    whatever order the snapshot provides; sorting is the snapshot's
    job.)"""
    class _StubEngine:
        def __init__(self, by_key):
            self._by_key = by_key
        def get(self, sym, ex):
            return self._by_key.get((sym, ex), {"z": 0.0})

    class _StubAgent:
        agent_id = "scalp"
        _capital = 0.0
        _positions = {}
        _observations = []
        def __init__(self):
            self._ofi_engine = _StubEngine({
                ("AAA/USDT", "mexc"): {"z":  2.5, "direction": "LONG",
                                       "strength": "strong",   "stale": False},
                ("BBB/USDT", "mexc"): {"z": -3.1, "direction": "SHORT",
                                       "strength": "strong",   "stale": False},
                ("CCC/USDT", "mexc"): {"z":  0.5, "direction": "NEUTRAL",
                                       "strength": "weak",     "stale": False},
            })
        def get_observation_summary(self):
            return {}

    # Restrict the (symbol × exchange) sweep to the three test symbols and
    # one exchange so the snapshot sees exactly our crafted rows.
    from config import settings as s
    import unittest.mock as _mock
    with _mock.patch.object(s, "SCALP_PAIRS",
                            ["AAA/USDT", "BBB/USDT", "CCC/USDT"]), \
         _mock.patch.dict(s.STRATEGY_EXCHANGE_MAP, {"scalp": ["mexc"]}):
        coord = SimpleNamespace(_agents=[_StubAgent()])
        dash = Dashboard(_mock_bot(), coordinator=coord)
        snap = dash._snapshot_scalp_agent()
    symbols = [r["symbol"] for r in snap["ofi_top"]]
    # BBB (|z|=3.1) > AAA (|z|=2.5) > CCC (|z|=0.5)
    assert symbols[:3] == ["BBB/USDT", "AAA/USDT", "CCC/USDT"]


def test_full_dashboard_includes_scalp_row(monkeypatch):
    """Smoke: the full render() now includes the scalp row without raising,
    and the row exposes the panel under its named layout slot."""
    monkeypatch.setattr("database.queries.get_today_trades", lambda: [])
    monkeypatch.setattr("database.queries.get_open_trades", lambda: [])
    monkeypatch.setattr("database.queries.get_recent_closed_trades",
                        lambda limit=10: [])
    monkeypatch.setattr("database.queries.get_signal_win_rate",
                        lambda **kw: {"total": 0, "win_rate": 0.0})
    monkeypatch.setattr("database.queries.get_today_skipped_signals", lambda: 0)
    monkeypatch.setattr("database.queries.get_arb_opportunity_stats",
                        lambda: {"total_detected": 0, "total_executed": 0,
                                 "execution_rate_pct": 0.0,
                                 "avg_gap_pct": 0.0, "max_gap_pct": 0.0,
                                 "top_pairs": []})
    dash = Dashboard(_mock_bot())
    layout = dash.render()
    # Render to a generously-sized buffer so every row materialises —
    # the default StringIO console crops at ~25 rows, which would hide
    # the new scalp slot below the fold.
    import io
    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, width=180, height=200,
            force_terminal=False, color_system=None).print(layout)
    text = buf.getvalue()
    # Placeholder text since no scalp agent is wired in this smoke test
    assert "Scalp agent not registered" in text
    # Layout exposes the row by its slot name
    assert layout["row_scalp"] is not None
