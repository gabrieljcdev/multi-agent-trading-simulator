"""
agents/opportunities/detectors/base.py

The two contracts every opportunity detector must satisfy:

  OpportunityCandidate — what a detector emits (pre-gate, pre-rank)
  BaseDetector         — what a detector class looks like

Sibling of data_sources/base.py:BaseDataSource — same plugin shape
(PLUGIN_PATTERN.md): class attrs for identity + cadence, one abstract
async method, is_available() consulted before scan, graceful
degradation (Rule 4: scan() returns [] on failure, NEVER raises to the
agent). Register instances in detectors/__init__.py:REGISTERED_DETECTORS.

Detection latency is the real product (§1.3): first_seen is the single
most valuable field a detector produces. Stamp it the instant the
candidate appears; it is IMMUTABLE downstream — re-detection never
overwrites it.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class OpportunityCandidate:
    """What a detector emits — the pre-gate, pre-rank record.

    feature_vector is the as-of-first_seen snapshot for the hypothesis
    log. NO look-ahead: it must contain only data available at or before
    first_seen — never backfill a feature with a later value (the
    load-bearing requirement for later DSR / CPCV).
    """
    opp_type: str            # "liquidation" | "funding" | "lp" | "launch"
    protocol: str
    chain: str
    market_key: str          # the per-type natural key (e.g. lending market address)
    detector_id: str         # which detector found it
    first_seen: datetime     # UTC, immutable — set the instant the candidate appears
    asset_class: str | None = None   # shared taxonomy (MARKET_VIEW §1)
    raw_detail: dict = field(default_factory=dict)      # type-specific → typed detail table
    feature_vector: dict = field(default_factory=dict)  # decision-time snapshot (no look-ahead)
    # Gap between the on-chain event timestamp and first_seen — the metric
    # the detection layer is graded on (§1.3). None when the event time
    # is unknown.
    detection_latency_ms: float | None = None


class BaseDetector(ABC):
    """Subclass to add a new spawning-ground watcher.

    Override the class attrs, implement scan(), append an instance to
    REGISTERED_DETECTORS in detectors/__init__.py. The agent imports only
    BaseDetector + the registration list — never a concrete detector by
    name; detectors never import each other. Adding a spawning ground
    later touches exactly one file + one registration line.
    """

    # Override in subclasses
    detector_id:      str  = "base"
    display_name:     str  = "Base Detector"
    opp_type:         str  = "liquidation"
    refresh_interval: int  = 60          # seconds — from settings, never hardcoded
    optional:         bool = True

    @abstractmethod
    async def scan(self) -> list[OpportunityCandidate]:
        """Watch the spawning ground; emit candidates.

        MUST NOT raise to the agent — return [] with an internal log on
        failure (graceful degradation, PLUGIN_PATTERN Rule 4).
        """
        ...

    def is_available(self) -> bool:
        """True if this detector can run right now (deps, endpoints,
        keys). The agent skips unavailable detectors silently."""
        return True


__all__ = ["OpportunityCandidate", "BaseDetector"]
