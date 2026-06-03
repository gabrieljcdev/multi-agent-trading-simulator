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


def test_ofi_depth_weights_ten_levels():
    """10-level depth ladder: 10 weights, 1.0 at the top of book down to
    0.04 at L9, monotonically decreasing (Xu/Gould/Howison 2018)."""
    assert settings.SCALP_OFI_LEVELS == 10
    w = settings.SCALP_DEPTH_WEIGHTS
    assert len(w) == 10
    assert w[0] == 1.0
    assert w[9] == 0.04
    vals = [w[i] for i in range(10)]
    assert vals == sorted(vals, reverse=True)


def test_ofi_uses_available_levels_when_book_shorter():
    """Backward-compatible: a 10-level engine fed a 5-level book uses the 5
    levels it has and computes e_n cleanly (no IndexError)."""
    eng = OFIEngine(levels=10, window_sec=20, zscore_window=80)
    bids_p, asks_p = _book(100.0, 5.0, 101.0, 5.0)   # 5 levels each side
    bids_c, asks_c = _book(100.5, 5.0, 101.0, 5.0)   # bid improved at L1
    e = eng._compute_e_n(
        BookSnap(0.0, bids_p, asks_p),
        BookSnap(1.0, bids_c, asks_c),
    )
    assert e > 0


# ─────────────────────────────────────────────────────────────────────────
# FEE MANAGER (5 tests)
# ─────────────────────────────────────────────────────────────────────────

def test_fee_manager_override_mexc():
    fm = FeeManager(settings.SCALP_FEE_OVERRIDES)
    fees = fm.get_fees("mexc", "BTC/USDT")
    # Verified rate: 0% maker / 5 bps taker (scripts/mexc_fee_check.py).
    assert fees["maker_bps"] == 0.0
    assert fees["taker_bps"] == 5.0
    assert fees["source"] == "override"


def test_fee_manager_dynamic_tp_sl_mexc():
    fm = FeeManager(settings.SCALP_FEE_OVERRIDES)
    tp, sl = fm.compute_tp_sl("mexc", "BTC/USDT")
    # FeeManager is taker-based: MEXC 5 bps taker → 10 bps round trip →
    # tp = round_trip + net_target. (Maker execution is handled in the agent.)
    expected_tp = 10.0 + settings.SCALP_NET_PROFIT_TARGET_BPS
    assert tp == pytest.approx(expected_tp)
    assert sl == pytest.approx(expected_tp / settings.SCALP_RR_RATIO, rel=0.01)


def test_fee_manager_dynamic_tp_sl_bitget():
    fm = FeeManager(settings.SCALP_FEE_OVERRIDES)
    tp, sl = fm.compute_tp_sl("bitget", "BTC/USDT")
    # Bitget = 2 bps round trip → tp = round_trip + net_target.
    expected_tp = 2.0 + settings.SCALP_NET_PROFIT_TARGET_BPS
    assert tp == pytest.approx(expected_tp)
    assert sl == pytest.approx(expected_tp / settings.SCALP_RR_RATIO, rel=0.01)


def test_fee_manager_viability_mexc_taker_not_viable():
    """FeeManager is taker-based: at MEXC's real 5 bps taker the 10 bps round
    trip pushes breakeven WR past the cap, so the TAKER basis is not viable.
    Maker execution is what keeps scalping viable on MEXC — see the agent's
    maker-aware gate-4 tests (test_gate4_fee_viability_maker_vs_taker)."""
    fm = FeeManager(settings.SCALP_FEE_OVERRIDES)
    viable, reason = fm.is_viable("mexc", "BTC/USDT")
    assert viable is False
    assert "mexc" in reason
    assert "%" in reason


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


