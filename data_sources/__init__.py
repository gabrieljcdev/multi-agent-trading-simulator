"""
data_sources/ — pluggable macro + on-chain + FX data aggregator.

Public surface:
    DataSources    — the orchestrator class (pull + push API)
    DataPoint      — one cached reading
    BaseDataSource — subclass to add a new source
    data_sources   — module-level singleton (CLAUDE.md singleton pattern)

See data_sources/sources/__init__.py for the docstring on adding a new
source. The aggregator never imports concrete source classes — every
source registers itself through that module's REGISTERED_SOURCES list.

PULL  (sync, cached):  data_sources.alpha_vantage.get_vix()
PULL  (async, fresh):  await data_sources.get("alpha_vantage", "vix")
PUSH:                  data_sources.subscribe("alpha_vantage.vix", cb)
REFRESH:               await data_sources.refresh_all()
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import uuid
from typing import Callable, Optional

from database import queries as db_queries

from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Subscriber bookkeeping
# ─────────────────────────────────────────────────────────────────────────

class _Subscription:
    __slots__ = ("sub_id", "pattern", "callback", "filter")

    def __init__(self, sub_id: str, pattern: str,
                 callback: Callable, filter: Optional[Callable] = None):
        self.sub_id   = sub_id
        self.pattern  = pattern
        self.callback = callback
        self.filter   = filter

    def matches(self, key: str) -> bool:
        # fnmatch supports the "*" wildcard at any segment, which is the
        # contract the spec asks for: "coinglass.funding_rate.*",
        # "fred.*", "*.vix" all work without bespoke parsing.
        return fnmatch.fnmatchcase(key, self.pattern)


# ─────────────────────────────────────────────────────────────────────────
# Aggregator
# ─────────────────────────────────────────────────────────────────────────

class DataSources:
    """Orchestrator + pub/sub hub for every registered data source.

    Constructed with no args to use REGISTERED_SOURCES. Pass `sources=[...]`
    for tests. Each source is exposed as an attribute keyed by source_id —
    `data_sources.coinglass`, `data_sources.alpha_vantage`, etc.
    """

    def __init__(self, sources: Optional[list[BaseDataSource]] = None):
        if sources is None:
            # Deferred import so test harnesses can stub REGISTERED_SOURCES
            # via monkeypatch before the singleton is constructed.
            from data_sources.sources import REGISTERED_SOURCES
            sources = REGISTERED_SOURCES

        self._sources: list[BaseDataSource] = list(sources)
        self._subscribers: list[_Subscription] = []

        for source in self._sources:
            # Attribute access — `data_sources.coinglass` etc.
            setattr(self, source.source_id, source)
            # Wire the notify callback so the base class can fan changes
            # to subscribers without importing this module.
            source._notify_callback = self._on_source_change

    # ─── Pull API ────────────────────────────────────────────────────────

    async def refresh_all(self) -> None:
        """Refresh every available source concurrently.

        One bad source must not break the others — asyncio.gather with
        return_exceptions swallows per-source failures. Each successful
        DataPoint is persisted to data_log.
        """
        active = [s for s in self._sources if s.is_available()]
        if not active:
            return

        await asyncio.gather(
            *(self._safe_refresh(s) for s in active),
            return_exceptions=True,
        )

        # Log every cached point — the base class already updated _cache.
        for source in active:
            for point in source.latest_points():
                try:
                    db_queries.log_data_point(point)
                except Exception as e:
                    logger.debug(f"log_data_point {point.key}: {e}")

    async def _safe_refresh(self, source: BaseDataSource) -> None:
        try:
            await source.refresh_if_stale()
        except Exception as e:
            logger.warning(f"data source {source.source_id} refresh failed: {e}")

    async def get(
        self,
        source_id: str,
        metric: str,
        symbol: Optional[str] = None,
    ) -> Optional[DataPoint]:
        """Convenience wrapper. Returns None if source_id is unknown."""
        source = getattr(self, source_id, None)
        if source is None:
            return None
        return await source.get(metric, symbol)

    def latest_snapshot(self) -> dict[str, DataPoint]:
        """Flatten every source's cache for the dashboard / debugging.

        Keys are canonical DataPoint keys ("source.metric" or
        "source.metric.symbol"). Sync — no awaits.
        """
        snap: dict[str, DataPoint] = {}
        for source in self._sources:
            for point in source.latest_points():
                snap[point.key] = point
        return snap

    # ─── Push API ────────────────────────────────────────────────────────

    def subscribe(
        self,
        pattern: str,
        callback: Callable,
        filter: Optional[Callable] = None,
    ) -> str:
        """Register a callback for data point updates.

        pattern   — "<source_id>.<metric>[.<symbol>]"; "*" wildcards at
                    any segment are honored (fnmatch syntax).
                    Examples:
                      "alpha_vantage.vix"           — exact
                      "coinglass.funding_rate.*"    — all symbols
                      "fred.*"                      — every fred metric
                      "*.vix"                       — vix from any source
        callback  — receives (new_point, previous_point). previous_point
                    is None on first observation.
        filter    — optional predicate; callback only fires when
                    filter(new_point) is truthy.

        Returns a subscription id usable with unsubscribe(). Callback may
        be a coroutine or a sync function — both are supported.
        """
        sub = _Subscription(
            sub_id=uuid.uuid4().hex,
            pattern=pattern,
            callback=callback,
            filter=filter,
        )
        self._subscribers.append(sub)
        return sub.sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        for i, sub in enumerate(self._subscribers):
            if sub.sub_id == subscription_id:
                self._subscribers.pop(i)
                return True
        return False

    async def _on_source_change(
        self,
        new: DataPoint,
        previous: Optional[DataPoint],
    ) -> None:
        """Wired into BaseDataSource as _notify_callback.

        Walks every subscription, dispatches matching callbacks via
        asyncio.create_task so a slow callback never blocks the fetch
        path. Failed callbacks log but never propagate.
        """
        for sub in list(self._subscribers):
            if not sub.matches(new.key):
                continue
            if sub.filter is not None:
                try:
                    if not sub.filter(new):
                        continue
                except Exception as e:
                    logger.debug(f"subscriber filter raised on {new.key}: {e}")
                    continue
            asyncio.create_task(self._invoke(sub, new, previous))

    async def _invoke(
        self,
        sub: _Subscription,
        new: DataPoint,
        previous: Optional[DataPoint],
    ) -> None:
        try:
            result = sub.callback(new, previous)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.warning(f"subscriber {sub.pattern} callback raised: {e}")

    # ─── Convenience for the bot loop ────────────────────────────────────

    async def run_refresh_loop(self, interval_sec: int) -> None:
        """Long-running task: refresh_all() every `interval_sec` seconds.

        The bot's start() can launch this alongside its other gather()
        tasks. Caller is responsible for cancelling on shutdown.
        """
        while True:
            try:
                await self.refresh_all()
            except Exception as e:
                logger.error(f"data_sources refresh loop: {e}", exc_info=True)
            await asyncio.sleep(interval_sec)


# Module-level singleton — CLAUDE.md singleton pattern.
data_sources = DataSources()


__all__ = [
    "DataSources",
    "BaseDataSource",
    "DataPoint",
    "data_sources",
]
