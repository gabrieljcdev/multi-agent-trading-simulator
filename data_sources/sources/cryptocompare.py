"""
data_sources/sources/cryptocompare.py

CryptoCompare aggregated price + market-cap + 24h volume per coin.
Free tier with key: 250k requests/month — well under any single-bot
load at the default 5-minute refresh.

Overlaps with CoinGecko (also free, no-key) but uses a different feed
mix under the hood; useful as a cross-check / redundancy source. News
endpoints exist but the sentiment module already covers that ground
via cryptopanic + RSS — not duplicated here.

Convenience methods are sync (cached) so the dashboard and quality
gate can read without awaiting.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)


class CryptoCompareSource(BaseDataSource):
    source_id        = "cryptocompare"
    display_name     = "CryptoCompare"
    refresh_interval = settings.CRYPTOCOMPARE_REFRESH_SEC
    optional         = True
    requires_api_key = True
    api_key_env_var  = "CRYPTOCOMPARE_API_KEY"

    def list_metrics(self) -> list[str]:
        return [
            "price",          # per-symbol latest price
            "volume_24h",     # per-symbol 24h aggregated volume (USD)
            "market_cap",     # per-symbol mcap (USD)
            "change_pct_24h", # per-symbol 24h % change
        ]

    async def fetch_all(self) -> list[DataPoint]:
        api_key = os.getenv(self.api_key_env_var, "")
        if not api_key:
            return [DataPoint(
                source_id=self.source_id, metric="api_key",
                value=0.0, error="missing CRYPTOCOMPARE_API_KEY",
            )]

        fsyms = list(settings.CRYPTOCOMPARE_FSYMS)
        url = f"{settings.CRYPTOCOMPARE_BASE_URL}/pricemultifull"
        params = {
            "fsyms":   ",".join(fsyms),
            "tsyms":   settings.CRYPTOCOMPARE_TSYM,
            "api_key": api_key,
        }
        try:
            timeout = aiohttp.ClientTimeout(
                total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC,
            )
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as r:
                    payload = await r.json(content_type=None)
        except Exception as e:
            logger.warning(f"cryptocompare fetch failed: {e}")
            return [DataPoint(
                source_id=self.source_id, metric="price",
                value=0.0, error=str(e),
            )]

        raw = (payload or {}).get("RAW") or {}
        if not raw:
            # Empty / Message-only payload (rate limit, bad key, …).
            note = payload.get("Message") if isinstance(payload, dict) else "empty"
            return [DataPoint(
                source_id=self.source_id, metric="price",
                value=0.0, error=str(note or "empty response"),
            )]

        points: list[DataPoint] = []
        tsym = settings.CRYPTOCOMPARE_TSYM
        for fsym, by_tsym in raw.items():
            quote = by_tsym.get(tsym) if isinstance(by_tsym, dict) else None
            if not quote:
                continue
            pair = f"{fsym}/{tsym}"
            price   = quote.get("PRICE")
            vol_24h = quote.get("VOLUME24HOURTO")
            mcap    = quote.get("MKTCAP")
            chg     = quote.get("CHANGEPCT24HOUR")
            if price is not None:
                points.append(DataPoint(
                    source_id=self.source_id, metric="price",
                    symbol=pair, value=float(price),
                    raw_data={"raw": quote},
                ))
            if vol_24h is not None:
                points.append(DataPoint(
                    source_id=self.source_id, metric="volume_24h",
                    symbol=pair, value=float(vol_24h),
                ))
            if mcap is not None:
                points.append(DataPoint(
                    source_id=self.source_id, metric="market_cap",
                    symbol=pair, value=float(mcap),
                ))
            if chg is not None:
                points.append(DataPoint(
                    source_id=self.source_id, metric="change_pct_24h",
                    symbol=pair, value=float(chg),
                ))
        return points

    # ── Sync convenience accessors ───────────────────────────────────────

    def get_price(self, fsym: str = "BTC") -> Optional[float]:
        return self.cached_value(
            "price", f"{fsym}/{settings.CRYPTOCOMPARE_TSYM}",
        )

    def get_volume_24h(self, fsym: str = "BTC") -> Optional[float]:
        return self.cached_value(
            "volume_24h", f"{fsym}/{settings.CRYPTOCOMPARE_TSYM}",
        )

    def get_market_cap(self, fsym: str = "BTC") -> Optional[float]:
        return self.cached_value(
            "market_cap", f"{fsym}/{settings.CRYPTOCOMPARE_TSYM}",
        )

    def get_change_pct_24h(self, fsym: str = "BTC") -> Optional[float]:
        return self.cached_value(
            "change_pct_24h", f"{fsym}/{settings.CRYPTOCOMPARE_TSYM}",
        )
