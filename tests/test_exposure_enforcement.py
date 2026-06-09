"""
tests/test_exposure_enforcement.py — portfolio + per-fund exposure enforcement.

Wires the long-standing "block_new_entries" TODO: Coordinator._enforce_exposure
blocks an agent's NEW entries (exits/management keep running) when its own fund
is over-deployed, or the whole book is over the portfolio cap. Enforced via the
shared BaseAgent._entries_blocked gate that every trading agent's entry path now
honours (alongside the operator halt).
"""

from __future__ import annotations

import asyncio
import types

import pytest

from config import settings
from agents.base import BaseAgent, AgentStats


class _FakeAgent(BaseAgent):
    """Minimal concrete BaseAgent for gate/enforcement tests."""

    def __init__(self, agent_id: str, alloc: float = 1000.0):
        super().__init__()
        self.agent_id = agent_id
        self.display_name = agent_id
        self.capital_allocation = alloc
        self.propagated: list[bool] = []

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def close_all_positions(self) -> None: ...

    async def get_stats(self) -> AgentStats:        # pragma: no cover - unused
        return _stats(self.agent_id, self.capital_allocation, 0.0)

    def _propagate_entries_blocked(self, blocked: bool) -> None:
        self.propagated.append(blocked)


def _stats(agent_id, alloc, deployed):
    return AgentStats(
        agent_id=agent_id, status="RUNNING",
        capital_allocated=alloc, capital_deployed=deployed,
        daily_pnl=0.0, daily_pnl_pct=0.0, total_pnl=0.0, trades_today=0,
        win_rate_today=0.0, win_rate_alltime=0.0, consecutive_losses=0,
        last_trade_time=None, error=None,
    )


# ── BaseAgent gate ────────────────────────────────────────────────────────

def test_set_entries_blocked_flips_propagates_and_is_idempotent():
    a = _FakeAgent("fake")
    assert a.entries_blocked is False
    a.set_entries_blocked(True, "fund 500%")
    assert a._entries_blocked is True and a.entries_blocked is True
    assert a.propagated == [True]
    a.set_entries_blocked(True)              # idempotent — no re-propagate
    assert a.propagated == [True]
    a.set_entries_blocked(False)
    assert a.entries_blocked is False
    assert a.propagated == [True, False]


def test_entries_blocked_covers_manual_halt_too():
    a = _FakeAgent("fake")
    a.halt_manual()
    assert a.entries_blocked is True         # operator halt alone blocks entries
    a.resume_manual()
    assert a.entries_blocked is False
    # exposure block is independent of the manual flag
    a.set_entries_blocked(True)
    assert a.entries_blocked is True
    assert a.manually_halted is False


# ── Coordinator enforcement ───────────────────────────────────────────────

def test_overdeployed_fund_is_blocked_others_are_not():
    from agents.coordinator import Coordinator
    funding = _FakeAgent("funding_arb", alloc=900.0)
    arb = _FakeAgent("arb", alloc=1600.0)
    coord = Coordinator(agents=[funding, arb])
    # funding deployed $4500 (500% of its $900 fund); arb deployed $0.
    coord._enforce_exposure(
        [_stats("funding_arb", 900.0, 4500.0), _stats("arb", 1600.0, 0.0)],
        {"total_exposure_pct": 50.0})
    assert funding._entries_blocked is True
    assert arb._entries_blocked is False


def test_fund_unblocks_when_exposure_recovers():
    from agents.coordinator import Coordinator
    funding = _FakeAgent("funding_arb", alloc=900.0)
    coord = Coordinator(agents=[funding])
    coord._enforce_exposure([_stats("funding_arb", 900.0, 4500.0)],
                            {"total_exposure_pct": 50.0})
    assert funding._entries_blocked is True
    # positions close -> deployed back under the fund cap -> unblocked
    coord._enforce_exposure([_stats("funding_arb", 900.0, 100.0)],
                            {"total_exposure_pct": 50.0})
    assert funding._entries_blocked is False


def test_portfolio_over_cap_blocks_every_agent():
    from agents.coordinator import Coordinator
    funding = _FakeAgent("funding_arb", alloc=900.0)
    arb = _FakeAgent("arb", alloc=1600.0)
    coord = Coordinator(agents=[funding, arb])
    # Each fund individually fine (0 deployed), but the book is over the
    # portfolio cap -> everyone is blocked.
    over = settings.PORTFOLIO_MAX_EXPOSURE_PCT + 50.0
    coord._enforce_exposure(
        [_stats("funding_arb", 900.0, 0.0), _stats("arb", 1600.0, 0.0)],
        {"total_exposure_pct": over})
    assert funding._entries_blocked is True
    assert arb._entries_blocked is True


def test_zero_allocation_agent_never_blocked_by_fund_rule():
    from agents.coordinator import Coordinator
    obs = _FakeAgent("opportunity_scanner", alloc=0.0)
    coord = Coordinator(agents=[obs])
    # alloc 0 -> fund rule N/A (no divide-by-zero, not flagged)
    coord._enforce_exposure([_stats("opportunity_scanner", 0.0, 0.0)],
                            {"total_exposure_pct": 50.0})
    assert obs._entries_blocked is False


# ── Wrapper propagation (exposure gate reaches the wrapped bot/engine) ─────

def test_signal_wrapper_propagates_to_bot():
    from agents import SignalAgentWrapper
    w = SignalAgentWrapper.__new__(SignalAgentWrapper)   # bypass heavy __init__
    w._bot = types.SimpleNamespace(_entries_blocked=False)
    w._propagate_entries_blocked(True)
    assert w._bot._entries_blocked is True
    w._propagate_entries_blocked(False)
    assert w._bot._entries_blocked is False


def test_arb_wrapper_propagates_to_engine():
    from agents import ArbAgentWrapper
    w = ArbAgentWrapper.__new__(ArbAgentWrapper)
    w._engine = types.SimpleNamespace(_entries_blocked=False)
    w._propagate_entries_blocked(True)
    assert w._engine._entries_blocked is True
