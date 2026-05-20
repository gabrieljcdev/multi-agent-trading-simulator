"""
tests/test_arb_engine.py — ArbEngine behaviour.

All tests use mock CCXT clients (AsyncMock methods). No real exchange
calls and no DB writes — log_arb_trade is patched per-test.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import settings
from execution.arb_engine import (
    ArbEngine, ArbOpportunity, ArbResult,
    gross_gap_pct, net_gap_pct, min_gap_threshold,
)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _mock_exchange(
    asks: list,
    bids: list,
    buy_fill_price: float = None,
    sell_fill_price: float = None,
) -> MagicMock:
    """Construct a mock CCXT-shaped client with the given book + fills."""
    ex = MagicMock()
    ex.fetch_order_book = AsyncMock(return_value={"asks": asks, "bids": bids})
    ex.create_market_buy_order = AsyncMock(
        return_value={"price": buy_fill_price if buy_fill_price is not None else asks[0][0]}
    )
    ex.create_market_sell_order = AsyncMock(
        return_value={"price": sell_fill_price if sell_fill_price is not None else bids[0][0]}
    )
    ex.close = AsyncMock()
    return ex


def _opp(symbol="BTC/USDT", buy_ex="bitget", sell_ex="kraken",
         buy=100.0, sell=100.4, net=0.20, size=10.0) -> ArbOpportunity:
    return ArbOpportunity(
        symbol=symbol, buy_exchange=buy_ex, sell_exchange=sell_ex,
        buy_price=buy, sell_price=sell,
        gross_gap_pct=(sell - buy) / buy * 100,
        net_gap_pct=net, max_size_usd=size, detected_at=time.monotonic(),
    )


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    """Never touch SQLite during arb-engine tests."""
    monkeypatch.setattr(
        "execution.arb_engine.db_queries.log_arb_trade",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "execution.arb_engine.db_queries.log_circuit_breaker",
        lambda *a, **k: None,
    )


# ─────────────────────────────────────────────────────────────────────────
# Gap math (pure helpers — no engine instance needed)
# ─────────────────────────────────────────────────────────────────────────

def test_gross_gap_calculation():
    assert gross_gap_pct(100.0, 100.4) == pytest.approx(0.4, abs=0.001)
    assert gross_gap_pct(100.0, 99.6)  == pytest.approx(-0.4, abs=0.001)
    assert gross_gap_pct(0.0,   100.0) == 0.0


def test_net_gap_after_fees_subtracts_both_legs():
    fee_map = {"bitget": 0.0001, "kraken": 0.0026}
    gross, net = net_gap_pct(100.0, 100.4, "bitget", "kraken", fee_map)
    assert gross == pytest.approx(0.4, abs=0.001)
    # Fees: (0.0001 + 0.0026) × 100 = 0.27 pct points
    assert net == pytest.approx(0.4 - 0.27, abs=0.001)


def test_min_gap_threshold_bitget_special_case():
    assert min_gap_threshold("bitget", "kraken") == settings.ARB_MIN_GAP_PCT
    assert min_gap_threshold("kraken", "bybit")  == settings.ARB_MIN_GAP_PCT_FALLBACK


# ─────────────────────────────────────────────────────────────────────────
# find_best_opportunity — threshold filter
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_opportunity_below_threshold_not_returned(monkeypatch):
    """A 0.02% gap is below the bitget special threshold (0.03%) so the
    engine returns no opportunity."""
    monkeypatch.setattr(settings, "ARB_WATCH_PAIRS", ["BTC/USDT"])
    a = _mock_exchange(asks=[[100.00, 50.0], [100.01, 50.0], [100.02, 50.0]],
                       bids=[[ 99.98, 50.0], [ 99.97, 50.0], [ 99.96, 50.0]])
    b = _mock_exchange(asks=[[100.05, 50.0], [100.06, 50.0], [100.07, 50.0]],
                       bids=[[100.02, 50.0], [100.01, 50.0], [100.00, 50.0]])
    # Buy A@100.00 → Sell B@100.02 → 0.02% gross, way below fees → no opp
    engine = ArbEngine(exchange_clients={"bitget": a, "kraken": b}, sim_mode=True)
    assert (await engine._find_best_opportunity()) is None


@pytest.mark.asyncio
async def test_opportunity_above_threshold_returned(monkeypatch):
    """A 0.5% gap easily exceeds threshold + fees → opportunity surfaces."""
    monkeypatch.setattr(settings, "ARB_WATCH_PAIRS", ["BTC/USDT"])
    # Buy bitget @ 100, sell kraken @ 100.5 → 0.5% gross, ~0.23% net > 0.03%
    a = _mock_exchange(asks=[[100.00, 50.0]] * 3, bids=[[99.99, 50.0]] * 3)
    b = _mock_exchange(asks=[[100.51, 50.0]] * 3, bids=[[100.50, 50.0]] * 3)
    engine = ArbEngine(exchange_clients={"bitget": a, "kraken": b}, sim_mode=True)
    opp = await engine._find_best_opportunity()
    assert opp is not None
    assert opp.buy_exchange == "bitget"
    assert opp.sell_exchange == "kraken"
    assert opp.net_gap_pct > settings.ARB_MIN_GAP_PCT


# ─────────────────────────────────────────────────────────────────────────
# Both legs fire concurrently — not sequentially
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_live_fills_runs_both_legs_concurrently(monkeypatch):
    """Each leg sleeps 100ms. Concurrent dispatch must finish in ~100ms,
    sequential would take ~200ms+."""
    buy_started_at  = []
    sell_started_at = []

    async def slow_buy(symbol, size):
        buy_started_at.append(time.monotonic())
        await asyncio.sleep(0.1)
        return {"price": 100.0}

    async def slow_sell(symbol, size):
        sell_started_at.append(time.monotonic())
        await asyncio.sleep(0.1)
        return {"price": 100.5}

    a = MagicMock()
    a.create_market_buy_order = slow_buy
    b = MagicMock()
    b.create_market_sell_order = slow_sell

    engine = ArbEngine(exchange_clients={"bitget": a, "kraken": b}, sim_mode=False)
    opp = _opp(buy=100.0, sell=100.5)

    t0 = time.monotonic()
    await engine._live_fills(opp, size_base=0.1)
    elapsed = time.monotonic() - t0

    # Concurrent → ~100ms, serial → ~200ms. Allow generous headroom for CI noise.
    assert elapsed < 0.18, f"legs ran sequentially ({elapsed:.3f}s)"
    # Both legs started within a few ms of each other
    assert abs(buy_started_at[0] - sell_started_at[0]) < 0.01


# ─────────────────────────────────────────────────────────────────────────
# Per-symbol lock prevents double execution
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_per_symbol_lock_blocks_second_attempt(monkeypatch):
    """Two _execute_arb tasks for the same symbol fired concurrently —
    the second must exit immediately without recording a trade.

    Uses sim_mode=False with slow exchange mocks: that forces an actual
    await inside the lock's critical section, so task A yields while
    holding the lock and task B sees lock.locked()=True.
    """
    monkeypatch.setattr(settings, "ARB_WATCH_PAIRS", ["BTC/USDT"])

    async def slow_buy(symbol, size):
        await asyncio.sleep(0.05)
        return {"price": 100.0}

    async def slow_sell(symbol, size):
        await asyncio.sleep(0.05)
        return {"price": 100.5}

    a = MagicMock()
    a.create_market_buy_order = slow_buy
    b = MagicMock()
    b.create_market_sell_order = slow_sell

    engine = ArbEngine(
        exchange_clients={"bitget": a, "kraken": b},
        sim_mode=False,
    )
    opp = _opp(buy=100.0, sell=100.5)

    await asyncio.gather(
        engine._execute_arb(opp),
        engine._execute_arb(opp),
        return_exceptions=True,
    )
    # First task acquires the lock and yields in _live_fills; second sees
    # locked() == True and returns without entering the critical section.
    assert engine._total_trades == 1


# ─────────────────────────────────────────────────────────────────────────
# Circuit breakers
# ─────────────────────────────────────────────────────────────────────────

def test_circuit_breaker_halts_on_daily_loss():
    engine = ArbEngine(exchange_clients={"bitget": MagicMock(), "kraken": MagicMock()},
                       sim_mode=True)
    # Push daily P&L past the halt threshold
    engine._daily_pnl_usd = -settings.ARB_DAILY_LOSS_HALT_USD - 0.01
    assert engine._cb_triggered() is True


def test_circuit_breaker_halts_on_consecutive_losses():
    engine = ArbEngine(exchange_clients={"bitget": MagicMock(), "kraken": MagicMock()},
                       sim_mode=True)
    engine._consecutive_losses = settings.ARB_CONSECUTIVE_LOSS_HALT
    assert engine._cb_triggered() is True


def test_circuit_breaker_clear_when_under_thresholds():
    engine = ArbEngine(exchange_clients={"bitget": MagicMock(), "kraken": MagicMock()},
                       sim_mode=True)
    engine._daily_pnl_usd = -1.0
    engine._consecutive_losses = 1
    assert engine._cb_triggered() is False


# ─────────────────────────────────────────────────────────────────────────
# Sim mode slippage model
# ─────────────────────────────────────────────────────────────────────────

def test_sim_fills_apply_slippage_model():
    engine = ArbEngine(exchange_clients={"bitget": MagicMock(), "kraken": MagicMock()},
                       sim_mode=True)
    opp = _opp(buy=100.0, sell=100.5)
    buy_fill, sell_fill = engine._sim_fills(opp)
    # +/- 0.02%
    assert buy_fill  == pytest.approx(100.0 * 1.0002, abs=0.0001)
    assert sell_fill == pytest.approx(100.5 * 0.9998, abs=0.0001)


@pytest.mark.asyncio
async def test_execute_arb_uses_sim_fills_in_sim_mode():
    """In sim mode the engine never calls create_market_*_order."""
    a = MagicMock()
    a.create_market_buy_order  = AsyncMock(side_effect=AssertionError("should not be called"))
    a.fetch_order_book = AsyncMock()
    b = MagicMock()
    b.create_market_sell_order = AsyncMock(side_effect=AssertionError("should not be called"))
    b.fetch_order_book = AsyncMock()
    engine = ArbEngine(exchange_clients={"bitget": a, "kraken": b}, sim_mode=True)
    await engine._execute_arb(_opp(buy=100.0, sell=100.5))
    a.create_market_buy_order.assert_not_called()
    b.create_market_sell_order.assert_not_called()
    assert engine._total_trades == 1


# ─────────────────────────────────────────────────────────────────────────
# Extensibility — new exchange in fee map auto-picks-up
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_exchange_in_fee_map_evaluated(monkeypatch):
    """Adding a new exchange to ARB_FEE_MAP makes opportunities involving
    it eligible — engine code itself doesn't change."""
    monkeypatch.setattr(settings, "ARB_WATCH_PAIRS", ["BTC/USDT"])
    monkeypatch.setattr(settings, "ARB_FEE_MAP", {
        **settings.ARB_FEE_MAP,
        "newvenue": 0.0005,    # 0.05% fee — newly added
    })
    a = _mock_exchange(asks=[[100.00, 50.0]] * 3, bids=[[99.99, 50.0]] * 3)
    new = _mock_exchange(asks=[[101.0, 50.0]] * 3, bids=[[100.9, 50.0]] * 3)
    engine = ArbEngine(exchange_clients={"bitget": a, "newvenue": new}, sim_mode=True)
    opp = await engine._find_best_opportunity()
    assert opp is not None
    # The new venue must appear in the chosen route
    assert "newvenue" in (opp.buy_exchange, opp.sell_exchange)


