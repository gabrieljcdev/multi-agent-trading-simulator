"""
core/regime_detector.py

Classifies the current market regime for every pair on every timeframe.
Runs continuously — updates on each candle close.

Regimes
-------
  TRENDING    — ADX strong, Hurst persistent, BB expanding
  RANGING     — ADX weak, Hurst random/reverting, BB compressed
  HIGH_VOL    — ATR at extreme, volatility spike regardless of direction
  CHOPPY      — ADX very low, no structure, avoid all directional trades

The regime determines which strategies activate and what score
adjustments apply to every signal passing through the quality gate.
"""

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

from utils.hurst import RollingHurst
from config import settings

logger = logging.getLogger(__name__)


# ── Regime constants ───────────────────────────────────────────────────────

TRENDING  = "trending"
RANGING   = "ranging"
HIGH_VOL  = "high_vol"
CHOPPY    = "choppy"
UNKNOWN   = "unknown"


@dataclass
class RegimeSnapshot:
    """Full regime picture for one pair at one timeframe."""
    pair:       str
    timeframe:  str
    timestamp:  datetime

    regime:     str = UNKNOWN

    # Raw indicator values
    adx:           Optional[float] = None
    atr:           Optional[float] = None
    atr_percentile: Optional[float] = None  # 0–100
    bb_width:      Optional[float] = None
    bb_width_avg:  Optional[float] = None
    hurst:         Optional[float] = None
    hurst_label:   str = "unknown"

    # Derived flags
    is_trending:   bool = False
    is_ranging:    bool = False
    is_high_vol:   bool = False
    is_choppy:     bool = False

    # Strategy activation (set by detector)
    momentum_ok:   bool = False
    reversion_ok:  bool = False
    grid_ok:       bool = False
    sweep_ok:      bool = False
    arb_ok:        bool = True    # Arb is always eligible

    # Score adjustments to apply to any signal on this pair/tf
    score_modifier: float = 0.0

    def summary(self) -> str:
        return (
            f"{self.pair} {self.timeframe} → {self.regime.upper()} "
            f"ADX={self.adx:.1f if self.adx else '?'} "
            f"H={self.hurst:.2f if self.hurst else '?'} "
            f"ATRpct={self.atr_percentile:.0f if self.atr_percentile else '?'}"
        )


