"""
agents/coordinator.py

Multi-agent coordinator. Agent-agnostic by design — only knows the
BaseAgent contract from agents.base, never imports concrete agents.
Concrete wrappers live in agents/__init__.py alongside REGISTERED_AGENTS.

Responsibilities:
  - Start every available agent concurrently
  - Aggregate portfolio-wide stats
  - Run portfolio-level circuit breakers (additive on top of per-agent CBs)
  - Coordinate a system-wide kill switch
  - Persist portfolio snapshots and agent events to the DB
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from config import settings
from database import queries as db_queries

from agents.base import (
    AgentStats, BaseAgent,
    RUNNING, PAUSED, HALTED, OFFLINE, STOPPED,
)

logger = logging.getLogger(__name__)


class Coordinator:
    """Orchestrates a roster of BaseAgent instances."""

    def __init__(
        self,
        agents:    Optional[list[BaseAgent]] = None,
        dashboard=None,
    ):
        # Default to the project's registered agents. Tests inject their
        # own list — REGISTERED_AGENTS is only touched on demand so importing
        # Coordinator alone never instantiates concrete agents.
        if agents is None:
            from agents import REGISTERED_AGENTS
            agents = REGISTERED_AGENTS
        self._agents: list[BaseAgent] = list(agents)

        self._dashboard = dashboard
        self._running = False
        self._halted_by_portfolio_cb = False

        self._check_capital_sum()

    # ── Public API ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start every available agent concurrently + the monitor loop."""
        self._running = True
        await self._propagate_dashboard()

        starts = []
        for agent in self._agents:
            if not agent.is_available():
                logger.info(f"Skipping unavailable agent: {agent.display_name}")
                self._log_event(agent.agent_id, "SKIPPED",
                                "is_available returned False")
                continue
            logger.info(f"Starting agent: {agent.display_name}")
            starts.append(self._safe_call(agent, "start"))

        # Monitor loop runs alongside; gather returns when all coros end
        # (most agent.start()s never return — they drive their own loops).
        await asyncio.gather(
            self._monitor_loop(),
            *starts,
            return_exceptions=True,
        )

    async def stop(self) -> None:
        """Graceful shutdown of every agent."""
        self._running = False
        await asyncio.gather(
            *(self._safe_call(a, "stop") for a in self._agents),
            return_exceptions=True,
        )
        for a in self._agents:
            try:
                s = await a.get_stats()
                logger.info(f"final stats: {a.agent_id} status={s.status} "
                            f"daily_pnl=${s.daily_pnl:.2f} trades={s.trades_today}")
            except Exception:
                pass

    async def kill_all(self, reason: str = "manual") -> dict:
        """EMERGENCY — close every position across every agent in parallel.

        Bypasses approval, profile, and per-agent checks. Returns a summary
        dict for the dashboard. Errors per agent are caught and logged but
        never re-raised — one stuck agent must not block the rest.
        """
        logger.critical(f"KILL ALL invoked — {reason}")
        results = await asyncio.gather(
            *(self._safe_close(a) for a in self._agents),
            return_exceptions=True,
        )
        n_ok = sum(1 for r in results if r is True)
        n_err = sum(1 for r in results if r is not True)

        self._log_event("portfolio", "KILLED",
                        f"reason={reason} agents_ok={n_ok} agents_err={n_err}")

        if self._dashboard is not None:
            try:
                self._dashboard.add_log(f"KILL ALL: {reason}", level="CRITICAL")
            except Exception:
                pass

        return {
            "reason":    reason,
            "agents_ok": n_ok,
            "agents_err": n_err,
            "timestamp": datetime.utcnow().isoformat(),
        }

    async def get_agent_stats(self) -> list[AgentStats]:
        """Pull get_stats() from every agent concurrently."""
        results = await asyncio.gather(
            *(self._safe_get_stats(a) for a in self._agents),
            return_exceptions=True,
        )
        stats: list[AgentStats] = []
        for r in results:
            if isinstance(r, AgentStats):
                stats.append(r)
        # Sorted by capital descending — biggest allocators first
        return sorted(stats, key=lambda s: s.capital_allocated, reverse=True)

    async def get_portfolio_stats(self) -> dict:
        """Aggregate AgentStats across the roster."""
        agent_stats = await self.get_agent_stats()

        total_allocated = sum(s.capital_allocated for s in agent_stats)
        total_deployed  = sum(s.capital_deployed  for s in agent_stats)
        total_daily_pnl = sum(s.daily_pnl         for s in agent_stats)
        total_trades    = sum(s.trades_today      for s in agent_stats)

        if total_allocated > 0:
            total_daily_pnl_pct = total_daily_pnl / total_allocated * 100
            total_exposure_pct  = total_deployed  / total_allocated * 100
        else:
            total_daily_pnl_pct = 0.0
            total_exposure_pct  = 0.0

        # Weighted-by-trade-count win rate
        wr_num = 0.0
        wr_den = 0.0
        for s in agent_stats:
            if s.trades_today > 0:
                wr_num += s.win_rate_today * s.trades_today
                wr_den += s.trades_today
        overall_wr_today = wr_num / wr_den if wr_den > 0 else 0.0

        running = sum(1 for s in agent_stats if s.status == RUNNING)
        halted  = sum(1 for s in agent_stats if s.status == HALTED)

        if self._halted_by_portfolio_cb or total_daily_pnl_pct <= -settings.PORTFOLIO_DAILY_LOSS_HALT_PCT:
            portfolio_status = "HALTED"
        elif halted > 0 or total_exposure_pct >= settings.PORTFOLIO_MAX_EXPOSURE_PCT:
            portfolio_status = "WARNING"
        else:
            portfolio_status = "HEALTHY"

        # total_equity = allocated + realized daily P&L
        total_equity = total_allocated + total_daily_pnl

        return {
            "total_equity":           total_equity,
            "total_daily_pnl":        total_daily_pnl,
            "total_daily_pnl_pct":    total_daily_pnl_pct,
            "total_exposure_pct":     total_exposure_pct,
            "total_trades_today":     total_trades,
            "overall_win_rate_today": overall_wr_today,
            "agents_running":         running,
            "agents_halted":          halted,
            "portfolio_status":       portfolio_status,
        }

    def get_agent(self, agent_id: str) -> Optional[BaseAgent]:
        """Look up an agent by id. None if not registered."""
        for a in self._agents:
            if a.agent_id == agent_id:
                return a
        return None

    def get_primary_bot(self):
        """Convenience for the dashboard: signal agent's CryptoBot if it
        exposes one, else None. Bot may not exist yet if signal_agent
        hasn't started — caller must handle None."""
        signal = self.get_agent("signal")
        return getattr(signal, "bot", None) if signal else None

    def set_dashboard(self, dashboard) -> None:
        """Late-bind the dashboard and propagate to every agent."""
        self._dashboard = dashboard
        # Best-effort propagation; agents that don't care will simply not
        # have a set_dashboard method.
        for a in self._agents:
            setter = getattr(a, "set_dashboard", None)
            if callable(setter):
                try:
                    setter(dashboard)
                except Exception as e:
                    logger.debug(f"agent {a.agent_id} set_dashboard failed: {e}")

    # ── Portfolio monitor loop ──────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        while self._running:
            await asyncio.sleep(settings.PORTFOLIO_MONITOR_INTERVAL_SEC)
            try:
                stats = await self.get_portfolio_stats()
                self._log_snapshot(stats)
                await self._check_portfolio_circuit_breakers(stats)
            except Exception as e:
                logger.error(f"portfolio monitor: {e}", exc_info=True)

    async def _check_portfolio_circuit_breakers(self, stats: dict) -> None:
        """Daily-loss + exposure CBs that sit above per-agent CBs."""
        loss_pct = stats.get("total_daily_pnl_pct", 0.0)
        if loss_pct <= -settings.PORTFOLIO_DAILY_LOSS_HALT_PCT:
            if not self._halted_by_portfolio_cb:
                logger.critical(
                    f"PORTFOLIO HALT — daily loss {loss_pct:.2f}% "
                    f"exceeds {settings.PORTFOLIO_DAILY_LOSS_HALT_PCT}%"
                )
                self._halted_by_portfolio_cb = True
                self._log_event("portfolio", "HALTED",
                                f"daily_loss {loss_pct:.2f}%")
                # Pause every agent (additive on top of per-agent CBs)
                await asyncio.gather(
                    *(self._safe_pause(a) for a in self._agents),
                    return_exceptions=True,
                )

        # Exposure CB is a soft block; per-agent entry gating isn't
        # universal yet (CryptoBot doesn't check it). Log it and bump
        # status to WARNING via get_portfolio_stats. The block_new_entries
        # enforcement is a TODO once agents grow that hook.
        exp = stats.get("total_exposure_pct", 0.0)
        if exp >= settings.PORTFOLIO_MAX_EXPOSURE_PCT:
            logger.warning(
                f"Portfolio exposure {exp:.1f}% >= "
                f"{settings.PORTFOLIO_MAX_EXPOSURE_PCT}% — "
                "block_new_entries enforcement not yet wired into agents"
            )

    # ── Safe wrappers — one bad agent must not crash the coordinator ───

    async def _safe_call(self, agent: BaseAgent, method_name: str) -> bool:
        try:
            await getattr(agent, method_name)()
            if method_name == "start":
                self._log_event(agent.agent_id, "STARTED", "")
            elif method_name == "stop":
                self._log_event(agent.agent_id, "STOPPED", "")
            return True
        except Exception as e:
            logger.error(f"agent {agent.agent_id} {method_name} failed: {e}",
                         exc_info=True)
            self._log_event(agent.agent_id, "ERROR", f"{method_name}: {e}")
            return False

    async def _safe_close(self, agent: BaseAgent) -> bool:
        try:
            await agent.close_all_positions()
            return True
        except Exception as e:
            logger.error(f"agent {agent.agent_id} close_all_positions: {e}")
            self._log_event(agent.agent_id, "ERROR",
                            f"close_all_positions: {e}")
            return False

    async def _safe_pause(self, agent: BaseAgent) -> bool:
        try:
            await agent.pause()
            return True
        except Exception as e:
            logger.error(f"agent {agent.agent_id} pause: {e}")
            return False

    async def _safe_get_stats(self, agent: BaseAgent) -> AgentStats:
        try:
            return await agent.get_stats()
        except Exception as e:
            logger.error(f"agent {agent.agent_id} get_stats: {e}")
            return AgentStats(
                agent_id=agent.agent_id,
                status=OFFLINE,
                capital_allocated=agent.capital_allocation,
                capital_deployed=0.0,
                daily_pnl=0.0, daily_pnl_pct=0.0,
                total_pnl=0.0, trades_today=0,
                win_rate_today=0.0, win_rate_alltime=0.0,
                consecutive_losses=0, last_trade_time=None,
                error=str(e),
            )

    # ── Internal helpers ────────────────────────────────────────────────

    def _check_capital_sum(self) -> None:
        total_allocated = sum(a.capital_allocation for a in self._agents)
        configured = sum(settings.EXCHANGE_BALANCES.values())
        if configured > 0 and total_allocated > configured:
            logger.warning(
                f"Agent capital sum ${total_allocated:,.0f} exceeds total "
                f"configured capital ${configured:,.0f} — "
                "agents may not have funds to meet their allocations"
            )

    def _log_event(self, agent_id: str, event_type: str, detail: str) -> None:
        try:
            db_queries.log_agent_event(agent_id, event_type, detail)
        except Exception as e:
            logger.debug(f"log_agent_event: {e}")

    def _log_snapshot(self, stats: dict) -> None:
        try:
            db_queries.log_portfolio_snapshot(stats)
        except Exception as e:
            logger.debug(f"log_portfolio_snapshot: {e}")

    async def _propagate_dashboard(self) -> None:
        if self._dashboard is None:
            return
        for a in self._agents:
            setter = getattr(a, "set_dashboard", None)
            if callable(setter):
                try:
                    setter(self._dashboard)
                except Exception as e:
                    logger.debug(f"set_dashboard {a.agent_id}: {e}")
