"""
tests/test_data_sources.py

Covers the data_sources plugin pattern end-to-end:

  - BaseDataSource cache + staleness behaviour
  - Each registered source's fetch_all / list_metrics / is_available
  - Aggregator: refresh_all concurrency, get(), latest_snapshot
  - Plugin compliance: aggregator never imports concrete classes;
    adding a new source via REGISTERED_SOURCES picks it up automatically
  - Pub/sub: exact + wildcard pattern matching, filters, multi-sub,
    callback exception isolation, unsubscribe
  - Quality gate integration: VIX, DXY, yield-curve modifiers
  - DB integration: log_data_point + get_data_history/latest/at_time
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import patch

import pytest

from data_sources import DataSources, DataPoint, BaseDataSource
from data_sources.sources.alpha_vantage import AlphaVantageSource
from data_sources.sources.coingecko     import CoinGeckoSource
from data_sources.sources.coinglass     import CoinglassSource
from data_sources.sources.fred          import FREDSource
from data_sources.sources.frankfurter   import FrankfurterSource


# ─────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────

class StubSource(BaseDataSource):
    """In-memory source for aggregator + pub/sub tests."""
    source_id        = "stub"
    display_name     = "Stub"
    refresh_interval = 0     # always considered stale unless overridden
    optional         = True

    def __init__(self, points=None):
        super().__init__()
        self._points = list(points or [])
        self.fetch_calls = 0

    def list_metrics(self) -> list[str]:
        return sorted({p.metric for p in self._points})

    async def fetch_all(self) -> list[DataPoint]:
        self.fetch_calls += 1
        # Render DataPoints under the source's *current* source_id — tests
        # that rename source_id after construction need the cache keys to
        # match cached_value() lookups.
        return [DataPoint(
            source_id=self.source_id, metric=p.metric, symbol=p.symbol,
            value=p.value, raw_data=dict(p.raw_data), error=p.error,
        ) for p in self._points]

    def set_points(self, points):
        self._points = list(points)


def _pt(metric: str, value: float, symbol=None, error=None) -> DataPoint:
    return DataPoint(
        source_id="stub", metric=metric, value=value,
        symbol=symbol, error=error,
    )


# ─────────────────────────────────────────────────────────────────────────
# BaseDataSource — cache, refresh, error fallback
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_base_caches_within_refresh_interval():
    s = StubSource([_pt("vix", 18.0)])
    s.refresh_interval = 999

    p1 = await s.get("vix")
    p2 = await s.get("vix")
    assert p1.value == 18.0 and p2.value == 18.0
    # Second get must reuse the cache
    assert s.fetch_calls == 1


@pytest.mark.asyncio
async def test_base_refreshes_when_stale():
    s = StubSource([_pt("vix", 18.0)])
    s.refresh_interval = 0  # always stale

    await s.get("vix")
    s.set_points([_pt("vix", 22.0)])
    p = await s.get("vix")
    assert p.value == 22.0
    assert s.fetch_calls == 2


@pytest.mark.asyncio
async def test_base_fetch_failure_keeps_cached_value():
    s = StubSource([_pt("vix", 18.0)])
    s.refresh_interval = 0

    await s.get("vix")
    # Force fetch_all to blow up — base must swallow and keep the cache.
    async def boom(): raise RuntimeError("network down")
    s.fetch_all = boom
    p = await s.get("vix")
    assert p is not None and p.value == 18.0


@pytest.mark.asyncio
async def test_cached_value_is_sync():
    s = StubSource([_pt("vix", 18.0)])
    s.refresh_interval = 0
    await s.get("vix")
    assert s.cached_value("vix") == 18.0
    assert s.cached_value("missing", default=1.23) == 1.23


def test_datapoint_key_with_and_without_symbol():
    assert DataPoint("s", "m", 1.0).key == "s.m"
    assert DataPoint("s", "m", 1.0, symbol="BTC/USDT").key == "s.m.BTC/USDT"


# ─────────────────────────────────────────────────────────────────────────
# Registered sources — list_metrics + is_available contracts
# ─────────────────────────────────────────────────────────────────────────

def test_frankfurter_is_always_available():
    # No key, no rate limit — must be available unconditionally.
    assert FrankfurterSource().is_available() is True


def test_alpha_vantage_requires_key(monkeypatch):
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    assert AlphaVantageSource().is_available() is False
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "x")
    assert AlphaVantageSource().is_available() is True


def test_fred_requires_key(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    assert FREDSource().is_available() is False
    monkeypatch.setenv("FRED_API_KEY", "x")
    assert FREDSource().is_available() is True


def test_coinglass_no_key_required():
    assert CoinglassSource().is_available() is True


def test_coingecko_works_without_key():
    """Public tier means available even with no key set."""
    assert CoinGeckoSource().is_available() is True


def test_coingecko_auth_picks_right_header(monkeypatch):
    """Empty env → no auth header. Demo key → x-cg-demo-api-key on the
    public base. Pro flag flipped → x-cg-pro-api-key on the pro base."""
    s = CoinGeckoSource()
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    monkeypatch.setattr("config.settings.COINGECKO_USE_PRO", False)
    base, headers = s._auth()
    assert base.endswith(".coingecko.com/api/v3")
    assert "pro" not in base
    assert headers == {}

    monkeypatch.setenv("COINGECKO_API_KEY", "CG-demo-test")
    base, headers = s._auth()
    assert "pro-api" not in base
    assert headers == {"x-cg-demo-api-key": "CG-demo-test"}

    monkeypatch.setattr("config.settings.COINGECKO_USE_PRO", True)
    base, headers = s._auth()
    assert "pro-api" in base
    assert headers == {"x-cg-pro-api-key": "CG-demo-test"}


def test_coingecko_metrics_and_convenience_methods():
    s = CoinGeckoSource()
    assert "btc_dominance" in s.list_metrics()
    assert "eth_dominance" in s.list_metrics()
    assert "total_market_cap" in s.list_metrics()

    # Empty cache → all None.
    assert s.get_btc_dominance() is None
    assert s.get_total_market_cap() is None

    s._cache[s._make_key("btc_dominance")] = DataPoint(
        "coingecko", "btc_dominance", 55.4,
    )
    assert s.get_btc_dominance() == 55.4


@pytest.mark.asyncio
async def test_coingecko_parses_global_payload(monkeypatch):
    """Wire a fake aiohttp response into the source and verify it shapes
    the /global JSON into the expected DataPoints."""
    sample = {
        "data": {
            "market_cap_percentage": {"btc": 58.2, "eth": 9.6, "sol": 2.1},
            "total_market_cap":      {"usd": 2_662_000_000_000},
            "total_volume":          {"usd": 79_000_000_000},
            "market_cap_change_percentage_24h_usd": 0.13,
        }
    }

    class _FakeResp:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def json(self, content_type=None): return sample

    class _FakeSession:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, url): return _FakeResp()

    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)

    s = CoinGeckoSource()
    points = await s.fetch_all()
    by_metric = {p.metric: p for p in points}
    assert by_metric["btc_dominance"].value == 58.2
    assert by_metric["eth_dominance"].value == 9.6
    assert by_metric["total_market_cap"].value == 2_662_000_000_000
    assert by_metric["total_volume_24h"].value == 79_000_000_000
    assert by_metric["market_cap_change_pct_24h"].value == 0.13
    # No DataPoint should carry an error on a happy-path response.
    assert all(p.error is None for p in points)


@pytest.mark.asyncio
async def test_coingecko_returns_error_point_on_empty_payload(monkeypatch):
    """Status-message responses (rate-limited, bad key, etc.) must be
    surfaced as a DataPoint with .error set — never propagate."""
    sample = {"status": {"error_message": "rate_limit_exceeded"}}

    class _FakeResp:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def json(self, content_type=None): return sample

    class _FakeSession:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, url): return _FakeResp()

    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)

    s = CoinGeckoSource()
    points = await s.fetch_all()
    assert len(points) == 1
    assert points[0].error and "rate_limit" in points[0].error


def test_each_source_lists_expected_metrics():
    av = AlphaVantageSource().list_metrics()
    assert "spy" in av
    assert "vix" not in av   # moved to FRED

    fr = FrankfurterSource().list_metrics()
    assert "dxy" in fr
    assert "fx_rate" in fr

    cg = CoinglassSource().list_metrics()
    for m in ("funding_rate", "open_interest", "long_short_ratio"):
        assert m in cg

    fred = FREDSource().list_metrics()
    for m in ("cpi", "yield_10y", "fed_funds", "vix"):
        assert m in fred


# ─────────────────────────────────────────────────────────────────────────
# Source convenience accessors against a primed cache
# ─────────────────────────────────────────────────────────────────────────

def test_fred_risk_sentiment_thresholds():
    """VIX lives on FRED (VIXCLS), not Alpha Vantage. Risk-regime
    classifier lives next to the data it reads from."""
    s = FREDSource()
    # Empty cache → UNKNOWN
    assert s.get_risk_sentiment() == "UNKNOWN"

    s._cache[s._make_key("vix")] = DataPoint("fred", "vix", 10.0)
    assert s.get_risk_sentiment() == "RISK_ON"

    s._cache[s._make_key("vix")] = DataPoint("fred", "vix", 20.0)
    assert s.get_risk_sentiment() == "NEUTRAL"

    s._cache[s._make_key("vix")] = DataPoint("fred", "vix", 28.0)
    assert s.get_risk_sentiment() == "RISK_OFF"

    s._cache[s._make_key("vix")] = DataPoint("fred", "vix", 40.0)
    assert s.get_risk_sentiment() == "CRISIS"


def test_alpha_vantage_does_not_carry_vix():
    """VIX intentionally not in Alpha Vantage's metric list — its
    GLOBAL_QUOTE endpoint returns empty for non-tradeable indices."""
    s = AlphaVantageSource()
    assert "vix" not in s.list_metrics()
    assert not hasattr(s, "get_vix")
    assert not hasattr(s, "get_risk_sentiment")


@pytest.mark.asyncio
async def test_alpha_vantage_paces_calls_between_symbols(monkeypatch):
    """Free tier blocks >1 call/sec. Verify the source sleeps between
    consecutive symbol fetches (but not before the first one)."""
    sleeps: list[float] = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("data_sources.sources.alpha_vantage.asyncio.sleep", _fake_sleep)
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "test-key")
    monkeypatch.setattr("config.settings.ALPHA_VANTAGE_SYMBOLS", ["SPY", "QQQ", "GLD"])
    monkeypatch.setattr("config.settings.ALPHA_VANTAGE_PACE_SEC", 1.3)

    # Fake the HTTP layer — return a minimal valid GLOBAL_QUOTE payload.
    sample = {"Global Quote": {"05. price": "1.0", "10. change percent": "0.5%"}}

    class _FakeResp:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def json(self, content_type=None): return sample

    class _FakeSession:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, url, params=None): return _FakeResp()

    monkeypatch.setattr(
        "data_sources.sources.alpha_vantage.aiohttp.ClientSession",
        _FakeSession,
    )

    s = AlphaVantageSource()
    await s.fetch_all()

    # 3 symbols → 2 inter-call sleeps of PACE_SEC each.
    assert sleeps == [1.3, 1.3]


def test_frankfurter_dxy_strength():
    s = FrankfurterSource()
    assert s.is_dxy_strong() is False  # empty cache
    s._cache[s._make_key("dxy")] = DataPoint("frankfurter", "dxy", 108.0)
    assert s.is_dxy_strong() is True
    s._cache[s._make_key("dxy")] = DataPoint("frankfurter", "dxy", 90.0)
    assert s.is_dxy_strong() is False
    assert s.is_dxy_weak() is True


def test_frankfurter_dxy_proxy_formula():
    """Geometric ICE-style DXY: 50.14348112 × Π rate^exp.

    With a snapshot near real-world May 2026 rates we expect ~99 — the
    earlier linear-weighted-sum formula returned 2236, off by 20x. Keep
    a tolerance band wide enough to absorb the missing index reset but
    tight enough to catch a regression to the old broken implementation.
    """
    s = FrankfurterSource()
    snapshot = {
        "EUR/USD": 1.16,
        "USD/JPY": 159.03,
        "GBP/USD": 1.34,
        "USD/CAD": 1.38,
        "USD/SEK": 9.38,
        "USD/CHF": 0.79,
    }
    dxy = s._dxy_proxy(snapshot)
    assert dxy is not None
    assert 95 < dxy < 105, f"DXY proxy {dxy:.2f} not in expected band 95–105"


def test_frankfurter_dxy_proxy_missing_leg_returns_none():
    """Any missing leg → None (caller renders dim '—' rather than NaN)."""
    s = FrankfurterSource()
    assert s._dxy_proxy({"EUR/USD": 1.16}) is None
    assert s._dxy_proxy({}) is None


def test_fred_yield_curve_inversion_falls_back_to_legs():
    s = FREDSource()
    assert s.is_yield_curve_inverted() is False
    # Direct spread missing — fall back to 10y - 2y subtraction.
    s._cache[s._make_key("yield_10y")] = DataPoint("fred", "yield_10y", 3.5)
    s._cache[s._make_key("yield_2y")]  = DataPoint("fred", "yield_2y",  4.8)
    assert s.is_yield_curve_inverted() is True


# ─────────────────────────────────────────────────────────────────────────
# Aggregator: refresh, get, snapshot, isolation
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aggregator_refresh_all_succeeds_concurrently():
    a = StubSource([_pt("vix", 18.0)]); a.source_id = "a"
    b = StubSource([_pt("dxy", 105.0)]); b.source_id = "b"
    # Re-init caches now that source_id changed (key uses source_id).
    a._cache.clear(); b._cache.clear()

    agg = DataSources(sources=[a, b])
    with patch("database.queries.log_data_point") as log:
        await agg.refresh_all()
    assert a.fetch_calls == 1 and b.fetch_calls == 1
    assert log.call_count >= 2


@pytest.mark.asyncio
async def test_aggregator_isolates_one_failing_source():
    good = StubSource([_pt("vix", 18.0)])
    good.source_id = "good"; good._cache.clear()

    class Bad(BaseDataSource):
        source_id = "bad"
        refresh_interval = 0
        def list_metrics(self): return []
        async def fetch_all(self): raise RuntimeError("nope")

    bad = Bad()
    agg = DataSources(sources=[good, bad])
    with patch("database.queries.log_data_point"):
        await agg.refresh_all()
    # Good still cached, bad silently skipped.
    assert good.cached_value("vix") == 18.0


@pytest.mark.asyncio
async def test_aggregator_unavailable_source_is_skipped():
    class Unavailable(BaseDataSource):
        source_id = "off"
        refresh_interval = 0
        def list_metrics(self): return []
        def is_available(self): return False
        async def fetch_all(self):
            raise AssertionError("must not be called when unavailable")

    agg = DataSources(sources=[Unavailable()])
    with patch("database.queries.log_data_point"):
        await agg.refresh_all()


@pytest.mark.asyncio
async def test_aggregator_get_returns_datapoint():
    s = StubSource([_pt("vix", 18.0)]); s.source_id = "av"; s._cache.clear()
    agg = DataSources(sources=[s])
    setattr(agg, "av", s)  # rewire the attribute since source_id changed post-init
    p = await agg.get("av", "vix")
    assert p is not None and p.value == 18.0
    assert await agg.get("missing", "x") is None


@pytest.mark.asyncio
async def test_latest_snapshot_flattens_every_cache():
    a = StubSource([_pt("vix", 18.0)]); a.source_id = "a"; a._cache.clear()
    b = StubSource([_pt("dxy", 100.0)]); b.source_id = "b"; b._cache.clear()
    agg = DataSources(sources=[a, b])
    with patch("database.queries.log_data_point"):
        await agg.refresh_all()
    snap = agg.latest_snapshot()
    assert "a.vix" in snap and "b.dxy" in snap


# ─────────────────────────────────────────────────────────────────────────
# Plugin pattern compliance
# ─────────────────────────────────────────────────────────────────────────

def test_aggregator_never_imports_concrete_sources():
    """data_sources/__init__.py must not name any concrete source class.
    The registry layer (data_sources/sources/__init__.py) is the only
    file that imports concrete classes."""
    import inspect, data_sources
    src = inspect.getsource(data_sources)
    for klass in ("Coinglass", "CoinGecko", "FRED", "AlphaVantage", "Frankfurter"):
        assert klass not in src, f"aggregator must not reference {klass}"


def test_registered_sources_picked_up_automatically():
    """A new source instance dropped into REGISTERED_SOURCES is exposed
    by the aggregator with no aggregator-side changes."""
    extra = StubSource([_pt("hello", 1.0)])
    extra.source_id = "extra"; extra._cache.clear()

    agg = DataSources(sources=[extra])
    assert getattr(agg, "extra") is extra


# ─────────────────────────────────────────────────────────────────────────
# Pub/sub
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_fires_on_value_change():
    s = StubSource([_pt("vix", 18.0)])
    s.source_id = "av"; s._cache.clear()
    agg = DataSources(sources=[s])

    received = []
    def cb(new, prev): received.append((new.value, prev))
    agg.subscribe("av.vix", cb)

    with patch("database.queries.log_data_point"):
        await agg.refresh_all()
    # Allow the create_task fan-out to drain.
    await asyncio.sleep(0)
    assert received and received[0][0] == 18.0 and received[0][1] is None

    s.set_points([_pt("vix", 25.0)])
    with patch("database.queries.log_data_point"):
        await agg.refresh_all()
    await asyncio.sleep(0)
    assert received[-1][0] == 25.0
    assert received[-1][1] is not None  # previous_point now populated


@pytest.mark.asyncio
async def test_subscribe_wildcard_matches_any_symbol():
    s = StubSource([
        _pt("funding_rate", 0.0001, symbol="BTC/USDT"),
        _pt("funding_rate", 0.0002, symbol="ETH/USDT"),
    ])
    s.source_id = "cg"; s._cache.clear()
    agg = DataSources(sources=[s])

    hits = []
    agg.subscribe("cg.funding_rate.*", lambda new, prev: hits.append(new.key))
    with patch("database.queries.log_data_point"):
        await agg.refresh_all()
    await asyncio.sleep(0)
    assert set(hits) == {"cg.funding_rate.BTC/USDT", "cg.funding_rate.ETH/USDT"}


@pytest.mark.asyncio
async def test_subscribe_filter_blocks_callback():
    s = StubSource([_pt("vix", 18.0)]); s.source_id = "av"; s._cache.clear()
    agg = DataSources(sources=[s])
    fired = []
    agg.subscribe("av.vix", lambda new, prev: fired.append(new.value),
                  filter=lambda dp: dp.value >= 30)
    with patch("database.queries.log_data_point"):
        await agg.refresh_all()
    await asyncio.sleep(0)
    assert fired == []  # 18 < 30

    s.set_points([_pt("vix", 35.0)])
    with patch("database.queries.log_data_point"):
        await agg.refresh_all()
    await asyncio.sleep(0)
    assert fired == [35.0]


@pytest.mark.asyncio
async def test_callback_exception_does_not_break_source():
    s = StubSource([_pt("vix", 18.0)]); s.source_id = "av"; s._cache.clear()
    agg = DataSources(sources=[s])
    safe = []
    agg.subscribe("av.vix", lambda new, prev: (_ for _ in ()).throw(RuntimeError("boom")))
    agg.subscribe("av.vix", lambda new, prev: safe.append(new.value))
    with patch("database.queries.log_data_point"):
        await agg.refresh_all()
    await asyncio.sleep(0)
    # The throwing callback must not prevent the safe one from firing.
    assert safe == [18.0]


@pytest.mark.asyncio
async def test_unsubscribe_stops_notifications():
    s = StubSource([_pt("vix", 18.0)]); s.source_id = "av"; s._cache.clear()
    agg = DataSources(sources=[s])
    received = []
    sub_id = agg.subscribe("av.vix", lambda new, prev: received.append(new.value))
    with patch("database.queries.log_data_point"):
        await agg.refresh_all()
    await asyncio.sleep(0)
    assert received == [18.0]

    assert agg.unsubscribe(sub_id) is True
    s.set_points([_pt("vix", 25.0)])
    with patch("database.queries.log_data_point"):
        await agg.refresh_all()
    await asyncio.sleep(0)
    assert received == [18.0]


@pytest.mark.asyncio
async def test_multiple_subscribers_same_pattern():
    s = StubSource([_pt("vix", 18.0)]); s.source_id = "av"; s._cache.clear()
    agg = DataSources(sources=[s])
    a, b = [], []
    agg.subscribe("av.vix", lambda new, prev: a.append(new.value))
    agg.subscribe("av.vix", lambda new, prev: b.append(new.value))
    with patch("database.queries.log_data_point"):
        await agg.refresh_all()
    await asyncio.sleep(0)
    assert a == [18.0] and b == [18.0]


# ─────────────────────────────────────────────────────────────────────────
# Quality gate integration
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def patch_macro_into_gate(monkeypatch):
    """Inject the module-level data_sources singleton with controllable
    state for quality_gate's macro modifier path."""
    from data_sources import data_sources as ds

    # Stash + reset relevant caches so test ordering is independent.
    fr_key  = ds.frankfurter._make_key("dxy")
    fred_keys = [
        ds.fred._make_key("vix"),
        ds.fred._make_key("yield_curve_spread"),
        ds.fred._make_key("yield_10y"),
        ds.fred._make_key("yield_2y"),
    ]
    saved = {
        fr_key: ds.frankfurter._cache.pop(fr_key, None),
    }
    for k in fred_keys:
        saved[k] = ds.fred._cache.pop(k, None)
    yield ds
    # Restore so other tests aren't perturbed.
    for k, v in saved.items():
        for src in (ds.frankfurter, ds.fred):
            cache = src._cache
            if v is None:
                cache.pop(k, None)
            elif k.startswith(src.source_id + "."):
                cache[k] = v