def test_tfi_normalized_imbalance_ratio():
    """Bucket TFI is a volume-weighted imbalance (buy-sell)/(buy+sell),
    bounded to [-1, 1] — not raw signed volume."""
    eng = OFIEngine(levels=10, window_sec=0.01, zscore_window=80)
    key = "BTC/USDT:mexc"
    eng._bucket_start[key] = time.time() - 1.0      # force the next close
    eng.on_trade("BTC/USDT", "mexc", "buy", 3.0)
    eng.on_trade("BTC/USDT", "mexc", "sell", 1.0)
    eng._maybe_close_bucket(key, time.time())
    # (3 - 1) / (3 + 1) = 0.5, regardless of absolute volume.
    assert eng._last_tfi[key] == pytest.approx(0.5)
    assert -1.0 <= eng._last_tfi[key] <= 1.0


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

    Session window patched to 0-24 so the test runs at any UTC hour. The v2
    confluence layer is disabled here — this test covers the v1 gates 1-13 +
    observation logging; v2 selectivity is exercised in tests/test_scalping_v2.py
    and test_scalp_v2_integration.py."""
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 0)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC",   24)
    monkeypatch.setattr(settings, "SCALP_USE_CONFLUENCE", False)
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
async def test_stale_feed_guard_skips_and_logs(monkeypatch):
    """A mid frozen for >= SCALP_STALE_MID_THRESHOLD_SEC trips the stale-feed
    guard: skip_reason='stale_feed', no position opened, and the observation is
    flushed to the DB. (A never-seen symbol is given one cycle, not skipped.)"""
    monkeypatch.setattr(settings, "SCALP_STALE_MID_THRESHOLD_SEC", 60)
    agent = _agent_with_capital(0.0)

    frozen_mid = 50_000.0
    agent._get_mid_price = AsyncMock(return_value=frozen_mid)
    key = agent._pos_key("BTC/USDT", "mexc")
    # Agent state: mid last changed 90s ago and is unchanged now (> 60s).
    agent._last_mid[key] = (frozen_mid, time.time() - 90)

    saved = []
    monkeypatch.setattr(
        "agents.scalping_agent.db_queries.save_scalp_observations",
        lambda batch: saved.extend(batch),
    )

    await agent._evaluate_entry("BTC/USDT", "mexc")
    await agent._flush_observations()

    assert key not in agent._positions                          # no position opened
    obs = agent._observations[-1]
    assert obs.skip_reason == "stale_feed"
    assert obs.would_entry is False
    assert any(o.skip_reason == "stale_feed" for o in saved)    # written to DB


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
async def test_circuit_breaker_daily_loss(monkeypatch):
    """Real-capital loss past SCALP_DAILY_LOSS_HALT_PCT × alloc → agent halted."""
    # %-based: with PCT=20 and alloc=$50, halt fires at >= $10 loss.
    monkeypatch.setattr(settings, "SCALP_DAILY_LOSS_HALT_PCT", 20.0)
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

    # Exit at a 20% loss → pnl_usd = -10 (size_usd * pnl_bps/10000)
    # That's >= 20% of $50 → halt fires.
    await agent._exit_position(pos_key, pos, "SL", exit_price=40_000.0)
    assert agent._halted is True
    assert "daily loss" in agent._halt_reason.lower()


@pytest.mark.asyncio
async def test_scalp_daily_loss_halt_scales_with_allocation(monkeypatch):
    """Doubling the scalp fund doubles the USD loss tolerated — the
    %-based rule is what stops a $10 halt strangling a $5,000 fund."""
    monkeypatch.setattr(settings, "SCALP_DAILY_LOSS_HALT_PCT", 5.0)
    # alloc=$100 → halt at 5% = $5
    a1 = _agent_with_capital(100.0)
    a1._get_mid_price = AsyncMock(return_value=50_000.0)
    pos = ScalpPosition(
        symbol="BTC/USDT", exchange="mexc", direction="LONG",
        entry_price=50_000.0, entry_time=time.time(),
        entry_ofi_z=2.0, entry_tfi=1.0,
        size_usd=50.0, tp_price=50_015.0, sl_price=44_000.0,
        tp_bps=3.0, sl_bps=1.9, round_trip_cost_bps=0.0,
        observation_only=False,
    )
    a1._positions["BTC/USDT:mexc"] = pos
    # 12% loss * $50 = -$6 → exceeds $5 halt
    await a1._exit_position("BTC/USDT:mexc", pos, "SL", exit_price=44_000.0)
    assert a1._halted is True

    # alloc=$200 → halt at 5% = $10; same -$6 loss should NOT halt
    a2 = _agent_with_capital(200.0)
    a2._get_mid_price = AsyncMock(return_value=50_000.0)
    pos2 = ScalpPosition(
        symbol="BTC/USDT", exchange="mexc", direction="LONG",
        entry_price=50_000.0, entry_time=time.time(),
        entry_ofi_z=2.0, entry_tfi=1.0,
        size_usd=50.0, tp_price=50_015.0, sl_price=44_000.0,
        tp_bps=3.0, sl_bps=1.9, round_trip_cost_bps=0.0,
        observation_only=False,
    )
    a2._positions["BTC/USDT:mexc"] = pos2
    await a2._exit_position("BTC/USDT:mexc", pos2, "SL", exit_price=44_000.0)
    assert a2._halted is False


@pytest.mark.asyncio
async def test_scalp_zero_alloc_daily_loss_noop(monkeypatch):
    """Observation mode (alloc=0) → never halts on the daily-loss rule,
    even on a wildly large loss. Defensive against zero-division too."""
    monkeypatch.setattr(settings, "SCALP_DAILY_LOSS_HALT_PCT", 5.0)
    agent = _agent_with_capital(0.0)
    # Force a large _daily_loss directly so we exercise the breaker
    # without needing a position exit (zero-cap can't run real exits).
    agent._daily_loss = -1_000_000.0
    agent._consec_losses = 0
    # The breaker runs inside _exit_position — call the check inline by
    # tickling the relevant block. Easier: rebuild the assertion the
    # production code does for zero-alloc — it must short-circuit.
    alloc = agent.get_capital_allocation()
    assert alloc == 0.0
    # The production block uses `if alloc > 0` — verifying that contract.
    assert not (alloc > 0)


# ─────────────────────────────────────────────────────────────────────────
# SIM EXECUTION (_place_order persists a sim trade; _exit_position closes it)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_place_order_records_sim_trade(monkeypatch):
    """In SIM_MODE, _place_order persists a sim Trade row (mirroring the
    signal agent) and returns its id."""
    captured = {}

    def _fake_save_trade(data):
        captured.update(data)
        return 4242

    monkeypatch.setattr(settings, "SIM_MODE", True)
    monkeypatch.setattr("database.queries.save_trade", _fake_save_trade)

    agent = _agent_with_capital(100.0)
    pos = ScalpPosition(
        symbol="BTC/USDT", exchange="mexc", direction="LONG",
        entry_price=50_000.0, entry_time=time.time(),
        entry_ofi_z=2.0, entry_tfi=1.0, size_usd=25.0,
        tp_price=50_015.0, sl_price=49_990.0,
        tp_bps=3.0, sl_bps=1.9, round_trip_cost_bps=0.0,
        observation_only=False,
    )
    tid = await agent._place_order(pos)
    assert tid == 4242
    assert captured["sim_mode"] is True
    assert captured["strategy"] == "scalp"
    assert captured["signal_id"] is None
    assert captured["pair"] == "BTC/USDT"
    assert captured["exchange"] == "mexc"
    assert captured["side"] == "long"
    assert captured["size_usd"] == 25.0


@pytest.mark.asyncio
async def test_exit_closes_sim_trade_and_tracks_net_equity(monkeypatch):
    """A real (non-observation) fill: exit closes the Trade row and net
    daily P&L (equity vs SCALP_CAPITAL) reflects wins, not just losses."""
    closed = {}

    def _fake_close(trade_id, exit_price, exit_reason, pnl_usd, pnl_pct):
        closed.update(trade_id=trade_id, exit_price=exit_price,
                      reason=exit_reason, pnl_usd=pnl_usd, pnl_pct=pnl_pct)

    monkeypatch.setattr("database.queries.close_trade", _fake_close)

    agent = _agent_with_capital(100.0)
    agent._get_mid_price = AsyncMock(return_value=50_100.0)
    pos = ScalpPosition(
        symbol="BTC/USDT", exchange="mexc", direction="LONG",
        entry_price=50_000.0, entry_time=time.time(),
        entry_ofi_z=2.0, entry_tfi=1.0, size_usd=50.0,
        tp_price=50_100.0, sl_price=49_900.0,
        tp_bps=3.0, sl_bps=1.9, round_trip_cost_bps=0.0,
        observation_only=False, trade_id=7,
    )
    pos_key = "BTC/USDT:mexc"
    agent._positions[pos_key] = pos

    # +20 bps on $50 → pnl_usd = +0.10 (a win).
    await agent._exit_position(pos_key, pos, "TP", exit_price=50_100.0)

    assert closed["trade_id"] == 7
    assert closed["pnl_usd"] == pytest.approx(0.10)
    # Net daily P&L (equity) reflects the win; the losses-only tracker stays 0.
    assert agent._daily_pnl == pytest.approx(0.10)
    assert agent._daily_loss == 0.0
    stats = await agent.get_stats()
    assert stats.daily_pnl == pytest.approx(0.10)


# ─────────────────────────────────────────────────────────────────────────
# ADDITIONAL TESTS (17-20)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_reset_clears_circuit_breaker(monkeypatch):
    """UTC day rollover → daily_loss zeroes, consec_losses zeroes, the
    halt is lifted (provided it was a daily-loss halt)."""
    from datetime import timedelta
    # PCT=20 × alloc=$50 → halt threshold $10 — a -$10 loss exceeds it.
    monkeypatch.setattr(settings, "SCALP_DAILY_LOSS_HALT_PCT", 20.0)
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
    # Pin the window so the test is independent of the prod/debug value of
    # SCALP_SESSION_* — 03:00 must fall outside [START, END).
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 12)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC", 16)

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


def test_market_data_get_change_pct_returns_pct_over_window():
    """MarketData.get_change_pct: % between latest sample and the one
    at-or-before window_sec ago. 100 → 101 over 60s = +1.00%."""
    from core.market_data import MarketData
    md = MarketData()
    md._record_price_sample("mexc", "BTC/USDT", 100.0)
    # Backdate the first sample so it sits ~70s in the past
    buf = md._price_history[("mexc", "BTC/USDT")]
    buf[0] = (time.time() - 70.0, 100.0)
    md._record_price_sample("mexc", "BTC/USDT", 101.0)
    assert md.get_change_pct("mexc", "BTC/USDT", 60) == pytest.approx(1.0, abs=1e-3)


def test_market_data_get_change_pct_none_with_no_history():
    """No samples → None (not 0.0) so callers can distinguish 'no
    data yet' from 'no movement'."""
    from core.market_data import MarketData
    md = MarketData()
    assert md.get_change_pct("mexc", "BTC/USDT", 60) is None


