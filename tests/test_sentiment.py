"""
tests/test_sentiment.py — SentimentAggregator behaviour.

Real sources hit the network, so every test uses stub sources that
implement BaseSentimentSource directly. This pins behaviour at the
aggregator/contract level: composite math, hard-block propagation,
session floors, extensibility.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from config import settings
from sentiment import SentimentAggregator, SentimentData
from sentiment.base import BaseSentimentSource, SourceResult


# ─────────────────────────────────────────────────────────────────────────
# Stub sources
# ─────────────────────────────────────────────────────────────────────────

class StubSource(BaseSentimentSource):
    """Returns a pre-canned SourceResult. Used to control composite inputs."""

    def __init__(
        self,
        source_id: str,
        score: float,
        weight: float,
        confidence: float = 1.0,
        hard_block: bool = False,
        block_reason: str = "",
        available: bool = True,
        raw_data: Optional[dict] = None,
    ):
        super().__init__()
        self.source_id = source_id
        self.weight = weight
        self.refresh_interval = 60
        self.optional = True
        self._score = score
        self._confidence = confidence
        self._hard_block = hard_block
        self._block_reason = block_reason
        self._available = available
        self._raw = raw_data or {}

    def is_available(self) -> bool:
        return self._available

    async def fetch(self) -> SourceResult:
        return SourceResult(
            source_id=self.source_id,
            score=self._score,
            confidence=self._confidence,
            hard_block=self._hard_block,
            block_reason=self._block_reason,
            raw_data=self._raw,
        )


class CrashingSource(BaseSentimentSource):
    """Always raises inside fetch(). Verifies degraded paths."""
    source_id = "crashing"
    refresh_interval = 60
    optional = True

    def __init__(self, weight: float = 0.5):
        super().__init__()
        self.weight = weight

    async def fetch(self) -> SourceResult:
        raise RuntimeError("simulated source failure")


@pytest.fixture(autouse=True)
def _patch_db_log(monkeypatch):
    """Avoid hitting SQLite during sentiment aggregator tests."""
    monkeypatch.setattr(
        "sentiment.aggregator.db_queries.log_sentiment_result",
        lambda *a, **k: None,
    )


# ─────────────────────────────────────────────────────────────────────────
# Composite math
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_composite_weighted_average():
    """composite = Σ(score·weight·conf) / Σ(weight·conf), clamped to ±100."""
    agg = SentimentAggregator(sources=[
        StubSource("a", score=+80, weight=0.4),   # contribution = 80*0.4 = 32
        StubSource("b", score=-20, weight=0.2),   # contribution = -20*0.2 = -4
        StubSource("c", score=+40, weight=0.4),   # contribution = 40*0.4 = 16
        # weight_sum = 1.0 → composite = (32 - 4 + 16) / 1.0 = 44
    ])
    data = await agg.refresh()
    assert data.composite_score == pytest.approx(44.0, abs=0.1)
    # +44 sits in the +40-tier of the modifier ladder
    assert data.sentiment_modifier == 10.0


@pytest.mark.asyncio
async def test_composite_clamps_to_bounds():
    """Even a wildly aligned set of sources should not push composite past ±100."""
    agg = SentimentAggregator(sources=[
        StubSource("hot", score=+100, weight=1.0, confidence=1.0),
    ])
    data = await agg.refresh()
    assert -100.0 <= data.composite_score <= 100.0
    assert data.sentiment_modifier == 20.0


# ─────────────────────────────────────────────────────────────────────────
# Hard block propagation
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hard_block_propagates_from_any_source():
    agg = SentimentAggregator(sources=[
        StubSource("a", score=+50, weight=0.4),
        StubSource("news", score=-20, weight=0.25,
                   hard_block=True, block_reason="exchange hack"),
        StubSource("c", score=+30, weight=0.2),
    ])
    data = await agg.refresh()
    assert data.hard_block is True
    assert "exchange hack" in data.block_reason


@pytest.mark.asyncio
async def test_hard_block_blocks_session_floor():
    agg = SentimentAggregator(sources=[
        StubSource("bad", score=+0, weight=0.4,
                   hard_block=True, block_reason="protocol drained"),
    ])
    await agg.refresh()
    allowed, reason = agg.passes_session_floor()
    assert allowed is False
    assert "hard_block" in reason


# ─────────────────────────────────────────────────────────────────────────
# Degraded / unavailable sources
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unavailable_source_excluded_from_composite():
    """is_available()==False sources are dropped before fetch."""
    agg = SentimentAggregator(sources=[
        StubSource("ok",   score=+80, weight=0.5),
        StubSource("dead", score=-80, weight=0.5, available=False),
    ])
    data = await agg.refresh()
    # Only "ok" contributes — composite should be +80 (single source)
    assert data.composite_score == pytest.approx(80.0, abs=0.1)
    assert data.sources_active == 1
    assert data.sources_available == 1


@pytest.mark.asyncio
async def test_crashing_source_does_not_break_aggregator():
    """A source whose fetch() raises is silently dropped, not propagated."""
    agg = SentimentAggregator(sources=[
        StubSource("good",  score=+50, weight=0.5),
        CrashingSource(weight=0.5),
    ])
    data = await agg.refresh()
    # Good source still scores; crashing source contributes nothing
    assert data.composite_score == pytest.approx(50.0, abs=0.1)


# ─────────────────────────────────────────────────────────────────────────
# Session floor — extreme fear
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_passes_session_floor_blocks_on_extreme_fear(monkeypatch):
    """Fear & Greed value below the floor blocks the session."""
    agg = SentimentAggregator(sources=[
        StubSource(
            "fear_greed", score=-80, weight=1.0,
            raw_data={"value": 10, "label": "extreme fear"},
        ),
    ])
    await agg.refresh()
    allowed, reason = agg.passes_session_floor()
    assert allowed is False
    assert "fear_greed" in reason


@pytest.mark.asyncio
async def test_passes_session_floor_blocks_below_composite_floor(monkeypatch):
    monkeypatch.setattr(settings, "SENTIMENT_COMPOSITE_FLOOR", -40)
    agg = SentimentAggregator(sources=[
        StubSource("a", score=-80, weight=1.0,
                   raw_data={"value": 50, "label": "neutral"}),
    ])
    await agg.refresh()
    allowed, reason = agg.passes_session_floor()
    assert allowed is False
    assert "composite" in reason


@pytest.mark.asyncio
async def test_passes_session_floor_allows_normal_conditions():
    agg = SentimentAggregator(sources=[
        StubSource("fear_greed", score=+10, weight=1.0,
                   raw_data={"value": 55, "label": "greed"}),
    ])
    await agg.refresh()
    allowed, reason = agg.passes_session_floor()
    assert allowed is True
    assert reason == ""


# ─────────────────────────────────────────────────────────────────────────
# BTC dump guard
# ─────────────────────────────────────────────────────────────────────────

def test_btc_guard_penalty_applies_below_threshold():
    agg = SentimentAggregator(sources=[])
    agg.set_btc_change_30m(-3.0)  # below SENTIMENT_BTC_GUARD_PCT (-2.0)
    assert agg.btc_guard_penalty("momentum", "ETH/USDT") == settings.SENTIMENT_BTC_GUARD_PENALTY


def test_btc_guard_no_penalty_above_threshold():
    agg = SentimentAggregator(sources=[])
    agg.set_btc_change_30m(-1.0)
    assert agg.btc_guard_penalty("momentum", "ETH/USDT") == 0.0


def test_btc_guard_exempts_arb():
    agg = SentimentAggregator(sources=[])
    agg.set_btc_change_30m(-5.0)
    # Arb has no directional exposure → no penalty even in a dump
    assert agg.btc_guard_penalty("arb", "ETH/USDT") == 0.0


def test_btc_guard_no_change_data_yet():
    agg = SentimentAggregator(sources=[])
    # set_btc_change_30m never called
    assert agg.btc_guard_penalty("momentum", "ETH/USDT") == 0.0


# ─────────────────────────────────────────────────────────────────────────
# Extensibility — new source plugs in via REGISTERED_SOURCES
# ─────────────────────────────────────────────────────────────────────────

class PluggableSourceFixture(BaseSentimentSource):
    """Stand-in for a future source — proves the contract is enough."""
    source_id = "new_source"
    refresh_interval = 60
    optional = True

    def __init__(self):
        super().__init__()
        self.weight = 0.5

    async def fetch(self) -> SourceResult:
        return SourceResult(source_id=self.source_id, score=+25.0,
                            raw_data={"hello": "world"})


@pytest.mark.asyncio
async def test_new_source_plugs_into_aggregator():
    """A class implementing BaseSentimentSource works without aggregator code changes."""
    agg = SentimentAggregator(sources=[
        StubSource("a", score=+50, weight=0.5),
        PluggableSourceFixture(),
    ])
    data = await agg.refresh()
    # Both sources contribute, weighted equally
    expected = (50 * 0.5 + 25 * 0.5) / 1.0
    assert data.composite_score == pytest.approx(expected, abs=0.1)
    assert data.sources_active == 2


@pytest.mark.asyncio
async def test_registered_sources_list_is_extensible():
    """The default REGISTERED_SOURCES is the only place to wire a new source in."""
    from sentiment.sources import REGISTERED_SOURCES
    assert len(REGISTERED_SOURCES) >= 5  # fear_greed, cryptopanic, reddit, trends, telegram


# ─────────────────────────────────────────────────────────────────────────
# fear_greed property + SentimentData shape
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fear_greed_property_returns_instance():
    """aggregator.fear_greed exposes the live FearGreedSource."""
    from sentiment.sources.fear_greed import FearGreedSource
    fg = FearGreedSource()
    agg = SentimentAggregator(sources=[fg])
    assert agg.fear_greed is fg


@pytest.mark.asyncio
async def test_dashboard_dict_populates(monkeypatch):
    agg = SentimentAggregator(sources=[
        StubSource(
            "fear_greed", score=+20, weight=0.4,
            raw_data={"value": 60, "label": "greed"},
        ),
        StubSource(
            "cryptopanic", score=+10, weight=0.25,
            raw_data={"headlines": ["BTC ETF approved", "ETH upgrade"]},
        ),
        StubSource(
            "reddit", score=+30, weight=0.2,
            raw_data={"bull_ratio": 0.65},
        ),
    ])
    await agg.refresh()
    latest = agg.latest
    assert latest["fear_greed_value"] == 60
    assert latest["fear_greed_label"] == "greed"
    assert latest["reddit_bullish_ratio"] == 0.65
    assert len(latest["top_headlines"]) == 2
    assert latest["sources_active"] == 3


# ─────────────────────────────────────────────────────────────────────────
# get_signal_modifier + is_hard_blocked — quality gate's consumers
# ─────────────────────────────────────────────────────────────────────────

def test_get_signal_modifier_defaults_to_zero_before_refresh():
    """Aggregator constructed but never refreshed → modifier 0 so the
    quality gate's `score += modifier` is a no-op rather than crashing
    or applying a stale value."""
    agg = SentimentAggregator(sources=[
        StubSource("fear_greed", score=0, weight=0.4),
    ])
    assert agg.get_signal_modifier() == 0


def test_is_hard_blocked_false_before_refresh():
    agg = SentimentAggregator(sources=[
        StubSource("fear_greed", score=0, weight=0.4),
    ])
    blocked, reason = agg.is_hard_blocked()
    assert blocked is False
    assert reason == ""


@pytest.mark.asyncio
async def test_hard_block_propagates_to_aggregator():
    """A source with hard_block=True triggers is_hard_blocked → True."""
    agg = SentimentAggregator(sources=[
        StubSource(
            "cryptopanic", score=-90, weight=0.25,
            hard_block=True, block_reason="major_exchange_hack",
        ),
        StubSource("fear_greed", score=0, weight=0.4),
    ])
    await agg.refresh()
    blocked, reason = agg.is_hard_blocked()
    assert blocked is True
    assert "major_exchange_hack" in reason


@pytest.mark.asyncio
async def test_get_signal_modifier_returns_step_value_after_refresh():
    """High positive composite (≥+60) maps to +20 per the existing
    sentiment _composite_to_modifier ladder."""
    agg = SentimentAggregator(sources=[
        StubSource("fear_greed", score=+70, weight=1.0),
    ])
    await agg.refresh()
    assert agg.get_signal_modifier() == 20

    agg2 = SentimentAggregator(sources=[
        StubSource("fear_greed", score=-70, weight=1.0),
    ])
    await agg2.refresh()
    assert agg2.get_signal_modifier() == -20
