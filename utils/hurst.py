"""
utils/hurst.py

Rolling Hurst exponent via Rescaled Range (R/S) analysis.

H > 0.55  → trending   (persistent, momentum favoured)
H < 0.48  → reverting  (anti-persistent, mean reversion favoured)
H ≈ 0.50  → random walk (no edge, avoid directional trades)

Academic basis: Lo (1991) R/S analysis. Widely validated on crypto
time series showing regime-dependent Hurst behaviour.
"""

import numpy as np
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def hurst_rs(series: np.ndarray) -> Optional[float]:
    """
    Compute Hurst exponent via R/S analysis on a price series.

    Parameters
    ----------
    series : np.ndarray
        1-D array of closing prices (or log returns). Minimum 50 values.

    Returns
    -------
    float | None
        Hurst exponent in [0, 1], or None if calculation fails.
    """
    if len(series) < 50:
        return None

    try:
        # Work on log returns for stationarity
        log_returns = np.diff(np.log(series + 1e-12))
        n = len(log_returns)

        # Build list of sub-period sizes (powers of 2 up to n/2)
        lags = []
        rs_values = []

        min_lag = 10
        max_lag = n // 2

        lag = min_lag
        while lag <= max_lag:
            rs = _compute_rs(log_returns, lag)
            if rs is not None and rs > 0:
                lags.append(lag)
                rs_values.append(rs)
            lag = int(lag * 1.5) + 1

        if len(lags) < 4:
            return None

        # Hurst = slope of log(R/S) vs log(n) regression
        log_lags = np.log(lags)
        log_rs   = np.log(rs_values)
        hurst, _ = np.polyfit(log_lags, log_rs, 1)

        # Clamp to valid range
        return float(np.clip(hurst, 0.0, 1.0))

    except Exception as e:
        logger.debug(f"Hurst calculation failed: {e}")
        return None


def _compute_rs(returns: np.ndarray, lag: int) -> Optional[float]:
    """Compute mean R/S for a given sub-period length."""
    n = len(returns)
    num_chunks = n // lag
    if num_chunks < 1:
        return None

    rs_list = []
    for i in range(num_chunks):
        chunk = returns[i * lag:(i + 1) * lag]
        if len(chunk) < 2:
            continue
        mean = np.mean(chunk)
        deviation = np.cumsum(chunk - mean)
        r = np.max(deviation) - np.min(deviation)  # Range
        s = np.std(chunk, ddof=1)                  # Std deviation
        if s > 0:
            rs_list.append(r / s)

    return float(np.mean(rs_list)) if rs_list else None


class RollingHurst:
    """
    Maintains a rolling Hurst exponent for a single price series.
    Call update() on each new candle close.
    """

    def __init__(self, lookback: int = 200):
        self.lookback = lookback
        self._prices: list[float] = []
        self._current: Optional[float] = None

    def update(self, price: float) -> Optional[float]:
        self._prices.append(price)
        if len(self._prices) > self.lookback:
            self._prices.pop(0)

        if len(self._prices) >= 50:
            self._current = hurst_rs(np.array(self._prices))

        return self._current

    @property
    def value(self) -> Optional[float]:
        return self._current

    def classify(self) -> str:
        """Return human-readable regime label for this Hurst value."""
        if self._current is None:
            return "unknown"
        from config.settings import HURST_TRENDING_MIN, HURST_REVERTING_MAX
        if self._current > HURST_TRENDING_MIN:
            return "trending"
        if self._current < HURST_REVERTING_MAX:
            return "reverting"
        return "random"
