"""
data_sources/sources/world_bank.py

World Bank Open Data — GDP growth, inflation, unemployment, one
indicator × one country per HTTP call. Free, no auth.

World Bank data is annual; refresh once a day is plenty. We pull a
fixed set of indicators (settings.WORLD_BANK_INDICATORS) for the
countries in settings.WORLD_BANK_COUNTRIES (default US, Eurozone,
China, Japan, UK). Each (country, indicator) becomes a DataPoint
keyed by symbol=country_iso3.

Mostly feeds the macro module's "global GDP / global inflation"
context — slow inputs that nudge the scenario label between
GOLDILOCKS / STAGFLATION rather than driving fast trade decisions.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)


class WorldBankSource(BaseDataSource):
    source_id        = "world_bank"
    display_name     = "World Bank"
    refresh_interval = settings.WORLD_BANK_REFRESH_SEC
    optional         = True
    requires_api_key = False

    def list_metrics(self) -> list[str]:
        return list(settings.WORLD_BANK_INDICATORS.keys())

    async def fetch_all(self) -> list[DataPoint]:
        countries = list(settings.WORLD_BANK_COUNTRIES)
        indicators = dict(settings.WORLD_BANK_INDICATORS)

        timeout = aiohttp.ClientTimeout(
            total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC,
        )
        tasks = []
        keys: list[tuple[str, str, str]] = []  # (metric, country, wb_code)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for metric, wb_code in indicators.items():
                for country in countries:
                    keys.append((metric, country, wb_code))
                    tasks.append(self._fetch_one(session, country, wb_code))
            results = await asyncio.gather(*tasks, return_exceptions=True)

        points: list[DataPoint] = []
        for (metric, country, wb_code), result in zip(keys, results):
            if isinstance(result, Exception):
                logger.debug(f"world_bank {country}/{wb_code}: {result}")
                points.append(DataPoint(
                    source_id=self.source_id, metric=metric,
                    symbol=country, value=0.0, error=str(result),
                ))
                continue
            value, year = result
            if value is None:
                points.append(DataPoint(
                    source_id=self.source_id, metric=metric,
                    symbol=country, value=0.0,
                    error="no_recent_observation",
                ))
                continue
            points.append(DataPoint(
                source_id=self.source_id, metric=metric,
                symbol=country, value=float(value),
                raw_data={"indicator": wb_code, "year": year},
            ))
        return points

    async def _fetch_one(
        self,
        session: aiohttp.ClientSession,
        country: str,
        wb_code: str,
    ) -> tuple[Optional[float], Optional[str]]:
        """Pull the most-recent non-null observation for (country, indicator).

        World Bank returns annual data with a long tail of None values
        for very recent years until they're published. We walk back
        through the returned page to find the freshest real reading.
        """
        url = f"{settings.WORLD_BANK_BASE_URL}/country/{country}/indicator/{wb_code}"
        # date range walks back 5 years so we usually catch one published value.
        from datetime import datetime as _dt
        this_year = _dt.utcnow().year
        params = {
            "format":   "json",
            "date":     f"{this_year - 5}:{this_year}",
            "per_page": 10,
        }
        async with session.get(url, params=params) as r:
            payload = await r.json(content_type=None)
        # World Bank's shape is [meta, [obs, …]]
        if not isinstance(payload, list) or len(payload) < 2:
            return None, None
        for obs in payload[1] or []:
            v = obs.get("value")
            if v is not None:
                return v, obs.get("date")
        return None, None

    # ── Sync accessors ───────────────────────────────────────────────────

    def get_gdp_growth(self, country: str = "USA") -> Optional[float]:
        return self.cached_value("gdp_growth_pct", country)

    def get_inflation(self, country: str = "USA") -> Optional[float]:
        return self.cached_value("inflation_pct", country)

    def get_unemployment(self, country: str = "USA") -> Optional[float]:
        return self.cached_value("unemployment_pct", country)
