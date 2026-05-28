"""
execution/chains/base_connector.py

The three contracts every chain connector must satisfy:

  VenueConfig — per-(chain, symbol) DEX venue + pool + advertised fee
  PoolState   — the snapshot the engine consumes for the edge math
  BaseChainConnector — ABC + is_available() default

Mirrors the plugin pattern (PLUGIN_PATTERN.md) one level deeper: where
agents/data_sources/sentiment have a base class + registry list, the
crosschain agent has its own sub-registry of chain connectors. The
engine imports ONLY BaseChainConnector + REGISTERED_CONNECTORS, never a
concrete connector by name, so adding Polygon or Linea is one file +
one line in __init__.py.

OBSERVATION-MODE invariant: submit_swap is declared on the ABC but
raises NotImplementedError at this layer. Live execution (web3 signing,
aggregator routing) is a SEPARATE later build, gated behind
settings.XCHAIN_LIVE_ENABLED. Nothing in the engine reaches submit_swap
while observation mode is active.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class VenueConfig:
    """Per-chain DEX venue + pool the connector reads.

    fee_bps here is the *documented* tier (configurable via settings); the
    engine's edge math reads PoolState.fee_bps which is sampled from the
    pool itself at scan time. The two should match — a divergence is a
    misconfigured pool address and the operator should hear about it.
    """
    venue:        str   # "uniswap_v3" | "aerodrome" | "velodrome" | "curve"
    pool_address: str   # the specific WETH-USDC pool to read
    fee_bps:      float # documented tier; pool's actual fee_bps wins in math


@dataclass
class PoolState:
    """Snapshot a connector returns for one (symbol, venue) on its chain.

    For constant-product (Uniswap v2 / Aerodrome / Velodrome stable+vol)
    pools, reserve_base and reserve_quote are the raw on-chain reserves.
    For concentrated-liquidity pools (Uniswap v3/v4) the connector must
    derive virtual reserves from the active tick liquidity so the engine
    can apply the same x*y=k optimal-size formula uniformly.

    depth_usd_1pct is the USDC notional tradable within 1% price impact
    on this pool — caps the engine's per-leg notional independently of
    the global XCHAIN_MAX_POSITION_USD ceiling.

    error is set when the RPC fetch failed; the engine treats a non-None
    error as "this chain is unavailable this scan" and logs but does not
    halt. block_number == 0 means the connector couldn't read the chain
    head; the staleness gate uses that as "infinitely stale".
    """
    connector_id:    str            # "arbitrum" | "base" | "optimism"
    symbol:          str            # "WETH-USDC"
    venue:           str
    reserve_base:    float          # x  (WETH)   — virtual for CL pools
    reserve_quote:   float          # y  (USDC)
    fee_bps:         float          # actual pool fee in bps (READ from pool)
    spot_price:      float          # y / x  (USDC per WETH)
    depth_usd_1pct:  float          # USDC notional within 1% impact
    block_number:    int
    timestamp:       float
    error:           Optional[str] = None


class BaseChainConnector(ABC):
    """Subclass to add a new L2 / L1 to the cross-chain agent.

    Override the class attrs, implement get_pool_state + gas_cost_usd, and
    append an instance of your class to REGISTERED_CONNECTORS in
    execution/chains/__init__.py. The engine picks it up automatically —
    no engine-side changes, no agent-side changes (Plugin Pattern Rule 1:
    the orchestrator never imports concrete plugins by name).
    """

    # Override in subclasses
    connector_id: str = "base"
    display_name: str = "Base Chain"
    rpc_env_var:  str = ""              # e.g. "ARBITRUM_RPC_URL"
    optional:     bool = True

    # symbol -> VenueConfig. Populated by __init__ from settings.XCHAIN_VENUES
    # so a settings change updates every connector without code edits.
    venues:       dict

    # ── Abstract ────────────────────────────────────────────────────────

    @abstractmethod
    async def get_pool_state(self, symbol: str) -> PoolState:
        """Snapshot the configured pool for `symbol` on this chain.

        Must NEVER raise — on RPC failure return a PoolState with
        error=<message>, block_number=0, and reserves=0.0 so the engine
        can log the skip and move on.
        """
        ...

    @abstractmethod
    async def gas_cost_usd(self) -> float:
        """Per-swap gas cost on this chain, in USD, as of right now.

        On RPC failure return float('inf') so the engine's gas_breakeven
        check refuses every trade until the chain comes back.
        """
        ...

    # ── Default behaviour — override only if your chain needs more ──────

    def is_available(self) -> bool:
        """True iff the RPC URL env var is present.

        Pool address "<FILL>" sentinel also counts as unavailable — a
        misconfigured pool would silently read the wrong pair, so we
        refuse to come online with a placeholder address. Mirrors the
        SignalAgentWrapper.is_available() pattern (env-var presence
        gates the whole connector).
        """
        if not self.rpc_env_var:
            return False
        if not os.getenv(self.rpc_env_var):
            return False
        # Every configured venue must have a concrete pool address.
        for venue in (self.venues or {}).values():
            if not venue.pool_address or venue.pool_address == "<FILL>":
                return False
        return True

    async def submit_swap(self, *args, **kwargs):
        """OBSERVATION MODE: live execution is a SEPARATE later build.

        Gated behind settings.XCHAIN_LIVE_ENABLED. Until that flag flips
        AND a concrete connector overrides this method with a web3
        signing path, every call here raises. The engine MUST NOT call
        this in observation mode — but if a misconfiguration ever does,
        we fail loudly rather than silently.
        """
        raise NotImplementedError(
            "live execution is a separate build (XCHAIN_LIVE_ENABLED + web3 signing)"
        )


__all__ = ["VenueConfig", "PoolState", "BaseChainConnector"]
