"""
sentiment/aggregator.py

Orchestrates every registered source, blends them into one composite
score, and exposes the pieces the bot + dashboard need.

The aggregator owns:
  - one instance of every source class in REGISTERED_SOURCES
  - the latest composite + per-source scores
  - the session-floor checks (extreme fear, news block, BTC dump guard)

Sources can be added freely (see sentiment/sources/__init__.py) — the
aggregator has no knowledge of them beyond the BaseSentimentSource ABC.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import settings
from database import queries as db_queries
from sentiment.base import BaseSentimentSource, SourceResult
from sentiment.sources import REGISTERED_SOURCES
from sentiment.sources.fear_greed import FearGreedSource

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Data shape
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class SentimentData:
    """The aggregated picture, suitable for the dashboard or the engine."""
    composite_score:    float           = 0.0    # -100..+100
    sentiment_modifier: float           = 0.0    # score adjustment for signals
    hard_block:         bool            = False
    block_reason:       str             = ""

    # Per-source surfaces
    fear_greed_value:   Optional[int]   = None   # 0..100 raw
    fear_greed_label:   str             = "unknown"

    news_score:         Optional[float] = None
    news_guard_active:  bool            = False
    blocking_headline:  str             = ""
    top_headlines:      list            = field(default_factory=list)

    reddit_score:        Optional[float] = None
    reddit_bullish_ratio: Optional[float] = None

    google_trends_score: Optional[float] = None

    btc_change_30m:     Optional[float] = None

    sources_active:     int             = 0
    sources_available:  int             = 0
    last_updated:       Optional[datetime] = None


# ─────────────────────────────────────────────────────────────────────────
# Modifier ladder
# ─────────────────────────────────────────────────────────────────────────

def _composite_to_modifier(composite: float) -> float:
    """Step function from composite score to signal-score modifier.

    Composite is -100..+100. We return an additive modifier in the
    -20..+20 range. The mapping is spec-defined — see prompts/build_sentiment.md.
    """
    if composite >= 60:
        return 20.0
    if composite >= 40:
        return 10.0
    if composite >= 20:
        return 5.0
    if composite <= -60:
        return -20.0
    if composite <= -40:
        return -10.0
    if composite <= -20:
        return -5.0
    return 0.0


# ─────────────────────────────────────────────────────────────────────────
# Aggregator
# ─────────────────────────────────────────────────────────────────────────

class SentimentAggregator:
    """Orchestrator + composite calculator.

    Construct with no args to use REGISTERED_SOURCES. Pass `sources=[...]`
    of pre-instantiated sources for tests.
    """

    def __init__(self, sources: Optional[list[BaseSentimentSource]] = None):
        if sources is None:
            sources = [cls() for cls in REGISTERED_SOURCES]
        self._sources: list[BaseSentimentSource] = sources

        self._data: SentimentData = SentimentData()
        self.latest: dict = {}     # dashboard-friendly mirror of _data
        self._last_refresh_at: Optional[datetime] = None
        self._btc_change_30m: Optional[float] = None

    # ── Public API used by core/bot.py ──────────────────────────────────

    async def refresh(self) -> SentimentData:
        """Pull from every available source concurrently and recompute."""
        active_sources = [s for s in self._sources if s.is_available()]
        results = await asyncio.gather(
            *[s.get() for s in active_sources],
            return_exceptions=True,
        )

        pairs: list[tuple[BaseSentimentSource, SourceResult]] = []
        for source, result in zip(active_sources, results):
            if isinstance(result, Exception):
                logger.debug(f"{source.source_id} raised: {result}")
                continue
            if result is None:
                continue
            pairs.append((source, result))

        composite = self._compute_composite(pairs)
        modifier  = _composite_to_modifier(composite)
        hard_block, block_reason = self._hard_block_from(pairs)

        data = SentimentData(
            composite_score=composite,
            sentiment_modifier=modifier,
            hard_block=hard_block,
            block_reason=block_reason,
            btc_change_30m=self._btc_change_30m,
            sources_active=len(pairs),
            sources_available=len(active_sources),
            last_updated=datetime.utcnow(),
        )
        self._populate_per_source_fields(data, pairs)

        self._data = data
        self.latest = self._to_dashboard_dict(data)
        self._last_refresh_at = data.last_updated

        # Persist every result with the composite snapshot — the dashboard
        # uses the per-source rows; backtesting uses the composite history.
        for _, result in pairs:
            try:
                db_queries.log_sentiment_result(result, composite_score=composite)
            except Exception as e:
                logger.warning(f"log_sentiment_result: {e}")

        return data

    async def get_current(self) -> SentimentData:
        """Return cached SentimentData, refreshing if it's stale."""
        if self._last_refresh_at is None:
            return await self.refresh()
        age = (datetime.utcnow() - self._last_refresh_at).total_seconds()
        if age >= settings.SENTIMENT_REFRESH_INTERVAL_SEC:
            return await self.refresh()
        return self._data

    def get_signal_modifier(self) -> int:
        """Step-ladder modifier the quality gate adds to a signal score.

        Mirrors macro_monitor.get_signal_modifier() — consumers don't
        need to know whether sentiment refreshed yet. Zero before the
        first refresh; never None so the caller's `score += mod` stays
        safe.
        """
        return int(self._data.sentiment_modifier or 0)

    def is_hard_blocked(self) -> tuple[bool, str]:
        """True (with reason) when a sentiment source has set a hard
        block — catastrophic-event keywords from cryptopanic etc. The
        quality gate short-circuits on this same as macro CRISIS.
        """
        d = self._data
        if d.hard_block:
            return True, d.block_reason or "sentiment_hard_block"
        if d.news_guard_active:
            return True, d.blocking_headline or "news_guard"
        return False, ""

    def passes_session_floor(self) -> tuple[bool, str]:
        """Returns (allowed, reason). Three independent floors must all pass:

          1. Composite >= SENTIMENT_COMPOSITE_FLOOR
          2. Fear & Greed value >= SENTIMENT_FEAR_GREED_FLOOR
          3. No source has set hard_block

        Also checked: an active news guard (cryptopanic block).
        """
        d = self._data
        if d.hard_block:
            return False, f"hard_block: {d.block_reason or 'unknown'}"
        if d.news_guard_active:
            return False, f"news_guard: {d.blocking_headline or 'active'}"
        # Check fear_greed before composite: an extreme F&G reading is the
        # more specific, more actionable failure mode and gives a clearer
        # log line ("extreme fear, value=10" beats "composite -80 < -60").
        if d.fear_greed_value is not None and d.fear_greed_value < settings.SENTIMENT_FEAR_GREED_FLOOR:
            return False, f"fear_greed {d.fear_greed_value} < floor {settings.SENTIMENT_FEAR_GREED_FLOOR}"
        if d.composite_score < settings.SENTIMENT_COMPOSITE_FLOOR:
            return False, f"composite {d.composite_score:.1f} < floor {settings.SENTIMENT_COMPOSITE_FLOOR}"
        return True, ""

    def set_btc_change_30m(self, pct: float) -> None:
        """Feed the 30-minute BTC % change. Used by btc_guard_penalty().

        `pct` is a percent (e.g. -2.5 for a 2.5% drop). The bot's market
        data layer is the canonical source — call this from a heartbeat
        loop or BTC price callback.
        """
        self._btc_change_30m = pct
        self._data.btc_change_30m = pct
        self.latest["btc_change_30m"] = pct

    def btc_guard_penalty(self, signal_type: str, pair: str) -> float:
        """Per-signal penalty for BTC dumps. Arb is exempt (no directional
        exposure); BTC-quoted alt signals take the penalty if the threshold
        trips. Returns 0 when no penalty applies.
        """
        if signal_type == "arb":
            return 0.0
        if self._btc_change_30m is None:
            return 0.0
        if self._btc_change_30m > settings.SENTIMENT_BTC_GUARD_PCT:
            return 0.0
        # BTC itself isn't "an alt" — but the academic finding is that even
        # BTC's own signals weaken right after a sharp drop, so we keep the
        # penalty universal across non-arb signal types.
        return float(settings.SENTIMENT_BTC_GUARD_PENALTY)

    @property
    def fear_greed(self) -> Optional[BaseSentimentSource]:
        """The FearGreedSource instance, so callers can fetch() it directly.

        Lets bot.py treat fear-greed as a special-case channel for the
        session-floor check without going through the full aggregator.
        """
        for s in self._sources:
            if isinstance(s, FearGreedSource):
                return s
        return None

    # ── Composite math ──────────────────────────────────────────────────

    def _compute_composite(
        self,
        pairs: list[tuple[BaseSentimentSource, SourceResult]],
    ) -> float:
        if not pairs:
            return 0.0
        weighted_sum = sum(
            r.score * s.weight * r.confidence for s, r in pairs
        )
        weight_sum = sum(s.weight * r.confidence for s, r in pairs)
        if weight_sum <= 0:
            return 0.0
        composite = weighted_sum / weight_sum
        return max(-100.0, min(100.0, composite))

    def _hard_block_from(
        self,
        pairs: list[tuple[BaseSentimentSource, SourceResult]],
    ) -> tuple[bool, str]:
        for _, r in pairs:
            if r.hard_block:
                return True, r.block_reason or f"{r.source_id} hard_block"
        return False, ""

    def _populate_per_source_fields(
        self,
        data: SentimentData,
        pairs: list[tuple[BaseSentimentSource, SourceResult]],
    ) -> None:
        for source, r in pairs:
            sid = source.source_id
            if sid == "fear_greed":
                raw = r.raw_data or {}
                data.fear_greed_value = raw.get("value")
                data.fear_greed_label = raw.get("label", "unknown")
            elif sid == "cryptopanic":
                data.news_score = r.score
                data.news_guard_active = r.hard_block
                data.blocking_headline = r.block_reason
                data.top_headlines = (r.raw_data or {}).get("headlines", [])[:3]
            elif sid == "reddit":
                data.reddit_score = r.score
                data.reddit_bullish_ratio = (r.raw_data or {}).get("bull_ratio")
            elif sid == "google_trends":
                data.google_trends_score = r.score

    def _to_dashboard_dict(self, d: SentimentData) -> dict:
        return {
            "fear_greed_value":     d.fear_greed_value,
            "fear_greed_label":     d.fear_greed_label,
            "news_score":           d.news_score,
            "news_guard_active":    d.news_guard_active,
            "blocking_headline":    d.blocking_headline,
            "top_headlines":        list(d.top_headlines),
            "reddit_score":         d.reddit_score,
            "reddit_bullish_ratio": d.reddit_bullish_ratio,
            "composite_score":      d.composite_score,
            "sentiment_modifier":   d.sentiment_modifier,
            "btc_change_30m":       d.btc_change_30m,
            "sources_active":       d.sources_active,
            "sources_available":    d.sources_available,
            "last_updated":         d.last_updated.isoformat() if d.last_updated else None,
        }


# Module-level singleton — CLAUDE.md singleton pattern.
sentiment = SentimentAggregator()
