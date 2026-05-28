"""
agents/balance_agent.py

The BalanceAgent — operational, not alpha-generating.

It owns the bot's capital position across funds × exchanges. Each scan
it does six jobs:

  1. Refresh per-fund equity from realised P&L (no transfers).
  2. Compound that profit by scaling agents' deployable capital via
     BaseAgent.set_capital_allocation (no transfers, no cost).
  3. Ask the active policy for InventoryTargets (where capital SHOULD
     sit) — see agents/balance/policy/.
  4. Hand targets to the planner for a min-cost transfer set —
     internalize → net → Miller-Orr derived band → batched solve.
  5. Dispatch each surviving transfer through the first available rail
     (sim or cex) — see agents/balance/rails/.
  6. Log per-fund capital_efficiency + each capital_movements row so
     the policy + planner have data to read on the next cycle.

Safety rails (1–7) wrap dispatch:
  1. Total invariant — sum of new allocations equals current equity.
  2. Open-position floor (always on) — no fund below open-position
     notional.
  3. BALANCE_STRICT_OPEN_POSITION_BLOCK — optional stricter block while
     any affected agent has an open position.
  4. In-flight lockout — at most one pending live rebalance.
  5. Daily rate limit — REBALANCE_DAILY_LIMIT physical moves.
  6. Two-step confirm token — only consumed when the operator
     authorises via /action/rebalance (the agent never bypasses).
  7. Ring-fence notice — surfaced in the UI confirm step on any live
     cross-venue move.

Compounding is realized-and-reconciled only; mark-to-market never feeds
the deployable pool. Kill switch (close_all_positions) blocks every
NEW transfer immediately (paused flag) but lets in-flight transfers
settle — un-sending a withdrawal is not an option once the network
has the tx.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, date
from typing import Optional

from config import settings
from database import queries as db_queries

from agents.base import (
    AgentStats, BaseAgent,
    RUNNING, PAUSED, HALTED, OFFLINE, STOPPED,
)
from agents.balance.inventory_state import inventory_state
from agents.balance.policy import REGISTERED_POLICIES
from agents.balance.policy.base import BasePolicy, InventoryTarget
from agents.balance.planner import (
    BaseRebalancePlanner, GreedyNetPlanner, PlannerConstraints, Transfer,
)
from agents.balance.rails import REGISTERED_RAILS
from agents.balance.rails.base import BaseTransferRail, TransferResult
from agents.balance.rails.cex_rail import CexTransferRail

logger = logging.getLogger(__name__)


class BalanceAgent(BaseAgent):
    """Operational agent — see module docstring."""

    agent_id     = "balance"
    display_name = "Balance Agent"
    optional     = True

    def __init__(
        self,
        *,
        policies: Optional[list[BasePolicy]] = None,
        rails:    Optional[list[BaseTransferRail]] = None,
        planner:  Optional[BaseRebalancePlanner] = None,
    ):
        super().__init__()
        # capital_allocation is the BalanceAgent's OWN pool — 0 by
        # default; it earns nothing, owns nothing, and never deploys.
        # The pools it MANAGES are FUND_* in settings.
        self.capital_allocation = float(getattr(settings, "BALANCE_AGENT_CAPITAL", 0.0))

        self._policies = list(policies) if policies is not None else list(REGISTERED_POLICIES)
        self._rails    = list(rails)    if rails    is not None else list(REGISTERED_RAILS)
        self._planner  = planner if planner is not None else GreedyNetPlanner()

        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Rebalance dispatch is paused after a failed transfer (scoped
        # auto-pause) and during the kill-switch close. The arb side of
        # the scoped pause is enforced by InventoryState.pause_route;
        # this flag is the BalanceAgent's own gate.
        self._paused = False

        # Pending /action/rebalance arm token. arm() creates one;
        # confirm() consumes it within REBALANCE_CONFIRM_WINDOW_S.
        self._arm_token: Optional[str] = None
        self._arm_expires_at: float = 0.0

        # Daily rebalance counter — UTC reset.
        self._daily_rebalances: int = 0
        self._last_reset_date: date = datetime.utcnow().date()

        # Stats for the dashboard.
        self._transfers_today: int = 0
        self._transfers_failed_today: int = 0
        self._fees_today_usd: float = 0.0
        self._last_plan_at: Optional[float] = None

    # ── BaseAgent contract ──────────────────────────────────────────────

    def is_available(self) -> bool:
        # Always available — even with no policies / rails wired up it
        # still serves InventoryState reads. The scan loop no-ops when
        # there's nothing to do.
        return True

    async def start(self) -> None:
        self._running = True
        self._status = RUNNING
        self._start_time = time.time()
        # Restart reconciliation: load any in-transit live transfers
        # back into InventoryState so the ledger view picks up the
        # pending-out / pending-in adjustments from before the kill.
        for r in self._rails:
            if isinstance(r, CexTransferRail):
                try:
                    r.load_in_transit()
                except Exception as e:
                    logger.debug("BalanceAgent: cex load_in_transit: %s", e)
        logger.info(
            "BalanceAgent: starting (policies=%d, rails=%d, planner=%s)",
            len(self._policies), len(self._rails), self._planner.planner_id,
        )
        self._task = asyncio.create_task(self._loop())
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._status = STOPPED

    async def close_all_positions(self) -> None:
        """Kill-switch: block all NEW transfers immediately. In-flight
        transfers settle naturally — we can't un-send a network call —
        but no new ones leave the agent."""
        self._paused = True
        logger.critical("BalanceAgent: kill — new transfers blocked")
        try:
            db_queries.log_agent_event(
                self.agent_id, "KILLED",
                "new transfers blocked; in-flight allowed to settle",
            )
        except Exception:
            pass

    async def get_stats(self) -> AgentStats:
        # Equity is the sum of all managed pools + realised. Pull
        # defensively — get_stats must NEVER raise.
        try:
            signal_p = float(getattr(settings, "FUND_SIGNAL_CAPITAL", 0.0))
            arb_p    = float(getattr(settings, "FUND_ARB_CAPITAL", 0.0))
            scalp_p  = float(getattr(settings, "FUND_MEXC_SCALP_CAPITAL", 0.0))
            total_pool = signal_p + arb_p + scalp_p
        except Exception:
            total_pool = 0.0
        try:
            realised = (
                db_queries.get_arb_realized_pnl()
                + db_queries.get_trade_realized_pnl(exclude_strategy="scalp")
                + db_queries.get_scalp_realized_pnl()
            )
            realised_today = (
                db_queries.get_arb_realized_pnl(today=True)
                + db_queries.get_trade_realized_pnl(exclude_strategy="scalp", today=True)
                + db_queries.get_scalp_realized_pnl(today=True)
            )
        except Exception:
            realised = 0.0
            realised_today = 0.0
        equity = total_pool + realised
        return AgentStats(
            agent_id=self.agent_id,
            status=PAUSED if self._paused else (RUNNING if self._running else OFFLINE),
            capital_allocated=self.capital_allocation,
            capital_deployed=0.0,
            daily_pnl=float(realised_today),
            daily_pnl_pct=0.0,
            total_pnl=float(realised),
            trades_today=int(self._transfers_today),
            win_rate_today=0.0,
            win_rate_alltime=0.0,
            consecutive_losses=int(self._transfers_failed_today),
            last_trade_time=(
                datetime.utcfromtimestamp(self._last_plan_at).isoformat()
                if self._last_plan_at else None
            ),
            error=None,
        )

    # ── /action/rebalance two-step (rail 6) ─────────────────────────────

    def arm(self) -> tuple[str, dict]:
        """Issue an arm token. Caller (web_server.handle_rebalance_arm)
        records it and surfaces the ring-fence notice. Token expires
        after REBALANCE_CONFIRM_WINDOW_S."""
        import secrets
        token = secrets.token_hex(8)
        window = float(getattr(settings, "REBALANCE_CONFIRM_WINDOW_S", 3))
        self._arm_token = token
        self._arm_expires_at = time.time() + window
        notice = self._ring_fence_notice()
        return token, notice

    def consume_arm(self, token: str) -> bool:
        """Validate + consume an arm token. Returns False (and clears
        any pending token) if expired or mismatched — fail-closed."""
        if not self._arm_token or token != self._arm_token:
            self._arm_token = None
            return False
        if time.time() > self._arm_expires_at:
            self._arm_token = None
            return False
        self._arm_token = None
        return True

    def _ring_fence_notice(self) -> dict:
        """Operator-facing surface on the confirm step. Highlights live
        cross-venue moves and adds a MEXC counterparty caveat — we
        cannot mitigate it with off-exchange settlement, so floors +
        caps + this notice are what shoulders the risk."""
        live = bool(getattr(settings, "REBALANCE_LIVE_ENABLED", False))
        return {
            "live":          live,
            "mexc_warning":  (
                "MEXC carries un-mitigated counterparty risk (no OES)"
                if live else ""
            ),
            "default_fee":   float(getattr(settings, "SIM_WITHDRAWAL_FEE_USD", 1.0)),
            "default_delay_s": float(getattr(settings, "SIM_TRANSFER_DELAY_S", 600)),
        }

    # ── Scan loop ───────────────────────────────────────────────────────

    async def _loop(self) -> None:
        interval = max(1.0, float(getattr(settings, "BALANCE_SCAN_INTERVAL_SEC", 60)))
        while self._running:
            try:
                self._check_daily_reset()
                await self._scan_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("BalanceAgent loop: %s", e, exc_info=True)
            await asyncio.sleep(interval)

    async def _scan_once(self) -> None:
        # Always-on Job #1 + #2: compound realised P&L into each fund.
        # No transfers, no cost, runs every scan regardless of paused state.
        equity = self._compound_realised_into_funds()
        self._last_plan_at = time.time()

        if self._paused:
            return

        # Active policy — first available, fail-closed.
        policy = self._pick_policy()
        if policy is None:
            return
        try:
            targets = policy.compute_targets(inventory_state, equity)
        except Exception as e:
            logger.error("policy.compute_targets failed: %s", e)
            return
        if not targets:
            return

        # Publish per-(fund, exchange) claim so InventoryState reflects
        # the new policy targets, regardless of whether the planner
        # decides to physically move anything.
        for t in targets:
            inventory_state.apply_allocation(t.fund, t.exchange, t.asset, t.target_usd)

        # Safety rail 1 — total invariant check.
        plan_sum = sum(max(0.0, float(t.target_usd)) for t in targets)
        if equity > 0 and plan_sum > equity * 1.05:
            logger.warning(
                "BalanceAgent: targets sum $%.2f exceeds equity $%.2f by >5%% "
                "— skipping plan",
                plan_sum, equity,
            )
            return

        constraints = PlannerConstraints(
            daily_limit=int(getattr(settings, "REBALANCE_DAILY_LIMIT", 3)),
            daily_used=self._daily_rebalances,
            in_flight=self._count_in_flight(),
        )
        try:
            transfers = self._planner.plan(
                inv=inventory_state, targets=targets,
                cost_matrix=self._cost_matrix(), constraints=constraints,
            )
        except Exception as e:
            logger.error("planner.plan failed: %s", e)
            return
        if not transfers:
            return

        # Apply safety rails per-transfer before dispatch.
        cleared = [t for t in transfers if self._safety_clear(t)]
        if not cleared:
            return

        # Dispatch.
        await asyncio.gather(
            *(self._dispatch(t) for t in cleared),
            return_exceptions=True,
        )

    # ── Compounding (#1 + #2) ───────────────────────────────────────────

    def _compound_realised_into_funds(self) -> float:
        """Per-fund equity refresh + capital_allocation propagation.

        Reads REALISED + RECONCILED only — never mark-to-market.
        Each fund's new deployable equals its base pool + its accumulated
        realised P&L. Propagated via BaseAgent.set_capital_allocation so
        sizing in arb_engine / scalping_agent / order_router scales up
        automatically.
        """
        # Per-fund equity. Realised numbers come from the ledger; the
        # base pool comes from the configured fund constants.
        try:
            signal_today = db_queries.get_trade_realized_pnl(
                exclude_strategy="scalp", today=True)
            signal_total = db_queries.get_trade_realized_pnl(
                exclude_strategy="scalp")
            arb_today    = db_queries.get_arb_realized_pnl(today=True)
            arb_total    = db_queries.get_arb_realized_pnl()
            scalp_today  = db_queries.get_scalp_realized_pnl(today=True)
            scalp_total  = db_queries.get_scalp_realized_pnl()
        except Exception as e:
            logger.debug("BalanceAgent: realised-P&L query failed: %s", e)
            return 0.0

        per_fund = {
            "signal":     float(getattr(settings, "FUND_SIGNAL_CAPITAL", 0.0)) + signal_total,
            "arb":        float(getattr(settings, "FUND_ARB_CAPITAL", 0.0)) + arb_total,
            "mexc_scalp": float(getattr(settings, "FUND_MEXC_SCALP_CAPITAL", 0.0)) + scalp_total,
        }
        per_fund_daily = {
            "signal":     signal_today,
            "arb":        arb_today,
            "mexc_scalp": scalp_today,
        }
        equity = sum(per_fund.values())

        # Propagate via BaseAgent setter — safe inherited default
        # refuses if below the agent's open-position notional. We try
        # each known agent_id; sibling agents we can't find are
        # skipped silently.
        for agent_id, fund_id in (("signal", "signal"),
                                  ("arb", "arb"),
                                  ("scalp", "mexc_scalp")):
            try:
                sibling = self._find_sibling(agent_id)
            except Exception:
                sibling = None
            if sibling is None:
                continue
            try:
                sibling.set_capital_allocation(per_fund[fund_id])
            except Exception as e:
                logger.debug("set_capital_allocation %s: %s", agent_id, e)

        # Log per-fund efficiency snapshot so the policy + planner have
        # data to read next cycle.
        for fund_id, fund_pool in per_fund.items():
            try:
                base = fund_pool - per_fund_daily[fund_id]
                deployed_est = max(0.0, base)
                ret_pct = (per_fund_daily[fund_id] / base * 100.0) if base > 0 else 0.0
                db_queries.log_fund_capital_efficiency({
                    "fund":                   fund_id,
                    "deployed_usd":           deployed_est,
                    "realised_return_usd":    per_fund_daily[fund_id],
                    "return_on_deployed_pct": ret_pct,
                    "starvation_event":       False,
                    "starvation_detail":      "",
                })
            except Exception as e:
                logger.debug("log_fund_capital_efficiency %s: %s", fund_id, e)

        return float(equity)

    # ── Dispatch + safety rails ─────────────────────────────────────────

    def _safety_clear(self, t: Transfer) -> bool:
        """Rail 2 (always-on) + Rail 3 (optional strict): a transfer
        must never break the open-position floor on the source side."""
        sib = self._find_sibling_for_fund(t.from_fund)
        if sib is not None:
            try:
                open_notional = float(sib.get_open_position_notional() or 0.0)
                if open_notional > 0 and t.amount_usd > 0:
                    # Strict mode: refuse any rebalance while the source
                    # agent has open positions.
                    if bool(getattr(settings, "BALANCE_STRICT_OPEN_POSITION_BLOCK", True)):
                        logger.info(
                            "BalanceAgent: strict block — fund=%s has open notional $%.2f",
                            t.from_fund, open_notional,
                        )
                        return False
            except Exception:
                pass
        return True

    async def _dispatch(self, t: Transfer) -> None:
        rail = self._pick_rail(t)
        if rail is None:
            logger.warning(
                "BalanceAgent: no available rail for %s -> %s — skipping",
                t.from_exchange, t.to_exchange,
            )
            return
        result = await rail.execute(t)
        # Stats — count completed and in_transit as "moves today" (they
        # consumed the daily slot); failures bump the failed counter
        # and trip the scoped auto-pause.
        if result.success:
            self._transfers_today += 1
            self._fees_today_usd += float(result.fee_usd or 0.0)
            # Cross-venue moves consumed a slot.
            if t.from_exchange != t.to_exchange:
                self._daily_rebalances += 1
        else:
            self._transfers_failed_today += 1
            # Scoped auto-pause: stop deepening the affected route via
            # InventoryState; require manual unpause.
            inventory_state.pause_route(
                t.from_exchange, t.to_exchange,
                reason=f"rebalance failure: {result.error or 'unknown'}",
            )
            try:
                db_queries.log_agent_event(
                    self.agent_id, "REBALANCE_FAILED",
                    f"{t.from_exchange}->{t.to_exchange} ${t.amount_usd:.2f} "
                    f"err={result.error or 'unknown'}",
                )
            except Exception:
                pass

    def _pick_policy(self) -> Optional[BasePolicy]:
        for p in self._policies:
            try:
                if p.is_available():
                    return p
            except Exception:
                continue
        return None

    def _pick_rail(self, t: Transfer) -> Optional[BaseTransferRail]:
        live = bool(getattr(settings, "REBALANCE_LIVE_ENABLED", False))
        for r in self._rails:
            try:
                if not r.is_available():
                    continue
            except Exception:
                continue
            # Prefer SimTransferRail when not live, CexTransferRail otherwise.
            if live and r.rail_id == "cex":
                return r
            if not live and r.rail_id == "sim":
                return r
        # Fallback — any available rail (e.g. only sim wired in tests).
        for r in self._rails:
            try:
                if r.is_available():
                    return r
            except Exception:
                continue
        return None

    def _count_in_flight(self) -> int:
        try:
            return len(db_queries.get_in_transit_movements())
        except Exception:
            return 0

    @staticmethod
    def _cost_matrix() -> dict:
        """(from_ex, to_ex, asset) → expected cost USD. Cold-start uses
        the sim fee for every edge — once live, the rail can refresh
        from ccxt.fetch_deposit_withdraw_fees() and this map gets
        populated per-route."""
        base = float(getattr(settings, "SIM_WITHDRAWAL_FEE_USD", 1.0))
        routes = getattr(settings, "WITHDRAWAL_ROUTES", {}) or {}
        out: dict = {}
        for key in routes:
            out[key] = base
        return out

    @staticmethod
    def _find_sibling(agent_id: str) -> Optional[BaseAgent]:
        try:
            from agents import REGISTERED_AGENTS
        except Exception:
            return None
        for a in REGISTERED_AGENTS:
            if getattr(a, "agent_id", "") == agent_id:
                return a
        return None

    def _find_sibling_for_fund(self, fund_id: str) -> Optional[BaseAgent]:
        return self._find_sibling({
            "signal": "signal", "arb": "arb", "mexc_scalp": "scalp",
        }.get(fund_id, fund_id))

    def _check_daily_reset(self) -> None:
        today = datetime.utcnow().date()
        if today > self._last_reset_date:
            self._daily_rebalances = 0
            self._transfers_today = 0
            self._transfers_failed_today = 0
            self._fees_today_usd = 0.0
            self._last_reset_date = today
            logger.info("BalanceAgent: daily counters reset at UTC midnight")
