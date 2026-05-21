"""
data_sources/sources/imf.py

IMF Datamapper API — global macro forecasts (NGDPDPC/PCPIPCH/LUR
mainly). Free, no auth. Quarterly publication, so refresh once a day
is plenty.

Datamapper exposes a simple JSON shape:
  /v1/<indicator>/<country_or_group> →
    {"values": {<indicator>: {<country>: {<year>: value, ...}}}, ...}
We pick the most-recent year per (indicator × country) and emit a
DataPoint keyed by symbol=country_iso3.

Uses the Datamapper host (HTTPS, JSON) rather than the older SDMX
endpoint which returns XML and serves over HTTP.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)


class IMFSource(BaseDataSource):
    source_id        = "imf"
    display_name     = "IMF Datamapper"
    refresh_interval = settings.IMF_REFRESH_SEC
    optional         = True
    requires_api_key = False

    def list_metrics(self) -> list[str]:
        return list(settings.IMF_INDICATORS.keys())

    async def fetch_all(self) -> list[DataPoint]:
        indicators = dict(settings.IMF_INDICATORS)
        countries = list(settings.IMF_COUNTRIES)
        timeout = aiohttp.ClientTimeout(
            total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC,
        )

        # One indicator request returns every country, so this is N
        # calls (where N = len(IMF_INDICATORS)), not N × M.
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [
                self._fetch_indicator(session, metric, imf_code)
                for metric, imf_code in indicators.items()
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        points: list[DataPoint] = []
        for (metric, imf_code), result in zip(indicators.items(), results):
            if isinstance(result, Exception):
                logger.debug(f"imf {imf_code}: {result}")
                points.append(DataPoint(
                    source_id=self.source_id, metric=metric,
                    value=0.0, error=str(result),
                ))
                continue
            by_country: dict = result or {}
            for country in countries:
                series = by_country.get(country)
                if not isinstance(series, dict) or not series:
                    points.append(DataPoint(
                        source_id=self.source_id, metric=metric,
                        symbol=country, value=0.0, error="no_data",
                    ))
                    continue
                year, val = self._latest(series)
                if val is None:
                    continue
                points.append(DataPoint(
                    source_id=self.source_id, metric=metric,
                    symbol=country, value=float(val),
                    raw_data={"imf_code": imf_code, "year": year},
                ))
        return points

    async def _fetch_indicator(
        self,
        session: aiohttp.ClientSession,
        metric: str,
        imf_code: str,
    ) -> dict:
        url = f"{settings.IMF_DATAMAPPER_URL}/{imf_code}"
        async with session.get(url) as r:
            payload = await r.json(content_type=None)
        # Shape: {"values": {imf_code: {country: {year: value}}}}
        return ((payload or {}).get("values") or {}).get(imf_code) or {}

    def _latest(self, series: dict) -> tuple[Optional[str], Optional[float]]:
        """Most recent year with a non-None value."""
        for year in sorted(series.keys(), reverse=True):
            val = series[year]
            if val is not None:
                try:
                    return year, float(val)
                except (TypeError, ValueError):
                    continue
        return None, None

    # ── Sync accessors ───────────────────────────────────────────────────

    def get_gdp_per_capita(self, country: str = "USA") -> Optional[float]:
        return self.cached_value("gdp_per_capita", country)

    def get_inflation_yoy(self, country: str = "USA") -> Optional[float]:
        return self.cached_value("inflation_yoy_pct", country)

    def get_unemployment(self, country: str = "USA") -> Optional[float]:
        return self.cached_value("unemployment_pct", country)
