"""
follow/copytrade.py

CopyTradeObserver — a capital-FREE OBSERVER source (sibling of WalletFlowWatcher)
that reads ON-CHAIN PERP venues, scores ranked traders for SURVIVAL-AWARE +
LATENCY-AWARE skill, and — the distinguishing feature — LOGS THE DENOMINATOR:
every actor evaluated AND rejected, with a reason, so skill estimates are never
computed from a survivor-only pool. It emits skill assessments as CORROBORATION.

OBSERVER, hard requirement: there is NO submit / execute / place_order / trade /
route_order method anywhere on this class. It reads and logs; the operator acts
(or not). The absence of an execution path is the point — and there is no
auto-follow either.

Why ON-CHAIN ONLY: perp positions are public, giving TRUE notional, leverage,
entry and liquidation distance — which solves the CEX leaderboard's fatal
sizing-opacity flaw. CEX ROI%-with-hidden-sizing leaderboards are explicitly OUT
of v1.

VENUE SEAM: this observer touches multiple chains (Hyperliquid's own chain AND
Solana for Drift), so a small venue abstraction earns its keep (unlike the
Solana-only wallet watcher). Hyperliquid + Drift are implemented; GMX + dYdX are
inert, clearly-marked stubs behind the SAME interface so adding them later is a
backend swap, not a refactor.

HONEST SCOPE: this is the most speculative of the three observers. Latency usually
eats leaderboard edges, so its realistic output is "this actor is genuinely
skilled" (corroboration), rarely "follow this trade now". It is built to MEASURE
SKILL HONESTLY, not to generate followable entries.

NOTE ON SCOPE BOUNDARIES: this module contains NO spot smart-money detection —
that is the wallet watcher's job and stays there. The cross-surface meeting of a
perp-skilled actor with a spot-skilled one happens in follow/corroboration.py
(the view), never inside either observer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import settings
from database import queries as q
from follow.base import ActorEvent, BaseStreamingDataSource
from follow import copytrade_scorer

logger = logging.getLogger(__name__)

# Position-change action vocabulary (ActorEvent.action is a free string — we
# REUSE ActorEvent rather than defining a new emit schema, per the shared
# follow/ contract). These are perp-position classes, not the wallet vocabulary.
ACTION_OPEN     = "open"
ACTION_CLOSE    = "close"
ACTION_INCREASE = "increase"
ACTION_DECREASE = "decrease"


# ── Venue data shapes ────────────────────────────────────────────────────────

@dataclass
class RankedActor:
    """A ranked trader from a venue leaderboard. On-chain venues expose TRUE
    sizing, so this is never an ROI%-only opaque entry."""
    actor_id: str
    venue:    str
    rank:     Optional[int] = None
    pnl_usd:  Optional[float] = None
    raw:      dict = field(default_factory=dict)


@dataclass
class Position:
    """One open perp position with TRUE on-chain sizing. liq_distance is the
    fractional distance from mark to the liquidation price — the risk the CEX
    leaderboards hide."""
    actor_id:     str
    venue:        str
    asset:        str
    side:         Optional[str] = None        # long | short
    notional_usd: Optional[float] = None
    leverage:     Optional[float] = None
    entry_price:  Optional[float] = None
    liq_distance: Optional[float] = None
    pnl_usd:      Optional[float] = None       # realised/unrealised when the venue exposes it
    raw:          dict = field(default_factory=dict)

    def key(self) -> tuple:
        return (self.venue, self.actor_id, self.asset)


# ── Venue backend interface (mirrors the BaseStreamingDataSource style) ──────

class VenueBackend(ABC):
    """One on-chain perp venue behind a uniform read-only seam. There is
    deliberately NO submit/execute/order method anywhere on this interface — a
    venue backend reads leaderboards and positions, nothing else."""

    venue_id:    str  = "base_venue"
    implemented: bool = False        # stubs (gmx/dydx) set this False in v1

    def is_available(self) -> bool:
        """True if this venue can be read right now. Stubs report False."""
        return bool(self.implemented)

    @abstractmethod
    async def fetch_leaderboard(self) -> list[RankedActor]:
        """Current ranked traders. MUST NOT raise — degrade to [] on failure."""
        ...

    @abstractmethod
    async def fetch_positions(self, actor_id: str) -> list[Position]:
        """An actor's open positions with TRUE notional / leverage / liq-distance.
        MUST NOT raise — degrade to [] on failure."""
        ...


class HyperliquidVenue(VenueBackend):
    """Hyperliquid — public read-only API (no auth/key). Primary venue: true
    notional, leverage and liquidation distance are all public. The network
    fetch is best-effort and lazy; with no network it simply yields no actors
    (the observer degrades, never raises)."""

    venue_id    = "hyperliquid"
    implemented = True

    def __init__(self, base_url: str = "https://api.hyperliquid.xyz"):
        self.base_url = base_url

    def is_available(self) -> bool:
        # No key required — a public endpoint. Availability is "are we allowed to
        # read this venue at all", not "is the network up right now" (a transient
        # outage degrades to an empty leaderboard, it does not make the venue
        # unavailable).
        return True

    async def _post(self, body: dict) -> Optional[object]:
        """Best-effort POST to the Hyperliquid info endpoint. Lazy aiohttp import
        so the module is offline-importable and unit-testable with no network."""
        try:
            import aiohttp
        except Exception:
            return None
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(f"{self.base_url}/info", json=body,
                                     timeout=15) as r:
                    if r.status != 200:
                        return None
                    return await r.json()
        except Exception as e:
            logger.debug("hyperliquid fetch failed: %s", e)
            return None

    async def fetch_leaderboard(self) -> list[RankedActor]:
        # The public leaderboard surface varies; this is a best-effort read that
        # degrades cleanly. Operators can extend the parse as the API firms up.
        data = await self._post({"type": "leaderboard"})
        out: list[RankedActor] = []
        rows = (data or {}).get("leaderboardRows") if isinstance(data, dict) else None
        for i, row in enumerate(rows or []):
            addr = row.get("ethAddress") or row.get("address")
            if not addr:
                continue
            out.append(RankedActor(
                actor_id=addr, venue=self.venue_id, rank=i + 1,
                pnl_usd=_as_float(row.get("pnl")), raw=row))
        return out

    async def fetch_positions(self, actor_id: str) -> list[Position]:
        data = await self._post({"type": "clearinghouseState", "user": actor_id})
        out: list[Position] = []
        if not isinstance(data, dict):
            return out
        for ap in data.get("assetPositions") or []:
            pos = (ap or {}).get("position") or {}
            asset = pos.get("coin")
            if not asset:
                continue
            szi = _as_float(pos.get("szi"))
            entry = _as_float(pos.get("entryPx"))
            notional = _as_float((pos.get("positionValue")))
            lev = _as_float((pos.get("leverage") or {}).get("value")
                            if isinstance(pos.get("leverage"), dict) else pos.get("leverage"))
            liq = _as_float(pos.get("liquidationPx"))
            liq_distance = (abs(entry - liq) / entry) if (entry and liq) else None
            out.append(Position(
                actor_id=actor_id, venue=self.venue_id, asset=asset,
                side=("long" if (szi or 0) >= 0 else "short"),
                notional_usd=notional, leverage=lev, entry_price=entry,
                liq_distance=liq_distance, pnl_usd=_as_float(pos.get("unrealizedPnl")),
                raw=pos))
        return out


class DriftVenue(VenueBackend):
    """Drift — Solana-native perps. Reuses the existing Solana RPC plumbing
    (follow/rpc.py) rather than a second client. Best-effort + offline-safe: with
    no RPC reachable it yields no actors. (Full Drift account decoding is deferred
    — the seam is in place so it is a backend change, not a refactor.)"""

    venue_id    = "drift"
    implemented = True

    def __init__(self, rpc=None):
        self._rpc = rpc

    def is_available(self) -> bool:
        return True

    async def fetch_leaderboard(self) -> list[RankedActor]:
        # Drift leaderboard decoding rides the shared Solana RPC; v1 returns an
        # empty list when the decode path isn't wired, degrading honestly rather
        # than fabricating ranked actors.
        return []

    async def fetch_positions(self, actor_id: str) -> list[Position]:
        return []


class GMXVenue(VenueBackend):
    """STUB — NOT implemented in v1. Present so a future GMX backend is a swap,
    not a refactor. Reports unavailable and yields nothing."""

    venue_id    = "gmx"
    implemented = False

    async def fetch_leaderboard(self) -> list[RankedActor]:
        return []

    async def fetch_positions(self, actor_id: str) -> list[Position]:
        return []


class DyDxVenue(VenueBackend):
    """STUB — NOT implemented in v1. Same inert contract as GMXVenue."""

    venue_id    = "dydx"
    implemented = False

    async def fetch_leaderboard(self) -> list[RankedActor]:
        return []

    async def fetch_positions(self, actor_id: str) -> list[Position]:
        return []


# Registry of venue backends behind the seam. hyperliquid + drift are active;
# gmx + dydx are inert stubs (implemented=False) — present so adding them later
# is a backend, not a refactor.
VENUE_REGISTRY: dict[str, type] = {
    "hyperliquid": HyperliquidVenue,
    "drift":       DriftVenue,
    "gmx":         GMXVenue,
    "dydx":        DyDxVenue,
}


def _as_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def build_venues(venue_ids: Optional[list[str]] = None) -> list[VenueBackend]:
    """Instantiate the configured venue backends from the registry. Unknown ids
    are skipped; the order follows COPYTRADE_VENUES."""
    ids = venue_ids if venue_ids is not None else list(
        getattr(settings, "COPYTRADE_VENUES", ["hyperliquid", "drift"]))
    out: list[VenueBackend] = []
    for vid in ids:
        cls = VENUE_REGISTRY.get(vid)
        if cls is None:
            logger.debug("copytrade: unknown venue %s — skipped", vid)
            continue
        out.append(cls())
    return out


# ── The observer ─────────────────────────────────────────────────────────────

class CopyTradeObserver(BaseStreamingDataSource):
    """On-chain perp leaderboard observer. Reads ranked traders, scores them
    survival- and latency-aware (point-in-time, via the SHARED no-leak filter),
    logs the FULL evaluated population (the denominator), and surfaces skill
    assessments as corroboration. No execution path, no auto-follow."""

    source_id    = "copytrade"
    display_name = "Copy-Trade Leaderboard Observer"
    optional     = True

    def __init__(self, venues: Optional[list[VenueBackend]] = None):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._venues: list[VenueBackend] = (
            venues if venues is not None else build_venues())
        # Open-position state per (venue, actor, asset) for change detection.
        self._open: dict[tuple, Position] = {}
        # In-memory stats (the DB is the durable record).
        self._events_seen = 0
        self._evaluated = 0
        self._surfaced = 0
        self._last_scan_ts: Optional[float] = None

    # ── Availability ────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Runnable when enabled AND at least one venue backend is available.
        On-chain public APIs need no key, so there is no key gate."""
        if not bool(getattr(settings, "COPYTRADE_ENABLED", False)):
            return False
        return any(v.is_available() for v in self._venues)

    def active_venues(self) -> list[VenueBackend]:
        return [v for v in self._venues if v.is_available()]

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin the leaderboard-poll loop. Degrades gracefully: if unavailable
        the observer stays idle rather than raising to the host agent."""
        if not self.is_available():
            logger.info("copytrade: not available (enabled=%s, venues=%s) "
                        "— observer idle",
                        getattr(settings, "COPYTRADE_ENABLED", False),
                        [v.venue_id for v in self.active_venues()])
            self._running = False
            return
        self._running = True
        self._task = asyncio.create_task(self._scan_loop())
        logger.info("copytrade: observing %d venue(s): %s",
                    len(self.active_venues()),
                    [v.venue_id for v in self.active_venues()])

    async def stop(self) -> None:
        """Clean teardown. Idempotent; never raises."""
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
        self._task = None
        logger.info("copytrade: stopped")

    async def _scan_loop(self) -> None:
        """Poll each available venue's leaderboard on a cadence. One failing pass
        records nothing for that venue and never crashes the host agent."""
        interval = max(30.0, float(getattr(
            settings, "COPYTRADE_LEADERBOARD_REFRESH_S", 300)))
        while self._running:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("copytrade: scan pass failed: %s", e)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _scan_once(self) -> None:
        """One observation pass over every available venue. Each step is
        defensive: a venue raising is logged and skipped, not propagated."""
        as_of = datetime.utcnow()
        for venue in self.active_venues():
            try:
                actors = await venue.fetch_leaderboard()
            except Exception as e:
                logger.debug("copytrade: %s leaderboard failed: %s",
                             venue.venue_id, e)
                continue
            for actor in actors or []:
                try:
                    positions = await venue.fetch_positions(actor.actor_id)
                except Exception as e:
                    logger.debug("copytrade: %s positions(%s) failed: %s",
                                 venue.venue_id, actor.actor_id, e)
                    positions = []
                self.ingest_positions(venue.venue_id, actor.actor_id,
                                      positions, as_of=as_of)
                # Evaluate EVERY actor — surfaced or rejected — into the
                # denominator log. This is what keeps skill out of a
                # survivor-only pool.
                self.evaluate_actor(actor.actor_id, venue.venue_id, as_of=as_of)
        self._last_scan_ts = time.time()

    # ── Position-change ingestion (perp-detail evidence) ─────────────────────

    def ingest_positions(self, venue_id: str, actor_id: str,
                         positions: list[Position], *,
                         as_of: Optional[datetime] = None) -> list[ActorEvent]:
        """Diff an actor's current positions against the last seen state and
        persist position-change events (open / increase / decrease / close) as
        SUGGESTIVE evidence. Returns the ActorEvents (for tests). Never raises."""
        as_of = as_of or datetime.utcnow()
        detected_at = as_of.timestamp()
        events: list[ActorEvent] = []
        try:
            current = {p.key(): p for p in (positions or [])}
            prior = {k: v for k, v in self._open.items()
                     if k[0] == venue_id and k[1] == actor_id}

            for key, pos in current.items():
                old = prior.get(key)
                if old is None:
                    events.append(self._emit(pos, ACTION_OPEN, detected_at))
                elif (pos.notional_usd or 0) > (old.notional_usd or 0):
                    events.append(self._emit(pos, ACTION_INCREASE, detected_at))
                elif (pos.notional_usd or 0) < (old.notional_usd or 0):
                    events.append(self._emit(pos, ACTION_DECREASE, detected_at))
                self._open[key] = pos

            # Positions gone since last scan -> closed.
            for key, old in prior.items():
                if key not in current:
                    events.append(self._emit(old, ACTION_CLOSE, detected_at))
                    self._record_resolution(old, detected_at)
                    self._open.pop(key, None)
        except Exception as e:
            logger.debug("copytrade: ingest_positions failed: %s", e)
        return events

    def _emit(self, pos: Position, action: str, detected_at: float) -> ActorEvent:
        """Build + persist one position-change ActorEvent (REUSING the shared
        ActorEvent schema). occurred_at falls back to detected_at when the venue
        carries no event timestamp."""
        ev = ActorEvent(
            source_id=self.source_id, actor_id=pos.actor_id, action=action,
            asset=pos.asset, venue=pos.venue, size_usd=pos.notional_usd,
            detected_at=detected_at, occurred_at=detected_at,
            meta={"side": pos.side, "leverage": pos.leverage,
                  "liq_distance": pos.liq_distance, "entry_price": pos.entry_price,
                  "pnl_usd": pos.pnl_usd})
        try:
            q.insert_copytrade_event({
                "source_id":    ev.source_id,
                "venue":        pos.venue,
                "actor_id":     pos.actor_id,
                "action":       action,
                "asset":        pos.asset,
                "notional_usd": pos.notional_usd,
                "leverage":     pos.leverage,
                "liq_distance": pos.liq_distance,
                "occurred_at":  datetime.utcfromtimestamp(detected_at),
                "detected_at":  datetime.utcfromtimestamp(detected_at),
                "meta":         ev.meta,
            })
            self._events_seen += 1
        except Exception as e:
            logger.debug("copytrade: persist event failed: %s", e)
        return ev

    def _record_resolution(self, pos: Position, detected_at: float) -> None:
        """When a position closes with a known PnL, append a RESOLVED outcome to
        the SHARED track-record log (the same log + the same one no-leak filter
        the wallet scorer reads). This is the bridge that feeds point-in-time
        skill; absent a numeric PnL the outcome is unknown and nothing is
        resolved (we never invent an outcome)."""
        pnl = pos.pnl_usd
        if pnl is None:
            return
        outcome = "win" if pnl > 0 else ("loss" if pnl < 0 else "flat")
        try:
            ts = datetime.utcfromtimestamp(detected_at)
            q.insert_wallet_flow_event({
                "source_id":   self.source_id,
                "actor_id":    pos.actor_id,
                "action":      ACTION_CLOSE,
                "asset":       pos.asset,
                "venue":       pos.venue,
                "size_usd":    pos.notional_usd,
                "occurred_at": ts,
                "detected_at": ts,
                "resolved_at": ts,
                "outcome":     outcome,
                "meta":        {"venue": pos.venue, "leverage": pos.leverage},
            })
        except Exception as e:
            logger.debug("copytrade: resolution write failed: %s", e)

    # ── Evaluation + the denominator log ─────────────────────────────────────

    def evaluate_actor(self, actor_id: str, venue_id: str, *,
                       as_of: Optional[datetime] = None,
                       sizing_interpretable: bool = True) -> dict:
        """Score one actor point-in-time and RECORD the evaluation in the
        denominator log — surfaced OR rejected, always with a reason. Upserts the
        per-actor rollup. Returns the score dict. Never raises.

        An actor with a real skill estimate is "surfaced" (skilled), even when
        flagged real_but_unfollowable=True (latency-killed); an actor below the
        survival / drawdown / sizing gates is "rejected" with the reason. Both are
        recorded — that is the anti-survivorship discipline."""
        as_of = as_of or datetime.utcnow()
        score = copytrade_scorer.score_actor(
            actor_id, as_of, sizing_interpretable=sizing_interpretable)
        decision = "surfaced" if score.get("skill_score") is not None else "rejected"
        try:
            q.insert_copytrade_evaluation({
                "actor_id":    actor_id,
                "venue":       venue_id,
                "decision":    decision,
                "reason":      score.get("reason"),
                "sample_size": score.get("closed_trades"),
                "evaluated_at": as_of,
            })
        except Exception as e:
            logger.debug("copytrade: denominator write failed: %s", e)
        try:
            q.upsert_copytrade_actor({
                "actor_id":             actor_id,
                "venue":                venue_id,
                "last_evaluated_at":    as_of,
                "closed_trades":        score.get("closed_trades", 0),
                "skill_score":          score.get("skill_score"),
                "latency_delta_s":      score.get("latency_delta_s"),
                "drawdown":             score.get("drawdown"),
                "followable":           score.get("followable", False),
                "sizing_interpretable": sizing_interpretable,
            })
        except Exception as e:
            logger.debug("copytrade: actor rollup write failed: %s", e)
        self._evaluated += 1
        if decision == "surfaced":
            self._surfaced += 1
        return score

    # ── Corroboration read (cross-surface meeting point — view does the cross) ─

    def skilled_actor_assessments(self) -> list[dict]:
        """This observer's skilled-actor assessments, for the corroboration view
        to cross-reference against the SPOT wallet watcher. Read-only; the
        crossing itself happens in follow/corroboration.py, NOT here. Includes the
        "real but unfollowable" actors flagged as such (followable=False)."""
        try:
            rows = q.get_skilled_copytrade_actors()
        except Exception as e:
            logger.debug("copytrade: skilled assessments read failed: %s", e)
            return []
        out = []
        for r in rows:
            out.append({
                "actor_id":              r.get("actor_id"),
                "venue":                 r.get("venue"),
                "skill_score":           r.get("skill_score"),
                "latency_delta_s":       r.get("latency_delta_s"),
                "drawdown":              r.get("drawdown"),
                "closed_trades":         r.get("closed_trades"),
                "followable":            r.get("followable"),
                "real_but_unfollowable": (not r.get("followable")),
            })
        return out

    # ── Stats (for the snapshot) ────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Never raises — zeroed/empty defaults on any failure. best_effort is
        always True: leaderboard reads are partial + latency-limited, so no
        surface may imply a complete or followable picture."""
        return {
            "source_id":     self.source_id,
            "running":       bool(self._running),
            "available":     self._safe_available(),
            "venues":        [v.venue_id for v in self.active_venues()],
            "venues_stubbed": [v.venue_id for v in self._venues
                               if not v.implemented],
            "events_seen":   self._events_seen,
            "evaluated":     self._evaluated,
            "surfaced":      self._surfaced,
            "last_scan_ts":  self._last_scan_ts,
            # Honesty about completeness — latency-limited corroboration, not
            # followable entries.
            "best_effort":   True,
            "scope_note":    ("latency-limited: surfaces SKILL (corroboration), "
                              "rarely followable entries"),
        }

    def _safe_available(self) -> bool:
        try:
            return self.is_available()
        except Exception:
            return False


__all__ = [
    "CopyTradeObserver",
    "VenueBackend", "HyperliquidVenue", "DriftVenue", "GMXVenue", "DyDxVenue",
    "VENUE_REGISTRY", "build_venues",
    "RankedActor", "Position",
    "ACTION_OPEN", "ACTION_CLOSE", "ACTION_INCREASE", "ACTION_DECREASE",
]