class RegimeDetector:
    """
    Maintains regime state for every (pair, timeframe) combination.
    Feed it candles via update() and read regime via get().
    """

    def __init__(self):
        # Key: (pair, timeframe) → RegimeSnapshot
        self._regimes: dict[tuple, RegimeSnapshot] = {}

        # Rolling Hurst per (pair, timeframe)
        self._hurst: dict[tuple, RollingHurst] = {}

        # ATR history for percentile calculation per (pair, timeframe)
        self._atr_history: dict[tuple, list[float]] = {}

        # BB width history for expansion detection
        self._bb_width_history: dict[tuple, list[float]] = {}

    def update(
        self,
        pair:      str,
        timeframe: str,
        close:     float,
        high:      float,
        low:       float,
        adx:       Optional[float],
        atr:       Optional[float],
        bb_upper:  Optional[float],
        bb_lower:  Optional[float],
        bb_mid:    Optional[float],
    ) -> RegimeSnapshot:
        """
        Process a new closed candle and return updated regime snapshot.
        Called by the market data layer on every candle close.
        """
        key = (pair, timeframe)

        # ── Hurst ─────────────────────────────────────────────────────────
        if key not in self._hurst:
            self._hurst[key] = RollingHurst(lookback=settings.HURST_LOOKBACK_BARS)
        hurst_val = self._hurst[key].update(close)
        hurst_label = self._hurst[key].classify()

        # ── ATR percentile ────────────────────────────────────────────────
        if key not in self._atr_history:
            self._atr_history[key] = []
        if atr is not None:
            self._atr_history[key].append(atr)
            if len(self._atr_history[key]) > settings.ATR_PERCENTILE_LOOKBACK:
                self._atr_history[key].pop(0)

        atr_pct = None
        if atr is not None and len(self._atr_history[key]) >= 10:
            hist = np.array(self._atr_history[key])
            atr_pct = float(np.sum(hist <= atr) / len(hist) * 100)

        # ── BB width ──────────────────────────────────────────────────────
        bb_width = None
        bb_width_avg = None
        if bb_upper is not None and bb_lower is not None and bb_mid and bb_mid > 0:
            bb_width = (bb_upper - bb_lower) / bb_mid

        if key not in self._bb_width_history:
            self._bb_width_history[key] = []
        if bb_width is not None:
            self._bb_width_history[key].append(bb_width)
            if len(self._bb_width_history[key]) > 50:
                self._bb_width_history[key].pop(0)
            if len(self._bb_width_history[key]) >= 5:
                bb_width_avg = float(np.mean(self._bb_width_history[key]))

        # ── Classify regime ───────────────────────────────────────────────
        regime = self._classify(
            adx=adx,
            atr_pct=atr_pct,
            hurst=hurst_val,
            bb_width=bb_width,
            bb_width_avg=bb_width_avg,
        )

        # ── Strategy activation ───────────────────────────────────────────
        momentum_ok  = self._momentum_ok(regime, adx, hurst_val)
        reversion_ok = self._reversion_ok(regime, adx, hurst_val)
        grid_ok      = self._grid_ok(regime, adx)
        sweep_ok     = regime in (RANGING, HIGH_VOL, TRENDING)

        # ── Score modifier ────────────────────────────────────────────────
        score_mod = self._score_modifier(regime, adx, hurst_val, atr_pct)

        snap = RegimeSnapshot(
            pair=pair,
            timeframe=timeframe,
            timestamp=datetime.utcnow(),
            regime=regime,
            adx=adx,
            atr=atr,
            atr_percentile=atr_pct,
            bb_width=bb_width,
            bb_width_avg=bb_width_avg,
            hurst=hurst_val,
            hurst_label=hurst_label,
            is_trending=regime == TRENDING,
            is_ranging=regime == RANGING,
            is_high_vol=regime == HIGH_VOL,
            is_choppy=regime == CHOPPY,
            momentum_ok=momentum_ok,
            reversion_ok=reversion_ok,
            grid_ok=grid_ok,
            sweep_ok=sweep_ok,
            arb_ok=True,
            score_modifier=score_mod,
        )

        self._regimes[key] = snap
        logger.debug(snap.summary())
        return snap

    def get(self, pair: str, timeframe: str) -> Optional[RegimeSnapshot]:
        return self._regimes.get((pair, timeframe))

    def get_primary(self, pair: str) -> Optional[RegimeSnapshot]:
        """Return regime for the slow timeframe (most reliable for classification)."""
        return self.get(pair, settings.SLOW_TIMEFRAME)

    def all_regimes(self) -> list[RegimeSnapshot]:
        return list(self._regimes.values())

    def pairs_by_regime(self, regime: str) -> list[str]:
        """Return unique pairs currently in the given regime (slow TF)."""
        return list({
            snap.pair
            for snap in self._regimes.values()
            if snap.timeframe == settings.SLOW_TIMEFRAME and snap.regime == regime
        })

    # ── Private classification logic ──────────────────────────────────────

    def _classify(
        self,
        adx:          Optional[float],
        atr_pct:      Optional[float],
        hurst:        Optional[float],
        bb_width:     Optional[float],
        bb_width_avg: Optional[float],
    ) -> str:

        # HIGH_VOL takes precedence when ATR is extreme
        if atr_pct is not None and atr_pct >= settings.ATR_HIGH_VOL_PERCENTILE:
            return HIGH_VOL

        # CHOPPY — no structure, avoid everything
        if adx is not None and adx < settings.ADX_CHOPPY_MAX:
            return CHOPPY

        # TRENDING — ADX strong + Hurst persistent
        trending_adx   = adx is not None   and adx   >= settings.ADX_TRENDING_MIN
        trending_hurst = hurst is not None and hurst >= settings.HURST_TRENDING_MIN

        if trending_adx and trending_hurst:
            return TRENDING

        # TRENDING with ADX alone (Hurst not yet calculated or borderline)
        if trending_adx and hurst is None:
            return TRENDING

        # RANGING — ADX weak + Hurst reverting/random
        ranging_adx   = adx is not None   and adx   <= settings.ADX_RANGING_MAX
        reverting_hurst = hurst is not None and hurst <= settings.HURST_TRENDING_MIN

        if ranging_adx:
            return RANGING

        # ADX in between — use Hurst to decide
        if hurst is not None:
            if hurst >= settings.HURST_TRENDING_MIN:
                return TRENDING
            if hurst <= settings.HURST_REVERTING_MAX:
                return RANGING

        return UNKNOWN

    def _momentum_ok(self, regime: str, adx: Optional[float], hurst: Optional[float]) -> bool:
        if regime == CHOPPY:
            return False
        if regime not in (TRENDING, HIGH_VOL):
            return False
        if settings.MOMENTUM_REQUIRE_HURST:
            if hurst is not None and hurst < settings.HURST_TRENDING_MIN:
                return False
        if adx is not None and adx < settings.MOMENTUM_MIN_ADX:
            return False
        return True

    def _reversion_ok(self, regime: str, adx: Optional[float], hurst: Optional[float]) -> bool:
        if regime in (CHOPPY, UNKNOWN):
            return False
        # Reversion works in ranging AND high_vol (flash reversions)
        if regime == TRENDING:
            return False   # Never fade a strong trend
        if settings.REVERSION_REQUIRE_HURST:
            if hurst is not None and hurst > settings.HURST_TRENDING_MIN:
                return False
        if adx is not None and adx > settings.REVERSION_MAX_ADX:
            return False
        return True

    def _grid_ok(self, regime: str, adx: Optional[float]) -> bool:
        if not settings.GRID_ENABLED:
            return False
        if regime in (CHOPPY, UNKNOWN):
            return False
        if adx is None:
            return False
        return settings.ADX_GRID_MIN <= adx <= settings.ADX_GRID_MAX

    def _score_modifier(
        self,
        regime:   str,
        adx:      Optional[float],
        hurst:    Optional[float],
        atr_pct:  Optional[float],
    ) -> float:
        """
        Return a score adjustment to apply to all signals on this pair/tf.
        Positive = conditions support trading. Negative = unfavourable.
        """
        mod = 0.0

        if regime == CHOPPY:
            return -30.0   # Effectively blocks most signals

        if regime == TRENDING:
            mod += 5.0
            if adx is not None and adx >= settings.ADX_STRONG_TREND:
                mod += 5.0

        if regime == HIGH_VOL:
            mod += 3.0   # Arb gaps widen, sweeps emerge — slightly positive overall

        # Hurst bonus/penalty
        if hurst is not None:
            if hurst >= 0.60:
                mod += 5.0
            elif hurst <= 0.42:
                mod += 3.0   # Good for reversion
            elif 0.47 <= hurst <= 0.53:
                mod -= 5.0   # Truly random — less edge

        return mod


# Module-level singleton
regime_detector = RegimeDetector()
