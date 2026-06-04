"""
agents/opportunities/exploration.py

FEATURE BLOCK 6a — the exploration lane's unconventional flagging +
scoring. The standard pipeline's discipline is correct for deciding
action, but in $0 observation mode it is too eager: it reasons away
anything risky-but-not-fatal, off-taxonomy, or interesting only as a
factor COMBINATION. This module records *why a survivor might be
interesting beyond the standard rank* — in both forms the operator
chose:

  structured — unconventional_factors (fixed vocabulary, queryable) +
               a numeric unconventional_score (0-1)
  free text  — unconventional_rationale, the agent's own words; where
               reasoning no predefined factor anticipated actually
               lives. The UI surfaces this field.

OBSERVATION-ONLY, absolutely: nothing here gates, sizes, reorders the
standard view, or makes any row actionable. Both forms carry the same
forward labels every other observation gets, so the DATA — not the
ranker, and not this scorer's confidence — eventually says whether the
unconventional reads paid off.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The fixed factor vocabulary (queryable/filterable later).
FACTOR_UGLY_BUT_SURVIVABLE_ORACLE = "ugly_but_survivable_oracle"
FACTOR_OFF_TAXONOMY_EDGE          = "off_taxonomy_edge"
FACTOR_FACTOR_STACK               = "factor_stack"
FACTOR_CONTRARIAN_REACHABILITY    = "contrarian_reachability"
FACTOR_IGNORED_BY_PROS_FLOOR      = "ignored_by_pros_floor"

FACTOR_VOCABULARY = (
    FACTOR_UGLY_BUT_SURVIVABLE_ORACLE,
    FACTOR_OFF_TAXONOMY_EDGE,
    FACTOR_FACTOR_STACK,
    FACTOR_CONTRARIAN_REACHABILITY,
    FACTOR_IGNORED_BY_PROS_FLOOR,
)

# Per-factor score contribution — heuristic seeds for the observation
# phase; the forward labels, not these weights, decide what mattered.
_FACTOR_WEIGHT = 0.25
# Notional below which professional searchers historically don't bother
# (the cost-to-care floor) — an exploration heuristic, not a gate.
_PROS_FLOOR_USD = 100.0


def score_unconventional(core_row: dict, detail: dict | None = None,
                         feature_vector: dict | None = None,
                         ) -> tuple[float, list[str], str]:
    """Returns (unconventional_score, unconventional_factors,
    unconventional_rationale) for one SURVIVOR row.

    Deterministic + testable: factors come from the fixed vocabulary,
    score is a clamped sum of factor weights, rationale is assembled in
    plain words. Never raises — a scoring hiccup returns the empty
    read (0.0, [], "")."""
    try:
        return _score(core_row or {}, detail or {}, feature_vector or {})
    except Exception as e:
        logger.debug("[exploration] scoring failed: %s", e)
        return 0.0, [], ""


def _score(core: dict, detail: dict,
           fv: dict) -> tuple[float, list[str], str]:
    factors: list[str] = []
    reasons: list[str] = []

    # Survivable-but-ugly oracle: the disciplined ranker buries these,
    # but ugly-yet-sane setups are exactly where pre-competition edge
    # hides (risk and competition are inversely coupled, §1.1).
    oracle = str(detail.get("oracle_type") or fv.get("oracle_type") or "").lower()
    flags = core.get("risk_flags") or []
    has_green_oracle = any(f.get("flag") == "sane_oracle" for f in flags)
    if oracle == "unknown" and not has_green_oracle:
        factors.append(FACTOR_UGLY_BUT_SURVIVABLE_ORACLE)
        reasons.append(
            "oracle is unverified but nothing about it is fatal — the kind "
            "of ugly-but-survivable setup the disciplined rank buries while "
            "it is least competed")

    # Cost-to-care floor: tiny markets look unreachable/uninteresting on
    # the standard read, but pros ignore sub-floor events — the retail
    # tier can actually win these.
    supply = fv.get("supply_usd_at_detection")
    if supply is not None and 0 < float(supply) < _PROS_FLOOR_USD:
        factors.append(FACTOR_IGNORED_BY_PROS_FLOOR)
        reasons.append(
            f"market holds ~${float(supply):.0f} — below the cost-to-care "
            "floor where professional searchers bother, so the small-event "
            "tier is plausibly uncontested")

    # Contrarian reachability: verdict says unreachable/unknown, yet the
    # competition read says nobody is there — the obvious read may be
    # wrong, which is itself a hypothesis worth labelling.
    if (core.get("reachability_verdict") in (None, "unknown", "unreachable")
            and core.get("competitor_trend") == "zero"):
        factors.append(FACTOR_CONTRARIAN_REACHABILITY)
        reasons.append(
            "looks unreachable on the obvious read, but zero competitors "
            "have shown up — if the reachability verdict is wrong, this "
            "window is free")

    # Factor stack: no single factor stands out, but several mild ones
    # coincide (young + healthy LIF + quiet) — non-obvious combinations
    # are precisely what the standard rank can't see.
    mild = 0
    if (fv.get("market_age_sec_at_detection") or 1e12) < 6 * 3600:
        mild += 1
    lif = detail.get("lif_pct") or fv.get("lif_pct")
    if lif is not None and 5.0 <= float(lif) <= 15.0:
        mild += 1
    if core.get("competitor_trend") in ("zero", "rising_slow"):
        mild += 1
    if mild >= 3:
        factors.append(FACTOR_FACTOR_STACK)
        reasons.append(
            "no single headline factor, but the combination — hours old, "
            "healthy LIF band, quiet competition — stacks into a better "
            "setup than any component suggests")

    score = min(1.0, len(factors) * _FACTOR_WEIGHT)
    rationale = "; ".join(reasons)
    return score, factors, rationale


__all__ = ["score_unconventional", "FACTOR_VOCABULARY",
           "FACTOR_UGLY_BUT_SURVIVABLE_ORACLE", "FACTOR_OFF_TAXONOMY_EDGE",
           "FACTOR_FACTOR_STACK", "FACTOR_CONTRARIAN_REACHABILITY",
           "FACTOR_IGNORED_BY_PROS_FLOOR"]
