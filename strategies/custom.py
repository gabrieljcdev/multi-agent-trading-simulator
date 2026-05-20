"""
strategies/custom.py
Your own strategy. Edit this file freely.

To activate: set ACTIVE_STRATEGY = "custom" in config/settings.py
or switch at runtime from the terminal with [profile] → custom strategy.

All methods have working defaults so you only need to override
what you actually want to change.
"""

from .base_strategy import BaseStrategy
from signals.base import Signal


class CustomStrategy(BaseStrategy):
    name        = "custom"
    description = "User-defined strategy. Edit strategies/custom.py."

    # ── Which scanners run? ────────────────────────────────
    async def should_run_arb(self)       -> bool: return True
    async def should_run_momentum(self)  -> bool: return True
    async def should_run_reversion(self) -> bool: return True

    # ── Score adjustment ───────────────────────────────────
    def score_signal(self, signal: Signal) -> float:
        """
        Adjust the signal score here.
        Example: boost signals with RSI divergence:
            if signal.rsi and signal.rsi < 35:
                return min(100, signal.score + 10)
        """
        return signal.score

    # ── Ranking ────────────────────────────────────────────
    def rank_signals(self, signals: list[Signal]) -> list[Signal]:
        """
        Return signals in the order you want to review them.
        Default: highest score first.
        """
        return sorted(signals, key=lambda s: s.score, reverse=True)

    # ── Optional overrides ─────────────────────────────────
    def filter_signal(self, signal: Signal) -> bool:
        """
        Return False to hard-block a signal.
        Example: only trade BTC and ETH:
            return signal.pair in ("BTC/USDT", "ETH/USDT")
        """
        return True

    def get_position_size_pct(self, signal: Signal, base_pct: float) -> float:
        """
        Dynamically size positions.
        Example: go bigger on high-conviction signals:
            if signal.score >= 85:
                return base_pct * 1.5
        """
        return base_pct

    def get_stop_loss_pct(self, signal: Signal, base_sl: float) -> float:
        return base_sl

    def get_take_profit_pct(self, signal: Signal, base_tp: float) -> float:
        return base_tp
