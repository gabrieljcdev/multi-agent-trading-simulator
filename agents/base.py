"""
agents/base.py

The two contracts every trading agent must satisfy:

  AgentStats — what get_stats() returns
  BaseAgent  — what an agent class looks like

Mirrors the BaseSentimentSource pattern from sentiment/base.py: classes
declare four class attrs (agent_id, display_name, capital_allocation,
optional), implement a small set of async methods, and the coordinator
wires them up via REGISTERED_AGENTS — no coordinator changes needed.

PlaceholderAgent is a convenience base class for agents that appear on
the dashboard roster but aren't built yet (e.g. macro, on-chain). It
returns OFFLINE status and zeroed stats.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# AgentStats — what get_stats() returns
# ─────────────────────────────────────────────────────────────────────────

# Lifecycle status values
RUNNING  = "RUNNING"
PAUSED   = "PAUSED"
HALTED   = "HALTED"
OFFLINE  = "OFFLINE"
STOPPED  = "STOPPED"


@dataclass
class AgentStats:
    agent_id:           str
    status:             str                # RUNNING/PAUSED/HALTED/OFFLINE/STOPPED
    capital_allocated:  float
    capital_deployed:   float              # currently sitting in positions
    daily_pnl:          float              # USD
    daily_pnl_pct:      float              # % of capital_allocated
    total_pnl:          float              # USD all-time
    trades_today:       int
    win_rate_today:     float              # 0–1
    win_rate_alltime:   float              # 0–1
    consecutive_losses: int
    last_trade_time:    Optional[str]      # ISO string, None if never traded
    error:              Optional[str]      # set if last get_stats failed gracefully


# ─────────────────────────────────────────────────────────────────────────
# BaseAgent — subclass + register in agents/__init__.py
# ─────────────────────────────────────────────────────────────────────────

class BaseAgent(ABC):
    """Subclass to add a new trading agent.

    Override the class attrs and implement the four abstract async methods.
    is_available() defaults to True; override it when the agent depends on
    keys, modules, or other runtime conditions.

    Standard lifecycle (pause / resume) and status tracking are handled
    here; do not override them.
    """

    # Override in subclasses
    agent_id:           str   = "base"
    display_name:       str   = "Base Agent"
    capital_allocation: float = 0.0
    optional:           bool  = True       # if False, coordinator won't start without it

    def __init__(self):
        # Lifecycle state — managed by the base class
        self._status:     str             = OFFLINE
        self._start_time: Optional[float] = None
        self._error:      Optional[str]   = None

    # ── Public API ──────────────────────────────────────────────────────

    @abstractmethod
    async def start(self) -> None:
        """Boot the agent. Must set self._status = RUNNING when ready."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Graceful shutdown. Must set self._status = STOPPED."""
        ...

    @abstractmethod
    async def get_stats(self) -> AgentStats:
        """Snapshot of the agent's current state. Must NEVER raise — return
        an AgentStats with .error set on failure."""
        ...

    @abstractmethod
    async def close_all_positions(self) -> None:
        """Kill-switch path: close every open position belonging to this
        agent. Called concurrently with the same on every other agent."""
        ...

    def is_available(self) -> bool:
        """Return True if this agent can run right now (deps installed,
        keys present, etc.). Coordinator skips unavailable agents on start."""
        return True

    # ── BalanceAgent compounding hooks — safe inherited defaults ────────
    # These let the BalanceAgent scale per-agent capital up as realised
    # profit accumulates without touching individual agents' sizing code.
    # Existing agents keep working with the defaults; agents that want to
    # compound override get/set and pair them with their sizing logic.

    def get_capital_allocation(self) -> float:
        """Current deployable capital for this agent's fund.

        Default reads ``self.capital_allocation`` — the configured fund
        constant. Subclasses that compound override this to track a
        live, P&L-grown deployable amount.
        """
        return float(getattr(self, "capital_allocation", 0.0) or 0.0)

    def set_capital_allocation(self, amount: float) -> bool:
        """Set deployable capital. Returns False (no-op) if the new
        amount would be below the agent's currently open-position
        notional — never shrink the deployable pool past committed risk.

        Default updates ``self.capital_allocation`` in place. Subclasses
        with a separate deployable pool (e.g. ScalpingAgent's ``_capital``
        vs ``capital_allocation``) override this to update both. Safe:
        always returns False on bad input; never raises.
        """
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            return False
        if amt < 0:
            return False
        try:
            open_notional = float(self.get_open_position_notional() or 0.0)
        except Exception:
            open_notional = 0.0
        if amt < open_notional:
            return False
        try:
            self.capital_allocation = amt
        except Exception:
            return False
        return True

    def get_open_position_notional(self) -> float:
        """USD notional of this agent's open positions.

        Default 0.0 — placeholder / operational agents have no open
        positions. Trading agents override to sum their live exposure.
        Drives the rail-2 floor: BalanceAgent never lowers an agent's
        allocation below this number.
        """
        return 0.0

    # ── Standard lifecycle — do not override ────────────────────────────

    async def pause(self) -> None:
        self._status = PAUSED
        logger.info(f"agent {self.agent_id}: paused")

    async def resume(self) -> None:
        if self._status == PAUSED:
            self._status = RUNNING
            logger.info(f"agent {self.agent_id}: resumed")

    @property
    def status(self) -> str:
        return self._status

    @property
    def uptime_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time


# ─────────────────────────────────────────────────────────────────────────
# PlaceholderAgent — base for "not yet built" agents
# ─────────────────────────────────────────────────────────────────────────

class PlaceholderAgent(BaseAgent):
    """Base class for agents that appear on the dashboard but aren't built
    yet. Always reports OFFLINE / zero. Subclass and set agent_id +
    display_name; everything else just works."""

    optional = True

    def is_available(self) -> bool:
        return False

    async def start(self) -> None:
        self._status = OFFLINE
        logger.debug(f"placeholder agent {self.agent_id}: start (no-op)")

    async def stop(self) -> None:
        # Already OFFLINE — nothing to do
        pass

    async def close_all_positions(self) -> None:
        # No positions to close
        pass

    async def get_stats(self) -> AgentStats:
        return AgentStats(
            agent_id=self.agent_id,
            status=OFFLINE,
            capital_allocated=self.capital_allocation,
            capital_deployed=0.0,
            daily_pnl=0.0,
            daily_pnl_pct=0.0,
            total_pnl=0.0,
            trades_today=0,
            win_rate_today=0.0,
            win_rate_alltime=0.0,
            consecutive_losses=0,
            last_trade_time=None,
            error=None,
        )
