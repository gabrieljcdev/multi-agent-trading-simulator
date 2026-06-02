"""
tests/test_funding_cross_venue.py — cross-venue carry observation (Phase 2).

Covers interval normalisation before differencing (Binance 8h vs
Hyperliquid 1h), the long/short leg selection + spread sign, the
projected_net_apr fee + interval-risk math, the would_enter flip around
FUNDING_MIN_APR, and the OBSERVATION-ONLY guarantee that nothing is placed
on either leg.

Engine is driven through injected fake venues (and, for the normalisation
test, the real plugins wired to ccxt stubs) — no network.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import settings
from execution.funding_engine import FundingEngine, FundingOpportunity
from execution.funding_venues.base_funding import (
    BaseFundingVenue, FundingQuote, annualise_funding,
)
from execution.funding_venues.binance import BinanceFundingVenue
from execution.funding_venues.hyperliquid import HyperliquidFundingVenue
from agents.funding_arb_agent import FundingArbAgent


# ─────────────────────────────────────────────────────────────────────────
# Fake venue — returns pre-baked quotes; controls funding_apr exactly
# ─────────────────────────────────────────────────────────────────────────

class FakeVenue(BaseFundingVenue):
    def __init__(self, venue_id: str, quotes: dict[str, FundingQuote]):
        self.venue_id = venue_id           # instance attr shadows class attr
        self._quotes = quotes

    async def fetch_funding(self, symbol: str) -> FundingQuote:
        q = self._quotes.get(symbol)
        if q is None:
            return FundingQuote(venue=self.venue_id, symbol=symbol,
                                funding_apr=None, funding_interval_sec=3600,
                                error="symbol_not_listed")
        return q

    async def list_perps(self) -> list[str]:
        return list(self._quotes)

    def is_available(self) -> bool:
        return True


def _q(venue, symbol, funding_apr, *, interval=3600, oi=5_000_000.0,
       taker=4.5, maker=1.5, depth_ok=True) -> FundingQuote:
    return FundingQuote(
        venue=venue, symbol=symbol, funding_apr=funding_apr,
        funding_interval_sec=interval, oi_usd=oi, depth_ok=depth_ok,
        taker_fee_bps=taker, maker_fee_bps=maker, ts=time.time(),
    )


@pytest.fixture(autouse=True)
def _enable_cross(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_CROSS_VENUE_ENABLED", True)
    monkeypatch.setattr(settings, "FUNDING_SYMBOLS", ["SOL/USDT"])
    monkeypatch.setattr(settings, "FUNDING_MIN_APR", 0.06)


# ─────────────────────────────────────────────────────────────────────────
# 1. interval normalisation before differencing (real plugins + stubs)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_interval_normalisation_before_differencing(monkeypatch):
    """Binance funds 8h, Hyperliquid 1h. The cross-venue spread must
    difference the ANNUALISED rates (each normalised by its own interval),
    NOT the raw per-interval rates."""
    monkeypatch.setattr(settings, "FUNDING_HL_BULK_TTL_SEC", 30)
    r_8h = 0.0005          # binance 8h rate → apr = 0.0005 * 1095 = 0.5475
    r_1h = 0.00001         # hl 1h rate      → apr = 0.00001 * 8760 = 0.0876

    binance_stub = MagicMock()
    binance_stub.fetch_funding_rate = AsyncMock(return_value={"fundingRate": r_8h})
    del binance_stub.fetch_open_interest
    binance_stub.close = AsyncMock()

    hl_markets = {"SOL/USDC:USDC": {"swap": True, "base": "SOL", "quote": "USDC"}}
    hl_stub = MagicMock()
    hl_stub.load_markets = AsyncMock(return_value=hl_markets)
    hl_stub.fetch_funding_rates = AsyncMock(
        return_value={"SOL/USDC:USDC": {"fundingRate": r_1h, "markPrice": 150.0}},
    )
    hl_stub.fetch_open_interests = AsyncMock(
        return_value={"SOL/USDC:USDC": {"openInterestValue": 1_000_000.0}},
    )
    hl_stub.close = AsyncMock()

    eng = FundingEngine(venues=[
        BinanceFundingVenue(ccxt_factory=lambda: binance_stub),
        HyperliquidFundingVenue(ccxt_factory=lambda: hl_stub),
    ])
    opps = await eng.scan()
    cross = [o for o in opps if o.legs == "cross_venue"]
    assert len(cross) == 1
    c = cross[0]
    # short = richer funding = binance (0.5475); long = hl (0.0876).
    assert c.venue_short == "binance"
    assert c.venue_long == "hyperliquid"
    expected_spread = annualise_funding(r_8h, 8 * 3600) - annualise_funding(r_1h, 3600)
    assert c.spread_apr == pytest.approx(expected_spread)
    # Sanity: differencing the RAW rates would have given a very different
    # (wrong) number — confirm we didn't do that.
    assert c.spread_apr != pytest.approx(r_8h - r_1h)


# ─────────────────────────────────────────────────────────────────────────
# 2. spread sign / leg selection
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spread_picks_richer_funding_as_short_leg():
    a = FakeVenue("venue_a", {"SOL/USDT": _q("venue_a", "SOL/USDT", 0.30)})
    b = FakeVenue("venue_b", {"SOL/USDT": _q("venue_b", "SOL/USDT", 0.05)})
    eng = FundingEngine(venues=[b, a])      # order shouldn't matter
    cross = [o for o in await eng.scan() if o.legs == "cross_venue"]
    assert len(cross) == 1
    c = cross[0]
    assert c.venue_short == "venue_a"        # richer funding → short to collect
    assert c.venue_long == "venue_b"
    assert c.spread_apr == pytest.approx(0.25)
    assert c.funding_apr_short == pytest.approx(0.30)
    assert c.funding_apr_long == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_single_symbol_on_one_venue_yields_no_cross():
    a = FakeVenue("venue_a", {"SOL/USDT": _q("venue_a", "SOL/USDT", 0.30)})
    eng = FundingEngine(venues=[a])
    cross = [o for o in await eng.scan() if o.legs == "cross_venue"]
    assert cross == []


# ─────────────────────────────────────────────────────────────────────────
# 3. projected_net_apr subtracts round-trip fees + interval-risk buffer
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_projected_net_apr_subtracts_fees_and_buffer(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_REQUIRE_MAKER_FEES", True)
    monkeypatch.setattr(settings, "FUNDING_FUNDING_INTERVAL_RISK_BPS", 1.5)
    monkeypatch.setattr(settings, "FUNDING_MAX_HOLD_SEC", 1_209_600)   # 14d

    a = FakeVenue("venue_a", {"SOL/USDT": _q("venue_a", "SOL/USDT", 0.40, maker=1.0, taker=4.0)})
    b = FakeVenue("venue_b", {"SOL/USDT": _q("venue_b", "SOL/USDT", 0.05, maker=2.0, taker=5.0)})
    eng = FundingEngine(venues=[a, b])
    c = [o for o in await eng.scan() if o.legs == "cross_venue"][0]

    spread = 0.35
    # maker preferred: legs sum = 1.0 + 2.0 = 3.0 bps; round trip = entry+exit
    # on both legs = 2 * 3.0 = 6.0 bps.
    year = 365.0 * 86400.0
    hold = 1_209_600.0
    rt_fees_apr = (6.0 / 1e4) * (year / hold)
    buffer_apr  = (1.5 / 1e4) * (year / hold)
    assert c.spread_apr == pytest.approx(spread)
    assert c.projected_net_apr == pytest.approx(spread - rt_fees_apr - buffer_apr)
    assert c.projected_net_apr < c.spread_apr


@pytest.mark.asyncio
async def test_taker_fallback_when_maker_unavailable(monkeypatch):
    """A leg with no maker tier (maker_fee_bps=0) falls back to taker."""
    monkeypatch.setattr(settings, "FUNDING_REQUIRE_MAKER_FEES", True)
    monkeypatch.setattr(settings, "FUNDING_FUNDING_INTERVAL_RISK_BPS", 1.5)
    monkeypatch.setattr(settings, "FUNDING_MAX_HOLD_SEC", 1_209_600)

    a = FakeVenue("venue_a", {"SOL/USDT": _q("venue_a", "SOL/USDT", 0.40, maker=0.0, taker=4.0)})
    b = FakeVenue("venue_b", {"SOL/USDT": _q("venue_b", "SOL/USDT", 0.05, maker=0.0, taker=5.0)})
    eng = FundingEngine(venues=[a, b])
    c = [o for o in await eng.scan() if o.legs == "cross_venue"][0]

    year, hold = 365.0 * 86400.0, 1_209_600.0
    rt_fees_apr = (2.0 * (4.0 + 5.0) / 1e4) * (year / hold)   # taker fallback
    buffer_apr  = (1.5 / 1e4) * (year / hold)
    assert c.projected_net_apr == pytest.approx(0.35 - rt_fees_apr - buffer_apr)


# ─────────────────────────────────────────────────────────────────────────
# 4. would_enter flips around FUNDING_MIN_APR (via agent logging)
# ─────────────────────────────────────────────────────────────────────────

def _cross_opp(net_apr: float, depth_ok: bool = True) -> FundingOpportunity:
    return FundingOpportunity(
        symbol="SOL/USDT", variant="delta_neutral",
        venue_long="hyperliquid", venue_short="binance",
        funding_apr=0.40, spread_apr=0.40, oi_usd=5e6, depth_ok=depth_ok,
        legs="cross_venue", projected_net_apr=net_apr,
        funding_interval_sec=3600.0, taker_fee_bps=9.0, maker_fee_bps=3.0,
        funding_apr_long=0.05, funding_apr_short=0.45,
    )


@pytest.mark.asyncio
async def test_would_enter_true_above_floor(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_MIN_APR", 0.06)
    captured: list = []
    monkeypatch.setattr(
        "agents.funding_arb_agent.db_queries.save_funding_observations",
        lambda rows: captured.extend(rows),
    )
    agent = FundingArbAgent(engine=FundingEngine(venues=[]))
    await agent._log_observation(_cross_opp(net_apr=0.20))
    assert len(captured) == 1
    row = captured[0]
    assert row["would_enter"] is True
    assert row["projected_net_apr"] == pytest.approx(0.20)
    assert row["observation_only"] is True


@pytest.mark.asyncio
async def test_would_enter_false_below_floor(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_MIN_APR", 0.06)
    captured: list = []
    monkeypatch.setattr(
        "agents.funding_arb_agent.db_queries.save_funding_observations",
        lambda rows: captured.extend(rows),
    )
    agent = FundingArbAgent(engine=FundingEngine(venues=[]))
    await agent._log_observation(_cross_opp(net_apr=0.02))    # 2% < 6% floor
    row = captured[0]
    assert row["would_enter"] is False
    assert row["skip_reason"] == "below_net_apr_floor"


# ─────────────────────────────────────────────────────────────────────────
# 5. OBSERVATION ONLY — nothing routed; single rows kept; flag honoured
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cross_venue_logging_routes_no_orders(monkeypatch):
    save_trade = MagicMock()
    monkeypatch.setattr("execution.funding_engine.db_queries.save_trade", save_trade)
    monkeypatch.setattr(
        "agents.funding_arb_agent.db_queries.save_funding_observations",
        lambda rows: None,
    )
    agent = FundingArbAgent(engine=FundingEngine(venues=[]))
    await agent._log_observation(_cross_opp(net_apr=0.20))
    assert save_trade.call_count == 0


@pytest.mark.asyncio
async def test_single_and_cross_rows_both_present():
    # Both single-venue rates above the 0.06 floor so both survive _filter;
    # the cross row is built from the same snapshot regardless.
    a = FakeVenue("venue_a", {"SOL/USDT": _q("venue_a", "SOL/USDT", 0.30)})
    b = FakeVenue("venue_b", {"SOL/USDT": _q("venue_b", "SOL/USDT", 0.08)})
    eng = FundingEngine(venues=[a, b])
    opps = await eng.scan()
    legs = sorted({o.legs for o in opps})
    assert legs == ["cross_venue", "single"]
    # Two single rows (one per venue) + one cross row.
    assert sum(o.legs == "single" for o in opps) == 2
    assert sum(o.legs == "cross_venue" for o in opps) == 1


@pytest.mark.asyncio
async def test_cross_venue_disabled_flag(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_CROSS_VENUE_ENABLED", False)
    a = FakeVenue("venue_a", {"SOL/USDT": _q("venue_a", "SOL/USDT", 0.30)})
    b = FakeVenue("venue_b", {"SOL/USDT": _q("venue_b", "SOL/USDT", 0.05)})
    eng = FundingEngine(venues=[a, b])
    cross = [o for o in await eng.scan() if o.legs == "cross_venue"]
    assert cross == []