def _make_signal(direction="long"):
    from signals.base import Signal
    # rsi populated to dodge a latent bug in Signal.summary() where it
    # tries to format a None rsi with .0f. Not the system under test.
    return Signal(
        pair="BTC/USDT", signal_type="momentum", direction=direction,
        exchange="binance", raw_score=80.0, rsi=50.0,
    )


@pytest.mark.asyncio
async def test_quality_gate_applies_crisis_vix_penalty(patch_macro_into_gate):
    ds = patch_macro_into_gate
    ds.fred._cache[ds.fred._make_key("vix")] = \
        DataPoint("fred", "vix", 40.0)

    from signals.quality_gate import QualityGate
    from config import settings
    g = QualityGate()
    # Bypass regime/guards/dedup paths by patching them away.
    with patch("signals.quality_gate.regime_detector.get_primary", return_value=None), \
         patch("signals.quality_gate.guard_runner.apply_all", return_value=(0.0, [])), \
         patch("signals.ofi.ofi_scorer.get_best", return_value=None):
        s = _make_signal()
        s.tf_5m = s.tf_15m = s.tf_1h = True
        passed, reasons, score = g.evaluate(s, open_positions=[], active_signals=[])

    # Score should be reduced by the crisis penalty.
    assert score <= 80.0 + settings.MACRO_CRISIS_PENALTY


