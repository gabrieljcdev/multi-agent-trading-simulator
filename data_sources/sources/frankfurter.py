"""
data_sources/sources/frankfurter.py

Free FX rates via api.frankfurter.app (ECB reference rates).

No key, no rate limit. We pull every settings.FRANKFURTER_PAIRS pair in
two calls (today + yesterday) so we can derive a 24h change and a
heuristic DXY proxy. The proxy is weighted to mirror ICE's basket but
calculated against today's rates only (Frankfurter doesn't publish DXY
directly).
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)

LATEST_ENDPOINT = "https://api.frankfurter.app/latest"

# ICE DXY weights — EUR is ~58% of true DXY weight, so the proxy tracks
# the real index closely enough for regime classification (RISK_OFF /
# STRONG / WEAK signals). Not suitable for trading the index itself.
DXY_WEIGHTS = {
    "EUR/USD": 0.576,
    "USD/JPY": 0.136,
    "GBP/USD": 0.119,
    "USD/CAD": 0.091,
    "USD/CHF": 0.036,
}

# Frankfurter uses USD as the canonical quote — for "USD/JPY" we ask
# for the JPY rate against USD, for "EUR/USD" we ask for USD against EUR.
_PAIR_FETCH: dict[str, tuple[str, str]] = {
    "EUR/USD": ("EUR", "USD"),
    "GBP/USD": ("GBP", "USD"),
    "AUD/USD": ("AUD", "USD"),
    "NZD/USD": ("NZD", "USD"),
    "USD/JPY": ("USD", "JPY"),
    "USD/CHF": ("USD", "CHF"),
    "USD/CAD": ("USD", "CAD"),
}


class FrankfurterSource(BaseDataSource):
    source_id        = "frankfurter"
    display_name     = "Frankfurter (FX)"
    refresh_interval = settings.FRANKFURTER_REFRESH_SEC
    optional         = True
    requires_api_key = False

    METRICS = (
        "fx_rate",        # per-pair rate
        "fx_change_24h",  # per-pair % change vs yesterday
        "dxy",            # weighted proxy
        "dxy_change_24h", # weighted proxy 24h change
    )

    def list_metrics(self) -> list[str]:
        return list(self.METRICS)

    async def fetch_all(self) -> list[DataPoint]:
        pairs = list(settings.FRANKFURTER_PAIRS)

        try:
            today = await self._fetch_rates(pairs)
            yesterday = await self._fetch_rates(pairs, days_ago=1)
        except Exception as e:
            logger.warning(f"frankfurter fetch failed: {e}")
            return [DataPoint(
                source_id=self.source_id, metric="fx_rate", value=0.0,
                error=str(e),
            )]

        points: list[DataPoint] = []
        for pair in pairs:
            rate = today.get(pair)
            prev = yesterday.get(pair)
            if rate is None:
                continue
            points.append(DataPoint(
                source_id=self.source_id, metric="fx_rate",
                symbol=pair, value=rate, raw_data={"date": today.get("_date")},
            ))
            if prev:
                change_pct = (rate - prev) / prev * 100.0
                points.append(DataPoint(
                    source_id=self.source_id, metric="fx_change_24h",
                    symbol=pair, value=change_pct,
                    raw_data={"today": rate, "yesterday": prev},
                ))

        dxy = self._dxy_proxy(today)
        if dxy is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="dxy", value=dxy,
                raw_data={"weights": DXY_WEIGHTS},
            ))
            prev_dxy = self._dxy_proxy(yesterday)
            if prev_dxy and prev_dxy != 0:
                points.append(DataPoint(
                    source_id=self.source_id, metric="dxy_change_24h",
                    value=(dxy - prev_dxy) / prev_dxy * 100.0,
                    raw_data={"today": dxy, "yesterday": prev_dxy},
                ))

        return points

    # ── Sync convenience accessors (dashboard reads these) ────────────────

    def get_dxy(self) -> Optional[float]:
        return self.cached_value("dxy")

    def get_dxy_change_24h(self) -> Optional[float]:
        return self.cached_value("dxy_change_24h")

    def get_eur_usd(self) -> Optional[float]:
        return self.cached_value("fx_rate", "EUR/USD")

    def get_gbp_usd(self) -> Optional[float]:
        return self.cached_value("fx_rate", "GBP/USD")

    def get_usd_jpy(self) -> Optional[float]:
        return self.cached_value("fx_rate", "USD/JPY")

    def is_dxy_strong(self) -> bool:
        dxy = self.get_dxy()
        if dxy is None:
            return False
        return dxy >= settings.DATA_DXY_STRONG_THRESHOLD

    def is_dxy_weak(self) -> bool:
        dxy = self.get_dxy()
        if dxy is None:
            return False
        return dxy <= settings.DATA_DXY_WEAK_THRESHOLD

    # ── Internals ────────────────────────────────────────────────────────

    async def _fetch_rates(
        self,
        pairs: list[str],
        days_ago: int = 0,
    ) -> dict:
        """Returns {pair: rate, "_date": <date>} for the requested pairs."""
        # Group pairs by base currency so we minimise round-trips. Each
        # base hits one Frankfurter call returning N quote rates.
        bases: dict[str, list[str]] = {}
        for pair in pairs:
            base, quote = _PAIR_FETCH.get(pair, (None, None))
            if base is None:
                continue
            bases.setdefault(base, []).append(quote)

        path = LATEST_ENDPOINT
        if days_ago > 0:
            from datetime import datetime, timedelta
            day = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            path = f"https://api.frankfurter.app/{day}"

        out: dict = {}
        timeout = aiohttp.ClientTimeout(total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for base, quotes in bases.items():
                params = {"from": base, "to": ",".join(set(quotes))}
                async with session.get(path, params=params) as r:
                    payload = await r.json(content_type=None)
                rates = payload.get("rates", {}) or {}
                out["_date"] = payload.get("date", "")
                for pair in pairs:
                    b, q = _PAIR_FETCH.get(pair, (None, None))
                    if b == base and q in rates:
                        out[pair] = float(rates[q])
        return out

    def _dxy_proxy(self, rates: dict) -> Optional[float]:
        """Weighted geometric-mean-ish proxy. Returns None if any required
        leg is missing. The formula matches the project spec — it's a
        rough proxy, not the actual ICE DXY calculation."""
        legs = []
        for pair, weight in DXY_WEIGHTS.items():
            rate = rates.get(pair)
            if rate is None or rate <= 0:
                return None
            if pair.startswith("USD/"):
                legs.append(weight * rate)
            else:
                # EUR/USD, GBP/USD — invert so a stronger USD raises the proxy.
                legs.append(weight * (1.0 / rate))
        return 100.0 * sum(legs)
