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
    PortfolioSnapshot, AgentEvent,
    ArbTrade, ArbOpportunity, FundingArbTrade,
    DataLog,
    MacroLog, CalendarEvent,
    ScalpObservationModel,
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

def log_arb_trade(result, sim_mode: bool = True) -> int:
    """Persist one ArbResult and return its new row id.

    `result` is duck-typed (execution.arb_engine.ArbResult) to avoid a
    queries→arb_engine import cycle. slippage_*_pct and status default
    via getattr so older callers that don't fill them still work.
    """
    opp = result.opportunity
    with get_session() as s:
        row = ArbTrade(
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
            status=getattr(result, "status", "executed"),
            slippage_buy_pct=getattr(result, "slippage_buy_pct", None),
            slippage_sell_pct=getattr(result, "slippage_sell_pct", None),
            sim_mode=sim_mode,
            success=result.success,
            error=result.error,
        )
        s.add(row)
        s.flush()
        return row.id


def log_arb_balance_fail(
    symbol:           str,
    buy_exchange:     str,
    sell_exchange:    str,
    detail:           str,
    sim_mode:         bool = True,
) -> int:
    """Persist a balance-check miss as an ArbTrade row with
    status='balance_fail'. Returns the new row id.

    Kept distinct from log_arb_trade because the gate fires before an
    ArbResult exists — we want the miss recorded with zero P&L and the
    failure detail in the error column.
    """
    with get_session() as s:
        row = ArbTrade(
            symbol=symbol,
            buy_exchange=buy_exchange,
            sell_exchange=sell_exchange,
            status="balance_fail",
            sim_mode=sim_mode,
            success=False,
            error=detail,
        )
        s.add(row)
        s.flush()
        return row.id


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


# ── Arb opportunities (detected gaps, executed + missed) ───

def log_arb_opportunity(
    symbol:          str,
    buy_exchange:    str,
    sell_exchange:   str,
    gap_pct:         float,
    threshold_pct:   float,
    above_threshold: bool,
    depth_buy_usd:   float,
    depth_sell_usd:  float,
    executed:        bool = False,
    arb_trade_id:    Optional[int] = None,
) -> int:
    """Persist a detected gap. Always logged once the liquidity check
    clears, regardless of whether the engine fired."""
    with get_session() as s:
        row = ArbOpportunity(
            symbol=symbol,
            buy_exchange=buy_exchange,
            sell_exchange=sell_exchange,
            gap_pct=gap_pct,
            threshold_pct=threshold_pct,
            above_threshold=above_threshold,
            depth_buy_usd=depth_buy_usd,
            depth_sell_usd=depth_sell_usd,
            executed=executed,
            arb_trade_id=arb_trade_id,
        )
        s.add(row)
        s.flush()
        return row.id


def mark_arb_opportunity_executed(opp_id: int, arb_trade_id: Optional[int]) -> None:
    """Flip executed=True on an ArbOpportunity row and link the firing
    arb_trades row, if known. No-op when the row is missing."""
    with get_session() as s:
        row = s.get(ArbOpportunity, opp_id)
        if row is None:
            return
        row.executed = True
        if arb_trade_id is not None:
            row.arb_trade_id = arb_trade_id


def get_arb_opportunities_today() -> list:
    """Every ArbOpportunity row detected today (UTC), newest first."""
    today = datetime.utcnow().date()
    with get_session() as s:
        return (
            s.query(ArbOpportunity)
            .filter(func.date(ArbOpportunity.detected_at) == today)
            .order_by(desc(ArbOpportunity.detected_at))
            .all()
        )


