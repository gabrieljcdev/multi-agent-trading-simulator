"""
database/queries.py
Reusable query functions. Everything that touches the DB goes through here.
"""

from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import func, desc

from .db import get_session
from .models import (
    Candle, Signal, Trade, SentimentSnapshot, SentimentLog,
    DailyStats, CircuitBreakerLog,
    PortfolioSnapshot, AgentEvent, ArbTrade,
    DataLog,
    MacroLog, CalendarEvent,
)


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


def get_today_skipped_signals() -> int:
    """Count signals skipped today (UTC).

    Returns an int directly — counters are the only consumer. The
    previous list-returning shape forced every caller through `len(...)`.
    A row counts as skipped if `user_action == 'skip'` OR `skip_reason`
    is populated (the gate writes skip_reason without bumping
    user_action when the cycle short-circuits before user-facing
    approval, e.g. on a SENTIMENT_HARD_BLOCK).
    """
    today_utc = datetime.utcnow().date()
    with get_session() as s:
        return (
            s.query(func.count(Signal.id))
            .filter(
                func.date(Signal.timestamp) == today_utc,
                (Signal.user_action == "skip") | (Signal.skip_reason.isnot(None)),
            )
            .scalar()
            or 0
        )


def get_trade_by_id(trade_id: int):
    """Single Trade row by primary key, or None if not found.

    Replaces the pattern of scanning today's trades for one id in
    core/bot.py:_get_trade_by_id — that helper now delegates here.
    """
    with get_session() as s:
        return s.get(Trade, trade_id)


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

def log_sentiment_result(result, composite_score: float = 0.0):
    """Persist a SourceResult to the sentiment_log table.

    `result` is a sentiment.base.SourceResult — duck-typed here to avoid
    a queries→sentiment import cycle. The aggregator passes composite_score
    so every row carries the blended value at the time it was written.
    """
    with get_session() as s:
        s.add(SentimentLog(
            source_id=result.source_id,
            score=result.score,
            composite_score=composite_score,
            hard_block=result.hard_block,
            block_reason=result.block_reason or "",
            confidence=result.confidence,
            raw_data=result.raw_data,
        ))


def get_sentiment_history(source_id: str, hours: int = 24):
    """Per-source history from the new aggregator log table."""
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as s:
        return (
            s.query(SentimentLog)
            .filter(SentimentLog.source_id == source_id,
                    SentimentLog.timestamp >= since)
            .order_by(SentimentLog.timestamp)
            .all()
        )


