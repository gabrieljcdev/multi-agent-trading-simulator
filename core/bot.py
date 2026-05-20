"""
core/bot.py
Main bot loop — wires everything together.
Handles all three approval modes: per_trade, window, autonomous.
"""
import asyncio
import logging
from datetime import datetime
from database.db import init_db
from core.market_data import MarketData
from core.agent import agent
from core.guards import guard_runner
from core.regime_detector import regime_detector
from signals.engine import SignalEngine
from signals.ofi import ofi_scorer
from execution.router import OrderRouter
from execution.kill_switch import KillSwitch
from execution.position_manager import PositionManager
from config import settings

logger = logging.getLogger(__name__)

class Bot:
    def __init__(self, profile, strategy, kill_switch: KillSwitch):
        self._profile      = profile
        self._strategy     = strategy
        self._kill_switch  = kill_switch
        self._running      = False
        self._window_open  = False
        self._window_approved_until = None

        self._market_data  = MarketData()
        self._signal_engine = SignalEngine(self._market_data, strategy.name)
        self._position_mgr = PositionManager(self._market_data)
        self._router       = OrderRouter()

        # Pending signals waiting for user approval
        self._pending: list = []
        self._on_user_input = None

    async def start(self):
        self._running = True
        init_db()
        logger.info("Bot starting...")
        logger.info(f"Mode: {'SIM' if settings.SIM_MODE else 'LIVE'}")
        logger.info(f"Approval: {settings.APPROVAL_MODE}")
        logger.info(f"Profile: {self._profile.name}")

        # Register signal callback
        self._signal_engine.on_signal(self._on_new_signal)

        # Wire up market data callback
        self._market_data.on_candle_close(self._on_candle)

        # Start all tasks concurrently
        await asyncio.gather(
            self._market_data.start(),
            self._scan_loop(),
            self._position_loop(),
            self._sentiment_loop(),
            self._keyboard_loop(),
        )

    async def _on_candle(self, exchange, pair, timeframe, df):
        """Called on every candle close — trigger a scan."""
        if timeframe == settings.MID_TIMEFRAME:
            await self._signal_engine.run_scan()

    async def _scan_loop(self):
        """Fallback scanner — runs every 60s regardless of candle closes."""
        while self._running:
            await asyncio.sleep(60)
            try:
                await self._signal_engine.run_scan()
            except Exception as e:
                logger.error(f"Scan loop error: {e}")

    async def _position_loop(self):
        """Check positions every 5 seconds."""
        while self._running:
            await asyncio.sleep(5)
            try:
                await self._position_mgr.check_positions()
                # Update signal engine with current positions
                self._signal_engine.update_positions(self._position_mgr._market_data and [] or [])
            except Exception as e:
                logger.error(f"Position loop error: {e}")

    async def _sentiment_loop(self):
        """Refresh sentiment every 5 minutes."""
        while self._running:
            try:
                scores = await self._fetch_sentiment()
                self._signal_engine.update_sentiment(scores)
            except Exception as e:
                logger.error(f"Sentiment loop error: {e}")
            await asyncio.sleep(settings.SENTIMENT_UPDATE_INTERVAL_SECONDS)

    async def _fetch_sentiment(self) -> dict:
        try:
            from sentiment.fear_greed import fetch as fg_fetch
            fg = await fg_fetch()
            return {
                "MARKET": {
                    "composite": fg["score"],
                    "velocity":  fg["delta"],
                    "fear_greed": fg["score"],
                }
            }
        except Exception as e:
            logger.warning(f"Sentiment fetch error: {e}")
            return {"MARKET": {"composite": 50, "velocity": 0}}

    async def _on_new_signal(self, signal):
        """Called when a signal passes the quality gate."""
        logger.info(f"New signal: {signal.summary()}")

        # Get regime and OFI context for Claude
        regime = regime_detector.get_primary(signal.pair)
        ofi    = ofi_scorer.get_best(signal.pair)

        # Evaluate with Claude
        signal = await agent.evaluate_signal(signal, regime, ofi)

        if settings.APPROVAL_MODE == "autonomous":
            await self._auto_execute(signal)
        elif settings.APPROVAL_MODE == "window":
            if self._window_open:
                await self._auto_execute(signal)
            else:
                self._pending.append(signal)
                self._print_signal(signal)
        else:
            # per_trade — queue for user approval
            self._pending.append(signal)
            self._print_signal(signal)

    async def _auto_execute(self, signal):
        """Execute without user approval (autonomous/window mode)."""
        if self._position_mgr.is_halted:
            logger.warning("Bot halted — skipping auto execute")
            return
        rec = signal.indicators.get("claude_rec", "")
        if rec == "SKIP":
            logger.info(f"Claude recommends SKIP for {signal.pair} — not executing")
            return
        result = await self._router.execute(signal, self._profile)
        if result:
            logger.info(f"Auto-executed: {signal.pair} @ {result['entry']:.4f}")

    async def _keyboard_loop(self):
        """Handle keyboard input for approvals and controls."""
        import sys
        while self._running:
            try:
                await asyncio.sleep(0.1)
                # In a real terminal this would use aioconsole or similar
                # For now signals are auto-processed
            except Exception:
                pass

    def _print_signal(self, signal):
        """Print signal to terminal for user review."""
        print(f"\n{'='*60}")
        print(f"NEW SIGNAL: {signal.pair} {signal.signal_type.upper()} {signal.direction.upper()}")
        print(f"Score: {signal.score:.0f}/100")
        if signal.claude_reasoning:
            print(f"\nClaude: {signal.claude_reasoning[:300]}...")
        if signal.suggested_entry:
            print(f"\nEntry: {signal.suggested_entry:.4f}")
        if signal.suggested_sl:
            print(f"SL:    {signal.suggested_sl:.4f}")
        if signal.suggested_tp:
            print(f"TP:    {signal.suggested_tp:.4f}")
        rec = signal.indicators.get("claude_rec", "?")
        print(f"\nRecommendation: {rec}")
        print(f"[G]o  [S]kip  [K]ill all")
        print('='*60)

    async def stop(self):
        self._running = False
        await self._market_data.stop()
        logger.info("Bot stopped")