def get_arb_opportunity_stats() -> dict:
    """Aggregate dashboard stats over today's detected opportunities.

    Keys: total_detected, total_executed, execution_rate_pct,
    avg_gap_pct, max_gap_pct, top_pairs (list of (symbol, count)).
    Returns zeros when the table is empty.
    """
    rows = get_arb_opportunities_today()
    if not rows:
        return {
            "total_detected":      0,
            "total_executed":      0,
            "execution_rate_pct":  0.0,
            "avg_gap_pct":         0.0,
            "max_gap_pct":         0.0,
            "top_pairs":           [],
        }
    total = len(rows)
    executed = sum(1 for r in rows if r.executed)
    gaps = [r.gap_pct for r in rows if r.gap_pct is not None]

    from collections import Counter
    counts = Counter(r.symbol for r in rows)
    top_pairs = counts.most_common(5)

    return {
        "total_detected":     total,
        "total_executed":     executed,
        "execution_rate_pct": (executed / total * 100.0) if total else 0.0,
        "avg_gap_pct":        sum(gaps) / len(gaps) if gaps else 0.0,
        "max_gap_pct":        max(gaps) if gaps else 0.0,
        "top_pairs":          top_pairs,
    }


# ── Funding rate arb ───────────────────────────────────────

def log_funding_arb_trade(result, sim_mode: bool = True) -> int:
    """Persist one FundingArbResult. Same duck-typing as log_arb_trade —
    avoids a queries→execution import cycle.

    Returns the new row id.
    """
    opp = result.opportunity
    with get_session() as s:
        row = FundingArbTrade(
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
            status=getattr(result, "status", "executed"),
            slippage_buy_pct=getattr(result, "slippage_buy_pct", None),
            slippage_sell_pct=getattr(result, "slippage_sell_pct", None),
            funding_rate_pct=getattr(opp, "funding_rate_pct", None),
            sim_mode=sim_mode,
            success=result.success,
            error=result.error,
        )
        s.add(row)
        s.flush()
        return row.id


def get_funding_arb_pnl_today() -> float:
    today = datetime.utcnow().date()
    with get_session() as s:
        rows = (
            s.query(FundingArbTrade.net_pnl_usd)
            .filter(func.date(FundingArbTrade.timestamp) == today,
                    FundingArbTrade.success == True,  # noqa: E712
                    FundingArbTrade.net_pnl_usd.isnot(None))
            .all()
        )
    return float(sum(r[0] for r in rows))


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


# ── Realised-P&L reconstruction (equity recovery on restart) ────────────────
# Each fund tracks daily/total P&L in in-memory counters that reset on every
# launch. These sum the persisted ledger so an agent can resume from its
# accumulated figure after a restart — or a hard kill, which never runs the
# clean-shutdown snapshot. `today=True` restricts to the current UTC date,
# matching the daily-P&L reset semantics.

def get_trade_realized_pnl(*, only_strategy: Optional[str] = None,
                           exclude_strategy: Optional[str] = None,
                           today: bool = False) -> float:
    """Sum realised pnl_usd over closed trades in the Trade ledger. Signal-fund
    fills and scalp fills share this table (arb lives in ArbTrade), so callers
    scope by strategy: the signal fund uses exclude_strategy="scalp"."""
    today_d = datetime.utcnow().date()
    with get_session() as s:
        rows = s.query(Trade).filter(Trade.pnl_usd.isnot(None)).all()
    total = 0.0
    for t in rows:
        strat = t.strategy or ""
        if only_strategy is not None and strat != only_strategy:
            continue
        if exclude_strategy is not None and strat == exclude_strategy:
            continue
        if today and not (t.timestamp_close and t.timestamp_close.date() == today_d):
            continue
        total += float(t.pnl_usd or 0.0)
    return round(total, 2)


def get_arb_realized_pnl(*, today: bool = False) -> float:
    """Sum realised net_pnl_usd over the arb fund's ledger (ArbTrade)."""
    today_d = datetime.utcnow().date()
    with get_session() as s:
        rows = s.query(ArbTrade).filter(ArbTrade.net_pnl_usd.isnot(None)).all()
    total = 0.0
    for r in rows:
        if today and not (r.timestamp and r.timestamp.date() == today_d):
            continue
        total += float(r.net_pnl_usd or 0.0)
    return round(total, 2)


