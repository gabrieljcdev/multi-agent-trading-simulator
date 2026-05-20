"""
execution/kill_switch.py
Emergency kill switch. One call closes everything immediately.
Bypasses all logic, queues, and approvals.
Called by [K] keypress or circuit breaker.
"""

import asyncio
import logging
from datetime import datetime

from database.queries import get_open_trades, close_trade, log_circuit_breaker

logger = logging.getLogger(__name__)


class KillSwitch:
    def __init__(self, exchange_manager=None, sim_mode: bool = True):
        self._exchange_manager = exchange_manager
        self._sim_mode = sim_mode
        self._active = False

    async def engage(self, reason: str = "manual") -> dict:
        """
        Close all open positions immediately.
        Returns summary of what was closed.
        """
        if self._active:
            logger.warning("Kill switch already engaged")
            return {}

        self._active = True
        logger.critical(f"KILL SWITCH ENGAGED — reason: {reason}")

        open_trades = get_open_trades()
        results = []

        if not open_trades:
            logger.info("Kill switch: no open positions to close")
            self._active = False
            return {"closed": 0, "trades": []}

        # Close all simultaneously
        tasks = [self._close_position(trade) for trade in open_trades]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Log to DB
        log_circuit_breaker(
            reason="kill_switch",
            detail=f"Triggered by: {reason}. Closed {len(open_trades)} positions."
        )

        summary = {
            "closed":    len(open_trades),
            "timestamp": datetime.utcnow().isoformat(),
            "reason":    reason,
            "trades":    [r for r in results if isinstance(r, dict)],
        }

        logger.critical(f"Kill switch complete. Closed {len(open_trades)} positions.")
        self._active = False
        return summary

    async def _close_position(self, trade) -> dict:
        """Close a single position — sim or live."""
        try:
            if self._sim_mode:
                # Sim: use last known price as exit
                exit_price = trade.entry_price  # TODO: use live price from market data
                pnl_pct = 0.0
            else:
                # Live: fire market sell on the exchange
                result = await self._exchange_manager.market_close(
                    exchange=trade.exchange,
                    pair=trade.pair,
                    side=trade.side,
                    size=trade.size_base,
                )
                exit_price = result.get("price", trade.entry_price)
                if trade.side == "long":
                    pnl_pct = (exit_price - trade.entry_price) / trade.entry_price
                else:
                    pnl_pct = (trade.entry_price - exit_price) / trade.entry_price

            close_trade(
                trade_id=trade.id,
                exit_price=exit_price,
                exit_reason="kill_switch",
                pnl_usd=trade.size_usd * pnl_pct,
                pnl_pct=pnl_pct,
            )

            logger.info(f"Closed {trade.pair} @ {exit_price:.4f} (kill switch)")
            return {"trade_id": trade.id, "pair": trade.pair, "exit_price": exit_price, "pnl_pct": pnl_pct}

        except Exception as e:
            logger.error(f"Failed to close trade {trade.id}: {e}")
            return {"trade_id": trade.id, "error": str(e)}
