"""
tests/test_bot.py — CryptoBot main loop tests.

Coverage:
  - _cycle skips when UTC time is inside the dead zone (02:00–06:00)
  - record_trade_result halts the bot once the daily-loss threshold trips
  - _route_for_approval dispatches correctly across per_trade / window / autonomous

External collaborators (MarketData, SignalEngine, PositionManager, OrderRouter,
KillSwitch) are injected as mocks. The bot's constructor accepts each of them
as keyword args so tests don't need real exchange connections or a DB.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import settings
from core.bot import CryptoBot, CircuitBreakerState


# ─────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────

def _stub_profile():
    return SimpleNamespace(
        name="balanced",
        max_position_size_pct=0.03,
        default_stop_loss_pct=0.01,
        default_take_profit_pct=0.02,
    )


def _stub_strategy():
    s = MagicMock()
    s.name = "default"
    return s


def _make_bot() -> CryptoBot:
    """Build a CryptoBot with every external collaborator mocked."""
    market_data = MagicMock()
    market_data.get_price.return_value = 100.0
    market_data.get_all_prices.return_value = {"binance": 100.0}
    market_data.start = AsyncMock()
    market_data.stop = AsyncMock()

    signal_engine = MagicMock()
    signal_engine.run_scan = AsyncMock()
    signal_engine.on_signal = MagicMock()
    signal_engine.update_sentiment = MagicMock()
    signal_engine.setup_scanners = MagicMock()

    position_mgr = MagicMock()
    position_mgr.check_positions = AsyncMock(return_value=[])

    router = MagicMock()
    router.execute = AsyncMock(return_value={
        "trade_id": 1, "entry": 100.0, "sl": 99.0, "tp": 101.0, "size_usd": 50.0,
    })

    kill_switch = MagicMock()
    kill_switch.engage = AsyncMock(return_value={"closed": 0})

    return CryptoBot(
        profile=_stub_profile(),
        strategy=_stub_strategy(),
        kill_switch=kill_switch,
        market_data=market_data,
        signal_engine=signal_engine,
        position_mgr=position_mgr,
        router=router,
    )


def _make_signal():
    """Lightweight Signal-shaped stub. SimpleNamespace is fine — bot only
    reads attributes and never type-checks against signals.base.Signal."""
    return SimpleNamespace(
        pair="BTC/USDT", exchange="binance",
        direction="long", signal_type="momentum",
        db_id=42,
        suggested_entry=100.0, suggested_sl=99.0,
        suggested_tp=102.0, suggested_size_pct=0.03,
        risk_reward=2.0,
        claude_reasoning="ok", claude_api_cost=0.001,
        indicators={"claude_rec": "GO"},
        summary=lambda: "test",
    )


# ─────────────────────────────────────────────────────────────────────────
# peek_pending()
# ─────────────────────────────────────────────────────────────────────────

def test_peek_pending_returns_none_when_empty():
    bot = _make_bot()
    assert bot.peek_pending() is None


def test_peek_pending_returns_front_without_consuming():
    bot = _make_bot()
    sig = _make_signal()
    bot._pending_signals.put_nowait(sig)
    # Peek twice — must not drain the queue
    assert bot.peek_pending() is sig
    assert bot.peek_pending() is sig
    assert bot._pending_signals.qsize() == 1


# ─────────────────────────────────────────────────────────────────────────
# CircuitBreakerState unit tests
# ─────────────────────────────────────────────────────────────────────────

def test_cb_state_trips_on_daily_loss():
    cb = CircuitBreakerState(starting_equity=400.0)
    cb.update_after_trade(-0.025)  # -2.5% daily loss
    triggered, reason = cb.evaluate()
    assert triggered
    assert "daily_loss" in reason


def test_cb_state_trips_on_consecutive_losses():
    cb = CircuitBreakerState(starting_equity=400.0)
    # Three tiny losses (well under daily-loss threshold) trip the streak rule
    for _ in range(3):
        cb.update_after_trade(-0.001)
    triggered, reason = cb.evaluate()
    assert triggered
    assert "consecutive_loss" in reason


def test_cb_state_resets_streak_on_win():
    cb = CircuitBreakerState(starting_equity=400.0)
    cb.update_after_trade(-0.001)
    cb.update_after_trade(-0.001)
    cb.update_after_trade(+0.001)
    assert cb.consecutive_losses == 0


# ─────────────────────────────────────────────────────────────────────────
# Test 1: _cycle skips inside dead zone
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cycle_skips_dead_zone():
    bot = _make_bot()
    inside_dead_zone = datetime(2026, 5, 20, 3, 0, 0)  # 03:00 UTC
    with patch("core.bot.datetime") as mock_dt:
        mock_dt.utcnow.return_value = inside_dead_zone
        # Allow datetime(...) constructor calls to still work
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        await bot._cycle()
    bot._signal_engine.run_scan.assert_not_called()


@pytest.mark.asyncio
async def test_cycle_runs_outside_dead_zone():
    bot = _make_bot()
    outside_dead_zone = datetime(2026, 5, 20, 13, 0, 0)  # 13:00 UTC
    with patch("core.bot.datetime") as mock_dt:
        mock_dt.utcnow.return_value = outside_dead_zone
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        await bot._cycle()
    bot._signal_engine.run_scan.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────
# Test 2: circuit breaker halts on daily loss
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_trade_result_halts_on_daily_loss(monkeypatch):
    bot = _make_bot()
    captured: dict = {}

    def fake_log(reason, detail, auto_resume_at=None):
        captured["reason"] = reason
        captured["detail"] = detail

    monkeypatch.setattr("core.bot.db_queries.log_circuit_breaker", fake_log)

    # -3% trade exceeds the 2% daily-loss threshold
    bot.record_trade_result(-0.03)

    assert bot._cb_state.halted is True
    assert "daily_loss" in bot._cb_state.halt_reason
    assert captured["reason"] == "daily_loss"


@pytest.mark.asyncio
async def test_cycle_skips_when_halted():
    bot = _make_bot()
    bot._cb_state.halt("test_halt")
    outside_dead_zone = datetime(2026, 5, 20, 13, 0, 0)
    with patch("core.bot.datetime") as mock_dt:
        mock_dt.utcnow.return_value = outside_dead_zone
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        await bot._cycle()
    bot._signal_engine.run_scan.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# Test 3: approval gate routes correctly per mode
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_per_trade_queues_signal(monkeypatch):
    bot = _make_bot()
    monkeypatch.setattr(settings, "APPROVAL_MODE", "per_trade")
    sig = _make_signal()
    await bot._route_for_approval(sig, 100.0)
    assert bot._pending_signals.qsize() == 1
    assert bot._router.execute.call_count == 0


@pytest.mark.asyncio
async def test_autonomous_executes_within_limits(monkeypatch):
    bot = _make_bot()
    monkeypatch.setattr(settings, "APPROVAL_MODE", "autonomous")
    sig = _make_signal()
    await bot._route_for_approval(sig, 100.0)
    assert bot._router.execute.call_count == 1
    assert bot._pending_signals.qsize() == 0


@pytest.mark.asyncio
async def test_autonomous_skip_when_rate_limited(monkeypatch):
    bot = _make_bot()
    monkeypatch.setattr(settings, "APPROVAL_MODE", "autonomous")
    monkeypatch.setattr("core.bot.db_queries.update_signal_skip", lambda *a, **k: None)
    # Saturate the hourly cap
    cap = settings.AUTO_MAX_TRADES_PER_HOUR
    bot._auto_trades_hour = [datetime.utcnow()] * cap
    bot._auto_trades_day = [datetime.utcnow()] * cap
    sig = _make_signal()
    await bot._route_for_approval(sig, 100.0)
    assert bot._router.execute.call_count == 0


@pytest.mark.asyncio
async def test_window_open_executes(monkeypatch):
    bot = _make_bot()
    monkeypatch.setattr(settings, "APPROVAL_MODE", "window")
    bot.approve_window(60)
    sig = _make_signal()
    await bot._route_for_approval(sig, 100.0)
    assert bot._router.execute.call_count == 1
    assert bot._pending_signals.qsize() == 0


@pytest.mark.asyncio
async def test_window_closed_queues(monkeypatch):
    bot = _make_bot()
    monkeypatch.setattr(settings, "APPROVAL_MODE", "window")
    # Window never opened → goes to pending
    sig = _make_signal()
    await bot._route_for_approval(sig, 100.0)
    assert bot._pending_signals.qsize() == 1
    assert bot._router.execute.call_count == 0


@pytest.mark.asyncio
async def test_claude_skip_short_circuits(monkeypatch):
    bot = _make_bot()
    monkeypatch.setattr(settings, "APPROVAL_MODE", "autonomous")
    monkeypatch.setattr("core.bot.db_queries.update_signal_skip", lambda *a, **k: None)
    sig = _make_signal()
    sig.indicators["claude_rec"] = "SKIP"
    await bot._route_for_approval(sig, 100.0)
    assert bot._router.execute.call_count == 0


# ─────────────────────────────────────────────────────────────────────────
# BTC guard — discrete 30m snapshot tracker in _heartbeat_loop
# ─────────────────────────────────────────────────────────────────────────

def test_btc_snapshot_first_call_stores_returns_none():
    """No snapshot yet → store + skip delta. The first heartbeat just
    seeds the tracker; sentiment_aggregator doesn't receive a fake 0%."""
    bot = _make_bot()
    delta = bot._update_btc_snapshot(80_000.0)
    assert delta is None
    assert bot._btc_price_30m_ago == 80_000.0
    assert bot._btc_price_30m_ago_time is not None


