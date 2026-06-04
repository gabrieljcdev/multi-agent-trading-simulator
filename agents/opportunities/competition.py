"""
agents/opportunities/competition.py

Stage 3's heartbeat — competition measurement + trajectory. The
headline field of the whole pipeline is competitor_trend, not edge
magnitude (time dominates magnitude, §1.2); a separate agent loop
re-measures on OPPORTUNITY_COMPETITION_RECHECK_SEC because edge decays
and a stale trajectory is a lie (§5).

Per-type recipe (this pass implements liquidation only; seams below):
  liquidation — group-by msg.sender on the market's Liquidate event
                logs over a trailing window → distinct liquidator count.
                Default event source is the same Morpho GraphQL API the
                createmarket detector reads; tests inject sample logs.
  chain (stub) — searcher-address sampling from MEV dashboards.
  pool  (stub) — LP concentration / distinct-LP count.
  perp  (stub) — distinct-taker / market-maker sampling on the venue.

competitor_trend transitions (zero → rising_slow → rising_fast →
saturated) are parameterised by settings constants seeded with
conservative placeholders.
# TODO: validate against observed window closures, do not trust until
# calibrated — the exact rate thresholds are an UNRESOLVED §6 open
# question; gathering the closures that calibrate them is the point of
# the observation phase.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from config import settings

logger = logging.getLogger(__name__)

TREND_ZERO        = "zero"
TREND_RISING_SLOW = "rising_slow"
TREND_RISING_FAST = "rising_fast"
TREND_SATURATED   = "saturated"

WINDOW_OPENING = "opening"
WINDOW_OPEN    = "open"
WINDOW_CLOSING = "closing"
WINDOW_CLOSED  = "closed"

# Query shape verified live against the Morpho API on 2026-06-04.
_LIQUIDATIONS_QUERY = """
query MarketLiquidations($key: String!, $since: Int!) {
  marketTransactions(
    first: 200, orderBy: Timestamp, orderDirection: Desc,
    where: { type_in: [Liquidation], marketUniqueKey_in: [$key],
             timestamp_gte: $since }
  ) {
    items {
      timestamp
      data { __typename
             ... on MarketTransactionLiquidationData { liquidator } }
    }
  }
}
"""


async def fetch_morpho_liquidation_events(market_key: str,
                                          since_ts: int) -> list[dict]:
    """Default liquidation-event source — the Morpho GraphQL API the
    createmarket detector already reads (no parallel HTTP stack).
    Returns [] on any failure; the measurer treats that as 'no events
    observed this check', never as an error to raise."""
    timeout = aiohttp.ClientTimeout(total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                settings.OPPORTUNITY_MORPHO_API_URL,
                json={"query": _LIQUIDATIONS_QUERY,
                      "variables": {"key": market_key, "since": int(since_ts)}},
            ) as r:
                payload = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        logger.debug("[competition] liquidation fetch failed: %s", e)
        return []
    if not isinstance(payload, dict) or payload.get("errors"):
        logger.debug("[competition] liquidation query errors: %s",
                     (payload or {}).get("errors"))
        return []
    return (((payload.get("data") or {}).get("marketTransactions") or {})
            .get("items") or [])


def count_distinct_liquidators(events: list[dict]) -> int:
    """Distinct msg.sender over the event list. Tolerant of the field
    living at data.liquidator, sender, or user.address depending on the
    event source — tests pin the behaviour with sample logs."""
    senders: set[str] = set()
    for ev in events or []:
        addr = (
            ((ev.get("data") or {}).get("liquidator"))
            or ev.get("liquidator")
            or ev.get("sender")
            or ((ev.get("user") or {}).get("address"))
        )
        if addr:
            senders.add(str(addr).lower())
    return len(senders)


