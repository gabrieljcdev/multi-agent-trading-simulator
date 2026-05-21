"""
data_sources/sources/bybit_derivs.py

Bybit USDT-perpetual derivatives via the v5 public market API. One
"tickers" call per market returns funding rate, OI, mark/index/last
prices, 24h % change — single round-trip per refresh, no auth.

Sits alongside binance_futures + coinglass; the three together give
us three independent reads on funding/OI per pair, useful for sanity
checks and exchange-specific divergence detection.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)


class BybitDerivsSource(BaseDataSource):
    source_id        = "bybit_derivs"
    display_name     = "Bybit Derivatives"
    refresh_interval = settings.BYBIT_REFRESH_SEC
    optional         = True
    requires_api_key = False

    def list_metrics(self) -> list[str]:
        return [
            "open_interest",
            "funding_rate",
            "mark_price",
            "change_pct_24h",
        ]

    async def fetch_all(self) -> list[DataPoint]:
        symbols = list(settings.BYBIT_SYMBOLS)
        timeout = aiohttp.ClientTimeout(total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            results = await asyncio.gather(
                *(self._fetch_for_symbol(session, s) for s in symbols),
                return_exceptions=True,
            )

        points: list[DataPoint] = []
        for symbol, result in zip(symbols, results):
            if isinstance(result, Exception):
                logger.debug(f"bybit {symbol}: {result}")
                points.append(DataPoint(
                    source_id=self.source_id, metric="funding_rate",
                    symbol=self._fmt(symbol), value=0.0, error=str(result),
                ))
                continue
            points.extend(result)
        return points

    async def _fetch_for_symbol(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
    ) -> list[DataPoint]:
        url = f"{settings.BYBIT_BASE_URL}/v5/market/tickers"
        async with session.get(
            url,
            params={"category": "linear", "symbol": symbol},
        ) as r:
            payload = await r.json(content_type=None)

        if payload.get("retCode") != 0:
            raise RuntimeError(payload.get("retMsg", "bybit error"))

        rows = (payload.get("result") or {}).get("list") or []
        if not rows:
            raise RuntimeError("empty tickers list")
        row = rows[0]

        pair = self._fmt(symbol)
        points: list[DataPoint] = []
        oi = self._float(row.get("openInterest"))
        funding = self._float(row.get("fundingRate"))
        mark = self._float(row.get("markPrice"))
        change_pct = self._float(row.get("price24hPcnt"))

        if oi is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="open_interest",
                symbol=pair, value=oi, raw_data={"raw": row},
            ))
        if funding is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="funding_rate",
                symbol=pair, value=funding,
            ))
        if mark is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="mark_price",
                symbol=pair, value=mark,
            ))
        if change_pct is not None:
            # Bybit returns a decimal (0.0123 = 1.23%) — surface as %.
            points.append(DataPoint(
                source_id=self.source_id, metric="change_pct_24h",
                symbol=pair, value=change_pct * 100.0,
            ))
        return points

    # ── Helpers + sync accessors ─────────────────────────────────────────

    def _fmt(self, symbol: str) -> str:
        for quote in ("USDT", "USDC", "USD"):
            if symbol.endswith(quote):
                return f"{symbol[:-len(quote)]}/{quote}"
        return symbol

    def _float(self, v) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def get_funding(self, pair: str = "BTC/USDT") -> Optional[float]:
        return self.cached_value("funding_rate", pair)

    def get_open_interest(self, pair: str = "BTC/USDT") -> Optional[float]:
        return self.cached_value("open_interest", pair)

    def get_mark_price(self, pair: str = "BTC/USDT") -> Optional[float]:
        return self.cached_value("mark_price", pair)
