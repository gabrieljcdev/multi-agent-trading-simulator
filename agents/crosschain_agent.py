"""
agents/crosschain_agent.py

Thin BaseAgent wrapper over execution.crosschain_engine.CrossChainArbEngine.

Mirrors ArbAgentWrapper (agents/__init__.py) — the engine owns the scan
loop, breakers, and DB writes; this class only adapts the engine's
get_stats() shape to AgentStats and gates is_available() on connector
count.

OBSERVATION MODE invariant
--------------------------
XCHAIN_CAPITAL == 0.0 by default. In that state every evaluation is
logged to xchain_observations with the full cost breakdown, but no
positions are opened. Once enough observation data confirms persistent
positive net-edge (see database.queries.get_xchain_summary +
prompts/build_crosschain_agent_v2.md "WHEN DONE" criteria), set
XCHAIN_CAPITAL > 0 and supply RPC URLs + funded wallets via keys.env.
Live execution is gated additionally on XCHAIN_LIVE_ENABLED — the
connectors' submit_swap raises NotImplementedError until that path is
implemented in a separate build.

Inventory targets
-----------------
get_inventory_targets() exposes the future BalanceAgent's input contract
(see execution/inventory.py and CAPITAL_REBALANCE_UI.md). Computed live
from the last N observations' chain-level entry frequency — no state is
stored here; recompute on demand.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from config import settings
from database import queries as db_queries

from agents.base import (
    AgentStats, BaseAgent,
    RUNNING, HALTED, OFFLINE, STOPPED,
)
from execution.crosschain_engine import CrossChainArbEngine
from execution.inventory import InventoryTarget, compute_inventory_targets

logger = logging.getLogger(__name__)


class CrossChainArbAgent(BaseAgent):
    agent_id     = "xchain"
    display_name = "Cross-Chain Arb"
    optional     = True

    def __init__(self):
        super().__init__()
        # capital_allocation tracks the fund pool (always shown on the
        # dashboard); the engine's *trading* budget is settings.XCHAIN_CAPITAL,
        # which is the toggle between observation (0) and execution (>0).
        # Same separation as ScalpingAgent.FUND_MEXC_SCALP_CAPITAL vs
        # SCALP_CAPITAL — observation mode is preserved regardless of fund size.
        self.capital_allocation = float(settings.XCHAIN_CAPITAL)
        self._engine: Optional[CrossChainArbEngine] = None
        self._dashboard = None

    # ── Availability ────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """True iff >=2 chain connectors are available.

        Each connector self-gates on (web3 importable AND RPC env var set
        AND pool addresses pinned), so this reduces to "have we wired up
        at least two chains?". Mirrors ArbAgentWrapper's >=2-exchange
        gate. No web3 / no RPC URLs / no pinned pools → agent stays
        OFFLINE silently.
        """
        try:
            from execution.chains import REGISTERED_CONNECTORS
        except ImportError:
            return False
        return sum(1 for c in REGISTERED_CONNECTORS if c.is_available()) >= 2

    @property
    def observation_mode(self) -> bool:
        """True when XCHAIN_CAPITAL == 0 — the agent logs evaluations to
        xchain_observations but never opens positions. Mirrors the
        FundingArbAgent.observation_mode pattern so the web UI can
        consistently render an OBS badge across agents."""
        return float(getattr(settings, "XCHAIN_CAPITAL", 0.0) or 0.0) <= 0.0

    def set_dashboard(self, dashboard) -> None:
        """Late-bind dashboard. Engine doesn't currently consume it, but
        the setter is here to keep the agent API consistent with arb /
        scalp wrappers — same shape, no surprises."""
        self._dashboard = dashboard

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.is_available():
            self._status = OFFLINE
            logger.info(
                "CrossChainArbAgent: <2 connectors available — staying OFFLINE",
            )
            return
        self._engine = CrossChainArbEngine()
        # Mirror any halt state set before the engine existed (Web UI v3.1).
        try:
            self._engine._manually_halted = bool(self._manually_halted)
        except Exception:
            pass
        self._status = RUNNING
        self._start_time = time.time()
        logger.info(
            "CrossChainArbAgent: starting engine (%s mode, capital=$%.0f)",
            "OBSERVATION" if settings.XCHAIN_CAPITAL == 0 else "EXECUTION-PENDING",
            settings.XCHAIN_CAPITAL,
        )
        await self._engine.start()       # runs forever until stop()

    async def stop(self) -> None:
        if self._engine is not None:
            try:
                await self._engine.stop()
            except Exception as e:
                logger.warning(f"CrossChainArbAgent stop: {e}")
        self._status = STOPPED

    async def close_all_positions(self) -> None:
        """No-op in observation mode (no positions to close).

        Still calls the engine's close_all_positions to log the kill
        event — same shape as ArbAgent.close_all_positions wrapping
        ArbEngine.close_all_positions.
        """
        if self._engine is not None:
            try:
                await self._engine.close_all_positions()
            except Exception as e:
                logger.error(f"CrossChainArbAgent close_all: {e}")

    # ── Operator-initiated halt (Web UI v3.1) ──────────────────────────
    # Engine owns the scan loop; propagate the flag onto it so
    # _scan_loop skips evaluation while halted. Observation mode has
    # no positions to manage, so halt affects evaluation only.

    def halt_manual(self) -> bool:
        state = super().halt_manual()
        if self._engine is not None:
            try:
                self._engine._manually_halted = True
            except Exception as e:
                logger.debug("CrossChainArbAgent halt_manual propagate: %s", e)
        return state

    def resume_manual(self) -> bool:
        # super().resume_manual() also invokes clear_circuit_breakers()
        # below, which wipes engine.cb so the next _cb_triggered() pass
        # returns False even if the underlying counters were tripped.
        state = super().resume_manual()
        if self._engine is not None:
            try:
                self._engine._manually_halted = False
            except Exception as e:
                logger.debug("CrossChainArbAgent resume_manual propagate: %s", e)
        return state

    def _propagate_entries_blocked(self, blocked: bool) -> None:
        # Mirror the coordinator's exposure gate into the cross-chain engine.
        if self._engine is not None:
            self._engine._entries_blocked = bool(blocked)

    def clear_circuit_breakers(self) -> None:
        """Resume operator-override: zero engine.cb so the next scan tick
        evaluates clean. Status flips back to RUNNING so the dashboard
        stops showing HALTED."""
        if self._engine is None:
            return
        try:
            from execution.crosschain_engine import STATUS_RUNNING
            cb = getattr(self._engine, "cb", None)
            if cb is not None:
                cb.halted              = False
                cb.halt_reason         = ""
                cb.daily_pnl_usd       = 0.0
                cb.consecutive_losses  = 0
            if getattr(self._engine, "_status", None) == "HALTED":
                self._engine._status = STATUS_RUNNING
        except Exception as e:
            logger.debug("CrossChainArbAgent clear_circuit_breakers: %s", e)

    # ── Stats ───────────────────────────────────────────────────────────

    async def get_stats(self) -> AgentStats:
        """Snapshot for the dashboard. NEVER raises — caught fall-through
        returns OFFLINE / zeroed stats with the error string set.

        In observation mode (XCHAIN_CAPITAL == 0) capital_allocated is 0,
        so the dashboard's render layer can use the same "OBSERVATION"
        chip it uses for ScalpingAgent. daily_pnl stays at 0 because no
        positions are opened in observation mode; the engine still tracks
        the field so the live build wires straight through.
        """
        if self._engine is None:
            return self._zero_stats(status=OFFLINE)

        try:
            es = self._engine.get_stats()
        except Exception as e:
            logger.debug("CrossChainArbAgent get_stats: %s", e)
            return self._zero_stats(status=OFFLINE, error=str(e))

        daily_pnl = float(es.get("daily_pnl", 0.0))
        daily_pnl_pct = (daily_pnl / self.capital_allocation * 100.0) \
            if self.capital_allocation else 0.0

        status = es.get("status", OFFLINE)
        if es.get("halted"):
            status = HALTED

        return AgentStats(
            agent_id=self.agent_id,
            status=status,
            capital_allocated=self.capital_allocation,
            capital_deployed=0.0,           # observation mode = nothing deployed
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            total_pnl=0.0,
            trades_today=int(es.get("would_entries_today", 0)),
            win_rate_today=0.0,
            win_rate_alltime=0.0,
            consecutive_losses=int(es.get("consecutive_losses", 0)),
            last_trade_time=es.get("last_scan_time"),
            error=None,
        )

    def _zero_stats(self, *, status: str, error: Optional[str] = None) -> AgentStats:
        return AgentStats(
            agent_id=self.agent_id,
            status=status,
            capital_allocated=self.capital_allocation,
            capital_deployed=0.0,
            daily_pnl=0.0, daily_pnl_pct=0.0,
            total_pnl=0.0,
            trades_today=0,
            win_rate_today=0.0,
            win_rate_alltime=0.0,
            consecutive_losses=0,
            last_trade_time=None,
            error=error,
        )

    # ── Inventory targets (BalanceAgent interface) ──────────────────────

    def get_inventory_targets(
        self,
        current_balances: Optional[dict[str, dict[str, float]]] = None,
        *,
        lookback_observations: int = 500,
    ) -> list[InventoryTarget]:
        """Publish the per-chain target USDC/WETH split for the BalanceAgent.

        Reads the last `lookback_observations` rows from xchain_observations
        and weights chains by their observed would_entry frequency, then
        calls execution.inventory.compute_inventory_targets. Returns an
        empty list if the DB is unreachable (never raises — kill-switch
        + dashboard paths must not be coupled to DB health).

        `current_balances` is duck-typed:
        ``{connector_id: {"base_usd": float, "quote_usd": float}}``. Pass
        what the BalanceAgent / wallet aggregator reports today; omit it
        for a cold-start view (all-zeros).
        """
        try:
            observations = db_queries.get_xchain_observations(
                limit=lookback_observations,
            )
        except Exception as e:
            logger.debug("get_inventory_targets: get_xchain_observations failed: %s", e)
            observations = []
        return compute_inventory_targets(
            observations=observations,
            current_balances=current_balances,
        )


__all__ = ["CrossChainArbAgent"]
