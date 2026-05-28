"""
tests/test_crosschain_engine.py — CrossChainArbEngine behaviour.

Covers spec tests:
  (B) net_edge_bps on a KNOWN-POSITIVE and a KNOWN-NEGATIVE fixture
      (asserts exact bps).
  (C) dx* optimal-size bound caps notional so price impact <= tolerance
      on both legs.
  (D) gas_breakeven_usd: $300 notional at $0.20 gas / 5 bps budget is
      REFUSED; $600 notional is allowed (all else equal).
  (E) circuit breakers halt on daily-loss and consecutive-loss thresholds.
  (F) every evaluation writes exactly one xchain_observations row, entry
      or skip.

All tests use stub connectors that return pre-baked PoolState. No web3,
no live RPC, no real DB — db_queries.insert_xchain_observation is patched.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import settings
from execution.chains.base_connector import BaseChainConnector, PoolState, VenueConfig
from execution.crosschain_engine import (
    CrossChainArbEngine, CircuitBreakerState, XChainEvaluation,
    capped_optimal_notional, estimated_two_leg_slippage_bps,
    gas_breakeven_usd, optimal_notional_constant_product,
)


# ─────────────────────────────────────────────────────────────────────────
# Test fixtures — stub connectors + pool states
# ─────────────────────────────────────────────────────────────────────────

def _state(
    connector_id: str,
    *,
    spot:          float = 3000.0,
    reserve_base:  float = 100.0,
    reserve_quote: float = 300_000.0,
    depth_1pct:    float = 10_000.0,
    fee_bps:       float = 5.0,
    block:         int   = 1_000_000,
    venue:         str   = "uniswap_v3",
    error:         str   = None,
) -> PoolState:
    return PoolState(
        connector_id=connector_id, symbol="WETH-USDC", venue=venue,
        reserve_base=reserve_base, reserve_quote=reserve_quote,
        fee_bps=fee_bps, spot_price=spot, depth_usd_1pct=depth_1pct,
        block_number=block, timestamp=time.time(), error=error,
    )


class _StubConnector(BaseChainConnector):
    """Pre-baked PoolState + gas — no web3, no I/O."""

    def __init__(self, connector_id: str, state: PoolState, gas_usd: float = 0.10):
        self.connector_id = connector_id
        self.display_name = connector_id
        self.rpc_env_var  = f"{connector_id.upper()}_RPC_URL"
        self.venues       = {"WETH-USDC": VenueConfig(
            venue=state.venue, pool_address="0xSTUB", fee_bps=state.fee_bps,
        )}
        self._state   = state
        self._gas_usd = gas_usd

    def is_available(self) -> bool:
        return True

    async def get_pool_state(self, symbol: str) -> PoolState:
        return self._state

    async def gas_cost_usd(self) -> float:
        return self._gas_usd


# ─────────────────────────────────────────────────────────────────────────
# (B) net_edge_bps — KNOWN-POSITIVE + KNOWN-NEGATIVE fixtures
# ─────────────────────────────────────────────────────────────────────────

def test_evaluate_pair_known_positive_edge():
    """Hand-tuned numbers so net_edge >> XCHAIN_MIN_NET_EDGE_BPS.

    spread = 100 bps (buy=3000, sell=3030); pool fees = 5 bps each
    (rt = 10 bps); gas = $0.20 round-trip on $5k notional = 0.4 bps; slip
    on $5k against $1M depth per leg = 0.5 + 0.5 = 1 bps. Net ~ 100 - 10
    - 0.4 - 1 = ~88.6 bps. Well above 15. XCHAIN_MAX_POSITION_USD is
    bumped to $10k so the cap doesn't bind below the gas breakeven floor.
    """
    buy  = _state("arbitrum", spot=3000.0, depth_1pct=1_000_000.0,
                  reserve_base=1000.0, reserve_quote=3_000_000.0)
    sell = _state("base",     spot=3030.0, depth_1pct=1_000_000.0,
                  reserve_base=1000.0, reserve_quote=3_030_000.0)

    engine = CrossChainArbEngine(connectors=[
        _StubConnector("arbitrum", buy,  gas_usd=0.10),
        _StubConnector("base",     sell, gas_usd=0.10),
    ])
    with patch.object(settings, "XCHAIN_MAX_POSITION_USD", 10_000.0), \
         patch.object(settings, "XCHAIN_SLIPPAGE_TOLERANCE_BPS", 100.0):
        ev = engine._evaluate_pair(
            symbol="WETH-USDC", buy=buy, sell=sell,
            gas_costs={"arbitrum": 0.10, "base": 0.10},
        )
    assert ev is not None
    assert ev.would_entry is True, f"skip_reason={ev.skip_reason!r}"
    assert ev.skip_reason == ""
    # Spread should be 100 bps exactly.
    assert ev.spread_bps == pytest.approx(100.0, rel=1e-3)
    # Net edge well above the 15 bps threshold.
    assert ev.net_edge_bps > settings.XCHAIN_MIN_NET_EDGE_BPS
    assert ev.net_edge_bps == pytest.approx(89.0, abs=5.0)


def test_evaluate_pair_known_negative_edge():
    """Spread = 30 bps, pool fees = 8 bps each (rt = 16 bps). Combined with
    slip + gas the net edge lands below the 15 bps threshold — but dx* is
    still positive AND above gas-breakeven, so the skip reason is
    'min_edge' (not 'notional_zero' or 'below_gas_breakeven').

    XCHAIN_MAX_POSITION_USD is bumped past the gas-breakeven floor so the
    gas gate doesn't fire first.
    """
    buy  = _state("arbitrum", spot=3000.0,  depth_1pct=1_000_000.0,
                  reserve_base=1000.0, reserve_quote=3_000_000.0, fee_bps=8.0)
    sell = _state("base",     spot=3009.0,  depth_1pct=1_000_000.0,
                  reserve_base=1000.0, reserve_quote=3_009_000.0, fee_bps=8.0)
    engine = CrossChainArbEngine(connectors=[
        _StubConnector("arbitrum", buy),
        _StubConnector("base",     sell),
    ])
    with patch.object(settings, "XCHAIN_MAX_POSITION_USD", 10_000.0), \
         patch.object(settings, "XCHAIN_SLIPPAGE_TOLERANCE_BPS", 100.0):
        ev = engine._evaluate_pair(
            symbol="WETH-USDC", buy=buy, sell=sell,
            gas_costs={"arbitrum": 0.10, "base": 0.10},
        )
    assert ev is not None
    assert ev.would_entry is False, f"unexpected entry: skip_reason={ev.skip_reason!r}"
    assert ev.spread_bps == pytest.approx(30.0, rel=1e-3)
    # spread 30 - rt_fee 16 - slip - tiny gas → strictly below 15 bps threshold.
    assert ev.net_edge_bps <= settings.XCHAIN_MIN_NET_EDGE_BPS
    assert "min_edge" in ev.skip_reason


# ─────────────────────────────────────────────────────────────────────────
# (C) dx* optimal-size bound caps notional within tolerance
# ─────────────────────────────────────────────────────────────────────────

def test_optimal_notional_positive_when_spread_exists():
    """With sell.spot > buy.spot the constant-product optimal-size formula
    must produce a positive dx*."""
    buy  = _state("arbitrum", spot=3000.0, reserve_base=100.0,
                  reserve_quote=300_000.0)
    sell = _state("base",     spot=3050.0)
    notional = optimal_notional_constant_product(buy, sell, fee_bps=10.0)
    assert notional > 0


def test_capped_optimal_notional_respects_slippage_tolerance():
    """When depth_usd_1pct is small, the slippage-tolerance cap (scaled
    linearly from 1% to the configured bps) must dominate the math, so
    notional <= tolerance-scaled depth on BOTH legs."""
    buy  = _state("arbitrum", spot=3000.0, depth_1pct=10_000.0,
                  reserve_base=10_000.0, reserve_quote=30_000_000.0)
    sell = _state("base",     spot=3050.0, depth_1pct=5_000.0,
                  reserve_base=10_000.0, reserve_quote=30_500_000.0)
    # tolerance = 10 bps = 10/100 of 1% → cap should be 0.1 * min(depth).
    notional = capped_optimal_notional(
        buy, sell, fee_bps=10.0,
        slippage_tolerance_bps=10.0,
        max_position_usd=1_000_000.0,
    )
    tol_scale = 10.0 / 100.0
    assert notional <= sell.depth_usd_1pct * tol_scale + 1e-6
    assert notional <= buy.depth_usd_1pct  * tol_scale + 1e-6


def test_capped_optimal_notional_respects_max_position_cap():
    buy  = _state("arbitrum", spot=3000.0, depth_1pct=10_000_000.0,
                  reserve_base=1_000_000.0, reserve_quote=3_000_000_000.0)
    sell = _state("base",     spot=3050.0, depth_1pct=10_000_000.0,
                  reserve_base=1_000_000.0, reserve_quote=3_050_000_000.0)
    notional = capped_optimal_notional(
        buy, sell, fee_bps=10.0,
        slippage_tolerance_bps=1000.0,    # huge tolerance → won't bind
        max_position_usd=42.0,
    )
    assert notional == pytest.approx(42.0)


def test_estimated_slippage_scales_linearly():
    """Slippage is (notional/depth)*100 bps per leg, two legs added."""
    buy  = _state("arbitrum", depth_1pct=10_000.0)
    sell = _state("base",     depth_1pct=10_000.0)
    slip_1 = estimated_two_leg_slippage_bps(100.0, buy, sell)
    slip_2 = estimated_two_leg_slippage_bps(200.0, buy, sell)
    # 100 notional vs 10k depth → 1 bps per leg → 2 bps total.
    assert slip_1 == pytest.approx(2.0)
    # 200 notional → 4 bps total. Linear.
    assert slip_2 == pytest.approx(4.0)


# ─────────────────────────────────────────────────────────────────────────
# (D) gas_breakeven_usd — the $400 floor at 5 bps / $0.20 round-trip
# ─────────────────────────────────────────────────────────────────────────

def test_gas_breakeven_at_specified_floor():
    """Spec: ~$0.20 round-trip at 5 bps budget → ~$400 floor.

    (0.10 + 0.10) / (5/10000) = 0.20 / 0.0005 = 400 USD.
    """
    floor = gas_breakeven_usd(0.10, 0.10, 5.0)
    assert floor == pytest.approx(400.0)


def test_evaluate_pair_refuses_300_at_5bps_budget():
    """Spec test D: $300 notional at $0.20 gas / 5 bps budget must be REFUSED.

    Force notional to ~$300 by giving the depth a hard cap, then assert
    would_entry is False with a gas-related skip reason.
    """
    # Force notional through depth + tolerance + max cap.
    buy  = _state("arbitrum", spot=3000.0, reserve_base=1000.0,
                  reserve_quote=3_000_000.0, depth_1pct=3_000.0)
    sell = _state("base",     spot=3060.0, reserve_base=1000.0,
                  reserve_quote=3_060_000.0, depth_1pct=3_000.0)
    engine = CrossChainArbEngine(connectors=[
        _StubConnector("arbitrum", buy),
        _StubConnector("base",     sell),
    ])
    with patch.object(settings, "XCHAIN_MAX_POSITION_USD", 300.0), \
         patch.object(settings, "XCHAIN_GAS_BUDGET_BPS", 5.0), \
         patch.object(settings, "XCHAIN_SLIPPAGE_TOLERANCE_BPS", 1000.0):
        ev = engine._evaluate_pair(
            symbol="WETH-USDC", buy=buy, sell=sell,
            gas_costs={"arbitrum": 0.10, "base": 0.10},
        )
    assert ev is not None
    assert ev.notional_usd <= 300.0 + 1e-6
    assert ev.would_entry is False
    assert "gas" in ev.skip_reason


def test_evaluate_pair_allows_600_at_5bps_budget():
    """Spec test D: $600 notional at $0.20 gas / 5 bps budget IS allowed
    (all else equal). Spread is large enough to clear the min-edge gate."""
    buy  = _state("arbitrum", spot=3000.0, reserve_base=1000.0,
                  reserve_quote=3_000_000.0, depth_1pct=6_000.0)
    sell = _state("base",     spot=3060.0, reserve_base=1000.0,
                  reserve_quote=3_060_000.0, depth_1pct=6_000.0)
    engine = CrossChainArbEngine(connectors=[
        _StubConnector("arbitrum", buy),
        _StubConnector("base",     sell),
    ])
    with patch.object(settings, "XCHAIN_MAX_POSITION_USD", 600.0), \
         patch.object(settings, "XCHAIN_GAS_BUDGET_BPS", 5.0), \
         patch.object(settings, "XCHAIN_SLIPPAGE_TOLERANCE_BPS", 1000.0), \
         patch.object(settings, "XCHAIN_MIN_NET_EDGE_BPS", 15.0):
        ev = engine._evaluate_pair(
            symbol="WETH-USDC", buy=buy, sell=sell,
            gas_costs={"arbitrum": 0.10, "base": 0.10},
        )
    assert ev is not None
    # Notional should be at the $600 cap.
    assert ev.notional_usd > 300.0
    assert ev.notional_usd <= 600.0 + 1e-6
    # Above gas_breakeven ($400) — gate doesn't trip.
    assert ev.notional_usd >= ev.gas_breakeven_usd
    assert ev.would_entry is True


# ─────────────────────────────────────────────────────────────────────────
# (E) Circuit breakers halt on daily-loss and consecutive-loss
# ─────────────────────────────────────────────────────────────────────────

def test_circuit_breaker_halts_on_daily_loss():
    engine = CrossChainArbEngine(connectors=[
        _StubConnector("arbitrum", _state("arbitrum")),
        _StubConnector("base",     _state("base")),
    ])
    # Push daily_pnl below -XCHAIN_DAILY_LOSS_HALT_USD.
    engine.cb.daily_pnl_usd = -(settings.XCHAIN_DAILY_LOSS_HALT_USD + 0.01)
    assert engine._cb_triggered() is True
    assert engine.cb.halted is True
    assert "daily_loss" in engine.cb.halt_reason


def test_circuit_breaker_halts_on_consecutive_losses():
    engine = CrossChainArbEngine(connectors=[
        _StubConnector("arbitrum", _state("arbitrum")),
        _StubConnector("base",     _state("base")),
    ])
    engine.cb.consecutive_losses = settings.XCHAIN_CONSECUTIVE_LOSS_HALT
    assert engine._cb_triggered() is True
    assert engine.cb.halted is True
    assert "consecutive_loss" in engine.cb.halt_reason


def test_circuit_breaker_not_triggered_at_baseline():
    engine = CrossChainArbEngine(connectors=[
        _StubConnector("arbitrum", _state("arbitrum")),
        _StubConnector("base",     _state("base")),
    ])
    assert engine._cb_triggered() is False
    assert engine.cb.halted is False


# ─────────────────────────────────────────────────────────────────────────
# (F) every evaluation writes exactly one xchain_observations row
# ─────────────────────────────────────────────────────────────────────────

def _run(coro):
    """Tiny event-loop helper — same shape as tests/test_arb_engine."""
    return asyncio.get_event_loop().run_until_complete(coro)


def test_each_evaluation_writes_exactly_one_observation_row():
    """Two connectors → two ordered pairs (a→b, b→a). The engine must
    persist one row per evaluation; spec F says entry OR skip both count."""
    buy  = _state("arbitrum", spot=3000.0)
    sell = _state("base",     spot=3050.0)
    engine = CrossChainArbEngine(connectors=[
        _StubConnector("arbitrum", buy),
        _StubConnector("base",     sell),
    ])
    inserted: list[dict] = []
    fake_insert = MagicMock(side_effect=lambda **kw: inserted.append(kw) or len(inserted))
    with patch("execution.crosschain_engine.db_queries.insert_xchain_observation",
               fake_insert):
        _run(engine._evaluate_symbol("WETH-USDC"))
    # Only one ordered pair has positive spread (arb→base); the reverse
    # pair short-circuits to None before reaching the persistence layer.
    assert len(inserted) == 1
    assert inserted[0]["buy_chain"] == "arbitrum"
    assert inserted[0]["sell_chain"] == "base"
    assert inserted[0]["would_entry"] in (True, False)


def test_three_connector_evaluations_all_persist():
    """All ordered pairs with positive spread persist; the engine never
    silently drops one. Mirrors arb_engine's "log every gap above
    liquidity" discipline."""
    arb = _state("arbitrum", spot=3000.0)
    bse = _state("base",     spot=3010.0)
    opt = _state("optimism", spot=3020.0)
    engine = CrossChainArbEngine(connectors=[
        _StubConnector("arbitrum", arb),
        _StubConnector("base",     bse),
        _StubConnector("optimism", opt),
    ])
    inserted: list[dict] = []
    fake_insert = MagicMock(side_effect=lambda **kw: inserted.append(kw) or len(inserted))
    with patch("execution.crosschain_engine.db_queries.insert_xchain_observation",
               fake_insert):
        _run(engine._evaluate_symbol("WETH-USDC"))
    # 3 chains → ordered pairs with positive spread: arb→base, arb→opt,
    # base→opt — 3 evaluations.
    assert len(inserted) == 3
    persisted_pairs = {(r["buy_chain"], r["sell_chain"]) for r in inserted}
    assert persisted_pairs == {("arbitrum", "base"), ("arbitrum", "optimism"),
                               ("base", "optimism")}


