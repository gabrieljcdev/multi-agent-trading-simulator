"""
agents/balance/

The four strictly-separated layers of the BalanceAgent:

    policy   → produces InventoryTarget per (fund, exchange)
    planner  → turns targets into minimum-cost transfers
    rails    → executes transfers (sim, cex; cross-chain seam)
    state    → InventoryState singleton (the shared ledger view)

Importing this package alone exposes the singleton and the dataclasses;
concrete policies / rails are picked up via REGISTERED_POLICIES /
REGISTERED_RAILS in their respective sub-packages (plugin pattern —
see PLUGIN_PATTERN.md).
"""

from agents.balance.inventory_state import InventoryState, inventory_state

__all__ = ["InventoryState", "inventory_state"]