def test_btc_snapshot_immature_window_returns_none():
    """Subsequent call before LOOKBACK - DRIFT minutes → keep snapshot,
    skip delta. Otherwise we'd emit a near-zero pct every heartbeat."""
    import time as _time
    bot = _make_bot()
    bot._update_btc_snapshot(80_000.0)
    # Force a "5-minute" age — well below 30m target.
    bot._btc_price_30m_ago_time = _time.time() - 5 * 60
    delta = bot._update_btc_snapshot(81_000.0)
    assert delta is None
    # Snapshot must not have been replaced.
    assert bot._btc_price_30m_ago == 80_000.0


def test_btc_snapshot_mature_window_emits_delta(monkeypatch):
    """Once the snapshot is ≥ LOOKBACK - DRIFT minutes old, return the
    % delta vs current and reset the snapshot to current."""
    import time as _time
    bot = _make_bot()
    bot._update_btc_snapshot(80_000.0)
    # Force the snapshot age past the threshold.
    bot._btc_price_30m_ago_time = _time.time() - (
        settings.BTC_GUARD_LOOKBACK_MINUTES - settings.BTC_GUARD_LOOKBACK_DRIFT_MINUTES
    ) * 60 - 1

    delta = bot._update_btc_snapshot(78_400.0)  # -2%
    assert delta is not None
    assert delta == pytest.approx(-2.0, rel=0.01)
    # Snapshot replaced with the current price; clock advanced.
    assert bot._btc_price_30m_ago == 78_400.0


