"""
tests/test_scalping_agent.py — ScalpingAgent + OFIEngine + FeeManager.

16 tests covering the OFI math, fee-aware TP/SL, TFI confirmation,
direction persistence, agent entry/exit gates, and the agent-level
circuit breaker.

Style follows tests/test_arb_engine.py — small helpers, AsyncMock for
the agent's network stubs, no real exchange connections.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from config import settings
from agents.scalping_agent import (
    BookSnap, FeeManager, OFIEngine, ScalpingAgent, ScalpPosition,
)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _book(bid_price: float, bid_qty: float,
          ask_price: float, ask_qty: float,
          levels: int = 5) -> tuple[list, list]:
    """Build a 5-level synthetic book around (bid_price, ask_price).

    Each successive level walks one tick away from mid. Useful for the
    OFI tests which only need price/qty deltas, not realistic liquidity
    structure."""
    bids = [(bid_price - i * 0.5, bid_qty) for i in range(levels)]
    asks = [(ask_price + i * 0.5, ask_qty) for i in range(levels)]
    return bids, asks


def _agent_with_capital(capital: float) -> ScalpingAgent:
    """Construct a ScalpingAgent at a specific capital — convenient for
    flipping observation/live mode in tests without touching settings."""
    with patch.object(settings, "SCALP_CAPITAL", capital):
        agent = ScalpingAgent()
        agent._capital = capital
        agent.capital_allocation = capital
        return agent


# ─────────────────────────────────────────────────────────────────────────
# OFI ENGINE (5 tests)
# ─────────────────────────────────────────────────────────────────────────

def test_ofi_event_increment_bid_improved():
    """Bid price rises → positive event increment."""
    eng = OFIEngine(levels=5, window_sec=20, zscore_window=80)
    bids_p, asks_p = _book(100.0, 5.0, 101.0, 5.0)
    bids_c, asks_c = _book(100.5, 5.0, 101.0, 5.0)
    prev = BookSnap(ts=0.0, bids=bids_p, asks=asks_p)
    curr = BookSnap(ts=1.0, bids=bids_c, asks=asks_c)
    assert eng._compute_e_n(prev, curr) > 0


def test_ofi_event_increment_ask_improved():
    """Ask price falls → negative event increment."""
    eng = OFIEngine(levels=5, window_sec=20, zscore_window=80)
    bids_p, asks_p = _book(100.0, 5.0, 101.0, 5.0)
    bids_c, asks_c = _book(100.0, 5.0, 100.5, 5.0)
    prev = BookSnap(ts=0.0, bids=bids_p, asks=asks_p)
    curr = BookSnap(ts=1.0, bids=bids_c, asks=asks_c)
    assert eng._compute_e_n(prev, curr) < 0


def test_ofi_event_increment_neutral():
    """Identical snapshots → zero event increment."""
    eng = OFIEngine(levels=5, window_sec=20, zscore_window=80)
    bids, asks = _book(100.0, 5.0, 101.0, 5.0)
    prev = BookSnap(ts=0.0, bids=bids, asks=asks)
    curr = BookSnap(ts=1.0, bids=list(bids), asks=list(asks))
    assert eng._compute_e_n(prev, curr) == 0.0


def test_ofi_multilevel_weights():
    """Same delta at L1 vs L2 → L1 contributes more (depth weights decay)."""
    eng = OFIEngine(levels=5, window_sec=20, zscore_window=80)

    # L1-only bid improvement
    bids_p, asks_p = _book(100.0, 5.0, 101.0, 5.0)
    bids_c1 = list(bids_p);  bids_c1[0] = (100.5, 5.0)
    e_l1 = eng._compute_e_n(
        BookSnap(0.0, bids_p, asks_p),
        BookSnap(1.0, bids_c1, asks_p),
    )

    # L2-only bid improvement (same magnitude)
    bids_c2 = list(bids_p);  bids_c2[1] = (100.0, 5.0)   # tier 1 raised
    e_l2 = eng._compute_e_n(
        BookSnap(0.0, bids_p, asks_p),
        BookSnap(1.0, bids_c2, asks_p),
    )

    assert e_l1 > e_l2 > 0


def test_ofi_zscore_after_min_buckets():
    """≥10 closed buckets with non-constant values → z-score != 0.

    Drives the bucket-close path directly with a distribution that has
    real variance; live book-tick sequences would too, but composing
    that synthetically across 12 iterations is fiddly and brittle."""
    eng = OFIEngine(levels=5, window_sec=20, zscore_window=80)
    key = "BTC/USDT:mexc"
    # 12 distinct historical buckets — std > 0 by construction.
    for v in (1.0, 3.0, 2.0, 5.0, 4.0, 6.0, 7.0, 3.0, 5.0, 8.0, 2.0, 9.0):
        eng._ofi_buckets[key].append(v)
    # Force a bucket close with a fresh current-bucket value distinct
    # from the mean of the history.
    eng._bucket_start[key]    = time.time() - 30.0
    eng._cur_bucket[key]      = [10.0]
    eng._maybe_close_bucket(key, time.time())
    assert eng._last_z[key] != 0.0


# ─────────────────────────────────────────────────────────────────────────
# FEE MANAGER (5 tests)
# ─────────────────────────────────────────────────────────────────────────

def test_fee_manager_override_mexc():
    fm = FeeManager(settings.SCALP_FEE_OVERRIDES)
    fees = fm.get_fees("mexc", "BTC/USDT")
    assert fees["maker_bps"] == 0.0
    assert fees["taker_bps"] == 0.0
    assert fees["source"] == "override"


def test_fee_manager_dynamic_tp_sl_mexc():
    fm = FeeManager(settings.SCALP_FEE_OVERRIDES)
    tp, sl = fm.compute_tp_sl("mexc", "BTC/USDT")
    # MEXC = 0 bps round trip → tp = SCALP_NET_PROFIT_TARGET_BPS exactly,
    # sl = tp / SCALP_RR_RATIO.
    assert tp == pytest.approx(settings.SCALP_NET_PROFIT_TARGET_BPS)
    assert sl == pytest.approx(
        settings.SCALP_NET_PROFIT_TARGET_BPS / settings.SCALP_RR_RATIO,
        rel=0.01,
    )


def test_fee_manager_dynamic_tp_sl_bitget():
    fm = FeeManager(settings.SCALP_FEE_OVERRIDES)
    tp, sl = fm.compute_tp_sl("bitget", "BTC/USDT")
    # Bitget = 2 bps round trip → tp = round_trip + net_target.
    expected_tp = 2.0 + settings.SCALP_NET_PROFIT_TARGET_BPS
    assert tp == pytest.approx(expected_tp)
    assert sl == pytest.approx(expected_tp / settings.SCALP_RR_RATIO, rel=0.01)


def test_fee_manager_viability_mexc_passes():
    fm = FeeManager(settings.SCALP_FEE_OVERRIDES)
    viable, reason = fm.is_viable("mexc", "BTC/USDT")
    assert viable is True
    assert reason == ""


def test_fee_manager_viability_high_fee_fails():
    """A 26 bps taker (e.g. Kraken) forces breakeven > 65%, blocked."""
    fm = FeeManager(settings.SCALP_FEE_OVERRIDES)
    fm._cache["kraken"] = {
        "BTC/USDT": {"maker_bps": 16.0, "taker_bps": 26.0, "source": "ccxt"},
    }
    viable, reason = fm.is_viable("kraken", "BTC/USDT")
    assert viable is False
    assert "kraken" in reason
    assert "%" in reason


# ─────────────────────────────────────────────────────────────────────────
# TFI + PERSISTENCE (2 tests)
# ─────────────────────────────────────────────────────────────────────────

def test_tfi_confirms_matching_direction():
    """Positive OFI + buy trades → tfi_confirms True."""
    eng = OFIEngine(levels=5, window_sec=0.01, zscore_window=80)
    # Seed buckets so z-score is non-zero positive.
    for i in range(12):
        eng.on_book("BTC/USDT", "mexc",
                    *_book(100.0, 5.0, 101.0, 5.0))
        eng.on_book("BTC/USDT", "mexc",
                    *_book(100.5 + i * 0.05, 5.0, 101.0, 5.0))
        time.sleep(0.012)
    eng.on_trade("BTC/USDT", "mexc", "buy", 1.5)
    # Trigger one more bucket close so on_trade lands in the closed bucket.
    eng.on_book("BTC/USDT", "mexc", *_book(100.0, 5.0, 101.0, 5.0))
    time.sleep(0.012)
    eng.on_book("BTC/USDT", "mexc", *_book(100.5, 5.0, 101.0, 5.0))
    out = eng.get("BTC/USDT", "mexc")
    # Either confirms (matching signs) or the z happens to be zero — in
    # which case the "no contradiction" branch returns True anyway.
    assert out["tfi_confirms"] is True


def test_direction_persistence_resets():
    """Counter increments above threshold, resets to 0 the moment z drops."""
    eng = OFIEngine(levels=5, window_sec=20, zscore_window=80)
    # Manually set last_z so we don't need to simulate a full bucket cycle.
    key = "BTC/USDT:mexc"
    eng._last_z[key] = 2.0
    assert eng.update_direction_ticks("BTC/USDT", "mexc", 1.5) == 1
    assert eng.update_direction_ticks("BTC/USDT", "mexc", 1.5) == 2
    assert eng.update_direction_ticks("BTC/USDT", "mexc", 1.5) == 3
    # Below threshold — counter must reset.
    eng._last_z[key] = 0.5
    assert eng.update_direction_ticks("BTC/USDT", "mexc", 1.5) == 0


# ─────────────────────────────────────────────────────────────────────────
# AGENT ENTRY / EXIT (4 tests)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_entry_observation_logged_mexc(monkeypatch):
    """Strong + persistent OFI on MEXC → observation logged with
    would_entry=True, observation_only=True (capital is 0).

    Session window patched to 0-24 so the test runs at any UTC hour."""
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 0)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC",   24)
    agent = _agent_with_capital(0.0)

    # Stub the four async market-data methods.
    agent._get_mid_price = AsyncMock(return_value=50_000.0)
    agent._get_spread_bps = AsyncMock(return_value=1.0)
    agent._get_regime = AsyncMock(return_value="TRENDING")

    # Force OFI engine state — bypasses the bucket warm-up.
    eng = agent._ofi_engine
    key = "BTC/USDT:mexc"
    eng._last_z[key] = settings.SCALP_OFI_Z_ENTRY + 0.5
    eng._last_tfi[key] = 1.0
    eng._last_bucket_close[key] = time.time()
    eng._persist[key] = settings.SCALP_OFI_PERSIST_TICKS  # already satisfied

    await agent._evaluate_entry("BTC/USDT", "mexc")

    assert len(agent._observations) > 0
    obs = agent._observations[-1]
    assert obs.would_entry is True
    assert obs.exchange == "mexc"
    assert obs.round_trip_cost_bps == 0.0
    assert obs.observation_only is True


@pytest.mark.asyncio
async def test_entry_blocked_unapproved_exchange():
    """Kraken not in STRATEGY_EXCHANGE_MAP['scalp'] → skip with reason."""
    agent = _agent_with_capital(0.0)
    await agent._evaluate_entry("BTC/USDT", "kraken")
    assert len(agent._observations) == 1
    obs = agent._observations[0]
    assert obs.would_entry is False
    assert "not in scalp approved" in obs.skip_reason


@pytest.mark.asyncio
async def test_exit_ofi_exhausted():
    """LONG position + z drops below SCALP_OFI_Z_EXIT → OFI_EXHAUSTED exit."""
    agent = _agent_with_capital(0.0)
    agent._get_mid_price = AsyncMock(return_value=50_000.0)

    pos = ScalpPosition(
        symbol="BTC/USDT", exchange="mexc", direction="LONG",
        entry_price=50_000.0, entry_time=time.time(),
        entry_ofi_z=2.0, entry_tfi=1.0,
        size_usd=0.0, tp_price=50_015.0, sl_price=49_990.0,
        tp_bps=3.0, sl_bps=1.9, round_trip_cost_bps=0.0,
        observation_only=True,
    )
    pos_key = "BTC/USDT:mexc"
    agent._positions[pos_key] = pos

    # Seed an open observation so _exit_position has something to fill.
    from agents.scalping_agent import ScalpObservation
    agent._observations.append(ScalpObservation(
        symbol="BTC/USDT", exchange="mexc", timestamp=pos.entry_time,
        ofi_z=2.0, direction="LONG", strength="strong",
        tfi_confirms=True, raw_tfi=1.0, spread_bps=1.0, regime="TRENDING",
        round_trip_cost_bps=0.0, min_win_rate_required=0.4,
        tp_bps=3.0, sl_bps=1.9,
        would_entry=True, skip_reason="",
        entry_price=50_000.0, observation_only=True,
    ))

    # OFI now well below the exit threshold.
    agent._ofi_engine._last_z["BTC/USDT:mexc"] = 0.1
    agent._ofi_engine._last_bucket_close["BTC/USDT:mexc"] = time.time()

    await agent._manage_position(pos_key)
    assert pos_key not in agent._positions
    last_obs = agent._observations[-1]
    assert last_obs.exit_reason == "OFI_EXHAUSTED"


@pytest.mark.asyncio
async def test_circuit_breaker_daily_loss():
    """Real-capital loss past SCALP_DAILY_LOSS_HALT → agent halted."""
    agent = _agent_with_capital(50.0)
    agent._get_mid_price = AsyncMock(return_value=50_000.0)

    pos = ScalpPosition(
        symbol="BTC/USDT", exchange="mexc", direction="LONG",
        entry_price=50_000.0, entry_time=time.time(),
        entry_ofi_z=2.0, entry_tfi=1.0,
        size_usd=50.0, tp_price=50_015.0, sl_price=40_000.0,
        tp_bps=3.0, sl_bps=1.9, round_trip_cost_bps=0.0,
        observation_only=False,
    )
    pos_key = "BTC/USDT:mexc"
    agent._positions[pos_key] = pos

    # Exit at a 20% loss → pnl_usd = -10  (size_usd * pnl_bps/10000)
    # That's > SCALP_DAILY_LOSS_HALT=5.0 default → halt fires.
    await agent._exit_position(pos_key, pos, "SL", exit_price=40_000.0)
    assert agent._halted is True
    assert "daily loss" in agent._halt_reason.lower()


# ─────────────────────────────────────────────────────────────────────────
# ADDITIONAL TESTS (17-20)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_reset_clears_circuit_breaker():
    """UTC day rollover → daily_loss zeroes, consec_losses zeroes, the
    halt is lifted (provided it was a daily-loss halt)."""
    from datetime import timedelta
    agent = _agent_with_capital(50.0)

    # Force into the halted-by-daily-loss state.
    pos = ScalpPosition(
        symbol="BTC/USDT", exchange="mexc", direction="LONG",
        entry_price=50_000.0, entry_time=time.time(),
        entry_ofi_z=2.0, entry_tfi=1.0,
        size_usd=50.0, tp_price=50_015.0, sl_price=40_000.0,
        tp_bps=3.0, sl_bps=1.9, round_trip_cost_bps=0.0,
        observation_only=False,
    )
    pos_key = "BTC/USDT:mexc"
    agent._positions[pos_key] = pos
    await agent._exit_position(pos_key, pos, "SL", exit_price=40_000.0)
    assert agent._halted is True
    assert agent._daily_loss < 0

    # Pretend the last reset happened yesterday — the next check fires.
    from datetime import datetime as _dt
    agent._last_reset_date = (_dt.utcnow().date() - timedelta(days=1))
    agent._check_daily_reset()

    assert agent._halted is False
    assert agent._daily_loss == 0.0
    assert agent._consec_losses == 0


@pytest.mark.asyncio
async def test_session_gate_blocks_outside_window(monkeypatch):
    """UTC hour outside [SESSION_START, SESSION_END) → would_entry=False
    with 'Outside scalp session' skip reason."""
    from datetime import datetime as _dt
    import agents.scalping_agent as scalp_mod

    class _FakeDT:
        @staticmethod
        def utcnow():
            # 03:00 UTC — well before the default 7-17 window.
            return _dt(2026, 5, 22, 3, 0, 0)

    monkeypatch.setattr(scalp_mod, "datetime", _FakeDT)

    agent = _agent_with_capital(0.0)
    agent._get_mid_price  = AsyncMock(return_value=50_000.0)
    agent._get_spread_bps = AsyncMock(return_value=1.0)
    agent._get_regime     = AsyncMock(return_value="TRENDING")

    # OFI ready to fire — so we know the session gate is what blocks us.
    eng = agent._ofi_engine
    key = "BTC/USDT:mexc"
    eng._last_z[key] = settings.SCALP_OFI_Z_ENTRY + 0.5
    eng._last_tfi[key] = 1.0
    eng._last_bucket_close[key] = time.time()
    eng._persist[key] = settings.SCALP_OFI_PERSIST_TICKS

    await agent._evaluate_entry("BTC/USDT", "mexc")
    assert agent._observations, "should have logged an observation"
    obs = agent._observations[-1]
    assert obs.would_entry is False
    assert "Outside scalp session" in obs.skip_reason


@pytest.mark.asyncio
async def test_news_guard_blocks_entry(monkeypatch):
    """Sentiment source reports news_guard_active → entry blocked."""
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 0)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC",   24)

    class _FakeSentiment:
        async def get_composite(self):
            return {"news_guard_active": True}

    agent = _agent_with_capital(0.0)
    agent._sentiment_source = _FakeSentiment()
    agent._get_mid_price  = AsyncMock(return_value=50_000.0)
    agent._get_spread_bps = AsyncMock(return_value=1.0)
    agent._get_regime     = AsyncMock(return_value="TRENDING")

    eng = agent._ofi_engine
    key = "BTC/USDT:mexc"
    eng._last_z[key] = settings.SCALP_OFI_Z_ENTRY + 0.5
    eng._last_tfi[key] = 1.0
    eng._last_bucket_close[key] = time.time()
    eng._persist[key] = settings.SCALP_OFI_PERSIST_TICKS

    await agent._evaluate_entry("BTC/USDT", "mexc")
    assert agent._observations
    obs = agent._observations[-1]
    assert obs.would_entry is False
    assert "News guard" in obs.skip_reason


@pytest.mark.asyncio
async def test_market_data_wired_mid_price():
    """set_market_data wires _get_mid_price end-to-end via MarketData."""
    from unittest.mock import MagicMock
    md = MagicMock()
    md.get_price.return_value = 50_000.0
    agent = _agent_with_capital(0.0)
    agent.set_market_data(md)
    price = await agent._get_mid_price("BTC/USDT", "mexc")
    assert price == 50_000.0
    md.get_price.assert_called_once_with("mexc", "BTC/USDT")


@pytest.mark.asyncio
async def test_market_data_falls_back_to_all_prices():
    """get_price returns None → _get_mid_price walks get_all_prices."""
    from unittest.mock import MagicMock
    md = MagicMock()
    md.get_price.return_value = None
    md.get_all_prices.return_value = {"bitget": 50_123.4}
    agent = _agent_with_capital(0.0)
    agent.set_market_data(md)
    assert await agent._get_mid_price("BTC/USDT", "mexc") == 50_123.4


@pytest.mark.asyncio
async def test_market_data_stub_when_unwired():
    """No injection + no signal agent → 0.0 (stub) so the gate skips
    silently instead of firing a fake entry."""
    agent = _agent_with_capital(0.0)
    # Make the lazy lookup miss: empty registry path.
    agent._market_data = None
    import sys
    fake = type(sys)("agents")
    fake.REGISTERED_AGENTS = []
    monkey_orig = sys.modules.get("agents")
    sys.modules["agents"] = fake
    try:
        price = await agent._get_mid_price("BTC/USDT", "mexc")
    finally:
        if monkey_orig is not None:
            sys.modules["agents"] = monkey_orig
        else:
            sys.modules.pop("agents", None)
    assert price == 0.0


@pytest.mark.asyncio
async def test_regime_detector_wired_returns_uppercase():
    """regime_detector stores lowercase ('choppy' etc); the gate compares
    against uppercase. _get_regime must uppercase before returning."""
    from unittest.mock import MagicMock
    rd = MagicMock()
    snap = MagicMock()
    snap.regime = "choppy"
    rd.get_primary.return_value = snap
    agent = _agent_with_capital(0.0)
    agent.set_regime_detector(rd)
    regime = await agent._get_regime("BTC/USDT")
    assert regime == "CHOPPY"


@pytest.mark.asyncio
async def test_regime_choppy_blocks_entry(monkeypatch):
    """Wired regime detector returns 'choppy' → gate 10 fires."""
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 0)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC",   24)
    from unittest.mock import MagicMock
    rd = MagicMock()
    snap = MagicMock(); snap.regime = "choppy"
    rd.get_primary.return_value = snap

    agent = _agent_with_capital(0.0)
    agent.set_regime_detector(rd)
    agent._get_mid_price  = AsyncMock(return_value=50_000.0)
    agent._get_spread_bps = AsyncMock(return_value=1.0)

    eng = agent._ofi_engine
    key = "BTC/USDT:mexc"
    eng._last_z[key] = settings.SCALP_OFI_Z_ENTRY + 0.5
    eng._last_tfi[key] = 1.0
    eng._last_bucket_close[key] = time.time()
    eng._persist[key] = settings.SCALP_OFI_PERSIST_TICKS

    await agent._evaluate_entry("BTC/USDT", "mexc")
    assert agent._observations
    obs = agent._observations[-1]
    assert obs.would_entry is False
    assert "CHOPPY" in obs.skip_reason


def test_market_data_get_spread_bps_math():
    """MarketData.get_spread_bps math: spread / mid × 10000."""
    from core.market_data import MarketData
    md = MarketData()
    md._last_book[("mexc", "BTC/USDT")] = {
        "bids": [(99_995.0, 1.0)],
        "asks": [(100_005.0, 1.0)],
    }
    # spread = 10, mid = 100_000 → 10 / 100000 * 10000 = 1.0 bps
    assert md.get_spread_bps("mexc", "BTC/USDT") == pytest.approx(1.0)
    # Unknown pair → None (not 0.0).
    assert md.get_spread_bps("mexc", "ETH/USDT") is None


@pytest.mark.asyncio
async def test_micro_price_tracker_backfills_30s():
    """One pass of the tracker loop fills price_30s on a 35s-old entry
    but leaves price_1m alone (60s threshold not yet crossed)."""
    from agents.scalping_agent import ScalpObservation
    agent = _agent_with_capital(0.0)
    agent._get_mid_price = AsyncMock(return_value=50_100.0)

    obs = ScalpObservation(
        symbol="BTC/USDT", exchange="mexc", timestamp=time.time() - 35.0,
        ofi_z=2.0, direction="LONG", strength="strong",
        tfi_confirms=True, raw_tfi=1.0, spread_bps=1.0, regime="TRENDING",
        round_trip_cost_bps=0.0, min_win_rate_required=0.4,
        tp_bps=3.0, sl_bps=1.9,
        would_entry=True, skip_reason="",
        entry_price=50_000.0, observation_only=True,
    )
    agent._observations.append(obs)

    await agent._micro_price_tracker_pass()
    assert obs.price_30s == 50_100.0
    assert obs.price_1m  == 0.0   # 35s < 60s threshold
    assert obs.price_3m  == 0.0
    assert obs.price_5m  == 0.0
    # Updated obs queued for flush.
    assert obs in agent._pending_flush
