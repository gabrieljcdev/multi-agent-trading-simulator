"""
agents/funding_arb_agent.py

Funding-rate arb agent (Phase 1 — observation mode only).

Shape mirrors agents/scalping_agent.py: a BaseAgent that runs an async
scan loop at settings.FUNDING_SCAN_INTERVAL_SEC, consults
execution.funding_engine.funding_engine for opportunities, and — while
FUNDING_OBSERVATION_MODE is True — writes a row to
funding_arb_observations per scan tick instead of routing orders.

Phase 1 invariants:
  * NEVER routes an order while FUNDING_OBSERVATION_MODE is True. The
    engine's open() carries a SIM_MODE assertion as defence in depth, but
    the agent loop itself short-circuits before reaching open().
  * No keys required. The engine reads funding via CCXT Binance public
    endpoints; an empty config/keys.env is valid.
  * Single venue (Binance), single variant (delta-neutral), majors only.

Circuit breakers:
  * Daily-loss halt at settings.FUNDING_DAILY_LOSS_HALT_USD.
  * Consecutive-loss halt at settings.FUNDING_CONSECUTIVE_LOSS_HALT.
  Daily counters reset at UTC midnight (same shape as ScalpingAgent).
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
    RUNNING, HALTED, OFFLINE, STOPPED,
)

from execution.funding_engine import (
    funding_engine,
    FundingEngine,
    FundingOpportunity,
    FundingPosition,
)

log = logging.getLogger(__name__)


class FundingArbAgent(BaseAgent):
    agent_id     = "funding_arb"
    display_name = "Funding-Rate Arb"
    optional     = True

    def __init__(self, engine: Optional[FundingEngine] = None):
        super().__init__()
        # Capital is the AGENT-FACING fund allocation. Phase 1 keeps both
        # the fund and the trading budget at FUNDING_CAPITAL_USD (default
        # 0.0) so the BalanceAgent's compounding view stays consistent
        # with observation mode.
        self.capital_allocation = float(settings.FUNDING_CAPITAL_USD)

        # Module-level singleton by default. Tests inject a fresh engine
        # so per-test state (locks, semaphore, ccxt stub) is isolated.
        self._engine: FundingEngine = engine or funding_engine

        # Open simulated positions keyed by symbol — Phase 1 only ever
        # writes/exits in observation mode, so this map is mostly empty;
        # the kill-switch path still iterates it for forward-compat.
        self._positions: dict[str, FundingPosition] = {}

        # Per-agent circuit-breaker state — independent of the portfolio
        # breaker, matches scalping_agent's shape.
        self._daily_loss:      float = 0.0
        self._daily_pnl:       float = 0.0
        self._consec_losses:   int   = 0
        self._halted:          bool  = False
        self._halt_reason:     str   = ""
        self._last_reset_date: date  = datetime.utcnow().date()

        # Lifecycle / loop handle.
        self._running                          = False
        self._task: Optional[asyncio.Task]     = None

        # Observation cache — the dashboard reads this directly for the
        # "live opportunities" strip without hitting the DB on every tick.
        self._last_opps: list[FundingOpportunity] = []

    # ── BaseAgent contract ──────────────────────────────────────────────

    def is_available(self) -> bool:
        # Observation mode runs without keys or capital — the engine reads
        # funding via Binance PUBLIC endpoints. Always available in Phase 1.
        return True

    @property
    def observation_mode(self) -> bool:
        """Whether the agent is currently in observation mode. Settings-driven
        so an operator can flip the flag without restarting the runtime."""
        return bool(settings.FUNDING_OBSERVATION_MODE)

    async def start(self) -> None:
        self._running = True
        self._status = RUNNING
        self._start_time = time.time()
        mode = "OBSERVATION" if self.observation_mode else "LIVE"
        log.info(
            "[FundingArbAgent] starting in %s mode (capital=$%.0f)",
            mode, self.capital_allocation,
        )
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
        # Engine holds a cached binance ccxt client — close it so we don't
        # leak the aiohttp session on shutdown.
        try:
            await self._engine.close()
        except Exception as e:
            log.debug(f"[FundingArbAgent] engine close: {e}")
        self._status = STOPPED
        log.info("[FundingArbAgent] stopped")

    async def close_all_positions(self) -> None:
        """Kill-switch path. Close every open simulated position concurrently.

        Phase 1 observation mode rarely has any open positions, but the
        contract requires honouring kill_switch — close every leg via
        asyncio.gather (mirrors ArbEngine.close_all_positions's parallel
        shape) so a portfolio-wide halt drains this agent atomically.
        """
        if not self._positions:
            return
        coros = [
            self._close(symbol, pos, reason="FORCE_EXIT")
            for symbol, pos in list(self._positions.items())
        ]
        await asyncio.gather(*coros, return_exceptions=True)

    async def get_stats(self) -> AgentStats:
        """Snapshot of agent state. NEVER raises — error → AgentStats(error=…)."""
        try:
            capital_deployed = sum(p.notional_usd for p in self._positions.values())
            daily_pnl_pct = (self._daily_pnl / self.capital_allocation * 100.0) \
                if self.capital_allocation else 0.0
            return AgentStats(
                agent_id=self.agent_id,
                status=HALTED if self._halted else (RUNNING if self._running else OFFLINE),
                capital_allocated=self.capital_allocation,
                capital_deployed=capital_deployed,
                daily_pnl=float(self._daily_pnl),
                daily_pnl_pct=daily_pnl_pct,
                total_pnl=0.0,                            # Phase 1: no realised P&L
                trades_today=len(self._positions),
                win_rate_today=0.0,
                win_rate_alltime=0.0,
                consecutive_losses=int(self._consec_losses),
                last_trade_time=None,
                error=None,
            )
        except Exception as e:
            log.debug(f"[FundingArbAgent] get_stats failed: {e}")
            return AgentStats(
                agent_id=self.agent_id, status=OFFLINE,
                capital_allocated=self.capital_allocation,
                capital_deployed=0.0, daily_pnl=0.0, daily_pnl_pct=0.0,
                total_pnl=0.0, trades_today=0,
                win_rate_today=0.0, win_rate_alltime=0.0,
                consecutive_losses=0, last_trade_time=None, error=str(e),
            )

    def get_observation_summary(self) -> dict:
        """Aggregate observation summary used by the dashboard panel.

        Reads through database.queries.get_funding_summary so it stays
        consistent with the SQL the operator runs by hand. Returns
        zeroed defaults on any DB hiccup — the panel rendering treats
        zeros as "no data yet" rather than an error.
        """
        try:
            return db_queries.get_funding_summary(days=7)
        except Exception as e:
            log.debug(f"[FundingArbAgent] get_observation_summary: {e}")
            return {
                "total": 0, "would_enter": 0, "closed": 0,
                "mean_net_apr_realized": 0.0, "mean_hold_hours": 0.0,
                "exit_reason": {},
            }

    # ── Main loop ───────────────────────────────────────────────────────

    async def _loop(self) -> None:
        interval = max(1.0, float(settings.FUNDING_SCAN_INTERVAL_SEC))
        while self._running:
            try:
                await asyncio.sleep(interval)
                if self._halted:
                    continue
                opps = await self._engine.scan() or []
                self._last_opps = list(opps)
                # Top N opportunities — sorted by APR descending so the
                # best carry wins when concurrency is constrained.
                opps.sort(key=lambda o: o.funding_apr, reverse=True)
                top = opps[: int(settings.FUNDING_MAX_CONCURRENT)]

                for opp in top:
                    if self.observation_mode:
                        await self._log_observation(opp)
                        # ROUTE NO ORDERS. The agent stays in observation
                        # mode until FUNDING_OBSERVATION_MODE is flipped.
                        continue
                    # Phase 1 should never reach this branch — gate enforced
                    # both at the agent level and in engine.open().
                    if opp.symbol in self._positions:
                        continue
                    await self._engine.open(opp)

                await self._manage_open_positions()
                self._check_daily_reset()
                self._check_circuit_breakers()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception(f"[FundingArbAgent] loop error: {e}")

    async def _manage_open_positions(self) -> None:
        """Sweep open positions and exit any that match a reason."""
        for symbol, pos in list(self._positions.items()):
            reason = self._engine.exit_reason(pos)
            if reason is None:
                continue
            await self._close(symbol, pos, reason=reason)

    async def _close(
        self, symbol: str, pos: FundingPosition, reason: str,
    ) -> None:
        """Close one simulated funding-arb position.

        Computes realised net P&L (funding collected minus fees + slippage),
        logs the close-time observation row, updates the consecutive-loss
        and daily-loss counters, and removes the position from _positions.
        Phase 1 observation mode never opens here, so this is a Phase-2
        scaffold — but the bookkeeping mirror it exactly so behaviour is
        stable across phases.
        """
        try:
            now_ts = time.time()
            hold_sec = max(0.0, now_ts - pos.opened_at)
            gross    = float(pos.funding_collected)
            fees     = float(pos.fees_paid)
            net      = gross - fees

            row = {
                "timestamp":   pos.opened_at,
                "symbol":      pos.opp.symbol,
                "variant":     pos.opp.variant,
                "venue_long":  pos.opp.venue_long,
                "venue_short": pos.opp.venue_short,
                "funding_apr": pos.opp.funding_apr,
                "spread_apr":  pos.opp.spread_apr,
                "oi_usd":      pos.opp.oi_usd,
                "depth_ok":    pos.opp.depth_ok,
                "notional_usd": pos.notional_usd,
                "margin_used":  pos.margin_used,
                "basis_at_entry": pos.basis_at_entry,
                "would_enter":  True,
                "exit_time":    now_ts,
                "exit_reason":  reason,
                "hold_sec":     hold_sec,
                "funding_collected": gross,
                "fees_paid":         fees,
                "pnl_usd":      net,
                "observation_only": self.observation_mode,
            }
            await asyncio.to_thread(db_queries.save_funding_observations, [row])

            self._daily_pnl += net
            if net < 0:
                self._daily_loss += -net
                self._consec_losses += 1
            else:
                self._consec_losses = 0
        finally:
            self._positions.pop(symbol, None)

    async def _log_observation(self, opp: FundingOpportunity) -> None:
        """Persist a would-enter observation row without touching any router.

        Simulated economics:
          * notional   = min(FUNDING_MAX_NOTIONAL_USD, oi_usd * MAX_OI_FRACTION).
                         When oi_usd is 0 (no OI from CCXT) we fall back to
                         FUNDING_MAX_NOTIONAL_USD outright.
          * funding/interval = notional × rate_8h × (interval / 8h).
                         Derived back from funding_apr ÷ 1095 so the SQL view
                         stays self-consistent with what the agent observed.
          * fees       = 2 × notional × FUNDING_SIM_SLIPPAGE_PCT (entry + exit).
                         Phase 1 models slippage as the fee proxy — there's no
                         CCXT taker-fee per leg here yet.
          * net_apr    = funding_apr − (fees / notional × annualisation).
        """
        notional_target = float(settings.FUNDING_MAX_NOTIONAL_USD)
        if opp.oi_usd > 0:
            notional = min(notional_target,
                           opp.oi_usd * float(settings.FUNDING_MAX_OI_FRACTION))
        else:
            notional = notional_target

        rate_8h         = opp.funding_apr / 1095.0
        interval_sec    = max(1.0, float(settings.FUNDING_SCAN_INTERVAL_SEC))
        intervals_per_8h = (8.0 * 3600.0) / interval_sec
        per_interval    = notional * rate_8h / max(intervals_per_8h, 1e-9)

        slip = float(settings.FUNDING_SIM_SLIPPAGE_PCT)
        # Round-trip = entry + exit. Pure slippage stand-in until Phase 2
        # wires per-leg taker fees off CCXT's fee_for() API.
        projected_fees      = 2.0 * notional * slip
        # Net APR pulls the fee drag out of funding_apr, annualised across
        # one assumed hold of FUNDING_MAX_HOLD_SEC (the worst-case carry).
        max_hold_sec = max(1.0, float(settings.FUNDING_MAX_HOLD_SEC))
        fee_drag_apr = (projected_fees / notional) * (365.0 * 86400.0 / max_hold_sec)
        projected_net_apr = opp.funding_apr - fee_drag_apr

        skip_reason = "" if opp.depth_ok else "depth_below_oi_mult"

        row = {
            "timestamp":    time.time(),
            "symbol":       opp.symbol,
            "variant":      opp.variant,
            "venue_long":   opp.venue_long,
            "venue_short":  opp.venue_short,
            "funding_apr":  opp.funding_apr,
            "spread_apr":   opp.spread_apr,
            "oi_usd":       opp.oi_usd,
            "depth_ok":     bool(opp.depth_ok),
            "notional_usd": notional,
            "margin_used":  notional / max(float(settings.FUNDING_TARGET_LEVERAGE), 1e-9),
            "basis_at_entry": 0.0,
            "projected_funding_per_interval": per_interval,
            "projected_fees":                 projected_fees,
            "projected_net_apr":              projected_net_apr,
            "would_enter":  bool(opp.depth_ok),
            "skip_reason":  skip_reason,
            "observation_only": True,
        }
        try:
            await asyncio.to_thread(db_queries.save_funding_observations, [row])
        except Exception as e:
            log.debug(f"[FundingArbAgent] save observation failed: {e}")

    # ── Lifecycle housekeeping ──────────────────────────────────────────

    def _check_daily_reset(self) -> None:
        """Reset daily counters at UTC midnight rollover."""
        today = datetime.utcnow().date()
        if today != self._last_reset_date:
            self._daily_loss      = 0.0
            self._daily_pnl       = 0.0
            self._consec_losses   = 0
            # Don't auto-clear _halted — the operator clears that explicitly.
            self._last_reset_date = today
            log.info("[FundingArbAgent] daily counters reset at UTC midnight")

    def _check_circuit_breakers(self) -> None:
        """Halt the loop if either circuit breaker trips.

        We compare daily_loss (cumulative loss USD) against the configured
        cap, and consec_losses against the configured run. Once halted the
        loop continues to tick (so the dashboard keeps reading get_stats)
        but the scan is short-circuited — same shape as ScalpingAgent.
        """
        if self._daily_loss >= float(settings.FUNDING_DAILY_LOSS_HALT_USD):
            if not self._halted:
                self._halted = True
                self._halt_reason = "daily_loss"
                log.warning("[FundingArbAgent] HALTED — daily-loss cap reached")
            return
        if self._consec_losses >= int(settings.FUNDING_CONSECUTIVE_LOSS_HALT):
            if not self._halted:
                self._halted = True
                self._halt_reason = "consecutive_loss"
                log.warning("[FundingArbAgent] HALTED — consecutive-loss cap reached")