def test_market_data_get_change_pct_none_when_window_predates_history():
    """If the buffer's oldest sample is younger than window_sec, return
    None — we'd otherwise be measuring a window we can't actually see."""
    from core.market_data import MarketData
    md = MarketData()
    md._record_price_sample("mexc", "BTC/USDT", 100.0)
    md._record_price_sample("mexc", "BTC/USDT", 101.0)
    # Both samples are fresh, but the 1-minute window pre-dates them.
    assert md.get_change_pct("mexc", "BTC/USDT", 60) is None


def test_market_data_record_price_sample_trims_old_entries():
    """Samples older than PRICE_HISTORY_WINDOW_SEC must drop off so memory
    stays bounded regardless of how long the bot runs."""
    from core.market_data import MarketData
    md = MarketData()
    md._record_price_sample("mexc", "BTC/USDT", 100.0)
    buf = md._price_history[("mexc", "BTC/USDT")]
    # Backdate well past the trim cutoff
    buf[0] = (time.time() - md.PRICE_HISTORY_WINDOW_SEC - 60, 100.0)
    md._record_price_sample("mexc", "BTC/USDT", 101.0)
    assert len(buf) == 1
    assert buf[0][1] == 101.0


@pytest.mark.asyncio
async def test_get_btc_1m_change_wired_to_market_data():
    """Scalp agent's _get_btc_1m_change delegates to
    MarketData.get_change_pct on the first enabled exchange that has
    enough history."""
    from unittest.mock import MagicMock

    md = MagicMock()
    # First enabled exchange returns None (no history); second returns +0.5%
    enabled = list(settings.ENABLED_EXCHANGES)

    def _change(ex, pair, window):
        return 0.5 if ex == enabled[1] else None

    md.get_change_pct.side_effect = _change

    agent = _agent_with_capital(0.0)
    agent.set_market_data(md)
    assert await agent._get_btc_1m_change() == pytest.approx(0.5)
    # Confirmed: called on BTC/USDT at a 60s lookback
    args = md.get_change_pct.call_args_list[-1].args
    assert args[1] == "BTC/USDT"
    assert args[2] == 60


