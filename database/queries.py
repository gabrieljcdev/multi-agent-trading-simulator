"""
database/queries.py
Reusable query functions. Everything that touches the DB goes through here.
"""

from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import func, desc

from .db import get_session
from .models import Candle, Signal, Trade, SentimentSnapshot, DailyStats, CircuitBreakerLog


# ── Candles ────────────────────────────────────────────────

def get_recent_candles(exchange: str, pair: str, timeframe: str, limit: int = 200):
    with get_session() as s:
        return (
            s.query(Candle)
            .filter_by(exchange=exchange, pair=pair, timeframe=timeframe)
            .order_by(desc(Candle.timestamp))
            .limit(limit)
            .all()
        )

def save_candle(candle_data: dict):
    with get_session() as s:
        candle = Candle(**candle_data)
        s.merge(candle)


# ── Signals ────────────────────────────────────────────────

def save_signal(signal_data: dict) -> int:
    with get_session() as s:
        signal = Signal(**signal_data)
        s.add(signal)
        s.flush()
        return signal.id

def update_signal_decision(signal_id: int, action: str):
    with get_session() as s:
        signal = s.get(Signal, signal_id)
        if signal:
            signal.user_action = action
            signal.user_action_at = datetime.utcnow()

def update_signal_outcome(signal_id: int, outcome: str, pnl_pct: float):
    with get_session() as s:
        signal = s.get(Signal, signal_id)
        if signal:
            signal.outcome = outcome
            signal.outcome_pnl_pct = pnl_pct

def update_signal_claude(signal_id: int, fields: dict):
    """Update Claude evaluation fields on an existing Signal row.

    `fields` may contain: claude_reasoning, claude_suggested_entry,
    claude_suggested_sl, claude_suggested_tp, claude_suggested_size,
    claude_risk_reward, claude_api_cost_usd, price_at_signal.
    Unknown keys are ignored to keep callers loosely coupled to the model.
    """
    allowed = {
        "claude_reasoning",
        "claude_suggested_entry",
        "claude_suggested_sl",
        "claude_suggested_tp",
        "claude_suggested_size",
        "claude_risk_reward",
        "claude_api_cost_usd",
        "price_at_signal",
    }
    with get_session() as s:
        signal = s.get(Signal, signal_id)
        if not signal:
            return
        for k, v in fields.items():
            if k in allowed and v is not None:
                setattr(signal, k, v)


def update_signal_skip(signal_id: int, reason: str, price_at_signal: Optional[float] = None):
    """Mark a signal as skipped with a free-text reason and price snapshot."""
    with get_session() as s:
        signal = s.get(Signal, signal_id)
        if not signal:
            return
        signal.user_action = "skip"
        signal.user_action_at = datetime.utcnow()
        signal.skip_reason = reason
        if price_at_signal is not None:
            signal.price_at_signal = price_at_signal


def update_signal_future_prices(signal_id: int, fields: dict):
    """Set any of price_1h / price_4h / price_24h on a Signal row."""
    allowed = {"price_1h", "price_4h", "price_24h"}
    with get_session() as s:
        signal = s.get(Signal, signal_id)
        if not signal:
            return
        for k, v in fields.items():
            if k in allowed and v is not None:
                setattr(signal, k, v)


def get_signals_needing_price_update(hours: int = 24) -> list:
    """Signals from the last N hours that still have any null future-price slot."""
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as s:
        return (
            s.query(Signal)
            .filter(
                Signal.timestamp >= since,
                # At least one slot still null
                (Signal.price_1h.is_(None))
                | (Signal.price_4h.is_(None))
                | (Signal.price_24h.is_(None)),
            )
            .all()
        )


def get_signal_history(days: int = 30, signal_type: str = None):
    with get_session() as s:
        q = s.query(Signal).filter(
            Signal.timestamp >= datetime.utcnow() - timedelta(days=days),
            Signal.user_action == "go"
        )
        if signal_type:
            q = q.filter(Signal.signal_type == signal_type)
        return q.order_by(desc(Signal.timestamp)).all()


# ── Trades ─────────────────────────────────────────────────

def save_trade(trade_data: dict) -> int:
    with get_session() as s:
        trade = Trade(**trade_data)
        s.add(trade)
        s.flush()
        return trade.id

