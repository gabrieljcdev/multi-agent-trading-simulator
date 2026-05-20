"""
strategies/scalper.py
Short-timeframe momentum scalping.
Focuses on 5m signals, tighter stops, faster exits.
"""

from .base_strategy import BaseStrategy
from signals.base import Signal


class ScalperStrategy(BaseStrategy):
    name        = "scalper"
    description = "5m momentum scalps. Tight SL/TP. High volume confirmation required."

    async def should_run_arb(self)       -> bool: return True   # Always welcome
    async def should_run_momentum(self)  -> bool: return True
    async def should_run_reversion(self) -> bool: return False  # Too slow for scalping

    def score_signal(self, signal: Signal) -> float:
        score = signal.score
        # Heavily reward 5m confirmation
        if signal.tf_5m:
            score += 8
        # Penalise if only 1h confirmed — signal may be too slow
        if signal.tf_1h and not signal.tf_5m:
            score -= 10
        # Reward strong volume
        if signal.volume_ratio and signal.volume_ratio >= 3.0:
            score += 5
        return min(100.0, score)

    def rank_signals(self, signals: list[Signal]) -> list[Signal]:
        return sorted(signals, key=lambda s: s.score, reverse=True)

    def filter_signal(self, signal: Signal) -> bool:
        # Scalper requires 5m confirmation
        if signal.signal_type == "momentum" and not signal.tf_5m:
            return False
        # Require strong volume for momentum scalps
        if signal.signal_type == "momentum":
            return (signal.volume_ratio or 0) >= 2.5
        return True

    def get_stop_loss_pct(self, signal: Signal, base_sl: float) -> float:
        return base_sl * 0.7   # Tighter stop: 30% smaller than profile default

    def get_take_profit_pct(self, signal: Signal, base_tp: float) -> float:
        return base_tp * 0.75  # Take profit quicker
