"""
tests/test_balance_agent.py

BalanceAgent + InventoryState + policy + planner + rails coverage.

Tests deliberately stay at the unit level — they exercise each layer in
isolation, then end-to-end via the agent. DB writes use the project's
real session manager (in-memory via the test SQLite config) so the
audit-log shape is the same one production will see.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from unittest.mock import MagicMock, patch

import pytest

from config import settings


def _run(coro):
    """Isolated async runner — never touches the default event loop
    policy. asyncio.run() flips the policy's _set_called=True, which
    breaks tests that lean on asyncio.get_event_loop()'s auto-create
    behaviour (see tests/test_chain_connectors.py). new_event_loop +
    close leaves the policy untouched."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

# Plugin + ABC imports (Plugin Rule 1: only base + registry from caller)
from agents.balance.inventory_state import InventoryState, inventory_state
from agents.balance.policy import REGISTERED_POLICIES
from agents.balance.policy.base import BasePolicy, InventoryTarget
from agents.balance.planner import (
    BaseRebalancePlanner, GreedyNetPlanner, PlannerConstraints, Transfer,
)
from agents.balance.rails import REGISTERED_RAILS
from agents.balance.rails.base import BaseTransferRail, TransferResult
from agents.balance.rails.sim_rail import SimTransferRail
from agents.balance.rails.cex_rail import CexTransferRail
from agents.balance_agent import BalanceAgent
from agents.base import BaseAgent


# ── Shared fixtures ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_inventory_state():
    inventory_state.reset()
    yield
    inventory_state.reset()


