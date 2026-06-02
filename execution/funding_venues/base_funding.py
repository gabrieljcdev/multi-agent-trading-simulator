"""
execution/funding_venues/base_funding.py

The two contracts every funding venue must satisfy:

  FundingQuote      — the per-(venue, symbol) funding snapshot the engine consumes
  BaseFundingVenue  — ABC + is_available() default

Mirrors the chain-connector plugin layer (execution/chains/base_connector.py)
one venue class deeper: where the cross-chain agent has a sub-registry of
chain connectors, the funding observer now has its own sub-registry of
funding venues. The engine imports ONLY BaseFundingVenue +
REGISTERED_FUNDING_VENUES, never a concrete venue by name, so adding a
third venue (dYdX, Aevo, …) is one file + one line in __init__.py.

OBSERVATION-MODE invariant: there is NO order-placement method anywhere on
this ABC or its subclasses. Funding venues are READ-ONLY — funding rate,
mark/index price, and open interest off public endpoints. Live execution
(signing, routing, hedging) is a separate, later, gated build that does NOT
live here. fetch_funding / list_perps are the only network surface.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


# Seconds in a 365-day year — the annualisation base. A venue's funding
# rate is per funding interval (Binance 8h, Hyperliquid 1h); annualising
# through funding_interval_sec is what lets the engine difference two
# venues' funding on the same clock (see annualise_funding).
SECONDS_PER_YEAR = 365 * 24 * 3600          # 31_536_000


def annualise_funding(rate_per_interval: float, interval_sec: float) -> float:
    """Annualise a per-interval funding rate.

    funding_apr = rate * (SECONDS_PER_YEAR / interval_sec). For Binance's
    8h interval (28_800s) this is exactly rate * 1095 — the constant Phase
    1 pinned and the existing tests assert on. For Hyperliquid's 1h
    interval (3_600s) it is rate * 8760. Returns 0.0 for a non-positive
    interval rather than dividing by zero.
    """
    if interval_sec <= 0:
        return 0.0
    return rate_per_interval * (SECONDS_PER_YEAR / interval_sec)


@dataclass
class FundingQuote:
    """One venue's funding snapshot for one symbol.

    funding_apr is ALREADY annualised (via annualise_funding) so the engine
    can difference two venues directly without re-normalising — the raw
    per-interval rate and its interval live in funding_interval_sec for
    auditability. oi_usd is the perp's open interest in USD (0.0 when the
    venue doesn't expose it — the engine's depth gate treats 0 as "no info,
    pass"). depth_ok is the venue's own "I returned usable data" flag; the
    engine applies its OI-fraction policy on top of it.

    taker_fee_bps / maker_fee_bps are the venue's perp trading fees in bps,
    used by the cross-venue break-even math (Phase 2). error is non-None
    when the fetch failed or the symbol isn't listed on this venue — the
    engine skips a quote with error set and never crashes the scan.
    """
    venue:               str
    symbol:              str
    funding_apr:         Optional[float]      # annualised; None on failure
    funding_interval_sec: float
    mark_price:          float = 0.0
    index_price:         float = 0.0
    oi_usd:              float = 0.0
    depth_ok:            bool  = True
    taker_fee_bps:       float = 0.0
    maker_fee_bps:       float = 0.0
    ts:                  float = 0.0
    error:               Optional[str] = None


class BaseFundingVenue(ABC):
    """Subclass to add a funding venue to the observer.

    Override the class attrs, implement fetch_funding + list_perps, and
    append an instance to REGISTERED_FUNDING_VENUES in
    execution/funding_venues/__init__.py. The engine picks it up
    automatically — no engine-side changes (Plugin Pattern Rule 1: the
    orchestrator never imports concrete plugins by name).

    NO order placement is declared here. This layer is observation-only by
    construction — there is nothing to gate because there is no write path.
    """

    # Override in subclasses
    venue_id:     str = "base"
    display_name: str = "Base Funding Venue"
    key_env_var:  Optional[str] = None      # optional rate-limit key, never a trading key
    optional:     bool = True

    # ── Abstract ────────────────────────────────────────────────────────

    @abstractmethod
    async def fetch_funding(self, symbol: str) -> FundingQuote:
        """Snapshot funding + mark/index + OI for `symbol` on this venue.

        Must NEVER raise — on network failure, rate-limit, or a symbol not
        listed here, return a FundingQuote with error=<reason> and
        funding_apr=None so the engine can skip it and move on.
        """
        ...

    @abstractmethod
    async def list_perps(self) -> list[str]:
        """The venue's perp universe (unified symbols), for Phase 3 discovery.

        Must NEVER raise — return [] on any failure. Reads are public and
        may be cached with a short TTL to respect venue rate limits.
        """
        ...

    # ── Default behaviour — override only if your venue needs more ───────

    def is_available(self) -> bool:
        """True iff the optional rate-limit key is present (when one is
        declared at all).

        Funding reads are public, so a venue with key_env_var=None is
        always available. A venue that declares an optional key is treated
        as available only when that key is set — graceful degradation: an
        unset endpoint reports unavailable and is skipped, never crashes.
        Subclasses that need a backend present (ccxt) override and AND this
        in (mirrors ArbitrumConnector.is_available checking web3).
        """
        if self.key_env_var is None:
            return True
        return bool(os.getenv(self.key_env_var))

    async def close(self) -> None:
        """Release any held client. Default no-op; ccxt-backed venues override."""
        return None


__all__ = [
    "FundingQuote",
    "BaseFundingVenue",
    "annualise_funding",
    "SECONDS_PER_YEAR",
]
