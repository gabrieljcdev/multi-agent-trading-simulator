"""
data_sources/sources/frankfurter.py

Free FX rates via api.frankfurter.dev (ECB reference rates).

No key, no rate limit. We pull every settings.FRANKFURTER_PAIRS pair on
the /latest endpoint plus the previous publication (latest_date - 1
calendar day) to derive a 24h change. The DXY proxy uses the canonical
ICE geometric formula:

    DXY = 50.14348112
        × EURUSD^(-0.576)
        × USDJPY^( 0.136)
        × GBPUSD^(-0.119)
        × USDCAD^( 0.091)
        × USDSEK^( 0.042)
        × USDCHF^( 0.036)

X/USD pairs use a negative exponent (so a stronger USD lowers the rate
and raises the index); USD/X pairs use a positive exponent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)

# v2 is "current" per the API root, but at the time of this change /v2
# returns 404 on /latest — only the openapi spec is served. v1 is frozen
# and stable, which is what we want for a data feed.
BASE = "https://api.frankfurter.dev/v1"

# (pair, signed_exponent). Negative exponent for X/USD pairs (EUR/USD,
# GBP/USD) because a stronger USD lowers those rates while raising DXY.
DXY_LEGS: list[tuple[str, float]] = [
    ("EUR/USD", -0.576),
    ("USD/JPY",  0.136),
    ("GBP/USD", -0.119),
    ("USD/CAD",  0.091),
    ("USD/SEK",  0.042),
    ("USD/CHF",  0.036),
]

# Calibration constant that anchors the index to 100 at March 1973.
DXY_BASE = 50.14348112

# Pair → (base, quote) for Frankfurter requests. Frankfurter takes one
# `from` currency and returns rates against every other listed `to`.
_PAIR_FETCH: dict[str, tuple[str, str]] = {
    "EUR/USD": ("EUR", "USD"),
    "GBP/USD": ("GBP", "USD"),
    "AUD/USD": ("AUD", "USD"),
    "NZD/USD": ("NZD", "USD"),
    "USD/JPY": ("USD", "JPY"),
    "USD/CHF": ("USD", "CHF"),
    "USD/CAD": ("USD", "CAD"),
    "USD/SEK": ("USD", "SEK"),
}


class FrankfurterSource(BaseDataSource):
    source_id        = "frankfurter"
    display_name     = "Frankfurter (FX)"
    refresh_interval = settings.FRANKFURTER_REFRESH_SEC
    optional         = True
    requires_api_key = False

    METRICS = (
        "fx_rate",        # per-pair rate
        "fx_change_24h",  # per-pair % change vs previous publication
        "dxy",            # geometric DXY proxy
        "dxy_change_24h", # proxy 24h change
    )

    def list_metrics(self) -> list[str]:
        return list(self.METRICS)

    async def fetch_all(self) -> list[DataPoint]:
        pairs = list(settings.FRANKFURTER_PAIRS)

        try:
            today = await self._fetch_rates(pairs)  # /latest
            yesterday = {}
            latest_date = today.get("_date", "")
            if latest_date:
                # Walk back one CALENDAR day from /latest's published
                # date. Frankfurter returns the latest *available* rates
                # ≤ the requested date, so weekends/holidays roll back
                # naturally — what matters is that we don't ask for the
                # same date /latest already gave us.
                try:
                    prev_dt = datetime.strptime(latest_date, "%Y-%m-%d") - timedelta(days=1)
                    yesterday = await self._fetch_rates(
                        pairs, target_date=prev_dt.strftime("%Y-%m-%d"),
                    )
                except Exception as e:
                    logger.debug(f"frankfurter previous-day fetch failed: {e}")
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
            if prev and prev != 0:
                change_pct = (rate - prev) / prev * 100.0
                points.append(DataPoint(
                    source_id=self.source_id, metric="fx_change_24h",
                    symbol=pair, value=change_pct,
                    raw_data={
                        "today":     rate,
                        "yesterday": prev,
                        "today_date":     today.get("_date"),
                        "yesterday_date": yesterday.get("_date"),
                    },
                ))

        dxy = self._dxy_proxy(today)
        if dxy is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="dxy", value=dxy,
                raw_data={"legs": [pair for pair, _ in DXY_LEGS]},
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
        target_date: Optional[str] = None,
    ) -> dict:
        """Returns {pair: rate, "_date": <iso>} for the requested pairs.

        target_date is None → /latest. Otherwise /<YYYY-MM-DD>.
        Pairs are grouped by `from` currency so each `from` only costs
        one HTTP round-trip.
        """
        # Group pairs by base currency so we minimise calls.
        bases: dict[str, list[str]] = {}
        for pair in pairs:
            base, quote = _PAIR_FETCH.get(pair, (None, None))
            if base is None:
                continue
            bases.setdefault(base, []).append(quote)

        path = f"{BASE}/latest" if target_date is None else f"{BASE}/{target_date}"

        out: dict = {}
        timeout = aiohttp.ClientTimeout(total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for base, quotes in bases.items():
                params = {"base": base, "symbols": ",".join(sorted(set(quotes)))}
                async with session.get(path, params=params) as r:
                    payload = await r.json(content_type=None)
                rates = payload.get("rates", {}) or {}
                # Every response carries the same publication date — keep
                # the most recently-seen one (they should all match).
                if payload.get("date"):
                    out["_date"] = payload.get("date")
                for pair in pairs:
                    b, q = _PAIR_FETCH.get(pair, (None, None))
                    if b == base and q in rates:
                        out[pair] = float(rates[q])
        return out

    def _dxy_proxy(self, rates: dict) -> Optional[float]:
        """ICE DXY geometric product. Returns None if any required leg
        is missing.

        Each leg is rate^exponent — negative exponent for X/USD pairs so
        a stronger USD raises the index. Product times the calibration
        constant 50.14348112 anchors the basket to 100 at March 1973.
        """
        product = 1.0
        for pair, exponent in DXY_LEGS:
            rate = rates.get(pair)
            if rate is None or rate <= 0:
                return None
            product *= rate ** exponent
        return DXY_BASE * product
