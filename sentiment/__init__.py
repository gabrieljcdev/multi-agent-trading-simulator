"""
sentiment/ — pluggable sentiment aggregator.

Public surface:
    SentimentAggregator   — the orchestrator class
    SentimentData         — the aggregated reading dataclass
    sentiment             — module-level singleton (CLAUDE.md pattern)
    BaseSentimentSource   — subclass to add a new source
    SourceResult          — what a source's fetch() returns

See sentiment/sources/__init__.py for the docstring on adding a new source.
"""

from sentiment.aggregator import (
    SentimentAggregator,
    SentimentData,
    sentiment,
)
from sentiment.base import BaseSentimentSource, SourceResult

__all__ = [
    "SentimentAggregator",
    "SentimentData",
    "sentiment",
    "BaseSentimentSource",
    "SourceResult",
]
