"""
tests/test_coordinator.py — Coordinator behaviour.

Tests use mock BaseAgent subclasses so we never construct a real CryptoBot
or open exchange connections. The Coordinator is agent-agnostic by design,
so this is the same surface the production wrappers go through.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from config import settings
from agents.base import (
    AgentStats, BaseAgent,
    RUNNING, PAUSED, HALTED, OFFLINE, STOPPED,
)
from agents.coordinator import Coordinator


# ─────────────────────────────────────────────────────────────────────────
# Mock agents
# ─────────────────────────────────────────────────────────────────────────

class MockAgent(BaseAgent):
    """Configurable BaseAgent stand-in. Tracks which lifecycle calls fired."""

    def __init__(
        self,
        agent_id:   str   = "mock",
        capital:    float = 100.0,
        available:  bool  = True,
        daily_pnl:  float = 0.0,
        deployed:   float = 0.0,
        trades:     int   = 0,
        wr_today:   float = 0.0,
    ):
        super().__init__()
        self.agent_id = agent_id
        self.display_name = f"Mock {agent_id}"
        self.capital_allocation = capital
        self._available_ret = available
        self._daily_pnl = daily_pnl
        self._deployed = deployed
        self._trades = trades
        self._wr_today = wr_today

        self.start_called = False
        self.stop_called  = False
        self.close_called = False
        self.pause_called = False

    def is_available(self) -> bool:
        return self._available_ret

    async def start(self) -> None:
        self.start_called = True
        self._status = RUNNING

    async def stop(self) -> None:
        self.stop_called = True
        self._status = STOPPED

    async def close_all_positions(self) -> None:
        # Slow on purpose — proves kill_all runs them concurrently
        await asyncio.sleep(0.05)
        self.close_called = True

    async def get_stats(self) -> AgentStats:
        return AgentStats(
            agent_id=self.agent_id, status=self._status,
            capital_allocated=self.capital_allocation,
            capital_deployed=self._deployed,
            daily_pnl=self._daily_pnl,
            daily_pnl_pct=(self._daily_pnl / self.capital_allocation * 100)
                          if self.capital_allocation else 0.0,
            total_pnl=0.0,
            trades_today=self._trades,
            win_rate_today=self._wr_today,
            win_rate_alltime=0.0,
            consecutive_losses=0,
            last_trade_time=None,
            error=None,
        )

    async def pause(self) -> None:
        self.pause_called = True
        await super().pause()


class CrashingAgent(BaseAgent):
    """Raises in every lifecycle method. Verifies _safe_* shields."""

    agent_id = "crashing"
    display_name = "Crashing Agent"
    capital_allocation = 50.0

    def is_available(self) -> bool:
        return True

    async def start(self) -> None:
        raise RuntimeError("boom-start")

    async def stop(self) -> None:
        raise RuntimeError("boom-stop")

    async def close_all_positions(self) -> None:
        raise RuntimeError("boom-close")

    async def get_stats(self) -> AgentStats:
        raise RuntimeError("boom-stats")


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    """Avoid SQLite during coordinator tests — every DB call becomes a no-op."""
    monkeypatch.setattr(
        "agents.coordinator.db_queries.log_portfolio_snapshot",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "agents.coordinator.db_queries.log_agent_event",
        lambda *a, **k: None,
    )


# ─────────────────────────────────────────────────────────────────────────
# kill_all — concurrent close_all_positions
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kill_all_calls_close_on_every_agent():
    agents = [MockAgent(agent_id=f"a{i}") for i in range(4)]
    coord = Coordinator(agents=agents)
    summary = await coord.kill_all(reason="test")
    assert all(a.close_called for a in agents)
    assert summary["agents_ok"] == 4
    assert summary["agents_err"] == 0


@pytest.mark.asyncio
async def test_kill_all_runs_close_concurrently():
    """Four agents that each sleep 50ms should finish in well under 200ms
    if kill_all is actually running them via asyncio.gather."""
    agents = [MockAgent(agent_id=f"a{i}") for i in range(4)]
    coord = Coordinator(agents=agents)
    t0 = asyncio.get_event_loop().time()
    await coord.kill_all()
    elapsed = asyncio.get_event_loop().time() - t0
    # Each agent sleeps 0.05s; serial would take ≥0.2s. Concurrent should
    # finish in roughly one sleep duration plus some overhead.
    assert elapsed < 0.15


# ─────────────────────────────────────────────────────────────────────────
# Unavailable agents skipped on start
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unavailable_agent_skipped_on_start():
    """Agents whose is_available() returns False never have start() called."""
    avail = MockAgent(agent_id="avail", available=True)
    unavail = MockAgent(agent_id="unavail", available=False)

    coord = Coordinator(agents=[avail, unavail])

    # We can't await full start() — it includes the monitor loop forever.
    # Use wait_for with a short timeout to exercise the start fanout.
    try:
        await asyncio.wait_for(coord.start(), timeout=0.2)
    except asyncio.TimeoutError:
        pass
    finally:
        coord._running = False

    assert avail.start_called is True
    assert unavail.start_called is False


# ─────────────────────────────────────────────────────────────────────────
# Portfolio circuit breakers
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_portfolio_halt_when_daily_loss_exceeds_threshold(monkeypatch):
    """A combined daily loss past PORTFOLIO_DAILY_LOSS_HALT_PCT pauses every agent."""
    # Two agents, each $100 alloc, each losing 4% = -$4 → combined -8/200 = -4% > 3%
    a = MockAgent(agent_id="a", capital=100.0, daily_pnl=-4.0)
    b = MockAgent(agent_id="b", capital=100.0, daily_pnl=-4.0)
    coord = Coordinator(agents=[a, b])

    stats = await coord.get_portfolio_stats()
    assert stats["total_daily_pnl_pct"] == pytest.approx(-4.0, abs=0.01)

    await coord._check_portfolio_circuit_breakers(stats)
    assert coord._halted_by_portfolio_cb is True
    assert a.pause_called is True
    assert b.pause_called is True


@pytest.mark.asyncio
async def test_portfolio_healthy_below_threshold():
    """Loss inside the threshold should not trip the halt."""
    a = MockAgent(agent_id="a", capital=100.0, daily_pnl=-1.0)
    b = MockAgent(agent_id="b", capital=100.0, daily_pnl=-1.0)
    coord = Coordinator(agents=[a, b])
    stats = await coord.get_portfolio_stats()
    assert stats["portfolio_status"] == "HEALTHY"
    await coord._check_portfolio_circuit_breakers(stats)
    assert coord._halted_by_portfolio_cb is False
    assert a.pause_called is False


# ─────────────────────────────────────────────────────────────────────────
# Stats aggregation
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_portfolio_stats_aggregates_correctly():
    a = MockAgent(agent_id="a", capital=300.0, daily_pnl=+6.0,
                  deployed=90.0, trades=5, wr_today=0.6)
    b = MockAgent(agent_id="b", capital=200.0, daily_pnl=+2.0,
                  deployed=50.0, trades=5, wr_today=0.4)
    coord = Coordinator(agents=[a, b])

    stats = await coord.get_portfolio_stats()
    assert stats["total_equity"]       == pytest.approx(500.0 + 8.0)
    assert stats["total_daily_pnl"]    == pytest.approx(8.0)
    assert stats["total_daily_pnl_pct"] == pytest.approx(8.0 / 500.0 * 100)
    assert stats["total_exposure_pct"] == pytest.approx(140.0 / 500.0 * 100)
    assert stats["total_trades_today"] == 10
    # Weighted by trade count: 5×0.6 + 5×0.4 = 5.0; / 10 = 0.5
    assert stats["overall_win_rate_today"] == pytest.approx(0.5)
    assert stats["portfolio_status"] == "HEALTHY"


@pytest.mark.asyncio
async def test_get_agent_stats_sorted_by_capital_desc():
    a = MockAgent(agent_id="small", capital=50.0)
    b = MockAgent(agent_id="big",   capital=500.0)
    c = MockAgent(agent_id="mid",   capital=200.0)
    coord = Coordinator(agents=[a, b, c])
    stats = await coord.get_agent_stats()
    assert [s.agent_id for s in stats] == ["big", "mid", "small"]


# ─────────────────────────────────────────────────────────────────────────
# Resilience — crashing agents don't take others down
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_crashing_agent_does_not_break_others_on_kill_all():
    good = MockAgent(agent_id="good")
    bad  = CrashingAgent()
    coord = Coordinator(agents=[good, bad])
    summary = await coord.kill_all()
    assert good.close_called is True
    assert summary["agents_ok"] == 1
    assert summary["agents_err"] == 1


@pytest.mark.asyncio
async def test_crashing_agent_get_stats_returns_error_field():
    bad = CrashingAgent()
    coord = Coordinator(agents=[bad])
    stats = await coord.get_agent_stats()
    assert len(stats) == 1
    assert stats[0].error is not None
    assert "boom-stats" in stats[0].error


# ─────────────────────────────────────────────────────────────────────────
# Extensibility — REGISTERED_AGENTS pattern
# ─────────────────────────────────────────────────────────────────────────

class NewlyBuiltAgent(BaseAgent):
    """Stand-in for a future agent — proves the BaseAgent contract is
    enough; Coordinator needs no changes to pick it up."""
    agent_id     = "newly_built"
    display_name = "Newly Built"

    def __init__(self):
        super().__init__()
        self.capital_allocation = 25.0

    async def start(self) -> None:
        self._status = RUNNING

    async def stop(self) -> None:
        self._status = STOPPED

    async def close_all_positions(self) -> None:
        pass

    async def get_stats(self) -> AgentStats:
        return AgentStats(
            agent_id=self.agent_id, status=self._status,
            capital_allocated=self.capital_allocation,
            capital_deployed=0.0,
            daily_pnl=+1.0, daily_pnl_pct=+4.0,
            total_pnl=0.0, trades_today=2,
            win_rate_today=1.0, win_rate_alltime=1.0,
            consecutive_losses=0, last_trade_time=None, error=None,
        )


@pytest.mark.asyncio
async def test_new_agent_class_plugs_in_via_coordinator():
    coord = Coordinator(agents=[NewlyBuiltAgent(), MockAgent(agent_id="other")])
    stats = await coord.get_portfolio_stats()
    # 2 trades from new + 0 from other = 2
    assert stats["total_trades_today"] == 2
    agent_list = await coord.get_agent_stats()
    assert any(a.agent_id == "newly_built" for a in agent_list)


# ─────────────────────────────────────────────────────────────────────────
# Misc
# ─────────────────────────────────────────────────────────────────────────

def test_get_agent_lookup():
    a = MockAgent(agent_id="alpha")
    b = MockAgent(agent_id="beta")
    coord = Coordinator(agents=[a, b])
    assert coord.get_agent("alpha") is a
    assert coord.get_agent("beta") is b
    assert coord.get_agent("missing") is None


def test_set_dashboard_propagates_to_agents():
    class DashAware(MockAgent):
        def __init__(self):
            super().__init__(agent_id="dash_aware")
            self.dashboard_set = None
        def set_dashboard(self, dashboard):
            self.dashboard_set = dashboard

    aware = DashAware()
    unaware = MockAgent(agent_id="unaware")
    coord = Coordinator(agents=[aware, unaware])
    dashboard = object()
    coord.set_dashboard(dashboard)
    assert aware.dashboard_set is dashboard
    # unaware agent has no set_dashboard — must not raise


# ─────────────────────────────────────────────────────────────────────────
# Per-fund circuit breakers — independent, ring-fenced halts
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_per_fund_circuit_breaker_halts_only_breaching_fund(monkeypatch):
    """A fund breaching -FUND_DAILY_LOSS_HALT_PCT of its OWN size is paused
    independently; healthy funds keep running. This is the ring-fence — a
    MEXC fund tripping must not touch signal/arb."""
    monkeypatch.setattr(settings, "FUND_DAILY_LOSS_HALT_PCT", 10.0)
    # scalp: -$11 on a $100 fund = -11% → breaches.
    scalp = MockAgent(agent_id="scalp", capital=100.0, daily_pnl=-11.0)
    # signal: -$1 on a $400 fund = -0.25% → fine.
    signal = MockAgent(agent_id="signal", capital=400.0, daily_pnl=-1.0)
    coord = Coordinator(agents=[scalp, signal])

    await coord._check_fund_circuit_breakers(await coord.get_agent_stats())

    assert scalp.pause_called is True
    assert signal.pause_called is False
    assert "scalp" in coord._fund_halted
    assert "signal" not in coord._fund_halted


@pytest.mark.asyncio
async def test_per_fund_circuit_breaker_clears_when_pnl_recovers(monkeypatch):
    """Once a fund's daily P&L recovers above the limit (e.g. after its UTC
    daily reset), its halt marker clears so a later dip can re-trigger."""
    monkeypatch.setattr(settings, "FUND_DAILY_LOSS_HALT_PCT", 10.0)
    fund = MockAgent(agent_id="arb", capital=500.0, daily_pnl=-75.0)
    coord = Coordinator(agents=[fund])

    await coord._check_fund_circuit_breakers(await coord.get_agent_stats())
    assert "arb" in coord._fund_halted

    fund._daily_pnl = 0.0   # daily reset zeroed the loss
    await coord._check_fund_circuit_breakers(await coord.get_agent_stats())
    assert "arb" not in coord._fund_halted


@pytest.mark.asyncio
async def test_total_equity_is_dynamic_sum_of_fund_equities():
    """Portfolio total_equity = sum of each fund's (allocation + daily P&L),
    so it tracks P&L dynamically rather than pinning to a starting constant."""
    a = MockAgent(agent_id="signal", capital=400.0, daily_pnl=10.0)
    b = MockAgent(agent_id="arb",    capital=500.0, daily_pnl=-5.0)
    c = MockAgent(agent_id="scalp",  capital=100.0, daily_pnl=0.0)
    coord = Coordinator(agents=[a, b, c])

    stats = await coord.get_portfolio_stats()
    assert stats["total_equity"] == pytest.approx(1005.0)   # 410 + 495 + 100

    b._daily_pnl = 20.0                                      # a fund's P&L moves
    stats2 = await coord.get_portfolio_stats()
    assert stats2["total_equity"] == pytest.approx(1030.0)   # not stuck