def get_scalp_realized_pnl(*, today: bool = False) -> float:
    """Sum realised P&L over closed scalp observations (would_entry,
    exit_price>0). pnl_usd is gross, which equals net at MEXC's 0% fees."""
    today_d = datetime.utcnow().date()
    with get_session() as s:
        rows = (
            s.query(ScalpObservationModel)
            .filter(ScalpObservationModel.would_entry == True,   # noqa: E712
                    ScalpObservationModel.exit_price > 0)
            .all()
        )
    total = 0.0
    for r in rows:
        if today and not (r.created_at and r.created_at.date() == today_d):
            continue
        total += float(r.pnl_usd or 0.0)
    return round(total, 2)


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


# ── Scalping agent (observation logs) ──────────────────────

def save_scalp_observations(obs_list: list) -> None:
    """Upsert a batch of scalp observations.

    `obs_list` is duck-typed (each row exposes the fields enumerated in
    the ScalpObservation dataclass). Natural key is
    (symbol, exchange, timestamp) — first write creates the row, later
    writes (when the position exits) fill exit_price + exit_reason etc.

    Synchronous on purpose — matches every other query helper. The agent
    can call this from a worker thread / asyncio.to_thread if it wants.
    DB failures are non-fatal at the call site; this helper just raises.
    """
    if not obs_list:
        return
    with get_session() as s:
        for o in obs_list:
            existing = (
                s.query(ScalpObservationModel)
                .filter_by(
                    symbol=o.symbol,
                    exchange=o.exchange,
                    timestamp=o.timestamp,
                )
                .first()
            )
            payload = dict(
                symbol=o.symbol, exchange=o.exchange, timestamp=o.timestamp,
                ofi_z=o.ofi_z, direction=o.direction, strength=o.strength,
                tfi_confirms=o.tfi_confirms, raw_tfi=o.raw_tfi,
                spread_bps=o.spread_bps, regime=o.regime,
                round_trip_cost_bps=o.round_trip_cost_bps,
                min_win_rate_required=o.min_win_rate_required,
                tp_bps=o.tp_bps, sl_bps=o.sl_bps,
                would_entry=o.would_entry, skip_reason=o.skip_reason,
                entry_price=o.entry_price,
                exit_price=o.exit_price, exit_time=o.exit_time,
                exit_reason=o.exit_reason,
                hold_sec=o.hold_sec, pnl_bps=o.pnl_bps, pnl_usd=o.pnl_usd,
                observation_only=o.observation_only,
                price_30s=getattr(o, "price_30s", 0.0),
                price_1m=getattr(o,  "price_1m",  0.0),
                price_3m=getattr(o,  "price_3m",  0.0),
                price_5m=getattr(o,  "price_5m",  0.0),
                confluence_score=getattr(o,      "confluence_score", None),
                strength_label=getattr(o,        "strength_label", None),
                cross_exchange_agrees=getattr(o, "cross_exchange_agrees", None),
                btc_compatible=getattr(o,        "btc_compatible", None),
                adverse_selection_ok=getattr(o,  "adverse_selection_ok", None),
                depth_ok=getattr(o,              "depth_ok", None),
                vwap_aligned=getattr(o,          "vwap_aligned", None),
                htf_aligned=getattr(o,           "htf_aligned", None),
                volume_adequate=getattr(o,       "volume_adequate", None),
                atr_bps=getattr(o,               "atr_bps", None),
                atr_adjusted=getattr(o,          "atr_adjusted", None),
                sl_clamped=getattr(o,            "sl_clamped", None),
                rr_actual=getattr(o,             "rr_actual", None),
            )
            if existing is None:
                s.add(ScalpObservationModel(**payload))
            else:
                # Overwrite when exit data has arrived OR when any micro
                # price slot has been filled — the tracker flushes a row
                # purely to backfill price_30s / 1m / 3m / 5m even before
                # the position closes, and we don't want to drop those.
                price_backfill = any((
                    payload["price_30s"] > 0, payload["price_1m"] > 0,
                    payload["price_3m"]  > 0, payload["price_5m"] > 0,
                ))
                if o.exit_price > 0 or price_backfill:
                    for k, v in payload.items():
                        setattr(existing, k, v)


