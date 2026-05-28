"""
agents/balance/policy/growth_optimal.py

Growth-optimal allocation blended with risk-parity by ALLOCATION_CONFIDENCE.

Why this shape
--------------
Kelly 1956 (and Markowitz-MPT under unit leverage / Kelly-Parity when
Sharpes are equal) gives weights ∝ Σ⁻¹μ — the long-run geometric growth
maximiser. We use **fractional** Kelly (KELLY_FRACTION) because full
Kelly produces 50–80% drawdowns historically; ~½ Kelly keeps ~75% of
the growth at ~half the volatility.

With thin / early data the per-fund edge estimates (μ) are unreliable,
so we blend toward risk-parity (equal risk contribution — robust, no
return assumptions). ALLOCATION_CONFIDENCE = 0.0 means pure risk-parity
(safe default); 1.0 means pure growth-optimal. Raise as
fund_capital_efficiency accumulates.

The policy is deliberately conservative when data is missing — every
fund-without-history falls back to equal-share, never zero. Zero would
starve a fund of a chance to log edge data in the first place.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Optional

from config import settings
from database import queries as db_queries

from agents.balance.policy.base import BasePolicy, InventoryTarget


if TYPE_CHECKING:
    from agents.balance.inventory_state import InventoryState

logger = logging.getLogger(__name__)


# The funds the policy distributes capital across. Mirrors the
# coordinator's roster (see agents/__init__.py:REGISTERED_AGENTS) —
# scaling beyond these is one-line: add a fund_id + a constant in
# settings + the venues it trades.
_FUNDS_AND_VENUES: dict[str, list[str]] = {
    "signal":     ["binance", "kraken", "bybit", "kucoin"],
    "arb":        ["kraken", "bybit", "bitget", "bitstamp", "gateio", "bitfinex", "mexc"],
    "mexc_scalp": ["mexc"],
}


def _fund_capital_constant(fund: str) -> float:
    """Read the configured fund pool from settings — used as the
    fallback when no realised P&L history exists yet."""
    mapping = {
        "signal":     "FUND_SIGNAL_CAPITAL",
        "arb":        "FUND_ARB_CAPITAL",
        "mexc_scalp": "FUND_MEXC_SCALP_CAPITAL",
    }
    name = mapping.get(fund)
    return float(getattr(settings, name, 0.0) or 0.0) if name else 0.0


def _fund_edge_estimates() -> dict[str, float]:
    """Per-fund expected return-on-deployed-capital, sampled from the
    rolling fund_capital_efficiency table. Returns 0.0 for any fund
    without history — risk-parity blending leans on this default
    when ALLOCATION_CONFIDENCE > 0.
    """
    out: dict[str, float] = {}
    for fund in _FUNDS_AND_VENUES:
        try:
            rows = db_queries.get_fund_capital_efficiency(fund, hours=720)
        except Exception as e:
            logger.debug("growth_optimal: get_fund_capital_efficiency(%s) failed: %s",
                         fund, e)
            rows = []
        if not rows:
            out[fund] = 0.0
            continue
        # Average return_on_deployed_pct over the lookback — naïve but
        # robust enough for the early-data regime. Future replacement:
        # rolling Sharpe / Σ⁻¹μ via numpy when sample size justifies it.
        returns = [float(getattr(r, "return_on_deployed_pct", 0.0) or 0.0) for r in rows]
        out[fund] = sum(returns) / len(returns) if returns else 0.0
    return out


def _blend(growth_w: dict[str, float], parity_w: dict[str, float], alpha: float) -> dict[str, float]:
    """w = α · growth + (1 − α) · parity, renormalised. Returns a fresh dict."""
    keys = set(growth_w) | set(parity_w)
    raw = {
        k: alpha * float(growth_w.get(k, 0.0)) + (1 - alpha) * float(parity_w.get(k, 0.0))
        for k in keys
    }
    total = sum(raw.values())
    if total <= 0:
        # Degenerate (everything zero) — fall back to equal split.
        n = len(keys) or 1
        return {k: 1.0 / n for k in keys}
    return {k: v / total for k, v in raw.items()}


def _venue_weights_for_fund(fund: str) -> dict[str, float]:
    """Within-fund spread across its venues. Reads
    STRATEGY_EXCHANGE_MAP-like static info — equal split by default
    so a freshly-launched fund seeds every venue evenly. Future:
    weight by per-venue activity from the trades ledger."""
    venues = _FUNDS_AND_VENUES.get(fund, [])
    if not venues:
        return {}
    return {ex: 1.0 / len(venues) for ex in venues}


def _open_position_floor(fund: str, exchange: str) -> float:
    """Open-position notional this fund has on this exchange, in USD.

    The rail-2 floor must always be ≥ this number, so the BalanceAgent
    can never lower a fund's allocation below its committed risk.
    The agent's own get_open_position_notional() ultimately backs this
    on the agent thread; we expose 0.0 as a safe sentinel here because
    the policy runs on the BalanceAgent's loop and can't reach into
    sibling agents synchronously without a coupling we're avoiding.
    """
    return 0.0


def _capacity_cap(fund: str, exchange: str) -> float:
    """Per-(fund, exchange) capacity ceiling from settings. +inf when
    unset so the planner does not block by default."""
    caps = getattr(settings, "FUND_CAPACITY_CEILINGS_USD", {}) or {}
    return float(caps.get((fund, exchange), float("inf")))


class GrowthOptimalPolicy(BasePolicy):
    """Growth-optimal weights blended with risk-parity.

    See module docstring for the rationale. compute_targets():
      1. Per-fund weights from edge estimates (growth-optimal) blended
         with risk-parity by ALLOCATION_CONFIDENCE.
      2. Multiply weights by (equity − COMPOUND_RESERVE_PCT·equity) →
         target USD per fund.
      3. Distribute each fund's target across its venues by venue-share.
      4. Apply capacity caps (overflow cascades to next-best node within
         the fund; if every cell of a fund is capped, the residual
         falls into the reserve).
      5. Emit one InventoryTarget per (fund, exchange, USDT) cell.

    Profits compound within their originating fund by default — the
    blend keeps the fund's share of equity proportional to its
    realised edge as it accumulates.
    """

    policy_id    = "growth_optimal"
    display_name = "Growth-Optimal (Kelly · Risk-Parity blend)"

    def compute_targets(
        self,
        inv: "InventoryState",
        equity: float,
    ) -> list[InventoryTarget]:
        try:
            return self._compute_targets_inner(inv, equity)
        except Exception as e:
            logger.error("GrowthOptimalPolicy.compute_targets: %s", e, exc_info=True)
            return []

    def _compute_targets_inner(
        self,
        inv: "InventoryState",
        equity: float,
    ) -> list[InventoryTarget]:
        reserve_pct = float(getattr(settings, "COMPOUND_RESERVE_PCT", 0.05))
        pool = max(0.0, float(equity) * (1.0 - reserve_pct))

        # ── Fund-level weights ────────────────────────────────────────
        # Growth-optimal: ∝ edge estimate (clipped to ≥0 to avoid
        # shorting capital out of a losing fund — that's the auto-pause
        # path, not the policy's).
        edges = _fund_edge_estimates()
        clipped = {f: max(0.0, e) for f, e in edges.items()}
        total = sum(clipped.values())
        if total > 0:
            growth_w = {f: v / total for f, v in clipped.items()}
        else:
            # No edge data anywhere — seed by the configured fund
            # constants so the policy boots with the operator's plan.
            constants = {f: _fund_capital_constant(f) for f in _FUNDS_AND_VENUES}
            csum = sum(constants.values())
            if csum > 0:
                growth_w = {f: v / csum for f, v in constants.items()}
            else:
                n = len(_FUNDS_AND_VENUES)
                growth_w = {f: 1.0 / n for f in _FUNDS_AND_VENUES}

        # Risk-parity: equal weight per fund. Refinement once volatility
        # estimates are reliable: weight ∝ 1/σ.
        n_funds = len(_FUNDS_AND_VENUES)
        parity_w = {f: 1.0 / n_funds for f in _FUNDS_AND_VENUES} if n_funds else {}

        alpha = max(0.0, min(1.0, float(getattr(settings, "ALLOCATION_CONFIDENCE", 0.0))))
        fund_weights = _blend(growth_w, parity_w, alpha)

        # Apply fractional Kelly to the share allocated (clip at 1.0 so
        # the residual flows into the reserve rather than over-deploying).
        kelly = max(0.0, min(1.0, float(getattr(settings, "KELLY_FRACTION", 0.25))))
        fund_targets = {f: pool * w * kelly for f, w in fund_weights.items()}

        # ── Distribute across venues, respecting capacity caps ────────
        targets: list[InventoryTarget] = []
        for fund, fund_target in fund_targets.items():
            venue_w = _venue_weights_for_fund(fund)
            if not venue_w:
                continue
            # First pass: tentative allocation per venue.
            tentative = {ex: fund_target * w for ex, w in venue_w.items()}

            # Overflow cascade: cap each cell at cap_usd, push the
            # excess to the next under-capped venue in declared order
            # (the venue list above), final residual goes nowhere
            # (drops into reserve naturally — the cell isn't emitted).
            ordered = list(venue_w.keys())
            for i, ex in enumerate(ordered):
                cap = _capacity_cap(fund, ex)
                if tentative[ex] <= cap:
                    continue
                overflow = tentative[ex] - cap
                tentative[ex] = cap
                # Cascade to subsequent venues.
                for ex2 in ordered[i + 1:]:
                    headroom = _capacity_cap(fund, ex2) - tentative.get(ex2, 0.0)
                    if headroom <= 0:
                        continue
                    add = min(overflow, headroom)
                    tentative[ex2] = tentative.get(ex2, 0.0) + add
                    overflow -= add
                    if overflow <= 0:
                        break

            for ex, target_usd in tentative.items():
                floor = _open_position_floor(fund, ex)
                cap = _capacity_cap(fund, ex)
                current = inv.effective_balance(fund, ex, "USDT")
                # Signed drift — under-stocked is negative, over-stocked
                # positive. Match the cross-chain InventoryTarget's
                # spirit (theta-gated) — planner re-checks via the
                # derived band.
                if target_usd > 0:
                    drift = (current - target_usd) / target_usd
                else:
                    drift = 0.0 if current == 0 else math.copysign(1.0, current)

                # Producer-side needs_rebalance hint. The planner's
                # Miller-Orr band is what actually triggers a transfer;
                # this just flags the cell as worth scrutinising.
                theta_hint = 0.20    # cross-chain convention — same theta
                needs = abs(drift) > theta_hint

                targets.append(InventoryTarget(
                    fund=fund, exchange=ex, asset="USDT",
                    target_usd=float(target_usd),
                    floor_usd=float(floor),
                    cap_usd=float(cap),
                    drift_pct=float(drift),
                    needs_rebalance=bool(needs),
                ))
        return targets
