"""
sentiment/sources/cryptopanic.py

News + community sentiment. Uses the CryptoPanic API when
CRYPTOPANIC_API_KEY is set in the env, otherwise falls back to a small
set of RSS feeds. Both paths feed the same keyword scorer, so the
output shape is consistent.

Hard blocks fire on a small set of catastrophic-event keywords —
exchange hack, protocol drain, emergency shutdown, etc. Those
short-circuit every signal in the system until the headline ages out
of the feed.
"""

import logging
import os
from typing import Optional

import aiohttp
import feedparser

from config import settings
from sentiment.base import BaseSentimentSource, SourceResult

logger = logging.getLogger(__name__)


POSITIVE_KEYWORDS = [
    "etf approved", "institutional", "partnership", "mainnet", "upgrade",
    "all-time high", "adoption", "integration", "reserve", "breakthrough",
]
NEGATIVE_KEYWORDS = [
    "hack", "exploit", "breach", "stolen", "bankruptcy", "lawsuit",
    "fraud", "crash", "collapse", "banned", "seized", "delisted",
    "rug", "exit scam", "ponzi",
]
BLOCKING_KEYWORDS = [
    "exchange hack", "major exploit", "protocol drained",
    "emergency shutdown", "systemic risk", "contagion",
]

# RSS fallback feeds used when no API key is present
RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
]

CRYPTOPANIC_ENDPOINT = (
    "https://cryptopanic.com/api/v1/posts/"
    "?auth_token={key}&filter=hot&public=true"
)


def _score_headlines(headlines: list[str]) -> tuple[float, float, bool, str, list[str]]:
    """Run the keyword scorer over a list of headline strings.

    Returns:
        score        — -100..+100 (positive matches minus negative, scaled)
        confidence   — 0..1 scaling with the number of articles seen
        hard_block   — True if any catastrophic keyword matched
        block_reason — the first matching catastrophic headline
        top          — up to 5 stored for the dashboard
    """
    pos = 0
    neg = 0
    hard_block = False
    block_reason = ""

    for h in headlines:
        text = h.lower()
        if not hard_block:
            for kw in BLOCKING_KEYWORDS:
                if kw in text:
                    hard_block = True
                    block_reason = h
                    break
        for kw in POSITIVE_KEYWORDS:
            if kw in text:
                pos += 1
                break
        for kw in NEGATIVE_KEYWORDS:
            if kw in text:
                neg += 1
                break

    total = max(1, pos + neg)
    # Net ratio, mapped to roughly +/- the matching density
    net = (pos - neg) / total
    matched_fraction = (pos + neg) / max(1, len(headlines))
    score = max(-100.0, min(100.0, net * 100.0 * matched_fraction + net * 30.0))

    # Confidence: more articles + more keyword hits → more trust
    confidence = min(1.0, len(headlines) / 25.0)

    return score, confidence, hard_block, block_reason, headlines[:5]


class CryptoPanicSource(BaseSentimentSource):
    source_id        = "cryptopanic"
    refresh_interval = 300            # 5 min
    optional         = True

    def __init__(self):
        super().__init__()
        self.weight = getattr(settings, "SENTIMENT_WEIGHT_CRYPTOPANIC", 0.25)

    def is_available(self) -> bool:
        # We always have at least the RSS fallback path.
        return True

    async def fetch(self) -> SourceResult:
        api_key = os.getenv("CRYPTOPANIC_API_KEY", "")
        try:
            if api_key:
                headlines = await self._fetch_api(api_key)
                source = "cryptopanic_api"
            else:
                headlines = await self._fetch_rss()
                source = "rss_fallback"

            if not headlines:
                return SourceResult(
                    source_id=self.source_id,
                    score=0.0,
                    confidence=0.0,
                    raw_data={"feed": source, "headlines": []},
                    error="no headlines",
                )

            score, confidence, hard_block, block_reason, top = _score_headlines(headlines)
            return SourceResult(
                source_id=self.source_id,
                score=score,
                confidence=confidence,
                hard_block=hard_block,
                block_reason=block_reason,
                raw_data={
                    "feed":      source,
                    "headlines": top,
                    "article_count": len(headlines),
                },
            )
        except Exception as e:
            logger.warning(f"cryptopanic fetch failed: {e}")
            return SourceResult(
                source_id=self.source_id,
                score=0.0,
                confidence=0.0,
                raw_data={"headlines": []},
                error=str(e),
            )

    async def _fetch_api(self, api_key: str) -> list[str]:
        url = CRYPTOPANIC_ENDPOINT.format(key=api_key)
        timeout = aiohttp.ClientTimeout(total=settings.SENTIMENT_HTTP_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url) as r:
                payload = await r.json(content_type=None)
        results = payload.get("results", []) or []
        return [item.get("title", "") for item in results if item.get("title")]

    async def _fetch_rss(self) -> list[str]:
        """feedparser is sync — wrap each request via aiohttp and parse the bytes."""
        timeout = aiohttp.ClientTimeout(total=settings.SENTIMENT_HTTP_TIMEOUT_SEC)
        headlines: list[str] = []
        async with aiohttp.ClientSession(timeout=timeout) as s:
            for url in RSS_FEEDS:
                try:
                    async with s.get(url) as r:
                        body = await r.read()
                    parsed = feedparser.parse(body)
                    for entry in parsed.entries[:20]:
                        title = entry.get("title", "")
                        if title:
                            headlines.append(title)
                except Exception as e:
                    logger.debug(f"RSS feed {url}: {e}")
        return headlines
