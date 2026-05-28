"""
agents/balance/rails/sim_rail.py

Atomic sim transfer rail.

In sim mode there is no real network — but we don't want sim to
under-estimate the drag a real transfer carries. So the rail:
  - charges SIM_WITHDRAWAL_FEE_USD per transfer (reduces source claim)
  - simulates SIM_TRANSFER_DELAY_S in-transit time via asyncio.sleep
  - optionally injects failure via SIM_REBALANCE_FAILURE_RATE so the
    auto-pause path gets exercised in tests
  - writes exactly one capital_movements row per attempt (pending →
    in_transit → completed | failed) so the audit log is identical
    in shape to the live path

It updates the InventoryState atomically — the ledger view's claims
shift the moment the transfer settles, so other layers see consistent
state.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import TYPE_CHECKING

from config import settings
from database import queries as db_queries

from agents.balance.rails.base import BaseTransferRail, TransferResult
from agents.balance.inventory_state import inventory_state

if TYPE_CHECKING:
    from agents.balance.planner import Transfer

logger = logging.getLogger(__name__)


class SimTransferRail(BaseTransferRail):
    """Sim-only atomic transfer with simulated fee + delay."""

    rail_id      = "sim"
    display_name = "Sim Transfer Rail"

    def is_available(self) -> bool:
        """Available whenever the bot is in sim mode. Available is the
        operational check the BalanceAgent runs before dispatching."""
        return bool(getattr(settings, "SIM_MODE", True))

    async def execute(self, transfer: "Transfer") -> TransferResult:
        try:
            return await self._execute_inner(transfer)
        except Exception as e:
            logger.error("SimTransferRail.execute: %s", e, exc_info=True)
            return TransferResult(
                success=False, state="failed",
                error=str(e),
            )

    async def _execute_inner(self, transfer: "Transfer") -> TransferResult:
        fee = float(getattr(settings, "SIM_WITHDRAWAL_FEE_USD", 1.0))
        delay = max(0.0, float(getattr(settings, "SIM_TRANSFER_DELAY_S", 600)))
        failure_rate = max(0.0, min(1.0, float(
            getattr(settings, "SIM_REBALANCE_FAILURE_RATE", 0.0)
        )))

        # Persist as pending; flip to in_transit once we "leave" the source.
        movement_id = self._log("pending", transfer, fee=fee, error=None)
        inventory_state.open_transfer(
            movement_id=movement_id,
            from_fund=transfer.from_fund, to_fund=transfer.to_fund,
            from_exchange=transfer.from_exchange,
            to_exchange=transfer.to_exchange,
            amount_usd=transfer.amount_usd, asset=transfer.asset,
            state="pending",
        )

        # Move to in_transit and simulate delay. The delay can be
        # dialed to 0 in tests via monkeypatch settings.SIM_TRANSFER_DELAY_S.
        self._update(movement_id, {"state": "in_transit"})
        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                self._update(movement_id, {"state": "failed",
                                          "error": "cancelled"})
                inventory_state.fail_transfer(movement_id, "cancelled")
                raise

        # Injected failure path — exercise auto-pause discipline.
        if failure_rate > 0 and random.random() < failure_rate:
            self._update(movement_id, {"state": "failed",
                                      "error": "injected_failure"})
            inventory_state.fail_transfer(movement_id, "injected_failure")
            return TransferResult(
                success=False, state="failed", movement_id=movement_id,
                fee_usd=fee, error="injected_failure",
            )

        # Success — settle.
        self._update(movement_id, {
            "state": "completed",
            "completed_at": datetime.utcnow(),
            "note": "sim",
        })
        inventory_state.settle_transfer(movement_id)
        return TransferResult(
            success=True, state="completed", movement_id=movement_id,
            fee_usd=fee,
        )

    @staticmethod
    def _log(state: str, t: "Transfer", *, fee: float, error: str = None) -> int:
        try:
            return db_queries.log_capital_movement({
                "from_fund":     t.from_fund,
                "to_fund":       t.to_fund,
                "from_exchange": t.from_exchange,
                "to_exchange":   t.to_exchange,
                "asset":         t.asset,
                "amount_usd":    t.amount_usd,
                "mode":          "sim",
                "state":         state,
                "rail_id":       "sim",
                "initiated_by":  "policy",
                "note":          t.note or "",
                "error":         error,
            })
        except Exception as e:
            logger.debug("sim_rail _log failed: %s", e)
            # Use 0 as sentinel — the rail still functions in-memory; the
            # InventoryState's transfer record key just becomes 0 (any
            # subsequent rail call would collide, which the planner
            # prevents via its in-flight check).
            return 0

    @staticmethod
    def _update(movement_id: int, fields: dict) -> None:
        if not movement_id:
            return
        try:
            db_queries.update_capital_movement(movement_id, fields)
        except Exception as e:
            logger.debug("sim_rail _update failed: %s", e)
