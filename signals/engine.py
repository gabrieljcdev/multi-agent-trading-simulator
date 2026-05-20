"""
signals/engine.py
Orchestrates all signal scanners.
"""
import asyncio
import logging
from typing import Callable
from signals.base import Signal
from strategies import get_strategy
from database.queries import save_signal
from config import settings

logger = logging.getLogger(__name__)

class SignalEngine:
    def __init__(self, market_data, strategy_name: str = "default"):
        self._market_data = market_data
        self._strategy    = get_strategy(strategy_name)
        self._active_signals: list[Signal] = []
        self._on_signal_callbacks: list[Callable] = []
        self._sentiment_scores: dict = {}
        self._open_positions:   list = []
        self._scan_count = 0
        self._scanners = []

    def setup_scanners(self):
        from signals.arbitrage import ArbScanner
        from signals.momentum import MomentumScanner
        from signals.reversion import ReversionScanner
        self._arb_scanner = ArbScanner(self._market_data)
        self._mom_scanner = MomentumScanner(self._market_data)
        self._rev_scanner = ReversionScanner(self._market_data)

    def on_signal(self, fn: Callable):
        self._on_signal_callbacks.append(fn)

    def update_sentiment(self, scores: dict):
        self._sentiment_scores = scores

    def update_positions(self, positions: list):
        self._open_positions = positions

    async def run_scan(self):
        from signals.quality_gate import quality_gate
        self._scan_count += 1
        self._expire_signals()
        candidates = []

        if await self._strategy.should_run_arb():
            try:
                candidates.extend(await self._arb_scanner.scan(self._sentiment_scores))
            except Exception as e:
                logger.error(f"Arb scanner: {e}")

        if await self._strategy.should_run_momentum():
            try:
                candidates.extend(await self._mom_scanner.scan(self._sentiment_scores))
            except Exception as e:
                logger.error(f"Momentum scanner: {e}")

        if await self._strategy.should_run_reversion():
            try:
                candidates.extend(await self._rev_scanner.scan(self._sentiment_scores))
            except Exception as e:
                logger.error(f"Reversion scanner: {e}")

        if not candidates:
            return

        candidates = [s for s in candidates if self._strategy.filter_signal(s)]
        for s in candidates:
            s.raw_score = self._strategy.score_signal(s)

        passed = []
        for signal in candidates:
            ok, reasons, final_score = quality_gate.evaluate(
                signal=signal,
                open_positions=self._open_positions,
                active_signals=self._active_signals,
            )
            if ok:
                signal.raw_score = final_score
                passed.append(signal)

        if not passed:
            return

        ranked = self._strategy.rank_signals(passed)
        top    = ranked[:settings.MAX_ACTIVE_SIGNALS - len(self._active_signals)]

        for signal in top:
            try:
                db_id = save_signal(signal.to_db_dict())
                signal.db_id = db_id
            except Exception as e:
                logger.error(f"Save signal: {e}")
            self._active_signals.append(signal)
            for cb in self._on_signal_callbacks:
                try:
                    await cb(signal)
                except Exception as e:
                    logger.error(f"Signal callback: {e}")

        logger.info(f"Scan #{self._scan_count}: {len(candidates)} candidates -> {len(passed)} passed -> {len(top)} queued")

    def get_active_signals(self) -> list:
        self._expire_signals()
        return list(self._active_signals)

    def remove_signal(self, signal):
        self._active_signals = [s for s in self._active_signals if s.db_id != signal.db_id]

    def _expire_signals(self):
        self._active_signals = [s for s in self._active_signals if not s.is_expired()]

    def switch_strategy(self, name: str):
        self._strategy = get_strategy(name)
        logger.info(f"Strategy: {name}")
