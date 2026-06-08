"""
follow/helius_parse.py

SHARED Helius transaction-parse helper. Turns one Helius "enhanced
transaction" payload into ActorEvents for the wallets we care about. Pure and
side-effect-free (no network, no DB, no clock) so it is trivially testable and
reusable — the meme-wallet source parses the same shape through this helper
rather than duplicating the logic.

Classification (Tier 1 events, all directly observable):
  - a watched wallet sends tokens TO a labelled exchange address  -> transfer_in
    (pre-sell tell)
  - a watched wallet receives tokens FROM a labelled exchange      -> transfer_out
    (withdrawal / accumulation tell)
  - a watched wallet is the feePayer of a SWAP                      -> swap
  - add/remove liquidity by a watched wallet                       -> lp_add/lp_remove

Flow is SUGGESTIVE, never deterministic — every event is an observation of an
on-chain action, not an inference of intent.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from follow.base import (
    ActorEvent,
    ACTION_SWAP, ACTION_TRANSFER_IN, ACTION_TRANSFER_OUT,
    ACTION_LP_ADD, ACTION_LP_REMOVE,
)

logger = logging.getLogger(__name__)

# Helius enhanced-tx `type` values we map to lp_add / lp_remove.
_LP_ADD_TYPES    = {"ADD_LIQUIDITY", "DEPOSIT"}
_LP_REMOVE_TYPES = {"REMOVE_LIQUIDITY", "WITHDRAW"}


def parse_transaction(
    tx: dict,
    watchlist: set[str],
    is_terminal: Callable[[str], bool],
    *,
    detected_at: float,
    label_name: Optional[Callable[[str], Optional[str]]] = None,
    source_id: str = "wallet_flow",
) -> list[ActorEvent]:
    """Parse one enhanced-tx dict into ActorEvents for watched wallets.

    watchlist   — addresses we track (only events touching one are emitted).
    is_terminal — callable(address) -> bool: is this a labelled exchange/router/
                  bridge address (follow/labels.is_terminal_address).
    detected_at — epoch seconds WE observed the tx (T + delta); the caller
                  stamps it so this stays clock-free and testable.
    label_name  — optional callable(address) -> exchange name for the venue field.

    Never raises — a malformed payload yields [] (graceful degradation).
    """
    if not isinstance(tx, dict):
        return []
    out: list[ActorEvent] = []
    try:
        occurred_at = float(tx.get("timestamp") or 0.0)
        tx_type = str(tx.get("type") or "").upper()
        fee_payer = tx.get("feePayer")

        def _venue(addr: str) -> Optional[str]:
            if label_name is None:
                return None
            try:
                return label_name(addr)
            except Exception:
                return None

        # Token transfers — the exchange-flow tells.
        for tr in (tx.get("tokenTransfers") or []):
            frm = tr.get("fromUserAccount")
            to = tr.get("toUserAccount")
            mint = tr.get("mint")
            amount = tr.get("tokenAmount")
            usd = tr.get("usdValue") or (tr.get("rawTokenAmount") or {}).get("usdValue")
            if frm in watchlist and to and is_terminal(to):
                out.append(ActorEvent(
                    source_id=source_id, actor_id=frm,
                    action=ACTION_TRANSFER_IN, asset=mint, venue=_venue(to),
                    size_usd=(float(usd) if usd is not None else None),
                    detected_at=detected_at, occurred_at=occurred_at,
                    meta={"counterparty": to, "mint": mint, "amount": amount,
                          "signature": tx.get("signature")},
                ))
            elif to in watchlist and frm and is_terminal(frm):
                out.append(ActorEvent(
                    source_id=source_id, actor_id=to,
                    action=ACTION_TRANSFER_OUT, asset=mint, venue=_venue(frm),
                    size_usd=(float(usd) if usd is not None else None),
                    detected_at=detected_at, occurred_at=occurred_at,
                    meta={"counterparty": frm, "mint": mint, "amount": amount,
                          "signature": tx.get("signature")},
                ))

        # Swap / LP action by a watched wallet (feePayer).
        if fee_payer in watchlist:
            action = None
            asset = None
            swap = (tx.get("events") or {}).get("swap")
            if tx_type == "SWAP" or swap:
                action = ACTION_SWAP
                if swap:
                    outs = swap.get("tokenOutputs") or []
                    if outs:
                        asset = outs[0].get("mint")
            elif tx_type in _LP_ADD_TYPES:
                action = ACTION_LP_ADD
            elif tx_type in _LP_REMOVE_TYPES:
                action = ACTION_LP_REMOVE
            if action is not None:
                out.append(ActorEvent(
                    source_id=source_id, actor_id=fee_payer,
                    action=action, asset=asset, venue=None,
                    size_usd=None,
                    detected_at=detected_at, occurred_at=occurred_at,
                    meta={"type": tx_type, "signature": tx.get("signature")},
                ))
    except Exception as e:
        logger.debug("helius parse failed: %s", e)
        return []
    return out


__all__ = ["parse_transaction"]
