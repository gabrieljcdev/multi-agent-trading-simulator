"""
tests/test_chain_connectors.py — chain connector ABC contract + registry.

Covers spec test (A): BaseChainConnector contract; is_available() gates
on RPC env var presence (and on pinned pool addresses, which is the
SAME gate the spec calls out — a missing pool reads the wrong pair, so
"<FILL>" must not pass).

No web3 calls, no live RPC. Each test stubs os.environ to simulate the
env var being present or absent.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from config import settings
from execution.chains import (
    REGISTERED_CONNECTORS,
    BaseChainConnector, PoolState, VenueConfig,
    ArbitrumConnector, BaseChainConnectorInstance, OptimismConnector,
)


# ─────────────────────────────────────────────────────────────────────────
# Registry shape — Plugin Pattern invariants
# ─────────────────────────────────────────────────────────────────────────

def test_registry_lists_all_three_chains():
    ids = [c.connector_id for c in REGISTERED_CONNECTORS]
    assert "arbitrum" in ids
    assert "base"     in ids
    assert "optimism" in ids


def test_every_connector_subclasses_base():
    for c in REGISTERED_CONNECTORS:
        assert isinstance(c, BaseChainConnector)


def test_every_connector_declares_required_attrs():
    """connector_id, display_name, rpc_env_var, venues — Plugin Pattern Rule 3."""
    for c in REGISTERED_CONNECTORS:
        assert c.connector_id
        assert c.display_name
        assert c.rpc_env_var
        assert isinstance(c.venues, dict) and len(c.venues) >= 1


# ─────────────────────────────────────────────────────────────────────────
# (A) is_available gates on RPC env var presence
# ─────────────────────────────────────────────────────────────────────────

def _set_pinned_pools():
    """settings.XCHAIN_VENUES ships with "<FILL>" pool addresses so the
    operator must pin them. For the is_available test we substitute real
    addresses so the env-var gate is what's actually being measured."""
    return patch.dict(settings.XCHAIN_VENUES, {
        "arbitrum": {"venue": "uniswap_v3", "pool_address": "0xAAAA", "fee_bps": 5.0},
        "base":     {"venue": "aerodrome",  "pool_address": "0xBBBB", "fee_bps": 5.0},
        "optimism": {"venue": "velodrome",  "pool_address": "0xCCCC", "fee_bps": 5.0},
    })


def test_is_available_false_without_rpc_env_var():
    """No env var → not available, even with pinned pool addresses."""
    with _set_pinned_pools(), patch.dict(os.environ, {}, clear=False):
        # Strip out any pre-existing RPC env vars from the runner's env.
        for var in ("ARBITRUM_RPC_URL", "BASE_RPC_URL", "OPTIMISM_RPC_URL"):
            os.environ.pop(var, None)
        # Each connector must read False — the env var is the gate.
        assert ArbitrumConnector().is_available() is False
        assert BaseChainConnectorInstance().is_available() is False
        assert OptimismConnector().is_available() is False


def test_is_available_false_with_fill_pool_sentinel():
    """RPC URL present but pool_address == '<FILL>' must still be unavailable —
    a placeholder address would silently read the wrong pool. Note: the test
    only meaningfully runs the gate when web3 is importable; without web3 the
    answer is False either way (which is also the correct behaviour)."""
    with patch.dict(os.environ, {
        "ARBITRUM_RPC_URL": "http://localhost:8545",
        "BASE_RPC_URL":     "http://localhost:8545",
        "OPTIMISM_RPC_URL": "http://localhost:8545",
    }):
        # XCHAIN_VENUES still has '<FILL>' (the shipped default).
        with patch.dict(settings.XCHAIN_VENUES, {
            "arbitrum": {"venue": "uniswap_v3", "pool_address": "<FILL>", "fee_bps": 5.0},
        }):
            assert ArbitrumConnector().is_available() is False


def test_base_connector_submit_swap_raises_in_observation_mode():
    """The ABC's submit_swap is the observation-mode gate. Calling it must
    raise NotImplementedError until the live-execution build lands. None
    of the three concrete connectors overrides it."""
    import asyncio
    for cls in (ArbitrumConnector, BaseChainConnectorInstance, OptimismConnector):
        with pytest.raises(NotImplementedError):
            asyncio.get_event_loop().run_until_complete(cls().submit_swap())


# ─────────────────────────────────────────────────────────────────────────
# PoolState + VenueConfig dataclass shape
# ─────────────────────────────────────────────────────────────────────────

def test_pool_state_roundtrip_fields():
    """PoolState exposes every field the engine math depends on."""
    s = PoolState(
        connector_id="arbitrum", symbol="WETH-USDC", venue="uniswap_v3",
        reserve_base=100.0, reserve_quote=300_000.0,
        fee_bps=5.0, spot_price=3000.0, depth_usd_1pct=10_000.0,
        block_number=123, timestamp=1.0,
    )
    assert s.error is None
    assert s.spot_price == 3000.0
    assert s.depth_usd_1pct == 10_000.0


def test_venue_config_carries_documented_tier():
    v = VenueConfig(venue="uniswap_v3", pool_address="0xAAAA", fee_bps=5.0)
    assert v.fee_bps == 5.0
    assert v.venue == "uniswap_v3"
