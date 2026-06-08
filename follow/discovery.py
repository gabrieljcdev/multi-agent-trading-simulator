"""
follow/discovery.py

Auto-discovery candidate pipeline. Mines the watcher's OWN historical event
stream for wallets that were early to tokens which later ran, and PROPOSES them
for operator review. It writes ONLY candidate rows — there is no code path from
discovery to "confirmed", ever (FEATURE BLOCK 4 / the HARD INVARIANT).

A discovered wallet reaches the review queue only if it clears the PROMOTION
GATE (ALL of):
  - resolved sample >= WALLETFLOW_DISCOVERY_MIN_SAMPLE   (STRICTER than the
    manual scorer gate — auto-discovery inflates multiple comparisons)
  - point-in-time skill score >= WALLETFLOW_DISCOVERY_MIN_SCORE
  - first-mover ratio >= WALLETFLOW_DISCOVERY_MIN_FIRSTMOVER
  - latency-δ within WALLETFLOW_DISCOVERY_MAX_DELTA_S

Bait-resistance WARNINGS are attached to each candidate (they DO NOT auto-reject
— they inform operator review, surfaced in the UI):
  - funding source traces to a known bad cluster (meme-scorer rug-rate). The
    meme source is not built yet, so this hook DEFAULTS to "unknown" and is
    clearly marked — never silently treated as "clean".
  - suspiciously clean record (near-zero losses over a large sample) -> red flag
  - appears in a coordinated funding cluster (N candidates sharing a funder)

The point-in-time scoring is the SHARED skill_scorer function — discovery does
not reimplement "what did we know at T".
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

from config import settings
from database import queries as q
from follow import provenance, funding
# Bind the SHARED point-in-time functions by reference — discovery must route
# through the one definition in skill_scorer, never a copy (asserted by test).
from follow.skill_scorer import score_wallet, resolved_actions_as_of

logger = logging.getLogger(__name__)


def _meme_rug_rate(funder: Optional[str]) -> Optional[float]:
    """Hook into the meme scorer's rug-rate for a funding source. The meme
    source is not built yet, so this returns None (-> "unknown" warning). When
    the meme source lands, wire it here — one place to change."""
    try:
        from follow.meme_wallets import funder_rug_rate  # type: ignore
    except Exception:
        return None
    try:
        return funder_rug_rate(funder)
    except Exception:
        return None


def _funder_of(address: str) -> Optional[str]:
    """Best-effort funding source. Reads the DERIVED first-funder cache
    (follow/funding.py), which the FollowAgent's slow background crawl populates
    from public RPC — the call-site swap off the old endpoint. Falls back to a
    funder stamped on the wallet's earliest observed event meta. This stays SYNC
    + offline (cache-only): discovery never blocks on or blasts the RPC, and the
    gating logic below is unchanged."""
    cached = funding.cached_funder(address)
    if cached:
        return cached
    try:
        actions = q.get_wallet_resolved_actions(address)
    except Exception:
        return None
    for a in actions:                      # ascending occurred_at
        funder = (a.get("meta") or {}).get("funder")
        if funder:
            return funder
    return None


def _bait_warnings(address: str, score: dict,
                   funder_counts: dict[str, int]) -> list[dict]:
    """Build the (informational, non-rejecting) bait-resistance warning list."""
    warnings: list[dict] = []

    # 1. Bad-cluster (meme rug-rate). Unavailable -> "unknown", clearly marked.
    funder = _funder_of(address)
    rug = _meme_rug_rate(funder)
    if rug is None:
        warnings.append({
            "flag": "bad_cluster", "severity": "unknown",
            "detail": "meme rug-rate hook unavailable — funding-cluster risk "
                      "UNKNOWN (not assumed clean)",
        })
    elif rug > 0.0:
        warnings.append({
            "flag": "bad_cluster", "severity": "warn",
            "detail": f"funding source rug-rate {rug:.2f}",
        })

    # 2. Suspiciously clean record — near-zero losses over a large sample.
    win_rate = score.get("win_rate")
    if (win_rate is not None
            and win_rate >= float(settings.WALLETFLOW_TOO_CLEAN_WINRATE)):
        warnings.append({
            "flag": "too_clean", "severity": "warn",
            "detail": f"win-rate {win_rate:.3f} over {score.get('resolved_sample')} "
                      f"resolved actions — too clean to be real, RED flag",
        })

    # 3. Coordinated funding cluster — N candidates sharing one funder.
    if funder and funder_counts.get(funder, 0) >= int(settings.WALLETFLOW_COORD_CLUSTER_MIN):
        warnings.append({
            "flag": "coordinated_cluster", "severity": "warn",
            "detail": f"shares funder {funder} with "
                      f"{funder_counts[funder]} 'independent' candidates",
        })
    return warnings


def _passes_promotion_gate(score: dict) -> bool:
    """ALL of sample / score / first-mover / latency-δ must clear. Failing any
    one keeps the wallet out of the review queue."""
    if score.get("skill_score") is None:                 # below sample gate
        return False
    if int(score.get("resolved_sample", 0)) < int(settings.WALLETFLOW_DISCOVERY_MIN_SAMPLE):
        return False
    if float(score.get("skill_score") or 0.0) < float(settings.WALLETFLOW_DISCOVERY_MIN_SCORE):
        return False
    if float(score.get("first_mover_ratio") or 0.0) < float(settings.WALLETFLOW_DISCOVERY_MIN_FIRSTMOVER):
        return False
    delta = score.get("latency_delta_s")
    if delta is None or float(delta) > float(settings.WALLETFLOW_DISCOVERY_MAX_DELTA_S):
        return False
    return True


def run_discovery(as_of: Optional[datetime] = None,
                  universe: Optional[list[str]] = None) -> list[dict]:
    """Run one discovery pass. Returns the list of NEWLY-proposed candidate
    dicts (gate-passers only). Writes ONLY candidate rows; never confirms.

    Rejected and already-watchlisted addresses are skipped (rejected wallets
    are never re-proposed — the HARD INVARIANT)."""
    as_of = as_of or datetime.utcnow()
    if universe is None:
        try:
            universe = q.get_distinct_flow_actors()
        except Exception as e:
            logger.debug("discovery universe read failed: %s", e)
            universe = []

    # Score every candidate first with the STRICTER discovery sample gate, then
    # build funder counts across the scored set for the coordinated-cluster check.
    scored: dict[str, dict] = {}
    funder_counts: dict[str, int] = defaultdict(int)
    for address in universe:
        # Skip anything already known — discovery only ever introduces NEW
        # candidates. Rejected is permanent; confirmed/manual/candidate are not
        # discovery's to re-touch.
        entry = q.get_watchlist_entry(address)
        if entry is not None:
            continue
        score = score_wallet(address, as_of,
                             min_sample=int(settings.WALLETFLOW_DISCOVERY_MIN_SAMPLE))
        scored[address] = score
        funder = _funder_of(address)
        if funder:
            funder_counts[funder] += 1

    proposed: list[dict] = []
    for address, score in scored.items():
        if not _passes_promotion_gate(score):
            continue
        warnings = _bait_warnings(address, score, funder_counts)
        # Propose: create the watchlist "candidate" row (refuses rejected /
        # already-watchlisted as a second guard).
        res = provenance.propose_candidate(address)
        if not res.get("ok"):
            logger.debug("discovery propose %s refused: %s", address, res)
            continue
        row = {
            "address":           address,
            "skill_score":       score.get("skill_score"),
            "resolved_sample":   score.get("resolved_sample"),
            "first_mover_ratio": score.get("first_mover_ratio"),
            "latency_delta_s":   score.get("latency_delta_s"),
            "warnings":          warnings,
            "gate_passed":       True,
            "review_state":      "pending",
        }
        try:
            q.insert_discovery_candidate(row)
        except Exception as e:
            logger.debug("discovery insert %s: %s", address, e)
            continue
        proposed.append(row)

    if proposed:
        logger.info("walletflow discovery: proposed %d candidate(s) for review",
                    len(proposed))
    return proposed


__all__ = ["run_discovery", "resolved_actions_as_of", "score_wallet"]