def get_scalp_summary(days: int = 7) -> dict:
    """Aggregate metrics across the last `days` of observations.

    Returns total_evaluated, would_enter, closed (would_entry & exit_price>0),
    win_rate, avg gross + net pnl_bps, avg hold_sec, plus a per-exchange
    breakdown. Net pnl is computed in SQL as pnl_bps - round_trip_cost_bps
    so callers don't carry the convention.
    """
    import time as _time
    cutoff = _time.time() - days * 86400
    out = {
        "total_evaluated": 0,
        "would_enter":     0,
        "closed":          0,
        "win_rate":        0.0,
        "avg_pnl_bps":     0.0,
        "avg_pnl_net_bps": 0.0,
        "avg_hold_sec":    0.0,
        "by_exchange":     {},
    }
    with get_session() as s:
        rows = (
            s.query(ScalpObservationModel)
            .filter(ScalpObservationModel.timestamp >= cutoff)
            .all()
        )
    if not rows:
        return out

    out["total_evaluated"] = len(rows)
    entries = [r for r in rows if r.would_entry]
    out["would_enter"]     = len(entries)
    closed = [r for r in entries if (r.exit_price or 0) > 0]
    out["closed"]          = len(closed)
    if not closed:
        return out

    wins = sum(1 for r in closed if (r.pnl_bps or 0) > 0)
    out["win_rate"]        = wins / len(closed)
    out["avg_pnl_bps"]     = sum((r.pnl_bps or 0) for r in closed) / len(closed)
    out["avg_pnl_net_bps"] = sum(
        (r.pnl_bps or 0) - (r.round_trip_cost_bps or 0) for r in closed
    ) / len(closed)
    out["avg_hold_sec"]    = sum((r.hold_sec or 0) for r in closed) / len(closed)

    by_ex: dict[str, dict] = {}
    for r in closed:
        ex = r.exchange
        agg = by_ex.setdefault(ex, {"n": 0, "wins": 0, "net_sum": 0.0})
        agg["n"] += 1
        if (r.pnl_bps or 0) > 0:
            agg["wins"] += 1
        agg["net_sum"] += (r.pnl_bps or 0) - (r.round_trip_cost_bps or 0)
    out["by_exchange"] = {
        ex: {
            "n":          agg["n"],
            "win_rate":   agg["wins"] / agg["n"] if agg["n"] else 0.0,
            "avg_pnl_net": agg["net_sum"] / agg["n"] if agg["n"] else 0.0,
        }
        for ex, agg in by_ex.items()
    }
    return out


def get_scalp_observations(
    symbol: Optional[str] = None,
    exchange: Optional[str] = None,
    would_entry_only: bool = True,
    closed_only: bool = True,
    limit: int = 500,
) -> list:
    """Filtered ScalpObservationModel rows as plain dicts. Newest first."""
    with get_session() as s:
        q = s.query(ScalpObservationModel)
        if symbol:
            q = q.filter(ScalpObservationModel.symbol == symbol)
        if exchange:
            q = q.filter(ScalpObservationModel.exchange == exchange)
        if would_entry_only:
            q = q.filter(ScalpObservationModel.would_entry.is_(True))
        if closed_only:
            q = q.filter(ScalpObservationModel.exit_price > 0)
        rows = (
            q.order_by(desc(ScalpObservationModel.timestamp))
            .limit(limit)
            .all()
        )
    # Detach into plain dicts so callers don't depend on the ORM session.
    return [
        {
            "id": r.id, "symbol": r.symbol, "exchange": r.exchange,
            "timestamp": r.timestamp,
            "ofi_z": r.ofi_z, "direction": r.direction, "strength": r.strength,
            "tfi_confirms": r.tfi_confirms, "raw_tfi": r.raw_tfi,
            "spread_bps": r.spread_bps, "regime": r.regime,
            "round_trip_cost_bps": r.round_trip_cost_bps,
            "min_win_rate_required": r.min_win_rate_required,
            "tp_bps": r.tp_bps, "sl_bps": r.sl_bps,
            "would_entry": r.would_entry, "skip_reason": r.skip_reason,
            "entry_price": r.entry_price,
            "exit_price": r.exit_price, "exit_time": r.exit_time,
            "exit_reason": r.exit_reason, "hold_sec": r.hold_sec,
            "pnl_bps": r.pnl_bps, "pnl_usd": r.pnl_usd,
            "observation_only": r.observation_only,
            "price_30s": r.price_30s, "price_1m": r.price_1m,
            "price_3m":  r.price_3m,  "price_5m": r.price_5m,
        }
        for r in rows
    ]


