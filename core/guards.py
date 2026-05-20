"""
core/guards.py

Three protective guards that run on every signal before it reaches Claude.

  BTC Guard         — detects BTC crash and penalises all alt signals
  Correlation Guard — prevents over-exposure to correlated positions
  News Guard        — scans recent headlines for market-moving events
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from config import settings
from signals.base import Signal

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# BTC CRASH GUARD
# ══════════════════════════════════════════════════════════════════════════════

class BTCGuard:
    """
    Monitors BTC price for sudden drops.
    When BTC drops > BTC_CRASH_PCT in BTC_CRASH_WINDOW_MINUTES,
    all alt signals take a score penalty.
    """

    def __init__(self):
        self._price_history: list[tuple[float, float]] = []  # (timestamp, price)
        self._triggered = False
        self._trigger_time: Optional[float] = None

    def update_price(self, price: float):
        now = time.time()
        self._price_history.append((now, price))
        # Keep only the window
        cutoff = now - settings.BTC_CRASH_WINDOW_MINUTES * 60
        self._price_history = [(t, p) for t, p in self._price_history if t >= cutoff]
        self._check()

    def _check(self):
        if len(self._price_history) < 2:
            self._triggered = False
            return
        oldest_price = self._price_history[0][1]
        latest_price = self._price_history[-1][1]
        if oldest_price == 0:
            return
        drop_pct = (oldest_price - latest_price) / oldest_price * 100
        was_triggered = self._triggered
        self._triggered = drop_pct >= settings.BTC_CRASH_PCT
        if self._triggered and not was_triggered:
            logger.warning(f"BTC guard triggered — drop {drop_pct:.2f}% in {settings.BTC_CRASH_WINDOW_MINUTES}m")
            self._trigger_time = time.time()

    def apply(self, signal: Signal) -> float:
        """Return score penalty to apply. 0 if guard not triggered or pair is BTC."""
        if not settings.BTC_GUARD_ENABLED:
            return 0.0
        if not self._triggered:
            return 0.0
        if "BTC" in signal.pair and signal.signal_type == "arb":
            return 0.0
        return -settings.BTC_GUARD_SCORE_PENALTY

    @property
    def is_triggered(self) -> bool:
        return self._triggered

    def status(self) -> str:
        if self._triggered:
            elapsed = (time.time() - (self._trigger_time or 0)) / 60
            return f"⚠ TRIGGERED {elapsed:.0f}m ago"
        return "OK"


# ══════════════════════════════════════════════════════════════════════════════
# POSITION CORRELATION GUARD
# ══════════════════════════════════════════════════════════════════════════════

# Static correlation table — updated from DB periodically
# Pairs with high historical price correlation
_KNOWN_HIGH_CORR = {
    frozenset(["ETH/USDT", "SOL/USDT"]):   0.88,
    frozenset(["ETH/USDT", "AVAX/USDT"]):  0.85,
    frozenset(["ETH/USDT", "BNB/USDT"]):   0.82,
    frozenset(["BTC/USDT", "ETH/USDT"]):   0.80,
    frozenset(["SOL/USDT", "AVAX/USDT"]):  0.83,
    frozenset(["BTC/USDT", "SOL/USDT"]):   0.78,
    frozenset(["MATIC/USDT", "ETH/USDT"]): 0.81,
    frozenset(["DOT/USDT", "ETH/USDT"]):   0.79,
    frozenset(["LINK/USDT", "ETH/USDT"]):  0.77,
}


class CorrelationGuard:
    """
    Checks whether a new signal would create over-correlated exposure
    with existing open positions.
    """

    def apply(self, signal: Signal, open_positions: list) -> float:
        """
        Return score penalty. 0 if no correlation issue.
        -999 if should be hard-blocked (above CORR_BLOCK_THRESHOLD).
        """
        if not settings.CORR_GUARD_ENABLED:
            return 0.0

        if settings.CORR_ARB_EXEMPT and signal.signal_type == "arb":
            return 0.0

        if not open_positions:
            return 0.0

        max_corr = 0.0
        for pos in open_positions:
            pair_set = frozenset([signal.pair, pos.pair])
            corr = _KNOWN_HIGH_CORR.get(pair_set, 0.0)
            max_corr = max(max_corr, corr)

        if max_corr >= settings.CORR_BLOCK_THRESHOLD:
            logger.info(f"Correlation guard: BLOCKING {signal.pair} (corr {max_corr:.2f})")
            return -999.0

        if max_corr >= settings.CORR_HIGH_THRESHOLD:
            logger.info(f"Correlation guard: penalising {signal.pair} (corr {max_corr:.2f})")
            return -settings.CORR_PENALTY_AMOUNT

        return 0.0

    def update_correlations(self, correlation_matrix: dict):
        """
        Update the correlation table from fresh DB data.
        Called periodically by the main loop.
        correlation_matrix: {frozenset([pair_a, pair_b]): float}
        """
        _KNOWN_HIGH_CORR.update(correlation_matrix)


# ══════════════════════════════════════════════════════════════════════════════
# NEWS GUARD
# ══════════════════════════════════════════════════════════════════════════════

class NewsGuard:
    """
    Scans recent news headlines for market-moving events.
    Block keywords hard-suppress signals.
    Warn keywords apply a score penalty.
    """

    def __init__(self):
        self._events: list[dict] = []    # {"text": str, "ts": float, "level": "block"|"warn"}
        self._last_scan: float = 0.0

    def ingest(self, headline: str, timestamp: Optional[float] = None):
        """Add a headline to the event buffer. Called by the news RSS feed."""
        ts  = timestamp or time.time()
        txt = headline.lower()

        level = None
        for kw in settings.NEWS_GUARD_BLOCK_KEYWORDS:
            if kw.lower() in txt:
                level = "block"
                break
        if level is None:
            for kw in settings.NEWS_GUARD_WARN_KEYWORDS:
                if kw.lower() in txt:
                    level = "warn"
                    break

        if level:
            self._events.append({"text": headline, "ts": ts, "level": level})
            logger.warning(f"News guard [{level.upper()}]: {headline[:80]}")

    def apply(self, signal: Signal) -> float:
        """Return score penalty based on recent events."""
        if not settings.NEWS_GUARD_ENABLED:
            return 0.0

        cutoff = time.time() - settings.NEWS_GUARD_LOOKBACK_MINUTES * 60
        recent = [e for e in self._events if e["ts"] >= cutoff]

        if not recent:
            return 0.0

        # Worst event wins
        if any(e["level"] == "block" for e in recent):
            return -settings.NEWS_GUARD_PENALTY_BLOCK

        if any(e["level"] == "warn" for e in recent):
            return -settings.NEWS_GUARD_PENALTY_WARN

        return 0.0

    def active_events(self) -> list[dict]:
        """Return events active within the lookback window."""
        cutoff = time.time() - settings.NEWS_GUARD_LOOKBACK_MINUTES * 60
        return [e for e in self._events if e["ts"] >= cutoff]

    def is_clear(self) -> bool:
        return not bool(self.active_events())

    def status(self) -> str:
        events = self.active_events()
        if not events:
            return "clear"
        blocks = [e for e in events if e["level"] == "block"]
        warns  = [e for e in events if e["level"] == "warn"]
        parts  = []
        if blocks:
            parts.append(f"⛔ {len(blocks)} block event(s)")
        if warns:
            parts.append(f"⚠ {len(warns)} warning(s)")
        return " · ".join(parts)

    def purge_old(self):
        """Remove events outside the lookback window."""
        cutoff = time.time() - settings.NEWS_GUARD_LOOKBACK_MINUTES * 60 * 2
        self._events = [e for e in self._events if e["ts"] >= cutoff]


# ══════════════════════════════════════════════════════════════════════════════
# COMBINED GUARD RUNNER
# ══════════════════════════════════════════════════════════════════════════════

class GuardRunner:
    """
    Applies all guards to a signal and returns the combined score penalty.
    A single -999 from any guard blocks the signal entirely.
    """

    def __init__(self):
        self.btc_guard   = BTCGuard()
        self.corr_guard  = CorrelationGuard()
        self.news_guard  = NewsGuard()

    def apply_all(self, signal: Signal, open_positions: list) -> tuple[float, list[str]]:
        """
        Returns (total_penalty, [reason strings]).
        If total_penalty <= -999, signal should be blocked.
        """
        penalties = []
        reasons   = []

        btc_pen  = self.btc_guard.apply(signal)
        corr_pen = self.corr_guard.apply(signal, open_positions)
        news_pen = self.news_guard.apply(signal)

        if btc_pen < 0:
            penalties.append(btc_pen)
            reasons.append(f"BTC crash guard ({btc_pen:+.0f})")
        if corr_pen < 0:
            penalties.append(corr_pen)
            reasons.append(f"Correlation guard ({corr_pen:+.0f})")
        if news_pen < 0:
            penalties.append(news_pen)
            reasons.append(f"News guard ({news_pen:+.0f})")

        total = sum(penalties)
        return total, reasons

    def status_summary(self) -> dict:
        return {
            "btc_guard":  self.btc_guard.status(),
            "news_guard": self.news_guard.status(),
        }


# Module-level singleton
guard_runner = GuardRunner()
