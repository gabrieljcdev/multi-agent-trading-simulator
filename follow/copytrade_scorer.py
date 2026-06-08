"""
follow/copytrade_scorer.py

Survival-aware + latency-aware skill scoring for ranked on-chain perp traders.
The point-in-time discipline is IDENTICAL to the wallet watcher's and shares its
ONE implementation: every skill number uses ONLY outcomes resolved STRICTLY
before the scoring moment, via skill_scorer.resolved_actions_as_of. That function
is imported by reference and re-exported here — it is NOT re-implemented (asserted
by an import-identity test). A copy-trade actor's resolved track record lives in
the SHARED wallet_flow_events log (source_id="copytrade"); the scorer reads it
through that one no-leak filter, exactly as the wallet scorer does.

Discipline encoded here (the honest-measurement core of this observer):
  - SURVIVAL-AWARE: the sample is the actor's RESOLVED CLOSED trades. Below
    COPYTRADE_MIN_CLOSED_TRADES the result is NO-SIGNAL (signal=False,
    skill_score=None) — a fatal gate, never a weak signal. ROI is never trusted
    before sample size and drawdown.
  - DRAWDOWN: a point-in-time drawdown proxy over the resolved outcome curve.
    Past COPYTRADE_MAX_DRAWDOWN the actor is rejected (risk too hot to call skill).
  - LATENCY-δ: mean(detected_at - occurred_at). An actor whose edge dies inside
    COPYTRADE_MAX_FOLLOWABLE_DELTA_S is "real but unfollowable" — surfaced as
    such (real_but_unfollowable=True, signal=False), never emitted as followable.
  - SIZING-INTERPRETABLE: True for on-chain perps (true notional is public).
    CEX ROI%-only leaderboards would be False — which is why CEX is out of v1.

The realistic output of this scorer is "this actor is genuinely skilled"
(corroboration), rarely "follow this trade now": latency usually eats the edge.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from config import settings
# Bind the SHARED point-in-time function by reference — the scorer routes through
# the ONE definition in skill_scorer, never a copy (asserted by import identity).
from follow.skill_scorer import resolved_actions_as_of

logger = logging.getLogger(__name__)

# Rejection reasons recorded in the denominator log — the WHY behind every N−M.
REASON_BELOW_MIN_CLOSED   = "below_min_closed_trades"
REASON_DELTA_TOO_HIGH     = "delta_too_high"
REASON_DRAWDOWN_TOO_DEEP  = "drawdown_too_deep"
REASON_SIZING_OPAQUE      = "sizing_uninterpretable"


def _latency_delta_s(actions: list[dict]) -> float:
    """Mean staleness between venue/block time and our observation time, seconds.
    Mirrors the wallet scorer's δ so followability is graded the same way."""
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


def _max_drawdown(actions: list[dict]) -> float:
    """Point-in-time drawdown proxy over the resolved outcome curve (win=+1,
    loss=-1, flat=0). Peak-to-trough drop normalised by the running peak, clamped
    to [0, 1]. Uses ONLY the resolved-as-of actions handed in — so it inherits the
    no-leak boundary of resolved_actions_as_of (no look-ahead)."""
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for a in actions:
        o = a.get("outcome")
        cum += 1.0 if o == "win" else (-1.0 if o == "loss" else 0.0)
        peak = max(peak, cum)
        if peak > 0:
            max_dd = max(max_dd, (peak - cum) / peak)
    return round(min(1.0, max_dd), 4)


def score_actor(actor_id: str, as_of: Optional[datetime] = None,
                *, min_sample: Optional[int] = None,
                sizing_interpretable: bool = True) -> dict:
    """Point-in-time survival + latency skill for one ranked actor.

    Returns a dict. The fatal min-CLOSED-trades gate yields signal=False with
    skill_score=None (NO-SIGNAL). Above the gate, signal is True ONLY when the
    actor also survives the drawdown gate AND is FOLLOWABLE (latency-δ within the
    window); a skilled-but-stale actor is reported real_but_unfollowable=True with
    signal=False — surfaced honestly, never acted on. `reason` carries the
    denominator-log rejection code on every non-signal path."""
    as_of = as_of or datetime.utcnow()
    min_sample = (int(settings.COPYTRADE_MIN_CLOSED_TRADES)
                  if min_sample is None else int(min_sample))
    # ONE shared no-leak read of the actor's resolved closed trades.
    actions = resolved_actions_as_of(actor_id, as_of)
    closed_trades = len(actions)

    base = {
        "actor_id":             actor_id,
        "as_of":                as_of.isoformat(),
        "closed_trades":        closed_trades,
        "min_sample":           min_sample,
        "sizing_interpretable": bool(sizing_interpretable),
    }

    def _no_signal(reason, **extra):
        return {**base, "signal": False, "skill_score": None,
                "win_rate": None, "latency_delta_s": None, "drawdown": None,
                "followable": False, "real_but_unfollowable": False,
                "reason": reason, **extra}

    # Sizing must be interpretable to make skill meaningful at all (CEX ROI%-only
    # would fail here — but CEX is out of v1, so on-chain venues pass).
    if not sizing_interpretable:
        return _no_signal(REASON_SIZING_OPAQUE)

    # Fatal survival gate — NO-SIGNAL, not a weak signal.
    if closed_trades < min_sample:
        return _no_signal(REASON_BELOW_MIN_CLOSED)

    wins = sum(1 for a in actions if a.get("outcome") == "win")
    win_rate = wins / closed_trades
    skill_score = win_rate                       # 0..1 demonstrated track record
    latency_delta_s = _latency_delta_s(actions)
    drawdown = _max_drawdown(actions)

    # Drawdown gate — risk too hot to credit skill. A rejection carries NO usable
    # skill estimate (skill_score stays None) so it lands in the denominator as
    # REJECTED, not surfaced; the drawdown value rides along for the rollup/UI.
    if drawdown > float(settings.COPYTRADE_MAX_DRAWDOWN):
        return _no_signal(REASON_DRAWDOWN_TOO_DEEP, drawdown=drawdown)

    followable = latency_delta_s <= float(settings.COPYTRADE_MAX_FOLLOWABLE_DELTA_S)
    real_but_unfollowable = (not followable)

    return {
        **base,
        "signal":                bool(followable),
        "skill_score":           round(skill_score, 4),
        "win_rate":              round(win_rate, 4),
        "wins":                  wins,
        "latency_delta_s":       round(latency_delta_s, 2),
        "drawdown":              drawdown,
        "followable":            bool(followable),
        "real_but_unfollowable": bool(real_but_unfollowable),
        # Latency usually eats leaderboard edges — name it honestly.
        "reason":                (None if followable else REASON_DELTA_TOO_HIGH),
    }


__all__ = [
    "resolved_actions_as_of", "score_actor",
    "REASON_BELOW_MIN_CLOSED", "REASON_DELTA_TOO_HIGH",
    "REASON_DRAWDOWN_TOO_DEEP", "REASON_SIZING_OPAQUE",
]
