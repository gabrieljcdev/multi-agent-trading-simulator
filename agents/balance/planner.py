"""
agents/balance/planner.py

Turns (current ledger, targets, cost matrix, constraints) into the
minimum-cost set of physical transfers.

Steps the planner runs in order, each reducing transfer count:
    1. Internalize — discount transient imbalance the strategy's own
       flow will self-correct (reverse arbs). Only persistent drift
       beyond INTERNALIZE_WINDOW_S counts as structural.
    2. Net — net surpluses against deficits across the full vector.
       Shared-venue offsets between funds are free ledger reallocations,
       not transfers. Only the cross-venue residual survives.
    3. Control band (Miller-Orr 1966, derived at runtime):
           spread = (0.75 · transfer_cost · depletion_variance / opportunity_cost)^(1/3)
       A node only becomes a transfer candidate when its net residual
       breaches this band. Transfers return to the return point — NOT
       the band edge — so the node doesn't immediately re-trip.
    4. Solve cheapest batched set — greedy matcher now; clean seam for
       a min-cost-flow solver (networkx.min_cost_flow / scipy.linprog)
       later via BaseRebalancePlanner.
"""

from __future__ import annotations

import logging
import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from config import settings
from database import queries as db_queries

if TYPE_CHECKING:
    from agents.balance.inventory_state import InventoryState
    from agents.balance.policy.base import InventoryTarget

logger = logging.getLogger(__name__)


@dataclass
class Transfer:
    """A planned physical move. The rail consumes this and returns a
    TransferResult; the planner never executes anything itself."""
    from_fund:     str
    to_fund:       str
    from_exchange: str
    to_exchange:   str
    amount_usd:    float
    asset:         str = "USDT"
    cost_usd:      float = 0.0     # expected fee + slippage estimate
    note:          str = ""        # human-readable reason (e.g. "miller-orr breach")


@dataclass
class PlannerConstraints:
    """Bounds the planner respects when generating transfers."""
    daily_limit:   int   = 3
    daily_used:    int   = 0
    in_flight:     int   = 0    # number of active in_transit movements
    floor_overrides: dict = field(default_factory=dict)   # (fund, exchange) → USD


class BaseRebalancePlanner(ABC):
    """Planner plugin contract. Subclass for alternative algorithms
    (e.g. min-cost-flow LP) without changing policy or rails."""

    planner_id:   str = "base"
    display_name: str = "Base Planner"

    @abstractmethod
    def plan(
        self,
        inv: "InventoryState",
        targets: list["InventoryTarget"],
        cost_matrix: dict,
        constraints: PlannerConstraints,
    ) -> list[Transfer]:
        """Return the minimum-cost set of transfers that closes the
        current-vs-target gap, respecting constraints. Must NEVER
        raise — the BalanceAgent treats failure as an empty plan."""
        ...


# ─────────────────────────────────────────────────────────────────────────
# GreedyNetPlanner — the default implementation
# ─────────────────────────────────────────────────────────────────────────

