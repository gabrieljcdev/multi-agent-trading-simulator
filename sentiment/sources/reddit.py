"""
sentiment/sources/reddit.py

Subreddit hot-post sentiment via PRAW. PRAW is synchronous, so all
calls are wrapped in run_in_executor to keep the event loop free.

Requires praw installed AND REDDIT_CLIENT_ID set in env. The base
get() flow degrades the source to neutral on any failure.
"""

import asyncio
import logging
import os
from typing import Optional

from config import settings
from sentiment.base import BaseSentimentSource, SourceResult

logger = logging.getLogger(__name__)

# Default subreddits if settings.REDDIT_SUBREDDITS isn't populated.
DEFAULT_SUBREDDITS = ["CryptoCurrency", "Bitcoin", "ethtrader"]

BULLISH_KEYWORDS = [
    "bullish", "moon", "pump", "buy", "long", "accumulate",
    "breakout", "rally", "ath", "adoption", "green",
]
BEARISH_KEYWORDS = [
    "bearish", "crash", "dump", "sell", "short", "capitulate",
    "breakdown", "collapse", "scam", "fear", "red",
]


def _classify(text: str) -> str:
    """Returns 'bull', 'bear', or 'neutral' based on first keyword hit.

    First-hit rather than counting because Reddit titles are short — one
    decisive keyword is more predictive than a tally of weak signals.
    """
    t = text.lower()
    for kw in BULLISH_KEYWORDS:
        if kw in t:
            return "bull"
    for kw in BEARISH_KEYWORDS:
        if kw in t:
            return "bear"
    return "neutral"


class RedditSource(BaseSentimentSource):
    source_id        = "reddit"
    refresh_interval = 600            # 10 min
    optional         = True

    def __init__(self):
        super().__init__()
        self.weight = getattr(settings, "SENTIMENT_WEIGHT_REDDIT", 0.2)

    def is_available(self) -> bool:
        try:
            import praw  # noqa: F401
        except ImportError:
            return False
        return bool(os.getenv("REDDIT_CLIENT_ID"))

    async def fetch(self) -> SourceResult:
        if not self.is_available():
            return SourceResult(
                source_id=self.source_id,
                score=0.0, confidence=0.0,
                raw_data={"reason": "praw missing or REDDIT_CLIENT_ID unset"},
                error="not available",
            )

        try:
            data = await asyncio.get_event_loop().run_in_executor(None, self._scrape_sync)
            return SourceResult(
                source_id=self.source_id,
                score=data["score"],
                confidence=data["confidence"],
                raw_data=data["raw"],
            )
        except Exception as e:
            logger.warning(f"reddit fetch failed: {e}")
            return SourceResult(
                source_id=self.source_id,
                score=0.0, confidence=0.0,
                raw_data={},
                error=str(e),
            )

    # ── Sync core (called inside the executor) ─────────────────────────

    def _scrape_sync(self) -> dict:
        import praw

        reddit = praw.Reddit(
            client_id=os.getenv("REDDIT_CLIENT_ID"),
            client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
            user_agent=os.getenv("REDDIT_USER_AGENT", "cryptobot/1.0"),
            check_for_async=False,
        )

        subs = getattr(settings, "REDDIT_SUBREDDITS", None) or DEFAULT_SUBREDDITS
        bull = bear = 0
        top_bull: list[str] = []
        top_bear: list[str] = []
        post_count = 0

        for name in subs:
            try:
                for post in reddit.subreddit(name).hot(limit=25):
                    title = post.title or ""
                    post_count += 1
                    cls = _classify(title)
                    if cls == "bull":
                        bull += 1
                        if len(top_bull) < 3:
                            top_bull.append(title)
                    elif cls == "bear":
                        bear += 1
                        if len(top_bear) < 3:
                            top_bear.append(title)
            except Exception as e:
                logger.debug(f"reddit /r/{name}: {e}")

        total = max(1, bull + bear)
        bull_ratio = bull / total
        # Centre on 0: 50% bullish → 0, 100% bullish → +100, 0% bullish → -100
        score = (bull_ratio - 0.5) * 200.0
        confidence = min(1.0, post_count / 50.0)

        return {
            "score": score,
            "confidence": confidence,
            "raw": {
                "bull_ratio":        bull_ratio,
                "post_count":        post_count,
                "bull_count":        bull,
                "bear_count":        bear,
                "top_bullish_posts": top_bull,
                "top_bearish_posts": top_bear,
                "subreddits":        subs,
            },
        }