def test_btc_price_fallback_uses_data_sources_when_market_data_empty(monkeypatch):
    """If market_data has no price, fall through to data_sources."""
    bot = _make_bot()
    bot._market_data.get_price.return_value = None
    bot._market_data.get_all_prices.return_value = {}

    class _FakeDS:
        cryptocompare = MagicMock()
        bybit_derivs  = MagicMock()
    _FakeDS.cryptocompare.get_price.return_value = 79_500.0

    monkeypatch.setitem(
        __import__("sys").modules,
        "data_sources",
        SimpleNamespace(data_sources=_FakeDS),
    )
    price = bot._btc_price_with_fallback()
    assert price == 79_500.0


def test_btc_price_fallback_returns_none_when_no_source(monkeypatch):
    """All price sources empty → None, never crash."""
    bot = _make_bot()
    bot._market_data.get_price.return_value = None
    bot._market_data.get_all_prices.return_value = {}

    # Force the data_sources import inside the fallback to fail.
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
                  else __builtins__.__import__

    def _boom(name, *a, **kw):
        if name == "data_sources":
            raise ImportError("offline")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", _boom)
    assert bot._btc_price_with_fallback() is None


# ─────────────────────────────────────────────────────────────────────────
# FIX 1 + FIX 5 + FIX 6 — equity recovery, clean shutdown, capital alloc
# ─────────────────────────────────────────────────────────────────────────

def test_startup_reads_last_equity_from_db(monkeypatch):
    """CircuitBreakerState gets seeded from get_last_equity when the DB
    has a prior snapshot — restart picks up where we left off."""
    monkeypatch.setattr("core.bot.db_queries.get_last_equity",
                        lambda: 1234.56)
    bot = _make_bot()
    assert bot._cb_state.current_equity == pytest.approx(1234.56)


def test_startup_uses_starting_capital_when_db_empty(monkeypatch):
    """Empty DB → fall through to settings.STARTING_CAPITAL (not the
    legacy sum(EXCHANGE_BALANCES))."""
    monkeypatch.setattr("core.bot.db_queries.get_last_equity",
                        lambda: None)
    monkeypatch.setattr(settings, "STARTING_CAPITAL", 1000.0)
    bot = _make_bot()
    assert bot._cb_state.current_equity == pytest.approx(1000.0)


