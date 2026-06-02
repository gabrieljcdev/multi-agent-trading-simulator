"""
tests/test_funding_longtail.py — long-tail/HIP-3 discovery + openness signal
(Phase 3).

Covers:
  * discovery unions list_perps() with FUNDING_SYMBOLS and caps at
    FUNDING_MAX_DISCOVERED_PAIRS;
  * is_long_tail / is_hip3 / pair_age_days tagged from mocked metadata
    (null when absent — never guessed);
  * spread_decay_bps_per_day slope correctness on a fixture history;
  * crowding_verdict thresholds (OPEN / COMPRESSING / CROWDED) and UNKNOWN
    on short history.

Everything is observation-only; no network.
"""

from __future__ import annotations

import time

import pytest

from config import settings
from execution.funding_engine import FundingEngine
from execution.funding_venues.base_funding import BaseFundingVenue, FundingQuote
from execution import funding_openness as fo


# ─────────────────────────────────────────────────────────────────────────
# Fake venue with discovery surface
# ─────────────────────────────────────────────────────────────────────────

class FakeVenue(BaseFundingVenue):
    def __init__(self, venue_id, quotes, perps=None, meta=None):
        self.venue_id = venue_id
        self._quotes = quotes                 # symbol -> FundingQuote
        self._perps = perps or list(quotes)   # list of native symbols
        self._meta = meta or {}               # symbol -> {is_hip3, pair_age_days}

    async def fetch_funding(self, symbol):
        q = self._quotes.get(symbol)
        if q is None:
            return FundingQuote(venue=self.venue_id, symbol=symbol,
                                funding_apr=None, funding_interval_sec=3600,
                                error="symbol_not_listed")
        return q

    async def list_perps(self):
        return list(self._perps)

    async def get_market_meta(self, symbol):
        return self._meta.get(symbol, {"is_hip3": None, "pair_age_days": None})

    def is_available(self):
        return True


def _q(venue, symbol, apr, oi=1_000_000.0):
    return FundingQuote(venue=venue, symbol=symbol, funding_apr=apr,
                        funding_interval_sec=3600, oi_usd=oi, depth_ok=True,
                        taker_fee_bps=4.5, maker_fee_bps=1.5, ts=time.time())