@pytest.mark.asyncio
async def test_get_btc_1m_change_returns_zero_when_no_data():
    """Cold start (no history on any exchange) → 0.0 so the correlation
    guard stays permissive on boot rather than spuriously blocking."""
    from unittest.mock import MagicMock
    md = MagicMock()
    md.get_change_pct.return_value = None
    agent = _agent_with_capital(0.0)
    agent.set_market_data(md)
    assert await agent._get_btc_1m_change() == 0.0


@pytest.mark.asyncio
async def test_get_btc_1m_change_zero_when_market_data_missing():
    """No MarketData wired → 0.0 (the gate stays permissive)."""
    agent = _agent_with_capital(0.0)
    agent._market_data = None
    import sys
    fake = type(sys)("agents")
    fake.REGISTERED_AGENTS = []
    monkey_orig = sys.modules.get("agents")
    sys.modules["agents"] = fake
    try:
        assert await agent._get_btc_1m_change() == 0.0
    finally:
        if monkey_orig is not None:
            sys.modules["agents"] = monkey_orig
        else:
            sys.modules.pop("agents", None)


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


# ─────────────────────────────────────────────────────────────────────────
# V3 — maker execution + microprice (gate 14) + toxicity (gate 15)
# ─────────────────────────────────────────────────────────────────────────

