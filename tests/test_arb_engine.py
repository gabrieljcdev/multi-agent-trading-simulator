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
    FundingRateArbEngine,
    gross_gap_pct, net_gap_pct, min_gap_threshold, slippage_pct,
)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

# Rich-enough free balance to pass the pre-execution capital gate for
# every test that doesn't deliberately stress the gate. Quote and base
# currencies share the same dict — fetch_balance.get('free').get(ccy)
# returns this number for any ccy the test passes through.
_AMPLE_BALANCE = {
    "free": {"USDT": 1_000_000.0, "BTC": 1_000.0, "ETH": 1_000.0,
             "SOL": 1_000.0, "BNB": 1_000.0},
}


def _mock_exchange(
    asks: list,
    bids: list,
    buy_fill_price: float = None,
    sell_fill_price: float = None,
    free_balance: dict | None = None,
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
    ex.fetch_balance = AsyncMock(
        return_value=free_balance if free_balance is not None else _AMPLE_BALANCE,
    )
    ex.close = AsyncMock()
    return ex


def _opp(symbol="BTC/USDT", buy_ex="bitget", sell_ex="kraken",
         buy=100.0, sell=100.4, net=0.20, size=10.0,
         spread_buy_pct=0.02, spread_sell_pct=0.02,
         depth_buy_usd=10.0, depth_sell_usd=10.0) -> ArbOpportunity:
    """Default spread + depth values produce slippage = 0.02% (the legacy
    flat-model number) so legacy assertions still work where applicable.
    Tests that exercise the depth-aware model override these explicitly."""
    return ArbOpportunity(
        symbol=symbol, buy_exchange=buy_ex, sell_exchange=sell_ex,
        buy_price=buy, sell_price=sell,
        gross_gap_pct=(sell - buy) / buy * 100,
        net_gap_pct=net, max_size_usd=size, detected_at=time.monotonic(),
        spread_buy_pct=spread_buy_pct, spread_sell_pct=spread_sell_pct,
        depth_buy_usd=depth_buy_usd, depth_sell_usd=depth_sell_usd,
    )


def _with_balance(ex: MagicMock, free: dict | None = None) -> MagicMock:
    """Attach a fetch_balance AsyncMock to an exchange built without the
    _mock_exchange helper (manual MagicMock construction)."""
    ex.fetch_balance = AsyncMock(
        return_value=free if free is not None else _AMPLE_BALANCE,
    )
    return ex


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    """Never touch SQLite during arb-engine tests.

    log_arb_trade returns an int (the new row id) in production; the
    stub returns 1 so callers chaining mark_arb_opportunity_executed
    on the id still work.
    """
    monkeypatch.setattr(
        "execution.arb_engine.db_queries.log_arb_trade",
        lambda *a, **k: 1,
    )
    monkeypatch.setattr(
        "execution.arb_engine.db_queries.log_circuit_breaker",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "execution.arb_engine.db_queries.log_arb_opportunity",
        lambda *a, **k: 1,
    )
    monkeypatch.setattr(
        "execution.arb_engine.db_queries.mark_arb_opportunity_executed",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "execution.arb_engine.db_queries.log_arb_balance_fail",
        lambda *a, **k: 2,
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
    _with_balance(a)
    b = MagicMock()
    b.create_market_sell_order = slow_sell
    _with_balance(b)

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
    _with_balance(a)
    b = MagicMock()
    b.create_market_sell_order = slow_sell
    _with_balance(b)

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
    # %-based: halt fires when daily P&L falls below -(PCT/100)*alloc.
    # Pick a concrete alloc so the threshold is deterministic.
    engine._capital_allocation = 500.0
    halt_usd = (settings.ARB_DAILY_LOSS_HALT_PCT / 100.0) * engine._capital_allocation
    engine._daily_pnl_usd = -halt_usd - 0.01
    assert engine._cb_triggered() is True


def test_circuit_breaker_halts_on_consecutive_losses():
    engine = ArbEngine(exchange_clients={"bitget": MagicMock(), "kraken": MagicMock()},
                       sim_mode=True)
    engine._consecutive_losses = settings.ARB_CONSECUTIVE_LOSS_HALT
    assert engine._cb_triggered() is True


def test_circuit_breaker_clear_when_under_thresholds():
    engine = ArbEngine(exchange_clients={"bitget": MagicMock(), "kraken": MagicMock()},
                       sim_mode=True)
    engine._capital_allocation = 500.0
    engine._daily_pnl_usd = -1.0
    engine._consecutive_losses = 1
    assert engine._cb_triggered() is False


# ─────────────────────────────────────────────────────────────────────────
# Sim mode slippage model
# ─────────────────────────────────────────────────────────────────────────

def test_sim_fills_apply_slippage_model():
    """With the default _opp spread=0.02% / depth=$10 / size=$10 the
    depth-aware model collapses to 0.02% per leg — same number the
    legacy flat model produced. Dedicated tests below stretch the
    model with varying size and depth."""
    engine = ArbEngine(exchange_clients={"bitget": MagicMock(), "kraken": MagicMock()},
                       sim_mode=True)
    opp = _opp(buy=100.0, sell=100.5)
    buy_fill, sell_fill, slip_buy, slip_sell = engine._sim_fills(opp)
    assert buy_fill  == pytest.approx(100.0 * 1.0002, abs=0.0001)
    assert sell_fill == pytest.approx(100.5 * 0.9998, abs=0.0001)
    assert slip_buy  == pytest.approx(0.02, abs=0.0001)
    assert slip_sell == pytest.approx(0.02, abs=0.0001)


@pytest.mark.asyncio
async def test_execute_arb_uses_sim_fills_in_sim_mode():
    """In sim mode the engine never calls create_market_*_order."""
    a = MagicMock()
    a.create_market_buy_order  = AsyncMock(side_effect=AssertionError("should not be called"))
    a.fetch_order_book = AsyncMock()
    _with_balance(a)
    b = MagicMock()
    b.create_market_sell_order = AsyncMock(side_effect=AssertionError("should not be called"))
    b.fetch_order_book = AsyncMock()
    _with_balance(b)
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
    _with_balance(a)
    b = MagicMock()
    b.fetch_order_book = AsyncMock()
    _with_balance(b)
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
    _with_balance(a)
    b = MagicMock()
    b.create_market_sell_order = AsyncMock(return_value={"price": 100.5})
    _with_balance(b)
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


# ─────────────────────────────────────────────────────────────────────────
# Pre-execution capital gate (balance verification)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_balance_check_blocks_execution_when_insufficient_funds():
    """The buy-side has $1 — far below the size × buffer requirement —
    so _execute_arb returns before any order leg fires."""
    a = MagicMock()
    a.create_market_buy_order = AsyncMock(side_effect=AssertionError("must not fire"))
    _with_balance(a, {"free": {"USDT": 1.0}})           # buy side: not enough quote
    b = MagicMock()
    b.create_market_sell_order = AsyncMock(side_effect=AssertionError("must not fire"))
    _with_balance(b)                                    # sell side: ample

    engine = ArbEngine(
        exchange_clients={"bitget": a, "kraken": b},
        sim_mode=False,
    )
    opp = _opp(buy=100.0, sell=100.5, size=100.0)
    await engine._execute_arb(opp)

    a.create_market_buy_order.assert_not_called()
    b.create_market_sell_order.assert_not_called()
    assert engine._total_trades == 0
    assert engine.missed_balance_checks == 1


@pytest.mark.asyncio
async def test_balance_check_logs_miss_to_db(monkeypatch):
    """The balance gate must persist the miss via log_arb_balance_fail
    with status='balance_fail'."""
    captured = {}

    def fake_log(symbol, buy_exchange, sell_exchange, detail, sim_mode):
        captured.update(symbol=symbol, buy_exchange=buy_exchange,
                        sell_exchange=sell_exchange, detail=detail,
                        sim_mode=sim_mode)
        return 99

    monkeypatch.setattr(
        "execution.arb_engine.db_queries.log_arb_balance_fail",
        fake_log,
    )

    a = _with_balance(MagicMock(), {"free": {"USDT": 0.5}})
    b = _with_balance(MagicMock())
    engine = ArbEngine(
        exchange_clients={"bitget": a, "kraken": b},
        sim_mode=False,
    )
    await engine._execute_arb(_opp(buy=100.0, sell=100.5, size=50.0))

    assert captured["symbol"]        == "BTC/USDT"
    assert captured["buy_exchange"]  == "bitget"
    assert captured["sell_exchange"] == "kraken"
    assert "USDT" in (captured["detail"] or "")
    assert engine.missed_balance_checks == 1


# ─────────────────────────────────────────────────────────────────────────
# Depth-aware slippage model
# ─────────────────────────────────────────────────────────────────────────

def test_slippage_model_increases_with_position_size():
    """slip = spread × sqrt(size/depth); 4× the size = 2× the slippage.
    Inputs chosen so both slippages live INSIDE the [MIN, MAX] clamp."""
    base_spread = 0.20    # 0.2% — comfortably above the 0.01% MIN
    depth_usd   = 100.0
    slip_small = slippage_pct(base_spread, size_usd=25.0,  depth_usd=depth_usd)
    slip_big   = slippage_pct(base_spread, size_usd=100.0, depth_usd=depth_usd)
    assert settings.ARB_SLIPPAGE_MIN_PCT < slip_small < settings.ARB_SLIPPAGE_MAX_PCT
    assert settings.ARB_SLIPPAGE_MIN_PCT < slip_big   < settings.ARB_SLIPPAGE_MAX_PCT
    assert slip_big > slip_small
    # 4× size → 2× sqrt → 2× slippage (both stay inside the clamp).
    assert slip_big == pytest.approx(slip_small * 2.0, rel=0.01)


def test_slippage_model_clamps_to_min_max():
    """Tiny inputs clamp UP to MIN; massive size/spread clamps DOWN to MAX."""
    # Below-min: nearly-zero spread or near-zero size → MIN.
    assert slippage_pct(0.0,  size_usd=10.0, depth_usd=1_000.0) == settings.ARB_SLIPPAGE_MIN_PCT
    assert slippage_pct(0.05, size_usd=0.0,  depth_usd=1_000.0) == settings.ARB_SLIPPAGE_MIN_PCT
    # Above-max: huge spread + size > depth → MAX.
    assert slippage_pct(5.0,  size_usd=10_000.0, depth_usd=10.0) == settings.ARB_SLIPPAGE_MAX_PCT


# ─────────────────────────────────────────────────────────────────────────
# Dynamic position sizing
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dynamic_sizing_scales_with_gap_width(monkeypatch):
    """A gap 2× the threshold yields a position 2× the base — provided
    the depth cap leaves room. We engineer a deep book so depth doesn't
    bind."""
    monkeypatch.setattr(settings, "ARB_WATCH_PAIRS", ["BTC/USDT"])
    monkeypatch.setattr(settings, "ARB_CAPITAL_PER_EXCHANGE", 1_000_000.0)

    # Deep book on both sides so 10% of depth ≫ dynamic size.
    a = _mock_exchange(asks=[[100.00, 1_000.0]] * 3, bids=[[ 99.99, 1_000.0]] * 3)
    # Build the sell side so the net gap is roughly 2× the bitget
    # threshold after fees. bitget fee 0.0001 + kraken fee 0.0026 = 0.27pct
    # → net = gross − 0.27. For 2× threshold (≈0.06%), gross ≈ 0.33%.
    sell_price = 100.0 * (1 + 0.0033)
    b = _mock_exchange(
        asks=[[sell_price * 1.0001, 1_000.0]] * 3,
        bids=[[sell_price,           1_000.0]] * 3,
    )
    engine = ArbEngine(exchange_clients={"bitget": a, "kraken": b}, sim_mode=True)
    opp = await engine._find_best_opportunity()
    assert opp is not None

    threshold = min_gap_threshold(opp.buy_exchange, opp.sell_exchange)
    gap_ratio = opp.net_gap_pct / threshold
    # Dynamic size should equal base × min(gap_ratio, cap), capped
    # before any depth cap kicks in here (book is huge).
    expected_multiplier = min(gap_ratio, settings.ARB_SIZE_MULTIPLIER_CAP)
    expected_size = settings.ARB_BASE_POSITION_USD * expected_multiplier
    assert opp.max_size_usd == pytest.approx(expected_size, rel=0.01)
    assert opp.max_size_usd > settings.ARB_BASE_POSITION_USD


@pytest.mark.asyncio
async def test_dynamic_sizing_caps_at_multiplier_cap(monkeypatch):
    """A 10× threshold gap caps at ARB_SIZE_MULTIPLIER_CAP × base
    regardless of how wide the gap really was."""
    monkeypatch.setattr(settings, "ARB_WATCH_PAIRS", ["BTC/USDT"])
    monkeypatch.setattr(settings, "ARB_CAPITAL_PER_EXCHANGE", 1_000_000.0)

    a = _mock_exchange(asks=[[100.00, 1_000.0]] * 3, bids=[[ 99.99, 1_000.0]] * 3)
    # Massive gap — 5% gross, well above any multiplier-cap range.
    b = _mock_exchange(asks=[[105.01, 1_000.0]] * 3, bids=[[105.00, 1_000.0]] * 3)

    engine = ArbEngine(exchange_clients={"bitget": a, "kraken": b}, sim_mode=True)
    opp = await engine._find_best_opportunity()
    assert opp is not None
    cap_size = settings.ARB_BASE_POSITION_USD * settings.ARB_SIZE_MULTIPLIER_CAP
    assert opp.max_size_usd == pytest.approx(cap_size, rel=0.001)


# ─────────────────────────────────────────────────────────────────────────
# Opportunity logging
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_opportunity_log_records_unexecuted_gaps(monkeypatch):
    """A gap that clears liquidity but sits BELOW the execution threshold
    must still produce an arb_opportunities row, with above_threshold=False
    and executed=False."""
    monkeypatch.setattr(settings, "ARB_WATCH_PAIRS", ["BTC/USDT"])

    calls = []
    monkeypatch.setattr(
        "execution.arb_engine.db_queries.log_arb_opportunity",
        lambda **kw: (calls.append(kw), 7)[1],
    )
    # Tiny gap below threshold; depth comfortably above liquidity floor.
    a = _mock_exchange(asks=[[100.00, 50.0]] * 3, bids=[[99.99, 50.0]] * 3)
    b = _mock_exchange(asks=[[100.05, 50.0]] * 3, bids=[[100.02, 50.0]] * 3)
    engine = ArbEngine(exchange_clients={"bitget": a, "kraken": b}, sim_mode=True)
    opp = await engine._find_best_opportunity()
    assert opp is None
    # At least one unexecuted gap was logged
    sub_threshold = [c for c in calls if not c.get("above_threshold")]
    assert sub_threshold, "expected an unexecuted opportunity row"
    row = sub_threshold[0]
    assert row["symbol"] == "BTC/USDT"
    # executed / arb_trade_id default in the query signature; the engine
    # never overrides them on the initial write — log_opportunity passes
    # neither key, so the DB column stays at its default False / NULL.
    assert "executed" not in row
    assert "arb_trade_id" not in row


@pytest.mark.asyncio
async def test_opportunity_log_records_executed_gaps_with_trade_id(monkeypatch):
    """Executable gaps get logged with the resolved arb_trades id once
    the trade fires."""
    monkeypatch.setattr(settings, "ARB_WATCH_PAIRS", ["BTC/USDT"])

    monkeypatch.setattr(
        "execution.arb_engine.db_queries.log_arb_opportunity",
        lambda **kw: 42,
    )
    monkeypatch.setattr(
        "execution.arb_engine.db_queries.log_arb_trade",
        lambda *a, **k: 777,
    )
    executed_calls = []
    monkeypatch.setattr(
        "execution.arb_engine.db_queries.mark_arb_opportunity_executed",
        lambda opp_id, trade_id: executed_calls.append((opp_id, trade_id)),
    )

    a = _mock_exchange(asks=[[100.00, 50.0]] * 3, bids=[[99.99, 50.0]] * 3)
    b = _mock_exchange(asks=[[100.51, 50.0]] * 3, bids=[[100.50, 50.0]] * 3)
    engine = ArbEngine(exchange_clients={"bitget": a, "kraken": b}, sim_mode=True)
    opp = await engine._find_best_opportunity()
    assert opp is not None
    assert opp.opportunity_log_id == 42
    await engine._execute_arb(opp)
    assert executed_calls == [(42, 777)]


# ─────────────────────────────────────────────────────────────────────────
# FundingRateArbEngine — Coinglass-not-wired stub + circuit breaker
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_funding_arb_engine_pulls_from_data_sources(monkeypatch):
    """fetch_funding_rates must read from the data_sources aggregator —
    no in-engine stub. Patching the aggregator's get_funding_rates
    flows straight through to the engine's return value."""
    import data_sources as ds_mod

    expected = {"BTC/USDT": 0.0001, "ETH/USDT": 0.00025}
    monkeypatch.setattr(ds_mod.data_sources, "get_funding_rates",
                        lambda: dict(expected))

    engine = FundingRateArbEngine(sim_mode=True)
    out = await engine.fetch_funding_rates()
    assert out == expected


@pytest.mark.asyncio
async def test_funding_arb_engine_returns_empty_when_no_coinglass_data(monkeypatch):
    """When no source has cached a funding rate yet, the engine returns
    an empty dict instead of raising — the scan loop treats that as
    'nothing to do' on that tick."""
    import data_sources as ds_mod
    monkeypatch.setattr(ds_mod.data_sources, "get_funding_rates", lambda: {})
    engine = FundingRateArbEngine(sim_mode=True)
    out = await engine.fetch_funding_rates()
    assert out == {}


def test_funding_arb_circuit_breaker_halts_on_daily_loss(monkeypatch):
    """Independent thresholds — the funding engine's halt uses
    ARB_FUNDING_DAILY_LOSS_HALT_PCT against FUND_ARB_CAPITAL, distinct
    from ArbEngine's."""
    monkeypatch.setattr(settings, "FUND_ARB_CAPITAL", 500.0)
    engine = FundingRateArbEngine(sim_mode=True)
    halt_usd = (settings.ARB_FUNDING_DAILY_LOSS_HALT_PCT / 100.0) * 500.0
    engine._daily_pnl_usd = -halt_usd - 0.01
    assert engine._cb_triggered() is True
    engine._daily_pnl_usd = 0.0
    engine._consecutive_losses = settings.ARB_FUNDING_CONSECUTIVE_LOSS_HALT
    assert engine._cb_triggered() is True
    engine._consecutive_losses = 0
    assert engine._cb_triggered() is False


def test_funding_arb_circuit_breaker_zero_alloc_noop(monkeypatch):
    """FUND_ARB_CAPITAL == 0 → the daily-loss halt is a no-op; only the
    consecutive-loss halt can trip the breaker."""
    monkeypatch.setattr(settings, "FUND_ARB_CAPITAL", 0.0)
    engine = FundingRateArbEngine(sim_mode=True)
    engine._daily_pnl_usd = -1_000_000.0
    assert engine._cb_triggered() is False  # never halts on % rule with 0 alloc


# ─────────────────────────────────────────────────────────────────────────
# FundingRateArbEngine — de-island wiring (capital allocation + breaker)
# ─────────────────────────────────────────────────────────────────────────

def test_funding_arb_initial_allocation_from_fund_constant(monkeypatch):
    """Fresh engine reads its starting allocation from FUND_ARB_CAPITAL —
    mirrors ArbEngine.__init__'s wiring so BalanceAgent compounding can
    raise it from a known baseline."""
    monkeypatch.setattr(settings, "FUND_ARB_CAPITAL", 750.0)
    engine = FundingRateArbEngine(sim_mode=True)
    assert engine._capital_allocation == 750.0


def test_funding_arb_set_capital_allocation_changes_field():
    """The engine-only set_capital_allocation setter writes
    _capital_allocation directly — FundingRateArbEngine has no agent
    wrapper in REGISTERED_AGENTS, so the setter is exposed on the engine."""
    engine = FundingRateArbEngine(sim_mode=True)
    engine.set_capital_allocation(1234.0)
    assert engine._capital_allocation == 1234.0
    # Non-numeric is ignored (logger.debug, no raise).
    engine.set_capital_allocation("not-a-number")  # type: ignore[arg-type]
    assert engine._capital_allocation == 1234.0
    # Negative clamps to 0 — never let the breaker reason about a negative
    # denominator.
    engine.set_capital_allocation(-50.0)
    assert engine._capital_allocation == 0.0


def test_funding_arb_breaker_scales_with_live_allocation(monkeypatch):
    """Doubling the live allocation doubles the tolerated USD loss.

    The breaker reads _capital_allocation (set by the setter), NOT the
    settings constant — so a runtime allocation change takes effect on
    the very next tick.
    """
    monkeypatch.setattr(settings, "FUND_ARB_CAPITAL", 500.0)
    engine = FundingRateArbEngine(sim_mode=True)

    pct = settings.ARB_FUNDING_DAILY_LOSS_HALT_PCT / 100.0
    halt_at_500 = pct * 500.0
    # At allocation $500, a $halt_at_500 + 1¢ loss trips.
    engine._daily_pnl_usd = -(halt_at_500 + 0.01)
    assert engine._cb_triggered() is True

    # Double the allocation via the setter — the same loss is no longer
    # at the halt threshold; the % rule scales with allocation.
    engine.set_capital_allocation(1000.0)
    halt_at_1000 = pct * 1000.0
    assert engine._cb_triggered() is False, (
        f"daily_pnl={engine._daily_pnl_usd} should be inside the $1000-alloc band "
        f"(halt at -${halt_at_1000:.2f})"
    )
    # Push the loss to the new halt to confirm the breaker still trips.
    engine._daily_pnl_usd = -(halt_at_1000 + 0.01)
    assert engine._cb_triggered() is True


def test_funding_arb_breaker_zero_allocation_no_divide_by_zero():
    """A 0-allocation engine never halts on the % rule and never raises
    a ZeroDivisionError. Mirrors the existing ArbEngine zero-alloc guard."""
    engine = FundingRateArbEngine(sim_mode=True)
    engine.set_capital_allocation(0.0)
    engine._daily_pnl_usd = -1_000_000.0
    # No raise, no halt on % rule.
    assert engine._cb_triggered() is False


def test_funding_arb_fund_claim_visible_to_balance_agent(monkeypatch):
    """FundingRateArbEngine lives inside the 'arb' fund. The BalanceAgent
    sees its capital through the same InventoryState.effective_balance
    lookup the rest of the arb fund uses — no separate claim is registered
    for the funding engine (it's a sub-engine of the arb fund), so the
    arb fund's view of bybit is shared between ArbEngine and
    FundingRateArbEngine.

    Updated 2026-05-29 after the effective_balance under-subscribed fix:
    a single fund claim no longer strands the venue's slack. To prove
    "claim visibility" we register a competing fund's claim and check
    that arb's effective balance correctly excludes it.
    """
    from agents.balance.inventory_state import inventory_state
    inventory_state.reset()

    monkeypatch.setattr(settings, "EXCHANGE_BALANCES", {"bybit": 1000.0})
    # Apply an arb-fund claim on bybit — what BalanceAgent does after a
    # policy.compute_targets cycle.
    inventory_state.apply_allocation("arb", "bybit", "USDT", 400.0)

    # Under-subscribed: arb gets its claim PLUS the unclaimed slack.
    assert inventory_state.effective_balance("arb", "bybit", "USDT") == 1000.0

    # A competing fund's claim is what actually narrows arb's view —
    # this is the ring-fence guarantee the engine relies on.
    inventory_state.apply_allocation("signal", "bybit", "USDT", 300.0)
    # arb sees physical (1000) - signal's claim (300) = 700 (still its
    # own 400 + the 300 of remaining slack).
    assert inventory_state.effective_balance("arb", "bybit", "USDT") == 700.0

    # And the engine itself can carry that allocation on its own field too.
    engine = FundingRateArbEngine(sim_mode=True)
    engine.set_capital_allocation(400.0)
    assert engine._capital_allocation == 400.0

    inventory_state.reset()