@pytest.mark.asyncio
async def test_quality_gate_strong_dxy_penalises_longs(patch_macro_into_gate):
    ds = patch_macro_into_gate
    ds.frankfurter._cache[ds.frankfurter._make_key("dxy")] = \
        DataPoint("frankfurter", "dxy", 110.0)

    from signals.quality_gate import QualityGate
    from config import settings
    g = QualityGate()
    with patch("signals.quality_gate.regime_detector.get_primary", return_value=None), \
         patch("signals.quality_gate.guard_runner.apply_all", return_value=(0.0, [])), \
         patch("signals.ofi.ofi_scorer.get_best", return_value=None):
        s = _make_signal(direction="long")
        s.tf_5m = s.tf_15m = s.tf_1h = True
        _, _, score_long = g.evaluate(s, [], [])

        s2 = _make_signal(direction="short")
        s2.tf_5m = s2.tf_15m = s2.tf_1h = True
        _, _, score_short = g.evaluate(s2, [], [])

    # Long takes the DXY penalty; short does not.
    assert score_long < score_short


@pytest.mark.asyncio
async def test_quality_gate_yield_inversion_penalty(patch_macro_into_gate):
    ds = patch_macro_into_gate
    ds.fred._cache[ds.fred._make_key("yield_curve_spread")] = \
        DataPoint("fred", "yield_curve_spread", -0.5)

    from signals.quality_gate import QualityGate
    from config import settings
    g = QualityGate()
    with patch("signals.quality_gate.regime_detector.get_primary", return_value=None), \
         patch("signals.quality_gate.guard_runner.apply_all", return_value=(0.0, [])), \
         patch("signals.ofi.ofi_scorer.get_best", return_value=None):
        s = _make_signal()
        s.tf_5m = s.tf_15m = s.tf_1h = True
        _, _, score = g.evaluate(s, [], [])

    assert score <= 80.0 + settings.MACRO_YIELD_INVERTED_PENALTY


