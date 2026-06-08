"""
follow/provenance.py

The watchlist provenance state machine + transitions. Built BEFORE discovery
because discovery may only ever drive ONE of these transitions
(propose -> candidate) and the machine is what guarantees it can do nothing
more.

States: "candidate" | "confirmed" | "manual" | "rejected"

HARD INVARIANT (enforced HERE and, defence-in-depth, in
queries.transition_provenance; covered by tests):
  - discovery may ONLY create rows in state "candidate"
  - ONLY an explicit operator action transitions candidate -> confirmed
  - rejected wallets are NEVER re-proposed by discovery (rejected is permanent)
  - the watcher emits/acts on signals ONLY from "manual" and "confirmed" wallets
  - "candidate" wallets are scored and displayed but NEVER influence a signal

Transitions:
  discovery -> candidate     (automatic, the ONLY automatic write discovery makes)
  candidate -> confirmed     (operator REST action ONLY)
  candidate -> rejected      (operator REST action ONLY; permanent)
  confirmed -> candidate     (automatic: trust expiry OR auto-demote breaker)
  manual                     (operator-created directly; behaves like confirmed)

All DB writes go through database/queries.py — this module is the policy
layer, queries.transition_provenance is the enforcement layer (they agree on
purpose).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from config import settings
from database import queries as q

logger = logging.getLogger(__name__)

# Canonical state vocabulary (queries.WALLET_PROVENANCE_STATES mirrors this).
CANDIDATE = "candidate"
CONFIRMED = "confirmed"
MANUAL    = "manual"
REJECTED  = "rejected"
STATES = (CANDIDATE, CONFIRMED, MANUAL, REJECTED)

# Actor classes permitted to drive a transition.
BY_DISCOVERY = "discovery"
BY_OPERATOR  = "operator"
BY_AUTO      = "auto"


def _trust_expiry_horizon(now: Optional[datetime] = None) -> datetime:
    """When a freshly-confirmed wallet's trust expires (the days arm; the
    actions arm is checked separately)."""
    now = now or datetime.utcnow()
    return now + timedelta(days=int(settings.WALLETFLOW_TRUST_EXPIRY_DAYS))


# ── State-changing operations (the only sanctioned transitions) ─────────────

def propose_candidate(address: str) -> dict:
    """Discovery's ONLY write: create (or no-op on) a "candidate" row.

    Refuses outright if the address was previously rejected — rejected wallets
    are never re-proposed. Refuses if the address is already known in any
    non-candidate state (discovery never promotes an existing wallet). Returns
    an envelope {"ok": bool, ...}; never raises."""
    entry = q.get_watchlist_entry(address)
    if entry is not None:
        if entry["provenance"] == REJECTED:
            return {"ok": False, "error": "rejected_permanent", "address": address}
        # Already on the watchlist in some state — discovery does not re-touch it.
        return {"ok": False, "error": "already_watchlisted",
                "address": address, "provenance": entry["provenance"]}
    return q.transition_provenance(address, CANDIDATE, by=BY_DISCOVERY)


def confirm(address: str) -> dict:
    """Operator action: candidate -> confirmed. The ONLY path to "confirmed".
    Sets the trust-expiry horizon. by is hard-wired to operator — there is no
    parameter that lets discovery reach this."""
    return q.transition_provenance(
        address, CONFIRMED, by=BY_OPERATOR,
        expires_at=_trust_expiry_horizon())


def reject(address: str) -> dict:
    """Operator action: candidate -> rejected (permanent; never re-proposed)."""
    res = q.transition_provenance(address, REJECTED, by=BY_OPERATOR)
    if res.get("ok"):
        # Close out any pending discovery proposals so the review queue and
        # discovery both stop surfacing the address.
        try:
            q.set_candidate_review_state(address, REJECTED)
        except Exception as e:
            logger.debug("reject(%s) candidate cleanup: %s", address, e)
    return res


def add_manual(address: str) -> dict:
    """Operator-created watchlist entry — behaves like confirmed for signals."""
    return q.transition_provenance(address, MANUAL, by=BY_OPERATOR)


def demote(address: str, reason: str) -> dict:
    """Automatic confirmed/manual -> candidate (trust expiry / breaker). by is
    "auto" — this is the only non-operator move into candidate from a trusted
    state."""
    return q.transition_provenance(address, CANDIDATE, by=BY_AUTO, reason=reason)


# ── Read helpers ────────────────────────────────────────────────────────────

def is_rejected(address: str) -> bool:
    entry = q.get_watchlist_entry(address)
    return entry is not None and entry["provenance"] == REJECTED


def is_signal_eligible(address: str) -> bool:
    """True only for manual + confirmed wallets — the invariant the watcher
    consults before EVER emitting a signal off a wallet's activity."""
    entry = q.get_watchlist_entry(address)
    return entry is not None and entry["provenance"] in (MANUAL, CONFIRMED)


# ── Trust lifecycle enforcement (auto transitions) ──────────────────────────

def trust_expired(entry: dict, now: Optional[datetime] = None) -> Optional[str]:
    """Return the expiry reason if a confirmed wallet's trust has lapsed
    (days OR actions, whichever first), else None. Operates on a watchlist
    dict so tests can drive it directly."""
    if entry.get("provenance") != CONFIRMED:
        return None
    now = now or datetime.utcnow()
    expires_at = entry.get("expires_at")
    if expires_at:
        try:
            horizon = datetime.fromisoformat(expires_at)
            if now >= horizon:
                return "trust_expiry_days"
        except (TypeError, ValueError):
            pass
    actions = int(entry.get("actions_since_confirm") or 0)
    if actions >= int(settings.WALLETFLOW_TRUST_EXPIRY_ACTIONS):
        return "trust_expiry_actions"
    return None


def enforce_trust_expiry(address: str,
                         now: Optional[datetime] = None) -> Optional[dict]:
    """Demote a confirmed wallet to candidate if its trust has expired.
    Returns the transition envelope when it fires, else None."""
    entry = q.get_watchlist_entry(address)
    if entry is None:
        return None
    reason = trust_expired(entry, now=now)
    if reason is None:
        return None
    logger.info("walletflow: %s trust expired (%s) -> candidate", address, reason)
    return demote(address, reason)


def enforce_demote_breaker(address: str,
                           recent_outcomes: list[str]) -> Optional[dict]:
    """Auto-demote breaker: a confirmed wallet whose recent emitted signals
    resolve badly past WALLETFLOW_DEMOTE_BAD_STREAK consecutive losses is
    demoted to candidate + flagged. recent_outcomes is newest-first
    ("win"/"loss"/"flat"). Returns the transition envelope when it fires."""
    entry = q.get_watchlist_entry(address)
    if entry is None or entry.get("provenance") != CONFIRMED:
        return None
    streak = 0
    for o in recent_outcomes:
        if o == "loss":
            streak += 1
        else:
            break
    if streak >= int(settings.WALLETFLOW_DEMOTE_BAD_STREAK):
        logger.warning("walletflow: %s bad streak %d -> auto-demote",
                       address, streak)
        return demote(address, f"demote_breaker:{streak}")
    return None


__all__ = [
    "CANDIDATE", "CONFIRMED", "MANUAL", "REJECTED", "STATES",
    "BY_DISCOVERY", "BY_OPERATOR", "BY_AUTO",
    "propose_candidate", "confirm", "reject", "add_manual", "demote",
    "is_rejected", "is_signal_eligible",
    "trust_expired", "enforce_trust_expiry", "enforce_demote_breaker",
]
