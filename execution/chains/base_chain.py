"""
execution/chains/base_chain.py

BaseChainConnectorInstance — reads WETH-USDC from an Aerodrome pool on Base.

File name is base_chain.py (NOT base.py) so the import never collides with
execution/chains/base_connector.py: BaseChainConnector (the ABC) vs
BaseChainConnectorInstance (Base-chain Aerodrome connector). Same naming
discipline as keeping "Base" the L2 distinct from "Base" the class word.

Aerodrome is a Solidly fork — WETH-USDC sits in the volatile (x*y=k)
branch, with the same getReserves() ABI as Uniswap v2. All reading
logic lives in _solidly_volatile.SolidlyVolatilePoolConnector; this file
only specialises connector_id / display_name / rpc_env_var. Aerodrome
runs 50–63% of Base DEX volume, which is why it's the chosen venue.
"""

from __future__ import annotations

from execution.chains._solidly_volatile import SolidlyVolatilePoolConnector


class BaseChainConnectorInstance(SolidlyVolatilePoolConnector):
    connector_id = "base"
    display_name = "Base (Aerodrome)"
    rpc_env_var  = "BASE_RPC_URL"
    optional     = True


__all__ = ["BaseChainConnectorInstance"]
