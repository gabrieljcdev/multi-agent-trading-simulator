"""
follow/skill_scorer.py

Point-in-time wallet-skill scoring. A wallet's score at time T uses ONLY
actions whose outcome was resolved STRICTLY before T — no look-ahead, ever.

CRITICAL: the point-in-time query is ONE shared function — resolved_actions_as_of.
The scorer, the auto-discovery pipeline, and the web as-of inspector all route
through it. There is no second "what did we know at T" implementation anywhere.

Discipline encoded here:
  - two clocks: every event has occurred_at (on-chain) and resolved_at (outcome
    known). resolved_actions_as_of(address, T) returns rows with resolved_at < T.
  - minimum RESOLVED sample fatal gate: below WALLETFLOW_MIN_RESOLVED_SAMPLE the
    result is NO-SIGNAL (signal=False), not a weak signal.
  - latency-δ: mean(detected_at - occurred_at). A wallet whose edge dies inside
    the followable window is flagged "real but unfollowable" rather than acted on.
  - first-mover ratio: fraction of WINS entered before the move (meta flag stamped
    at detection), the discriminator between genuine edge and momentum-chasing.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from config import settings
from database import queries as q

logger = logging.getLogger(__name__)


def resolved_actions_as_of(address: str, as_of: datetime) -> list[dict]:
    """THE single point-in-time query. Returns the wallet's actions whose
    outcome was resolved STRICTLY before `as_of` (resolved_at < as_of) — the
    only data a score computed at `as_of` is permitted to see.

    Reused verbatim by score_wallet, the discovery pipeline, and the web as-of
    reconstruction. Do NOT add a second as-of filter elsewhere."""
    rows = q.get_wallet_resolved_actions(address)   # raw datetimes, all resolved
    out = []
    for r in rows:
        ra = r.get("resolved_at")
        if ra is None:
            continue
        if ra < as_of:          # STRICTLY less — the no-leak boundary
            out.append(r)
    return out


def _latency_delta_s(actions: list[dict]) -> float:
    """Mean staleness between on-chain time and our observation time, seconds."""
    deltas = []
    for a in actions:
        occ, det = a.get("occurred_at"), a.get("detected_at")
        if occ is None or det is None:
            continue
        try:
            deltas.append((det - occ).total_seconds())
        except Exception:
            continue
    return (sum(deltas) / len(deltas)) if deltas else 0.0


def _first_mover_ratio(actions: list[dict]) -> float:
    """Fraction of WINS the wallet entered before the move/news. The
    first-mover flag is stamped on the event meta at detection time; absent a
    flag the win counts as NOT first-mover (conservative)."""
    wins = [a for a in actions if a.get("outcome") == "win"]
    if not wins:
        return 0.0
    first = sum(1 for a in wins if bool((a.get("meta") or {}).get("first_mover")))
    return first / len(wins)


def score_wallet(address: str, as_of: Optional[datetime] = None,
                 *, min_sample: Optional[int] = None) -> dict:
    """Point-in-time skill score for one wallet.

    Returns a dict. The fatal min-RESOLVED-sample gate yields signal=False with
    skill_score=None (NO-SIGNAL — not a weak signal). Above the gate, signal is
    True only if the wallet is also FOLLOWABLE (latency-δ within the followable
    window); a skilled-but-stale wallet is reported real_but_unfollowable=True
    with signal=False, surfaced as such rather than acted on."""
    as_of = as_of or datetime.utcnow()
    min_sample = (int(settings.WALLETFLOW_MIN_RESOLVED_SAMPLE)
                  if min_sample is None else int(min_sample))
    actions = resolved_actions_as_of(address, as_of)
    resolved_sample = len(actions)

    base = {
        "address":          address,
        "as_of":            as_of.isoformat(),
        "resolved_sample":  resolved_sample,
        "min_sample":       min_sample,
    }

    if resolved_sample < min_sample:
        # Fatal gate — NO-SIGNAL, not a weak signal.
        return {**base, "signal": False, "skill_score": None,
                "reason": "below_min_resolved_sample",
                "win_rate": None, "first_mover_ratio": None,
                "latency_delta_s": None, "followable": False,
                "real_but_unfollowable": False}

    wins = sum(1 for a in actions if a.get("outcome") == "win")
    win_rate = wins / resolved_sample
    skill_score = win_rate                      # 0..1, demonstrated track record
    first_mover_ratio = _first_mover_ratio(actions)
    latency_delta_s = _latency_delta_s(actions)
    followable = latency_delta_s <= float(settings.WALLETFLOW_MAX_FOLLOWABLE_DELTA_S)
    real_but_unfollowable = (not followable)

    return {
        **base,
        "signal":                bool(followable),
        "skill_score":           round(skill_score, 4),
        "win_rate":              round(win_rate, 4),
        "wins":                  wins,
        "first_mover_ratio":     round(first_mover_ratio, 4),
        "latency_delta_s":       round(latency_delta_s, 2),
        "followable":            bool(followable),
        "real_but_unfollowable": bool(real_but_unfollowable),
        "reason":                (None if followable else "edge_dies_within_delta"),
    }


__all__ = ["resolved_actions_as_of", "score_wallet"]
