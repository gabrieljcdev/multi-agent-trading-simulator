"""
data_sources/sources/ecb.py

European Central Bank statistical data API — main refinancing rate,
deposit-facility rate, EUR/USD spot. Free, no auth. SDMX-JSON shape
that pivots a deep nested structure around dimension indices.

Refresh hourly: the rates change rarely but the spot moves daily.

Why this matters: gives the macro module a non-US central-bank rate
read. When the Fed is tightening but the ECB is easing, RateEnvironment
shifts dollar-weighted vs euro-weighted views — useful colour even
though our composite weight is small.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)


class ECBSource(BaseDataSource):
    source_id        = "ecb"
    display_name     = "ECB Stats"
    refresh_interval = settings.ECB_REFRESH_SEC
    optional         = True
    requires_api_key = False

    def list_metrics(self) -> list[str]:
        return [metric for _, _, metric in settings.ECB_SERIES]

    async def fetch_all(self) -> list[DataPoint]:
        series = list(settings.ECB_SERIES)
        timeout = aiohttp.ClientTimeout(
            total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC,
        )

        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [
                self._fetch_one(session, dataflow, key)
                for dataflow, key, _ in series
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        points: list[DataPoint] = []
        for (dataflow, key, metric), result in zip(series, results):
            if isinstance(result, Exception):
                logger.debug(f"ecb {dataflow}/{key}: {result}")
                points.append(DataPoint(
                    source_id=self.source_id, metric=metric,
                    value=0.0, error=str(result),
                ))
                continue
            if result is None:
                points.append(DataPoint(
                    source_id=self.source_id, metric=metric,
                    value=0.0, error="no_observation",
                ))
                continue
            points.append(DataPoint(
                source_id=self.source_id, metric=metric,
                value=float(result),
                raw_data={"dataflow": dataflow, "key": key},
            ))
        return points

    async def _fetch_one(
        self,
        session: aiohttp.ClientSession,
        dataflow: str,
        key: str,
    ) -> Optional[float]:
        """ECB SDMX-JSON: `dataSets[0].series[<dim-tuple>].observations[<idx>]`.
        Each observation is `[value, status, ...]`. We ask for the last
        observation only to keep the response minimal."""
        url = f"{settings.ECB_BASE_URL}/{dataflow}/{key}"
        params = {"lastNObservations": 1}
        headers = {"Accept": "application/json"}
        async with session.get(url, params=params, headers=headers) as r:
            payload = await r.json(content_type=None)

        datasets = payload.get("dataSets") or []
        if not datasets:
            return None
        series_dict = datasets[0].get("series") or {}
        if not series_dict:
            return None
        # Only one series per request — first (and only) entry.
        first_series = next(iter(series_dict.values()))
        obs = first_series.get("observations") or {}
        if not obs:
            return None
        # ECB uses "0" as the key for the single observation we asked for.
        first_obs = obs.get("0") or next(iter(obs.values()))
        if not first_obs:
            return None
        try:
            return float(first_obs[0])
        except (TypeError, ValueError, IndexError):
            return None

    # ── Sync accessors ───────────────────────────────────────────────────

    def get_refinancing_rate(self) -> Optional[float]:
        return self.cached_value("refinancing_rate")

    def get_deposit_facility_rate(self) -> Optional[float]:
        return self.cached_value("deposit_facility_rate")

    def get_eur_usd(self) -> Optional[float]:
        return self.cached_value("eur_usd_spot")
