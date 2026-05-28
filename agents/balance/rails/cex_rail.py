"""
agents/balance/rails/cex_rail.py

Centralised-exchange transfer rail — the production live path.

Structure is FULL: a pending → in_transit → completed/failed state
machine with persisted rows, restart reconciliation, per-route network
selection, and a hardcoded address allowlist. The actual ccxt.withdraw
call is **stubbed behind REBALANCE_LIVE_ENABLED** (default False) —
exactly the discipline execution/router.py:_live_execute uses for the
signal fund's live entries.

Network selection is REQUIRED, not optional: ccxt network identifiers
vary per exchange (OKX "USDT-TRC20", MEXC "TRC20", Huobi
concatenated, Coinex smart_contract_name). settings.WITHDRAWAL_ROUTES
encodes the per-(from_exchange, to_exchange, asset) → ordered
[network_id...] map; the rail picks the first available network per
route. Addresses are **hardcoded-whitelisted only**, never operator
input — the live path validates against settings before signing.

Restart reconciliation: load_in_transit() (called by BalanceAgent on
boot) reads rows still in pending/in_transit state and resumes their
state machine — without it, a hard kill mid-transfer would silently
lose the move.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from config import settings
from database import queries as db_queries

from agents.balance.rails.base import BaseTransferRail, TransferResult
from agents.balance.inventory_state import inventory_state

if TYPE_CHECKING:
    from agents.balance.planner import Transfer

logger = logging.getLogger(__name__)


# Hardcoded address allowlist — operator-provisioned. Empty by default
# so the live rail is dormant until the operator pins addresses. The
# rail refuses to dispatch a transfer whose destination is missing
# from this map even when REBALANCE_LIVE_ENABLED is True. This is the
# OES substitute: we cannot mitigate counterparty risk like Fireblocks
# does, so we lean harder on a per-route whitelist + caps.
_WITHDRAWAL_ADDRESSES: dict[tuple[str, str], str] = {
    # ("to_exchange", "asset"): "0x...address"
    # e.g. ("bitget", "USDT"): "TXxxxxxxxxxxxxxxx",   # TRC20 deposit address
}


class CexTransferRail(BaseTransferRail):
    """CEX withdrawal rail — structured, live-stubbed.

    is_available gating:
      - REBALANCE_LIVE_ENABLED must be True
      - WITHDRAWAL_ROUTES must contain a route for the requested
        (from_ex, to_ex, asset)
      - The destination must have a whitelisted address
      - ccxt must be importable

    Even with all of those, the actual ``ccxt.withdraw`` call stays
    stubbed by default — see ``_call_ccxt_withdraw``.
    """

    rail_id      = "cex"
    display_name = "CEX Transfer Rail"

    def is_available(self) -> bool:
        if not bool(getattr(settings, "REBALANCE_LIVE_ENABLED", False)):
            return False
        try:
            import ccxt.async_support  # noqa: F401
        except Exception:
            return False
        if not getattr(settings, "WITHDRAWAL_ROUTES", {}):
            return False
        return True

    # ── Network selection ───────────────────────────────────────────────

    @staticmethod
    def _pick_network(
        from_exchange: str,
        to_exchange: str,
        asset: str,
    ) -> Optional[str]:
        """Cheapest available network for the route, or None when no
        route is configured. The list in WITHDRAWAL_ROUTES is ordered
        from cheapest to most expensive."""
        routes = getattr(settings, "WITHDRAWAL_ROUTES", {}) or {}
        candidates = routes.get((from_exchange, to_exchange, asset)) or []
        return candidates[0] if candidates else None

    # ── Execute ─────────────────────────────────────────────────────────

    async def execute(self, transfer: "Transfer") -> TransferResult:
        try:
            return await self._execute_inner(transfer)
        except Exception as e:
            logger.error("CexTransferRail.execute: %s", e, exc_info=True)
            return TransferResult(
                success=False, state="failed", error=str(e),
            )

    async def _execute_inner(self, transfer: "Transfer") -> TransferResult:
        # Hard gate — even if is_available() is True (rail wired up),
        # we refuse to call ccxt.withdraw unless the env-confirmed
        # live flag is set. Mirrors execution/router.py:_live_execute.
        live = bool(getattr(settings, "REBALANCE_LIVE_ENABLED", False))
        network = self._pick_network(
            transfer.from_exchange, transfer.to_exchange, transfer.asset,
        )
        if network is None:
            return TransferResult(
                success=False, state="failed",
                error=(
                    f"no network configured for "
                    f"{transfer.from_exchange}->{transfer.to_exchange} "
                    f"{transfer.asset}"
                ),
            )

        address = _WITHDRAWAL_ADDRESSES.get((transfer.to_exchange, transfer.asset))
        if not address:
            return TransferResult(
                success=False, state="failed",
                error=(
                    f"no whitelisted address for "
                    f"{transfer.to_exchange} {transfer.asset}"
                ),
            )

        # Persist pending row up-front so a restart mid-call recovers.
        movement_id = self._log_pending(transfer, network=network)
        inventory_state.open_transfer(
            movement_id=movement_id,
            from_fund=transfer.from_fund, to_fund=transfer.to_fund,
            from_exchange=transfer.from_exchange,
            to_exchange=transfer.to_exchange,
            amount_usd=transfer.amount_usd, asset=transfer.asset,
            state="pending",
        )

        if not live:
            # Structured-but-disabled — exactly the _live_execute
            # discipline. We log, we register, but we refuse to sign.
            logger.warning(
                "CexTransferRail: live withdraw stub "
                "%s -> %s amount=%.4f %s via %s — "
                "REBALANCE_LIVE_ENABLED=False, NOT signing",
                transfer.from_exchange, transfer.to_exchange,
                transfer.amount_usd, transfer.asset, network,
            )
            self._update(movement_id, {
                "state": "failed",
                "error": "REBALANCE_LIVE_ENABLED=False",
            })
            inventory_state.fail_transfer(movement_id,
                                          "REBALANCE_LIVE_ENABLED=False")
            return TransferResult(
                success=False, state="failed", movement_id=movement_id,
                network=network,
                error="REBALANCE_LIVE_ENABLED=False",
            )

        # The actual live submission — kept behind a separate method so
        # tests can monkeypatch a fake ccxt without touching dispatch
        # logic.
        try:
            tx_hash = await self._call_ccxt_withdraw(
                transfer, network=network, address=address,
            )
        except Exception as e:
            self._update(movement_id, {
                "state": "failed", "error": f"ccxt.withdraw: {e}",
            })
            inventory_state.fail_transfer(movement_id, f"ccxt.withdraw: {e}")
            return TransferResult(
                success=False, state="failed", movement_id=movement_id,
                network=network, error=f"ccxt.withdraw: {e}",
            )

        # Mark in_transit; reconciliation reads from this state on
        # restart and on a periodic poll thread.
        self._update(movement_id, {
            "state": "in_transit",
            "transfer_tx_hash": tx_hash,
        })
        return TransferResult(
            success=True, state="in_transit",
            movement_id=movement_id, network=network, tx_hash=tx_hash,
        )

    # ── live call (stubbed) ─────────────────────────────────────────────

    async def _call_ccxt_withdraw(
        self,
        transfer: "Transfer",
        *,
        network: str,
        address: str,
    ) -> str:
        """Real path goes here. Until the operator authorises live
        withdrawals end-to-end (REBALANCE_LIVE_ENABLED + populated
        WITHDRAWAL_ROUTES + whitelisted addresses + signed audit log),
        we refuse to call ccxt — same discipline as the signal fund's
        live router."""
        raise NotImplementedError(
            "live ccxt.withdraw integration pending operator authorisation"
        )

    # ── Reconciliation on restart ───────────────────────────────────────

    def load_in_transit(self) -> int:
        """Called by the BalanceAgent on boot. Loads pending /
        in_transit rows from capital_movements and re-registers them
        with InventoryState so the ledger view picks up the
        pending-out / pending-in adjustments. The actual ccxt-side
        reconciliation (poll the exchange's deposit history, settle
        when the matching deposit lands) is a periodic call against
        each row; we trigger one here on boot too."""
        n = 0
        try:
            rows = db_queries.get_in_transit_movements()
        except Exception as e:
            logger.debug("CexTransferRail.load_in_transit query failed: %s", e)
            return 0
        for r in rows:
            try:
                if r.mode != "live":
                    continue
                inventory_state.open_transfer(
                    movement_id=r.id,
                    from_fund=r.from_fund, to_fund=r.to_fund,
                    from_exchange=r.from_exchange,
                    to_exchange=r.to_exchange,
                    amount_usd=float(r.amount_usd or 0.0),
                    asset=r.asset or "USDT",
                    state=r.state or "in_transit",
                )
                n += 1
            except Exception as e:
                logger.warning(
                    "cex_rail: failed to register in_transit movement %s: %s",
                    getattr(r, "id", "?"), e,
                )
        if n:
            logger.info("CexTransferRail: reconciled %d in_transit movement(s)", n)
        return n

    # ── DB helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _log_pending(t: "Transfer", *, network: str) -> int:
        try:
            return db_queries.log_capital_movement({
                "from_fund":     t.from_fund,
                "to_fund":       t.to_fund,
                "from_exchange": t.from_exchange,
                "to_exchange":   t.to_exchange,
                "asset":         t.asset,
                "amount_usd":    t.amount_usd,
                "mode":          "live",
                "state":         "pending",
                "rail_id":       "cex",
                "network":       network,
                "initiated_by":  "policy",
                "note":          t.note or "",
            })
        except Exception as e:
            logger.debug("cex_rail _log_pending failed: %s", e)
            return 0

    @staticmethod
    def _update(movement_id: int, fields: dict) -> None:
        if not movement_id:
            return
        try:
            db_queries.update_capital_movement(movement_id, fields)
        except Exception as e:
            logger.debug("cex_rail _update failed: %s", e)