# Analysis SQL — paste into sqlite3 once observations have accumulated.
# All net P&L is computed live as (pnl_bps - round_trip_cost_bps); stored
# pnl_bps is gross by design.
#
# Core performance — win rate + net P&L by exchange:
#   SELECT exchange, direction,
#          COUNT(*) as n,
#          ROUND(AVG(CASE WHEN pnl_bps > 0 THEN 1.0 ELSE 0.0 END), 3) as win_rate,
#          ROUND(AVG(pnl_bps), 3) as avg_gross_bps,
#          ROUND(AVG(pnl_bps - round_trip_cost_bps), 3) as avg_net_bps,
#          ROUND(AVG(min_win_rate_required), 3) as avg_breakeven_wr
#   FROM scalp_observations
#   WHERE would_entry = 1 AND exit_price > 0
#   GROUP BY exchange, direction;
#
# Does higher OFI z predict better net outcomes?
#   SELECT exchange,
#          ROUND(ofi_z * 2) / 2 as z_bucket,
#          COUNT(*) as n,
#          ROUND(AVG(pnl_bps - round_trip_cost_bps), 3) as avg_net_bps,
#          ROUND(AVG(CASE WHEN pnl_bps > 0 THEN 1.0 ELSE 0.0 END), 3) as win_rate
#   FROM scalp_observations
#   WHERE would_entry = 1 AND exit_price > 0
#   GROUP BY exchange, z_bucket ORDER BY exchange, z_bucket;
#
# Why is the gate blocking entries?
#   SELECT exchange, skip_reason, COUNT(*) as n
#   FROM scalp_observations WHERE would_entry = 0
#   GROUP BY exchange, skip_reason ORDER BY n DESC;
#
# Exit-reason breakdown per exchange:
#   SELECT exchange, exit_reason, COUNT(*) as n,
#          ROUND(AVG(pnl_bps - round_trip_cost_bps), 3) as avg_net_bps
#   FROM scalp_observations
#   WHERE would_entry = 1 AND exit_price > 0
#   GROUP BY exchange, exit_reason ORDER BY exchange, n DESC;
#
# Did OFI predict direction correctly, regardless of exit reason?
# Uses the micro-tracker price_1m / price_3m backfills — answers
# "was the signal right even when OFI_EXHAUSTED cut us out early?"
#   SELECT exchange, direction,
#          ROUND(AVG(CASE
#              WHEN direction='LONG'  AND price_1m > entry_price THEN 1.0
#              WHEN direction='SHORT' AND price_1m < entry_price THEN 1.0
#              ELSE 0.0 END), 3) as directional_accuracy_1m,
#          ROUND(AVG(CASE
#              WHEN direction='LONG'  AND price_3m > entry_price THEN 1.0
#              WHEN direction='SHORT' AND price_3m < entry_price THEN 1.0
#              ELSE 0.0 END), 3) as directional_accuracy_3m,
#          COUNT(*) as n
#   FROM scalp_observations
#   WHERE would_entry = 1 AND price_1m > 0
#   GROUP BY exchange, direction;
#
# Is OFI_EXHAUSTED cutting winners short? Compare net at exit vs net
# if we'd held to 3 minutes. If "if held 3m" > "at exit" consistently,
# consider raising SCALP_OFI_Z_EXIT.
#   SELECT exit_reason,
#          ROUND(AVG(pnl_bps - round_trip_cost_bps), 3) as avg_net_at_exit,
#          ROUND(AVG(CASE WHEN direction='LONG'
#              THEN (price_3m - entry_price)/entry_price*10000 - round_trip_cost_bps
#              ELSE (entry_price - price_3m)/entry_price*10000 - round_trip_cost_bps
#              END), 3) as avg_net_if_held_3m,
#          COUNT(*) as n
#   FROM scalp_observations
#   WHERE would_entry = 1 AND exit_price > 0 AND price_3m > 0
#   GROUP BY exit_reason;


