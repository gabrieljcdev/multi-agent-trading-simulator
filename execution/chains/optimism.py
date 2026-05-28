"""
execution/chains/optimism.py

OptimismConnector — reads WETH-USDC from a Velodrome pool on Optimism.

Velodrome is the dominant Optimism DEX (and the Solidly fork that
inspired Aerodrome on Base). WETH-USDC sits in Velodrome's volatile
(x*y=k) branch with an ABI shape identical to Aerodrome/Uniswap v2.
All reading logic lives in _solidly_volatile.SolidlyVolatilePoolConnector;
this file specialises only the chain-identifying attrs.
"""

from __future__ import annotations

from execution.chains._solidly_volatile import SolidlyVolatilePoolConnector


class OptimismConnector(SolidlyVolatilePoolConnector):
    connector_id = "optimism"
    display_name = "Optimism (Velodrome)"
    rpc_env_var  = "OPTIMISM_RPC_URL"
    optional     = True


__all__ = ["OptimismConnector"]
