"""
sentiment/sources/google_trends.py

Search interest as a slow sentiment signal. pytrends is sync — calls
are wrapped in run_in_executor.

The "score" here is rising-vs-baseline: current interest minus 50,
scaled. We weight this lightly (0.1) because Google Trends moves on
a longer cycle than the rest of the stack.
"""

import asyncio
import logging
from typing import Optional

from config import settings
from sentiment.base import BaseSentimentSource, SourceResult

logger = logging.getLogger(__name__)

KEYWORDS = ["bitcoin", "crypto", "ethereum"]
TIMEFRAME = "now 7-d"


class GoogleTrendsSource(BaseSentimentSource):
    source_id        = "google_trends"
    refresh_interval = 3600           # 1 hour
    optional         = True

    def __init__(self):
        super().__init__()
        self.weight = getattr(settings, "SENTIMENT_WEIGHT_GOOGLE_TRENDS", 0.1)

    def is_available(self) -> bool:
        try:
            import pytrends  # noqa: F401
            return True
        except ImportError:
            return False

    async def fetch(self) -> SourceResult:
        if not self.is_available():
            return SourceResult(
                source_id=self.source_id,
                score=0.0, confidence=0.0,
                raw_data={"reason": "pytrends not installed"},
                error="not available",
            )
        try:
            data = await asyncio.get_event_loop().run_in_executor(None, self._fetch_sync)
            return SourceResult(
                source_id=self.source_id,
                score=data["score"],
                confidence=0.6,        # slow signal — fixed confidence per spec
                raw_data=data["raw"],
            )
        except Exception as e:
            logger.warning(f"google_trends fetch failed: {e}")
            return SourceResult(
                source_id=self.source_id,
                score=0.0, confidence=0.0,
                raw_data={},
                error=str(e),
            )

    def _fetch_sync(self) -> dict:
        from pytrends.request import TrendReq

        pytrends = TrendReq(hl="en-US", tz=0)
        pytrends.build_payload(KEYWORDS, cat=0, timeframe=TIMEFRAME, geo="", gprop="")
        df = pytrends.interest_over_time()
        if df is None or df.empty:
            return {"score": 0.0, "raw": {"reason": "empty payload"}}

        # Use the mean across kw of the latest reading as the "current"
        latest = df[KEYWORDS].iloc[-1].mean()
        score = (float(latest) - 50.0) * 2.0
        return {
            "score": max(-100.0, min(100.0, score)),
            "raw": {
                "keywords":         KEYWORDS,
                "timeframe":        TIMEFRAME,
                "current_interest": float(latest),
            },
        }