# ── Analytics helpers (used by predictive engine) ──────────

def get_signal_win_rate(signal_type: str = None, days: int = 30,
                        exclude_strategy: str = None) -> dict:
    """Return win rate stats for model training and UI display.

    exclude_strategy drops trades whose `strategy` matches (e.g. "scalp"):
    other funds write to the shared trades table, so the signal fund's win
    rate must not count them.
    """
    with get_session() as s:
        q = s.query(Trade).filter(
            Trade.timestamp_open >= datetime.utcnow() - timedelta(days=days),
            Trade.pnl_pct.isnot(None)
        )
        if signal_type:
            q = q.filter(Trade.signal_type == signal_type)
        trades = q.all()
        if exclude_strategy:
            trades = [t for t in trades if t.strategy != exclude_strategy]

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


# ── Web control panel (ui/web_server.py) ───────────────────────────────────

_SESSIONS = ("LONDON", "NEW_YORK", "ASIA", "OFF_HOURS")


def _session_for_hour(hour: int) -> str:
    """UTC trading session for an hour — mirrors Dashboard._current_session.
    There is no `session` column on trades, so callers derive it here."""
    if 7 <= hour < 13:
        return "LONDON"
    if 13 <= hour < 20:
        return "NEW_YORK"
    if 0 <= hour < 7:
        return "ASIA"
    return "OFF_HOURS"


def get_session_pnl_today() -> dict:
    """Today's realised P&L grouped by UTC trading session — across every
    fund. Combines the Trade ledger (signal + scalp fills, by close time)
    and the ArbTrade ledger (arb fills, by execution time); arb is the most
    active fund, so omitting it left the panel looking frozen. Always returns
    all four session keys. {"LONDON": {"pnl": 5.20, "trades": 4}, ...}."""
    out = {s: {"pnl": 0.0, "trades": 0} for s in _SESSIONS}
    today = datetime.utcnow().date()
    with get_session() as s:
        rows = (
            s.query(Trade)
            .filter(func.date(Trade.timestamp_close) == today,
                    Trade.pnl_usd.isnot(None))
            .all()
        )
        for t in rows:
            ts = t.timestamp_close or t.timestamp_open
            sess = _session_for_hour(ts.hour) if ts else "OFF_HOURS"
            out[sess]["pnl"] += float(t.pnl_usd or 0.0)
            out[sess]["trades"] += 1
        arb_rows = (
            s.query(ArbTrade)
            .filter(func.date(ArbTrade.timestamp) == today,
                    ArbTrade.net_pnl_usd.isnot(None))
            .all()
        )
        for r in arb_rows:
            sess = _session_for_hour(r.timestamp.hour) if r.timestamp else "OFF_HOURS"
            out[sess]["pnl"] += float(r.net_pnl_usd or 0.0)
            out[sess]["trades"] += 1
    for v in out.values():
        v["pnl"] = round(v["pnl"], 2)
    return out


