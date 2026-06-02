"""
tools/observer_report.py

Operator-facing report over the observation ledgers. Read-only — it never
places, sizes, or mutates anything; it just summarises what the observers
have accumulated so a go-live decision is driven by per-pair data.

Funding observer (the funding-frontier layer):

  python -m tools.observer_report --observer funding_arb --since 24h
      → leads with the crowding rollup (OPEN/COMPRESSING/CROWDED mix),
        venue/pair counts, and the edge headline (best net APR overall vs
        best among OPEN pairs).

  python -m tools.observer_report --observer funding_arb --candidates
      → the literal go-live shortlist: crowding_verdict=OPEN & would_enter
        rows, ranked by projected_net_apr descending. This is what the gate
        consumes.
"""

from __future__ import annotations

import argparse
import time
from typing import Optional

from database import queries as db_queries


def parse_since(s: str) -> float:
    """Parse a '24h' / '7d' / '90m' window into DAYS (float). Bare numbers
    are read as days. Defaults to 1.0 day on anything unparseable."""
    if not s:
        return 1.0
    s = s.strip().lower()
    try:
        if s.endswith("h"):
            return float(s[:-1]) / 24.0
        if s.endswith("d"):
            return float(s[:-1])
        if s.endswith("m"):
            return float(s[:-1]) / (24.0 * 60.0)
        return float(s)
    except ValueError:
        return 1.0


def _fmt_pct(x: float) -> str:
    return f"{x*100:+.2f}%"


def funding_report(days: float = 1.0, *, candidates: bool = False,
                   now: Optional[float] = None) -> str:
    """Build the funding-observer report text. `now` is injectable for tests."""
    now = time.time() if now is None else now
    cutoff = now - days * 86400.0

    if candidates:
        return _funding_candidates(cutoff)
    return _funding_rollup(days)


def _funding_rollup(days: float) -> str:
    summ = db_queries.get_funding_summary(days=max(days, 1e-6))
    lines: list[str] = []
    lines.append(f"═══ FUNDING OBSERVER — last {days:g}d ═══")
    # Crowding rollup leads (the gate's first question: is the edge open?).
    lines.append("crowding mix:")
    for label in ("OPEN", "COMPRESSING", "CROWDED", "UNKNOWN"):
        lines.append(f"    {label:<12} {summ.get(f'pct_crowding_{label}', 0.0):5.1f}%")
    lines.append("")
    lines.append(f"observed pairs : {summ.get('total', 0)}  "
                 f"(would-enter {summ.get('would_enter', 0)}, "
                 f"closed {summ.get('closed', 0)})")
    lines.append(f"cross-venue    : {summ.get('n_cross_venue', 0)}")
    lines.append(f"long-tail      : {summ.get('n_long_tail', 0)}   "
                 f"hip3 {summ.get('n_hip3', 0)}")
    lines.append("")
    lines.append(f"best net APR (overall) : {_fmt_pct(summ.get('best_projected_net_apr', 0.0))}")
    lines.append(f"best net APR (OPEN)    : {_fmt_pct(summ.get('best_projected_net_apr_open', 0.0))}")
    lines.append(f"mean realised net APR  : {_fmt_pct(summ.get('mean_net_apr_realized', 0.0))}")
    if summ.get("exit_reason"):
        lines.append(f"exit reasons   : {summ['exit_reason']}")
    return "\n".join(lines)


def _funding_candidates(cutoff: float) -> str:
    rows = db_queries.get_funding_observations(would_enter_only=True, limit=2000) or []
    cands = [
        r for r in rows
        if (r.get("timestamp") or 0) >= cutoff
        and r.get("crowding_verdict") == "OPEN"
        and r.get("would_enter")
    ]
    cands.sort(key=lambda r: float(r.get("projected_net_apr") or 0.0), reverse=True)

    lines: list[str] = []
    lines.append("═══ FUNDING GO-LIVE CANDIDATES (OPEN & would_enter) ═══")
    if not cands:
        lines.append("(none — no OPEN-verdict would-enter pairs in window)")
        return "\n".join(lines)
    lines.append(f"{'symbol':<18} {'legs':<11} {'net APR':>9}  "
                 f"{'long':<10} {'short':<10} {'age_d':>6}  hip3")
    for r in cands:
        age = r.get("pair_age_days")
        age_s = f"{age:6.1f}" if age is not None else "     —"
        hip3 = "y" if r.get("is_hip3") is True else ("?" if r.get("is_hip3") is None else "n")
        lines.append(
            f"{r['symbol']:<18} {(r.get('legs') or 'single'):<11} "
            f"{_fmt_pct(r.get('projected_net_apr') or 0.0):>9}  "
            f"{(r.get('venue_long') or ''):<10} {(r.get('venue_short') or ''):<10} "
            f"{age_s}  {hip3}"
        )
    return "\n".join(lines)


def build_report(observer: str, days: float, candidates: bool) -> str:
    if observer == "funding_arb":
        return funding_report(days, candidates=candidates)
    return f"observer '{observer}' has no report section yet"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="observer_report",
        description="Read-only report over the observation ledgers.",
    )
    ap.add_argument("--observer", default="funding_arb",
                    help="which observer to report on (default: funding_arb)")
    ap.add_argument("--since", default="24h",
                    help="lookback window, e.g. 24h / 7d / 90m (default: 24h)")
    ap.add_argument("--candidates", action="store_true",
                    help="list OPEN & would-enter pairs ranked by net APR")
    args = ap.parse_args(argv)

    days = parse_since(args.since)
    print(build_report(args.observer, days, args.candidates))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
