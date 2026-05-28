"""
agents/balance/policy/

Policy plugins for the BalanceAgent — see PLUGIN_PATTERN.md.

To add a new policy:
  1. Create agents/balance/policy/my_policy.py
  2. Subclass BasePolicy from .base
  3. Set policy_id + display_name class attrs
  4. Implement compute_targets(inv, equity) -> list[InventoryTarget]
  5. Append an instance to REGISTERED_POLICIES below

The BalanceAgent imports only BasePolicy + REGISTERED_POLICIES — never a
concrete policy by name (Plugin Rule 1). At runtime it iterates the list
and consults each policy's is_available() before calling compute_targets.
"""

from agents.balance.policy.base import (
    BasePolicy, InventoryTarget,
)
from agents.balance.policy.growth_optimal import GrowthOptimalPolicy


REGISTERED_POLICIES: list[BasePolicy] = [
    GrowthOptimalPolicy(),
    # Add new policies here (instances). The BalanceAgent uses the first
    # available policy each scan; later policies are reserved for A/B sims.
]


__all__ = [
    "BasePolicy",
    "InventoryTarget",
    "GrowthOptimalPolicy",
    "REGISTERED_POLICIES",
]