@pytest.fixture(autouse=True)
def _longtail_settings(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_LONGTAIL_ENABLED", True)
    monkeypatch.setattr(settings, "FUNDING_CROSS_VENUE_ENABLED", False)
    monkeypatch.setattr(settings, "FUNDING_SYMBOLS", ["BTC/USDT"])
    monkeypatch.setattr(settings, "FUNDING_MIN_APR", 0.0)
    monkeypatch.setattr(settings, "FUNDING_MAX_DISCOVERED_PAIRS", 40)


# ─────────────────────────────────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discovery_unions_and_tags_long_tail():
    quotes = {
        "BTC/USDT":         _q("hl", "BTC/USDT", 0.10),        # curated
        "FART/USDC:USDC":   _q("hl", "FART/USDC:USDC", 0.40),  # long-tail
        "MOON/USDC:USDC":   _q("hl", "MOON/USDC:USDC", 0.30),  # long-tail
        "BTC/USDC:USDC":    _q("hl", "BTC/USDC:USDC", 0.11),   # base BTC = curated → excluded
    }
    v = FakeVenue("hl", quotes,
                  perps=["BTC/USDC:USDC", "FART/USDC:USDC", "MOON/USDC:USDC"])
    eng = FundingEngine(venues=[v])
    opps = await eng.scan()
    by_symbol = {o.symbol: o for o in opps}
    # curated BTC present and NOT long-tail
    assert by_symbol["BTC/USDT"].is_long_tail is False
    # discovered long-tail pairs present and flagged
    assert by_symbol["FART/USDC:USDC"].is_long_tail is True
    assert by_symbol["MOON/USDC:USDC"].is_long_tail is True
    # the BTC-base native perp was excluded as a curated-base duplicate
    assert "BTC/USDC:USDC" not in by_symbol


@pytest.mark.asyncio
async def test_discovery_caps_at_max_discovered_pairs(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_MAX_DISCOVERED_PAIRS", 1)
    quotes = {
        "BTC/USDT":       _q("hl", "BTC/USDT", 0.10),
        "FART/USDC:USDC": _q("hl", "FART/USDC:USDC", 0.40),
        "MOON/USDC:USDC": _q("hl", "MOON/USDC:USDC", 0.30),
    }
    v = FakeVenue("hl", quotes, perps=["FART/USDC:USDC", "MOON/USDC:USDC"])
    eng = FundingEngine(venues=[v])
    opps = await eng.scan()
    long_tail = [o for o in opps if o.is_long_tail]
    assert len(long_tail) == 1          # capped to a single discovered pair


@pytest.mark.asyncio
async def test_discovery_disabled_flag(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_LONGTAIL_ENABLED", False)
    quotes = {
        "BTC/USDT":       _q("hl", "BTC/USDT", 0.10),
        "FART/USDC:USDC": _q("hl", "FART/USDC:USDC", 0.40),
    }
    v = FakeVenue("hl", quotes, perps=["FART/USDC:USDC"])
    eng = FundingEngine(venues=[v])
    opps = await eng.scan()
    assert all(not o.is_long_tail for o in opps)
    assert all(o.symbol == "BTC/USDT" for o in opps)


@pytest.mark.asyncio
async def test_hip3_and_age_tagged_from_metadata_else_null():
    quotes = {
        "BTC/USDT":       _q("hl", "BTC/USDT", 0.10),
        "FART/USDC:USDC": _q("hl", "FART/USDC:USDC", 0.40),
        "PLAIN/USDC:USDC": _q("hl", "PLAIN/USDC:USDC", 0.20),
    }
    meta = {
        "FART/USDC:USDC": {"is_hip3": True, "pair_age_days": 9.0},
        # PLAIN has no metadata → must stay null, not guessed
    }
    v = FakeVenue("hl", quotes,
                  perps=["FART/USDC:USDC", "PLAIN/USDC:USDC"], meta=meta)
    eng = FundingEngine(venues=[v])
    by_symbol = {o.symbol: o for o in await eng.scan()}
    assert by_symbol["FART/USDC:USDC"].is_hip3 is True
    assert by_symbol["FART/USDC:USDC"].pair_age_days == pytest.approx(9.0)
    assert by_symbol["PLAIN/USDC:USDC"].is_hip3 is None
    assert by_symbol["PLAIN/USDC:USDC"].pair_age_days is None


# ─────────────────────────────────────────────────────────────────────────
# Openness pure helpers
# ─────────────────────────────────────────────────────────────────────────

def test_decay_positive_when_spread_compresses():
    # net APR falls 0.20 -> 0.18 over one day = 200 bps/day compression.
    samples = [(0.0, 0.20), (86400.0, 0.18)]
    assert fo.decay_bps_per_day(samples) == pytest.approx(200.0)


def test_decay_negative_when_spread_widens():
    samples = [(0.0, 0.18), (86400.0, 0.20)]
    assert fo.decay_bps_per_day(samples) == pytest.approx(-200.0)


def test_decay_zero_on_short_or_flat_history():
    assert fo.decay_bps_per_day([(0.0, 0.2)]) == 0.0
    assert fo.decay_bps_per_day([(5.0, 0.2), (5.0, 0.1)]) == 0.0    # no time span


def test_oi_growth_pct_24h():
    samples = [(0.0, 1_000_000.0), (86400.0, 1_500_000.0)]
    assert fo.oi_growth_pct_24h(samples) == pytest.approx(50.0)


def test_oi_growth_none_when_insufficient():
    assert fo.oi_growth_pct_24h([(0.0, 1e6)]) is None
    assert fo.oi_growth_pct_24h([(0.0, 0.0), (10.0, 5.0)]) is None   # zero baseline


# ─────────────────────────────────────────────────────────────────────────
# crowding_verdict thresholds
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def _verdict_thresholds(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_CROWDED_DECAY_BPS_DAY", 8.0)
    monkeypatch.setattr(settings, "FUNDING_OPEN_MAX_DECAY_BPS_DAY", 2.0)
    monkeypatch.setattr(settings, "FUNDING_YOUNG_PAIR_MAX_AGE_DAYS", 21.0)


def test_verdict_crowded(_verdict_thresholds):
    assert fo.classify_crowding(10.0, 5.0, 10.0, n_samples=5) == fo.CROWDED


def test_verdict_open_when_low_decay_young_growing(_verdict_thresholds):
    assert fo.classify_crowding(1.0, 5.0, 10.0, n_samples=5) == fo.OPEN


def test_verdict_open_with_unknown_age_and_oi(_verdict_thresholds):
    # Unknown signals don't contradict OPEN.
    assert fo.classify_crowding(1.0, None, None, n_samples=5) == fo.OPEN


def test_verdict_compressing_when_old_pair(_verdict_thresholds):
    # Low decay but an old pair → not a fresh OPEN window.
    assert fo.classify_crowding(1.0, 30.0, 10.0, n_samples=5) == fo.COMPRESSING


def test_verdict_compressing_mid_band(_verdict_thresholds):
    assert fo.classify_crowding(5.0, 5.0, 10.0, n_samples=5) == fo.COMPRESSING


def test_verdict_unknown_on_short_history(_verdict_thresholds):
    assert fo.classify_crowding(1.0, 5.0, 10.0, n_samples=2) == fo.UNKNOWN


# ─────────────────────────────────────────────────────────────────────────
# OpennessTracker integration + first-scan UNKNOWN
# ─────────────────────────────────────────────────────────────────────────

def test_tracker_unknown_then_classifies(monkeypatch, _verdict_thresholds):
    monkeypatch.setattr(settings, "FUNDING_DECAY_WINDOW_HOURS", 96)
    t = fo.OpennessTracker()
    # One sample → UNKNOWN (never fabricate OPEN).
    t.record("X", 0.0, 0.20, 1e6)
    assert t.snapshot("X").crowding_verdict == fo.UNKNOWN
    # Three samples, compressing fast → CROWDED.
    t.record("X", 86400.0, 0.10, 1e6)
    t.record("X", 172800.0, 0.02, 1e6)
    snap = t.snapshot("X", pair_age_days=5.0)
    assert snap.n_samples == 3
    assert snap.crowding_verdict == fo.CROWDED
    assert snap.spread_decay_bps_per_day > 0


def test_tracker_prunes_outside_window(monkeypatch, _verdict_thresholds):
    monkeypatch.setattr(settings, "FUNDING_DECAY_WINDOW_HOURS", 24)
    t = fo.OpennessTracker()
    t.record("X", 0.0, 0.20, 1e6)
    # A sample 48h later prunes everything older than 24h before it.
    t.record("X", 48 * 3600.0, 0.18, 1e6)
    assert t.snapshot("X").n_samples == 1


@pytest.mark.asyncio
async def test_first_scan_tags_unknown_verdict():
    v = FakeVenue("hl", {"BTC/USDT": _q("hl", "BTC/USDT", 0.10)}, perps=[])
    eng = FundingEngine(venues=[v])
    opps = await eng.scan()
    assert opps
    assert all(o.crowding_verdict == fo.UNKNOWN for o in opps)
