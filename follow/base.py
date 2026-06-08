"""
follow/base.py

The two contracts every follow/ streaming source must satisfy:

  ActorEvent             — what a source emits (the shared follow schema)
  BaseStreamingDataSource — what a streaming source class looks like

Streaming sibling of data_sources/base.py:BaseDataSource and
agents/opportunities/detectors/base.py:BaseDetector — same plugin shape
(PLUGIN_PATTERN.md): class attrs for identity, is_available() consulted
before start(), graceful degradation (never raise to the host agent).
The difference is lifecycle: a streaming source holds a live connection,
so it implements start()/stop() rather than a fetch_all()/scan() poll.

This module is the home of the SHARED follow schema (ActorEvent) and the
shared action / provenance vocabulary. A sibling follow source (e.g. the
meme-wallet scorer) REUSES ActorEvent rather than defining its own.

OBSERVER discipline: there is no submit / execute / trade method anywhere
in this contract. A streaming follow source watches and logs; the absence
of an execution path is a hard requirement, not an oversight.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Action vocabulary (the `action` field of an ActorEvent) ─────────────────
ACTION_SWAP         = "swap"
ACTION_TRANSFER_IN  = "transfer_in"    # transfer TO a labelled exchange (pre-sell tell)
ACTION_TRANSFER_OUT = "transfer_out"   # withdrawal FROM an exchange to a wallet (accumulation tell)
ACTION_LP_ADD       = "lp_add"
ACTION_LP_REMOVE    = "lp_remove"

ACTIONS = (
    ACTION_SWAP, ACTION_TRANSFER_IN, ACTION_TRANSFER_OUT,
    ACTION_LP_ADD, ACTION_LP_REMOVE,
)


@dataclass
class ActorEvent:
    """What a follow source emits — the shared, source-agnostic record.

    Two clocks (load-bearing for the point-in-time scorer): occurred_at is
    on-chain block time; detected_at is when WE observed it (T + delta). The
    gap between them is the latency-δ the scorer grades a wallet's
    followability on. meta carries event-type-specific extras (exchange
    label, counterparty, token mint).
    """
    source_id:   str                 # "wallet_flow"
    actor_id:    str                 # wallet address
    action:      str                 # one of ACTIONS
    asset:       str | None = None
    venue:       str | None = None   # exchange/label name when known
    size_usd:    float | None = None
    detected_at: float = 0.0         # epoch seconds — when WE observed it (T + delta)
    occurred_at: float = 0.0         # epoch seconds — on-chain block time
    meta:        dict = field(default_factory=dict)


class BaseStreamingDataSource(ABC):
    """Subclass to add a live-connection follow source.

    Override the class attrs, implement start()/stop()/get_stats(), and
    append an instance to follow/__init__.py:REGISTERED_FOLLOW_SOURCES. The
    host FollowAgent imports only this base + the registration list, never a
    concrete source by name (PLUGIN_PATTERN.md). is_available() is consulted
    before start(); an unavailable source is skipped silently, not by raising.

    Unlike BaseDataSource (poll: fetch_all) and BaseDetector (poll: scan),
    this is a STREAMING contract — the source opens a connection in start()
    and tears it down in stop(). There is deliberately NO submit/execute
    method: a follow source is an observer.
    """

    # Override in subclasses
    source_id:    str  = "base_stream"
    display_name: str  = "Base Streaming Source"
    optional:     bool = True

    @abstractmethod
    async def start(self) -> None:
        """Open the live stream and begin observing. MUST NOT raise to the
        host agent on a connection failure — log and degrade (the agent
        keeps running; an unavailable stream simply produces no events)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Clean teardown of the live stream. Idempotent; never raises."""
        ...

    @abstractmethod
    def get_stats(self) -> dict:
        """Snapshot of the source's current state for the dashboard. MUST
        NEVER raise — return a dict (zeroed/empty on any internal failure)."""
        ...

    def is_available(self) -> bool:
        """True if this source can run right now (key present, label set
        loaded, deps importable). The host agent skips it silently if not."""
        return True


__all__ = [
    "ActorEvent",
    "BaseStreamingDataSource",
    "ACTIONS",
    "ACTION_SWAP", "ACTION_TRANSFER_IN", "ACTION_TRANSFER_OUT",
    "ACTION_LP_ADD", "ACTION_LP_REMOVE",
]
