"""
agents/balance/rails/base.py

BaseTransferRail contract + TransferResult.

Rails never raise to the agent — every error path returns a
TransferResult with `success=False` and `error` set. The BalanceAgent
treats a None return or an exception escape as a bug; this contract
lets rails fail loudly in their own log without breaking the planner
loop or sibling rails.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agents.balance.planner import Transfer


@dataclass
class TransferResult:
    """Rail return shape. movement_id is the capital_movements row id
    if the rail persisted one (every rail SHOULD); None otherwise (a
    rail that elected to do nothing). state mirrors the persisted row's
    final state for this attempt: completed | failed | in_transit
    (live ccxt; reconciliation happens later)."""
    success:     bool
    state:       str            # completed | failed | in_transit
    movement_id: Optional[int]  = None
    fee_usd:     float          = 0.0
    network:     Optional[str]  = None
    tx_hash:     Optional[str]  = None
    error:       Optional[str]  = None


class BaseTransferRail(ABC):
    """Subclass + register in REGISTERED_RAILS. The BalanceAgent picks
    the FIRST rail that returns True from is_available() and that the
    Transfer is compatible with (e.g. CexTransferRail requires
    ccxt-known venues; CrossChainTransferRail requires chain ids)."""

    # Override in subclasses
    rail_id:      str = "base"
    display_name: str = "Base Rail"

    @abstractmethod
    def is_available(self) -> bool:
        """True iff this rail can be used in the current mode/state.
        SimTransferRail always returns True; CexTransferRail returns
        True only when REBALANCE_LIVE_ENABLED + a populated
        WITHDRAWAL_ROUTES map are present."""
        ...

    @abstractmethod
    async def execute(self, transfer: "Transfer") -> TransferResult:
        """Execute a single transfer. NEVER raises — wraps errors in
        TransferResult(success=False, error=...). Implementations are
        responsible for persisting the capital_movements row, updating
        InventoryState, and handling any cancellation / rollback."""
        ...
