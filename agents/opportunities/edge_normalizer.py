"""
agents/opportunities/edge_normalizer.py

Edge normalization (FEATURE BLOCK 4) — every edge normalizes to
ANNUALIZED % so a liquidation bonus, a funding stream, and an LP yield
rank on one axis. One pinned method per opp_type; one-shot and
continuous edges annualize differently:

  continuous (funding, lp) — annualize directly from the periodic rate.
  one-shot   (liquidation) — LIF is a PER-EVENT return, not an annual
      rate. Annualize as LIF_pct × expected_events_per_year, estimated
      from OBSERVED liquidation frequency — and (None, "low") until
      enough events are observed. Annualizing a single observed bonus
      as if it recurred continuously would manufacture a false fat
      edge; when frequency is unknown the view shows the raw per-event
      LIF with an explicit "frequency unknown" marker instead of a
      fabricated APR.

edge_annualized_pct is never rendered without competitor_trend adjacent
— enforced in ranker.py and the view payload, not here.
"""

from __future__ import annotations

import logging

from config import settings

logger = logging.getLogger(__name__)

CONFIDENCE_LOW    = "low"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_HIGH   = "high"

_HOURS_PER_YEAR = 365.0 * 24.0


class EdgeNormalizer:
    """annualize(candidate) → (edge_annualized_pct, edge_confidence).
    Returns (None, "low") when inputs are insufficient — never guesses.

    `candidate` is duck-typed: anything exposing .opp_type and
    .raw_detail (an OpportunityCandidate or a core-row shim)."""

    def annualize(self, candidate) -> tuple[float | None, str]:
        opp_type = getattr(candidate, "opp_type", None) \
            or (candidate.get("opp_type") if isinstance(candidate, dict) else None)
        detail = getattr(candidate, "raw_detail", None) \
            or (candidate.get("raw_detail") if isinstance(candidate, dict) else None) \
            or {}

        if opp_type == "liquidation":
            return self._one_shot_liquidation(detail)
        if opp_type == "funding":
            return self._continuous_funding(detail)
        if opp_type == "lp":
            return self._continuous_lp(detail)
        # launch (and anything off-taxonomy): no pinned method yet.
        return None, CONFIDENCE_LOW

    # ── One-shot: liquidation ───────────────────────────────────────────

    def _one_shot_liquidation(self, d: dict) -> tuple[float | None, str]:
        """LIF_pct × expected_events_per_year, frequency observed from
        the market itself. Below OPPORTUNITY_MIN_EVENTS_FOR_FREQUENCY
        observed events → (None, "low"): no fabricated APR."""
        lif_pct = d.get("lif_pct")
        n_events = d.get("observed_liquidation_events")
        window_h = d.get("observed_window_h")
        if lif_pct is None or not n_events or not window_h:
            return None, CONFIDENCE_LOW
        if int(n_events) < int(settings.OPPORTUNITY_MIN_EVENTS_FOR_FREQUENCY):
            return None, CONFIDENCE_LOW
        events_per_year = float(n_events) * (_HOURS_PER_YEAR / float(window_h))
        edge = float(lif_pct) * events_per_year
        # Frequency from a real sample but still a single market's short
        # history — medium at best until horizons of labels accumulate.
        return edge, CONFIDENCE_MEDIUM

    # ── Continuous: funding ─────────────────────────────────────────────

    def _continuous_funding(self, d: dict) -> tuple[float | None, str]:
        """funding bps × payments/year → annualized %."""
        rate_bps = d.get("funding_rate_bps")
        interval_h = d.get("payment_interval_h")
        if rate_bps is None or not interval_h:
            return None, CONFIDENCE_LOW
        payments_per_year = _HOURS_PER_YEAR / float(interval_h)
        edge = (float(rate_bps) / 100.0) * payments_per_year   # bps → %
        return edge, CONFIDENCE_MEDIUM

    # ── Continuous: lp ──────────────────────────────────────────────────

    def _continuous_lp(self, d: dict) -> tuple[float | None, str]:
        """LP net yield (fees − expected IL/LVR) × turnover. The
        new_pool_detector is not built this pass (unresolved noise
        floor, §6) — inputs are never present yet, so this returns
        (None, "low") until that detector lands and pins them."""
        net_yield_pct = d.get("net_yield_pct")
        turnover_per_year = d.get("turnover_per_year")
        if net_yield_pct is None or turnover_per_year is None:
            return None, CONFIDENCE_LOW
        return float(net_yield_pct) * float(turnover_per_year), CONFIDENCE_MEDIUM


# Module-level singleton (project convention).
edge_normalizer = EdgeNormalizer()

__all__ = ["EdgeNormalizer", "edge_normalizer",
           "CONFIDENCE_LOW", "CONFIDENCE_MEDIUM", "CONFIDENCE_HIGH"]
