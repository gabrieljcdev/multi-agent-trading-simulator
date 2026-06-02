"""
tests/test_funding_venues.py — funding-venue plugin layer (Phase 1).

Covers the BaseFundingVenue contract, the Binance plugin as a regression
lock against the engine's former _fetch_native_funding, the Hyperliquid
plugin's bulk-fetch / rate-limit / symbol-resolution behaviour, the
is_available() gating, and the OBSERVATION-ONLY no-signing guarantee
(there is no order-placement surface anywhere in execution/funding_venues).

Network is always mocked — every test injects a ccxt stub via the venue's
ccxt_factory, so no test touches a live endpoint.
"""

from __future__ import annotations

import inspect

import pytest
from unittest.mock import AsyncMock, MagicMock

from config import settings
from execution.funding_venues import (
    REGISTERED_FUNDING_VENUES,
    BaseFundingVenue,
    BinanceFundingVenue,
    HyperliquidFundingVenue,
    FundingQuote,
)
from execution.funding_venues.base_funding import annualise_funding, SECONDS_PER_YEAR


# ─────────────────────────────────────────────────────────────────────────
# Stubs
# ─────────────────────────────────────────────────────────────────────────

def _binance_stub(rate_8h: float = 0.0001, oi_usd: float | None = None) -> MagicMock:
    ex = MagicMock()
    ex.fetch_funding_rate = AsyncMock(return_value={"fundingRate": rate_8h})
    if oi_usd is not None:
        ex.fetch_open_interest = AsyncMock(
            return_value={"openInterestAmount": float(oi_usd)},
        )
    else:
        if hasattr(ex, "fetch_open_interest"):
            del ex.fetch_open_interest
    ex.close = AsyncMock()
    return ex


def _hl_stub(
    rate_1h: float = 0.0001,
    *,
    markets: dict | None = None,
    funding: dict | None = None,
    oi: dict | None = None,
    raise_funding: Exception | None = None,
) -> MagicMock:
    """Hyperliquid ccxt stub. Defaults expose BTC + ETH swaps."""
    if markets is None:
        markets = {
            "BTC/USDC:USDC": {"swap": True, "base": "BTC", "quote": "USDC"},
            "ETH/USDC:USDC": {"swap": True, "base": "ETH", "quote": "USDC"},
        }
    if funding is None:
        funding = {
            "BTC/USDC:USDC": {"fundingRate": rate_1h, "markPrice": 60000.0,
                              "indexPrice": 60010.0},
            "ETH/USDC:USDC": {"fundingRate": rate_1h, "markPrice": 3000.0,
                              "indexPrice": 3001.0},
        }
    if oi is None:
        oi = {
            "BTC/USDC:USDC": {"openInterestValue": 5_000_000.0},
            "ETH/USDC:USDC": {"openInterestValue": 2_000_000.0},
        }
    ex = MagicMock()
    ex.load_markets = AsyncMock(return_value=markets)
    if raise_funding is not None:
        ex.fetch_funding_rates = AsyncMock(side_effect=raise_funding)
    else:
        ex.fetch_funding_rates = AsyncMock(return_value=funding)
    ex.fetch_open_interests = AsyncMock(return_value=oi)
    ex.close = AsyncMock()
    return ex


# ─────────────────────────────────────────────────────────────────────────
# annualise_funding helper
# ─────────────────────────────────────────────────────────────────────────

def test_annualise_funding_binance_8h_is_1095():
    """8h interval annualises a rate to rate*1095 — the Phase-1 constant."""
    assert annualise_funding(0.0001, 8 * 3600) == pytest.approx(0.0001 * 1095.0)


def test_annualise_funding_hyperliquid_1h_is_8760():
    """1h interval annualises to rate*8760."""
    assert annualise_funding(0.0001, 3600) == pytest.approx(0.0001 * 8760.0)


def test_annualise_funding_nonpositive_interval_is_zero():
    assert annualise_funding(0.01, 0) == 0.0
    assert annualise_funding(0.01, -5) == 0.0
    assert SECONDS_PER_YEAR == 365 * 24 * 3600


# ─────────────────────────────────────────────────────────────────────────
# BaseFundingVenue contract
# ─────────────────────────────────────────────────────────────────────────

def test_registry_populated_and_typed():
    assert len(REGISTERED_FUNDING_VENUES) >= 2
    for v in REGISTERED_FUNDING_VENUES:
        assert isinstance(v, BaseFundingVenue)
        assert isinstance(v.venue_id, str) and v.venue_id
    ids = [v.venue_id for v in REGISTERED_FUNDING_VENUES]
    assert "binance" in ids and "hyperliquid" in ids


def test_no_order_placement_surface_anywhere():
    """OBSERVATION-ONLY guarantee: no funding venue exposes any
    order-placement / signing method. If a live build ever adds one it must
    be a conscious, reviewed change that breaks this test."""
    banned = {
        "submit_swap", "create_order", "place_order", "open", "_place",
        "sign", "sign_transaction", "submit_order", "execute",
    }
    classes = [BaseFundingVenue, BinanceFundingVenue, HyperliquidFundingVenue]
    for cls in classes:
        names = {n for n, _ in inspect.getmembers(cls)}
        leaked = names & banned
        assert not leaked, f"{cls.__name__} exposes order-placement methods: {leaked}"