# ─────────────────────────────────────────────────────────────────────────
# Dashboard hook
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dashboard_add_arb_called_on_completed_trade():
    a = MagicMock()
    a.fetch_order_book = AsyncMock()
    b = MagicMock()
    b.fetch_order_book = AsyncMock()
    dash = MagicMock()
    engine = ArbEngine(
        exchange_clients={"bitget": a, "kraken": b},
        dashboard=dash,
        sim_mode=True,
    )
    await engine._execute_arb(_opp(buy=100.0, sell=100.5))
    dash.add_arb.assert_called_once()
    kwargs = dash.add_arb.call_args.kwargs
    assert kwargs["pair"]    == "BTC/USDT"
    assert kwargs["buy_ex"]  == "bitget"
    assert kwargs["sell_ex"] == "kraken"


@pytest.mark.asyncio
async def test_dashboard_not_called_on_failed_trade(monkeypatch):
    """Failed arb (e.g. exchange error in live mode) must not push to dashboard."""
    a = MagicMock()
    a.create_market_buy_order = AsyncMock(side_effect=RuntimeError("api down"))
    b = MagicMock()
    b.create_market_sell_order = AsyncMock(return_value={"price": 100.5})
    dash = MagicMock()
    engine = ArbEngine(
        exchange_clients={"bitget": a, "kraken": b},
        dashboard=dash,
        sim_mode=False,
    )
    await engine._execute_arb(_opp(buy=100.0, sell=100.5))
    dash.add_arb.assert_not_called()
    # The failure didn't crash the engine
    assert engine._total_trades == 0
