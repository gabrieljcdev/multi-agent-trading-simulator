"""
sentiment/sources/telegram.py

Telegram channel sentiment — STUB.

TODO: implement via Telethon. The Telethon client is event-driven, so
this source should subscribe to TELEGRAM_CHANNELS at start-up and push
incoming messages into a rolling buffer. fetch() would then score the
buffer's recent contents rather than poll. For now the source declares
itself unavailable and returns a neutral result.

Implementation notes for future-you:
  - Telethon needs TELEGRAM_API_ID + TELEGRAM_API_HASH in env
  - Session string also needs persisting (Telethon stores it locally)
  - Use a background asyncio task started by the aggregator, not by fetch()
  - Same keyword scorer as reddit.py would work for first pass
"""

from config import settings
from sentiment.base import BaseSentimentSource, SourceResult


class TelegramSource(BaseSentimentSource):
    source_id        = "telegram"
    refresh_interval = 0              # event-driven once wired
    optional         = True

    def __init__(self):
        super().__init__()
        self.weight = getattr(settings, "SENTIMENT_WEIGHT_TELEGRAM", 0.05)

    def is_available(self) -> bool:
        return False  # stub

    async def fetch(self) -> SourceResult:
        return SourceResult(
            source_id=self.source_id,
            score=0.0,
            confidence=0.0,
            raw_data={"status": "stub"},
            error="Telegram source not yet implemented",
        )
