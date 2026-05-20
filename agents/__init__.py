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
        daily_pnl_pct = getattr(cb, "daily_pnl_pct", 0.0) if cb else 0.0
        daily_pnl_usd = daily_pnl_pct / 100.0 * self.capital_allocation
        consec        = getattr(cb, "consecutive_losses", 0) if cb else 0
        halted        = bool(getattr(cb, "halted", False))

        # Pull DB-derived figures, but never let a DB hiccup poison get_stats.
        try:
            today  = db_queries.get_today_trades()
            wr_t   = db_queries.get_signal_win_rate(days=1)
            wr_all = db_queries.get_signal_win_rate(days=365)
            open_t = db_queries.get_open_trades()
        except Exception:
            today, wr_t, wr_all, open_t = [], {"total": 0}, {"total": 0}, []

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
            total_pnl=0.0,                # all-time P&L tracking is a future job
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

    def is_available(self) -> bool:
        # Dedicated arb engine isn't built yet.
        try:
            from execution import arb_engine  # noqa: F401
        except ImportError:
            return False
        # Need at least two exchange keys for cross-exchange arb
        if settings.SIM_MODE:
            return True
        keyed = sum(
            1 for ex in settings.ENABLED_EXCHANGES
            if os.getenv(f"{ex.upper()}_API_KEY")
        )
        return keyed >= 2

    async def start(self) -> None:
        if not self.is_available():
            self._status = OFFLINE
            return
        # TODO: wire in real arb engine when execution/arb_engine.py exists
        self._status = OFFLINE

    async def stop(self) -> None:
        self._status = STOPPED

    async def close_all_positions(self) -> None:
        # Arb positions close themselves on completion; if a real engine
        # arrives, route the kill through it here.
        pass

    async def get_stats(self) -> AgentStats:
        return AgentStats(
            agent_id=self.agent_id,
            status=self._status if self._status != RUNNING else RUNNING,
            capital_allocated=self.capital_allocation,
            capital_deployed=0.0, daily_pnl=0.0, daily_pnl_pct=0.0,
            total_pnl=0.0, trades_today=0,
            win_rate_today=0.0, win_rate_alltime=0.0,
            consecutive_losses=0, last_trade_time=None, error=None,
        )


# ─────────────────────────────────────────────────────────────────────────
# Placeholder agents — render on dashboard, no behaviour yet
# ─────────────────────────────────────────────────────────────────────────

class MacroAgentPlaceholder(PlaceholderAgent):
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

REGISTERED_AGENTS: list[BaseAgent] = [
    SignalAgentWrapper(),
    ArbAgentWrapper(),
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
    "MacroAgentPlaceholder",
    "SentimentAgentPlaceholder",
    "OnChainAgentPlaceholder",
]
