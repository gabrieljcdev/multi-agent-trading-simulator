"""
data_sources/sources/binance_futures.py

Binance USDT-M perpetual derivatives — open interest, current funding
rate, top-trader long/short account ratio.

Public endpoints, no auth, no rate-limit headroom needed at the default
60-second refresh. More reliable than Coinglass's free tier and covers
the same metrics; running both gives a cross-check.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)


class BinanceFuturesSource(BaseDataSource):
    source_id        = "binance_futures"
    display_name     = "Binance Futures"
    refresh_interval = settings.BINANCE_FUTURES_REFRESH_SEC
    optional         = True
    requires_api_key = False

    def list_metrics(self) -> list[str]:
        return [
            "open_interest",       # per-symbol contracts (base units)
            "funding_rate",        # per-symbol latest funding (decimal)
            "long_short_ratio",    # top traders, account-weighted
        ]

    async def fetch_all(self) -> list[DataPoint]:
        symbols = list(settings.BINANCE_FUTURES_SYMBOLS)
        timeout = aiohttp.ClientTimeout(total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            results = await asyncio.gather(
                *(self._fetch_for_symbol(session, s) for s in symbols),
                return_exceptions=True,
            )

        points: list[DataPoint] = []
        for symbol, result in zip(symbols, results):
            if isinstance(result, Exception):
                logger.debug(f"binance_futures {symbol}: {result}")
                points.append(DataPoint(
                    source_id=self.source_id, metric="open_interest",
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
        base = settings.BINANCE_FUTURES_BASE_URL
        pair = self._fmt(symbol)
        points: list[DataPoint] = []

        # Open interest.
        async with session.get(
            f"{base}/fapi/v1/openInterest",
            params={"symbol": symbol},
        ) as r:
            oi_payload = await r.json(content_type=None)
        oi_val = self._float(oi_payload.get("openInterest"))
        points.append(DataPoint(
            source_id=self.source_id, metric="open_interest",
            symbol=pair, value=oi_val or 0.0,
            error=None if oi_val is not None else "no_oi",
            raw_data={"raw": oi_payload},
        ))

        # Funding rate (latest single row).
        async with session.get(
            f"{base}/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": 1},
        ) as r:
            fr_payload = await r.json(content_type=None)
        funding = None
        if isinstance(fr_payload, list) and fr_payload:
            funding = self._float(fr_payload[0].get("fundingRate"))
        points.append(DataPoint(
            source_id=self.source_id, metric="funding_rate",
            symbol=pair, value=funding or 0.0,
            error=None if funding is not None else "no_funding",
        ))

        # Top-trader long/short account ratio (1h period, latest bucket).
        async with session.get(
            f"{base}/futures/data/topLongShortAccountRatio",
            params={"symbol": symbol, "period": "1h", "limit": 1},
        ) as r:
            ls_payload = await r.json(content_type=None)
        ratio = None
        if isinstance(ls_payload, list) and ls_payload:
            ratio = self._float(ls_payload[0].get("longShortRatio"))
        points.append(DataPoint(
            source_id=self.source_id, metric="long_short_ratio",
            symbol=pair, value=ratio or 1.0,
            error=None if ratio is not None else "no_ls",
        ))
        return points

    # ── Helpers + sync accessors ─────────────────────────────────────────

    def _fmt(self, symbol: str) -> str:
        """Binance uses BTCUSDT — surface as BTC/USDT to match the
        rest of the system's pair vocabulary."""
        for quote in ("USDT", "USDC", "BUSD", "USD"):
            if symbol.endswith(quote):
                return f"{symbol[:-len(quote)]}/{quote}"
        return symbol

    def _float(self, v) -> Optional[float]:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def get_open_interest(self, pair: str = "BTC/USDT") -> Optional[float]:
        return self.cached_value("open_interest", pair)

    def get_funding(self, pair: str = "BTC/USDT") -> Optional[float]:
        return self.cached_value("funding_rate", pair)

    def get_long_short_ratio(self, pair: str = "BTC/USDT") -> Optional[float]:
        return self.cached_value("long_short_ratio", pair)
