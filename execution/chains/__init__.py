"""
execution/chains/__init__.py

Registry of chain connectors the cross-chain arb engine discovers by
default.

═══════════════════════════════════════════════════════════════════════
To add a new chain
═══════════════════════════════════════════════════════════════════════

1. Create execution/chains/my_chain.py
2. Subclass BaseChainConnector (from execution.chains.base_connector)
   — or SolidlyVolatilePoolConnector (from ._solidly_volatile) if your
   WETH-USDC venue is a Solidly-fork volatile pool, which gets you the
   pool reads + gas math for free.
3. Set the class attrs:
       connector_id = "my_chain"
       display_name = "My Chain (Venue)"
       rpc_env_var  = "MY_CHAIN_RPC_URL"
4. Implement: async get_pool_state(symbol), async gas_cost_usd().
   is_available() defaults to "env var present AND every pool address
   pinned"; override if your chain needs more.
5. Append an instance of your class to REGISTERED_CONNECTORS below.
6. Add a slot to settings.XCHAIN_VENUES + settings.XCHAIN_CHAINS.

The cross-chain engine + the BalanceAgent inventory targets pick it up
automatically — no engine-side changes, no agent-side changes (Plugin
Pattern Rule 1: the orchestrator never imports concrete plugins by
name). Mirrors agents/__init__.py and sentiment/__init__.py.
"""

from __future__ import annotations

from execution.chains.base_connector import (
    BaseChainConnector, PoolState, VenueConfig,
)
from execution.chains.arbitrum  import ArbitrumConnector
from execution.chains.base_chain import BaseChainConnectorInstance
from execution.chains.optimism  import OptimismConnector


REGISTERED_CONNECTORS: list[BaseChainConnector] = [
    ArbitrumConnector(),
    BaseChainConnectorInstance(),
    OptimismConnector(),
    # Add new chain connectors here (instances).
]


__all__ = [
    "REGISTERED_CONNECTORS",
    "BaseChainConnector",
    "PoolState",
    "VenueConfig",
    "ArbitrumConnector",
    "BaseChainConnectorInstance",
    "OptimismConnector",
]