def close_trade(trade_id: int, exit_price: float, exit_reason: str,
                pnl_usd: float, pnl_pct: float):
    with get_session() as s:
        trade = s.get(Trade, trade_id)
        if trade:
            trade.exit_price      = exit_price
            trade.exit_reason     = exit_reason
            trade.pnl_usd         = pnl_usd
            trade.pnl_pct         = pnl_pct
            trade.timestamp_close = datetime.utcnow()
            if trade.timestamp_open:
                delta = datetime.utcnow() - trade.timestamp_open
                trade.hold_minutes = delta.total_seconds() / 60

def get_open_trades():
    with get_session() as s:
        return (
            s.query(Trade)
            .filter(Trade.timestamp_close.is_(None))
            .all()
        )

def get_today_trades():
    today = datetime.utcnow().date()
    with get_session() as s:
        return (
            s.query(Trade)
            .filter(func.date(Trade.timestamp_open) == today)
            .order_by(desc(Trade.timestamp_open))
            .all()
        )

def get_today_pnl_pct() -> float:
    trades = get_today_trades()
    if not trades:
        return 0.0
    closed = [t for t in trades if t.pnl_pct is not None]
    return sum(t.pnl_pct for t in closed)

def get_recent_closed_trades(limit: int = 5) -> list:
    """Most recent N closed trades, newest first. Used by self-review loop."""
    with get_session() as s:
        return (
            s.query(Trade)
            .filter(Trade.timestamp_close.isnot(None))
            .order_by(desc(Trade.timestamp_close))
            .limit(limit)
            .all()
        )


def save_postmortem(trade_id: int, text: str):
    """Persist Claude's self-review for a closed trade."""
    with get_session() as s:
        trade = s.get(Trade, trade_id)
        if trade:
            trade.claude_postmortem = text


def get_consecutive_losses() -> int:
    with get_session() as s:
        recent = (
            s.query(Trade)
            .filter(Trade.timestamp_close.isnot(None))
            .order_by(desc(Trade.timestamp_close))
            .limit(10)
            .all()
        )
    count = 0
    for t in recent:
        if t.pnl_pct is not None and t.pnl_pct < 0:
            count += 1
        else:
            break
    return count


# ── Sentiment ──────────────────────────────────────────────

def save_sentiment(data: dict):
    with get_session() as s:
        snap = SentimentSnapshot(**data)
        s.add(snap)

def get_latest_sentiment(coin: str = "MARKET") -> Optional[SentimentSnapshot]:
    with get_session() as s:
        return (
            s.query(SentimentSnapshot)
            .filter_by(coin=coin)
            .order_by(desc(SentimentSnapshot.timestamp))
            .first()
        )

def get_sentiment_history(coin: str, hours: int = 24):
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as s:
        return (
            s.query(SentimentSnapshot)
            .filter(SentimentSnapshot.coin == coin,
                    SentimentSnapshot.timestamp >= since)
            .order_by(SentimentSnapshot.timestamp)
            .all()
        )


# ── Daily Stats ────────────────────────────────────────────

def upsert_daily_stats(date_str: str, data: dict):
    with get_session() as s:
        existing = s.query(DailyStats).filter_by(date=date_str).first()
        if existing:
            for k, v in data.items():
                setattr(existing, k, v)
        else:
            s.add(DailyStats(date=date_str, **data))


# ── Circuit Breakers ───────────────────────────────────────

def log_circuit_breaker(reason: str, detail: str, auto_resume_at: datetime = None):
    with get_session() as s:
        s.add(CircuitBreakerLog(
            reason=reason,
            detail=detail,
            auto_resume_at=auto_resume_at
        ))


# ── Analytics helpers (used by predictive engine) ──────────

def get_signal_win_rate(signal_type: str = None, days: int = 30) -> dict:
    """Return win rate stats for model training and UI display."""
    with get_session() as s:
        q = s.query(Trade).filter(
            Trade.timestamp_open >= datetime.utcnow() - timedelta(days=days),
            Trade.pnl_pct.isnot(None)
        )
        if signal_type:
            q = q.filter(Trade.signal_type == signal_type)
        trades = q.all()

    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "avg_pnl": 0.0}

    wins   = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    return {
        "total":    len(trades),
        "wins":     len(wins),
        "losses":   len(losses),
        "win_rate": len(wins) / len(trades),
        "avg_pnl":  sum(t.pnl_pct for t in trades) / len(trades),
    }
