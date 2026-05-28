"""
tests/test_inventory_targets.py — execution/inventory.py.

Covers spec test (G): compute_inventory_targets flags needs_rebalance
when drift > theta (XCHAIN_INVENTORY_DRIFT_PCT).

Also pins the weighting rule (more would_entry rows on a chain →
more inventory on that chain) and the equal-share cold-start fallback.
"""

from __future__ import annotations

import pytest

from config import settings
from execution.inventory import InventoryTarget, compute_inventory_targets


CHAINS = ["arbitrum", "base", "optimism"]


def _obs(buy_chain: str, sell_chain: str, would_entry: bool = True) -> dict:
    """Minimal duck-typed observation row — only the fields the
    weighter consumes."""
    return {"buy_chain": buy_chain, "sell_chain": sell_chain,
            "would_entry": would_entry}


# ─────────────────────────────────────────────────────────────────────────
# (G) needs_rebalance fires when drift > theta
# ─────────────────────────────────────────────────────────────────────────

def test_needs_rebalance_true_when_drift_exceeds_theta():
    """At theta=0.20, a chain with $0 vs target $50 has drift=1.0 → must
    rebalance. With perfect balance (current == target) → no rebalance."""
    # Equal observations across chains → equal weights → $300/3 = $100 each.
    obs = [_obs("arbitrum", "base"), _obs("base", "optimism"),
           _obs("optimism", "arbitrum")]
    targets = compute_inventory_targets(
        observations=obs,
        current_balances={
            "arbitrum": {"base_usd":  0.0, "quote_usd":  0.0},   # severely under-funded
            "base":     {"base_usd": 50.0, "quote_usd": 50.0},   # at target ($50 each side)
            "optimism": {"base_usd": 50.0, "quote_usd": 50.0},   # at target
        },
        capital_usd=300.0,
        chains=CHAINS,
        drift_threshold=0.20,
    )
    by_chain = {t.connector_id: t for t in targets}
    assert by_chain["arbitrum"].needs_rebalance is True
    assert by_chain["arbitrum"].drift_pct == pytest.approx(1.0)
    assert by_chain["base"].needs_rebalance is False
    assert by_chain["optimism"].needs_rebalance is False


def test_needs_rebalance_false_just_under_theta():
    """drift just under theta=0.20 must NOT trigger rebalance — verifies
    the comparison is strictly >, not >=."""
    # Single observation per chain → equal weight. Capital $300, 3 chains.
    # Target per chain = $100; split 50/50 base/quote = $50 each.
    obs = [_obs("arbitrum", "base"), _obs("base", "optimism"),
           _obs("optimism", "arbitrum")]
    # current = $40 each ; drift = (50-40)/50 = 0.20 — NOT strictly > 0.20
    targets = compute_inventory_targets(
        observations=obs,
        current_balances={c: {"base_usd": 40.0, "quote_usd": 40.0} for c in CHAINS},
        capital_usd=300.0,
        chains=CHAINS,
        drift_threshold=0.20,
    )
    for t in targets:
        assert t.drift_pct == pytest.approx(0.20)
        assert t.needs_rebalance is False


# ─────────────────────────────────────────────────────────────────────────
# Weighting rule — more entries → more inventory
# ─────────────────────────────────────────────────────────────────────────

def test_weighting_favours_chains_with_more_entries():
    """If arbitrum participates in 10 entries and base in 1, arbitrum's
    target inventory must exceed base's (after applying the soft floor)."""
    obs = (
        [_obs("arbitrum", "optimism")] * 10
        + [_obs("base", "optimism")]
    )
    targets = compute_inventory_targets(
        observations=obs,
        capital_usd=1000.0,
        chains=CHAINS,
        drift_threshold=0.20,
    )
    by_chain = {t.connector_id: t for t in targets}
    assert by_chain["arbitrum"].target_base_usd > by_chain["base"].target_base_usd


def test_weighting_floor_prevents_zero_allocation():
    """A chain with zero observed entries still gets the soft floor —
    the BalanceAgent must be able to seed inventory there."""
    obs = [_obs("arbitrum", "base")] * 10
    targets = compute_inventory_targets(
        observations=obs,
        capital_usd=1000.0,
        chains=CHAINS,
        drift_threshold=0.20,
    )
    by_chain = {t.connector_id: t for t in targets}
    # optimism never appeared — but floor (= 1/(2*N) = 1/6 ~ 16.7% of capital)
    # guarantees non-zero target.
    assert by_chain["optimism"].target_base_usd  > 0
    assert by_chain["optimism"].target_quote_usd > 0


def test_cold_start_falls_back_to_equal_split():
    """No would_entry observations anywhere → equal split. The BalanceAgent
    can seed inventory before any data lands."""
    obs = []                  # nothing observed yet
    targets = compute_inventory_targets(
        observations=obs,
        capital_usd=900.0,
        chains=CHAINS,
        drift_threshold=0.20,
    )
    # 3 chains × $300 each, then 50/50 base/quote = $150 each side.
    for t in targets:
        assert t.target_base_usd  == pytest.approx(150.0)
        assert t.target_quote_usd == pytest.approx(150.0)


def test_observation_mode_capital_zero_emits_zero_targets():
    """Capital 0 (default observation mode) → targets are zero, no rebalance."""
    obs = [_obs("arbitrum", "base")]
    targets = compute_inventory_targets(
        observations=obs,
        capital_usd=0.0,
        chains=CHAINS,
    )
    for t in targets:
        assert t.target_base_usd  == 0.0
        assert t.target_quote_usd == 0.0
        assert t.needs_rebalance is False


def test_targets_emitted_per_enabled_chain():
    """One InventoryTarget per chain — even when no obs reference it."""
    targets = compute_inventory_targets(
        observations=[_obs("arbitrum", "base")],
        capital_usd=300.0,
        chains=CHAINS,
    )
    assert {t.connector_id for t in targets} == set(CHAINS)


def test_inventory_target_dataclass_carries_symbol():
    targets = compute_inventory_targets(
        observations=[], capital_usd=100.0, chains=CHAINS,
        symbol="WETH-USDC",
    )
    assert all(t.symbol == "WETH-USDC" for t in targets)