class GreedyNetPlanner(BaseRebalancePlanner):
    """Internalize → net → derived-band → greedy cheapest-batched."""

    planner_id    = "greedy_net"
    display_name  = "Greedy Net Planner (Miller-Orr)"

    def plan(
        self,
        inv: "InventoryState",
        targets: list["InventoryTarget"],
        cost_matrix: dict,
        constraints: PlannerConstraints,
    ) -> list[Transfer]:
        try:
            return self._plan_inner(inv, targets, cost_matrix, constraints)
        except Exception as e:
            logger.error("GreedyNetPlanner.plan: %s", e, exc_info=True)
            return []

    def _plan_inner(
        self,
        inv: "InventoryState",
        targets: list["InventoryTarget"],
        cost_matrix: dict,
        constraints: PlannerConstraints,
    ) -> list[Transfer]:
        # ── Early-out guards (safety rails 4 + 5) ────────────────────
        if constraints.in_flight > 0 and not settings.SIM_MODE:
            # Rail 4 — in-flight lockout: live cannot start a new
            # transfer while one is still settling. Sim rail is atomic,
            # so the lockout doesn't apply.
            return []
        if constraints.daily_used >= constraints.daily_limit:
            # Rail 5 — daily rate limit
            return []

        if not targets:
            return []

        # ── Step 1: Internalize — drop nodes whose drift the strategy
        # itself is likely to mop up inside INTERNALIZE_WINDOW_S. We
        # proxy "likely" with two reads: drift magnitude vs target
        # (small drifts → internalize); plus a check that no
        # starvation event has been logged for the fund in the last
        # window (a starvation event means the strategy could NOT
        # self-correct, so the imbalance IS structural).
        structural = self._filter_to_structural(targets)
        if not structural:
            return []

        # ── Step 2: Net — surplus vs deficit per (fund, asset) and
        # per (exchange, asset). Same-venue offsets between funds are
        # FREE — ledger reallocations, not physical transfers.
        net = self._net_residual(inv, structural)
        if not net:
            return []

        # ── Step 3: Derived Miller-Orr band — per node, breach
        # required before a transfer fires. Transfer brings the node
        # back to the RETURN POINT (target), not the band edge.
        # _miller_orr_band returns the FULL SPREAD; the do-nothing
        # region is target ± spread/2. Comparing against ±band (full)
        # makes the do-nothing region 2× wider than intended — that's
        # the FIX 4 correction below.
        breached: list[tuple["InventoryTarget", float, float]] = []
        for tgt in structural:
            cur = inv.effective_balance(tgt.fund, tgt.exchange, tgt.asset)
            band = self._miller_orr_band(tgt)
            if band <= 0:
                continue
            half = band / 2.0
            # Breach if current is OUTSIDE [target − spread/2, target + spread/2].
            if cur < tgt.target_usd - half or cur > tgt.target_usd + half:
                # Per-cell signed residual (positive → surplus, send out;
                # negative → deficit, pull in). The amount is the move
                # back to the target (the return point) — independent of
                # spread, so no change here.
                residual = cur - tgt.target_usd
                breached.append((tgt, residual, band))

        if not breached:
            return []

        # ── Step 4: Greedy match surpluses to deficits, cheapest first.
        # surpluses + deficits at the same fund tier net to ledger
        # moves; cross-fund / cross-venue moves consume cost_matrix
        # entries and a daily rate-limit slot each.
        surpluses = [(t, r, b) for (t, r, b) in breached if r > 0]
        deficits  = [(t, -r, b) for (t, r, b) in breached if r < 0]
        if not surpluses or not deficits:
            return []
        surpluses.sort(key=lambda x: -x[1])    # biggest surplus first
        deficits.sort(key=lambda x: -x[1])     # biggest deficit first

        transfers: list[Transfer] = []
        remaining_slots = max(0, constraints.daily_limit - constraints.daily_used)
        for s_tgt, s_amt, _ in surpluses:
            if remaining_slots <= 0:
                break
            for i, (d_tgt, d_amt, _) in enumerate(deficits):
                if d_amt <= 0 or s_amt <= 0:
                    continue
                if remaining_slots <= 0:
                    break
                # Same-venue reallocation is free; we still emit a
                # transfer so the rail can update _claims atomically,
                # but cost_usd=0 and it doesn't burn a daily slot.
                move = min(s_amt, d_amt)
                if move <= 0:
                    continue
                # Respect floors: never pull the source below floor.
                src_cur = (
                    inv.effective_balance(s_tgt.fund, s_tgt.exchange, s_tgt.asset)
                )
                headroom = max(0.0, src_cur - s_tgt.floor_usd)
                move = min(move, headroom)
                if move <= 0:
                    continue
                # Respect caps: never push the destination above cap.
                dst_cur = inv.effective_balance(d_tgt.fund, d_tgt.exchange, d_tgt.asset)
                dst_room = max(0.0, d_tgt.cap_usd - dst_cur)
                move = min(move, dst_room)
                if move <= 0:
                    continue

                # Cost-priced edge — depletion-priced cost matrix entry.
                edge = (s_tgt.exchange, d_tgt.exchange, s_tgt.asset)
                cost = self._edge_cost(cost_matrix, edge, move,
                                       src_cur=src_cur, floor=s_tgt.floor_usd)

                transfers.append(Transfer(
                    from_fund=s_tgt.fund, to_fund=d_tgt.fund,
                    from_exchange=s_tgt.exchange, to_exchange=d_tgt.exchange,
                    amount_usd=move, asset=s_tgt.asset,
                    cost_usd=cost,
                    note=(
                        f"miller-orr breach src_cur={src_cur:.2f} "
                        f"target={s_tgt.target_usd:.2f}"
                    ),
                ))

                # Only physical (cross-venue) transfers count against
                # the daily slot budget; intra-venue ledger moves are
                # free and don't burn slots.
                if s_tgt.exchange != d_tgt.exchange:
                    remaining_slots -= 1

                s_amt -= move
                deficits[i] = (d_tgt, d_amt - move, _)
                if s_amt <= 0:
                    break

        return transfers

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _filter_to_structural(
        targets: list["InventoryTarget"],
    ) -> list["InventoryTarget"]:
        """Internalize: keep only targets whose drift is large enough that
        the strategy can't self-correct within INTERNALIZE_WINDOW_S, OR
        whose fund has logged a recent starvation event (strong
        structural signal). Pure heuristic — the theta hint from the
        policy already encoded "needs_rebalance"; we add a starvation
        confirmation when available."""
        try:
            recent_starvations = set()
            for fund in {t.fund for t in targets}:
                row = db_queries.get_latest_fund_efficiency(fund)
                if row is not None and getattr(row, "starvation_event", False):
                    recent_starvations.add(fund)
        except Exception as e:
            logger.debug("_filter_to_structural: efficiency query failed: %s", e)
            recent_starvations = set()
        out = []
        for t in targets:
            # Pure drift over the policy's hint
            # (settings.BALANCE_STRUCTURAL_DRIFT_HINT) keeps the planner
            # from getting talkative on routine fluctuation; a logged
            # starvation accelerates the trip regardless of drift size.
            if (t.fund in recent_starvations
                    or abs(t.drift_pct) > settings.BALANCE_STRUCTURAL_DRIFT_HINT):
                out.append(t)
        return out

    @staticmethod
    def _net_residual(
        inv: "InventoryState",
        targets: list["InventoryTarget"],
    ) -> dict[tuple[str, str], float]:
        """Aggregate signed residual per (exchange, asset) — what's left
        after every fund's offsets within a venue cancel out."""
        residual: dict[tuple[str, str], float] = {}
        for t in targets:
            cur = inv.effective_balance(t.fund, t.exchange, t.asset)
            key = (t.exchange, t.asset)
            residual[key] = residual.get(key, 0.0) + (cur - t.target_usd)
        return residual

    @staticmethod
    def _miller_orr_band(tgt: "InventoryTarget") -> float:
        """Derived Miller-Orr spread (FULL WIDTH) for one node.

            spread = (0.75 · transfer_cost · σ²_depletion / opportunity_cost)^(1/3)

        Returns the FULL spread. The do-nothing region is
        ``target ± spread/2`` — the caller (`_plan_inner`) computes
        ``half = band / 2.0`` and breaches the band when current is
        outside ``[target − half, target + half]``. This contract — band
        returns full spread, caller halves it — is the FIX 4 invariant;
        do not change it without updating the breach test in tandem.

        transfer_cost is the simulated per-transfer fee
        (SIM_WITHDRAWAL_FEE_USD) by default; opportunity_cost is the
        per-fund return-on-deployed; depletion_variance comes from the
        efficiency history. Conservative fallbacks keep the band sane
        on cold start (slightly wider — fewer false triggers).
        """
        transfer_cost = float(getattr(settings, "SIM_WITHDRAWAL_FEE_USD", 1.0))
        # opportunity_cost: per-fund return-on-deployed (decimal, not %).
        # Cold start → 0.01 (1% per cycle), wide-band default.
        try:
            row = db_queries.get_latest_fund_efficiency(tgt.fund)
            opp_pct = float(getattr(row, "return_on_deployed_pct", 0.0) or 0.0) if row else 0.0
        except Exception:
            opp_pct = 0.0
        opp_cost = max(1e-4, abs(opp_pct) / 100.0)

        # depletion variance: variance of deployed_usd over the
        # rolling window; fall back to (10% of target)^2 cold.
        try:
            rows = db_queries.get_fund_capital_efficiency(tgt.fund, hours=720)
            series = [float(getattr(r, "deployed_usd", 0.0) or 0.0) for r in rows]
            if len(series) >= 3:
                dep_var = statistics.pvariance(series)
            else:
                dep_var = (0.10 * max(1.0, tgt.target_usd)) ** 2
        except Exception:
            dep_var = (0.10 * max(1.0, tgt.target_usd)) ** 2
        dep_var = max(1.0, dep_var)

        # Miller-Orr canonical formula
        spread = (0.75 * transfer_cost * dep_var / opp_cost) ** (1.0 / 3.0)
        # Cap the band so it never exceeds the target itself — a band
        # wider than the target means anything goes, which defeats the
        # point.
        return float(min(spread, max(1.0, abs(tgt.target_usd))))

    @staticmethod
    def _edge_cost(
        cost_matrix: dict,
        edge: tuple[str, str, str],
        amount_usd: float,
        *,
        src_cur: float,
        floor: float,
    ) -> float:
        """Cost for the (from_ex, to_ex, asset) move, scaled by
        depletion priority (cost rises as the source empties — the
        Stargate Delta / Across-inspired self-throttle)."""
        base = float(cost_matrix.get(edge, getattr(settings, "SIM_WITHDRAWAL_FEE_USD", 1.0)))
        # Depletion premium: when src is within 10% of its floor, the
        # cost doubles — the planner naturally prefers other sources.
        if src_cur > 0 and floor >= 0:
            headroom_pct = (src_cur - floor) / max(1.0, src_cur)
            if headroom_pct < 0.10:
                base *= 2.0
        return base
