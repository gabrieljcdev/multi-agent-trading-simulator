"""
execution/chains/_solidly_volatile.py

Shared partial implementation for Solidly-fork *volatile* pools (the
x*y=k branch). Aerodrome on Base and Velodrome on Optimism are both
Solidly forks with both stable (x^3*y + x*y^3 = k) and volatile (x*y=k)
pools — WETH-USDC is the volatile type on both, with identical read
ABIs. Factoring this out avoids the 80-line duplication that would
otherwise sit in execution/chains/base_chain.py and optimism.py.

Concrete connectors (BaseChainConnectorInstance, OptimismConnector)
import only this module + base_connector.py + libs — consistent with
Plugin Pattern Rule 2 (connectors are self-contained, importing only
the base contract + libraries needed to satisfy it). The shared parent
is part of the chain-connector layer's own internal API; it isn't
imported by the engine or the agent.
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Optional

from config import settings
from execution.chains.base_connector import (
    BaseChainConnector, PoolState, VenueConfig,
)

logger = logging.getLogger(__name__)


# Minimal Solidly-volatile ABI fragments. Same shape as Uniswap v2's
# Pair: getReserves() returns (reserve0, reserve1, blockTimestampLast).
_SOLIDLY_POOL_ABI = [
    {"inputs": [], "name": "getReserves",
     "outputs": [
         {"name": "_reserve0",           "type": "uint256"},
         {"name": "_reserve1",           "type": "uint256"},
         {"name": "_blockTimestampLast", "type": "uint256"},
     ], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0",
     "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1",
     "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "stable",
     "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
]
_ERC20_ABI = [
    {"inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}],
     "stateMutability": "view", "type": "function"},
]


def _try_import_web3():
    """Lazy import. Returns the Web3 class or None — never raises."""
    try:
        from web3 import Web3                   # type: ignore
        return Web3
    except Exception:
        return None


class SolidlyVolatilePoolConnector(BaseChainConnector):
    """Partial implementation for any chain whose WETH-USDC venue is a
    Solidly-volatile (x*y=k) pool. Subclasses must set connector_id,
    display_name, rpc_env_var, and (optionally) SWAP_GAS_UNITS — they
    inherit the pool read + gas math unchanged."""

    # Average gas units for a Solidly-fork single-hop volatile swap.
    # Per-subclass override is fine if calibration reveals a difference.
    SWAP_GAS_UNITS = 120_000

    def __init__(self):
        cfg = settings.XCHAIN_VENUES.get(self.connector_id, {})
        self.venues: dict[str, VenueConfig] = {
            sym: VenueConfig(
                venue=cfg.get("venue", ""),
                pool_address=cfg.get("pool_address", "<FILL>"),
                fee_bps=float(cfg.get("fee_bps", 5.0)),
            )
            for sym in settings.XCHAIN_SYMBOLS
        }
        self._w3 = None
        self._decimals_cache: dict[str, int] = {}

    # ── is_available — extends base check with web3 importability ──────

    def is_available(self) -> bool:
        if _try_import_web3() is None:
            return False
        return super().is_available()

    # ── Web3 lazy resolver ──────────────────────────────────────────────

    def _resolve_w3(self):
        if self._w3 is not None:
            return self._w3
        Web3 = _try_import_web3()
        if Web3 is None:
            return None
        rpc = os.getenv(self.rpc_env_var)
        if not rpc:
            return None
        try:
            self._w3 = Web3(Web3.HTTPProvider(rpc))
        except Exception as e:
            logger.debug("[%s] Web3 init failed: %s", self.connector_id, e)
            return None
        return self._w3

    async def _decimals(self, addr: str, default: int) -> int:
        if addr in self._decimals_cache:
            return self._decimals_cache[addr]
        w3 = self._resolve_w3()
        if w3 is None:
            return default
        try:
            erc = w3.eth.contract(address=addr, abi=_ERC20_ABI)
            dec = int(erc.functions.decimals().call())
            self._decimals_cache[addr] = dec
            return dec
        except Exception as e:
            logger.debug("[%s] decimals(%s) failed: %s", self.connector_id, addr, e)
            return default

    # ── Pool state ──────────────────────────────────────────────────────

    async def get_pool_state(self, symbol: str) -> PoolState:
        venue = self.venues.get(symbol)
        if venue is None:
            return self._error_state(symbol, "no_venue_configured")

        w3 = self._resolve_w3()
        if w3 is None:
            return self._error_state(symbol, "rpc_unavailable", venue=venue)

        try:
            pool = w3.eth.contract(address=venue.pool_address, abi=_SOLIDLY_POOL_ABI)
            r0, r1, _ = pool.functions.getReserves().call()
            token0     = pool.functions.token0().call()
            token1     = pool.functions.token1().call()
            block_num  = int(w3.eth.block_number)
        except Exception as e:
            logger.debug("[%s] pool read failed: %s", self.connector_id, e)
            return self._error_state(symbol, f"pool_read_failed:{e}", venue=venue)

        if int(r0) == 0 or int(r1) == 0:
            return self._error_state(symbol, "empty_pool", venue=venue, block=block_num)

        d0 = await self._decimals(token0, 18)
        d1 = await self._decimals(token1, 6)

        # Orient as base=WETH, quote=USDC (USDC has fewer decimals than WETH
        # for every WETH-USDC pool on these chains). spot is USDC per WETH.
        if d0 < d1:
            reserve_quote = float(r0) / (10 ** d0)
            reserve_base  = float(r1) / (10 ** d1)
        else:
            reserve_base  = float(r0) / (10 ** d0)
            reserve_quote = float(r1) / (10 ** d1)

        spot_usdc_per_weth = reserve_quote / reserve_base if reserve_base > 0 else 0.0

        # 1% slippage notional on constant-product: y * (sqrt(1.01)-1).
        depth_1pct = reserve_quote * (math.sqrt(1.01) - 1.0)

        # Solidly fork stores the fee on the factory, not the pool — we use
        # the documented tier as the actual fee. Operator pins it via
        # settings.XCHAIN_VENUES, which is also where the math reads it.
        return PoolState(
            connector_id=self.connector_id,
            symbol=symbol,
            venue=venue.venue,
            reserve_base=reserve_base,
            reserve_quote=reserve_quote,
            fee_bps=venue.fee_bps,
            spot_price=spot_usdc_per_weth,
            depth_usd_1pct=depth_1pct,
            block_number=block_num,
            timestamp=time.time(),
            error=None,
        )

    def _error_state(
        self,
        symbol: str,
        reason: str,
        *,
        venue: Optional[VenueConfig] = None,
        block: int = 0,
    ) -> PoolState:
        return PoolState(
            connector_id=self.connector_id,
            symbol=symbol,
            venue=(venue.venue if venue else ""),
            reserve_base=0.0,
            reserve_quote=0.0,
            fee_bps=(venue.fee_bps if venue else 0.0),
            spot_price=0.0,
            depth_usd_1pct=0.0,
            block_number=block,
            timestamp=time.time(),
            error=reason,
        )

    # ── Gas ─────────────────────────────────────────────────────────────

    async def gas_cost_usd(self) -> float:
        """Per-swap gas in USD = gasPrice * SWAP_GAS_UNITS * ETH/USD / 1e18.

        ETH/USD comes from the configured pool's spot_price so we never need
        a second data source. inf on failure — engine treats inf as "skip".
        """
        w3 = self._resolve_w3()
        if w3 is None:
            return float("inf")
        try:
            gas_price_wei = int(w3.eth.gas_price)
        except Exception as e:
            logger.debug("[%s] gas_price failed: %s", self.connector_id, e)
            return float("inf")
        first_sym = settings.XCHAIN_SYMBOLS[0] if settings.XCHAIN_SYMBOLS else None
        if first_sym is None:
            return float("inf")
        state = await self.get_pool_state(first_sym)
        if state.error or state.spot_price <= 0:
            return float("inf")
        return (gas_price_wei * self.SWAP_GAS_UNITS / 1e18) * state.spot_price


__all__ = ["SolidlyVolatilePoolConnector"]