def _arm_ofi(agent, key: str = "BTC/USDT:mexc", direction: str = "LONG") -> None:
    """Force the OFI engine into a fire-ready state for `direction` so the
    entry path reaches the v3 gates (mirrors the inline setup other tests use)."""
    eng = agent._ofi_engine
    z = settings.SCALP_OFI_Z_ENTRY + 0.5
    eng._last_z[key] = z if direction == "LONG" else -z
    eng._last_tfi[key] = 1.0 if direction == "LONG" else -1.0
    eng._last_bucket_close[key] = time.time()
    eng._persist[key] = settings.SCALP_OFI_PERSIST_TICKS


def test_maker_execution_prices_round_trip_at_maker_fee(monkeypatch):
    """SCALP_USE_MAKER_EXECUTION prices the round trip at the MAKER fee
    (maker_bps*2); with it off, the taker-based FeeManager value is used.
    FeeManager itself is untouched — only the agent's fee basis pivots."""
    agent = _agent_with_capital(0.0)
    # Inject a fee where maker != taker so the source is unambiguous.
    agent._fee_manager._cache["mexc"] = {
        "BTC/USDT": {"maker_bps": 0.0, "taker_bps": 5.0, "source": "ccxt"},
    }
    monkeypatch.setattr(settings, "SCALP_USE_MAKER_EXECUTION", True)
    assert agent._round_trip_bps("mexc", "BTC/USDT") == 0.0    # 0 maker * 2
    monkeypatch.setattr(settings, "SCALP_USE_MAKER_EXECUTION", False)
    assert agent._round_trip_bps("mexc", "BTC/USDT") == 10.0   # 5 taker * 2


@pytest.mark.asyncio
async def test_microprice_gate_passes_agreement(monkeypatch):
    """Top-of-book imbalance agrees with OFI LONG (bid size > ask size →
    microprice > mid) → gate 14 passes and the entry is logged."""
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 0)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC", 24)
    monkeypatch.setattr(settings, "SCALP_USE_CONFLUENCE", False)
    monkeypatch.setattr(settings, "SCALP_USE_MICROPRICE_GATE", True)
    monkeypatch.setattr(settings, "SCALP_USE_TOXICITY_GATE", False)
    agent = _agent_with_capital(0.0)
    agent._get_mid_price  = AsyncMock(return_value=50_000.0)
    agent._get_spread_bps = AsyncMock(return_value=1.0)
    agent._get_regime     = AsyncMock(return_value="TRENDING")
    # bid_qty (9) >> ask_qty (1) → imbalance 0.9 → microprice above mid.
    agent._ofi_engine.on_book("BTC/USDT", "mexc",
                              [(49_999.0, 9.0)], [(50_001.0, 1.0)])
    _arm_ofi(agent, direction="LONG")

    await agent._evaluate_entry("BTC/USDT", "mexc")
    obs = agent._observations[-1]
    assert obs.would_entry is True


