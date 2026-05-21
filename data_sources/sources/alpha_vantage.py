"""
data_sources/sources/alpha_vantage.py

Alpha Vantage proxies for traditional-market context: SPY, QQQ, GLD, TLT.

Free tier is 25 calls/day, ~1/sec burst — we rate-limit aggressively via
ALPHA_VANTAGE_DAILY_CALL_BUDGET (daily) and ALPHA_VANTAGE_PACE_SEC
(per-call sleep). Once the daily budget is exhausted the source returns
the last cached values with error="rate_limit_exhausted" so the quality
gate and dashboard degrade gracefully.

VIX deliberately not here — Alpha Vantage's GLOBAL_QUOTE returns empty
for non-tradeable indices. VIX lives at FRED (series VIXCLS), and the
risk-regime classifier is on FREDSource.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from config import settings
from data_sources.base import BaseDataSource, DataPoint

logger = logging.getLogger(__name__)

ENDPOINT = "https://www.alphavantage.co/query"


class AlphaVantageSource(BaseDataSource):
    source_id        = "alpha_vantage"
    display_name     = "Alpha Vantage"
    refresh_interval = settings.ALPHA_VANTAGE_REFRESH_SEC
    optional         = True
    requires_api_key = True
    api_key_env_var  = "ALPHA_VANTAGE_API_KEY"

    def __init__(self):
        super().__init__()
        # Per-day call counter. (date, count) — resets on the first call
        # of a new UTC day. Plenty good for a 20/day budget; we don't need
        # per-minute throttling because refresh_interval (15m) already
        # produces ~1 sweep per 15 min.
        self._call_date: Optional[str] = None
        self._calls_today: int = 0

    def list_metrics(self) -> list[str]:
        return [
            "spy", "spy_change_pct",
            "qqq", "qqq_change_pct",
            "gld", "tlt",
        ]

    async def fetch_all(self) -> list[DataPoint]:
        api_key = os.getenv(self.api_key_env_var, "")
        if not api_key:
            return [DataPoint(
                source_id=self.source_id, metric="api_key",
                value=0.0, error="missing ALPHA_VANTAGE_API_KEY",
            )]

        # Budget gate: each fetch_all() needs one call per symbol — refuse
        # if we'd blow the daily budget.
        self._reset_budget_if_new_day()
        symbols = list(settings.ALPHA_VANTAGE_SYMBOLS)
        needed  = len(symbols)
        if self._calls_today + needed > settings.ALPHA_VANTAGE_DAILY_CALL_BUDGET:
            logger.info(
                f"alpha_vantage budget exhausted "
                f"({self._calls_today}/{settings.ALPHA_VANTAGE_DAILY_CALL_BUDGET}) "
                "— returning cached values"
            )
            return [DataPoint(
                source_id=self.source_id, metric="budget",
                value=float(self._calls_today),
                error="rate_limit_exhausted",
            )]

        points: list[DataPoint] = []
        timeout = aiohttp.ClientTimeout(total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for i, symbol in enumerate(symbols):
                # Spread calls — free tier blocks >1/sec hard. The sleep
                # only happens between calls (not before the first or
                # after the last) so a single-symbol fetch stays fast.
                if i > 0:
                    await asyncio.sleep(settings.ALPHA_VANTAGE_PACE_SEC)
                try:
                    points.extend(await self._fetch_symbol(session, symbol, api_key))
                except Exception as e:
                    logger.debug(f"alpha_vantage {symbol}: {e}")
                    points.append(DataPoint(
                        source_id=self.source_id, metric=symbol.lower(),
                        value=0.0, error=str(e),
                    ))
                self._calls_today += 1
        return points

    async def _fetch_symbol(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        api_key: str,
    ) -> list[DataPoint]:
        params = {"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": api_key}
        async with session.get(ENDPOINT, params=params) as r:
            payload = await r.json(content_type=None)

        quote = payload.get("Global Quote") or {}
        if not quote:
            # Alpha Vantage returns {"Note": "..."} when rate-limited.
            note = payload.get("Note") or payload.get("Information") or "empty response"
            raise RuntimeError(note)

        price = float(quote.get("05. price") or 0.0)
        change_pct_raw = (quote.get("10. change percent") or "").rstrip("%")
        try:
            change_pct = float(change_pct_raw) if change_pct_raw else None
        except ValueError:
            change_pct = None

        metric_name = self._metric_for(symbol)
        points = [DataPoint(
            source_id=self.source_id, metric=metric_name,
            value=price,
            raw_data={"symbol": symbol, "global_quote": quote},
        )]
        # Only some symbols carry a meaningful daily change column we expose
        # by name. Everything else still has the raw quote under raw_data.
        if change_pct is not None and metric_name in ("spy", "qqq"):
            points.append(DataPoint(
                source_id=self.source_id,
                metric=f"{metric_name}_change_pct",
                value=change_pct,
                raw_data={"symbol": symbol},
            ))
        return points

    def _metric_for(self, symbol: str) -> str:
        return {
            "SPY": "spy",
            "QQQ": "qqq",
            "GLD": "gld",
            "TLT": "tlt",
        }.get(symbol, symbol.lower())

    def _reset_budget_if_new_day(self) -> None:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        if self._call_date != today:
            self._call_date = today
            self._calls_today = 0

    # ── Sync convenience accessors ───────────────────────────────────────

    def get_spy(self) -> Optional[float]:
        return self.cached_value("spy")

    def get_spy_change_pct(self) -> Optional[float]:
        return self.cached_value("spy_change_pct")

    def get_qqq(self) -> Optional[float]:
        return self.cached_value("qqq")

    def get_gold(self) -> Optional[float]:
        return self.cached_value("gld")
