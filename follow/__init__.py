"""
follow/

Capital-FREE OBSERVER sources that watch on-chain actors (wallets) and surface
what they do — never trading on it. Hosted by the FollowAgent, which is
registered with the coordinator as a $0 observer (no capital, no positions, no
execution path).

═══════════════════════════════════════════════════════════════════════
To add a new follow source (one file + one line)
═══════════════════════════════════════════════════════════════════════

1. Create follow/my_source.py
2. Subclass BaseStreamingDataSource (from follow.base)
3. Set the class attrs:
       source_id    = "my_source"
       display_name = "My Source"
       optional     = True
4. Implement: async start(), async stop(), get_stats()
       and is_available() if it needs keys/deps
   (there is NO submit/execute method — follow sources are observers)
5. Append an instance of your class to REGISTERED_FOLLOW_SOURCES below

The FollowAgent imports only BaseStreamingDataSource + this list, never a
concrete source by name (PLUGIN_PATTERN.md). It starts only available sources
and never grants any of them capital or trading authority.
"""

from __future__ import annotations

from follow.base import ActorEvent, BaseStreamingDataSource
from follow.wallet_flow import WalletFlowWatcher


REGISTERED_FOLLOW_SOURCES: list[BaseStreamingDataSource] = [
    WalletFlowWatcher(),
    # Add new follow sources here (instances). e.g. the meme-wallet scorer,
    # which will REUSE follow/helius_parse.py + follow/labels.py.
]

__all__ = [
    "REGISTERED_FOLLOW_SOURCES",
    "BaseStreamingDataSource",
    "ActorEvent",
    "WalletFlowWatcher",
]
