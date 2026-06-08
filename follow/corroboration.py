"""
follow/corroboration.py

The Tier-2 CORROBORATION VIEW — a thin cross-reference where the follow/ observers
MEET, without either one reaching into the other. A trader skilled on PERPS (the
copy-trade observer) who ALSO shows up as a skilled SPOT accumulator (the wallet
watcher) is a stronger, cross-surface-corroborated signal than either alone.

DELIBERATELY THIN. This is a cross-reference, NOT a new consensus engine:
  - it does NO detection of its own — it only JOINS assessments the two
    observers already produced;
  - the spot smart-money logic stays in the wallet watcher; the perp skill logic
    stays in the copy-trade observer; neither is duplicated here;
  - the crossing key is the actor identity (address) shared across surfaces.

Both sources FEED this view; the view owns no scoring. If a richer consensus is
ever wanted it belongs in a new module — this one stays a join.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def crossreference(perp_assessments: list[dict],
                   spot_assessments: list[dict]) -> list[dict]:
    """Pure join of perp-skill assessments (from the copy-trade observer) and
    spot-skill assessments (from the wallet watcher) on actor identity. Returns
    one row per actor that appears in EITHER surface, flagging the ones that
    appear in BOTH as cross-surface-corroborated. No scoring, no detection —
    just the cross-reference. Never raises."""
    out: list[dict] = []
    try:
        perp_by_id = {a.get("actor_id"): a for a in (perp_assessments or [])
                      if a.get("actor_id")}
        spot_by_id = {a.get("address") or a.get("actor_id"): a
                      for a in (spot_assessments or [])
                      if (a.get("address") or a.get("actor_id"))}
        for actor_id in sorted(set(perp_by_id) | set(spot_by_id)):
            perp = perp_by_id.get(actor_id)
            spot = spot_by_id.get(actor_id)
            out.append({
                "actor_id":         actor_id,
                "perp":             perp,
                "spot":             spot,
                "perp_skilled":     perp is not None,
                "spot_skilled":     spot is not None,
                # The whole point: a perp-skilled actor who ALSO accumulates on
                # spot is a stronger signal than either surface alone.
                "cross_surface":    perp is not None and spot is not None,
            })
    except Exception as e:
        logger.debug("corroboration: crossreference failed: %s", e)
    return out


def build_view(perp_source=None, spot_assessments: Optional[list[dict]] = None) -> dict:
    """Assemble the corroboration view from whatever each surface exposes.

    perp_source is the copy-trade observer (anything exposing
    skilled_actor_assessments()); spot_assessments is the wallet watcher's skilled
    set (passed in by the host — the wallet watcher's skill lives there, not here).
    Defensive: a missing/raising source contributes an empty list. Never raises."""
    perp: list[dict] = []
    if perp_source is not None:
        getter = getattr(perp_source, "skilled_actor_assessments", None)
        if callable(getter):
            try:
                perp = getter() or []
            except Exception as e:
                logger.debug("corroboration: perp assessments read failed: %s", e)
    spot = spot_assessments or []
    rows = crossreference(perp, spot)
    return {
        "rows":          rows,
        "n_perp":        len(perp),
        "n_spot":        len(spot),
        "n_cross":       sum(1 for r in rows if r.get("cross_surface")),
    }


__all__ = ["crossreference", "build_view"]
