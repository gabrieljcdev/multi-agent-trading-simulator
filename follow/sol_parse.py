"""
follow/sol_parse.py

Pure raw-instruction decoder over STANDARD Solana RPC transaction JSON
(getTransaction / logsSubscribe-then-getTransaction payloads, `jsonParsed`
encoding). Was follow/helius_parse.py — no longer Helius-specific now that the
data layer rides the free public RPC. Still a PURE function module: no network,
no DB, no clock; fully unit-testable on captured fixtures.

CAPABILITY IS BROAD, decoding into ActorEvent.action of:
  - "transfer_in"  — a watched wallet sends to a labelled exchange (pre-sell tell)
  - "transfer_out" — a watched wallet receives from a labelled exchange (accumulation)
  - "swap"         — a watched wallet is the feePayer of a known-DEX instruction
  - "lp_add"       — add-liquidity on a known DEX
  - "launch"       — a launchpad (pump.fun) mint/create by a watched wallet
  - "unparsed"     — fallback: a watched account touched a program we don't decode;
                     the raw program id(s) ride in meta for later triage. NEVER
                     dropped silently.

SUBSCRIPTION (in wallet_flow.py) stays NARROW — this capability is only ever
applied to transactions that already touch a watched/labelled address. The
launchpad format is decoded WHEN IT APPEARS in a watched tx, NOT by subscribing
to the launchpad firehose.

SPL note: raw RPC exposes token ACCOUNTS + the `authority` (the signing owner),
not always the destination's owner wallet. We classify on the authority (the
acting wallet) and the labelled counterparty; full destination-owner resolution
would need extra getAccountInfo calls and is deliberately deferred (operators
may also label token accounts). SOL (system) transfers carry wallet addresses
directly and classify fully.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from config import settings
from follow.base import (
    ActorEvent,
    ACTION_SWAP, ACTION_TRANSFER_IN, ACTION_TRANSFER_OUT, ACTION_LP_ADD,
)

logger = logging.getLogger(__name__)

# Action for launchpad mint/create + the unknown-program fallback.
ACTION_LAUNCH   = "launch"
ACTION_UNPARSED = "unparsed"

SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
TOKEN_PROGRAM_ID  = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# name -> on-chain program id. Which of these are ACTIVE is settings-driven
# (WALLETFLOW_PARSE_DEXES / WALLETFLOW_PARSE_LAUNCHPADS); anything not active and
# not a system/token transfer falls through to "unparsed".
DEX_PROGRAM_IDS = {
    "raydium": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",   # Raydium AMM v4
    "orca":    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",    # Orca Whirlpools
}
LAUNCHPAD_PROGRAM_IDS = {
    "pumpfun": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",    # pump.fun
}


# ── Low-level shape helpers (tolerant of jsonParsed + legacy shapes) ─────────

def _account_keys(tx: dict) -> list[dict]:
    msg = ((tx.get("transaction") or {}).get("message") or {})
    keys = msg.get("accountKeys") or []
    out = []
    for k in keys:
        if isinstance(k, dict):
            out.append(k)
        else:
            out.append({"pubkey": k, "signer": False})
    return out


def _fee_payer(tx: dict) -> Optional[str]:
    keys = _account_keys(tx)
    for k in keys:
        if k.get("signer"):
            return k.get("pubkey")
    return keys[0].get("pubkey") if keys else None


def _instructions(tx: dict) -> list[dict]:
    msg = ((tx.get("transaction") or {}).get("message") or {})
    return list(msg.get("instructions") or [])


def _ix_program_id(ix: dict) -> Optional[str]:
    pid = ix.get("programId")
    if pid:
        return pid
    prog = ix.get("program")
    if prog == "system":
        return SYSTEM_PROGRAM_ID
    if prog in ("spl-token", "spl-token-2022"):
        return TOKEN_PROGRAM_ID
    return None


def _ix_accounts(ix: dict) -> list[str]:
    return [a for a in (ix.get("accounts") or []) if isinstance(a, str)]


def sol_transfers(tx: dict) -> list[dict]:
    """Every native-SOL system transfer in a tx as {source, destination,
    lamports}. Used by both the watcher classifier and the funding crawler."""
    out = []
    for ix in _instructions(tx):
        if ix.get("program") != "system":
            continue
        parsed = ix.get("parsed") or {}
        if parsed.get("type") not in ("transfer", "transferWithSeed"):
            continue
        info = parsed.get("info") or {}
        src, dst = info.get("source"), info.get("destination")
        if src and dst:
            out.append({"source": src, "destination": dst,
                        "lamports": info.get("lamports")})
    return out


def _spl_transfers(tx: dict) -> list[dict]:
    """SPL token transfers as {authority, source, destination, mint, amount}."""
    out = []
    for ix in _instructions(tx):
        if ix.get("program") not in ("spl-token", "spl-token-2022"):
            continue
        parsed = ix.get("parsed") or {}
        if parsed.get("type") not in ("transfer", "transferChecked"):
            continue
        info = parsed.get("info") or {}
        amount = info.get("amount")
        if amount is None:
            amount = (info.get("tokenAmount") or {}).get("amount")
        out.append({
            "authority":   info.get("authority") or info.get("multisigAuthority"),
            "source":      info.get("source"),
            "destination": info.get("destination"),
            "mint":        info.get("mint"),
            "amount":      amount,
        })
    return out


def _active_program_ids(names_setting: str, registry: dict,
                        override) -> set[str]:
    names = override if override is not None else getattr(settings, names_setting, [])
    return {registry[n] for n in names if n in registry}


# ── The decoder ──────────────────────────────────────────────────────────────

def parse_transaction(
    tx: dict,
    watchlist: set[str],
    is_terminal: Callable[[str], bool],
    *,
    detected_at: float,
    label_name: Optional[Callable[[str], Optional[str]]] = None,
    source_id: str = "wallet_flow",
    dexes: Optional[list[str]] = None,
    launchpads: Optional[list[str]] = None,
) -> list[ActorEvent]:
    """Decode one standard Solana RPC tx dict into ActorEvents for watched
    wallets. Never raises — a malformed payload yields [] (graceful
    degradation). An unrecognised program touching a watched account yields an
    "unparsed" event (never silently dropped)."""
    if not isinstance(tx, dict):
        return []
    out: list[ActorEvent] = []
    try:
        occurred_at = float(tx.get("blockTime") or 0.0)
        fee_payer = _fee_payer(tx)
        dex_ids = _active_program_ids("WALLETFLOW_PARSE_DEXES",
                                      DEX_PROGRAM_IDS, dexes)
        lp_ids = _active_program_ids("WALLETFLOW_PARSE_LAUNCHPADS",
                                     LAUNCHPAD_PROGRAM_IDS, launchpads)

        def _venue(addr: str) -> Optional[str]:
            if label_name is None:
                return None
            try:
                return label_name(addr)
            except Exception:
                return None

        def _classify_transfer(actor_a, actor_b, asset, amount):
            # a -> b. transfer_in: watched sends to a terminal exchange.
            #         transfer_out: watched receives from a terminal exchange.
            if actor_a in watchlist and actor_b and is_terminal(actor_b):
                out.append(ActorEvent(
                    source_id=source_id, actor_id=actor_a,
                    action=ACTION_TRANSFER_IN, asset=asset, venue=_venue(actor_b),
                    size_usd=None, detected_at=detected_at, occurred_at=occurred_at,
                    meta={"counterparty": actor_b, "amount": amount,
                          "signature": _sig(tx)}))
            elif actor_b in watchlist and actor_a and is_terminal(actor_a):
                out.append(ActorEvent(
                    source_id=source_id, actor_id=actor_b,
                    action=ACTION_TRANSFER_OUT, asset=asset, venue=_venue(actor_a),
                    size_usd=None, detected_at=detected_at, occurred_at=occurred_at,
                    meta={"counterparty": actor_a, "amount": amount,
                          "signature": _sig(tx)}))

        # SOL transfers (wallet addresses).
        for t in sol_transfers(tx):
            _classify_transfer(t["source"], t["destination"], "SOL", t["lamports"])
        # SPL transfers (classify on the acting authority + labelled counterparty).
        for t in _spl_transfers(tx):
            _classify_transfer(t.get("authority"), t.get("destination"),
                               t.get("mint"), t.get("amount"))

        # DEX / launchpad / unknown — scan instructions by program id.
        unknown_pids: list[str] = []
        for ix in _instructions(tx):
            pid = _ix_program_id(ix)
            if pid in (SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID):
                continue                                   # handled above
            actor = fee_payer if fee_payer in watchlist else \
                next((a for a in _ix_accounts(ix) if a in watchlist), None)
            if actor is None:
                continue
            if pid in dex_ids:
                out.append(ActorEvent(
                    source_id=source_id, actor_id=actor, action=ACTION_SWAP,
                    asset=None, venue=None, size_usd=None,
                    detected_at=detected_at, occurred_at=occurred_at,
                    meta={"program_id": pid, "signature": _sig(tx)}))
            elif pid in lp_ids:
                out.append(ActorEvent(
                    source_id=source_id, actor_id=actor, action=ACTION_LAUNCH,
                    asset=None, venue=None, size_usd=None,
                    detected_at=detected_at, occurred_at=occurred_at,
                    meta={"program_id": pid, "launchpad": True,
                          "signature": _sig(tx)}))
            elif pid:
                unknown_pids.append(pid)

        # Fallback — a watched account touched a program we don't decode, and
        # nothing else fired for it: emit ONE "unparsed" event (never dropped).
        if unknown_pids and not out:
            actor = fee_payer if fee_payer in watchlist else \
                next((k["pubkey"] for k in _account_keys(tx)
                      if k.get("pubkey") in watchlist), None)
            if actor is not None:
                out.append(ActorEvent(
                    source_id=source_id, actor_id=actor, action=ACTION_UNPARSED,
                    asset=None, venue=None, size_usd=None,
                    detected_at=detected_at, occurred_at=occurred_at,
                    meta={"program_ids": sorted(set(unknown_pids)),
                          "signature": _sig(tx)}))
    except Exception as e:
        logger.debug("sol parse failed: %s", e)
        return []
    return out


def _sig(tx: dict) -> Optional[str]:
    sigs = (tx.get("transaction") or {}).get("signatures")
    if sigs:
        return sigs[0]
    return tx.get("signature")


def first_inbound_sol_sender(tx: dict, address: str) -> Optional[str]:
    """The source of the first inbound SOL transfer to `address` in this tx, or
    None. Pure — used by the funding-source crawler."""
    for t in sol_transfers(tx):
        if t.get("destination") == address and t.get("source"):
            return t["source"]
    return None


# ── Launchpad + rug-event decoding (meme scorer; REUSES the helpers above) ───────
# These ADD launch/rug decoding capability to the module that already owns Solana
# decoding — they do NOT rewrite parse_transaction's watched-wallet ACTION_LAUNCH
# flag (that stays as-is). They reuse the SAME launchpad program-id registry
# (LAUNCHPAD_PROGRAM_IDS), instruction iteration, and SPL helpers, and stay PURE
# (no network/DB/clock). The meme LaunchSource samples the launchpad and calls
# these; the rug helpers feed the maturation/labelling layer.

def _mint_initialized(tx: dict) -> Optional[str]:
    """The mint of the first initializeMint(2) SPL instruction in the tx, if any.
    A launchpad create mints a new token, so this is the most reliable mint id."""
    for ix in _instructions(tx):
        if ix.get("program") not in ("spl-token", "spl-token-2022"):
            continue
        parsed = ix.get("parsed") or {}
        if parsed.get("type") in ("initializeMint", "initializeMint2"):
            mint = (parsed.get("info") or {}).get("mint")
            if mint:
                return mint
    return None


def early_buyer(tx: dict, mint: str) -> Optional[str]:
    """Best-effort: the wallet that BOUGHT `mint` in this tx — the fee payer of a
    tx that moves the token, EXCLUDING the launchpad create itself (the mint
    initialization is the creator, not a buyer). Returns None if the tx is the
    create, doesn't touch `mint`, or is unparseable. PURE + tolerant (no network
    / DB / clock) — the meme scorer's early-buyer derivation layers the funder
    lookup on top of this. The fee payer is used (not the SPL destination, which
    is a token account, not the owner wallet)."""
    if not isinstance(tx, dict) or not mint:
        return None
    try:
        if _mint_initialized(tx) == mint:
            return None                              # the create tx, not a buy
        if not any(t.get("mint") == mint for t in _spl_transfers(tx)):
            return None                              # tx doesn't move this token
        return _fee_payer(tx)
    except Exception:
        return None


def launch_events(tx: dict, *, launchpads: Optional[list[str]] = None) -> list[dict]:
    """Decode a launchpad (pump.fun) create tx into new-launch records:
    {mint, creator, launchpad, signature}. PURE + tolerant — a malformed or
    non-launch tx yields [] (graceful degradation), so the sampling LaunchSource
    can hand it raw RPC payloads. The mint is taken from the initializeMint
    instruction when present, else the first account of the launchpad instruction
    (best-effort without the launchpad IDL — documented). creator = fee payer."""
    if not isinstance(tx, dict):
        return []
    out: list[dict] = []
    try:
        lp_ids = _active_program_ids("WALLETFLOW_PARSE_LAUNCHPADS",
                                     LAUNCHPAD_PROGRAM_IDS, launchpads)
        if not lp_ids:
            return []
        creator = _fee_payer(tx)
        sig = _sig(tx)
        init_mint = _mint_initialized(tx)
        for ix in _instructions(tx):
            pid = _ix_program_id(ix)
            if pid not in lp_ids:
                continue
            accts = _ix_accounts(ix)
            mint = init_mint or (accts[0] if accts else None)
            if not mint:
                continue
            out.append({"mint": mint, "creator": creator,
                        "launchpad": pid, "signature": sig})
            break          # one launch per tx (a create mints one token)
    except Exception as e:
        logger.debug("launch_events parse failed: %s", e)
        return []
    return out


# Irreversible rug-event types — these label a launch instantly + HARD (the
# ground-truth set). They are detected from the SAME decode primitives, never
# inferred from gameable floors.
RUG_LP_REMOVE        = "lp_remove"        # liquidity pulled / yanked
RUG_MINT_AUTH_ABUSE  = "mint_auth_abuse"  # post-launch mint-authority mint/assign
RUG_DEV_DUMP         = "dev_dump"         # creator dumps full token allocation


def rug_events(tx: dict, *, mint: Optional[str] = None,
               creator: Optional[str] = None) -> list[dict]:
    """Detect IRREVERSIBLE rug events in a tx for a given mint/creator. Returns a
    list of {type, signature} for liquidity removal, mint-authority abuse, or a
    full dev dump. PURE + tolerant (malformed tx -> []). These are the HARD
    ground-truth labels; slow-death (soft) is handled separately by the
    sustained-floor labeller, NOT here."""
    if not isinstance(tx, dict):
        return []
    out: list[dict] = []
    try:
        sig = _sig(tx)
        # Mint-authority abuse: a post-launch mintTo / setAuthority on the mint.
        for ix in _instructions(tx):
            if ix.get("program") not in ("spl-token", "spl-token-2022"):
                continue
            parsed = ix.get("parsed") or {}
            ptype = parsed.get("type")
            info = parsed.get("info") or {}
            if mint is not None and info.get("mint") not in (mint, None):
                continue
            if ptype in ("mintTo", "mintToChecked"):
                out.append({"type": RUG_MINT_AUTH_ABUSE, "signature": sig})
            elif ptype == "setAuthority" and info.get("authorityType") == "mintTokens":
                out.append({"type": RUG_MINT_AUTH_ABUSE, "signature": sig})
        # Liquidity removal: an lp_remove on a known DEX (the launch's pool).
        for ix in _instructions(tx):
            pid = _ix_program_id(ix)
            if pid in _active_program_ids("WALLETFLOW_PARSE_DEXES",
                                          DEX_PROGRAM_IDS, None):
                parsed = ix.get("parsed") or {}
                if parsed.get("type") in ("withdraw", "removeLiquidity", "lp_remove"):
                    out.append({"type": RUG_LP_REMOVE, "signature": sig})
        # Dev dump: the creator is the SPL transfer authority sending the token out.
        if creator is not None:
            for t in _spl_transfers(tx):
                if t.get("authority") == creator and \
                        (mint is None or t.get("mint") == mint):
                    out.append({"type": RUG_DEV_DUMP, "signature": sig})
                    break
    except Exception as e:
        logger.debug("rug_events parse failed: %s", e)
        return []
    return out


__all__ = [
    "parse_transaction", "sol_transfers", "first_inbound_sol_sender",
    "launch_events", "rug_events",
    "RUG_LP_REMOVE", "RUG_MINT_AUTH_ABUSE", "RUG_DEV_DUMP",
    "ACTION_LAUNCH", "ACTION_UNPARSED",
    "DEX_PROGRAM_IDS", "LAUNCHPAD_PROGRAM_IDS",
    "SYSTEM_PROGRAM_ID", "TOKEN_PROGRAM_ID",
]
