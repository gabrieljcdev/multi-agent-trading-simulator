"""
strategies/default.py
Balanced strategy — all three signal types active, ranked by score.
This is the recommended starting strategy.
"""

from .base_strategy import BaseStrategy
from signals.base import Signal


class DefaultStrategy(BaseStrategy):
    name        = "default"
    description = "All signal types active. Ranked by score. Arb gets slight priority."

    async def should_run_arb(self)       -> bool: return True
    async def should_run_momentum(self)  -> bool: return True
    async def should_run_reversion(self) -> bool: return True

    def score_signal(self, signal: Signal) -> float:
        # Arb signals get a small boost — lower risk, deterministic
        if signal.signal_type == "arb":
            return min(100.0, signal.score + 5)
        return signal.score

    def rank_signals(self, signals: list[Signal]) -> list[Signal]:
        # Arb first if scores are close, then by score descending
        def rank_key(s):
            type_priority = {"arb": 2, "momentum": 1, "reversion": 0}
            return (type_priority.get(s.signal_type, 0), s.score)
        return sorted(signals, key=rank_key, reverse=True)
