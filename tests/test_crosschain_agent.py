"""
tests/test_crosschain_agent.py — CrossChainArbAgent BaseAgent contract.

Covers spec test (H): agent satisfies the full BaseAgent contract and
reports OBSERVATION (capital_allocated=0, status=OFFLINE before start)
when XCHAIN_CAPITAL == 0; get_inventory_targets() returns the published
targets.

Also pins the registry membership — "xchain" must appear in
REGISTERED_AGENTS without any coordinator-side changes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from config import settings
from agents import REGISTERED_AGENTS
from agents.base import AgentStats, BaseAgent, OFFLINE, RUNNING, STOPPED
from agents.crosschain_agent import CrossChainArbAgent
from execution.inventory import InventoryTarget


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────────────────────────────────────────────────────────
# Registry — agent appears automatically, no coordinator changes
# ─────────────────────────────────────────────────────────────────────────

def test_xchain_agent_registered():
    ids = [a.agent_id for a in REGISTERED_AGENTS]
    assert "xchain" in ids


def test_registered_xchain_is_crosschain_agent():
    by_id = {a.agent_id: a for a in REGISTERED_AGENTS}
    assert isinstance(by_id["xchain"], CrossChainArbAgent)


# ─────────────────────────────────────────────────────────────────────────
# (H) BaseAgent contract
# ─────────────────────────────────────────────────────────────────────────

def test_subclasses_base_agent():
    agent = CrossChainArbAgent()
    assert isinstance(agent, BaseAgent)


def test_required_class_attrs():
    agent = CrossChainArbAgent()
    assert agent.agent_id     == "xchain"
    assert agent.display_name == "Cross-Chain Arb"
    assert agent.optional is True


def test_observation_mode_capital_zero_by_default():
    """XCHAIN_CAPITAL defaults to 0.0 — capital_allocation reflects that."""
    with patch.object(settings, "XCHAIN_CAPITAL", 0.0):
        agent = CrossChainArbAgent()
        assert agent.capital_allocation == 0.0


def test_get_stats_returns_offline_before_start():
    """get_stats MUST NEVER raise; before start() it returns OFFLINE."""
    agent = CrossChainArbAgent()
    stats = _run(agent.get_stats())
    assert isinstance(stats, AgentStats)
    assert stats.status == OFFLINE
    assert stats.capital_deployed == 0.0
    assert stats.daily_pnl == 0.0
    assert stats.error is None


def test_get_stats_reports_observation_capital_when_xchain_capital_zero():
    """Observation mode: capital_allocated stays at 0.0 — the dashboard
    renders this as the OBSERVATION chip."""
    with patch.object(settings, "XCHAIN_CAPITAL", 0.0):
        agent = CrossChainArbAgent()
        stats = _run(agent.get_stats())
        assert stats.capital_allocated == 0.0


def test_close_all_positions_is_safe_with_no_engine():
    """Kill-switch path must be a no-op before start()."""
    agent = CrossChainArbAgent()
    _run(agent.close_all_positions())     # must not raise
    # No status change — still OFFLINE.
    assert agent.status == OFFLINE


def test_stop_is_safe_with_no_engine():
    agent = CrossChainArbAgent()
    _run(agent.stop())
    assert agent.status == STOPPED


def test_start_offline_without_two_connectors(monkeypatch):
    """Engine refuses to start with <2 connectors available — agent stays
    OFFLINE. Stub the registry to <2 available connectors and observe."""
    fake_registry = []                                # zero connectors
    monkeypatch.setattr("execution.chains.REGISTERED_CONNECTORS", fake_registry)
    agent = CrossChainArbAgent()
    _run(agent.start())                              # must not block / raise
    assert agent.status == OFFLINE


# ─────────────────────────────────────────────────────────────────────────
# get_inventory_targets() — publishes the BalanceAgent's input
# ─────────────────────────────────────────────────────────────────────────

def test_get_inventory_targets_returns_inventory_target_list():
    agent = CrossChainArbAgent()
    with patch("agents.crosschain_agent.db_queries.get_xchain_observations",
               return_value=[]):
        out = agent.get_inventory_targets()
    assert isinstance(out, list)
    assert all(isinstance(t, InventoryTarget) for t in out)
    # Defaults match settings.XCHAIN_CHAINS — one target per enabled chain.
    assert {t.connector_id for t in out} == set(settings.XCHAIN_CHAINS)


def test_get_inventory_targets_swallows_db_errors():
    """get_xchain_observations raising must not propagate — kill-switch
    + dashboard paths must not be coupled to DB health."""
    agent = CrossChainArbAgent()
    with patch("agents.crosschain_agent.db_queries.get_xchain_observations",
               side_effect=RuntimeError("db dead")):
        out = agent.get_inventory_targets()
    # Falls through to compute_inventory_targets with empty observations.
    assert isinstance(out, list)
    assert len(out) == len(settings.XCHAIN_CHAINS)


def test_get_inventory_targets_uses_current_balances_for_drift():
    """When current_balances are passed, drift_pct reflects them — not
    a stale internal cache."""
    agent = CrossChainArbAgent()
    with patch("agents.crosschain_agent.db_queries.get_xchain_observations",
               return_value=[
                   {"buy_chain": "arbitrum", "sell_chain": "base",
                    "would_entry": True},
                   {"buy_chain": "base", "sell_chain": "optimism",
                    "would_entry": True},
                   {"buy_chain": "optimism", "sell_chain": "arbitrum",
                    "would_entry": True},
               ]), \
         patch.object(settings, "XCHAIN_CAPITAL", 300.0), \
         patch.object(settings, "XCHAIN_INVENTORY_DRIFT_PCT", 0.20):
        # Severely under-funded arbitrum.
        out = agent.get_inventory_targets(current_balances={
            "arbitrum": {"base_usd":  0.0, "quote_usd":  0.0},
            "base":     {"base_usd": 50.0, "quote_usd": 50.0},
            "optimism": {"base_usd": 50.0, "quote_usd": 50.0},
        })
    by_chain = {t.connector_id: t for t in out}
    assert by_chain["arbitrum"].needs_rebalance is True
    assert by_chain["base"].needs_rebalance is False
