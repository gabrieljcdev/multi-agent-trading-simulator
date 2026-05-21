"""
data_sources/sources/us_treasury.py

US Treasury daily yield curve via the public CSV feed. Free, no auth.

Heavily redundant with FRED (DGS10/DGS2/DGS30 already wired through
the fred source) — kept for cross-source verification and because the
spec calls for it. If FRED ever goes down or rate-limits, this is a
fallback. Otherwise FRED is preferred.

CSV columns (date + each yield tenor) come back newest-first; we
read the first data row and surface the standard tenors as
DataPoints. Refresh hourly because the CSV updates around 17:00 ET.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)

# CSV header → public metric name. Anything not listed here is ignored.
_HEADER_TO_METRIC = {
    "1 Mo":  "yield_1m",
    "3 Mo":  "yield_3m",
    "6 Mo":  "yield_6m",
    "1 Yr":  "yield_1y",
    "2 Yr":  "yield_2y",
    "5 Yr":  "yield_5y",
    "10 Yr": "yield_10y",
    "20 Yr": "yield_20y",
    "30 Yr": "yield_30y",
}


class USTreasurySource(BaseDataSource):
    source_id        = "us_treasury"
    display_name     = "US Treasury (yields)"
    refresh_interval = settings.US_TREASURY_REFRESH_SEC
    optional         = True
    requires_api_key = False

    def list_metrics(self) -> list[str]:
        return list(_HEADER_TO_METRIC.values()) + ["yield_curve_2_10_spread"]

    async def fetch_all(self) -> list[DataPoint]:
        # The CSV endpoint is filtered by year-month. Pull the current
        # month; the first row is the latest published day.
        ym = datetime.utcnow().strftime("%Y%m")
        url = f"{settings.US_TREASURY_BASE_URL}/{ym}"
        params = {
            "type": "daily_treasury_yield_curve",
            "field_tdr_date_value_month": ym,
        }
        try:
            timeout = aiohttp.ClientTimeout(
                total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC,
            )
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as r:
                    body = await r.text()
        except Exception as e:
            logger.warning(f"us_treasury fetch failed: {e}")
            return [DataPoint(
                source_id=self.source_id, metric="yield_10y",
                value=0.0, error=str(e),
            )]

        reader = csv.reader(io.StringIO(body))
        try:
            header = next(reader)
            row    = next(reader)
        except StopIteration:
            return [DataPoint(
                source_id=self.source_id, metric="yield_10y",
                value=0.0, error="empty CSV",
            )]

        date_str = row[0] if row else ""
        col_to_value: dict[str, Optional[float]] = {}
        for col, raw in zip(header, row):
            if col == "Date":
                continue
            try:
                col_to_value[col] = float(raw)
            except (TypeError, ValueError):
                col_to_value[col] = None

        points: list[DataPoint] = []
        for col_name, metric in _HEADER_TO_METRIC.items():
            v = col_to_value.get(col_name)
            if v is None:
                continue
            points.append(DataPoint(
                source_id=self.source_id, metric=metric,
                value=v, raw_data={"date": date_str},
            ))

        # Derived spread — convenience for the macro module's
        # yield-curve check; comes back the same shape as FRED's.
        ten = col_to_value.get("10 Yr")
        two = col_to_value.get("2 Yr")
        if ten is not None and two is not None:
            points.append(DataPoint(
                source_id=self.source_id,
                metric="yield_curve_2_10_spread",
                value=ten - two,
                raw_data={"date": date_str},
            ))
        return points

    # ── Sync accessors ───────────────────────────────────────────────────

    def get_10y_yield(self) -> Optional[float]:
        return self.cached_value("yield_10y")

    def get_2y_yield(self) -> Optional[float]:
        return self.cached_value("yield_2y")

    def get_30y_yield(self) -> Optional[float]:
        return self.cached_value("yield_30y")

    def get_yield_curve_spread(self) -> Optional[float]:
        return self.cached_value("yield_curve_2_10_spread")
