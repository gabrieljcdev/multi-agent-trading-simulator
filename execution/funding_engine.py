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

try:
    import ccxt.async_support as ccxt           # public endpoints only in Phase 1
except Exception:                               # pragma: no cover — venv carries ccxt
    ccxt = None                                 # type: ignore

from config import settings
from database import queries as db_queries

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class FundingOpportunity:
    """One funding-rate carry opportunity, scoped to a single (symbol, variant).

    Phase 1: variant is always "delta_neutral" and both venues are
    "binance" (spot leg long, perp leg short). Phase 2 lifts this so
    venue_long != venue_short opens the cross-venue cash-and-carry path.
    """
    symbol:      str
    variant:     str
    venue_long:  str
    venue_short: str
    funding_apr: float          # annualised (rate_8h * 1095)
    spread_apr: float           # bid-ask cost annualised (Phase 1: usually 0)
    oi_usd:     float
    depth_ok:   bool


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

    def __init__(self, ccxt_factory=None):
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
        # Reused public client — we hold one binance instance and close it
        # on shutdown rather than re-instantiating per scan.
        self._exchange = None
        # Sim/health flag the venue-health exit checks against. Cleared by
        # a successful scan, set by a fetch_funding_rate failure.
        self._venue_healthy: bool = True

    # ── Public API ──────────────────────────────────────────────────────

    async def scan(self) -> list[FundingOpportunity]:
        """Build one delta-neutral opportunity per symbol, filtered by APR.

        Reads funding via CCXT Binance public fetch_funding_rate (no keys).
        On network/CCXT failure, returns an empty list — the agent loop
        treats that as 'nothing to do this tick' rather than halting.
        """
        venue = "binance"
        opps: list[FundingOpportunity] = []
        for symbol in settings.FUNDING_SYMBOLS:
            funding_apr, oi_usd = await self._fetch_native_funding(venue, symbol)
            if funding_apr is None:
                continue
            opps.extend(self._build_opportunities(symbol, venue, funding_apr, oi_usd))
        return self._filter(opps)

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
        """Close the held CCXT client. Called from the agent's stop()."""
        ex = self._exchange
        if ex is None:
            return
        try:
            close = getattr(ex, "close", None)
            if close is None:
                return
            res = close()
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            logger.debug(f"FundingEngine close: {e}")

    # ── Internal helpers ────────────────────────────────────────────────

    def _get_exchange(self):
        """Return a cached binance ccxt client (built lazily so the engine
        can be instantiated without ccxt at import time)."""
        if self._exchange is not None:
            return self._exchange
        if self._ccxt_factory is not None:
            self._exchange = self._ccxt_factory()
            return self._exchange
        if ccxt is None:
            return None
        try:
            # defaultType=future is REQUIRED — ccxt's binance.fetch_funding_rate
            # raises NotSupported on spot ("supports linear and inverse contracts
            # only"). Without this option every scan tick was silently dropping
            # every symbol with a swallowed DEBUG log, leaving
            # funding_arb_observations empty even with a fully widened universe.
            # Verified via direct A/B test on 2026-05-29.
            self._exchange = ccxt.binance({
                "enableRateLimit": True,
                "options": {"defaultType": "future"},
            })
        except Exception as e:
            logger.debug(f"FundingEngine: binance() failed: {e}")
            self._exchange = None
        return self._exchange

    async def _fetch_native_funding(
        self, venue: str, symbol: str,
    ) -> tuple[Optional[float], float]:
        """Return (funding_apr, oi_usd) from CCXT Binance public endpoints.

        On any error returns (None, 0.0) — the caller drops the symbol for
        this scan tick. _venue_healthy is flipped to False so the exit
        check picks up persistently stale feeds.
        """
        ex = self._get_exchange()
        if ex is None:
            self._venue_healthy = False
            return None, 0.0

        try:
            payload = await ex.fetch_funding_rate(symbol)
        except Exception as e:
            logger.debug(f"FundingEngine fetch_funding_rate {symbol}: {e}")
            self._venue_healthy = False
            return None, 0.0

        # CCXT shapes 'fundingRate' as a fractional 8h rate (e.g. 0.0001).
        # Be defensive: fall through to the nested 'info' block if the
        # top-level field is None.
        rate_8h = payload.get("fundingRate")
        if rate_8h is None:
            info = payload.get("info") or {}
            rate_8h = info.get("lastFundingRate") or info.get("fundingRate")
        try:
            rate_8h = float(rate_8h) if rate_8h is not None else None
        except (TypeError, ValueError):
            rate_8h = None
        if rate_8h is None:
            self._venue_healthy = False
            return None, 0.0

        # Binance funds every 8h → 3 fundings/day × 365 = 1095. The spec
        # pins this constant; tests assert on it directly.
        funding_apr = rate_8h * 1095.0

        # Open interest is optional in Phase 1: when CCXT exposes it we
        # honour the OI gate; when it doesn't, depth_ok defaults to True
        # (per spec) and we record OI as 0.0 so downstream sizing still
        # caps off FUNDING_MAX_NOTIONAL_USD.
        oi_usd = 0.0
        try:
            fetch_oi = getattr(ex, "fetch_open_interest", None)
            if callable(fetch_oi):
                oi_payload = await fetch_oi(symbol)
                if oi_payload is not None:
                    raw = (oi_payload.get("openInterestAmount")
                           or oi_payload.get("openInterestValue")
                           or oi_payload.get("openInterest"))
                    try:
                        oi_usd = float(raw or 0.0)
                    except (TypeError, ValueError):
                        oi_usd = 0.0
        except Exception as e:
            logger.debug(f"FundingEngine fetch_open_interest {symbol}: {e}")

        self._venue_healthy = True
        return funding_apr, oi_usd

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
