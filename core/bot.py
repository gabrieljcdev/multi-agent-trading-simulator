"""
core/bot.py

CryptoBot — the main bot.

One async _cycle() runs every BOT_LOOP_INTERVAL_SEC. It checks the dead
zone and circuit breakers, then drives a signal scan. Signals that
pass the quality gate fire _on_signal, which evaluates with Claude and
routes through the approval gate (per_trade / window / autonomous).

Background loops run concurrently via asyncio.gather:
  _cycle_loop                — the main scan/evaluate/route cycle
  _heartbeat_loop            — refresh sentiment + equity every HEARTBEAT_INTERVAL_SEC
  _position_watcher_loop     — poll SL/TP, drive record_trade_result on close
  _self_review_loop          — every SELF_REVIEW_EVERY_N_TRADES closes, run Claude self-review
  _future_price_tracker_loop — record price_1h / price_4h / price_24h on past signals
                               (the data we use later to score quality-gate decisions)

Public API:
  start(), stop(), trigger_kill_switch(), approve_window(), record_trade_result()
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from config import settings
from core.agent import agent
from core.guards import guard_runner
from core.market_data import MarketData
from core.regime_detector import regime_detector
from database import queries as db_queries
from database.db import init_db
from execution.kill_switch import KillSwitch
from execution.position_manager import PositionManager
from execution.router import OrderRouter
from signals.engine import SignalEngine
from signals.ofi import ofi_scorer

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# Circuit breaker state — kept in-memory on the bot.
# PositionManager has its own halt flag for DB-derived checks; both can
# independently halt, but this one is the authoritative source for the
# main cycle (it's hot and we don't want to hit the DB on every tick).
# ══════════════════════════════════════════════════════════════════════════


class CircuitBreakerState:
    """Tracks daily P&L, consecutive losses, drawdown, peak equity."""

    def __init__(self, starting_equity: Optional[float] = None):
        starting = starting_equity if starting_equity is not None \
            else sum(settings.EXCHANGE_BALANCES.values())
        self.daily_pnl_pct: float = 0.0
        self.consecutive_losses: int = 0
        self.current_equity: float = starting
        self.daily_peak_equity: float = starting
        self.last_reset_date = datetime.utcnow().date()
        self.halted: bool = False
        self.halt_reason: Optional[str] = None
        self.halt_at: Optional[datetime] = None

    @property
    def drawdown_pct(self) -> float:
        if self.daily_peak_equity <= 0:
            return 0.0
        return (self.current_equity - self.daily_peak_equity) / self.daily_peak_equity * 100

    def reset_if_new_day(self):
        today = datetime.utcnow().date()
        if today > self.last_reset_date:
            self.daily_pnl_pct = 0.0
            self.daily_peak_equity = self.current_equity
            self.last_reset_date = today

    def update_after_trade(self, pnl_pct: float):
        """pnl_pct is the per-trade return as a fraction (e.g. -0.012 = -1.2%)."""
        self.daily_pnl_pct += pnl_pct * 100  # stored as percent for threshold compare
        if pnl_pct < 0:
            self.consecutive_losses += 1
        elif pnl_pct > 0:
            self.consecutive_losses = 0
        self.current_equity *= (1.0 + pnl_pct)
        if self.current_equity > self.daily_peak_equity:
            self.daily_peak_equity = self.current_equity

    def evaluate(self) -> tuple[bool, str]:
        """Return (should_halt, reason). Reason is empty when not triggered."""
        cb = settings.CIRCUIT_BREAKERS

        dl = cb.get("daily_loss", {})
        if dl.get("enabled") and self.daily_pnl_pct <= -dl["threshold_pct"]:
            return True, f"daily_loss {self.daily_pnl_pct:.2f}%"

        cl = cb.get("consecutive_loss", {})
        if cl.get("enabled") and self.consecutive_losses >= cl["count"]:
            return True, f"consecutive_loss {self.consecutive_losses}"

        dd = cb.get("drawdown", {})
        if dd.get("enabled") and self.drawdown_pct <= -dd["threshold_pct"]:
            return True, f"drawdown {self.drawdown_pct:.2f}%"

        return False, ""

    def halt(self, reason: str):
        self.halted = True
        self.halt_reason = reason
        self.halt_at = datetime.utcnow()


# ══════════════════════════════════════════════════════════════════════════
# CryptoBot
# ══════════════════════════════════════════════════════════════════════════


class CryptoBot:
    """
    Async trading bot. Build with explicit profile/strategy/kill_switch
    for testability, or let the constructor pull defaults from settings.
    """

    def __init__(
        self,
        profile=None,
        strategy=None,
        kill_switch: Optional[KillSwitch] = None,
        market_data: Optional[MarketData] = None,
        signal_engine: Optional[SignalEngine] = None,
        position_mgr: Optional[PositionManager] = None,
        router: Optional[OrderRouter] = None,
    ):
        # Resolve defaults lazily so tests can inject everything.
        if profile is None:
            from profiles.profile_manager import profile_manager
            profile = profile_manager.load(settings.ACTIVE_PROFILE)
        if strategy is None:
            from strategies import get_strategy
            strategy = get_strategy(settings.ACTIVE_STRATEGY)
        if kill_switch is None:
            kill_switch = KillSwitch(sim_mode=settings.SIM_MODE)

        self._profile = profile
        self._strategy = strategy
        self._kill_switch = kill_switch

        self._market_data = market_data if market_data is not None else MarketData()
        self._signal_engine = signal_engine if signal_engine is not None \
            else SignalEngine(self._market_data, strategy.name)
        # Engine constructs scanners lazily; ensure they exist before run_scan
        if hasattr(self._signal_engine, "setup_scanners"):
            try:
                self._signal_engine.setup_scanners()
            except Exception as e:
                logger.warning(f"signal_engine.setup_scanners failed: {e}")

        self._position_mgr = position_mgr if position_mgr is not None \
            else PositionManager(self._market_data)
        self._router = router if router is not None else OrderRouter()

        # Sentiment aggregator — deferred import keeps the constructor light
        # and avoids any circular-import risk during early boot.
        from sentiment import sentiment as sentiment_singleton
        self._sentiment = sentiment_singleton

        # Circuit breaker — authoritative for cycle decisions
        self._cb_state = CircuitBreakerState()

        # Window-mode state
        self._window_until: Optional[datetime] = None
        self._window_trades: list[datetime] = []

        # Autonomous-mode rate-limit windows
        self._auto_trades_hour: list[datetime] = []
        self._auto_trades_day: list[datetime] = []

        # Per-trade pending queue (drained by the UI keyboard loop;
        # the UI module is currently a stub, so callers can also pop from this directly)
        self._pending_signals: asyncio.Queue = asyncio.Queue()

        self._closed_trade_count: int = 0
        self._running: bool = False

        # Wire SignalEngine callback to our handler
        self._signal_engine.on_signal(self._on_signal)

    # ── Public API ──────────────────────────────────────────────────────

    async def start(self):
        """Boot exchanges + launch every background loop."""
        self._running = True
        init_db()
        logger.info("CryptoBot starting...")
        logger.info(f"  Mode:     {'SIM' if settings.SIM_MODE else 'LIVE'}")
        logger.info(f"  Profile:  {self._profile.name}")
        logger.info(f"  Strategy: {self._strategy.name}")
        logger.info(f"  Approval: {settings.APPROVAL_MODE}")

        await asyncio.gather(
            self._market_data.start(),
            self._cycle_loop(),
            self._heartbeat_loop(),
            self._position_watcher_loop(),
            self._self_review_loop(),
            self._future_price_tracker_loop(),
            return_exceptions=True,
        )

    async def stop(self):
        """Graceful shutdown — flag loops to stop and close exchanges."""
        self._running = False
        try:
            await self._market_data.stop()
        except Exception as e:
            logger.warning(f"market_data.stop: {e}")
        logger.info("CryptoBot stopped")

    async def trigger_kill_switch(self, reason: str = "manual") -> dict:
        """Close every open position immediately, then halt the bot."""
        result = await self._kill_switch.engage(reason=reason)
        self._cb_state.halt(f"kill_switch: {reason}")
        return result

    def approve_window(self, duration_minutes: int) -> datetime:
        """Open a trading window. Clamped to [WINDOW_MIN, WINDOW_MAX]."""
        duration = max(
            settings.WINDOW_MIN_DURATION_MINUTES,
            min(duration_minutes, settings.WINDOW_MAX_DURATION_MINUTES),
        )
        self._window_until = datetime.utcnow() + timedelta(minutes=duration)
        logger.info(f"Trading window approved — {duration}m until {self._window_until.isoformat()}")
        return self._window_until

    def record_trade_result(self, pnl_pct: float):
        """Update circuit-breaker state from a closed trade's return.

        pnl_pct is a fraction (-0.012 = -1.2%). Call this exactly once
        per closed trade. The position watcher does this automatically;
        external callers (tests, manual closes) can also invoke it.
        """
        self._cb_state.reset_if_new_day()
        self._cb_state.update_after_trade(pnl_pct)
        triggered, reason = self._cb_state.evaluate()
        if triggered and not self._cb_state.halted:
            self._cb_state.halt(reason)
            db_queries.log_circuit_breaker(
                reason=reason.split()[0],
                detail=(
                    f"daily_pnl={self._cb_state.daily_pnl_pct:.2f}% "
                    f"consec_losses={self._cb_state.consecutive_losses} "
                    f"drawdown={self._cb_state.drawdown_pct:.2f}% "
                    f"equity={self._cb_state.current_equity:.2f}"
                ),
            )
            logger.critical(f"Circuit breaker tripped: {reason}")

    # ── Main cycle ──────────────────────────────────────────────────────

    async def _cycle_loop(self):
        while self._running:
            try:
                await self._cycle()
            except Exception as e:
                logger.error(f"Cycle error: {e}", exc_info=True)
            await asyncio.sleep(settings.BOT_LOOP_INTERVAL_SEC)

    async def _cycle(self):
        """One pass: dead-zone gate → CB gate → sentiment session-floor → scan."""
        self._cb_state.reset_if_new_day()

        if self._in_dead_zone():
            logger.debug("Dead zone — cycle skipped")
            return

        # Re-evaluate every cycle in case external state changed (rare path,
        # mostly the trip happens in record_trade_result).
        if not self._cb_state.halted:
            triggered, reason = self._cb_state.evaluate()
            if triggered:
                self._cb_state.halt(reason)
                db_queries.log_circuit_breaker(
                    reason=reason.split()[0],
                    detail=f"Tripped at cycle entry. State: "
                           f"daily_pnl={self._cb_state.daily_pnl_pct:.2f}% "
                           f"consec_losses={self._cb_state.consecutive_losses} "
                           f"drawdown={self._cb_state.drawdown_pct:.2f}%",
                )
                logger.critical(f"Circuit breaker tripped at cycle entry: {reason}")

        if self._cb_state.halted:
            logger.debug(f"Halted ({self._cb_state.halt_reason}) — cycle skipped")
            return

        # Sentiment session floor — extreme fear, hard-block headlines, etc.
        # Reads cached aggregator state; heartbeat keeps it fresh.
        try:
            from sentiment import sentiment as sentiment_agg
            passes, reason = sentiment_agg.passes_session_floor()
            if not passes:
                logger.info(f"Session floor blocked: {reason}")
                return
        except Exception as e:
            # Sentiment is best-effort: a broken aggregator must not stop
            # the trading loop. Log and continue.
            logger.debug(f"session_floor check skipped: {e}")

        # The engine internally invokes regime, guards, OFI, sentiment via
        # the quality gate, and fires _on_signal for each passing candidate.
        await self._signal_engine.run_scan()

    # ── Signal handling ─────────────────────────────────────────────────

    async def _on_signal(self, signal):
        """Called by SignalEngine when a signal passes the quality gate."""
        try:
            regime = regime_detector.get_primary(signal.pair)
        except Exception:
            regime = None
        try:
            ofi = ofi_scorer.get_best(signal.pair)
        except Exception:
            ofi = None

        # Claude evaluation
        signal = await agent.evaluate_signal(signal, regime, ofi)

        # Snapshot the price at signal time for outcome analysis
        price_at_signal = self._price_for(signal)

        # Persist Claude's verdict + price snapshot to the signal row the
        # engine already created (engine.run_scan saved it pre-Claude).
        if getattr(signal, "db_id", None):
            try:
                db_queries.update_signal_claude(signal.db_id, {
                    "claude_reasoning":       signal.claude_reasoning,
                    "claude_suggested_entry": signal.suggested_entry,
                    "claude_suggested_sl":    signal.suggested_sl,
                    "claude_suggested_tp":    signal.suggested_tp,
                    "claude_suggested_size":  signal.suggested_size_pct,
                    "claude_risk_reward":     signal.risk_reward,
                    "claude_api_cost_usd":    signal.claude_api_cost,
                    "price_at_signal":        price_at_signal,
                })
            except Exception as e:
                logger.warning(f"update_signal_claude: {e}")

        await self._route_for_approval(signal, price_at_signal)

    async def _route_for_approval(self, signal, price_at_signal: Optional[float]):
        """Dispatch to the right path based on APPROVAL_MODE."""
        mode = settings.APPROVAL_MODE
        claude_rec = (signal.indicators or {}).get("claude_rec", "")

        if claude_rec == "SKIP":
            self._record_skip(signal, "claude_skip", price_at_signal)
            return

        if mode == "autonomous":
            if not self._auto_rate_limit_ok():
                self._record_skip(signal, "autonomous_rate_limit", price_at_signal)
                return
            await self._execute_signal(signal)

        elif mode == "window":
            if self._window_open():
                if self._window_rate_limit_ok():
                    await self._execute_signal(signal)
                else:
                    self._record_skip(signal, "window_rate_limit", price_at_signal)
            else:
                # Window is closed — wait for approve_window() before firing.
                await self._pending_signals.put(signal)
                logger.info(f"Window closed — queued {signal.pair}")

        else:
            # per_trade — caller (UI keyboard loop) drains the queue and decides.
            # TODO: ui/prompts.py is a stub; until it exists, per_trade signals
            # only land in the queue and never auto-execute.
            await self._pending_signals.put(signal)
            logger.info(f"Awaiting approval: {signal.summary()}")

    def _record_skip(self, signal, reason: str, price_at_signal: Optional[float]):
        try:
            if getattr(signal, "db_id", None):
                db_queries.update_signal_skip(signal.db_id, reason, price_at_signal)
        except Exception as e:
            logger.warning(f"update_signal_skip: {e}")
        logger.info(f"Skipped {signal.pair} — {reason}")

    async def _execute_signal(self, signal):
        if self._cb_state.halted:
            logger.warning("Halted — refusing to execute")
            return
        result = await self._router.execute(signal, self._profile)
        if not result:
            return
        now = datetime.utcnow()
        self._auto_trades_hour.append(now)
        self._auto_trades_day.append(now)
        self._window_trades.append(now)
        logger.info(f"Executed: {signal.pair} @ {result['entry']:.4f}")

    # ── Rate-limit helpers ──────────────────────────────────────────────

    def _window_open(self) -> bool:
        return self._window_until is not None and datetime.utcnow() < self._window_until

    def _window_rate_limit_ok(self) -> bool:
        cap = settings.WINDOW_MAX_TRADES_PER_HOUR
        if cap == 0:
            return True
        cutoff = datetime.utcnow() - timedelta(hours=1)
        self._window_trades = [t for t in self._window_trades if t >= cutoff]
        return len(self._window_trades) < cap

    def _auto_rate_limit_ok(self) -> bool:
        now = datetime.utcnow()
        hour_cutoff = now - timedelta(hours=1)
        day_cutoff = now - timedelta(days=1)
        self._auto_trades_hour = [t for t in self._auto_trades_hour if t >= hour_cutoff]
        self._auto_trades_day = [t for t in self._auto_trades_day if t >= day_cutoff]
        hour_cap = settings.AUTO_MAX_TRADES_PER_HOUR
        day_cap = settings.AUTO_MAX_TRADES_PER_DAY
        if hour_cap and len(self._auto_trades_hour) >= hour_cap:
            return False
        if day_cap and len(self._auto_trades_day) >= day_cap:
            return False
        return True

    # ── Background loops ────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        while self._running:
            await asyncio.sleep(settings.HEARTBEAT_INTERVAL_SEC)
            try:
                scores = await self._fetch_sentiment()
                self._signal_engine.update_sentiment(scores)

                # Feed the latest BTC price into BTCGuard's rolling history,
                # then forward its 30-minute % change to the sentiment
                # aggregator so its dump guard has live data.
                btc_price = self._price_for_pair("BTC/USDT")
                if btc_price is not None:
                    guard_runner.btc_guard.update_price(btc_price)
                    pct = guard_runner.btc_guard.change_pct_30m()
                    if pct is not None:
                        self._sentiment.set_btc_change_30m(pct)

                # TODO: recompute equity from market_data + open positions
                #       and feed self._cb_state.current_equity so drawdown stays accurate.
            except Exception as e:
                logger.warning(f"Heartbeat: {e}")

    async def _position_watcher_loop(self):
        while self._running:
            await asyncio.sleep(settings.POSITION_WATCHER_INTERVAL_SEC)
            try:
                closed_ids = await self._position_mgr.check_positions()
                if not closed_ids:
                    continue
                # The position manager has already written exit fields;
                # fetch the rows so we can drive our circuit-breaker state.
                for trade_id in closed_ids:
                    trade = self._get_trade_by_id(trade_id)
                    if trade is None or trade.pnl_pct is None:
                        continue
                    self.record_trade_result(trade.pnl_pct)
                    self._closed_trade_count += 1
            except Exception as e:
                logger.error(f"Position watcher: {e}", exc_info=True)

    async def _self_review_loop(self):
        last_reviewed_at = 0
        while self._running:
            # Check roughly once per minute — actual trigger is event-based on count.
            await asyncio.sleep(60)
            try:
                if not settings.SELF_REVIEW_ENABLED:
                    continue
                delta = self._closed_trade_count - last_reviewed_at
                if delta < settings.SELF_REVIEW_EVERY_N_TRADES:
                    continue
                last_reviewed_at = self._closed_trade_count
                trades = db_queries.get_recent_closed_trades(
                    limit=settings.SELF_REVIEW_EVERY_N_TRADES
                )
                for trade in trades:
                    review = await agent.self_review(trade)
                    if review:
                        db_queries.save_postmortem(trade.id, review)
            except Exception as e:
                logger.warning(f"Self review: {e}")

    async def _future_price_tracker_loop(self):
        """Fill in price_1h / price_4h / price_24h on past signals.

        Loops every FUTURE_PRICE_TRACKER_INTERVAL_SEC. For each signal in
        the last 24h that still has a null slot, if the signal is now ~1h /
        ~4h / ~24h old, write the current price into the matching column.
        """
        while self._running:
            await asyncio.sleep(settings.FUTURE_PRICE_TRACKER_INTERVAL_SEC)
            try:
                rows = db_queries.get_signals_needing_price_update(hours=24)
                for row in rows:
                    age_min = (datetime.utcnow() - row.timestamp).total_seconds() / 60
                    fields: dict = {}
                    price = self._price_for_pair(row.pair)
                    if price is None:
                        continue
                    # Loose windows: tracker polls hourly so any signal that
                    # crosses each landmark since the last poll catches a price.
                    if row.price_1h is None and age_min >= 60:
                        fields["price_1h"] = price
                    if row.price_4h is None and age_min >= 240:
                        fields["price_4h"] = price
                    if row.price_24h is None and age_min >= 1440:
                        fields["price_24h"] = price
                    if fields:
                        db_queries.update_signal_future_prices(row.id, fields)
            except Exception as e:
                logger.warning(f"Future price tracker: {e}")

    # ── Helpers ─────────────────────────────────────────────────────────

    def _in_dead_zone(self) -> bool:
        dead = settings.SESSION_WINDOWS.get("dead_zone")
        if not dead:
            return False
        now_hhmm = datetime.utcnow().strftime("%H:%M")
        return dead["start"] <= now_hhmm < dead["end"]

    def _price_for(self, signal) -> Optional[float]:
        """Best-effort price snapshot for a signal at decision time."""
        if signal.suggested_entry:
            return float(signal.suggested_entry)
        return self._price_for_pair(signal.pair, signal.exchange)

    def _price_for_pair(self, pair: str, exchange: Optional[str] = None) -> Optional[float]:
        try:
            if exchange:
                p = self._market_data.get_price(exchange, pair)
                if p is not None:
                    return float(p)
            prices = self._market_data.get_all_prices(pair)
            if prices:
                return float(next(iter(prices.values())))
        except Exception:
            pass
        return None

    def _get_trade_by_id(self, trade_id: int):
        """Look up a single Trade row. Avoids leaking SQLAlchemy session here."""
        # TODO: queries.get_open_trades returns a session-bound list; for closed
        # trades we currently scan today's trades. A dedicated get_trade(id)
        # query in database/queries.py would be cleaner — keeping it here for
        # now so the bot does not bypass the queries layer.
        try:
            todays = db_queries.get_today_trades()
            for t in todays:
                if t.id == trade_id:
                    return t
        except Exception as e:
            logger.warning(f"get_trade_by_id: {e}")
        return None

    async def _fetch_sentiment(self) -> dict:
        """Pull market sentiment via the pluggable aggregator.

        The aggregator works on a -100..+100 composite. The scanners
        expect 0..100 (50 = neutral). Translate before returning.
        """
        try:
            from sentiment import sentiment as sentiment_agg
            data = await sentiment_agg.get_current()
            composite_0_100 = 50.0 + (data.composite_score / 2.0)
            return {
                "MARKET": {
                    "composite":  composite_0_100,
                    "velocity":   data.sentiment_modifier,
                    "fear_greed": data.fear_greed_value if data.fear_greed_value is not None else 50,
                    "hard_block": data.hard_block,
                }
            }
        except Exception as e:
            logger.warning(f"sentiment refresh failed: {e}")
            return {"MARKET": {"composite": 50, "velocity": 0}}
