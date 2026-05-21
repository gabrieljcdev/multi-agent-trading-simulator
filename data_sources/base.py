"""
data_sources/base.py

Universal contract every data source plugin implements. Mirrors the
sentiment plugin pattern: a source declares its identity, refresh cadence,
and metric list, then implements fetch_all(). The base class owns cache,
staleness, error recovery, and pub/sub notification — concrete sources
never have to touch any of that.

  DataPoint        — one cached reading (source × metric × symbol)
  BaseDataSource   — subclass + implement fetch_all() + list_metrics()

The aggregator (data_sources/__init__.py) discovers each instance via
data_sources/sources/__init__.py:REGISTERED_SOURCES — no concrete imports
needed in either layer. See PLUGIN_PATTERN.md (or the docstring in
data_sources/sources/__init__.py) for the 5-step add-a-source recipe.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# DataPoint — one reading
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class DataPoint:
    """One reading from one source at one point in time.

    `key` is the canonical pub/sub key: "<source_id>.<metric>" for global
    metrics (VIX, CPI, DXY) or "<source_id>.<metric>.<symbol>" for
    per-symbol metrics (funding rate per pair). Subscribers match on this.
    """
    source_id: str
    metric:    str
    value:     float
    symbol:    Optional[str]  = None
    timestamp: float          = 0.0
    raw_data:  dict           = field(default_factory=dict)
    error:     Optional[str]  = None

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    @property
    def key(self) -> str:
        if self.symbol:
            return f"{self.source_id}.{self.metric}.{self.symbol}"
        return f"{self.source_id}.{self.metric}"


# ─────────────────────────────────────────────────────────────────────────
# BaseDataSource — subclass + register in data_sources/sources/__init__.py
# ─────────────────────────────────────────────────────────────────────────

class BaseDataSource(ABC):
    """Subclass and set the class attrs, then implement fetch_all() and
    list_metrics(). Cache management, staleness, error fallback, and
    subscriber notification live in this base — do not override them.
    """

    # Override in subclasses
    source_id:        str  = "base"
    display_name:     str  = "Base"
    refresh_interval: int  = 300            # seconds
    optional:         bool = True
    requires_api_key: bool = False
    api_key_env_var:  str  = ""

    def __init__(self):
        # Per-key cache. Instance-level (NEVER class-level) — a class-level
        # mutable would be shared across every subclass instance.
        self._cache: dict[str, DataPoint] = {}
        self._last_fetch_time: float = 0.0
        # Aggregator wires this in so sources can announce changes without
        # importing the aggregator (would create an import cycle).
        self._notify_callback = None

    # ── Required hooks ──────────────────────────────────────────────────

    @abstractmethod
    async def fetch_all(self) -> list[DataPoint]:
        """Fetch every metric × symbol this source publishes.

        Must never raise — on network/parse failure return a list of
        DataPoints with .error set, or an empty list. The base class will
        fall through to cached values automatically.
        """
        ...

    @abstractmethod
    def list_metrics(self) -> list[str]:
        """Metrics this source publishes. Used for discoverability +
        subscriber pattern validation."""
        ...

    def is_available(self) -> bool:
        """Return True if this source can run.

        Default: True if it doesn't require an API key, else True only
        when the key env var is set. Subclasses override for richer
        dependency checks (module import, network reachability, etc.).
        """
        if not self.requires_api_key:
            return True
        import os
        return bool(os.getenv(self.api_key_env_var, ""))

    # ── Public read API ─────────────────────────────────────────────────

    async def get(
        self,
        metric: str,
        symbol: Optional[str] = None,
    ) -> Optional[DataPoint]:
        """Return a cached point if fresh, else trigger a refresh.

        Behaviour:
          - If cache is fresh (within refresh_interval): return cached.
          - Else: call fetch_all(), update cache, notify subscribers for
            every changed key, then return the requested point.
          - On fetch error: return cached value with .error if available,
            else None.
        """
        key = self._make_key(metric, symbol)
        await self.refresh_if_stale()
        return self._cache.get(key)

    async def refresh_if_stale(self) -> None:
        """Refresh the whole source if the cache is stale.

        Source-level (not per-metric): one fetch_all() call covers every
        DataPoint this source provides. Cheaper than per-key polling and
        matches every API we wrap (each issues one call for N metrics).
        """
        now = time.time()
        if (now - self._last_fetch_time) < self.refresh_interval and self._cache:
            return
        await self._do_fetch()

    async def _do_fetch(self) -> None:
        """Run fetch_all() with cache merge + change notification.

        Never raises. Subscribers fire for keys whose value changed
        compared to the previous cached point (or on first observation).
        """
        try:
            points = await self.fetch_all() or []
        except Exception as e:
            logger.warning(f"{self.source_id}: fetch_all raised — {e}")
            return

        self._last_fetch_time = time.time()
        for point in points:
            previous = self._cache.get(point.key)
            # Don't clobber a good cached value with an error-state
            # placeholder — the last known good reading is more useful
            # to consumers (macro modifier, dashboard) than zero. Error
            # points still enter the cache on a first-ever fetch so
            # cached_value can report None rather than the wrong value.
            if point.error and previous is not None and previous.error is None:
                continue
            self._cache[point.key] = point

            # Fire subscribers only when the underlying value changed —
            # repeated identical reads don't need to wake anyone.
            if self._notify_callback is None:
                continue
            if previous is None or previous.value != point.value or previous.error != point.error:
                try:
                    await self._notify_callback(point, previous)
                except Exception as e:
                    logger.debug(f"{self.source_id}: notify failed for {point.key}: {e}")

    # ── Helpers ─────────────────────────────────────────────────────────

    def _make_key(self, metric: str, symbol: Optional[str] = None) -> str:
        if symbol:
            return f"{self.source_id}.{metric}.{symbol}"
        return f"{self.source_id}.{metric}"

    def cached(self, metric: str, symbol: Optional[str] = None) -> Optional[DataPoint]:
        """Sync read of the cached point. Dashboard + quality-gate paths
        use this — they cannot await get() from a sync context."""
        return self._cache.get(self._make_key(metric, symbol))

    def cached_value(
        self,
        metric: str,
        symbol: Optional[str] = None,
        default: Optional[float] = None,
    ) -> Optional[float]:
        """Sync float accessor — None if no reading yet, the last good
        cached value if available, or the default when the only cached
        point has an error set (treat error placeholders as 'missing')."""
        p = self.cached(metric, symbol)
        if p is None or p.error is not None:
            return default
        return p.value

    def latest_points(self) -> list[DataPoint]:
        """Every cached DataPoint, in insertion order. Used by the
        aggregator's latest_snapshot() and DB logging."""
        return list(self._cache.values())