def get_top_pairs(n: int = 5) -> list[dict]:
    """Top n pairs by total realised P&L (all-time, closed trades)."""
    from collections import defaultdict
    agg: dict = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    with get_session() as s:
        rows = s.query(Trade).filter(Trade.pnl_usd.isnot(None)).all()
        for t in rows:
            a = agg[t.pair]
            a["pnl"] += float(t.pnl_usd or 0.0)
            a["trades"] += 1
            if (t.pnl_pct if t.pnl_pct is not None else t.pnl_usd or 0) > 0:
                a["wins"] += 1
    out = [
        {"pair": p, "pnl": round(a["pnl"], 2), "trades": a["trades"],
         "win_rate": round(a["wins"] / a["trades"] * 100, 1) if a["trades"] else 0.0}
        for p, a in agg.items()
    ]
    out.sort(key=lambda d: d["pnl"], reverse=True)
    return out[:n]


def get_strategy_performance() -> list[dict]:
    """Performance by signal track (closed trades). Always returns the four
    tracks; scalp is excluded (it's a separate fund)."""
    from collections import defaultdict
    names = {
        "arb":       "Track A — Arbitrage",
        "momentum":  "Track B — Momentum",
        "reversion": "Track C — Mean Reversion",
        "sweep":     "Track D — Liquidity Sweep",
    }
    agg: dict = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    with get_session() as s:
        rows = s.query(Trade).filter(Trade.pnl_usd.isnot(None)).all()
        for t in rows:
            st = (t.signal_type or "").lower()
            if st not in names:
                continue
            a = agg[st]
            a["pnl"] += float(t.pnl_usd or 0.0)
            a["trades"] += 1
            if (t.pnl_pct or 0) > 0:
                a["wins"] += 1
    out = []
    for st, name in names.items():
        a = agg.get(st, {"pnl": 0.0, "trades": 0, "wins": 0})
        out.append({
            "name": name,
            "trades": a["trades"],
            "win_rate": round(a["wins"] / a["trades"] * 100, 1) if a["trades"] else 0.0,
            "pnl": round(a["pnl"], 2),
        })
    return out


def get_recent_postmortems(n: int = 3) -> list[dict]:
    """Last n Claude self-reviews. Stored on Trade.claude_postmortem (there
    is no separate postmortems table)."""
    out: list[dict] = []
    with get_session() as s:
        rows = (
            s.query(Trade)
            .filter(Trade.claude_postmortem.isnot(None))
            .order_by(desc(Trade.timestamp_close))
            .limit(n)
            .all()
        )
        for t in rows:
            ts = t.timestamp_close or t.timestamp_open
            out.append({
                "ts": ts.strftime("%H:%M") if ts else "—",
                "trade_range": f"trade {t.id}",
                "body": t.claude_postmortem or "",
                "tags": [],
            })
    return out


def _arb_trade_to_dict(r) -> dict:
    gross = float(r.gross_pnl_usd or 0.0)
    net = float(r.net_pnl_usd or 0.0)
    return {
        "id": r.id,
        "ts": r.timestamp.strftime("%H:%M") if r.timestamp else "—",
        "pair": r.symbol,
        "buy_exchange": r.buy_exchange,
        "sell_exchange": r.sell_exchange,
        "gap_pct": round(float(r.net_gap_pct or 0.0), 4),
        "size_usd": round(float(r.size_usd or 0.0), 2),
        "net_pnl": round(net, 2),
        "fees": round(gross - net, 2),
        "fill_ms": round(float(r.execution_ms or 0.0)),
        "status": r.status or ("filled" if r.success else "—"),
    }


def get_arb_trades_all() -> list[dict]:
    """All arb trades, newest first — for the web UI Arb History tab."""
    with get_session() as s:
        rows = s.query(ArbTrade).order_by(desc(ArbTrade.timestamp)).all()
        return [_arb_trade_to_dict(r) for r in rows]


# ── Scalp activation readiness (v1 + v2 observation→live gate) ──────────────