def get_composite_history(hours: int = 24):
    """All composite scores ever computed in the lookback window."""
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as s:
        return (
            s.query(SentimentLog.timestamp, SentimentLog.composite_score)
            .filter(SentimentLog.timestamp >= since)
            .order_by(SentimentLog.timestamp)
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


# ── Arb engine ─────────────────────────────────────────────

def log_arb_trade(result, sim_mode: bool = True) -> None:
    """Persist one ArbResult.

    `result` is duck-typed (execution.arb_engine.ArbResult) to avoid a
    queries→arb_engine import cycle.
    """
    opp = result.opportunity
    with get_session() as s:
        s.add(ArbTrade(
            symbol=opp.symbol,
            buy_exchange=opp.buy_exchange,
            sell_exchange=opp.sell_exchange,
            buy_price=opp.buy_price,
            sell_price=opp.sell_price,
            buy_fill=result.buy_fill,
            sell_fill=result.sell_fill,
            gross_gap_pct=opp.gross_gap_pct,
            net_gap_pct=opp.net_gap_pct,
            size_usd=opp.max_size_usd,
            gross_pnl_usd=result.gross_pnl_usd,
            net_pnl_usd=result.net_pnl_usd,
            execution_ms=result.execution_ms,
            sim_mode=sim_mode,
            success=result.success,
            error=result.error,
        ))


def get_arb_trades(hours: int = 24) -> list:
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as s:
        return (
            s.query(ArbTrade)
            .filter(ArbTrade.timestamp >= since)
            .order_by(desc(ArbTrade.timestamp))
            .all()
        )


def get_arb_pnl_today() -> float:
    today = datetime.utcnow().date()
    with get_session() as s:
        rows = (
            s.query(ArbTrade.net_pnl_usd)
            .filter(func.date(ArbTrade.timestamp) == today,
                    ArbTrade.success == True,  # noqa: E712 (SQLAlchemy idiom)
                    ArbTrade.net_pnl_usd.isnot(None))
            .all()
        )
    return float(sum(r[0] for r in rows))


def get_arb_stats() -> dict:
    """Aggregate stats for the dashboard: trade count, win rate, avg P&L,
    best pair, best exchange combination."""
    with get_session() as s:
        rows = (
            s.query(ArbTrade)
            .filter(ArbTrade.success == True)  # noqa: E712
            .all()
        )
    if not rows:
        return {"total": 0, "wins": 0, "win_rate": 0.0, "avg_net_pnl": 0.0,
                "best_pair": None, "best_combo": None}
    pnls = [r.net_pnl_usd or 0.0 for r in rows]
    wins = sum(1 for p in pnls if p > 0)

    from collections import defaultdict
    pair_pnl = defaultdict(float)
    combo_pnl = defaultdict(float)
    for r in rows:
        pair_pnl[r.symbol] += r.net_pnl_usd or 0.0
        key = f"{r.buy_exchange}->{r.sell_exchange}"
        combo_pnl[key] += r.net_pnl_usd or 0.0
    best_pair  = max(pair_pnl.items(),  key=lambda kv: kv[1])[0] if pair_pnl  else None
    best_combo = max(combo_pnl.items(), key=lambda kv: kv[1])[0] if combo_pnl else None

    return {
        "total":       len(rows),
        "wins":        wins,
        "win_rate":    wins / len(rows),
        "avg_net_pnl": sum(pnls) / len(pnls),
        "best_pair":   best_pair,
        "best_combo":  best_combo,
    }


# ── Portfolio + agents ─────────────────────────────────────

def log_portfolio_snapshot(stats: dict) -> None:
    """Persist one aggregated portfolio state row.

    `stats` is the dict returned by Coordinator.get_portfolio_stats —
    well-known fields as columns, whole dict as JSON.

    Timestamp is set explicitly here even though the column has
    `default=datetime.utcnow`. Explicit set documents intent and
    protects against any future change that swaps the default to a
    column-level expression (which SQLite renders as the unix epoch
    0 / NULL depending on the driver). Stored as a Python datetime —
    callers using `SELECT timestamp FROM portfolio_snapshots` will see
    ISO strings, not unix floats.
    """
    with get_session() as s:
        s.add(PortfolioSnapshot(
            timestamp=datetime.utcnow(),
            total_equity=stats.get("total_equity",       0.0),
            total_daily_pnl=stats.get("total_daily_pnl", 0.0),
            total_exposure_pct=stats.get("total_exposure_pct", 0.0),
            agents_running=stats.get("agents_running", 0),
            portfolio_status=stats.get("portfolio_status", "?"),
            snapshot_json=stats,
        ))


def log_agent_event(agent_id: str, event_type: str, detail: str = "") -> None:
    """Append a lifecycle event for an agent (or 'portfolio' for cross-agent events)."""
    with get_session() as s:
        s.add(AgentEvent(
            agent_id=agent_id,
            event_type=event_type,
            detail=detail or "",
        ))


def get_last_equity() -> Optional[float]:
    """Return total_equity from the most recent portfolio_snapshot row,
    or None when the table is empty.

    Drives the bot's equity recovery on startup — see core/bot.py.
    Returns None (not 0.0) so the caller can distinguish "fresh DB" from
    "we genuinely went bust".
    """
    with get_session() as s:
        row = (
            s.query(PortfolioSnapshot.total_equity)
            .order_by(desc(PortfolioSnapshot.timestamp))
            .first()
        )
    if row is None or row[0] is None:
        return None
    return float(row[0])


def get_portfolio_history(hours: int = 24) -> list:
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as s:
        return (
            s.query(PortfolioSnapshot)
            .filter(PortfolioSnapshot.timestamp >= since)
            .order_by(PortfolioSnapshot.timestamp)
            .all()
        )


def get_agent_events(agent_id: Optional[str] = None, limit: int = 50) -> list:
    with get_session() as s:
        q = s.query(AgentEvent)
        if agent_id:
            q = q.filter(AgentEvent.agent_id == agent_id)
        return q.order_by(desc(AgentEvent.timestamp)).limit(limit).all()


# ── Circuit Breakers ───────────────────────────────────────

def log_circuit_breaker(reason: str, detail: str, auto_resume_at: datetime = None):
    with get_session() as s:
        s.add(CircuitBreakerLog(
            reason=reason,
            detail=detail,
            auto_resume_at=auto_resume_at
        ))


# ── Data sources (macro / on-chain / fx) ───────────────────

def log_data_point(point) -> None:
    """Persist a data_sources.base.DataPoint to data_log.

    `point` is duck-typed (DataPoint) to avoid a queries→data_sources
    import cycle. The DataPoint's own timestamp is float epoch — we store
    a datetime for DB ergonomics.
    """
    from datetime import datetime as _dt
    ts = _dt.utcfromtimestamp(point.timestamp) if getattr(point, "timestamp", 0) else _dt.utcnow()
    with get_session() as s:
        s.add(DataLog(
            timestamp=ts,
            source_id=point.source_id,
            metric=point.metric,
            symbol=point.symbol,
            value=point.value,
            raw_data=point.raw_data,
            error=point.error,
        ))


def get_data_history(
    source_id: str,
    metric: str,
    symbol: Optional[str] = None,
    hours: int = 24,
) -> list:
    """Chronological list of DataLog rows for a (source, metric[, symbol]).

    Pass symbol=None for global metrics (VIX, DXY, CPI). Newest last so
    callers can plot directly.
    """
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as s:
        q = (
            s.query(DataLog)
            .filter(DataLog.source_id == source_id,
                    DataLog.metric    == metric,
                    DataLog.timestamp >= since)
        )
        if symbol is None:
            q = q.filter(DataLog.symbol.is_(None))
        else:
            q = q.filter(DataLog.symbol == symbol)
        return q.order_by(DataLog.timestamp).all()


def get_latest_data_point(
    source_id: str,
    metric: str,
    symbol: Optional[str] = None,
) -> Optional[DataLog]:
    """Most recent DataLog row for the key. None if nothing has been logged."""
    with get_session() as s:
        q = (
            s.query(DataLog)
            .filter(DataLog.source_id == source_id,
                    DataLog.metric    == metric)
        )
        if symbol is None:
            q = q.filter(DataLog.symbol.is_(None))
        else:
            q = q.filter(DataLog.symbol == symbol)
        return q.order_by(desc(DataLog.timestamp)).first()


def get_data_at_time(
    source_id: str,
    metric: str,
    timestamp: datetime,
    symbol: Optional[str] = None,
) -> Optional[DataLog]:
    """Closest DataLog row at or before the given timestamp.

    Used for retrospective analysis — "what was VIX when this trade
    opened?". Returns None if nothing was logged before that instant.
    """
    with get_session() as s:
        q = (
            s.query(DataLog)
            .filter(DataLog.source_id == source_id,
                    DataLog.metric    == metric,
                    DataLog.timestamp <= timestamp)
        )
        if symbol is None:
            q = q.filter(DataLog.symbol.is_(None))
        else:
            q = q.filter(DataLog.symbol == symbol)
        return q.order_by(desc(DataLog.timestamp)).first()


# ── Macro regime + calendar ────────────────────────────────

def save_macro_regime(regime) -> None:
    """Persist a macro.regime.MacroRegime row to macro_log.

    `regime` is duck-typed (macro.MacroRegime) to avoid a queries→macro
    import cycle. The full regime is also stored as raw_data JSON so the
    analysis dashboard can reconstruct without joining tables.
    """
    # Pull only well-known fields by name; everything else lives in JSON.
    def _enum_name(v):
        return getattr(v, "name", str(v) if v is not None else None)
    raw = getattr(regime, "raw_data", None) or {}
    with get_session() as s:
        s.add(MacroLog(
            scenario=_enum_name(getattr(regime, "scenario", None)),
            macro_score=getattr(regime, "macro_score", None),
            dollar_strength=_enum_name(getattr(regime, "dollar", None)),
            risk_appetite=_enum_name(getattr(regime, "risk", None)),
            rate_environment=_enum_name(getattr(regime, "rates", None)),
            vol_regime=_enum_name(getattr(regime, "vol", None)),
            dxy=getattr(regime, "dxy", None),
            vix=getattr(regime, "vix", None),
            yield_10y=getattr(regime, "yield_10y", None),
            yield_2y=getattr(regime, "yield_2y", None),
            yield_curve=getattr(regime, "yield_curve", None),
            fed_funds_rate=getattr(regime, "fed_funds_rate", None),
            cpi_yoy=getattr(regime, "cpi_yoy", None),
            confidence=getattr(regime, "confidence", None),
            raw_data=raw,
        ))


def get_macro_history(hours: int = 24) -> list:
    """All MacroLog rows from the last `hours`, newest last."""
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as s:
        return (
            s.query(MacroLog)
            .filter(MacroLog.timestamp >= since)
            .order_by(MacroLog.timestamp)
            .all()
        )


def save_calendar_events(events: list) -> None:
    """Upsert calendar events keyed on event_id.

    `events` are duck-typed macro.signals.CalendarEvent. event_id is
    expected to be stable across refreshes — we overwrite forecast /
    actual / previous in place rather than accumulating duplicates.
    """
    if not events:
        return
    with get_session() as s:
        for e in events:
            existing = (
                s.query(CalendarEvent)
                .filter_by(event_id=e.event_id)
                .first()
            )
            payload = dict(
                event_id=e.event_id,
                title=e.title,
                country=getattr(e, "country", None),
                scheduled_utc=e.scheduled_utc,
                impact=getattr(getattr(e, "impact", None), "name", None),
                actual=getattr(e, "actual", None),
                forecast=getattr(e, "forecast", None),
                previous=getattr(e, "previous", None),
                source_id=getattr(e, "source_id", None),
                fetched_at=datetime.utcnow(),
            )
            if existing:
                for k, v in payload.items():
                    setattr(existing, k, v)
            else:
                s.add(CalendarEvent(**payload))


def get_pending_events(hours_ahead: int = 48) -> list:
    """Upcoming events between now and now+hours_ahead, soonest first."""
    now = datetime.utcnow()
    until = now + timedelta(hours=hours_ahead)
    with get_session() as s:
        return (
            s.query(CalendarEvent)
            .filter(CalendarEvent.scheduled_utc >= now,
                    CalendarEvent.scheduled_utc <= until)
            .order_by(CalendarEvent.scheduled_utc)
            .all()
        )


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
