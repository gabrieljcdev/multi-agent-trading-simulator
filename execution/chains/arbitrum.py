"""
execution/chains/arbitrum.py

ArbitrumConnector — reads WETH-USDC from a Uniswap v3 pool on Arbitrum.

The v3 0.05% WETH-USDC pool is the deepest WETH-USDC venue on Arbitrum;
default pool_address comes from settings.XCHAIN_VENUES["arbitrum"] so
the operator can pin it without code changes.

Concentrated-liquidity pools store sqrtPriceX96 + active-tick L in slot0
+ liquidity(); we derive virtual reserves so the engine's edge math (an
x*y=k optimal-size formula) applies uniformly across v3 and the Solidly
volatile pools on Base/Optimism.

Lazy web3 import — keeps this file importable in environments without
web3 installed (default cryptobot venv). is_available() returns False
when either web3 is missing or ARBITRUM_RPC_URL is unset, so the
connector silently drops out of REGISTERED_CONNECTORS-driven scans
without breaking the engine.
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


# Minimal ABIs — just the read methods the connector calls. Pinning these
# here (vs. depending on an external ABI registry) keeps the connector
# self-contained per Plugin Pattern Rule 2.
_UNISWAP_V3_POOL_ABI = [
    {"inputs": [], "name": "slot0",
     "outputs": [
         {"name": "sqrtPriceX96",               "type": "uint160"},
         {"name": "tick",                       "type": "int24"},
         {"name": "observationIndex",           "type": "uint16"},
         {"name": "observationCardinality",     "type": "uint16"},
         {"name": "observationCardinalityNext", "type": "uint16"},
         {"name": "feeProtocol",                "type": "uint8"},
         {"name": "unlocked",                   "type": "bool"},
     ], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "liquidity",
     "outputs": [{"name": "", "type": "uint128"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "fee",
     "outputs": [{"name": "", "type": "uint24"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0",
     "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1",
     "outputs": [{"name": "", "type": "address"}],
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


class ArbitrumConnector(BaseChainConnector):
    connector_id = "arbitrum"
    display_name = "Arbitrum"
    rpc_env_var  = "ARBITRUM_RPC_URL"
    optional     = True

    # Average gas units burned by a Uniswap v3 single-pool swap on
    # Arbitrum (operational measurement, not a guess — keep in this
    # constant so a calibration update is one edit).
    SWAP_GAS_UNITS = 150_000

    def __init__(self):
        cfg = settings.XCHAIN_VENUES.get(self.connector_id, {})
        # Build a VenueConfig per symbol the engine watches. The engine
        # always passes a symbol to get_pool_state, so the lookup is
        # constant-time per scan.
        self.venues: dict[str, VenueConfig] = {
            sym: VenueConfig(
                venue=cfg.get("venue", "uniswap_v3"),
                pool_address=cfg.get("pool_address", "<FILL>"),
                fee_bps=float(cfg.get("fee_bps", 5.0)),
            )
            for sym in settings.XCHAIN_SYMBOLS
        }
        self._w3 = None                      # lazy
        self._decimals_cache: dict[str, int] = {}

    # ── is_available — extends the base check with web3 importability ──

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
            pool = w3.eth.contract(address=venue.pool_address, abi=_UNISWAP_V3_POOL_ABI)
            slot0     = pool.functions.slot0().call()
            liquidity = int(pool.functions.liquidity().call())
            fee_raw   = int(pool.functions.fee().call())          # 1e-6 units
            token0    = pool.functions.token0().call()
            token1    = pool.functions.token1().call()
            block_num = int(w3.eth.block_number)
        except Exception as e:
            logger.debug("[%s] pool read failed: %s", self.connector_id, e)
            return self._error_state(symbol, f"pool_read_failed:{e}", venue=venue)

        # Decimals: WETH=18, USDC=6 on every L2 here. We still query when we
        # can — operator might pin a non-standard pool — but fall back fast.
        d0 = await self._decimals(token0, 18)
        d1 = await self._decimals(token1, 6)

        # spot_price = (sqrtP/2^96)^2 * 10^(d0-d1), interpreted as token1
        # per token0. We then orient it as USDC per WETH no matter which
        # side token0 lands on.
        sqrt_price_x96 = int(slot0[0])
        if sqrt_price_x96 == 0 or liquidity == 0:
            return self._error_state(symbol, "empty_pool", venue=venue, block=block_num)
        sqrt_p = sqrt_price_x96 / (2 ** 96)
        price_t1_per_t0 = (sqrt_p ** 2) * (10 ** (d0 - d1))

        # Virtual reserves: x = L / sqrtP, y = L * sqrtP. We then scale by
        # decimals to get human units (WETH and USDC).
        x_raw = liquidity / sqrt_p                      # token0 raw
        y_raw = liquidity * sqrt_p                      # token1 raw
        x_human = x_raw / (10 ** d0)
        y_human = y_raw / (10 ** d1)

        # Orient as base=WETH, quote=USDC (USDC always has the lower decimals
        # of the two for WETH-USDC pools).
        if d0 < d1:
            # token0 is the quote (USDC), token1 is the base (WETH).
            spot_usdc_per_weth = 1.0 / price_t1_per_t0 if price_t1_per_t0 else 0.0
            reserve_base  = y_human
            reserve_quote = x_human
        else:
            spot_usdc_per_weth = price_t1_per_t0
            reserve_base  = x_human
            reserve_quote = y_human

        # depth_usd_1pct on a constant-product pool: dy at 1% slippage is
        # approximately y * (sqrt(1.01) - 1) ≈ y * 0.004987 (≈ 0.5% of quote
        # reserves for a 1% price move on x*y=k).
        depth_1pct = reserve_quote * (math.sqrt(1.01) - 1.0)

        # Pool fee in bps. fee_raw is in 1e-6 units (Uniswap convention:
        # 500 = 0.05% = 5 bps). Engine math reads from PoolState.fee_bps.
        actual_fee_bps = fee_raw / 100.0

        return PoolState(
            connector_id=self.connector_id,
            symbol=symbol,
            venue=venue.venue,
            reserve_base=reserve_base,
            reserve_quote=reserve_quote,
            fee_bps=actual_fee_bps,
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
        """Per-swap gas in USD = gasPrice(wei) * SWAP_GAS_UNITS * ETH/USD / 1e18.

        ETH/USD comes from the same WETH-USDC pool's spot_price so we don't
        need a second data source. Returns inf on any failure — the engine
        treats inf as "this chain is too expensive right now" and skips.
        """
        w3 = self._resolve_w3()
        if w3 is None:
            return float("inf")
        try:
            gas_price_wei = int(w3.eth.gas_price)
        except Exception as e:
            logger.debug("[%s] gas_price failed: %s", self.connector_id, e)
            return float("inf")
        # Sample ETH/USD off the configured pool. If that fails (pool down)
        # the chain is unusable for this scan anyway — bail out as expensive.
        first_sym = settings.XCHAIN_SYMBOLS[0] if settings.XCHAIN_SYMBOLS else None
        if first_sym is None:
            return float("inf")
        state = await self.get_pool_state(first_sym)
        if state.error or state.spot_price <= 0:
            return float("inf")
        return (gas_price_wei * self.SWAP_GAS_UNITS / 1e18) * state.spot_price
