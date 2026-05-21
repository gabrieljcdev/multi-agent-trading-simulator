"""
data_sources/sources/fred.py

Federal Reserve Economic Data via fred.stlouisfed.org. Requires a free
API key set as FRED_API_KEY in keys.env. Pulls a small fixed set of
series (settings.FRED_SERIES) used by the Macro panel + quality gate's
yield-curve modifier.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)

OBS_ENDPOINT = "https://api.stlouisfed.org/fred/series/observations"

# Series ID → exposed metric name. Keeps the public-facing metric vocab
# stable even if FRED renames a series.
_SERIES_TO_METRIC = {
    "CPIAUCSL": "cpi",
    "DGS10":    "yield_10y",
    "DGS2":     "yield_2y",
    "DGS30":    "yield_30y",
    "DFF":      "fed_funds",
    "M2SL":     "m2",
    "UNRATE":   "unemployment",
    "T10Y2Y":   "yield_curve_spread",
}


class FREDSource(BaseDataSource):
    source_id        = "fred"
    display_name     = "FRED"
    refresh_interval = settings.FRED_REFRESH_SEC
    optional         = True
    requires_api_key = True
    api_key_env_var  = "FRED_API_KEY"

    def list_metrics(self) -> list[str]:
        # Order matches settings.FRED_SERIES.
        return [_SERIES_TO_METRIC[s] for s in settings.FRED_SERIES
                if s in _SERIES_TO_METRIC]

    async def fetch_all(self) -> list[DataPoint]:
        api_key = os.getenv(self.api_key_env_var, "")
        if not api_key:
            return [DataPoint(
                source_id=self.source_id, metric="api_key",
                value=0.0, error="missing FRED_API_KEY",
            )]

        points: list[DataPoint] = []
        timeout = aiohttp.ClientTimeout(total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for series_id in settings.FRED_SERIES:
                metric = _SERIES_TO_METRIC.get(series_id)
                if metric is None:
                    continue
                try:
                    value, observation_date = await self._fetch_latest(session, series_id, api_key)
                    if value is None:
                        continue
                    points.append(DataPoint(
                        source_id=self.source_id, metric=metric,
                        value=value,
                        raw_data={"series_id": series_id, "date": observation_date},
                    ))
                except Exception as e:
                    logger.debug(f"fred fetch {series_id}: {e}")
                    points.append(DataPoint(
                        source_id=self.source_id, metric=metric,
                        value=0.0, error=str(e),
                    ))
        return points

    async def _fetch_latest(
        self,
        session: aiohttp.ClientSession,
        series_id: str,
        api_key: str,
    ) -> tuple[Optional[float], str]:
        params = {
            "series_id":         series_id,
            "api_key":           api_key,
            "file_type":         "json",
            "sort_order":        "desc",
            "limit":             5,         # latest may be ".", grab a few
        }
        async with session.get(OBS_ENDPOINT, params=params) as r:
            payload = await r.json(content_type=None)
        for obs in payload.get("observations") or []:
            raw = obs.get("value", "")
            # FRED uses "." for "no data yet" — skip and try the next.
            if raw and raw != ".":
                try:
                    return float(raw), obs.get("date", "")
                except ValueError:
                    continue
        return None, ""

    # ── Sync convenience accessors ───────────────────────────────────────

    def get_cpi(self) -> Optional[float]:
        return self.cached_value("cpi")

    def get_10y_yield(self) -> Optional[float]:
        return self.cached_value("yield_10y")

    def get_2y_yield(self) -> Optional[float]:
        return self.cached_value("yield_2y")

    def get_30y_yield(self) -> Optional[float]:
        return self.cached_value("yield_30y")

    def get_fed_funds(self) -> Optional[float]:
        return self.cached_value("fed_funds")

    def get_m2(self) -> Optional[float]:
        return self.cached_value("m2")

    def get_unemployment(self) -> Optional[float]:
        return self.cached_value("unemployment")

    def get_yield_curve_spread(self) -> Optional[float]:
        """10Y minus 2Y. Negative = inverted (historical recession signal)."""
        direct = self.cached_value("yield_curve_spread")
        if direct is not None:
            return direct
        # FRED's T10Y2Y series sometimes lags; fall back to subtracting
        # the legs we already cached.
        ten = self.get_10y_yield()
        two = self.get_2y_yield()
        if ten is None or two is None:
            return None
        return ten - two

    def is_yield_curve_inverted(self) -> bool:
        spread = self.get_yield_curve_spread()
        return spread is not None and spread < 0
