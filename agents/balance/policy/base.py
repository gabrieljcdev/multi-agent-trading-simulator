"""
agents/balance/policy/base.py

BasePolicy contract + InventoryTarget — the universal interface every
inventory producer (growth-optimal policy, cross-chain agent, future
strategies) emits and every consumer (planner, dashboard, rails)
reads.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from agents.balance.inventory_state import InventoryState


@dataclass
class InventoryTarget:
    """One (fund, exchange, asset) cell's growth-optimal target.

    Same shape the cross-chain agent emits via
    ``execution.inventory.InventoryTarget`` (with chain→exchange naming).
    Universal so the planner doesn't need to know whether a target came
    from the policy layer or a separate strategy.

    target_usd / floor_usd / cap_usd are USD-denominated. drift_pct is
    (current − target) / target — signed, so a negative value means
    the node is under-stocked. needs_rebalance is a producer-side hint;
    the planner re-runs the band check itself before transferring.
    """
    fund:            str
    exchange:        str
    asset:           str            # "USDT" or alt symbol
    target_usd:      float          # growth-optimal target value at this node
    floor_usd:       float          # hard minimum (open-position notional + safety)
    cap_usd:         float          # capacity ceiling (counterparty cap for MEXC)
    drift_pct:       float          # (current − target) / target, signed
    needs_rebalance: bool           # producer-side hint; planner re-verifies


class BasePolicy(ABC):
    """Policy plugin contract.

    Subclass + register in REGISTERED_POLICIES. The BalanceAgent calls
    compute_targets() once per scan with the live InventoryState and
    the portfolio's deployable equity (pool); returns a list of
    InventoryTarget rows covering every cell the policy wants the
    planner to consider.
    """

    # Override in subclasses
    policy_id:    str = "base"
    display_name: str = "Base Policy"

    def is_available(self) -> bool:
        """Return True when the policy can compute meaningful targets.
        Default: always True. Override if the policy depends on
        per-fund edge data that might be cold-started."""
        return True

    @abstractmethod
    def compute_targets(
        self,
        inv: "InventoryState",
        equity: float,
    ) -> list[InventoryTarget]:
        """Return target inventory split across every relevant
        (fund, exchange, asset) cell. Must NEVER raise; on error the
        BalanceAgent will skip this policy for the cycle.
        """
        ...
