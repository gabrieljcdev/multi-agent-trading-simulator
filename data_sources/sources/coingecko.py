"""
data_sources/sources/coingecko.py

CoinGecko /global — broad crypto-market context.

Works without a key (public free tier, ~10-30 calls/min). If
COINGECKO_API_KEY is set the request is signed with `x-cg-demo-api-key`
on the public base URL (demo plan, ~30 calls/min, 10k/month) or with
`x-cg-pro-api-key` against pro-api.coingecko.com when COINGECKO_USE_PRO
is True. Same endpoint shape in every case.

Single endpoint, five metrics — well under any rate limit at the
default 5-minute refresh.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)

PUBLIC_BASE = "https://api.coingecko.com/api/v3"
PRO_BASE    = "https://pro-api.coingecko.com/api/v3"


class CoinGeckoSource(BaseDataSource):
    source_id        = "coingecko"
    display_name     = "CoinGecko"
    refresh_interval = settings.COINGECKO_REFRESH_SEC
    optional         = True
    # Public tier works without a key — set False so the aggregator
    # treats us as always-available. The fetch path uses the key only to
    # raise rate limits when one is present.
    requires_api_key = False
    api_key_env_var  = "COINGECKO_API_KEY"

    def list_metrics(self) -> list[str]:
        return [
            "btc_dominance",
            "eth_dominance",
            "total_market_cap",
            "total_volume_24h",
            "market_cap_change_pct_24h",
        ]

    async def fetch_all(self) -> list[DataPoint]:
        base, headers = self._auth()
        url = f"{base}/global"

        try:
            timeout = aiohttp.ClientTimeout(total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as r:
                    payload = await r.json(content_type=None)
        except Exception as e:
            logger.warning(f"coingecko fetch failed: {e}")
            return [DataPoint(
                source_id=self.source_id, metric="btc_dominance",
                value=0.0, error=str(e),
            )]

        data = (payload or {}).get("data") or {}
        if not data:
            note = payload.get("status", {}).get("error_message") if isinstance(payload, dict) else "empty"
            return [DataPoint(
                source_id=self.source_id, metric="btc_dominance",
                value=0.0, error=str(note or "empty response"),
            )]

        mcap_pct = data.get("market_cap_percentage") or {}
        total_mcap = (data.get("total_market_cap") or {}).get("usd")
        total_vol  = (data.get("total_volume")     or {}).get("usd")
        mcap_chg   = data.get("market_cap_change_percentage_24h_usd")

        points: list[DataPoint] = []

        if mcap_pct.get("btc") is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="btc_dominance",
                value=float(mcap_pct["btc"]),
                raw_data={"sample": dict(list(mcap_pct.items())[:10])},
            ))
        if mcap_pct.get("eth") is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="eth_dominance",
                value=float(mcap_pct["eth"]),
            ))
        if total_mcap is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="total_market_cap",
                value=float(total_mcap),
                raw_data={"unit": "USD"},
            ))
        if total_vol is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="total_volume_24h",
                value=float(total_vol),
                raw_data={"unit": "USD"},
            ))
        if mcap_chg is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="market_cap_change_pct_24h",
                value=float(mcap_chg),
            ))

        return points

    def _auth(self) -> tuple[str, dict]:
        """Resolve base URL + auth header from COINGECKO_USE_PRO + key env."""
        api_key = os.getenv(self.api_key_env_var, "")
        if settings.COINGECKO_USE_PRO and api_key:
            return PRO_BASE, {"x-cg-pro-api-key": api_key}
        if api_key:
            # Demo key on the public base URL — higher rate limits.
            return PUBLIC_BASE, {"x-cg-demo-api-key": api_key}
        return PUBLIC_BASE, {}

    # ── Sync convenience accessors (dashboard reads these) ────────────────

    def get_btc_dominance(self) -> Optional[float]:
        return self.cached_value("btc_dominance")

    def get_eth_dominance(self) -> Optional[float]:
        return self.cached_value("eth_dominance")

    def get_total_market_cap(self) -> Optional[float]:
        return self.cached_value("total_market_cap")

    def get_total_volume_24h(self) -> Optional[float]:
        return self.cached_value("total_volume_24h")

    def get_market_cap_change_pct_24h(self) -> Optional[float]:
        return self.cached_value("market_cap_change_pct_24h")
