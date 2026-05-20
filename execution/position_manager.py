"""
execution/position_manager.py
Monitors open positions and triggers SL/TP exits.
"""
import logging
from datetime import datetime
from database.queries import get_open_trades, close_trade, get_today_pnl_pct, get_consecutive_losses
from database.queries import log_circuit_breaker
from config import settings

logger = logging.getLogger(__name__)

class PositionManager:
    def __init__(self, market_data=None):
        self._market_data = market_data
        self._halted = False
        self._paused = False
        self._halt_reason = ""

    async def check_positions(self) -> list:
        """Check all open positions against current prices. Returns list of closed trade ids."""
        if self._halted:
            return []
        closed = []
        trades = get_open_trades()
        for trade in trades:
            price = self._get_price(trade)
            if price is None:
                continue
            exit_reason = None
            if trade.side == "long":
                if trade.stop_loss and price <= trade.stop_loss:
                    exit_reason = "sl_hit"
                elif trade.take_profit and price >= trade.take_profit:
                    exit_reason = "tp_hit"
            elif trade.side == "short":
                if trade.stop_loss and price >= trade.stop_loss:
                    exit_reason = "sl_hit"
                elif trade.take_profit and price <= trade.take_profit:
                    exit_reason = "tp_hit"
            if exit_reason:
                pnl_pct = self._calc_pnl(trade, price)
                close_trade(trade.id, price, exit_reason, trade.size_usd * pnl_pct, pnl_pct)
                closed.append(trade.id)
                logger.info(f"Position closed: {trade.pair} {exit_reason} pnl={pnl_pct*100:.2f}%")
        await self._check_circuit_breakers()
        return closed

    def _get_price(self, trade):
        if self._market_data:
            return self._market_data.get_price(trade.exchange, trade.pair)
        return None

    def _calc_pnl(self, trade, exit_price) -> float:
        if trade.entry_price == 0:
            return 0.0
        if trade.side == "long":
            return (exit_price - trade.entry_price) / trade.entry_price
        elif trade.side == "short":
            return (trade.entry_price - exit_price) / trade.entry_price
        return 0.0

    async def _check_circuit_breakers(self):
        cb = settings.CIRCUIT_BREAKERS
        # Daily loss
        if cb["daily_loss"]["enabled"]:
            daily_pnl = get_today_pnl_pct()
            if daily_pnl <= -cb["daily_loss"]["threshold_pct"] / 100:
                self._trigger(cb["daily_loss"]["action"],
                    f"Daily loss limit hit: {daily_pnl*100:.2f}%")
                return
        # Consecutive losses
        if cb["consecutive_loss"]["enabled"]:
            streak = get_consecutive_losses()
            if streak >= cb["consecutive_loss"]["count"]:
                self._trigger(cb["consecutive_loss"]["action"],
                    f"{streak} consecutive losses")

    def _trigger(self, action: str, reason: str):
        log_circuit_breaker(action, reason)
        if action == "halt":
            self._halted = True
            self._halt_reason = reason
            logger.critical(f"CIRCUIT BREAKER HALT: {reason}")
        elif action == "pause":
            self._paused = True
            logger.warning(f"CIRCUIT BREAKER PAUSE: {reason}")

    def resume(self):
        self._halted = False
        self._paused = False
        self._halt_reason = ""

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def halt_reason(self) -> str:
        return self._halt_reason
