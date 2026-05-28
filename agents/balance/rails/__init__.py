"""
agents/balance/rails/

Transfer rails for the BalanceAgent — see PLUGIN_PATTERN.md.

To add a new rail:
  1. Create agents/balance/rails/my_rail.py
  2. Subclass BaseTransferRail from .base
  3. Set rail_id + display_name class attrs
  4. Implement async execute(transfer) -> TransferResult, is_available()
  5. Append an instance to REGISTERED_RAILS below

The BalanceAgent imports only BaseTransferRail + REGISTERED_RAILS — never
a concrete rail class by name (Plugin Rule 1).

Current set:
  - SimTransferRail  — atomic ledger move; always available in sim mode.
  - CexTransferRail  — full IN_TRANSIT state machine wrapping ccxt.withdraw;
                       structured but gated behind REBALANCE_LIVE_ENABLED.

A future CrossChainTransferRail will consume CrossChainArbAgent.
get_inventory_targets() and route Circle CCTP / canonical bridges; it
plugs in HERE as one extra REGISTERED_RAILS entry — no agent change.
"""

from agents.balance.rails.base import (
    BaseTransferRail, TransferResult,
)
from agents.balance.rails.sim_rail import SimTransferRail
from agents.balance.rails.cex_rail import CexTransferRail


REGISTERED_RAILS: list[BaseTransferRail] = [
    SimTransferRail(),
    CexTransferRail(),
    # CrossChainTransferRail() — future plug-in; will read
    # CrossChainArbAgent.get_inventory_targets() and route over Circle
    # CCTP / canonical bridges with per-bridge caps. Do NOT build now.
]


__all__ = [
    "BaseTransferRail",
    "TransferResult",
    "SimTransferRail",
    "CexTransferRail",
    "REGISTERED_RAILS",
]
