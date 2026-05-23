"""
data_sources/sources/coinglass.py

Crypto derivatives data via open-api.coinglass.com. Free tier, no key.
Pulls funding rate, open interest, 24h liquidations, and long/short
ratio per pair in settings.COINGLASS_WATCH_PAIRS.

Coinglass's free endpoints are batched by symbol — one call per symbol
per metric. We accept that cost: refresh_interval is 5 min and a sweep
of 16 pairs × 4 endpoints fits comfortably even on a flaky link.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)

# Public v2 endpoints. The "futures/funding_rates_chart" path returns
# the latest funding rate; "open_interest_chart" and "liquidation_chart"
# the corresponding 24h aggregates.
BASE = "https://open-api.coinglass.com/public/v2"


class CoinglassSource(BaseDataSource):
    source_id        = "coinglass"
    display_name     = "Coinglass"
    refresh_interval = settings.COINGLASS_REFRESH_SEC
    optional         = True
    requires_api_key = False

    def __init__(self):
        super().__init__()
        # Free-tier rate cap: cap concurrent HTTP requests. Each refresh
        # bursts ~16 pairs × 4 endpoints; without this an enthusiastic
        # asyncio.gather punches straight through Coinglass's quota.
        self._rate_limit = asyncio.Semaphore(settings.COINGLASS_RATE_LIMIT_PER_MIN)

    def list_metrics(self) -> list[str]:
        return [
            "funding_rate",
            "open_interest",
            "liquidations_long_24h",
            "liquidations_short_24h",
            "long_short_ratio",
        ]

    async def fetch_all(self) -> list[DataPoint]:
        pairs = list(settings.COINGLASS_WATCH_PAIRS)
        timeout = aiohttp.ClientTimeout(total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            results = await asyncio.gather(
                *(self._fetch_for_symbol(session, p) for p in pairs),
                return_exceptions=True,
            )

        points: list[DataPoint] = []
        for pair, result in zip(pairs, results):
            if isinstance(result, Exception):
                logger.debug(f"coinglass {pair}: {result}")
                points.append(DataPoint(
                    source_id=self.source_id, metric="funding_rate",
                    symbol=pair, value=0.0, error=str(result),
                ))
                continue
            points.extend(result)
        return points

    async def _fetch_for_symbol(
        self,
        session: aiohttp.ClientSession,
        pair: str,
    ) -> list[DataPoint]:
        # Coinglass takes the base symbol (BTC, ETH) — strip /USDT.
        symbol = pair.split("/")[0]
        points: list[DataPoint] = []

        # Funding rate (decimal, 0.0001 == 0.01%).
        funding = await self._safe_get(session, "/funding_rates_chart", {"symbol": symbol})
        rate = self._extract_latest(funding, key="rate")
        points.append(DataPoint(
            source_id=self.source_id, metric="funding_rate",
            symbol=pair, value=rate or 0.0,
            raw_data={"sym": symbol, "raw": funding},
            error=None if rate is not None else "no_funding_data",
        ))

        # Open interest (USD).
        oi = await self._safe_get(session, "/open_interest_chart", {"symbol": symbol})
        oi_val = self._extract_latest(oi, key="oi")
        points.append(DataPoint(
            source_id=self.source_id, metric="open_interest",
            symbol=pair, value=oi_val or 0.0,
            raw_data={"sym": symbol},
            error=None if oi_val is not None else "no_oi_data",
        ))

        # Liquidations (long + short, 24h USD).
        liq = await self._safe_get(session, "/liquidation_chart", {"symbol": symbol})
        long_liq  = self._extract_latest(liq, key="long")  or 0.0
        short_liq = self._extract_latest(liq, key="short") or 0.0
        points.append(DataPoint(
            source_id=self.source_id, metric="liquidations_long_24h",
            symbol=pair, value=long_liq,
        ))
        points.append(DataPoint(
            source_id=self.source_id, metric="liquidations_short_24h",
            symbol=pair, value=short_liq,
        ))

        # Long/short account ratio.
        ls = await self._safe_get(session, "/long_short_ratio", {"symbol": symbol})
        ratio = self._extract_latest(ls, key="long_short_ratio")
        points.append(DataPoint(
            source_id=self.source_id, metric="long_short_ratio",
            symbol=pair, value=ratio or 1.0,
            error=None if ratio is not None else "no_ls_data",
        ))

        return points

    async def _safe_get(
        self,
        session: aiohttp.ClientSession,
        path: str,
        params: dict,
    ) -> dict:
        url = BASE + path
        async with self._rate_limit:
            async with session.get(url, params=params) as r:
                try:
                    return await r.json(content_type=None)
                except Exception:
                    return {}

    def _extract_latest(self, payload: dict, key: str) -> Optional[float]:
        """Coinglass shapes vary by endpoint; pull the most recent numeric
        value from the canonical `data` envelope.

        Accepts dict or list payloads. Returns None on any structural
        mismatch — caller logs an error point in that case."""
        data = (payload or {}).get("data")
        if not data:
            return None
        # Time-series shape: dict with arrays keyed by metric name.
        if isinstance(data, dict):
            series = data.get(key) or data.get(key + "s") or data.get(f"{key}List")
            if isinstance(series, list) and series:
                last = series[-1]
                try:
                    return float(last)
                except (TypeError, ValueError):
                    pass
            # Some endpoints flatten the latest into top-level fields.
            try:
                return float(data.get(key))
            except (TypeError, ValueError):
                return None
        # Some endpoints return list[dict].
        if isinstance(data, list) and data:
            try:
                return float(data[-1].get(key))
            except (AttributeError, TypeError, ValueError):
                return None
        return None

    # ── Sync convenience accessors ───────────────────────────────────────

    def get_funding(self, pair: str) -> Optional[float]:
        return self.cached_value("funding_rate", pair)

    def get_open_interest(self, pair: str) -> Optional[float]:
        return self.cached_value("open_interest", pair)

    def get_long_short_ratio(self, pair: str) -> Optional[float]:
        return self.cached_value("long_short_ratio", pair)

    def get_liquidations_24h(self, pair: str) -> dict:
        long_l  = self.cached_value("liquidations_long_24h",  pair) or 0.0
        short_l = self.cached_value("liquidations_short_24h", pair) or 0.0
        return {"long": long_l, "short": short_l, "total": long_l + short_l}
