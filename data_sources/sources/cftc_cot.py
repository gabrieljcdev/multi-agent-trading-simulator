"""
data_sources/sources/cftc_cot.py

CFTC Commitments of Traders disaggregated report — large speculator
positioning in CME Bitcoin futures. Weekly publication (Tuesdays for
the prior Tuesday's close), free, no auth.

Most useful as a slow-moving sentiment signal: extreme net-long
spec positioning historically marks local tops; extreme net-short
marks local bottoms. The bot is intra-day so this won't trigger
trades directly, but the macro module can pick it up as a regime
input later.

Fetches the latest row for settings.CFTC_CONTRACT (default
"BITCOIN - CHICAGO MERCANTILE EXCHANGE") and surfaces non-commercial
long/short positions + the derived net.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)


class CFTCCOTSource(BaseDataSource):
    source_id        = "cftc_cot"
    display_name     = "CFTC COT (Bitcoin)"
    refresh_interval = settings.CFTC_REFRESH_SEC
    optional         = True
    requires_api_key = False

    def list_metrics(self) -> list[str]:
        return [
            "spec_net",          # non-commercial long − short
            "spec_long",         # non-commercial long contracts
            "spec_short",        # non-commercial short contracts
            "open_interest",     # total OI all positions
            "long_short_ratio",  # non-commercial L / S
        ]

    async def fetch_all(self) -> list[DataPoint]:
        url = settings.CFTC_BASE_URL
        params = {
            "$limit": 1,
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "market_and_exchange_names": settings.CFTC_CONTRACT,
        }
        try:
            timeout = aiohttp.ClientTimeout(
                total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC,
            )
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as r:
                    payload = await r.json(content_type=None)
        except Exception as e:
            logger.warning(f"cftc_cot fetch failed: {e}")
            return [DataPoint(
                source_id=self.source_id, metric="spec_net",
                value=0.0, error=str(e),
            )]

        if not isinstance(payload, list) or not payload:
            return [DataPoint(
                source_id=self.source_id, metric="spec_net",
                value=0.0, error="empty response",
            )]
        row = payload[0]
        date = row.get("report_date_as_yyyy_mm_dd")

        def _int(field):
            v = row.get(field)
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        spec_long  = _int("noncomm_positions_long_all")
        spec_short = _int("noncomm_positions_short_all")
        oi         = _int("open_interest_all")
        spec_net = None
        ls_ratio = None
        if spec_long is not None and spec_short is not None:
            spec_net = spec_long - spec_short
            ls_ratio = spec_long / spec_short if spec_short else None

        points: list[DataPoint] = []
        raw = {"report_date": date, "row": row}
        if spec_long is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="spec_long",
                value=float(spec_long), raw_data=raw,
            ))
        if spec_short is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="spec_short",
                value=float(spec_short),
            ))
        if spec_net is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="spec_net",
                value=float(spec_net),
            ))
        if oi is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="open_interest",
                value=float(oi),
            ))
        if ls_ratio is not None:
            points.append(DataPoint(
                source_id=self.source_id, metric="long_short_ratio",
                value=float(ls_ratio),
            ))
        return points

    # ── Sync accessors ───────────────────────────────────────────────────

    def get_spec_net(self) -> Optional[float]:
        return self.cached_value("spec_net")

    def get_spec_long(self) -> Optional[float]:
        return self.cached_value("spec_long")

    def get_spec_short(self) -> Optional[float]:
        return self.cached_value("spec_short")

    def get_long_short_ratio(self) -> Optional[float]:
        return self.cached_value("long_short_ratio")