@pytest.mark.asyncio
async def test_quality_gate_missing_macro_data_does_not_block(patch_macro_into_gate):
    # All caches empty by fixture — gate must still pass purely on
    # technical merit (no macro adjustment applied).
    from signals.quality_gate import QualityGate
    g = QualityGate()
    with patch("signals.quality_gate.regime_detector.get_primary", return_value=None), \
         patch("signals.quality_gate.guard_runner.apply_all", return_value=(0.0, [])), \
         patch("signals.ofi.ofi_scorer.get_best", return_value=None):
        s = _make_signal()
        s.tf_5m = s.tf_15m = s.tf_1h = True
        passed, _, _ = g.evaluate(s, [], [])
    assert passed is True


# ─────────────────────────────────────────────────────────────────────────
# DB integration
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    """Spin up a tmp SQLite DB so log_data_point + history queries hit a
    real engine. Cleans up after each test."""
    db_file = tmp_path / "test_ds.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_file)

    # Rebuild the engine + session factory against the new path.
    import importlib, database.db, database.queries
    importlib.reload(database.db)
    importlib.reload(database.queries)
    database.db.init_db()
    yield database.queries


def test_log_data_point_round_trip(temp_db):
    q = temp_db
    p = DataPoint("alpha_vantage", "vix", 22.5, raw_data={"symbol": "VIX"})
    q.log_data_point(p)

    latest = q.get_latest_data_point("alpha_vantage", "vix")
    assert latest is not None
    assert latest.value == 22.5
    assert latest.source_id == "alpha_vantage"


def test_get_data_history_returns_chronological(temp_db):
    q = temp_db
    for v in (18.0, 19.0, 20.0):
        q.log_data_point(DataPoint("alpha_vantage", "vix", v))

    rows = q.get_data_history("alpha_vantage", "vix", hours=24)
    assert [r.value for r in rows] == [18.0, 19.0, 20.0]


def test_get_data_at_time(temp_db):
    q = temp_db
    # Insert two rows; ensure get_data_at_time picks the one before the cutoff.
    import time
    p1 = DataPoint("fred", "cpi", 100.0); p1.timestamp = time.time() - 7200
    p2 = DataPoint("fred", "cpi", 110.0); p2.timestamp = time.time() - 60
    q.log_data_point(p1); q.log_data_point(p2)

    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    row = q.get_data_at_time("fred", "cpi", one_hour_ago)
    assert row is not None
    assert row.value == 100.0
