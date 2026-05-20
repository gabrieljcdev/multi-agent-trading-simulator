"""
sentiment/base.py

The two contracts every sentiment source must follow:

  SourceResult        — what a fetch returns
  BaseSentimentSource — what a source class looks like

A source supplies a -100..+100 score plus optional raw data, confidence,
and a hard-block flag. Hard blocks short-circuit composite logic — any
source returning hard_block=True suppresses all signals until cleared.

Sources never need to manage their own caching/retry/skip-on-error logic:
get() on the base class handles it. Just implement fetch() and the
class-level attrs (source_id, weight, refresh_interval, optional).
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SourceResult:
    """One reading from one source at one point in time."""
    source_id:    str
    score:        float                       # -100 to +100, 0 is neutral
    raw_data:     dict           = field(default_factory=dict)
    hard_block:   bool           = False
    block_reason: str            = ""
    confidence:   float          = 1.0        # 0–1, weights composite
    timestamp:    float          = 0.0
    error:        Optional[str]  = None

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()
        # Clamp score to its declared range so no source can poison the
        # composite with extreme values, no matter what its fetch returns.
        self.score = max(-100.0, min(100.0, float(self.score)))
        self.confidence = max(0.0, min(1.0, float(self.confidence)))


class BaseSentimentSource(ABC):
    """Subclass and set the four class attrs, then implement fetch().

    The aggregator calls get() — never fetch() directly — so cache and
    error-handling logic stay in one place.
    """

    # Override in subclasses
    source_id:        str  = "base"
    weight:           float = 0.0       # contribution to composite
    refresh_interval: int  = 300        # seconds between live fetches
    optional:         bool = True       # if True, errors don't block the aggregator

    def __init__(self):
        # Internal cache — DO NOT override. Used by get() for cache-or-fetch.
        self._last_result:    Optional[SourceResult] = None
        self._last_fetch_time: float                 = 0.0

    # ── Public API ──────────────────────────────────────────────────────

    @abstractmethod
    async def fetch(self) -> SourceResult:
        """Live fetch from the source. Must never raise — return a
        SourceResult with the error field set on any failure."""
        ...

    def is_available(self) -> bool:
        """Return True if this source can run (deps installed, key present).
        Checked before fetch() is called; default returns True."""
        return True

    async def get(self) -> Optional[SourceResult]:
        """Return a fresh result if cache is stale, else cached.

        Behaviour:
          - If `refresh_interval` has not elapsed since last fetch and we
            have a cached result, return the cached one.
          - Otherwise call fetch(). On success cache and return.
          - On exception, return the previous cached result if one exists.
          - If no cache and fetch fails, return None.
        """
        now = time.time()
        if self._last_result is not None and self.refresh_interval > 0:
            if (now - self._last_fetch_time) < self.refresh_interval:
                return self._last_result

        try:
            result = await self.fetch()
        except Exception as e:
            logger.warning(f"{self.source_id}: fetch raised — {e}")
            return self._last_result   # may be None

        if result is None:
            return self._last_result

        self._last_result = result
        self._last_fetch_time = now
        return result

    # Useful in tests + dashboard
    def last(self) -> Optional[SourceResult]:
        return self._last_result
