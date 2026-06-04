"""
agents/opportunities/detectors/

Registry of opportunity detectors the OpportunityScannerAgent runs.

═══════════════════════════════════════════════════════════════════════
To add a new detector (one file + one line — zero core-logic changes)
═══════════════════════════════════════════════════════════════════════

1. Create agents/opportunities/detectors/my_detector.py
2. Subclass BaseDetector (from .base)
3. Set the class attrs:
       detector_id      = "my_detector"
       display_name     = "My Detector"
       opp_type         = "liquidation" | "funding" | "lp" | "launch"
       refresh_interval = settings.OPPORTUNITY_MY_DETECTOR_REFRESH_SEC
       optional         = True
4. Implement: async scan() -> list[OpportunityCandidate]
       (return [] on failure — NEVER raise to the agent)
   and is_available() if it needs deps/keys
5. Append an instance of your class to REGISTERED_DETECTORS below
6. Add your refresh/threshold settings to config/settings.py

The agent picks it up automatically — it imports only BaseDetector +
this list, never a concrete detector by name (PLUGIN_PATTERN.md).
"""

from __future__ import annotations

from agents.opportunities.detectors.base import BaseDetector, OpportunityCandidate
from agents.opportunities.detectors.createmarket_detector import CreateMarketDetector


REGISTERED_DETECTORS: list[BaseDetector] = [
    CreateMarketDetector(),
    # NewPerpDetector(),   — stub: new perp listings (opp_type="funding");
    #                        consumes binance_futures / bybit_derivs feeds,
    #                        NOT a new HTTP integration.
    # NewChainDetector(),  — stub: new chain / sequencer launches
    #                        (opp_type="launch").
    # NewPoolDetector(),   — stub: new AMM pools (opp_type="lp"); NOT
    #                        implemented — noise floor is an unresolved §6
    #                        open question (OPPORTUNITY_LAUNCH_MIN_LIQUIDITY_USD
    #                        stays commented in settings until designed).
]

__all__ = ["REGISTERED_DETECTORS", "BaseDetector", "OpportunityCandidate"]
