"""
execution/funding_venues/__init__.py

Registry of funding venues the FundingEngine discovers by default.

═══════════════════════════════════════════════════════════════════════
To add a new funding venue
═══════════════════════════════════════════════════════════════════════

1. Create execution/funding_venues/my_venue.py
2. Subclass BaseFundingVenue (from execution.funding_venues.base_funding).
3. Set the class attrs:
       venue_id     = "my_venue"
       display_name = "My Venue"
       key_env_var  = None            # or an OPTIONAL public-rate-limit key
4. Implement: async fetch_funding(symbol), async list_perps().
   is_available() defaults to "public → always; keyed → key present".
   There is NO order-placement method — this layer is observation-only.
5. Append an instance to REGISTERED_FUNDING_VENUES below.
6. Add its id to settings.FUNDING_VENUES_ENABLED to switch it on.

The FundingEngine + FundingArbAgent pick it up automatically — no engine
edit, no agent edit (Plugin Pattern Rule 1: the orchestrator never imports
concrete plugins by name). Mirrors execution/chains/__init__.py.
"""

from __future__ import annotations

from execution.funding_venues.base_funding import (
    BaseFundingVenue, FundingQuote, annualise_funding,
)
from execution.funding_venues.binance import BinanceFundingVenue
from execution.funding_venues.hyperliquid import HyperliquidFundingVenue


REGISTERED_FUNDING_VENUES: list[BaseFundingVenue] = [
    BinanceFundingVenue(),
    HyperliquidFundingVenue(),
    # Add new funding venues here (instances).
]


__all__ = [
    "REGISTERED_FUNDING_VENUES",
    "BaseFundingVenue",
    "FundingQuote",
    "annualise_funding",
    "BinanceFundingVenue",
    "HyperliquidFundingVenue",
]