def derive_trend(count: int, history: list[dict]) -> str:
    """Trend from the rate of change of competitor_count over the
    history window. Thresholds from settings — conservative
    placeholders. # TODO: calibrate against observed window closures."""
    count = int(count or 0)
    if count >= int(settings.OPPORTUNITY_SATURATED_COUNT):
        return TREND_SATURATED

    rate = _competitors_per_day(history, count)
    if rate >= float(settings.OPPORTUNITY_TREND_RISING_FAST_PER_DAY):
        return TREND_RISING_FAST
    if rate >= float(settings.OPPORTUNITY_TREND_RISING_SLOW_PER_DAY):
        return TREND_RISING_SLOW
    if count == 0:
        return TREND_ZERO
    # Non-zero but flat: someone is already there — the conservative
    # read inside the fixed vocabulary is rising_slow.
    # TODO: calibrate — flat-but-occupied may deserve its own label once
    # observed closures say how those windows actually behave.
    return TREND_RISING_SLOW


def derive_window_status(trend: str, count: int) -> str:
    """Window lifecycle from trend + count, same parameterisation.
    # TODO: calibrate against observed window closures (§6)."""
    if trend == TREND_SATURATED:
        return WINDOW_CLOSED
    if trend == TREND_RISING_FAST:
        return WINDOW_CLOSING
    if trend == TREND_ZERO:
        return WINDOW_OPENING if int(count or 0) == 0 else WINDOW_OPEN
    return WINDOW_OPEN


def _competitors_per_day(history: list[dict], current_count: int) -> float:
    """Slope (new competitors/day) across the trailing history window.
    History entries are {ts: iso8601, count: int}; entries outside
    OPPORTUNITY_COMPETITION_WINDOW_H are ignored."""
    cutoff = (datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()
              - float(settings.OPPORTUNITY_COMPETITION_WINDOW_H) * 3600.0)
    pts: list[tuple[float, int]] = []
    for h in history or []:
        try:
            ts = datetime.fromisoformat(str(h["ts"]))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            t = ts.timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if t >= cutoff:
            pts.append((t, int(h.get("count") or 0)))
    if not pts:
        return 0.0
    pts.sort()
    t0, c0 = pts[0]
    t1 = datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()
    span_days = (t1 - t0) / 86400.0
    if span_days <= 0:
        return 0.0
    return (int(current_count or 0) - c0) / span_days


class CompetitionMeasurer:
    """measure(core_row) → (competitor_count, competitor_trend,
    count_history_append). Per-type recipe; the event fetcher is
    injectable so tests run from sample event logs."""

    def __init__(self, liquidation_event_fetcher=None):
        self._fetch_liquidations = (liquidation_event_fetcher
                                    or fetch_morpho_liquidation_events)

    async def measure(self, core_row: dict) -> tuple[int, str, list[dict]]:
        opp_type = core_row.get("opp_type")
        history  = list(core_row.get("competitor_count_history") or [])

        if opp_type == "liquidation":
            since = int(
                datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()
                - float(settings.OPPORTUNITY_COMPETITION_WINDOW_H) * 3600.0
            )
            events = await self._fetch_liquidations(
                core_row.get("market_key"), since) or []
            count = count_distinct_liquidators(events)
        else:
            # Stub recipes (chain / pool / perp) — carry the last known
            # count until their sampling sources are built.
            count = int(core_row.get("competitor_count") or 0)

        trend = derive_trend(count, history)
        append = [{
            "ts":    datetime.utcnow().isoformat(),
            "count": int(count),
        }]
        return int(count), trend, append


# Module-level singleton (project convention).
competition_measurer = CompetitionMeasurer()

__all__ = [
    "CompetitionMeasurer", "competition_measurer",
    "count_distinct_liquidators", "derive_trend", "derive_window_status",
    "fetch_morpho_liquidation_events",
    "TREND_ZERO", "TREND_RISING_SLOW", "TREND_RISING_FAST", "TREND_SATURATED",
    "WINDOW_OPENING", "WINDOW_OPEN", "WINDOW_CLOSING", "WINDOW_CLOSED",
]
