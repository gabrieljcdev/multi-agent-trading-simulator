"""
strategies/arb_only.py
Arbitrage-only strategy. Nothing fires unless it's a cross-exchange gap.
Lowest risk profile — no directional exposure.
"""

from .base_strategy import BaseStrategy
from signals.base import Signal


class ArbOnlyStrategy(BaseStrategy):
    name        = "arb_only"
    description = "Cross-exchange arbitrage only. No directional trades."

    async def should_run_arb(self)       -> bool: return True
    async def should_run_momentum(self)  -> bool: return False
    async def should_run_reversion(self) -> bool: return False

    def score_signal(self, signal: Signal) -> float:
        return signal.score

    def rank_signals(self, signals: list[Signal]) -> list[Signal]:
        # Rank by gap_pct first, then score
        return sorted(
            signals,
            key=lambda s: (s.arb_gap_pct or 0, s.score),
            reverse=True
        )

    def get_position_size_pct(self, signal: Signal, base_pct: float) -> float:
        # Arb is lower risk — allow slightly larger position
        return min(base_pct * 1.5, 0.05)
