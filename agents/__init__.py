"""
agents/__init__.py

Registry of trading agents the Coordinator manages by default.

═══════════════════════════════════════════════════════════════════════
To add a new agent
═══════════════════════════════════════════════════════════════════════

1. Create agents/my_agent.py
2. Subclass BaseAgent (from agents.base)
3. Set the class attrs:
       agent_id           = "my_agent"
       display_name       = "My Agent"
       capital_allocation = settings.MY_AGENT_CAPITAL
       optional           = True / False
4. Implement: async start(), stop(), get_stats(), close_all_positions()
       and is_available() if it needs deps/keys
5. Append an instance of your class to REGISTERED_AGENTS below
6. Add MY_AGENT_CAPITAL to config/settings.py

The Coordinator, dashboard, and DB logging pick it up automatically — no
coordinator changes needed. Mirrors the sentiment-source plugin pattern.

Concrete wrappers (SignalAgentWrapper, ArbAgentWrapper, the three
placeholders) live in this module rather than the Coordinator, so the
Coordinator stays agent-agnostic and only depends on the BaseAgent ABC.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from config import settings
from database import queries as db_queries

from agents.base import (
    AgentStats, BaseAgent, PlaceholderAgent,
    RUNNING, PAUSED, HALTED, OFFLINE, STOPPED,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# SignalAgentWrapper — wraps the existing CryptoBot
# ─────────────────────────────────────────────────────────────────────────

class SignalAgentWrapper(BaseAgent):
    agent_id     = "signal"
    display_name = "Signal Agent"
    optional     = False   # the project's core agent — coordinator warns if missing

    def __init__(self):
        super().__init__()
        self.capital_allocation = settings.SIGNAL_AGENT_CAPITAL
        self._bot = None                  # CryptoBot, created lazily in start()
        self._dashboard = None

    # ── Expose the bot for dashboard wiring ────────────────────────────

    @property
    def bot(self):
        return self._bot

    def set_dashboard(self, dashboard) -> None:
        """Late-bind dashboard. If bot already exists, push it into
        market_data so health updates flow."""
        self._dashboard = dashboard
        if self._bot is not None:
            md = getattr(self._bot, "_market_data", None)
            if md is not None and hasattr(md, "set_dashboard"):
                md.set_dashboard(dashboard)
            if hasattr(dashboard, "set_bot"):
                dashboard.set_bot(self._bot)

    # ── Availability ────────────────────────────────────────────────────

    def is_available(self) -> bool:
        # In sim mode we can run without exchange keys.
        if settings.SIM_MODE:
            return True
        # Live mode — need at least one configured exchange key.
        for ex in settings.ENABLED_EXCHANGES:
            if os.getenv(f"{ex.upper()}_API_KEY"):
                return True
        return False

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        import time as _time
        from core.bot import CryptoBot
        from execution.kill_switch import KillSwitch

        kill_switch = KillSwitch(sim_mode=settings.SIM_MODE)
        self._bot = CryptoBot(kill_switch=kill_switch)
        # Late-bind dashboard now that the bot's market_data exists
        if self._dashboard is not None:
            self.set_dashboard(self._dashboard)

        self._status = RUNNING
        self._start_time = _time.time()
        logger.info("SignalAgent: starting CryptoBot")
        await self._bot.start()   # this typically never returns

    async def stop(self) -> None:
        if self._bot is not None:
            try:
                await self._bot.stop()
            except Exception as e:
                logger.warning(f"SignalAgent stop: {e}")
        self._status = STOPPED

    async def close_all_positions(self) -> None:
        if self._bot is not None:
            try:
                await self._bot.trigger_kill_switch(reason="coordinator")
            except Exception as e:
                logger.error(f"SignalAgent kill: {e}")

    # ── BalanceAgent compounding hooks ──────────────────────────────────

    def get_open_position_notional(self) -> float:
        """USD notional of the signal fund's open positions.

        Reads non-scalp open trades (scalp belongs to its own fund) and
        sums their size_usd. Defensive: never raises."""
        try:
            return float(sum(
                (t.size_usd or 0.0) for t in db_queries.get_open_trades()
                if (t.strategy or "") != "scalp"
            ))
        except Exception:
            return 0.0

    def set_capital_allocation(self, amount: float) -> bool:
        """Update the deployable pool. Propagates to the OrderRouter's
        _portfolio_value via the bot so position sizing scales as the
        fund compounds. Refuses if amount < open-position notional."""
        ok = super().set_capital_allocation(amount)
        if not ok:
            return False
        # Propagate to OrderRouter — sizing reads _portfolio_value.
        if self._bot is not None:
            router = getattr(self._bot, "_router", None)
            if router is None:
                router = getattr(self._bot, "_order_router", None)
            if router is not None:
                update = getattr(router, "update_portfolio_value", None)
                if callable(update):
                    try:
                        update(float(amount))
                    except Exception as e:
                        logger.debug("SignalAgent update_portfolio_value: %s", e)
        return True

    async def get_stats(self) -> AgentStats:
        # Bot not started yet → OFFLINE with zeroed numbers.
        if self._bot is None:
            return AgentStats(
                agent_id=self.agent_id, status=OFFLINE,
                capital_allocated=self.capital_allocation,
                capital_deployed=0.0, daily_pnl=0.0, daily_pnl_pct=0.0,
                total_pnl=0.0, trades_today=0,
                win_rate_today=0.0, win_rate_alltime=0.0,
                consecutive_losses=0, last_trade_time=None, error=None,
            )

        cb = getattr(self._bot, "_cb_state", None)
        consec        = getattr(cb, "consecutive_losses", 0) if cb else 0
        halted        = bool(getattr(cb, "halted", False))

        # Pull DB-derived figures, but never let a DB hiccup poison get_stats.
        # Scalp fills live in the shared trades table but belong to the
        # MEXC-scalp fund — exclude strategy="scalp" so they don't pollute
        # the signal fund's trade count, win rate, or deployed capital.
        #
        # P&L is read from the persisted ledger rather than the in-memory
        # _cb_state counters (which reseed to STARTING_CAPITAL every launch),
        # so a restart resumes from the fund's accumulated figure: daily_pnl =
        # today's realised, total_pnl = all-time realised.
        try:
            today  = [t for t in db_queries.get_today_trades()
                      if (t.strategy or "") != "scalp"]
            wr_t   = db_queries.get_signal_win_rate(days=1,   exclude_strategy="scalp")
            wr_all = db_queries.get_signal_win_rate(days=365, exclude_strategy="scalp")
            open_t = [t for t in db_queries.get_open_trades()
                      if (t.strategy or "") != "scalp"]
            daily_pnl_usd = db_queries.get_trade_realized_pnl(
                exclude_strategy="scalp", today=True)
            total_pnl_usd = db_queries.get_trade_realized_pnl(exclude_strategy="scalp")
        except Exception:
            today, wr_t, wr_all, open_t = [], {"total": 0}, {"total": 0}, []
            # DB unreachable → fall back to the in-memory daily figure.
            dpp = getattr(cb, "daily_pnl_pct", 0.0) if cb else 0.0
            daily_pnl_usd = dpp / 100.0 * self.capital_allocation
            total_pnl_usd = 0.0

        daily_pnl_pct = (daily_pnl_usd / self.capital_allocation * 100.0) \
            if self.capital_allocation else 0.0

        trades_today = len(today)
        capital_deployed = sum((t.size_usd or 0.0) for t in open_t)
        last_trade_time = None
        if today:
            ts = today[0].timestamp_open
            last_trade_time = ts.isoformat() if ts else None

        status = HALTED if halted else (self._status if self._status != OFFLINE else RUNNING)

        return AgentStats(
            agent_id=self.agent_id,
            status=status,
            capital_allocated=self.capital_allocation,
            capital_deployed=capital_deployed,
            daily_pnl=daily_pnl_usd,
            daily_pnl_pct=daily_pnl_pct,
            total_pnl=total_pnl_usd,
            trades_today=trades_today,
            win_rate_today=wr_t.get("win_rate", 0.0) if wr_t.get("total") else 0.0,
            win_rate_alltime=wr_all.get("win_rate", 0.0) if wr_all.get("total") else 0.0,
            consecutive_losses=consec,
            last_trade_time=last_trade_time,
            error=None,
        )


# ─────────────────────────────────────────────────────────────────────────
# ArbAgentWrapper — wraps execution.arb_engine when that module exists
# ─────────────────────────────────────────────────────────────────────────

class ArbAgentWrapper(BaseAgent):
    agent_id     = "arb"
    display_name = "Arb Agent"
    optional     = True

    def __init__(self):
        super().__init__()
        self.capital_allocation = settings.ARB_AGENT_CAPITAL
        self._engine = None
        self._dashboard = None

    def is_available(self) -> bool:
        """ArbEngine importable AND ≥2 exchanges usable.

        Sim mode counts any exchange in ARB_FEE_MAP that ccxt knows about
        (public endpoints don't need keys). Live mode requires API key + secret.
        """
        try:
            from execution.arb_engine import ArbEngine  # noqa: F401
        except ImportError:
            return False
        if settings.SIM_MODE:
            return len(settings.ARB_FEE_MAP) >= 2
        keyed = sum(
            1 for ex in settings.ARB_FEE_MAP
            if os.getenv(f"{ex.upper()}_API_KEY") and os.getenv(f"{ex.upper()}_SECRET")
        )
        return keyed >= 2

    def set_dashboard(self, dashboard) -> None:
        self._dashboard = dashboard
        if self._engine is not None:
            self._engine.dashboard = dashboard

    async def start(self) -> None:
        if not self.is_available():
            self._status = OFFLINE
            return
        from execution.arb_engine import ArbEngine
        # Single arb fund across every approved arb venue — MEXC included
        # (STRATEGY_EXCHANGE_MAP["arb"]). MEXC arb trades count toward this
        # fund's P&L; there is no separate MEXC-arb fund.
        self._engine = ArbEngine(
            dashboard=self._dashboard,
            fund_id="arb",
            exchanges=list(settings.STRATEGY_EXCHANGE_MAP.get("arb", [])),
        )
        self._reconstruct_engine_pnl(self._engine)
        import time as _time
        self._status = RUNNING
        self._start_time = _time.time()
        logger.info("ArbAgent: starting ArbEngine (arb fund, incl. MEXC)")
        await self._engine.start()      # runs forever until stop()

    @staticmethod
    def _reconstruct_engine_pnl(engine) -> None:
        """Seed the engine's in-memory P&L counters from the persisted arb
        ledger so a restart resumes from accumulated P&L rather than zero."""
        try:
            engine._total_pnl_usd = db_queries.get_arb_realized_pnl()
            engine._daily_pnl_usd = db_queries.get_arb_realized_pnl(today=True)
        except Exception as e:
            logger.debug(f"arb P&L reconstruction skipped: {e}")

    async def stop(self) -> None:
        if self._engine is not None:
            try:
                await self._engine.stop()
            except Exception as e:
                logger.warning(f"ArbAgent stop: {e}")
        self._status = STOPPED

    async def close_all_positions(self) -> None:
        if self._engine is not None:
            try:
                await self._engine.close_all_positions()
            except Exception as e:
                logger.error(f"ArbAgent close_all: {e}")

    # ── BalanceAgent compounding hooks ──────────────────────────────────

    def get_open_position_notional(self) -> float:
        """Arb positions complete in milliseconds — there's never a
        material open notional. We approximate as engine's in-flight
        arbs × base position so the floor is non-zero while a trade is
        live (the floor still rejects shrinking past zero)."""
        if self._engine is None:
            return 0.0
        active = int(getattr(self._engine, "_active_arbs", 0) or 0)
        return active * float(settings.ARB_BASE_POSITION_USD)

    def set_capital_allocation(self, amount: float) -> bool:
        """Update the engine's deployable allocation so position sizing
        scales with realised P&L. Refuses below open-position notional."""
        ok = super().set_capital_allocation(amount)
        if not ok:
            return False
        if self._engine is not None:
            try:
                self._engine._capital_allocation = float(amount)
            except Exception as e:
                logger.debug("ArbAgent _capital_allocation propagation: %s", e)
        return True

    async def get_stats(self) -> AgentStats:
        if self._engine is None:
            return AgentStats(
                agent_id=self.agent_id, status=OFFLINE,
                capital_allocated=self.capital_allocation,
                capital_deployed=0.0, daily_pnl=0.0, daily_pnl_pct=0.0,
                total_pnl=0.0, trades_today=0,
                win_rate_today=0.0, win_rate_alltime=0.0,
                consecutive_losses=0, last_trade_time=None, error=None,
            )

        es = self._engine.get_stats()
        daily_pnl = float(es.get("daily_pnl", 0.0))
        daily_pnl_pct = (daily_pnl / self.capital_allocation * 100.0) \
            if self.capital_allocation else 0.0
        # Today-only win rate from DB
        try:
            stats = db_queries.get_arb_stats() or {}
            wr_alltime = float(stats.get("win_rate", 0.0))
        except Exception:
            wr_alltime = 0.0
        return AgentStats(
            agent_id=self.agent_id,
            status=es.get("status", OFFLINE),
            capital_allocated=self.capital_allocation,
            capital_deployed=0.0,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            total_pnl=float(es.get("total_pnl", 0.0)),
            trades_today=int(es.get("total_trades", 0)),
            win_rate_today=wr_alltime,
            win_rate_alltime=wr_alltime,
            consecutive_losses=int(es.get("consecutive_losses", 0)),
            last_trade_time=es.get("last_trade_time"),
            error=None,
        )


# ─────────────────────────────────────────────────────────────────────────
# Placeholder agents — render on dashboard, no behaviour yet
# ─────────────────────────────────────────────────────────────────────────

class MacroAgentPlaceholder(PlaceholderAgent):
    """Placeholder — no capital, no behaviour. Reserves the dashboard
    slot and the agent_id for the real implementation.

    When implementing the real MacroAgent:
      - Subclass BaseAgent (not PlaceholderAgent)
      - Consume macro.macro_monitor.get_current_regime() for context
        (scenario, scores, dimensional flags) — that's the regime view
      - Consume macro.macro_monitor.get_macro_signals() for discrete
        events (VIX_CRISIS, YIELD_CURVE_INVERSION, DOLLAR_STRENGTH).
        These are generated each tick but not yet consumed by any agent
      - See macro/signals.py for the MacroSignal dataclass shape
      - React to scenarios rather than scan signals — e.g. open BTC
        hedges on CRISIS, scale into longs on EASING_CYCLE, reduce
        exposure on TIGHTENING_CYCLE
      - Give it its own capital pool + circuit breakers in
        config/settings.py (MACRO_AGENT_CAPITAL, etc.)
      - The monitor's run_refresh_loop already runs as part of bot
        startup, so no extra fetching plumbing is needed here
    """
    agent_id     = "macro"
    display_name = "Macro Agent"


class SentimentAgentPlaceholder(PlaceholderAgent):
    agent_id     = "sentiment_agent"
    display_name = "Sentiment Agent"


class OnChainAgentPlaceholder(PlaceholderAgent):
    agent_id     = "onchain"
    display_name = "On-Chain Agent"


# ─────────────────────────────────────────────────────────────────────────
# Registry — Coordinator picks this up by default
# ─────────────────────────────────────────────────────────────────────────

from agents.scalping_agent import ScalpingAgent
from agents.crosschain_agent import CrossChainArbAgent
from agents.balance_agent import BalanceAgent


REGISTERED_AGENTS: list[BaseAgent] = [
    SignalAgentWrapper(),
    ArbAgentWrapper(),
    ScalpingAgent(),
    CrossChainArbAgent(),
    BalanceAgent(),
    MacroAgentPlaceholder(),
    SentimentAgentPlaceholder(),
    OnChainAgentPlaceholder(),
    # Add new agents here (instances).
]

__all__ = [
    "REGISTERED_AGENTS",
    "BaseAgent",
    "PlaceholderAgent",
    "AgentStats",
    "SignalAgentWrapper",
    "ArbAgentWrapper",
    "ScalpingAgent",
    "CrossChainArbAgent",
    "BalanceAgent",
    "MacroAgentPlaceholder",
    "SentimentAgentPlaceholder",
    "OnChainAgentPlaceholder",
]