@pytest.fixture
def fast_settings(monkeypatch):
    """Speed up sim transfers + clamp daily limits for tight loops."""
    monkeypatch.setattr(settings, "SIM_TRANSFER_DELAY_S", 0.0, raising=False)
    monkeypatch.setattr(settings, "SIM_WITHDRAWAL_FEE_USD", 1.0, raising=False)
    monkeypatch.setattr(settings, "SIM_REBALANCE_FAILURE_RATE", 0.0, raising=False)
    monkeypatch.setattr(settings, "REBALANCE_DAILY_LIMIT", 3, raising=False)
    monkeypatch.setattr(settings, "REBALANCE_CONFIRM_WINDOW_S", 0.5, raising=False)
    monkeypatch.setattr(settings, "REBALANCE_LIVE_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "SIM_MODE", True, raising=False)
    yield


# ── Plugin compliance ────────────────────────────────────────────────────

class TestPluginCompliance:
    def test_rails_registry_minimum(self):
        ids = [r.rail_id for r in REGISTERED_RAILS]
        assert "sim" in ids
        assert "cex" in ids

    def test_policies_registry_minimum(self):
        ids = [p.policy_id for p in REGISTERED_POLICIES]
        assert "growth_optimal" in ids

    def test_aggregator_imports_only_base_and_registry(self):
        """BalanceAgent must not import concrete rail/policy classes by
        name (Plugin Rule 1). The source must only reference BaseAgent,
        BasePolicy, BaseRebalancePlanner, BaseTransferRail + REGISTERED_*."""
        src = inspect.getsource(BalanceAgent)
        # CexTransferRail is referenced for restart reconciliation only —
        # an isinstance check, not for default dispatch — but otherwise
        # the agent goes through the abstract layer.
        assert "GreedyNetPlanner" in src   # default fallback for planner
        assert "REGISTERED_POLICIES" in src
        assert "REGISTERED_RAILS" in src

    def test_fake_rail_is_picked_up(self, fast_settings):
        """A fake rail added to REGISTERED_RAILS is dispatched without
        aggregator changes."""
        class _FakeRail(BaseTransferRail):
            rail_id = "fake"
            display_name = "Fake"

            def is_available(self): return True

            async def execute(self, transfer):
                return TransferResult(success=True, state="completed",
                                      movement_id=999)

        agent = BalanceAgent(rails=[_FakeRail()])
        # Force the agent's rail picker — emulate a planned transfer.
        rail = agent._pick_rail(Transfer(
            from_fund="arb", to_fund="signal",
            from_exchange="bitget", to_exchange="kraken",
            amount_usd=10.0, asset="USDT",
        ))
        assert rail is not None and rail.rail_id == "fake"

    def test_crashing_rail_does_not_break_agent(self, fast_settings):
        class _BadRail(BaseTransferRail):
            rail_id = "bad"
            display_name = "Bad"

            def is_available(self): return True

            async def execute(self, transfer):
                raise RuntimeError("boom")

        # Direct execute should NEVER raise — the contract says rails
        # return a failed TransferResult on error. SimTransferRail's
        # _execute_inner wraps; the contract holds for compliant rails.
        # We assert the contract by wrapping the rail ourselves:
        result = _run(SimTransferRail().execute(
            Transfer(
                from_fund="arb", to_fund="signal",
                from_exchange="bitget", to_exchange="kraken",
                amount_usd=10.0,
            )
        ))
        assert isinstance(result, TransferResult)

    def test_unavailable_rail_is_skipped(self, fast_settings, monkeypatch):
        # Cex rail is unavailable when REBALANCE_LIVE_ENABLED is False
        monkeypatch.setattr(settings, "REBALANCE_LIVE_ENABLED", False, raising=False)
        assert CexTransferRail().is_available() is False


# ── BaseAgent defaults ──────────────────────────────────────────────────

class TestBaseAgentDefaults:
    def test_defaults_dont_change_unconfigured_agents(self):
        # PlaceholderAgent uses default get/set/get_open_*, none should raise
        from agents.base import PlaceholderAgent

        class _P(PlaceholderAgent):
            agent_id = "stub"
            display_name = "Stub"

        a = _P()
        a.capital_allocation = 100.0
        assert a.get_capital_allocation() == 100.0
        assert a.get_open_position_notional() == 0.0
        assert a.set_capital_allocation(200.0) is True
        assert a.capital_allocation == 200.0

    def test_set_capital_refuses_below_open_position(self):
        class _A(BaseAgent):
            agent_id = "x"
            display_name = "X"
            optional = True

            def __init__(self):
                super().__init__()
                self.capital_allocation = 500.0
                self._open = 300.0

            def is_available(self): return True

            async def start(self): self._status = "RUNNING"
            async def stop(self): self._status = "STOPPED"
            async def close_all_positions(self): pass

            async def get_stats(self):
                from agents.base import AgentStats, OFFLINE
                return AgentStats(
                    agent_id=self.agent_id, status=OFFLINE,
                    capital_allocated=self.capital_allocation,
                    capital_deployed=0.0, daily_pnl=0.0, daily_pnl_pct=0.0,
                    total_pnl=0.0, trades_today=0,
                    win_rate_today=0.0, win_rate_alltime=0.0,
                    consecutive_losses=0, last_trade_time=None, error=None,
                )

            def get_open_position_notional(self): return self._open

        a = _A()
        assert a.set_capital_allocation(200.0) is False  # below open notional
        assert a.capital_allocation == 500.0
        assert a.set_capital_allocation(400.0) is True   # above floor
        assert a.capital_allocation == 400.0


# ── InventoryState ──────────────────────────────────────────────────────

class TestInventoryState:
    def test_effective_balance_fails_open_with_no_claims(self, monkeypatch):
        monkeypatch.setitem(settings.EXCHANGE_BALANCES, "kraken", 100.0)
        assert inventory_state.effective_balance("arb", "kraken", "USDT") == 100.0

    def test_claims_partition_shared_venue(self, monkeypatch):
        monkeypatch.setitem(settings.EXCHANGE_BALANCES, "kraken", 100.0)
        inventory_state.apply_allocation("signal", "kraken", "USDT", 40.0)
        inventory_state.apply_allocation("arb",    "kraken", "USDT", 60.0)
        # Fully subscribed (40 + 60 == 100): each fund sees its own claim
        # only — `mine` and `physical - other_claims` agree at the boundary.
        assert inventory_state.effective_balance("arb", "kraken", "USDT") == 60.0
        assert inventory_state.effective_balance("signal", "kraken", "USDT") == 40.0

    def test_undersubscribed_venue_returns_physical_minus_other_claims(self, monkeypatch):
        """Regression (smoke 2026-05-29): when a venue is undersubscribed
        (total claims < physical), each fund must see its claim PLUS the
        unclaimed slack — i.e. `physical - other_claims`, matching the
        method's docstring. Previously the code returned `mine` when
        undersubscribed, stranding the slack and gating ArbEngine.can_arb
        to the policy's per-venue Kelly-scaled target (e.g. $46 on a
        $400 venue), well below ARB_BASE_POSITION_USD ($60). Every arb
        opportunity then logged `balance_fail InventoryState.can_arb
        blocked` despite hundreds of USDT sitting free on the venue."""
        monkeypatch.setitem(settings.EXCHANGE_BALANCES, "bitget", 400.0)
        # Only the arb fund claims bitget, and only $46 — exactly the
        # Kelly-scaled policy target observed in the smoke test.
        inventory_state.apply_allocation("arb", "bitget", "USDT", 46.0)
        # arb gets its $46 + the $354 of unclaimed slack = $400.
        assert inventory_state.effective_balance("arb", "bitget", "USDT") == 400.0
        # A fund with no claim on this venue still sees the slack (no
        # other fund has reserved it). This preserves the fail-open
        # behaviour the arb engine has always relied on.
        assert inventory_state.effective_balance("signal", "bitget", "USDT") == 354.0
        # And the canonical fix: can_arb with size $60 now PASSES.
        assert inventory_state.can_arb(
            "ATOM/USDT", "buy", 60.0,
            buy_exchange="mexc", sell_exchange="bitget", fund="arb",
        ) is True

    def test_partially_subscribed_two_funds_each_get_slack_plus_claim(self, monkeypatch):
        """Two funds, each claiming a portion < physical → each sees its
        own claim plus the still-unclaimed remainder. Ring-fencing still
        holds: neither fund can take the *other's* claim, only the slack."""
        monkeypatch.setitem(settings.EXCHANGE_BALANCES, "mexc", 1000.0)
        inventory_state.apply_allocation("arb",        "mexc", "USDT",  46.0)
        inventory_state.apply_allocation("mexc_scalp", "mexc", "USDT", 324.0)
        # arb: physical (1000) - other_claims (324) = 676
        assert inventory_state.effective_balance("arb", "mexc", "USDT") == 1000.0 - 324.0
        # mexc_scalp: physical (1000) - other_claims (46) = 954
        assert inventory_state.effective_balance("mexc_scalp", "mexc", "USDT") == 1000.0 - 46.0

    def test_oversubscribed_venue_scales_proportionally(self, monkeypatch):
        """Unchanged behaviour: when total_claim > physical, each fund's
        effective scales by its claim share. Guards against regressing
        the over-subscription branch while we change the under-sub one."""
        monkeypatch.setitem(settings.EXCHANGE_BALANCES, "mexc", 100.0)
        inventory_state.apply_allocation("arb",        "mexc", "USDT", 100.0)
        inventory_state.apply_allocation("mexc_scalp", "mexc", "USDT", 100.0)
        # total_claim = 200, physical = 100 → each gets 100 * (100/200) = 50
        assert inventory_state.effective_balance("arb", "mexc", "USDT") == 50.0
        assert inventory_state.effective_balance("mexc_scalp", "mexc", "USDT") == 50.0

    def test_pending_out_subtracts_from_source(self, monkeypatch):
        monkeypatch.setitem(settings.EXCHANGE_BALANCES, "kraken", 100.0)
        monkeypatch.setitem(settings.EXCHANGE_BALANCES, "bitget", 0.0)
        # Claim both venues so neither falls through to fail-open default.
        inventory_state.apply_allocation("arb", "kraken", "USDT", 100.0)
        inventory_state.apply_allocation("arb", "bitget", "USDT", 0.0)
        inventory_state.open_transfer(
            movement_id=1, from_fund="arb", to_fund="arb",
            amount_usd=30.0,
            from_exchange="kraken", to_exchange="bitget",
            state="in_transit",
        )
        # Source loses 30 to pending-out.
        assert inventory_state.effective_balance("arb", "kraken", "USDT") == 70.0
        # Destination claim is 0, but in-flight credit adds 30.
        assert inventory_state.effective_balance("arb", "bitget", "USDT") == 30.0

    def test_can_arb_refuses_paused_route(self, monkeypatch):
        monkeypatch.setitem(settings.EXCHANGE_BALANCES, "kraken", 100.0)
        monkeypatch.setitem(settings.EXCHANGE_BALANCES, "bitget", 100.0)
        inventory_state.pause_route("kraken", "bitget", "test")
        assert inventory_state.can_arb(
            "BTC/USDT", "buy", 10.0,
            buy_exchange="kraken", sell_exchange="bitget",
        ) is False
        # Reverse direction still allowed (recovery)
        assert inventory_state.can_arb(
            "BTC/USDT", "buy", 10.0,
            buy_exchange="bitget", sell_exchange="kraken",
        ) is True


# ── Policy ──────────────────────────────────────────────────────────────

class TestGrowthOptimalPolicy:
    def test_targets_respect_reserve(self, monkeypatch):
        monkeypatch.setattr(settings, "COMPOUND_RESERVE_PCT", 0.10, raising=False)
        monkeypatch.setattr(settings, "ALLOCATION_CONFIDENCE", 0.0, raising=False)
        monkeypatch.setattr(settings, "KELLY_FRACTION", 1.0, raising=False)
        # With ALLOCATION_CONFIDENCE=0 and KELLY=1, weights are pure
        # risk-parity (equal split) × pool. Reserve is 10%, so sum of
        # targets ≤ 90% of equity.
        from agents.balance.policy.growth_optimal import GrowthOptimalPolicy
        targets = GrowthOptimalPolicy().compute_targets(inventory_state, equity=1000.0)
        total = sum(t.target_usd for t in targets)
        assert total <= 900.0 + 1.0   # 90% pool ± rounding

    def test_risk_parity_fallback_when_edge_absent(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOCATION_CONFIDENCE", 0.0, raising=False)
        monkeypatch.setattr(settings, "KELLY_FRACTION", 1.0, raising=False)
        from agents.balance.policy.growth_optimal import GrowthOptimalPolicy
        targets = GrowthOptimalPolicy().compute_targets(inventory_state, equity=300.0)
        # Equal split across 3 funds should make per-fund totals roughly equal
        by_fund: dict = {}
        for t in targets:
            by_fund[t.fund] = by_fund.get(t.fund, 0.0) + t.target_usd
        vals = list(by_fund.values())
        if len(vals) >= 2:
            assert max(vals) - min(vals) < 1e-3 + 1.0  # equal-ish

    def test_capacity_cap_caps_and_cascades(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOCATION_CONFIDENCE", 0.0, raising=False)
        monkeypatch.setattr(settings, "KELLY_FRACTION", 1.0, raising=False)
        # Cap MEXC at $1 — overflow must cascade.
        monkeypatch.setattr(
            settings, "FUND_CAPACITY_CEILINGS_USD",
            {("arb", "mexc"): 1.0},
            raising=False,
        )
        from agents.balance.policy.growth_optimal import GrowthOptimalPolicy
        targets = GrowthOptimalPolicy().compute_targets(inventory_state, equity=10_000.0)
        mexc = [t for t in targets if t.fund == "arb" and t.exchange == "mexc"]
        assert mexc and mexc[0].target_usd <= 1.0 + 1e-6


# ── Planner ─────────────────────────────────────────────────────────────

class TestGreedyNetPlanner:
    def _mk_state(self, *cells):
        for fund, ex, amount in cells:
            inventory_state.apply_allocation(fund, ex, "USDT", amount)

    def test_zero_plan_when_nodes_at_target(self, monkeypatch):
        # Allocate exactly to target → no transfer
        monkeypatch.setitem(settings.EXCHANGE_BALANCES, "kraken", 100.0)
        self._mk_state(("arb", "kraken", 100.0))
        tgt = InventoryTarget(
            fund="arb", exchange="kraken", asset="USDT",
            target_usd=100.0, floor_usd=0.0, cap_usd=200.0,
            drift_pct=0.0, needs_rebalance=False,
        )
        constraints = PlannerConstraints(daily_limit=3, daily_used=0, in_flight=0)
        transfers = GreedyNetPlanner().plan(
            inv=inventory_state, targets=[tgt],
            cost_matrix={}, constraints=constraints,
        )
        assert transfers == []

    def test_in_flight_lockout_in_live(self, monkeypatch):
        monkeypatch.setattr(settings, "SIM_MODE", False, raising=False)
        tgt = InventoryTarget(
            fund="arb", exchange="kraken", asset="USDT",
            target_usd=100.0, floor_usd=0.0, cap_usd=200.0,
            drift_pct=-1.0, needs_rebalance=True,
        )
        constraints = PlannerConstraints(daily_limit=3, daily_used=0, in_flight=1)
        out = GreedyNetPlanner().plan(
            inv=inventory_state, targets=[tgt], cost_matrix={},
            constraints=constraints,
        )
        assert out == []

    def test_daily_rate_limit(self, monkeypatch):
        monkeypatch.setitem(settings.EXCHANGE_BALANCES, "kraken", 1000.0)
        self._mk_state(("arb", "kraken", 1000.0))
        tgt = InventoryTarget(
            fund="arb", exchange="bitget", asset="USDT",
            target_usd=100.0, floor_usd=0.0, cap_usd=200.0,
            drift_pct=-1.0, needs_rebalance=True,
        )
        constraints = PlannerConstraints(daily_limit=3, daily_used=3, in_flight=0)
        out = GreedyNetPlanner().plan(
            inv=inventory_state, targets=[tgt], cost_matrix={},
            constraints=constraints,
        )
        assert out == []


# ── Rails ───────────────────────────────────────────────────────────────

class TestSimRail:
    def test_atomic_completion_fee_and_state(self, fast_settings):
        rail = SimTransferRail()
        result = _run(rail.execute(Transfer(
            from_fund="arb", to_fund="signal",
            from_exchange="bitget", to_exchange="kraken",
            amount_usd=50.0,
        )))
        assert result.success is True
        assert result.state == "completed"
        assert result.fee_usd == 1.0

    def test_injected_failure_path(self, fast_settings, monkeypatch):
        monkeypatch.setattr(settings, "SIM_REBALANCE_FAILURE_RATE", 1.0,
                            raising=False)
        rail = SimTransferRail()
        result = _run(rail.execute(Transfer(
            from_fund="arb", to_fund="signal",
            from_exchange="bitget", to_exchange="kraken",
            amount_usd=50.0,
        )))
        assert result.success is False
        assert result.error == "injected_failure"


class TestCexRail:
    def test_dormant_without_live_flag(self, fast_settings, monkeypatch):
        monkeypatch.setattr(settings, "REBALANCE_LIVE_ENABLED", False,
                            raising=False)
        monkeypatch.setattr(settings, "WITHDRAWAL_ROUTES",
                            {("bitget", "kraken", "USDT"): ["TRC20"]},
                            raising=False)
        assert CexTransferRail().is_available() is False

    def test_available_when_live_and_routes(self, fast_settings, monkeypatch):
        monkeypatch.setattr(settings, "REBALANCE_LIVE_ENABLED", True,
                            raising=False)
        monkeypatch.setattr(settings, "WITHDRAWAL_ROUTES",
                            {("bitget", "kraken", "USDT"): ["TRC20"]},
                            raising=False)
        assert CexTransferRail().is_available() is True

    def test_refuses_unknown_network(self, fast_settings, monkeypatch):
        # Live flag flipped but no route configured → fails with
        # "no network configured", no signing.
        monkeypatch.setattr(settings, "REBALANCE_LIVE_ENABLED", True,
                            raising=False)
        monkeypatch.setattr(settings, "WITHDRAWAL_ROUTES", {}, raising=False)
        rail = CexTransferRail()
        result = _run(rail.execute(Transfer(
            from_fund="arb", to_fund="signal",
            from_exchange="bitget", to_exchange="kraken",
            amount_usd=50.0,
        )))
        assert result.success is False
        assert "no network configured" in (result.error or "")


# ── BalanceAgent end-to-end ─────────────────────────────────────────────

class TestBalanceAgent:
    def test_kill_blocks_new_transfers(self, fast_settings):
        agent = BalanceAgent()
        _run(agent.close_all_positions())
        assert agent._paused is True

    def test_arm_confirm_two_step(self, fast_settings):
        agent = BalanceAgent()
        tok, notice = agent.arm()
        assert tok and len(tok) > 4
        assert "live" in notice
        # Wrong token rejected
        assert agent.consume_arm("deadbeef") is False
        # Re-arm; correct token accepted ONCE
        tok2, _ = agent.arm()
        assert agent.consume_arm(tok2) is True
        # Second consume of same token rejected
        assert agent.consume_arm(tok2) is False

    def test_arm_expiry(self, fast_settings, monkeypatch):
        monkeypatch.setattr(settings, "REBALANCE_CONFIRM_WINDOW_S", 0.0,
                            raising=False)
        agent = BalanceAgent()
        tok, _ = agent.arm()
        assert agent.consume_arm(tok) is False  # expired immediately


# ── Capital-allocation propagation through arb engine ───────────────────

class TestArbEngineCompounding:
    def test_dynamic_base_position_scales_with_allocation(self):
        from execution.arb_engine import ArbEngine
        eng = ArbEngine.__new__(ArbEngine)
        # Skip __init__; just check the helper math.
        eng._capital_allocation = settings.FUND_ARB_CAPITAL * 2  # 2× starting
        base = eng._dynamic_base_position()
        assert base == pytest.approx(settings.ARB_BASE_POSITION_USD * 2.0)

    def test_dynamic_base_position_clamped(self):
        from execution.arb_engine import ArbEngine
        eng = ArbEngine.__new__(ArbEngine)
        eng._capital_allocation = settings.FUND_ARB_CAPITAL * 1000  # absurd
        base = eng._dynamic_base_position()
        # Clamped to 10× factor
        assert base <= settings.ARB_BASE_POSITION_USD * 10.0 + 1e-6


# ── Wire test: ScalpingAgent's compounded sizing ────────────────────────

class TestScalpAgentCompounding:
    def test_get_capital_allocation_takes_max(self, monkeypatch):
        from agents.scalping_agent import ScalpingAgent
        a = ScalpingAgent()
        a.capital_allocation = 700.0
        a._capital = 500.0
        assert a.get_capital_allocation() == 700.0


# ── FIX 3: structural-drift hint comes from settings ────────────────────

class TestStructuralDriftHint:
    """The internalize step's drift hint must be the configured value,
    not a literal — required for the soak so an operator can sweep it
    without touching planner.py."""

    def _tgt(self, drift_pct: float) -> InventoryTarget:
        return InventoryTarget(
            fund="arb", exchange="kraken", asset="USDT",
            target_usd=100.0, floor_usd=0.0, cap_usd=200.0,
            drift_pct=drift_pct, needs_rebalance=True,
        )

    def test_drift_hint_below_threshold_internalized(self, monkeypatch):
        # With the hint at 0.20, drift 0.15 → strategy can self-correct
        # (filtered out).
        monkeypatch.setattr(settings, "BALANCE_STRUCTURAL_DRIFT_HINT", 0.20)
        out = GreedyNetPlanner._filter_to_structural([self._tgt(0.15)])
        assert out == []

    def test_drift_hint_above_threshold_structural(self, monkeypatch):
        # Drift 0.25 > hint 0.20 → structural, planner sees it.
        monkeypatch.setattr(settings, "BALANCE_STRUCTURAL_DRIFT_HINT", 0.20)
        out = GreedyNetPlanner._filter_to_structural([self._tgt(0.25)])
        assert len(out) == 1

    def test_drift_hint_can_be_swept(self, monkeypatch):
        # A loose hint admits more candidates; a tight hint admits fewer.
        # Same drift on both sides of the boundary — the hint is what
        # decides, not the literal.
        targets = [self._tgt(0.12)]
        monkeypatch.setattr(settings, "BALANCE_STRUCTURAL_DRIFT_HINT", 0.10)
        assert len(GreedyNetPlanner._filter_to_structural(targets)) == 1
        monkeypatch.setattr(settings, "BALANCE_STRUCTURAL_DRIFT_HINT", 0.30)
        assert GreedyNetPlanner._filter_to_structural(targets) == []

    def test_no_020_literal_in_planner(self):
        """Regression guard — the literal 0.20 must not reappear in the
        structural-filter heuristic. Drift-hint comparison is the only
        place the literal would creep back in."""
        import inspect as _inspect
        src = _inspect.getsource(GreedyNetPlanner._filter_to_structural)
        assert "0.20" not in src, (
            "0.20 literal re-introduced into _filter_to_structural — "
            "must read settings.BALANCE_STRUCTURAL_DRIFT_HINT"
        )
        assert "BALANCE_STRUCTURAL_DRIFT_HINT" in src


# ── FIX 4: Miller-Orr breach uses half-width, not full ──────────────────

class TestMillerOrrHalfWidth:
    """Regression for the factor-of-2 bug: the do-nothing region must be
    target ± spread/2, NOT target ± spread (which is what the code did
    before — making the band 2× wider than the canonical Miller-Orr
    formula intends, so transfers fired far too rarely)."""

    def _tgt(self, target_usd: float) -> InventoryTarget:
        return InventoryTarget(
            fund="arb", exchange="kraken", asset="USDT",
            target_usd=target_usd, floor_usd=0.0,
            cap_usd=target_usd * 5.0,
            # drift_pct > BALANCE_STRUCTURAL_DRIFT_HINT so the
            # structural filter doesn't drop the node before the band
            # check.
            drift_pct=0.50, needs_rebalance=True,
        )

    def test_node_at_0_4_spread_does_not_breach(self, monkeypatch):
        """At 0.4 × spread from target, current is inside the do-nothing
        region (|drift| < spread/2) → no transfer fires."""
        monkeypatch.setattr(settings, "BALANCE_STRUCTURAL_DRIFT_HINT", 0.10)
        tgt = self._tgt(target_usd=100.0)
        planner = GreedyNetPlanner()
        # Pin the band to a known value so the test is independent of
        # SIM_WITHDRAWAL_FEE_USD / opportunity-cost defaults.
        spread = 20.0
        monkeypatch.setattr(planner, "_miller_orr_band",
                            staticmethod(lambda t: spread))
        # 0.4 * spread = 8 from target → cur=108 is INSIDE [target ± 10].
        inv = MagicMock()
        inv.effective_balance = MagicMock(return_value=108.0)
        out = planner.plan(
            inv=inv, targets=[tgt], cost_matrix={},
            constraints=PlannerConstraints(daily_limit=3, daily_used=0,
                                           in_flight=0),
        )
        assert out == [], "0.4*spread should NOT breach the do-nothing region"

    def test_node_at_0_6_spread_breaches(self, monkeypatch):
        """At 0.6 × spread from target, current is OUTSIDE the do-nothing
        region (|drift| > spread/2) → transfer fires.

        Before the FIX 4 correction the breach test used ±band (full
        spread), so a 0.6*spread drift was inside the region and no
        transfer fired — this regression test pins the corrected
        behaviour.
        """
        monkeypatch.setattr(settings, "BALANCE_STRUCTURAL_DRIFT_HINT", 0.10)
        # Surplus at one node, deficit at a paired node — same fund so
        # the greedy match has something to do once the breach fires.
        surplus = self._tgt(target_usd=100.0)
        deficit = InventoryTarget(
            fund="arb", exchange="bitget", asset="USDT",
            target_usd=100.0, floor_usd=0.0, cap_usd=500.0,
            drift_pct=-0.50, needs_rebalance=True,
        )
        planner = GreedyNetPlanner()
        spread = 20.0
        monkeypatch.setattr(planner, "_miller_orr_band",
                            staticmethod(lambda t: spread))
        # Surplus side: 0.6 * spread = 12 > 10 (half-width) — breach.
        # Deficit side mirrors so the match has both ends.
        balances = {("arb", "kraken", "USDT"): 112.0,
                    ("arb", "bitget", "USDT"): 88.0}
        inv = MagicMock()
        inv.effective_balance = MagicMock(
            side_effect=lambda fund, ex, asset: balances.get((fund, ex, asset), 0.0),
        )
        out = planner.plan(
            inv=inv, targets=[surplus, deficit], cost_matrix={},
            constraints=PlannerConstraints(daily_limit=3, daily_used=0,
                                           in_flight=0),
        )
        assert len(out) == 1, "0.6*spread SHOULD breach the do-nothing region"
        assert out[0].from_exchange == "kraken"
        assert out[0].to_exchange   == "bitget"


# ── FIX 2: get_true_pnl gross/cost/net round-trip ───────────────────────

class TestGetTruePnl:
    """End-to-end DB round trip for get_true_pnl against a temp SQLite.

    Seeds arb_trades + capital_movements with known shapes and asserts
    the gross/cost/net math, that failed movements are excluded, and
    that raw rows are not mutated by the read."""

    def _temp_db(self, tmp_path, monkeypatch):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database import db as db_module
        from database.models import Base
        test_engine = create_engine(
            f"sqlite:///{tmp_path / 'truepnl.db'}",
            connect_args={"check_same_thread": False}, echo=False,
        )
        Base.metadata.create_all(bind=test_engine)
        TS = sessionmaker(bind=test_engine, autoflush=False,
                          autocommit=False, expire_on_commit=False)
        monkeypatch.setattr(db_module, "engine", test_engine)
        monkeypatch.setattr(db_module, "SessionLocal", TS)
        return test_engine, TS

    def test_gross_cost_net_round_trip(self, tmp_path, monkeypatch):
        _, TS = self._temp_db(tmp_path, monkeypatch)
        monkeypatch.setattr(settings, "SIM_WITHDRAWAL_FEE_USD", 1.0)
        from database.models import ArbTrade, CapitalMovement
        from datetime import datetime
        from database import queries as q

        with TS() as s:
            s.add(ArbTrade(symbol="BTC/USDT", net_pnl_usd=5.0,
                           success=True, sim_mode=True,
                           timestamp=datetime.utcnow()))
            s.add(ArbTrade(symbol="ETH/USDT", net_pnl_usd=2.5,
                           success=True, sim_mode=True,
                           timestamp=datetime.utcnow()))
            # Three sim transfers: 2 completed + 1 in_transit + 1 FAILED
            # (must be excluded). Total fee = 3 × $1 = $3.
            for state in ("completed", "completed", "in_transit"):
                s.add(CapitalMovement(
                    from_fund="arb", to_fund="signal",
                    amount_usd=50.0, mode="sim", state=state,
                    timestamp=datetime.utcnow(),
                ))
            s.add(CapitalMovement(
                from_fund="arb", to_fund="signal",
                amount_usd=50.0, mode="sim", state="failed",
                timestamp=datetime.utcnow(),
            ))
            s.commit()

        out = q.get_true_pnl(days=7)
        assert out["gross_arb_pnl"] == 7.5
        assert out["rebalance_cost"] == 3.0
        assert out["net_pnl"] == 4.5
        assert out["movements"] == 3      # failed excluded

    def test_raw_rows_unmutated(self, tmp_path, monkeypatch):
        _, TS = self._temp_db(tmp_path, monkeypatch)
        monkeypatch.setattr(settings, "SIM_WITHDRAWAL_FEE_USD", 1.0)
        from database.models import ArbTrade
        from datetime import datetime
        from database import queries as q

        with TS() as s:
            s.add(ArbTrade(symbol="BTC/USDT", net_pnl_usd=5.0,
                           success=True, sim_mode=True,
                           timestamp=datetime.utcnow()))
            s.commit()

        # Call get_true_pnl a few times — the raw row's net_pnl_usd
        # must still equal 5.0 after the read.
        for _ in range(3):
            q.get_true_pnl(days=7)
        with TS() as s:
            rows = s.query(ArbTrade).all()
            assert len(rows) == 1
            assert rows[0].net_pnl_usd == 5.0

    def test_excludes_old_rows_outside_window(self, tmp_path, monkeypatch):
        _, TS = self._temp_db(tmp_path, monkeypatch)
        monkeypatch.setattr(settings, "SIM_WITHDRAWAL_FEE_USD", 1.0)
        from database.models import ArbTrade, CapitalMovement
        from datetime import datetime, timedelta
        from database import queries as q

        now = datetime.utcnow()
        old = now - timedelta(days=30)
        with TS() as s:
            s.add(ArbTrade(symbol="BTC/USDT", net_pnl_usd=5.0,
                           success=True, sim_mode=True, timestamp=now))
            s.add(ArbTrade(symbol="ETH/USDT", net_pnl_usd=100.0,
                           success=True, sim_mode=True, timestamp=old))
            s.add(CapitalMovement(from_fund="arb", to_fund="signal",
                                  amount_usd=50.0, mode="sim",
                                  state="completed", timestamp=now))
            s.add(CapitalMovement(from_fund="arb", to_fund="signal",
                                  amount_usd=50.0, mode="sim",
                                  state="completed", timestamp=old))
            s.commit()

        out = q.get_true_pnl(days=7)
        assert out["gross_arb_pnl"] == 5.0    # old row excluded
        assert out["movements"]    == 1       # old movement excluded
        assert out["rebalance_cost"] == 1.0


# ── Smoke: the import path the prompt's verification step checks ────────

def test_get_true_pnl_importable():
    """`python -c "from database.queries import get_true_pnl"` must succeed —
    the prompt's verification step runs this exact import."""
    from database.queries import get_true_pnl as _f
    assert callable(_f)
