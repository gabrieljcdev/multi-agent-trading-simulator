"""
signals/quality_gate.py

The quality gate is the last filter before a signal reaches Claude.
It enforces score threshold, regime fit, session timing, multi-TF
confirmation, guard penalties, and deduplication.

Only signals that pass every check get queued for Claude's evaluation.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from signals.base import Signal
from core.regime_detector import regime_detector, CHOPPY
from core.guards import guard_runner
from config import settings

logger = logging.getLogger(__name__)


def _session_modifier() -> float:
    """Return score modifier based on current UTC hour."""
    if not settings.SESSION_SCORING_ENABLED:
        return 0.0

    now_utc = datetime.utcnow().strftime("%H:%M")

    for window_name, window in settings.SESSION_WINDOWS.items():
        start = window["start"]
        end   = window["end"]
        boost = window["score_boost"]

        # Handle windows that don't cross midnight
        if start <= now_utc < end:
            return float(boost)

    return 0.0


class QualityGate:
    """
    Evaluates a signal against all quality criteria.
    Returns (passed: bool, reasons: list[str], adjusted_score: float).
    """

    def __init__(self):
        self._recent_passed: list[Signal] = []  # Deduplication buffer

    def evaluate(
        self,
        signal: Signal,
        open_positions: list,
        active_signals: list[Signal],
    ) -> tuple[bool, list[str], float]:
        """
        Run all gate checks.

        Returns
        -------
        passed       : bool   — True if signal should proceed to Claude
        fail_reasons : list   — Why it failed (empty if passed)
        final_score  : float  — Score after all modifiers applied
        """
        fail_reasons = []
        score = signal.raw_score

        # ── 1. Regime fit ──────────────────────────────────────────────────
        regime_snap = regime_detector.get_primary(signal.pair)
        if regime_snap:
            # Block if choppy
            if regime_snap.is_choppy:
                return False, ["regime is choppy — all signals blocked"], score

            # Check strategy-regime fit
            if signal.signal_type == "momentum" and not regime_snap.momentum_ok:
                return False, [f"momentum blocked in {regime_snap.regime} regime"], score

            if signal.signal_type == "reversion" and not regime_snap.reversion_ok:
                return False, [f"reversion blocked in {regime_snap.regime} regime"], score

            # Apply regime score modifier
            score += regime_snap.score_modifier

        # ── 2. Session timing ──────────────────────────────────────────────
        session_mod = _session_modifier()
        score += session_mod
        if session_mod < -10:
            fail_reasons.append(f"dead zone session (−{abs(session_mod):.0f})")

        # ── 3. Guards (BTC, correlation, news) ───────────────────────────
        guard_penalty, guard_reasons = guard_runner.apply_all(signal, open_positions)
        score += guard_penalty
        if guard_penalty <= -999:
            return False, guard_reasons, score
        if guard_reasons:
            fail_reasons.extend(guard_reasons)

        # ── 4. OFI confirmation ────────────────────────────────────────────
        from signals.ofi import ofi_scorer
        ofi = ofi_scorer.get_best(signal.pair)
        if ofi:
            ofi_mod = ofi.signal_modifier(signal.direction)
            score += ofi_mod
            signal.indicators["ofi_ema"] = ofi.ema_ofi
            signal.indicators["ofi_mod"] = ofi_mod

        # ── 5. Sentiment composite + hard block ──────────────────────────
        # Read from the aggregator singleton — the modifier is computed
        # once per refresh and shared. signal.sentiment_mod stays on the
        # Signal dataclass so it's logged to the DB, but the gate's
        # decision uses the live aggregator value. Hard-block from a
        # news-guard event short-circuits the signal entirely.
        try:
            from sentiment import sentiment as sentiment_aggregator

            blocked, reason = sentiment_aggregator.is_hard_blocked()
            if blocked:
                return False, [
                    f"{settings.SENTIMENT_HARD_BLOCK_SKIP_REASON}: {reason}"
                ], score

            sentiment_mod = sentiment_aggregator.get_signal_modifier()
            score += sentiment_mod
            # Mirror to the Signal field so historical analysis (the
            # signals row) reflects what the gate actually used.
            signal.sentiment_mod = float(sentiment_mod)
        except Exception as e:
            # Aggregator not constructed yet or broken — fall through to
            # whatever the scanner pre-populated on signal.sentiment_mod.
            logger.debug(f"sentiment aggregator skipped: {e}")
            score += signal.sentiment_mod

        # ── 5b. Macro modifier (macro/) ──────────────────────────────────
        # Step-ladder modifier from the macro regime (mirrors the
        # sentiment composite_to_modifier shape). MacroMonitor reads
        # data_sources internally so the gate doesn't need to know
        # which source provides which metric. Missing macro module or
        # uninitialised cache → modifier 0, never blocks a signal.
        try:
            from macro import macro_monitor

            macro_mod = macro_monitor.get_signal_modifier()
            score += macro_mod
            if macro_mod != 0:
                signal.indicators["macro_modifier"] = macro_mod

            regime = macro_monitor.get_current_regime()
            if regime is not None:
                signal.indicators["macro_scenario"] = regime.scenario.name
        except Exception as e:
            logger.debug(f"macro modifier skipped: {e}")

        # ── 6. Multi-timeframe confirmation ───────────────────────────────
        if settings.REQUIRE_MULTI_TF_CONFIRM:
            tf_count = signal.timeframe_confirmations
            if tf_count < settings.MIN_TF_CONFIRMATIONS:
                return False, [
                    f"only {tf_count}/{settings.MIN_TF_CONFIRMATIONS} timeframes confirmed"
                ], score

        # ── 7. Score threshold ─────────────────────────────────────────────
        if score < settings.SIGNAL_SCORE_THRESHOLD:
            return False, [
                f"score {score:.1f} below threshold {settings.SIGNAL_SCORE_THRESHOLD}"
            ], score

        # ── 8. Max active signals ──────────────────────────────────────────
        non_expired = [s for s in active_signals if not s.is_expired()]
        if len(non_expired) >= settings.MAX_ACTIVE_SIGNALS:
            return False, [
                f"max active signals ({settings.MAX_ACTIVE_SIGNALS}) reached"
            ], score

        # ── 9. Deduplication ──────────────────────────────────────────────
        if self._is_duplicate(signal):
            return False, ["duplicate signal — same pair/type seen recently"], score

        # ── 10. Min R/R (if SL/TP already estimated) ──────────────────────
        if signal.suggested_sl and signal.suggested_tp and signal.suggested_entry:
            entry = signal.suggested_entry
            if signal.direction == "long":
                reward = (signal.suggested_tp - entry) / entry
                risk   = (entry - signal.suggested_sl) / entry
            else:
                reward = (entry - signal.suggested_tp) / entry
                risk   = (signal.suggested_sl - entry) / entry

            if risk > 0:
                rr = reward / risk
                signal.risk_reward = rr
                if rr < settings.MIN_RISK_REWARD_RATIO:
                    return False, [
                        f"R/R {rr:.2f} below minimum {settings.MIN_RISK_REWARD_RATIO}"
                    ], score

        # ── Passed ─────────────────────────────────────────────────────────
        signal.raw_score = score   # Update with all modifiers applied
        self._recent_passed.append(signal)
        self._prune_dedup_buffer()

        logger.info(f"Signal PASSED gate: {signal.summary()} final_score={score:.1f}")
        return True, [], score

    def _is_duplicate(self, signal: Signal) -> bool:
        """Block if same pair + signal_type seen in last expiry window."""
        cutoff = datetime.utcnow() - timedelta(minutes=settings.SIGNAL_EXPIRY_MINUTES)
        for recent in self._recent_passed:
            if (recent.pair == signal.pair
                    and recent.signal_type == signal.signal_type
                    and recent.created_at >= cutoff):
                return True
        return False

    def _prune_dedup_buffer(self):
        cutoff = datetime.utcnow() - timedelta(minutes=settings.SIGNAL_EXPIRY_MINUTES * 2)
        self._recent_passed = [s for s in self._recent_passed if s.created_at >= cutoff]


# Module-level singleton
quality_gate = QualityGate()
