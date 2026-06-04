"""
agents/opportunities/ranker.py

Stage 3's ordering — TrajectoryRanker. Time dominates magnitude (§1.2):
the headline sort key is competitor_trend, NOT edge_annualized_pct. A
zero-competitor survivor outranks a higher-edge market whose competitor
count just jumped 1→4 — a fat edge with rising competition is a closing
window. Edge is the secondary sort; reachability breaks ties.

The ranker never re-screens (gate's job) and never measures (competition
recipe's job). It DOES enforce the render invariant: a row's edge is
NEVER surfaced without its competitor_trend attached — render_row()
strips the edge and marks it rather than show a naked number.

NOTE: database/queries.get_ranked_opportunities mirrors this ordering
locally (queries.py can't import the agents package without a circular
import); tests pin both stay in sync.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Headline order: earlier = better. Earlier-is-the-thesis.
TREND_RANK = {"zero": 0, "rising_slow": 1, "rising_fast": 2, "saturated": 3}
REACHABILITY_RANK = {"reachable": 0, "unknown": 1, "unreachable": 2}

EDGE_FREQUENCY_UNKNOWN = "frequency unknown"


def render_row(row: dict) -> dict:
    """View-payload invariant: edge never renders without trajectory
    adjacent. If competitor_trend is missing, the edge is withheld and
    the marker says why; an unannualized edge (None) renders as the
    explicit frequency-unknown marker, never a fabricated APR."""
    out = dict(row)
    trend = out.get("competitor_trend")
    if not trend:
        out["edge_annualized_pct"] = None
        out["edge_display"] = "edge withheld — competition trajectory unknown"
        return out
    edge = out.get("edge_annualized_pct")
    if edge is None:
        lif = None
        # Per-event LIF (if known) shows raw with the explicit marker.
        detail = out.get("detail") or {}
        lif = detail.get("lif_pct")
        out["edge_display"] = (
            f"{lif:.1f}%/event — {EDGE_FREQUENCY_UNKNOWN}"
            if lif is not None else EDGE_FREQUENCY_UNKNOWN
        )
    else:
        out["edge_display"] = f"{edge:.1f}%/yr"
    return out


class TrajectoryRanker:
    """rank(survivor_rows) — trajectory (headline) → edge (secondary) →
    reachability (tie-break). Input rows are survivor dicts (the gate
    already filtered the view); the ranker never re-screens."""

    def rank(self, survivor_rows: list) -> list:
        rows = [render_row(r) for r in (survivor_rows or [])]
        rows.sort(key=self._key)
        return rows

    @staticmethod
    def _key(row: dict):
        trend_rank = TREND_RANK.get(row.get("competitor_trend"),
                                    len(TREND_RANK))
        edge = row.get("edge_annualized_pct")
        edge_key = -(edge if edge is not None else float("-inf"))
        reach = REACHABILITY_RANK.get(row.get("reachability_verdict"),
                                      REACHABILITY_RANK["unknown"])
        return (trend_rank, edge_key, reach)


# Module-level singleton (project convention).
trajectory_ranker = TrajectoryRanker()

__all__ = ["TrajectoryRanker", "trajectory_ranker", "render_row",
           "TREND_RANK", "REACHABILITY_RANK", "EDGE_FREQUENCY_UNKNOWN"]
