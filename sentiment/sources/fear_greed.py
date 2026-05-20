"""
sentiment/sources/fear_greed.py

Alternative.me Fear & Greed Index — the only source the aggregator
treats as non-optional. No API key required.
"""

import logging
from typing import Optional

import aiohttp

from config import settings
from sentiment.base import BaseSentimentSource, SourceResult

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.alternative.me/fng/?limit=1"


class FearGreedSource(BaseSentimentSource):
    source_id        = "fear_greed"
    refresh_interval = 900          # 15 min
    optional         = False        # core source — failure logs loud

    def __init__(self):
        super().__init__()
        # Settings override class default. Falls back if missing.
        self.weight = getattr(settings, "SENTIMENT_WEIGHT_FEAR_GREED", 0.4)

    async def fetch(self) -> SourceResult:
        try:
            timeout = aiohttp.ClientTimeout(total=settings.SENTIMENT_HTTP_TIMEOUT_SEC)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(ENDPOINT) as r:
                    payload = await r.json(content_type=None)

            entry = (payload.get("data") or [{}])[0]
            value = int(entry.get("value", 50))
            label = entry.get("value_classification", "neutral")
            ts    = entry.get("timestamp", "")

            # Map 0..100 (extreme fear → extreme greed) to -100..+100
            score = (value - 50) * 2.0

            return SourceResult(
                source_id=self.source_id,
                score=score,
                confidence=1.0,
                raw_data={"value": value, "label": label, "timestamp": ts},
            )
        except Exception as e:
            logger.warning(f"fear_greed fetch failed: {e}")
            return SourceResult(
                source_id=self.source_id,
                score=0.0,
                confidence=0.0,
                raw_data={"value": 50, "label": "neutral"},
                error=str(e),
            )