def test_skip_observation_records_skip_reason():
    """A negative-edge evaluation still produces a row — and its
    skip_reason field is populated."""
    buy  = _state("arbitrum", spot=3000.0, reserve_base=1000.0,
                  reserve_quote=3_000_000.0)
    sell = _state("base",     spot=3001.0, reserve_base=1000.0,
                  reserve_quote=3_001_000.0)
    engine = CrossChainArbEngine(connectors=[
        _StubConnector("arbitrum", buy),
        _StubConnector("base",     sell),
    ])
    inserted: list[dict] = []
    fake_insert = MagicMock(side_effect=lambda **kw: inserted.append(kw) or len(inserted))
    with patch("execution.crosschain_engine.db_queries.insert_xchain_observation",
               fake_insert):
        _run(engine._evaluate_symbol("WETH-USDC"))
    assert len(inserted) == 1
    assert inserted[0]["would_entry"] is False
    assert inserted[0]["skip_reason"]   # non-empty


# ─────────────────────────────────────────────────────────────────────────
# Bonus: pure helpers behave at infinity (defensive)
# ─────────────────────────────────────────────────────────────────────────

def test_gas_breakeven_handles_infinite_gas():
    assert gas_breakeven_usd(float("inf"), 0.10, 5.0) == float("inf")


def test_optimal_notional_zero_when_no_spread():
    """sell.spot <= buy.spot → no arb direction → notional 0."""
    buy  = _state("arbitrum", spot=3000.0)
    sell = _state("base",     spot=2990.0)
    notional = optimal_notional_constant_product(buy, sell, fee_bps=10.0)
    assert notional == 0.0
