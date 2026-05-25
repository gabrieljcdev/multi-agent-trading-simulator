"""
execution/router.py
Order router — handles both sim and live order execution.
"""
import logging
from datetime import datetime
from typing import Optional
from signals.base import Signal
from database.queries import save_trade, close_trade, get_open_trades
from config import settings

logger = logging.getLogger(__name__)

class OrderRouter:
    def __init__(self, exchange_manager=None):
        self._exchange_manager = exchange_manager
        self._sim_mode = settings.SIM_MODE
        # The signal agent IS the SIGNAL fund — size off its ring-fenced
        # allocation, not the whole portfolio. EXCHANGE_BALANCES is the
        # per-venue sim ledger across ALL funds (sums to STARTING_CAPITAL);
        # sizing off that sum would let signal risk beyond its fund.
        self._portfolio_value = settings.FUND_SIGNAL_CAPITAL

    async def execute(self, signal: Signal, profile) -> Optional[dict]:
        """Execute a trade from an approved signal."""
        try:
            size_usd = self._portfolio_value * profile.max_position_size_pct
            sl_pct   = profile.default_stop_loss_pct
            tp_pct   = profile.default_take_profit_pct
            entry    = signal.suggested_entry or self._get_price(signal)
            if not entry:
                logger.error(f"No price for {signal.pair}")
                return None
            if signal.direction == "long":
                sl = signal.suggested_sl or entry * (1 - sl_pct)
                tp = signal.suggested_tp or entry * (1 + tp_pct)
            elif signal.direction == "short":
                sl = signal.suggested_sl or entry * (1 + sl_pct)
                tp = signal.suggested_tp or entry * (1 - tp_pct)
            else:
                sl = entry * (1 - sl_pct)
                tp = entry * (1 + tp_pct)

            if self._sim_mode:
                trade_id = self._sim_execute(signal, entry, sl, tp, size_usd)
            else:
                trade_id = await self._live_execute(signal, entry, sl, tp, size_usd)

            if trade_id:
                logger.info(f"Trade opened: {signal.pair} {signal.direction} @ {entry:.4f} SL={sl:.4f} TP={tp:.4f} size=${size_usd:.2f}")
                return {"trade_id": trade_id, "entry": entry, "sl": sl, "tp": tp, "size_usd": size_usd}
            return None
        except Exception as e:
            logger.error(f"Order execution error: {e}")
            return None

    def _sim_execute(self, signal, entry, sl, tp, size_usd) -> int:
        trade_data = {
            "signal_id":      signal.db_id,
            "pair":           signal.pair,
            "exchange":       signal.exchange,
            "exchange_b":     signal.exchange_b,
            "side":           signal.direction,
            "signal_type":    signal.signal_type,
            "entry_price":    entry,
            "size_usd":       size_usd,
            "size_base":      size_usd / entry if entry > 0 else 0,
            "stop_loss":      sl,
            "take_profit":    tp,
            "sim_mode":       True,
            "profile":        settings.ACTIVE_PROFILE,
            "strategy":       settings.ACTIVE_STRATEGY,
            "timestamp_open": datetime.utcnow(),
        }
        return save_trade(trade_data)

    async def _live_execute(self, signal, entry, sl, tp, size_usd) -> Optional[int]:
        logger.warning("LIVE execution — real money")
        return None  # Implement when ready for live

    def _get_price(self, signal: Signal) -> Optional[float]:
        return None  # MarketData provides this via callback

    def update_portfolio_value(self, value: float):
        self._portfolio_value = value
