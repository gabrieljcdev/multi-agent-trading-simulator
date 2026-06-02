"""
execution/funding_openness.py

The funding-frontier openness / decay signal — the "find the carry on a pair
nobody has automated yet, and tell how long that lasts" instrument.

Crowding in funding carry shows up as SPREAD COMPRESSION OVER TIME (others
automating the same pair bid the edge away), not as competing transactions.
So this module keeps a short rolling history of each pair's projected net
APR + open interest and derives:

  * spread_decay_bps_per_day — slope of projected_net_apr over the window,
    signed so a COMPRESSING spread (net APR falling) reads POSITIVE. A fast
    positive decay = the corner is being automated by others.
  * oi_growth_pct_24h        — open-interest change over ~24h. Rising OI on a
    young pair = the richness window is live; collapsing OI = avoid.
  * crowding_verdict         — OPEN | COMPRESSING | CROWDED | UNKNOWN from
    thresholds on decay + age + OI. UNKNOWN whenever history is too short —
    we never fabricate OPEN.

Pure functions read thresholds from config.settings so a sweep retunes them
without touching this logic. OBSERVATION ONLY: nothing here places or sizes
a trade — the verdict is recorded for the go-live gate, not used to flip
entry in this phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import settings


# Verdict labels (kept as constants so callers don't stringly-type them).
OPEN        = "OPEN"
COMPRESSING = "COMPRESSING"
CROWDED     = "CROWDED"
UNKNOWN     = "UNKNOWN"

# Minimum samples before a verdict is anything but UNKNOWN. Two points define
# a line but not a trend; three is the floor for "we've seen this hold up".
_MIN_SAMPLES = 3


def _slope(xs: list[float], ys: list[float]) -> float:
    """Least-squares slope dy/dx. 0.0 when x has no spread (vertical)."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return num / den


def decay_bps_per_day(samples: list[tuple[float, float]]) -> float:
    """Signed compression rate of projected net APR, in bps of APR per day.

    samples: [(ts_sec, net_apr_fraction), …] in any order. Returns a POSITIVE
    value when the spread is compressing (net APR trending DOWN) — that's the
    "being automated" signal — and negative when it's widening. 0.0 when
    there's too little history or no time span.
    """
    if len(samples) < 2:
        return 0.0
    xs = [float(s[0]) for s in samples]
    ys = [float(s[1]) * 1e4 for s in samples]          # APR fraction → bps
    if (max(xs) - min(xs)) <= 0:
        return 0.0
    slope_bps_per_sec = _slope(xs, ys)
    slope_bps_per_day = slope_bps_per_sec * 86400.0
    return -slope_bps_per_day                          # falling spread → positive decay


def oi_growth_pct_24h(samples: list[tuple[float, float]]) -> Optional[float]:
    """Percent change in open interest over ~the last 24h.

    samples: [(ts_sec, oi_usd), …]. Compares the latest sample to the oldest
    sample within the trailing 24h window (or the oldest available when the
    history is shorter than 24h). Returns None when there's < 2 points or the
    baseline OI is non-positive (can't form a ratio).
    """
    if len(samples) < 2:
        return None
    ordered = sorted(samples, key=lambda s: s[0])
    latest_ts, latest_oi = ordered[-1]
    cutoff = latest_ts - 24 * 3600
    # Oldest sample at or after the cutoff; fall back to the very oldest.
    baseline = next((s for s in ordered if s[0] >= cutoff), ordered[0])
    base_oi = float(baseline[1])
    if base_oi <= 0:
        return None
    return (float(latest_oi) - base_oi) / base_oi * 100.0


def classify_crowding(
    decay_bps_day: float,
    pair_age_days: Optional[float],
    oi_growth_pct: Optional[float],
    *,
    n_samples: int,
) -> str:
    """Map (decay, age, OI growth) to a crowding verdict.

    UNKNOWN when history is too short (never fabricate OPEN). Otherwise:
      * decay >= FUNDING_CROWDED_DECAY_BPS_DAY            → CROWDED
      * decay <= FUNDING_OPEN_MAX_DECAY_BPS_DAY AND the age/OI signals don't
        contradict (young-or-unknown pair, non-shrinking OI)  → OPEN
      * everything in between                              → COMPRESSING
    """
    if n_samples < _MIN_SAMPLES:
        return UNKNOWN

    crowded_floor = float(settings.FUNDING_CROWDED_DECAY_BPS_DAY)
    open_ceiling  = float(settings.FUNDING_OPEN_MAX_DECAY_BPS_DAY)
    young_cutoff  = float(settings.FUNDING_YOUNG_PAIR_MAX_AGE_DAYS)

    if decay_bps_day >= crowded_floor:
        return CROWDED
    if decay_bps_day <= open_ceiling:
        # Age/OI must AGREE for OPEN. Unknown signals don't contradict, but a
        # known-old pair or a known-shrinking OI demotes it to COMPRESSING.
        age_ok = (pair_age_days is None) or (pair_age_days <= young_cutoff)
        oi_ok  = (oi_growth_pct is None) or (oi_growth_pct >= 0.0)
        if age_ok and oi_ok:
            return OPEN
        return COMPRESSING
    return COMPRESSING


@dataclass
class OpennessSnapshot:
    spread_decay_bps_per_day: float
    oi_growth_pct_24h:        Optional[float]
    crowding_verdict:         str
    n_samples:                int


class OpennessTracker:
    """Rolling per-symbol history of (ts, net_apr, oi_usd) → OpennessSnapshot.

    Lives on the engine singleton so history accumulates across scans. The
    window is FUNDING_DECAY_WINDOW_HOURS; samples older than that are pruned
    on every record so memory stays bounded to the window.
    """

    def __init__(self):
        self._hist: dict[str, list[tuple[float, float, float]]] = {}

    def record(self, symbol: str, ts: float, net_apr: float, oi_usd: float) -> None:
        window_sec = max(1.0, float(settings.FUNDING_DECAY_WINDOW_HOURS) * 3600.0)
        hist = self._hist.setdefault(symbol, [])
        hist.append((float(ts), float(net_apr), float(oi_usd)))
        cutoff = float(ts) - window_sec
        # Prune outside the window (keep ascending by ts).
        self._hist[symbol] = [s for s in hist if s[0] >= cutoff]

    def snapshot(
        self, symbol: str, pair_age_days: Optional[float] = None,
    ) -> OpennessSnapshot:
        hist = self._hist.get(symbol, [])
        decay   = decay_bps_per_day([(s[0], s[1]) for s in hist])
        growth  = oi_growth_pct_24h([(s[0], s[2]) for s in hist])
        verdict = classify_crowding(
            decay, pair_age_days, growth, n_samples=len(hist),
        )
        return OpennessSnapshot(
            spread_decay_bps_per_day=decay,
            oi_growth_pct_24h=growth,
            crowding_verdict=verdict,
            n_samples=len(hist),
        )


__all__ = [
    "OPEN", "COMPRESSING", "CROWDED", "UNKNOWN",
    "decay_bps_per_day", "oi_growth_pct_24h", "classify_crowding",
    "OpennessSnapshot", "OpennessTracker",
]
