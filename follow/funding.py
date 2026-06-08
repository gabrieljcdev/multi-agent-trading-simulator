"""
follow/funding.py

Derived funding-source lookup over the free public RPC — replaces the old
one-call funding endpoint. A wallet's FIRST funder is found by paginating
getSignaturesForAddress back to the earliest, getTransaction on the oldest, and
returning the sender of the first inbound SOL transfer. Respects the labels
stop-list (a terminal exchange/router/bridge is a valid origin — we stop there,
we cannot trace inside a CEX).

Every RPC call goes through the shared rate-limited client (follow/rpc.py) — a
slow background crawl, never a burst. The first funder is immutable, so results
are cached hard (settings-driven TTL/size) to avoid re-crawling.

discovery.py reads the CACHE synchronously via cached_funder() (never network,
so discovery stays non-blocking and offline-testable); the async crawl that
populates the cache runs as the FollowAgent's slow background pass.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from config import settings
from follow import labels
from follow import sol_parse

logger = logging.getLogger(__name__)

# address -> (funder_or_None, stored_monotonic). Insertion-ordered for cheap
# oldest-first eviction when over the size cap.
_CACHE: dict[str, tuple[Optional[str], float]] = {}


def _ttl_seconds() -> float:
    return float(getattr(settings, "WALLETFLOW_FUNDING_CACHE_TTL_DAYS", 365)) * 86400.0


def _cache_max() -> int:
    return int(getattr(settings, "WALLETFLOW_FUNDING_CACHE_MAX", 50000))


def clear_cache() -> None:
    """Test hook / operator reset."""
    _CACHE.clear()


def cached_funder(address: str) -> Optional[str]:
    """SYNC cache-only read (never network). Returns the cached first funder,
    or None if not crawled yet or the entry has aged out. discovery uses this
    so it never blocks on or blasts the RPC."""
    entry = _CACHE.get(address)
    if entry is None:
        return None
    funder, ts = entry
    if (time.monotonic() - ts) > _ttl_seconds():
        _CACHE.pop(address, None)
        return None
    return funder


def is_cached(address: str) -> bool:
    return address in _CACHE


def _store(address: str, funder: Optional[str]) -> None:
    if address in _CACHE:
        _CACHE.pop(address, None)
    _CACHE[address] = (funder, time.monotonic())
    # Evict oldest while over cap.
    while len(_CACHE) > _cache_max():
        oldest = next(iter(_CACHE))
        _CACHE.pop(oldest, None)


async def first_funder(address: str, rpc, *,
                       is_terminal: Optional[Callable[[str], bool]] = None,
                       max_sigs: Optional[int] = None) -> Optional[str]:
    """Crawl public RPC for the wallet's first funder; cache and return it
    (None if none found). All RPC calls go through `rpc` (the shared throttled
    client). Cached results short-circuit with NO RPC call."""
    if address in _CACHE:
        return cached_funder(address)
    is_terminal = is_terminal or labels.is_terminal_address
    cap = int(getattr(settings, "WALLETFLOW_FUNDING_CRAWL_MAX_SIGS", 1000)
              if max_sigs is None else max_sigs)

    # Paginate signatures newest -> oldest, capped (slow crawl, one limiter).
    sigs: list[dict] = []
    before: Optional[str] = None
    try:
        while len(sigs) < cap:
            page = min(1000, cap - len(sigs))
            batch = await rpc.get_signatures_for_address(
                address, before=before, limit=page)
            if not batch:
                break
            sigs.extend(batch)
            before = batch[-1].get("signature")
            if len(batch) < page:
                break
    except Exception as e:
        logger.debug("funding: signature crawl for %s failed: %s", address, e)
        return cached_funder(address)

    # Oldest-first: the earliest inbound SOL transfer's sender is the funder.
    funder: Optional[str] = None
    for s in reversed(sigs):
        sig = s.get("signature")
        if not sig:
            continue
        try:
            tx = await rpc.get_transaction(sig)
        except Exception as e:
            logger.debug("funding: getTransaction %s failed: %s", sig, e)
            continue
        sender = sol_parse.first_inbound_sol_sender(tx or {}, address)
        if sender:
            funder = sender
            # Stop-list: a terminal origin (CEX/router/bridge) is where the
            # trace legitimately ends — we cannot see inside it.
            if is_terminal(sender):
                logger.debug("funding: %s funded from terminal %s (stop)",
                             address, sender)
            break

    _store(address, funder)
    return funder


__all__ = ["first_funder", "cached_funder", "is_cached", "clear_cache"]
