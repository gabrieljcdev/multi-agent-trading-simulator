"""
follow/labels.py

The exchange-address label set: load, lookup, and staleness metadata. This is
the ONE source of truth for "is this address an exchange / router / bridge /
multisig" — SHARED with the meme scorer's terminal stop-list. A flow TO a
labelled address is a pre-sell tell (transfer_in); a withdrawal FROM one is an
accumulation tell (transfer_out). Wallet-skill scoring also uses it as the
terminal stop-list (a hop into an exchange ends a wallet's traceable path).

Durable storage is the exchange_labels table (via database/queries.py). This
module owns the seed + the lookup/staleness API on top of it.

Honesty about freshness is load-bearing: exchanges rotate deposit addresses,
so a stale label set silently misses flow. The seed below is INTENTIONALLY
stamped with an old last_verified_at so label_set_staleness() reports the set
as stale until an operator (or an on-chain label feed) re-verifies it — the set
is never silently treated as fresh/authoritative.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from database import queries as q

logger = logging.getLogger(__name__)

# Illustrative seed of widely-published Solana CEX/bridge addresses. These are
# a STARTING POINT for observation, not verified ground truth — they are
# stamped stale (below) so the operator is prompted to verify before the set is
# trusted. Replace/extend via an on-chain label feed or operator entry.
# address -> (label, exchange_name)
_SEED_LABELS: dict[str, tuple[str, str]] = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": ("exchange", "binance"),
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": ("exchange", "binance"),
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": ("exchange", "coinbase"),
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": ("exchange", "coinbase"),
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": ("exchange", "kraken"),
    "3gd3dqgtJ4jWfBfLYTX67DALFetjc5iS72sCgRhCkW2u": ("bridge",   "wormhole"),
}

# Deliberately old: the seed is unverified, so the set must read as stale (not
# silently "clean") until re-verified. label_set_staleness() keys off this.
_SEED_VERIFIED_AT = datetime(2024, 1, 1)


def load_seed_labels() -> int:
    """Idempotently load the seed label set into the DB. Returns the number of
    addresses written. Safe to call on every start — upsert won't duplicate.
    Best-effort: a DB hiccup degrades to 0, never raises to the caller."""
    n = 0
    for address, (label, name) in _SEED_LABELS.items():
        try:
            if q.get_exchange_label(address) is None:
                q.upsert_exchange_label(
                    address, label, exchange_name=name, source="seed",
                    last_verified_at=_SEED_VERIFIED_AT)
                n += 1
        except Exception as e:
            logger.debug("load_seed_labels(%s): %s", address, e)
    return n


def lookup(address: str) -> Optional[dict]:
    """Label metadata for an address, or None if it is not a labelled
    terminal address. Never raises."""
    try:
        return q.get_exchange_label(address)
    except Exception as e:
        logger.debug("labels.lookup(%s): %s", address, e)
        return None


def is_terminal_address(address: str) -> bool:
    """True if the address is a labelled exchange/router/bridge/multisig — the
    SHARED terminal check used by both the flow classifier and the skill
    scorer's path-tracing stop-list."""
    try:
        return q.is_terminal_address(address)
    except Exception as e:
        logger.debug("labels.is_terminal_address(%s): %s", address, e)
        return False


def staleness() -> dict:
    """Coverage + staleness metadata (count, last_verified_at, age_days,
    is_stale). The web health panel surfaces this prominently."""
    try:
        return q.label_set_staleness()
    except Exception as e:
        logger.debug("labels.staleness: %s", e)
        return {"count": 0, "last_verified_at": None, "age_days": None,
                "warn_days": 7, "is_stale": True}


def is_available() -> bool:
    """True once the label set is non-empty — the watcher requires it to
    classify flow, so it is part of WalletFlowWatcher.is_available()."""
    try:
        return int(staleness().get("count", 0)) > 0
    except Exception:
        return False


__all__ = [
    "load_seed_labels", "lookup", "is_terminal_address",
    "staleness", "is_available",
]