def test_venues_read_no_trading_key_env(monkeypatch):
    """Venues must never gate on or read a private/trading key. key_env_var,
    when set at all, is an OPTIONAL public rate-limit override — never a
    secret. Binance + Hyperliquid declare None."""
    for cls in (BinanceFundingVenue, HyperliquidFundingVenue):
        kev = cls.key_env_var
        assert kev is None or "SECRET" not in kev.upper()
        assert kev is None or "PRIVATE" not in kev.upper()


# ─────────────────────────────────────────────────────────────────────────
# BinanceFundingVenue — regression lock vs the old _fetch_native_funding
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_binance_fetch_funding_annualises_to_1095():
    v = BinanceFundingVenue(ccxt_factory=lambda: _binance_stub(rate_8h=0.0001))
    q = await v.fetch_funding("BTC/USDT")
    assert q.error is None
    assert q.venue == "binance"
    assert q.funding_interval_sec == 8 * 3600
    assert q.funding_apr == pytest.approx(0.0001 * 1095.0)


@pytest.mark.asyncio
async def test_binance_oi_absent_defaults_zero_and_depth_ok():
    """No fetch_open_interest on the stub → oi_usd 0.0, depth_ok True (the
    exact graceful fallback the engine relied on)."""
    v = BinanceFundingVenue(ccxt_factory=lambda: _binance_stub(oi_usd=None))
    q = await v.fetch_funding("BTC/USDT")
    assert q.oi_usd == 0.0
    assert q.depth_ok is True


@pytest.mark.asyncio
async def test_binance_oi_present_is_read():
    v = BinanceFundingVenue(ccxt_factory=lambda: _binance_stub(oi_usd=1_000_000.0))
    q = await v.fetch_funding("BTC/USDT")
    assert q.oi_usd == pytest.approx(1_000_000.0)


@pytest.mark.asyncio
async def test_binance_fetch_failure_returns_error_not_raise():
    ex = MagicMock()
    ex.fetch_funding_rate = AsyncMock(side_effect=RuntimeError("network"))
    v = BinanceFundingVenue(ccxt_factory=lambda: ex)
    q = await v.fetch_funding("BTC/USDT")
    assert q.funding_apr is None
    assert q.error is not None


def test_binance_client_uses_future_market_type():
    """Regression: defaultType must be future or fetch_funding_rate 500s."""
    pytest.importorskip("ccxt.async_support")
    v = BinanceFundingVenue()
    client = v._get_exchange()
    assert client is not None
    assert client.options.get("defaultType") == "future"


def test_binance_is_available_with_factory():
    assert BinanceFundingVenue(ccxt_factory=lambda: _binance_stub()).is_available() is True


# ─────────────────────────────────────────────────────────────────────────
# HyperliquidFundingVenue
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hl_fetch_funding_annualises_to_8760_and_resolves_base(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_HL_BULK_TTL_SEC", 30)
    v = HyperliquidFundingVenue(ccxt_factory=lambda: _hl_stub(rate_1h=0.0001))
    # Canonical "BTC/USDT" must resolve to HL's "BTC/USDC:USDC" by base coin.
    q = await v.fetch_funding("BTC/USDT")
    assert q.error is None
    assert q.venue == "hyperliquid"
    assert q.funding_interval_sec == 3600
    assert q.funding_apr == pytest.approx(0.0001 * 8760.0)
    assert q.oi_usd == pytest.approx(5_000_000.0)
    assert q.mark_price == pytest.approx(60000.0)


@pytest.mark.asyncio
async def test_hl_symbol_not_listed_returns_error(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_HL_BULK_TTL_SEC", 30)
    v = HyperliquidFundingVenue(ccxt_factory=lambda: _hl_stub())
    q = await v.fetch_funding("FARTCOIN/USDT")
    assert q.funding_apr is None
    assert q.error == "symbol_not_listed"


@pytest.mark.asyncio
async def test_hl_rate_limit_sets_error_and_does_not_raise(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_HL_BULK_TTL_SEC", 30)

    class RateLimitExceeded(Exception):
        pass

    v = HyperliquidFundingVenue(
        ccxt_factory=lambda: _hl_stub(raise_funding=RateLimitExceeded("429 too many")),
    )
    q = await v.fetch_funding("BTC/USDT")
    assert q.funding_apr is None
    assert q.error == "rate_limited"


@pytest.mark.asyncio
async def test_hl_bulk_cache_avoids_refetch_within_ttl(monkeypatch):
    """Two fetches inside the TTL → exactly one bulk funding call (rate-limit
    discipline: an N-symbol scan is not N network calls)."""
    monkeypatch.setattr(settings, "FUNDING_HL_BULK_TTL_SEC", 300)
    stub = _hl_stub()
    v = HyperliquidFundingVenue(ccxt_factory=lambda: stub)
    await v.fetch_funding("BTC/USDT")
    await v.fetch_funding("ETH/USDT")
    assert stub.fetch_funding_rates.await_count == 1


@pytest.mark.asyncio
async def test_hl_list_perps_returns_swap_universe(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_HL_BULK_TTL_SEC", 30)
    v = HyperliquidFundingVenue(ccxt_factory=lambda: _hl_stub())
    perps = await v.list_perps()
    assert "BTC/USDC:USDC" in perps and "ETH/USDC:USDC" in perps


def test_hl_is_available_with_factory():
    assert HyperliquidFundingVenue(ccxt_factory=lambda: _hl_stub()).is_available() is True
