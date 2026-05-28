"""
agents/balance/inventory_state.py

The fund-aware ledger view every other layer reads.

Why this exists
---------------
EXCHANGE_BALANCES (config/settings.py) is a per-venue ledger, NOT a
per-fund one — funds share venues (kraken/bybit are shared between the
signal fund and the arb fund; MEXC backs scalp and is also an arb venue).
A naive ``ex.fetch_balance()`` would let one fund consume another
fund's claim on a shared venue. InventoryState applies each fund's
claim on each venue, subtracts pending-out transfers, and adds
pending-in transfers — the result is what an agent can actually use
right now.

It also surfaces ``can_arb()`` — the two-band gate the ArbEngine
consults. The SOFT halt band stops new trades that would push a node
past its safety floor; the HARD impulse band is what triggers a
rebalance proposal in the planner.

Module-level singleton ``inventory_state`` mirrors the
``sentiment_aggregator`` pattern. Reads are lock-free; writes
(only BalanceAgent calls these) take ``_lock`` for atomicity.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class _PendingTransfer:
    """In-memory record of an in-flight transfer.

    Mirrors a subset of the persisted ``capital_movements`` row so the
    sim path can run without a DB session, and the live path can
    reconstruct from the persisted row on restart. The persisted row
    is the source of truth for live; this is the fast in-memory view.
    """
    movement_id:   int
    from_fund:     str
    to_fund:       str
    from_exchange: Optional[str]
    to_exchange:   Optional[str]
    amount_usd:    float
    asset:         str
    state:         str          # pending | in_transit | completed | failed
    opened_at:     float = field(default_factory=time.time)


class InventoryState:
    """Singleton ledger view shared across agents.

    Read API (the rest of the system):
        effective_balance(fund, exchange, asset)
        can_arb(pair, side, size)
        is_arb_halted(pair, side)
        get_snapshot()

    Write API (BalanceAgent only):
        apply_allocation(fund, exchange, asset, target_usd)
        open_transfer(movement_id, from_fund, to_fund, …)
        settle_transfer(movement_id)
        fail_transfer(movement_id, reason)
        pause_route(buy_exchange, sell_exchange, reason)
        clear_route_pause(buy_exchange, sell_exchange)

    Defaults are permissive — a fresh state with no claims registered
    treats every fund as owning its own venue share, and ``can_arb``
    falls through to True. Operators get fail-open behaviour before
    the BalanceAgent has booted; once it boots and registers claims
    the gates start blocking.
    """

    def __init__(self):
        self._lock = threading.RLock()
        # Per-fund claim of each venue's physical balance, in USD-equivalent
        # terms for now (USDT-quoted). _claims[(fund, exchange, asset)] = USD.
        # When unset, effective_balance treats the venue as available to
        # any fund (the fail-open default).
        self._claims: dict[tuple[str, str, str], float] = {}
        # Per-(fund, exchange) deployable allocation snapshot — what the
        # BalanceAgent thinks should be sitting here. Used as the "current"
        # in the policy / planner.
        self._allocations: dict[tuple[str, str], float] = {}
        # In-flight transfers keyed by movement_id.
        self._transfers: dict[int, _PendingTransfer] = {}
        # Scoped halt: (buy_exchange, sell_exchange) routes the planner has
        # marked unsafe. ArbEngine's can_arb consults this to stop
        # *deepening* the imbalance while allowing reducing arbs.
        self._paused_routes: dict[tuple[str, str], str] = {}

    # ── Reads ───────────────────────────────────────────────────────────

    def effective_balance(self, fund: str, exchange: str, asset: str = "USDT") -> float:
        """What this fund can actually deploy on this venue right now.

        physical (EXCHANGE_BALANCES) − other funds' claims on this venue
            − this fund's pending OUT
            + this fund's pending IN

        Falls back to the venue's physical balance when no claims are
        registered — the fail-open default the rest of the system needs
        before the BalanceAgent has booted.
        """
        physical = float(settings.EXCHANGE_BALANCES.get(exchange, 0.0) or 0.0)
        with self._lock:
            # Total claim across all funds on (exchange, asset)
            total_claim = 0.0
            mine = 0.0
            has_claims = False
            for (f, ex, a), v in self._claims.items():
                if ex == exchange and a == asset:
                    has_claims = True
                    total_claim += v
                    if f == fund:
                        mine = v
            pending_out = sum(
                t.amount_usd for t in self._transfers.values()
                if t.from_fund == fund and t.from_exchange == exchange
                and t.asset == asset and t.state in ("pending", "in_transit")
            )
            pending_in = sum(
                t.amount_usd for t in self._transfers.values()
                if t.to_fund == fund and t.to_exchange == exchange
                and t.asset == asset and t.state == "in_transit"
            )

        if not has_claims:
            # No fund has registered a claim on this venue — every fund
            # sees the physical balance. This is the fail-open default
            # the existing arb engine has always used.
            base = physical
        else:
            # Respect claims: this fund's share, scaled by the claim
            # ratio if claims overshoot the physical (oversubscribed
            # venue → proportional).
            if total_claim > physical and total_claim > 0:
                base = physical * (mine / total_claim)
            else:
                base = mine
        return max(0.0, base - pending_out + pending_in)

    def get_allocation(self, fund: str, exchange: str) -> float:
        """The BalanceAgent's recorded allocation for this (fund, exchange).
        0.0 when nothing is registered."""
        with self._lock:
            return float(self._allocations.get((fund, exchange), 0.0))

    def is_arb_halted(self, pair: str, side: str = "buy") -> bool:
        """Convenience: True iff any route involving this pair's venues
        is paused. The dashboard surface; for the actual gate, the
        engine calls can_arb."""
        # Without a (buy_ex, sell_ex) the pair alone can't tell us — the
        # gate runs against actual routes. Used only as a coarse dashboard
        # hint: True if ANY route is paused right now.
        with self._lock:
            return bool(self._paused_routes)

    def can_arb(
        self,
        pair: str,
        side: str,
        size: float,
        *,
        buy_exchange: Optional[str] = None,
        sell_exchange: Optional[str] = None,
        fund: str = "arb",
    ) -> bool:
        """Two-band gate the ArbEngine consults before firing.

        SOFT band — would this trade push the buy-side node past its
        configured cap, or the sell-side node below its floor? If yes,
        refuse.

        HARD band — the planner's job; we don't decide rebalance here.

        Scoped-pause check: if (buy_exchange, sell_exchange) is in the
        paused routes, refuse. Reducing arbs (opposite direction) are
        still allowed — `_paused_routes` is keyed by direction.
        """
        if buy_exchange is None or sell_exchange is None:
            # Without route info we can't enforce the scoped pause; let
            # the engine through and rely on the in-flight balance check.
            return True
        with self._lock:
            if (buy_exchange, sell_exchange) in self._paused_routes:
                return False
        # Capacity cap on the buy side; floor on the sell side. Caps come
        # from FUND_CAPACITY_CEILINGS_USD (per-(fund,exchange) when
        # specified; +inf otherwise so an empty map blocks nothing).
        caps = getattr(settings, "FUND_CAPACITY_CEILINGS_USD", {}) or {}
        buy_cap = float(caps.get((fund, buy_exchange), float("inf")))
        if buy_cap < float("inf"):
            buy_bal = self.effective_balance(fund, buy_exchange)
            if buy_bal + size > buy_cap:
                return False
        # SELL side floor — never push below the agent's claimed share.
        # An effective balance < size means we don't have the inventory.
        sell_bal = self.effective_balance(fund, sell_exchange)
        if sell_bal < size:
            return False
        return True

    def get_snapshot(self) -> dict:
        """Dashboard-friendly view of the live state. Safe to call
        every push tick; never raises."""
        with self._lock:
            claims_by_fund: dict[str, dict[str, float]] = defaultdict(dict)
            for (f, ex, a), v in self._claims.items():
                claims_by_fund[f][f"{ex}:{a}"] = v
            allocations = {f"{f}:{ex}": v for (f, ex), v in self._allocations.items()}
            pending = [
                {
                    "movement_id":  t.movement_id,
                    "from_fund":    t.from_fund,
                    "to_fund":      t.to_fund,
                    "from_exchange": t.from_exchange,
                    "to_exchange":  t.to_exchange,
                    "amount_usd":   t.amount_usd,
                    "asset":        t.asset,
                    "state":        t.state,
                    "age_sec":      time.time() - t.opened_at,
                }
                for t in self._transfers.values()
            ]
            paused_routes = [
                {"buy_exchange": k[0], "sell_exchange": k[1], "reason": v}
                for k, v in self._paused_routes.items()
            ]
        return {
            "claims_by_fund": dict(claims_by_fund),
            "allocations":    allocations,
            "pending":        pending,
            "paused_routes":  paused_routes,
        }

    # ── Writes (BalanceAgent only) ──────────────────────────────────────

    def apply_allocation(
        self,
        fund: str,
        exchange: str,
        asset: str,
        target_usd: float,
    ) -> None:
        """Record the BalanceAgent's claim on (fund, exchange, asset).

        Replaces any prior claim — call once per scan per cell. The
        rest of the system reads this via effective_balance.
        """
        try:
            v = float(target_usd)
        except (TypeError, ValueError):
            return
        if v < 0:
            v = 0.0
        with self._lock:
            self._claims[(fund, exchange, asset)] = v
            self._allocations[(fund, exchange)] = v

    def open_transfer(
        self,
        movement_id: int,
        from_fund: str,
        to_fund: str,
        amount_usd: float,
        *,
        from_exchange: Optional[str] = None,
        to_exchange: Optional[str] = None,
        asset: str = "USDT",
        state: str = "in_transit",
    ) -> None:
        """Register an in-flight transfer. amount_usd is subtracted from
        the source fund's effective balance immediately, and added to
        the destination's only on settle (we don't optimistically credit
        until the rail confirms in_transit)."""
        with self._lock:
            self._transfers[movement_id] = _PendingTransfer(
                movement_id=movement_id,
                from_fund=from_fund, to_fund=to_fund,
                from_exchange=from_exchange, to_exchange=to_exchange,
                amount_usd=float(amount_usd or 0.0),
                asset=asset, state=state,
            )

    def settle_transfer(self, movement_id: int) -> None:
        """Mark a transfer completed and remove it from the in-flight set.
        The destination fund's effective balance picks up the inventory
        on the next physical ledger update; we just stop holding the
        pending-in adjustment."""
        with self._lock:
            self._transfers.pop(movement_id, None)

    def fail_transfer(self, movement_id: int, reason: str = "") -> None:
        """Mark a transfer failed and remove. The source fund's effective
        balance recovers immediately (we stop subtracting pending-out)."""
        if reason:
            logger.warning("InventoryState: transfer %s failed — %s",
                           movement_id, reason)
        with self._lock:
            self._transfers.pop(movement_id, None)

    # ── Scoped pause for failed rebalances (safety rail) ────────────────

    def pause_route(self, buy_exchange: str, sell_exchange: str, reason: str) -> None:
        """Block further deepening of a route after a rebalance failure.
        Reducing arbs (sell_exchange→buy_exchange) stay allowed —
        symmetric pause would also block the recovery path."""
        with self._lock:
            self._paused_routes[(buy_exchange, sell_exchange)] = reason
        logger.critical(
            "InventoryState: route %s→%s PAUSED — %s",
            buy_exchange, sell_exchange, reason,
        )

    def clear_route_pause(self, buy_exchange: str, sell_exchange: str) -> None:
        """Manual unpause — called from the operator UI / scripts after
        the root cause is fixed."""
        with self._lock:
            self._paused_routes.pop((buy_exchange, sell_exchange), None)

    # ── Test / reset hook ──────────────────────────────────────────────

    def reset(self) -> None:
        """Wipe every internal map. Used only by the test suite — never
        in production code. There is no production reason to clear the
        ledger view."""
        with self._lock:
            self._claims.clear()
            self._allocations.clear()
            self._transfers.clear()
            self._paused_routes.clear()


# Module-level singleton — CLAUDE.md singleton pattern.
inventory_state = InventoryState()