def test_capital_allocation_matches_settings(monkeypatch):
    """SignalAgent + ArbAgent read their capital_allocation from the
    settings module (no hardcoded values in the agent files)."""
    monkeypatch.setattr(settings, "SIGNAL_AGENT_CAPITAL", 400.0)
    monkeypatch.setattr(settings, "ARB_AGENT_CAPITAL",    600.0)
    # Re-import the agents module so the wrappers pick up patched values.
    from agents import SignalAgentWrapper, ArbAgentWrapper
    sa = SignalAgentWrapper()
    aa = ArbAgentWrapper()
    assert sa.capital_allocation == 400.0
    assert aa.capital_allocation == 600.0


@pytest.mark.asyncio
async def test_clean_shutdown_writes_final_snapshot(monkeypatch):
    """bot.shutdown writes a portfolio_snapshot with SHUTDOWN status."""
    snapshots = []
    monkeypatch.setattr("core.bot.db_queries.log_portfolio_snapshot",
                        lambda stats: snapshots.append(stats))
    monkeypatch.setattr("core.bot.db_queries.log_agent_event",
                        lambda *a, **k: None)

    bot = _make_bot()
    bot._running = True
    await bot.shutdown("user_quit")

    assert len(snapshots) == 1
    assert snapshots[0]["portfolio_status"] == "SHUTDOWN"
    assert snapshots[0]["reason"] == "user_quit"
    assert bot._running is False


@pytest.mark.asyncio
async def test_clean_shutdown_logs_agent_event(monkeypatch):
    """bot.shutdown writes a SHUTDOWN row to agent_events."""
    events = []
    monkeypatch.setattr("core.bot.db_queries.log_portfolio_snapshot",
                        lambda stats: None)
    monkeypatch.setattr("core.bot.db_queries.log_agent_event",
                        lambda agent_id, ev, detail="": events.append((agent_id, ev, detail)))
    monkeypatch.setattr(settings, "SHUTDOWN_LOG_EVENT", True)

    bot = _make_bot()
    bot._running = True
    await bot.shutdown("sigint")

    assert ("portfolio", "SHUTDOWN", "sigint") in events


@pytest.mark.asyncio
async def test_shutdown_is_idempotent(monkeypatch):
    """Calling shutdown twice writes only one snapshot/event."""
    snapshots = []
    monkeypatch.setattr("core.bot.db_queries.log_portfolio_snapshot",
                        lambda s: snapshots.append(s))
    monkeypatch.setattr("core.bot.db_queries.log_agent_event",
                        lambda *a, **k: None)

    bot = _make_bot()
    bot._running = True
    await bot.shutdown("first")
    await bot.shutdown("second")          # should be a no-op
    assert len(snapshots) == 1


@pytest.mark.asyncio
async def test_approve_next_pending_drains_queue_and_executes(monkeypatch):
    """`a` command calls approve_next_pending → pop sig → _execute_signal."""
    bot = _make_bot()
    sig = _make_signal()
    await bot._pending_signals.put(sig)

    executed = []

    async def _fake_exec(s):
        executed.append(s)

    monkeypatch.setattr(bot, "_execute_signal", _fake_exec)
    ok = await bot.approve_next_pending()
    assert ok is True
    assert executed == [sig]
    assert bot._pending_signals.empty()


@pytest.mark.asyncio
async def test_approve_next_pending_when_empty_is_noop():
    bot = _make_bot()
    ok = await bot.approve_next_pending()
    assert ok is False


@pytest.mark.asyncio
async def test_skip_next_pending_records_skip(monkeypatch):
    bot = _make_bot()
    sig = _make_signal()
    sig.db_id = 42
    await bot._pending_signals.put(sig)

    skipped = []
    monkeypatch.setattr(
        "core.bot.db_queries.update_signal_skip",
        lambda sid, reason, price_at_signal=None: skipped.append((sid, reason)),
    )
    ok = await bot.skip_next_pending("user_skipped")
    assert ok is True
    assert skipped == [(42, "user_skipped")]


# ─────────────────────────────────────────────────────────────────────────
# Pause flag — `p` command target
# ─────────────────────────────────────────────────────────────────────────

def test_toggle_pause_flips_flag_and_returns_state():
    bot = _make_bot()
    assert bot._paused is False
    assert bot.toggle_pause() is True   and bot._paused is True
    assert bot.toggle_pause() is False  and bot._paused is False


@pytest.mark.asyncio
async def test_paused_cycle_skips_scan(monkeypatch):
    """When _paused is True, _cycle returns before reaching the signal
    engine's run_scan."""
    bot = _make_bot()
    bot._paused = True
    bot._signal_engine.run_scan = AsyncMock()
    await bot._cycle()
    bot._signal_engine.run_scan.assert_not_called()
