"""
sentiment/sources/__init__.py

Registry of sentiment sources the aggregator polls by default.

═══════════════════════════════════════════════════════════════════════
To add a new sentiment source
═══════════════════════════════════════════════════════════════════════

1. Create sentiment/sources/my_source.py
2. Subclass BaseSentimentSource (from sentiment.base)
3. Set the four class attrs:
       source_id        = "my_source"
       refresh_interval = 600         # seconds
       optional         = True        # False if a hard dependency
4. In __init__, pull your weight from settings:
       self.weight = getattr(settings, "SENTIMENT_WEIGHT_MY_SOURCE", 0.1)
5. Implement:
       async def fetch(self) -> SourceResult: ...
       def is_available(self) -> bool: ...   # only if needs deps/keys
6. Append your class to REGISTERED_SOURCES below
7. Add SENTIMENT_WEIGHT_MY_SOURCE to config/settings.py

The aggregator does not need to change. It instantiates every entry in
REGISTERED_SOURCES, skips any whose is_available() returns False, and
folds the rest into the composite weighted by weight × confidence.

Sources never have to manage caching, retries, or error logging —
BaseSentimentSource.get() handles all of that. fetch() should just
return a SourceResult, with the .error field set if anything broke.
"""

from sentiment.sources.fear_greed    import FearGreedSource
from sentiment.sources.cryptopanic   import CryptoPanicSource
from sentiment.sources.reddit        import RedditSource
from sentiment.sources.google_trends import GoogleTrendsSource
from sentiment.sources.telegram      import TelegramSource


# Ordered registry — the aggregator instantiates each class in order.
REGISTERED_SOURCES: list[type] = [
    FearGreedSource,
    CryptoPanicSource,
    RedditSource,
    GoogleTrendsSource,
    TelegramSource,
]