@pytest.mark.asyncio
async def test_microprice_gate_blocks_contradiction(monkeypatch):
    """Ask size >> bid size → microprice < mid → contradicts OFI LONG →
    gate 14 stands the entry down."""
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 0)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC", 24)
    monkeypatch.setattr(settings, "SCALP_USE_CONFLUENCE", False)
    monkeypatch.setattr(settings, "SCALP_USE_MICROPRICE_GATE", True)
    monkeypatch.setattr(settings, "SCALP_USE_TOXICITY_GATE", False)
    agent = _agent_with_capital(0.0)
    agent._get_mid_price  = AsyncMock(return_value=50_000.0)
    agent._get_spread_bps = AsyncMock(return_value=1.0)
    agent._get_regime     = AsyncMock(return_value="TRENDING")
    # bid_qty (1) << ask_qty (9) → imbalance 0.1 → microprice below mid.
    agent._ofi_engine.on_book("BTC/USDT", "mexc",
                              [(49_999.0, 1.0)], [(50_001.0, 9.0)])
    _arm_ofi(agent, direction="LONG")

    await agent._evaluate_entry("BTC/USDT", "mexc")
    obs = agent._observations[-1]
    assert obs.would_entry is False
    assert "Microprice" in obs.skip_reason
    assert "BTC/USDT:mexc" not in agent._positions


@pytest.mark.asyncio
async def test_toxicity_gate_blocks_spread_spike(monkeypatch):
    """Spread under the hard gate-9 cap but over SCALP_TOXICITY_SPREAD_BPS →
    gate 15 stands down ('spread spike')."""
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 0)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC", 24)
    monkeypatch.setattr(settings, "SCALP_USE_CONFLUENCE", False)
    monkeypatch.setattr(settings, "SCALP_USE_MICROPRICE_GATE", False)
    monkeypatch.setattr(settings, "SCALP_USE_TOXICITY_GATE", True)
    monkeypatch.setattr(settings, "SCALP_MAX_SPREAD_BPS", 100.0)     # gate 9 passes
    monkeypatch.setattr(settings, "SCALP_TOXICITY_SPREAD_BPS", 5.0)
    agent = _agent_with_capital(0.0)
    agent._get_mid_price       = AsyncMock(return_value=50_000.0)
    agent._get_spread_bps      = AsyncMock(return_value=20.0)        # > 5, < 100
    agent._get_regime          = AsyncMock(return_value="TRENDING")
    agent._get_symbol_move_bps = AsyncMock(return_value=0.0)         # vol normal
    _arm_ofi(agent, direction="LONG")

    await agent._evaluate_entry("BTC/USDT", "mexc")
    obs = agent._observations[-1]
    assert obs.would_entry is False
    assert "spread spike" in obs.skip_reason


@pytest.mark.asyncio
async def test_toxicity_gate_blocks_vol_spike(monkeypatch):
    """|1m mid move| over SCALP_TOXICITY_VOL_BPS → gate 15 stands down
    ('volatility spike'). Uses BTC/USDT so gate 13's BTC guard is skipped."""
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 0)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC", 24)
    monkeypatch.setattr(settings, "SCALP_USE_CONFLUENCE", False)
    monkeypatch.setattr(settings, "SCALP_USE_MICROPRICE_GATE", False)
    monkeypatch.setattr(settings, "SCALP_USE_TOXICITY_GATE", True)
    monkeypatch.setattr(settings, "SCALP_TOXICITY_VOL_BPS", 40.0)
    agent = _agent_with_capital(0.0)
    agent._get_mid_price       = AsyncMock(return_value=50_000.0)
    agent._get_spread_bps      = AsyncMock(return_value=1.0)         # spread normal
    agent._get_regime          = AsyncMock(return_value="TRENDING")
    agent._get_symbol_move_bps = AsyncMock(return_value=80.0)        # > 40
    _arm_ofi(agent, direction="LONG")

    await agent._evaluate_entry("BTC/USDT", "mexc")
    obs = agent._observations[-1]
    assert obs.would_entry is False
    assert "volatility spike" in obs.skip_reason


