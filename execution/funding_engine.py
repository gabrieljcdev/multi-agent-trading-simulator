"""
execution/funding_engine.py

Funding-rate arbitrage engine for the FundingArbAgent (Phase 1).

Phase 1 scope (held tight):
  * Single venue: Binance. Variant: delta-neutral (long spot + short perp).
  * Funding read from CCXT Binance PUBLIC fetch_funding_rate — no API keys.
  * Majors only: settings.FUNDING_SYMBOLS = ["BTC/USDT", "ETH/USDT"].
  * OBSERVATION MODE is a hard gate. While settings.FUNDING_OBSERVATION_MODE
    is True, no orders reach any exchange — _place asserts that
    settings.SIM_MODE is True before routing through OrderRouter, which then
    writes a sim Trade row. The agent additionally bypasses open() entirely
    in observation mode and writes to funding_arb_observations instead.

Design echo of execution/arb_engine.py:
  * Both legs always via a single asyncio.gather — never sequential.
  * Per-symbol asyncio.Lock + class-level Semaphore so two scans can't
    race on the same pair and total concurrent opens are capped.
  * Sim slippage is a flat ±FUNDING_SIM_SLIPPAGE_PCT applied to mid —
    same shape as ArbEngine._sim_fills, simpler model (Phase 1).

Funding APR annualisation:
  * Binance funds every 8h, so funding_apr = rate_8h * (24/8) * 365 = rate_8h * 1095.
  * The 1095 constant is mandated by the spec — it's also what tests pin.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import settings
from database import queries as db_queries
from execution.funding_venues import (
    REGISTERED_FUNDING_VENUES,
    BaseFundingVenue,
    BinanceFundingVenue,
    FundingQuote,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class FundingOpportunity:
    """One funding-rate carry opportunity.

    Two shapes share this struct, distinguished by `legs`:

      * legs="single"      — one venue's funding (the Phase-1 shape).
        venue_long == venue_short; funding_apr is that venue's annualised
        rate; spread_apr is the bid-ask leg cost (usually 0).
      * legs="cross_venue" — Phase-2 delta-neutral carry across two venues
        for the SAME symbol. venue_short is the richer-funding leg (short to
        collect), venue_long the cheaper leg (long to stay delta-neutral).
        funding_apr carries the spread (for sorting/coarse view), spread_apr
        is funding_apr(short) − funding_apr(long) (annualised, so already
        interval-normalised), and projected_net_apr nets out round-trip fees
        + the interval-risk buffer. Nothing is placed on either leg.
    """
    symbol:      str
    variant:     str
    venue_long:  str
    venue_short: str
    funding_apr: float          # single: venue rate; cross: spread (annualised)
    spread_apr: float           # cross-venue funding spread (annualised)
    oi_usd:     float
    depth_ok:   bool
    # ── cross-venue extensions (defaults keep the single-venue shape) ────
    legs:                 str             = "single"
    projected_net_apr:    Optional[float] = None    # spread − fees − interval-risk
    funding_interval_sec: Optional[float] = None
    taker_fee_bps:        float           = 0.0
    maker_fee_bps:        float           = 0.0
    funding_apr_long:     Optional[float] = None     # the long leg's annualised funding
    funding_apr_short:    Optional[float] = None     # the short leg's annualised funding


@dataclass
class FundingPosition:
    """A would-be (or sim) funding-rate carry position.

    opened_at defaults to time.time() at construction so callers don't
    have to remember to stamp it — matches arb_engine's ArbResult shape.
    """
    opp:               FundingOpportunity
    notional_usd:      float
    margin_used:       float
    basis_at_entry:    float
    funding_collected: float = 0.0
    fees_paid:         float = 0.0
    opened_at:         float = field(default_factory=time.time)


# ─────────────────────────────────────────────────────────────────────────
# FundingEngine
# ─────────────────────────────────────────────────────────────────────────

class FundingEngine:
    """Scan + simulated execution for the funding-rate carry trade.

    Construct with no args. Tests can inject a `ccxt_factory` callable
    returning a stub binance client (so fetch_funding_rate is mockable
    without hitting the network); production code uses the real
    ccxt.async_support.binance().
    """

    def __init__(self, ccxt_factory=None, venues: Optional[list[BaseFundingVenue]] = None):
        # Per-symbol re-entry locks — exactly like ArbEngine._symbol_locks.
        self._symbol_locks: dict[str, asyncio.Lock] = {
            sym: asyncio.Lock() for sym in settings.FUNDING_SYMBOLS
        }
        # Cap concurrent opens. Module-level singleton holds one instance
        # per process so the semaphore counts agent-wide.
        self._semaphore = asyncio.Semaphore(settings.FUNDING_MAX_CONCURRENT)
        # Optional injection: tests can supply a callable returning a stub
        # binance client. None → build a real ccxt.async_support.binance().
        self._ccxt_factory = ccxt_factory

        # ── Funding-venue plugin layer ──────────────────────────────────
        # The engine iterates a list of BaseFundingVenue plugins instead of
        # reaching into ccxt directly. Resolution order:
        #   * explicit venues=        → use them (chains-engine-style injection)
        #   * ccxt_factory= (legacy)  → binance-only with the injected stub,
        #     so the single-venue observation output is BYTE-IDENTICAL to
        #     Phase 1 (the existing test suite pins this).
        #   * neither (production)    → REGISTERED_FUNDING_VENUES, filtered by
        #     settings.FUNDING_VENUES_ENABLED and is_available().
        if venues is not None:
            self._venues: list[BaseFundingVenue] = list(venues)
        elif ccxt_factory is not None:
            self._venues = [BinanceFundingVenue(ccxt_factory=ccxt_factory)]
        else:
            enabled = set(
                getattr(settings, "FUNDING_VENUES_ENABLED", ["binance"]) or ["binance"]
            )
            self._venues = [
                v for v in REGISTERED_FUNDING_VENUES
                if v.venue_id in enabled and v.is_available()
            ]

        # Most recent scan's raw quotes, keyed by (venue_id, symbol) — kept
        # so Phase 2 can build cross-venue carry from the same snapshot
        # without re-fetching. Refreshed every scan().
        self._last_quotes: dict[tuple[str, str], FundingQuote] = {}

        # Sim/health flag the venue-health exit checks against. Cleared by
        # a successful scan, set when no venue returned a usable quote.
        self._venue_healthy: bool = True

    # ── Public API ──────────────────────────────────────────────────────

    async def scan(self) -> list[FundingOpportunity]:
        """Build one single-venue carry opportunity per (venue, symbol),
        filtered by |APR|.

        Iterates every registered funding venue (Binance, Hyperliquid, …)
        through the plugin layer — a venue/pair that errors or isn't listed
        is skipped with no crash. The raw quotes are cached in
        self._last_quotes so the cross-venue pass (Phase 2) reuses the same
        snapshot. On total failure returns [] — the agent loop treats that
        as 'nothing to do this tick' rather than halting.
        """
        opps: list[FundingOpportunity] = []
        self._last_quotes = {}
        any_success = False
        for venue in self._venues:
            for symbol in settings.FUNDING_SYMBOLS:
                quote = await venue.fetch_funding(symbol)
                if quote.error is not None or quote.funding_apr is None:
                    continue
                any_success = True
                self._last_quotes[(venue.venue_id, symbol)] = quote
                opps.extend(self._build_opportunities(
                    symbol, venue.venue_id, quote.funding_apr, quote.oi_usd,
                ))
        # venue_health reflects whether ANY venue produced a usable quote
        # this scan — drives the venue_health exit reason. (Matches the old
        # single-venue semantics: success → healthy, total failure → not.)
        self._venue_healthy = any_success or not self._venues

        single = self._filter(opps)
        # Phase 2 — cross-venue carry. Built from the SAME snapshot
        # (self._last_quotes) so no extra fetch; observation-only.
        cross = (
            self._build_cross_venue()
            if getattr(settings, "FUNDING_CROSS_VENUE_ENABLED", False)
            else []
        )
        return single + cross

    # ── Cross-venue carry (Phase 2) ─────────────────────────────────────

    def _build_cross_venue(self) -> list[FundingOpportunity]:
        """Build a delta-neutral cross-venue carry per symbol present on ≥2
        available venues, from this scan's cached quotes.

        Legs: short the richer-funding venue (collect funding), long the
        cheaper one (stay delta-neutral). spread_apr differences the two
        ANNUALISED funding rates — which is interval-normalised by
        construction, because each quote was annualised through its own
        funding_interval_sec (Binance 8h vs Hyperliquid 1h) before landing
        here. projected_net_apr then nets fees + the interval-risk buffer.

        OBSERVATION ONLY — this returns opportunities to log; nothing is
        placed. The cross-venue open() leg map is a separate, gated build.
        """
        # symbol -> {venue_id: quote}
        by_symbol: dict[str, dict[str, FundingQuote]] = {}
        for (venue_id, symbol), q in self._last_quotes.items():
            by_symbol.setdefault(symbol, {})[venue_id] = q

        out: list[FundingOpportunity] = []
        for symbol, venue_quotes in by_symbol.items():
            if len(venue_quotes) < 2:
                continue
            items = list(venue_quotes.items())          # [(venue_id, quote)]
            short_vid, short_q = max(items, key=lambda kv: kv[1].funding_apr)
            long_vid,  long_q  = min(items, key=lambda kv: kv[1].funding_apr)
            if short_vid == long_vid:
                continue

            spread_apr = short_q.funding_apr - long_q.funding_apr
            net_apr    = self._cross_net_apr(spread_apr, long_q, short_q)

            out.append(FundingOpportunity(
                symbol=symbol,
                variant="delta_neutral",
                venue_long=long_vid,
                venue_short=short_vid,
                funding_apr=spread_apr,          # spread drives sort/coarse view
                spread_apr=spread_apr,
                oi_usd=min(long_q.oi_usd, short_q.oi_usd),   # the tighter leg caps size
                depth_ok=(bool(short_q.depth_ok) and bool(long_q.depth_ok)),
                legs="cross_venue",
                projected_net_apr=net_apr,
                funding_interval_sec=min(
                    long_q.funding_interval_sec, short_q.funding_interval_sec,
                ),
                taker_fee_bps=short_q.taker_fee_bps + long_q.taker_fee_bps,
                maker_fee_bps=short_q.maker_fee_bps + long_q.maker_fee_bps,
                funding_apr_long=long_q.funding_apr,
                funding_apr_short=short_q.funding_apr,
            ))
        return out

    @staticmethod
    def _cross_net_apr(
        spread_apr: float, long_q: FundingQuote, short_q: FundingQuote,
    ) -> float:
        """projected_net_apr = spread_apr − round_trip_fees_apr − interval_risk.

        round-trip fees = entry + exit on BOTH legs (4 fills). Maker fees are
        preferred when FUNDING_REQUIRE_MAKER_FEES is set and a maker tier is
        available (the documented ~1.3 bps/8h break-even assumes maker fills);
        a leg with only a taker tier falls back to taker. Both the fee drag
        and the interval-risk buffer are annualised over the assumed hold
        (FUNDING_MAX_HOLD_SEC) — a per-hold haircut, consistent with the
        single-venue fee model. funding_interval_sec is persisted so a future
        calibration can switch the buffer to a per-interval accrual if the
        soak data warrants; this stays the calibration seam.
        """
        use_maker = bool(getattr(settings, "FUNDING_REQUIRE_MAKER_FEES", True))

        def _leg_fee(q: FundingQuote) -> float:
            if use_maker and q.maker_fee_bps > 0:
                return q.maker_fee_bps
            return q.taker_fee_bps

        rt_fee_bps = 2.0 * (_leg_fee(long_q) + _leg_fee(short_q))
        hold = max(1.0, float(settings.FUNDING_MAX_HOLD_SEC))
        year = 365.0 * 86400.0
        rt_fees_apr = (rt_fee_bps / 1e4) * (year / hold)
        buffer_apr  = (
            float(settings.FUNDING_FUNDING_INTERVAL_RISK_BPS) / 1e4
        ) * (year / hold)
        return spread_apr - rt_fees_apr - buffer_apr

    async def open(self, opp: FundingOpportunity) -> None:
        """Open both legs of a delta-neutral carry concurrently.

        Acquires the per-symbol lock + the engine semaphore (exactly like
        arb_engine), sizes off settings.FUNDING_MAX_NOTIONAL_USD and the
        OI fraction cap, then fires both legs via a single asyncio.gather.

        Live order placement is gated through OrderRouter (sim writes a
        Trade row; live is a stub that returns None). FUNDING_OBSERVATION_MODE
        adds a hard assertion that SIM_MODE must be True before any leg
        reaches the router — so a misconfigured Phase-1 deployment cannot
        accidentally route an order. The agent's loop additionally bypasses
        open() entirely in observation mode.
        """
        if settings.FUNDING_OBSERVATION_MODE:
            # Defence in depth: observation mode is a HARD GATE. Even if the
            # agent loop misroutes a call to open(), this assert refuses to
            # touch the router unless SIM_MODE is also True. With SIM_MODE
            # True the router's _sim_execute writes a sim Trade row only —
            # no live exchange call is reachable from here.
            assert settings.SIM_MODE, (
                "FUNDING_OBSERVATION_MODE refuses to open while SIM_MODE is False"
            )
        # Phase-1 open() supports only delta_neutral (long-spot + short-perp).
        # reverse_carry is observation-only until Phase 2 wires the inverse
        # leg map; refuse here so a future code path that bypasses the
        # FUNDING_OBSERVATION_MODE check can't route the wrong pair.
        if opp.variant != "delta_neutral":
            logger.warning(
                "[FundingEngine] refusing to open variant=%s (observation-only)",
                opp.variant,
            )
            return

        lock = self._symbol_locks.get(opp.symbol)
        if lock is None:
            # First-seen symbol (e.g. tests injecting a new pair) — create
            # the lock on demand so we still enforce single-flight per pair.
            lock = asyncio.Lock()
            self._symbol_locks[opp.symbol] = lock
        if lock.locked():
            # Per-symbol re-entry guard — somebody else already on this pair.
            return

        async with lock, self._semaphore:
            notional = min(
                float(settings.FUNDING_MAX_NOTIONAL_USD),
                float(opp.oi_usd) * float(settings.FUNDING_MAX_OI_FRACTION),
            )
            margin_used = notional / max(float(settings.FUNDING_TARGET_LEVERAGE), 1e-9)
            basis_at_entry = self._basis(opp)

            pos = FundingPosition(
                opp=opp,
                notional_usd=notional,
                margin_used=margin_used,
                basis_at_entry=basis_at_entry,
            )
            # BOTH legs concurrently — never sequential.
            await asyncio.gather(
                self._place(pos, leg="long"),
                self._place(pos, leg="short"),
            )

    def exit_reason(self, pos: FundingPosition) -> Optional[str]:
        """Return the first matching exit reason in priority order, or None.

        Priority (mandated by spec):
          1. funding_decay  — opp.funding_apr below FUNDING_FLIP_EXIT_APR.
          2. basis_blowout  — perp-spot drift > FUNDING_BASIS_SIGMA_EXIT * sigma.
          3. margin_breach  — margin ratio below FUNDING_MARGIN_ALERT_RATIO * maint.
          4. venue_health   — fetch_funding_rate has been erroring (stale feed).
          5. max_hold       — position age > FUNDING_MAX_HOLD_SEC.
        """
        if pos.opp.funding_apr < settings.FUNDING_FLIP_EXIT_APR:
            return "funding_decay"
        if self._basis_blowout(pos):
            return "basis_blowout"
        if self._margin_breach(pos):
            return "margin_breach"
        if self._venue_unhealthy(pos):
            return "venue_health"
        age = time.time() - pos.opened_at
        if age > settings.FUNDING_MAX_HOLD_SEC:
            return "max_hold"
        return None

    async def close(self) -> None:
        """Close every venue's held client. Called from the agent's stop()."""
        for venue in self._venues:
            try:
                await venue.close()
            except Exception as e:
                logger.debug(f"FundingEngine close {venue.venue_id}: {e}")

    # ── Internal helpers ────────────────────────────────────────────────

    def _get_exchange(self):
        """Return the binance venue's cached ccxt client (built lazily).

        Kept on the engine surface for backward compatibility — the binance
        funding venue now owns the client. Returns None when no binance
        venue is active.
        """
        for venue in self._venues:
            if getattr(venue, "venue_id", None) == "binance":
                return venue._get_exchange()
        return None

    async def _fetch_native_funding(
        self, venue: str, symbol: str,
    ) -> tuple[Optional[float], float]:
        """Return (funding_apr, oi_usd) for one (venue, symbol) via the
        plugin layer — a thin shim over BaseFundingVenue.fetch_funding.

        On any error returns (None, 0.0) so legacy callers drop the symbol
        for this scan tick. Resolution by venue_id; an unknown venue id
        returns (None, 0.0).
        """
        plugin = next((v for v in self._venues if v.venue_id == venue), None)
        if plugin is None:
            self._venue_healthy = False
            return None, 0.0
        quote = await plugin.fetch_funding(symbol)
        if quote.error is not None or quote.funding_apr is None:
            self._venue_healthy = False
            return None, 0.0
        self._venue_healthy = True
        return quote.funding_apr, quote.oi_usd

    def _build_opportunities(
        self, symbol: str, venue: str, funding_apr: float, oi_usd: float,
    ) -> list[FundingOpportunity]:
        """Construct the funding-carry opportunity for one (symbol, venue).

        Returns exactly one opportunity per symbol — venue_long and
        venue_short are the same exchange (spot vs perp), with the
        spot/perp role determined by the variant:

          * funding_apr >= 0  → variant="delta_neutral"
            long-spot + short-perp; receives funding.
          * funding_apr <  0  → variant="reverse_carry"
            long-perp + short-spot; the negative funding now flows
            TO the short side, so this side receives the carry.

        Both variants flag the same depth_ok / oi_usd / spread_apr
        fields; only `variant` (and the implied leg assignment)
        differs. The reverse_carry path is OBSERVATION-ONLY until
        Phase 2 wires the corresponding open()/_place() leg map —
        FundingEngine.open() rejects non-delta_neutral variants as a
        defence-in-depth guard so a flipped FUNDING_OBSERVATION_MODE
        can't accidentally route the wrong leg pairing.
        """
        notional_target = float(settings.FUNDING_MAX_NOTIONAL_USD)
        min_required_oi = float(settings.FUNDING_MIN_OI_MULT) * notional_target
        # Per spec: "if open interest fetch_oi available, else skip the check
        # and set True". oi_usd == 0 (we didn't get OI) → depth_ok = True.
        if oi_usd <= 0.0:
            depth_ok = True
        else:
            depth_ok = oi_usd >= min_required_oi

        variant = "delta_neutral" if funding_apr >= 0 else "reverse_carry"
        return [FundingOpportunity(
            symbol=symbol,
            variant=variant,
            venue_long=venue,
            venue_short=venue,
            funding_apr=funding_apr,
            spread_apr=0.0,            # bid-ask leg handled by sim slippage
            oi_usd=oi_usd,
            depth_ok=depth_ok,
        )]

    @staticmethod
    def _filter(opps: list[FundingOpportunity]) -> list[FundingOpportunity]:
        """Drop opps whose |funding_apr| is below FUNDING_MIN_APR. The check
        is symmetric so both variants surface: positive APR → delta_neutral
        (receive funding), negative APR → reverse_carry (receive funding on
        the short leg). depth_ok is propagated through — callers can treat
        a False as a soft skip via skip_reason."""
        floor = float(settings.FUNDING_MIN_APR)
        return [o for o in opps if abs(o.funding_apr) >= floor]

    async def _place(self, pos: FundingPosition, leg: str) -> Optional[int]:
        """Route one leg through OrderRouter.

        SIM_MODE: route through OrderRouter._sim_execute, which writes a sim
        Trade row (sim_mode=True) with strategy="funding_arb". Mirrors the
        arb engine's sim shape: a flat ±FUNDING_SIM_SLIPPAGE_PCT around the
        leg mid. We model "mid" as opp.funding_apr-derived: in Phase 1 we
        don't have an entry price for each leg (CCXT fetch_funding_rate
        returns the rate, not the order book), so a synthetic mid of 1.0
        is used purely so the slippage model has something to apply — the
        sim Trade row's pnl is then driven by the funding accrual model
        in the agent's _close path, not this fill price.

        Live (SIM_MODE=False) is a stub. Phase 1 never reaches it: the
        observation-mode assert in open() refuses to proceed without
        SIM_MODE, and the agent loop bypasses open() entirely while
        FUNDING_OBSERVATION_MODE is True.
        """
        if not settings.SIM_MODE:
            logger.info(
                "[FundingEngine] _place LIVE stub %s leg=%s notional=$%.2f",
                pos.opp.symbol, leg, pos.notional_usd,
            )
            return None

        # Sim fill: synthetic mid ± slippage. Phase 1 doesn't carry per-leg
        # prices on the opportunity (the spec's funding_apr is the trade's
        # economic signal, not a fill price), so the row records notional /
        # 1.0 as a unit-price placeholder. Net P&L is computed in the
        # agent's close path from realised funding accrual.
        slip = float(settings.FUNDING_SIM_SLIPPAGE_PCT)
        side = "long" if leg == "long" else "short"
        # ±slip — long pays slip, short receives. Same shape as ArbEngine.
        fill_price = 1.0 * (1.0 + slip) if leg == "long" else 1.0 * (1.0 - slip)

        trade_data = {
            "signal_id":      None,
            "pair":           pos.opp.symbol,
            "exchange":       pos.opp.venue_long if leg == "long" else pos.opp.venue_short,
            "side":           side,
            "signal_type":    "funding_arb",
            "entry_price":    fill_price,
            "size_usd":       float(pos.notional_usd),
            "size_base":      float(pos.notional_usd) / fill_price if fill_price > 0 else 0.0,
            "stop_loss":      None,
            "take_profit":    None,
            "sim_mode":       True,
            "profile":        settings.ACTIVE_PROFILE,
            "strategy":       "funding_arb",
            "timestamp_open": datetime.utcnow(),
        }
        try:
            return await asyncio.to_thread(db_queries.save_trade, trade_data)
        except Exception as e:
            logger.warning(f"[FundingEngine] sim _place save_trade failed: {e}")
            return None

    @staticmethod
    def _basis(opp: FundingOpportunity) -> float:
        """Perp-spot price drift in % at entry.

        Phase 1 keeps this at 0.0 — fetch_funding_rate doesn't return both
        spot and perp prices in one call, and adding a second fetch per
        symbol per scan isn't worth the cost when the basis-blowout exit
        is rare on Binance majors. The hook exists so Phase 2 (cross-venue
        / cash-and-carry) can populate it without a refactor.
        """
        return 0.0

    @staticmethod
    def _basis_blowout(pos: FundingPosition) -> bool:
        """True when perp-spot drift exceeds FUNDING_BASIS_SIGMA_EXIT * sigma.

        Phase 1 returns False unconditionally — see _basis. The signature
        exists so the priority-ordered exit check in exit_reason() is
        stable across phases.
        """
        return False

    @staticmethod
    def _margin_breach(pos: FundingPosition) -> bool:
        """True when the margin ratio falls below
        FUNDING_MARGIN_ALERT_RATIO × exchange maintenance margin.

        Phase 1 returns False — observation mode never actually posts
        margin, so there's nothing to breach. Phase 2 wires the perp
        leg's live margin ratio in here.
        """
        return False

    def _venue_unhealthy(self, pos: FundingPosition) -> bool:
        """True when the funding feed has been failing.

        Drives the venue_health exit reason. Flipped to False on every
        successful _fetch_native_funding and to True on any exception.
        """
        return not self._venue_healthy


# Module-level singleton — import this rather than re-instantiating
# FundingEngine. Mirrors quality_gate / ofi_scorer / regime_detector.
funding_engine = FundingEngine()
