"""
macro/monitor.py

MacroMonitor — reads data_sources singleton + calendar plugin, computes
a MacroRegime each tick, exposes a step-ladder signal modifier mirroring
the sentiment aggregator's shape.

The monitor never fetches external APIs directly. Every metric flows
through data_sources/ so caching, retries, and rate-limit handling stay
in one place. Calendar events are the one exception — they're discrete
and infrequent enough that going through data_sources would be overkill.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from config import settings
from database import queries as db_queries

from macro.regime import (
    DollarStrength, RiskAppetite, RateEnvironment, VolRegime,
    MacroScenario, MacroRegime,
)
from macro.signals import (
    CalendarEvent, EventImpact, PendingEvent, MacroSignal,
)
from macro.sources import REGISTERED_CALENDAR_SOURCES

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Per-component score curves (-100..+100)
# Read the underlying numeric value (not the 3-level enum) so VIX 18 and
# VIX 12 — both "CALM" enum-wise — land on different score bands.
# ─────────────────────────────────────────────────────────────────────────

def _vix_score(vix: Optional[float]) -> float:
    if vix is None:
        return 0.0
    if vix < settings.MACRO_VIX_CALM:           # < 15  truly calm
        return 100.0
    if vix < settings.MACRO_VIX_ELEVATED:       # 15-25 mild positive
        return 30.0
    if vix < settings.MACRO_VIX_CRISIS:         # 25-35
        return -60.0
    return -100.0                               # >= 35 crisis


def _dollar_score(dxy: Optional[float]) -> float:
    if dxy is None:
        return 0.0
    if dxy <= settings.MACRO_DXY_WEAK:
        return 100.0
    if dxy >= settings.MACRO_DXY_STRONG:
        return -100.0
    return 0.0


def _yield_curve_score(spread: Optional[float]) -> float:
    if spread is None:
        return 0.0
    if spread < settings.MACRO_YIELD_CURVE_INVERSION:
        return -100.0
    if spread > 0.5:
        return 60.0
    return 0.0


def _rate_env_score(rates: RateEnvironment) -> float:
    return {
        RateEnvironment.EASING:     100.0,
        RateEnvironment.TIGHTENING: -100.0,
    }.get(rates, 0.0)


def _inflation_score(cpi_yoy: Optional[float]) -> float:
    if cpi_yoy is None:
        return 0.0
    if cpi_yoy < settings.MACRO_INFLATION_LOW:
        return 100.0
    if cpi_yoy >= settings.MACRO_INFLATION_HIGH:
        return -100.0
    return 0.0


def _score_to_modifier(macro_score: float) -> int:
    """Step ladder mirroring sentiment._composite_to_modifier."""
    if macro_score >= 60:
        return 15
    if macro_score >= 40:
        return 10
    if macro_score >= 20:
        return 5
    if macro_score <= -60:
        return -15
    if macro_score <= -40:
        return -10
    if macro_score <= -20:
        return -5
    return 0


# ─────────────────────────────────────────────────────────────────────────
# MacroMonitor
# ─────────────────────────────────────────────────────────────────────────

class MacroMonitor:
    """Reads macro inputs from data_sources, computes a MacroRegime,
    exposes pull API for the bot loop + dashboard.

    Construct with no args to use REGISTERED_CALENDAR_SOURCES. Tests
    inject their own list to pin behaviour.
    """

    def __init__(
        self,
        calendar_sources: Optional[list] = None,
    ):
        # Calendar sources are class-instantiated like sentiment.
        if calendar_sources is None:
            calendar_sources = [cls() for cls in REGISTERED_CALENDAR_SOURCES]
        self._calendar_sources = calendar_sources

        self._regime:  Optional[MacroRegime]  = None
        self._signals: list[MacroSignal]      = []
        self._events:  list[CalendarEvent]    = []
        self._last_calendar_fetch: float      = 0.0
        self._running = False

    # ── Public API ──────────────────────────────────────────────────────

    async def refresh(self) -> MacroRegime:
        """Pull fresh macro inputs + recompute the regime.

        Idempotent and safe to call from any context. On any partial
        failure we still emit a regime — confidence decays instead.
        """
        # Pull macro inputs from the data_sources singleton. Deferred
        # import avoids macro→data_sources→… cycles at module load.
        try:
            from data_sources import data_sources as ds
        except Exception as e:
            logger.warning(f"macro: data_sources unavailable — {e}")
            ds = None

        dxy        = self._safe(ds, "frankfurter",   "get_dxy")
        vix        = self._safe(ds, "fred",          "get_vix")
        yield_10y  = self._safe(ds, "fred",          "get_10y_yield")
        yield_2y   = self._safe(ds, "fred",          "get_2y_yield")
        fed_funds  = self._safe(ds, "fred",          "get_fed_funds")
        cpi_yoy    = self._safe(ds, "fred",          "get_cpi_yoy")

        yield_curve = None
        if yield_10y is not None and yield_2y is not None:
            yield_curve = yield_10y - yield_2y

        dollar = self._classify_dollar(dxy)
        vol    = self._classify_vol(vix)
        rates  = await self._classify_rates(fed_funds)
        risk   = self._classify_risk(vol, dollar)
        scenario = self._scenario_for(dollar, risk, rates, vol, yield_curve, cpi_yoy)

        # Weighted score composition. Each component is in [-100, +100]
        # and weights sum to 1.0, so the composite is naturally bounded.
        macro_score = (
            settings.MACRO_WEIGHT_VIX         * _vix_score(vix)
            + settings.MACRO_WEIGHT_DOLLAR    * _dollar_score(dxy)
            + settings.MACRO_WEIGHT_YIELD_CURVE * _yield_curve_score(yield_curve)
            + settings.MACRO_WEIGHT_RATE_ENV  * _rate_env_score(rates)
            + settings.MACRO_WEIGHT_INFLATION * _inflation_score(cpi_yoy)
        )

        confidence = self._confidence(
            dxy=dxy, vix=vix,
            yield_10y=yield_10y, yield_2y=yield_2y,
            fed_funds=fed_funds, cpi_yoy=cpi_yoy,
        )

        regime = MacroRegime(
            scenario=scenario,
            dollar=dollar, risk=risk, rates=rates, vol=vol,
            macro_score=macro_score,
            dxy=dxy, vix=vix,
            yield_10y=yield_10y, yield_2y=yield_2y,
            yield_curve=yield_curve,
            fed_funds_rate=fed_funds,
            cpi_yoy=cpi_yoy,
            confidence=confidence,
            raw_data={
                "vix_score":          _vix_score(vix),
                "dollar_score":       _dollar_score(dxy),
                "yield_curve_score":  _yield_curve_score(yield_curve),
                "rate_env_score":     _rate_env_score(rates),
                "inflation_score":    _inflation_score(cpi_yoy),
            },
        )
        self._regime = regime
        self._signals = self._derive_signals(regime)

        # Persist — best-effort, never block the loop on a DB hiccup.
        try:
            db_queries.save_macro_regime(regime)
        except Exception as e:
            logger.debug(f"save_macro_regime failed: {e}")

        return regime

    def get_current_regime(self) -> Optional[MacroRegime]:
        """Latest cached MacroRegime, or None before the first refresh."""
        return self._regime

    def get_signal_modifier(self) -> int:
        """Step-ladder modifier to add to signal scores. Zero if no
        regime is cached yet."""
        if self._regime is None:
            return 0
        return _score_to_modifier(self._regime.macro_score)

    def is_hard_blocked(self) -> bool:
        """CRISIS scenario short-circuits all signals, same shape as
        the sentiment news guard."""
        return self._regime is not None and self._regime.is_hard_block

    def get_macro_signals(self) -> list[MacroSignal]:
        """Discrete events derived from the latest regime — for the
        future MacroAgent. Empty list before first refresh."""
        return list(self._signals)

    async def refresh_calendar(self) -> list[CalendarEvent]:
        """Pull events from every available calendar source + persist."""
        all_events: list[CalendarEvent] = []
        for source in self._calendar_sources:
            if not source.is_available():
                continue
            try:
                evs = await source.fetch_events() or []
            except Exception as e:
                logger.warning(f"calendar {source.source_id} fetch raised: {e}")
                evs = []
            all_events.extend(evs)

        try:
            db_queries.save_calendar_events(all_events)
        except Exception as e:
            logger.debug(f"save_calendar_events failed: {e}")

        self._events = all_events
        self._last_calendar_fetch = time.time()
        return all_events

    def get_pending_events(self, n: int = 3) -> list[PendingEvent]:
        """Next `n` upcoming events sorted by scheduled time.

        Reads cached events first; falls through to the DB if the
        in-memory list is empty (e.g. fresh process, calendar fetched
        in a prior run).
        """
        now = datetime.utcnow()
        cached = [e for e in self._events if e.scheduled_utc >= now]
        if not cached:
            try:
                rows = db_queries.get_pending_events(hours_ahead=72) or []
                cached = [
                    CalendarEvent(
                        event_id=r.event_id, title=r.title,
                        country=r.country or "?", scheduled_utc=r.scheduled_utc,
                        impact=EventImpact[r.impact] if r.impact else EventImpact.LOW,
                        source_id=r.source_id or "?",
                        actual=r.actual, forecast=r.forecast, previous=r.previous,
                    )
                    for r in rows
                ]
            except Exception as e:
                logger.debug(f"get_pending_events DB read failed: {e}")
                cached = []
        cached.sort(key=lambda e: e.scheduled_utc)
        return [PendingEvent.from_calendar_event(e, now) for e in cached[:n]]

    async def run_refresh_loop(self) -> None:
        """Long-running task: refresh regime every
        MACRO_REFRESH_INTERVAL_SEC; refresh calendar hourly.

        Caller is responsible for cancelling on shutdown. The bot's
        start() launches this alongside its other gather() tasks.
        """
        self._running = True
        # Calendar gets refreshed before the first regime read so the
        # pre-event pause can fire on cycle 1.
        try:
            await self.refresh_calendar()
        except Exception as e:
            logger.debug(f"initial calendar refresh failed: {e}")

        while self._running:
            try:
                await self.refresh()
            except Exception as e:
                logger.error(f"macro_monitor refresh: {e}", exc_info=True)

            now = time.time()
            if now - self._last_calendar_fetch > 3600:
                try:
                    await self.refresh_calendar()
                except Exception as e:
                    logger.debug(f"calendar refresh failed: {e}")

            await asyncio.sleep(settings.MACRO_REFRESH_INTERVAL_SEC)

    def stop(self) -> None:
        self._running = False

    # ── Dimensional classifiers ─────────────────────────────────────────

    def _classify_dollar(self, dxy: Optional[float]) -> DollarStrength:
        if dxy is None:
            return DollarStrength.NEUTRAL
        if dxy >= settings.MACRO_DXY_STRONG:
            return DollarStrength.STRONG
        if dxy <= settings.MACRO_DXY_WEAK:
            return DollarStrength.WEAK
        return DollarStrength.NEUTRAL

    def _classify_vol(self, vix: Optional[float]) -> VolRegime:
        if vix is None:
            return VolRegime.CALM
        if vix >= settings.MACRO_VIX_CRISIS:
            return VolRegime.CRISIS
        if vix >= settings.MACRO_VIX_ELEVATED:
            return VolRegime.ELEVATED
        return VolRegime.CALM

    async def _classify_rates(self, fed_funds: Optional[float]) -> RateEnvironment:
        """Tighten/ease based on fed funds change over the lookback
        window. Falls back to NEUTRAL when we don't have history yet."""
        if fed_funds is None:
            return RateEnvironment.NEUTRAL
        # Pull historical fed funds from data_log. Anchored to "approx
        # MACRO_RATE_LOOKBACK_DAYS ago" — exact match isn't required.
        try:
            ref_time = datetime.utcnow() - timedelta(
                days=settings.MACRO_RATE_LOOKBACK_DAYS,
            )
            past = db_queries.get_data_at_time(
                "fred", "fed_funds", ref_time,
            )
        except Exception as e:
            logger.debug(f"rate-env history lookup failed: {e}")
            past = None
        if past is None or past.value is None:
            return RateEnvironment.NEUTRAL
        delta = fed_funds - past.value
        if delta >= settings.MACRO_RATE_CHANGE_TIGHTENING:
            return RateEnvironment.TIGHTENING
        if delta <= settings.MACRO_RATE_CHANGE_EASING:
            return RateEnvironment.EASING
        return RateEnvironment.NEUTRAL

    def _classify_risk(
        self,
        vol: VolRegime,
        dollar: DollarStrength,
    ) -> RiskAppetite:
        if vol is VolRegime.CRISIS:
            return RiskAppetite.RISK_OFF
        if vol is VolRegime.ELEVATED or dollar is DollarStrength.STRONG:
            return RiskAppetite.RISK_OFF
        if vol is VolRegime.CALM and dollar is not DollarStrength.STRONG:
            return RiskAppetite.RISK_ON
        return RiskAppetite.NEUTRAL

    # ── Scenario priority ladder ────────────────────────────────────────

    def _scenario_for(
        self,
        dollar:  DollarStrength,
        risk:    RiskAppetite,
        rates:   RateEnvironment,
        vol:     VolRegime,
        yield_curve: Optional[float],
        cpi_yoy: Optional[float],
    ) -> MacroScenario:
        """Priority-ordered. Most severe first; first match wins."""
        if vol is VolRegime.CRISIS:
            return MacroScenario.CRISIS

        yield_inverted = (
            yield_curve is not None
            and yield_curve < settings.MACRO_YIELD_CURVE_INVERSION
        )
        if vol is VolRegime.ELEVATED and (yield_inverted or dollar is DollarStrength.STRONG):
            return MacroScenario.RISK_OFF

        high_infl = cpi_yoy is not None and cpi_yoy >= settings.MACRO_INFLATION_HIGH
        if high_infl and rates is RateEnvironment.TIGHTENING and risk is RiskAppetite.RISK_OFF:
            return MacroScenario.STAGFLATION

        if rates is RateEnvironment.TIGHTENING and dollar is not DollarStrength.WEAK \
                and vol is not VolRegime.CRISIS:
            return MacroScenario.TIGHTENING_CYCLE

        if rates is RateEnvironment.EASING and dollar is not DollarStrength.STRONG:
            return MacroScenario.EASING_CYCLE

        mid_infl = (
            cpi_yoy is not None
            and settings.MACRO_INFLATION_LOW <= cpi_yoy < settings.MACRO_INFLATION_HIGH
        )
        if risk is RiskAppetite.RISK_ON and mid_infl and vol is not VolRegime.CRISIS:
            return MacroScenario.REFLATION

        if vol is VolRegime.CALM \
                and dollar is not DollarStrength.STRONG \
                and rates is not RateEnvironment.TIGHTENING:
            return MacroScenario.GOLDILOCKS

        return MacroScenario.NEUTRAL

    # ── Helpers ─────────────────────────────────────────────────────────

    def _safe(self, ds, source_id: str, method: str) -> Optional[float]:
        """data_sources.<source_id>.<method>() with a try/except wrapper."""
        if ds is None:
            return None
        source = getattr(ds, source_id, None)
        if source is None:
            return None
        fn = getattr(source, method, None)
        if not callable(fn):
            return None
        try:
            return fn()
        except Exception as e:
            logger.debug(f"{source_id}.{method} raised: {e}")
            return None

    def _confidence(self, **fields) -> float:
        """Fraction of macro inputs present. Decayed by 0.5 once the
        latest data_sources timestamp is older than
        MACRO_CONFIDENCE_STALE_HOURS."""
        present = sum(1 for v in fields.values() if v is not None)
        total = max(1, len(fields))
        base = present / total

        # Best-effort staleness check using the data_log table — the
        # newest macro row written tells us how fresh the data is.
        try:
            latest = db_queries.get_latest_data_point("fred", "vix")
            if latest is None:
                return base
            age_h = (datetime.utcnow() - latest.timestamp).total_seconds() / 3600
            if age_h > settings.MACRO_CONFIDENCE_STALE_HOURS:
                base *= 0.5
        except Exception:
            pass
        return base

    def _derive_signals(self, regime: MacroRegime) -> list[MacroSignal]:
        """Discrete events for the future MacroAgent. Computed but not
        consumed — the agent picks these up when it lands."""
        signals: list[MacroSignal] = []

        if regime.vol is VolRegime.CRISIS:
            signals.append(MacroSignal(
                signal_id=f"vix_crisis_{int(regime.last_updated)}",
                signal_type="VIX_CRISIS",
                direction="RISK_OFF",
                strength=1.0,
                description=f"VIX {regime.vix:.1f} >= crisis threshold "
                            f"{settings.MACRO_VIX_CRISIS}",
                source_metrics={"vix": regime.vix},
                timestamp=regime.last_updated,
            ))
        elif regime.vol is VolRegime.ELEVATED:
            signals.append(MacroSignal(
                signal_id=f"vix_elevated_{int(regime.last_updated)}",
                signal_type="VIX_ELEVATED",
                direction="RISK_OFF",
                strength=0.6,
                description=f"VIX {regime.vix:.1f} in elevated band",
                source_metrics={"vix": regime.vix},
                timestamp=regime.last_updated,
            ))

        if regime.yield_curve is not None \
                and regime.yield_curve < settings.MACRO_YIELD_CURVE_INVERSION:
            signals.append(MacroSignal(
                signal_id=f"yield_inversion_{int(regime.last_updated)}",
                signal_type="YIELD_CURVE_INVERSION",
                direction="RISK_OFF",
                strength=min(1.0, abs(regime.yield_curve) / 1.0),
                description=f"10y-2y spread {regime.yield_curve:.2f}",
                source_metrics={
                    "yield_10y": regime.yield_10y,
                    "yield_2y":  regime.yield_2y,
                },
                timestamp=regime.last_updated,
            ))

        if regime.dollar is DollarStrength.STRONG:
            signals.append(MacroSignal(
                signal_id=f"dollar_strong_{int(regime.last_updated)}",
                signal_type="DOLLAR_STRENGTH",
                direction="RISK_OFF",
                strength=0.5,
                description=f"DXY {regime.dxy:.1f} >= "
                            f"{settings.MACRO_DXY_STRONG}",
                source_metrics={"dxy": regime.dxy},
                timestamp=regime.last_updated,
            ))

        return signals