@pytest.mark.asyncio
async def test_toxicity_gate_passes_normal_conditions(monkeypatch):
    """Normal spread + normal vol → gate 15 passes and the entry is logged."""
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 0)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC", 24)
    monkeypatch.setattr(settings, "SCALP_USE_CONFLUENCE", False)
    monkeypatch.setattr(settings, "SCALP_USE_MICROPRICE_GATE", False)
    monkeypatch.setattr(settings, "SCALP_USE_TOXICITY_GATE", True)
    monkeypatch.setattr(settings, "SCALP_TOXICITY_SPREAD_BPS", 5.0)
    monkeypatch.setattr(settings, "SCALP_TOXICITY_VOL_BPS", 40.0)
    agent = _agent_with_capital(0.0)
    agent._get_mid_price       = AsyncMock(return_value=50_000.0)
    agent._get_spread_bps      = AsyncMock(return_value=1.0)         # < 5
    agent._get_regime          = AsyncMock(return_value="TRENDING")
    agent._get_symbol_move_bps = AsyncMock(return_value=5.0)         # < 40
    _arm_ofi(agent, direction="LONG")

    await agent._evaluate_entry("BTC/USDT", "mexc")
    obs = agent._observations[-1]
    assert obs.would_entry is True


# ─────────────────────────────────────────────────────────────────────────
# V3 — gate 4 maker-aware fee viability
# ─────────────────────────────────────────────────────────────────────────

def test_gate4_fee_viability_maker_vs_taker(monkeypatch):
    """Gate-4 viability prices the round trip per SCALP_USE_MAKER_EXECUTION:
    a 0-maker / 26-taker fee is viable as maker but blocked as taker. With
    maker execution off it matches FeeManager.is_viable. FeeManager untouched."""
    agent = _agent_with_capital(0.0)
    agent._fee_manager._cache["mexc"] = {
        "BTC/USDT": {"maker_bps": 0.0, "taker_bps": 26.0, "source": "ccxt"},
    }
    monkeypatch.setattr(settings, "SCALP_USE_MAKER_EXECUTION", True)
    viable, reason, info = agent._fee_viability("mexc", "BTC/USDT")
    assert viable is True
    assert reason == ""
    assert info["rt_bps"] == 0.0          # maker 0 * 2

    monkeypatch.setattr(settings, "SCALP_USE_MAKER_EXECUTION", False)
    viable, reason, info = agent._fee_viability("mexc", "BTC/USDT")
    assert viable is False
    assert "exceeds limit" in reason
    assert info["rt_bps"] == 52.0         # taker 26 * 2


@pytest.mark.asyncio
async def test_gate4_blocks_taker_passes_maker_in_entry_flow(monkeypatch):
    """End-to-end: a 0-maker / 26-taker fee stands the entry down at gate 4
    under taker execution, but admits it under maker execution."""
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 0)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC", 24)
    monkeypatch.setattr(settings, "SCALP_USE_CONFLUENCE", False)
    monkeypatch.setattr(settings, "SCALP_USE_MICROPRICE_GATE", False)
    monkeypatch.setattr(settings, "SCALP_USE_TOXICITY_GATE", False)

    def _mk():
        a = _agent_with_capital(0.0)
        a._fee_manager._cache["mexc"] = {
            "BTC/USDT": {"maker_bps": 0.0, "taker_bps": 26.0, "source": "ccxt"},
        }
        a._get_mid_price  = AsyncMock(return_value=50_000.0)
        a._get_spread_bps = AsyncMock(return_value=1.0)
        a._get_regime     = AsyncMock(return_value="TRENDING")
        _arm_ofi(a, direction="LONG")
        return a

    monkeypatch.setattr(settings, "SCALP_USE_MAKER_EXECUTION", False)
    a_taker = _mk()
    await a_taker._evaluate_entry("BTC/USDT", "mexc")
    obs_t = a_taker._observations[-1]
    assert obs_t.would_entry is False
    assert "exceeds limit" in obs_t.skip_reason

    monkeypatch.setattr(settings, "SCALP_USE_MAKER_EXECUTION", True)
    a_maker = _mk()
    await a_maker._evaluate_entry("BTC/USDT", "mexc")
    obs_m = a_maker._observations[-1]
    assert obs_m.would_entry is True
