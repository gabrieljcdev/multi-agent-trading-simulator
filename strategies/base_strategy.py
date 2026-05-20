"""
strategies/base_strategy.py
Abstract base class all strategies must implement.
Plug in any strategy by inheriting this and registering it.
"""

from abc import ABC, abstractmethod
from typing import Optional
from signals.base import Signal


class BaseStrategy(ABC):
    """
    A strategy controls WHICH signals the engine produces and how they
    are prioritised. It does not change the underlying indicators —
    it controls the filters, weights, and ranking logic.
    """

    name:        str = "base"
    description: str = "Base strategy — do not use directly"

    @abstractmethod
    async def should_run_arb(self) -> bool:
        """Should the arbitrage scanner run for this strategy?"""
        ...

    @abstractmethod
    async def should_run_momentum(self) -> bool:
        """Should the momentum scanner run?"""
        ...

    @abstractmethod
    async def should_run_reversion(self) -> bool:
        """Should the mean reversion scanner run?"""
        ...

    @abstractmethod
    def score_signal(self, signal: Signal) -> float:
        """
        Apply strategy-specific score adjustments.
        Return the adjusted score (0–100).
        Useful for e.g. boosting arb scores in arb_only mode.
        """
        ...

    @abstractmethod
    def rank_signals(self, signals: list[Signal]) -> list[Signal]:
        """
        Given a list of passing signals, return them in priority order.
        The top 3 will be shown to Claude and the user.
        """
        ...

    def filter_signal(self, signal: Signal) -> bool:
        """
        Optional: return False to hard-block a signal before scoring.
        Default: accept everything that passed the quality gate.
        """
        return True

    def get_position_size_pct(self, signal: Signal, base_pct: float) -> float:
        """
        Override to dynamically adjust position size per signal.
        Default: return the profile's base position size unchanged.
        """
        return base_pct

    def get_stop_loss_pct(self, signal: Signal, base_sl: float) -> float:
        """Override to dynamically set stop loss per signal."""
        return base_sl

    def get_take_profit_pct(self, signal: Signal, base_tp: float) -> float:
        """Override to dynamically set take profit per signal."""
        return base_tp