def get_scalp_activation_stats() -> dict:
    """Closed-entry stats both readiness checks consume, from
    scalp_observations (would_entry=1, exit_price>0). Mirrors Query 8 in
    scalping_v2/SCALPING_V2_RECALIBRATION.md."""
    with get_session() as s:
        rows = (
            s.query(ScalpObservationModel)
            .filter(ScalpObservationModel.would_entry == True,   # noqa: E712
                    ScalpObservationModel.exit_price > 0)
            .all()
        )
    n = len(rows)
    if n == 0:
        return {"n_closed": 0, "win_rate": 0.0, "avg_net_bps": 0.0,
                "max_hold_pct": 0.0, "directional_accuracy_1m": 0.0}
    wins     = sum(1 for r in rows if (r.pnl_bps or 0.0) > 0)
    net_sum  = sum((r.pnl_bps or 0.0) - (r.round_trip_cost_bps or 0.0) for r in rows)
    max_hold = sum(1 for r in rows if r.exit_reason == "MAX_HOLD")
    dir_rows = [r for r in rows if r.price_1m and r.price_1m > 0 and r.entry_price]
    dir_hits = sum(
        1 for r in dir_rows
        if (r.direction == "LONG"  and r.price_1m > r.entry_price)
        or (r.direction == "SHORT" and r.price_1m < r.entry_price)
    )
    return {
        "n_closed":                n,
        "win_rate":                wins / n,
        "avg_net_bps":             net_sum / n,
        "max_hold_pct":            max_hold / n,
        "directional_accuracy_1m": (dir_hits / len(dir_rows)) if dir_rows else 0.0,
    }


def _scalp_readiness(stats: dict, *, min_obs, min_wr, min_net,
                     max_hold, min_dir) -> dict:
    checks = []
    if stats["n_closed"] < min_obs:
        checks.append(f"n_closed {stats['n_closed']} < {min_obs}")
    if stats["win_rate"] < min_wr:
        checks.append(f"win_rate {stats['win_rate']:.3f} < {min_wr}")
    if stats["avg_net_bps"] < min_net:
        checks.append(f"avg_net_bps {stats['avg_net_bps']:.2f} < {min_net}")
    if stats["max_hold_pct"] > max_hold:
        checks.append(f"max_hold_pct {stats['max_hold_pct']:.3f} > {max_hold}")
    if stats["directional_accuracy_1m"] < min_dir:
        checks.append(f"directional_acc_1m {stats['directional_accuracy_1m']:.3f} < {min_dir}")
    return {"ready": not checks, "reasons_failing": checks, "stats": stats}


def get_scalp_activation_readiness() -> dict:
    """v1 readiness — observation→live gate vs the SCALP_*_FOR_LIVE thresholds."""
    from config import settings
    return _scalp_readiness(
        get_scalp_activation_stats(),
        min_obs=settings.SCALP_MIN_OBSERVATIONS_FOR_LIVE,
        min_wr=settings.SCALP_MIN_WIN_RATE_FOR_LIVE,
        min_net=settings.SCALP_MIN_AVG_NET_BPS_FOR_LIVE,
        max_hold=settings.SCALP_MAX_HOLD_EXIT_PCT,
        min_dir=settings.SCALP_MIN_DIRECTIONAL_ACC_1M,
    )


def get_scalp_activation_readiness_v2() -> dict:
    """v2 readiness — tighter thresholds (SCALP_*_FOR_LIVE_V2)."""
    from config import settings
    return _scalp_readiness(
        get_scalp_activation_stats(),
        min_obs=settings.SCALP_MIN_OBSERVATIONS_FOR_LIVE_V2,
        min_wr=settings.SCALP_MIN_WIN_RATE_FOR_LIVE_V2,
        min_net=settings.SCALP_MIN_AVG_NET_BPS_FOR_LIVE_V2,
        max_hold=settings.SCALP_MAX_HOLD_EXIT_PCT_V2,
        min_dir=settings.SCALP_MIN_DIRECTIONAL_ACC_1M_V2,
    )
