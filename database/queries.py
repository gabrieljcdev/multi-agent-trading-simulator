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
    FundingArbObservationModel,
    XChainObservation,
    OpportunityCore, OpportunityObservation,
    OpportunityLiquidationDetail, OpportunityFundingDetail,
    OpportunityLpDetail, OpportunityLaunchDetail,
    WalletFlowEvent, WalletWatchlist, DiscoveryCandidate, ExchangeLabel,
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


def get_true_pnl(days: int = 7) -> dict:
    """Gross arb P&L netted against the BalanceAgent's rebalance cost.

    Returns:
        {
          "gross_arb_pnl": float,  # SUM(arb_trades.net_pnl_usd) over window
          "rebalance_cost": float, # SUM(capital_movements fee) over same window,
                                   #   completed + in_transit only (failed excluded)
          "net_pnl":        float, # gross_arb_pnl − rebalance_cost
          "movements":      int,   # count of fee-bearing movements in window
        }

    Raw rows are NOT mutated — this aggregation is derived at read time.
    The rebalance cost is per-movement: sim rows charge
    SIM_WITHDRAWAL_FEE_USD (what the SimTransferRail actually deducted);
    live rows charge 0 because the live rail doesn't yet persist per-row
    network fees (TODO: wire once live transfers carry a fee_usd column).

    This is the single source of truth for "is arb profitable AFTER moving
    capital around" — which is the metric the upcoming soak needs.
    """
    from config import settings as _s
    from .models import CapitalMovement

    since = datetime.utcnow() - timedelta(days=days)
    sim_fee = float(getattr(_s, "SIM_WITHDRAWAL_FEE_USD", 0.0) or 0.0)

    with get_session() as s:
        # Gross arb P&L — successful arbs with a recorded net_pnl.
        gross_rows = (
            s.query(ArbTrade.net_pnl_usd)
            .filter(ArbTrade.timestamp >= since,
                    ArbTrade.success == True,   # noqa: E712
                    ArbTrade.net_pnl_usd.isnot(None))
            .all()
        )
        gross = float(sum((r[0] or 0.0) for r in gross_rows))

        # Movements that incurred a real cost — completed + in_transit;
        # failed rows are excluded because they were rolled back and the
        # rail refunds the fee. Sim and live rows are counted; cost
        # mapping depends on mode.
        mvts = (
            s.query(CapitalMovement.mode)
            .filter(CapitalMovement.timestamp >= since,
                    CapitalMovement.state.in_(("completed", "in_transit")))
            .all()
        )
        sim_count  = sum(1 for (m,) in mvts if (m or "sim") == "sim")
        cost = sim_count * sim_fee
        n_total = len(mvts)

    return {
        "gross_arb_pnl":  round(gross, 2),
        "rebalance_cost": round(cost,  2),
        "net_pnl":        round(gross - cost, 2),
        "movements":      n_total,
    }


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


# ── Funding-rate arb observations (Phase 1 — observation mode) ─────────────

def save_funding_observations(rows: list) -> None:
    """Upsert a batch of funding-rate arb observations.

    Each row is a dict (NOT a dataclass — that lets the agent build rows
    inline without a wrapper class). Natural key is (symbol, timestamp)
    plus variant so the same opportunity can reappear with exit fields.
    First write creates the row; later writes (when the position closes)
    overwrite when exit_time>0 so the close-time row carries the final
    funding_collected / pnl / exit_reason.

    Synchronous — matches save_scalp_observations. DB failures raise; the
    agent wraps with asyncio.to_thread + try/except so a hiccup never
    halts the loop.
    """
    if not rows:
        return
    allowed = {
        "timestamp", "symbol", "variant", "venue_long", "venue_short",
        "funding_apr", "spread_apr", "oi_usd", "depth_ok",
        "notional_usd", "margin_used", "basis_at_entry",
        "projected_funding_per_interval", "projected_fees", "projected_net_apr",
        "would_enter", "skip_reason",
        "exit_time", "exit_reason", "hold_sec",
        "funding_collected", "fees_paid", "pnl_usd",
        "observation_only",
        # Funding-frontier columns (Phase 2-4):
        "legs", "funding_interval_sec", "taker_fee_bps", "maker_fee_bps",
        "is_long_tail", "is_hip3", "pair_age_days",
        "spread_decay_bps_per_day", "oi_growth_pct_24h", "crowding_verdict",
    }
    with get_session() as s:
        for r in rows:
            payload = {k: v for k, v in r.items() if k in allowed}
            existing = (
                s.query(FundingArbObservationModel)
                .filter_by(
                    symbol=payload.get("symbol"),
                    timestamp=payload.get("timestamp"),
                    variant=payload.get("variant"),
                )
                .first()
            )
            if existing is None:
                s.add(FundingArbObservationModel(**payload))
            else:
                # Overwrite when exit data has arrived — entry rows are
                # already persisted; later writes carry the close-time fields.
                if (payload.get("exit_time") or 0) > 0:
                    for k, v in payload.items():
                        setattr(existing, k, v)


def get_funding_summary(days: int = 7) -> dict:
    """Aggregate metrics across the last `days` of funding observations.

    Keys: total, would_enter, closed, mean_net_apr_realized, mean_hold_hours,
    exit_reason breakdown. Closed = a would_enter row whose exit_time>0.
    Returns zeros when the table is empty so callers don't carry the
    empty-case dance.
    """
    import time as _time
    cutoff = _time.time() - days * 86400
    out = {
        "total":                 0,
        "would_enter":           0,
        "closed":                0,
        "mean_net_apr_realized": 0.0,
        "mean_hold_hours":       0.0,
        "exit_reason":           {},
        # ── Funding-frontier rollups ──────────────────────────────────
        "n_cross_venue":         0,
        "n_long_tail":           0,
        "n_hip3":                0,
        "pct_crowding_OPEN":         0.0,
        "pct_crowding_COMPRESSING":  0.0,
        "pct_crowding_CROWDED":      0.0,
        "pct_crowding_UNKNOWN":      0.0,
        "best_projected_net_apr_open": 0.0,
        "best_projected_net_apr":      0.0,
    }
    with get_session() as s:
        rows = (
            s.query(FundingArbObservationModel)
            .filter(FundingArbObservationModel.timestamp >= cutoff)
            .all()
        )
    if not rows:
        return out

    out["total"] = len(rows)
    entries = [r for r in rows if r.would_enter]
    out["would_enter"] = len(entries)
    closed = [r for r in entries if (r.exit_time or 0) > 0]
    out["closed"] = len(closed)

    if closed:
        # Realised net APR: annualise (pnl / notional) by elapsed time. Fall
        # back to projected_net_apr when notional/hold are zero (defensive).
        apr_sum  = 0.0
        hold_sum = 0.0
        for r in closed:
            hold_sec = float(r.hold_sec or 0.0)
            hold_sum += hold_sec
            notional = float(r.notional_usd or 0.0)
            if notional > 0 and hold_sec > 0:
                yearly = (float(r.pnl_usd or 0.0) / notional) * (365.0 * 86400.0 / hold_sec)
                apr_sum += yearly
            else:
                apr_sum += float(r.projected_net_apr or 0.0)
        out["mean_net_apr_realized"] = apr_sum / len(closed)
        out["mean_hold_hours"]       = (hold_sum / len(closed)) / 3600.0

        from collections import Counter
        reasons = Counter(
            (r.exit_reason or "unknown") for r in closed
        )
        out["exit_reason"] = dict(reasons)

    # ── Funding-frontier rollups ──────────────────────────────────────────
    out["n_cross_venue"] = sum(1 for r in rows if (r.legs or "single") == "cross_venue")
    out["n_long_tail"]   = sum(1 for r in rows if bool(r.is_long_tail))
    out["n_hip3"]        = sum(1 for r in rows if r.is_hip3 is True)

    # Crowding mix — % across rows that carry a verdict (UNKNOWN included).
    verdicts = [r.crowding_verdict for r in rows if r.crowding_verdict]
    if verdicts:
        from collections import Counter as _Counter
        vc = _Counter(verdicts)
        n_v = len(verdicts)
        for label in ("OPEN", "COMPRESSING", "CROWDED", "UNKNOWN"):
            out[f"pct_crowding_{label}"] = vc.get(label, 0) / n_v * 100.0

    # Gate-relevant headline: best net APR among OPEN-verdict pairs only.
    open_aprs = [
        float(r.projected_net_apr or 0.0)
        for r in rows
        if r.crowding_verdict == "OPEN"
    ]
    if open_aprs:
        out["best_projected_net_apr_open"] = max(open_aprs)

    all_aprs = [r.projected_net_apr for r in rows if r.projected_net_apr is not None]
    if all_aprs:
        out["best_projected_net_apr"] = max(float(a) for a in all_aprs)

    return out


def get_funding_observations(
    symbol: Optional[str] = None,
    would_enter_only: bool = False,
    limit: int = 500,
) -> list:
    """Newest-first FundingArbObservationModel rows as plain dicts.
    Powers the dashboard panel's "live opportunities" + "recent closed"
    sections without keeping ORM rows alive past the session."""
    with get_session() as s:
        q = s.query(FundingArbObservationModel)
        if symbol:
            q = q.filter(FundingArbObservationModel.symbol == symbol)
        if would_enter_only:
            q = q.filter(FundingArbObservationModel.would_enter.is_(True))
        rows = (
            q.order_by(desc(FundingArbObservationModel.timestamp))
            .limit(limit)
            .all()
        )
    return [
        {
            "id":           r.id,
            "timestamp":    r.timestamp,
            "symbol":       r.symbol,
            "variant":      r.variant,
            "venue_long":   r.venue_long,
            "venue_short":  r.venue_short,
            "funding_apr":  r.funding_apr,
            "spread_apr":   r.spread_apr,
            "oi_usd":       r.oi_usd,
            "depth_ok":     r.depth_ok,
            "notional_usd": r.notional_usd,
            "margin_used":  r.margin_used,
            "basis_at_entry":  r.basis_at_entry,
            "projected_funding_per_interval": r.projected_funding_per_interval,
            "projected_fees":     r.projected_fees,
            "projected_net_apr":  r.projected_net_apr,
            "would_enter":     r.would_enter,
            "skip_reason":     r.skip_reason,
            "exit_time":       r.exit_time,
            "exit_reason":     r.exit_reason,
            "hold_sec":        r.hold_sec,
            "funding_collected": r.funding_collected,
            "fees_paid":         r.fees_paid,
            "pnl_usd":           r.pnl_usd,
            "observation_only":  r.observation_only,
            "legs":                     r.legs,
            "funding_interval_sec":     r.funding_interval_sec,
            "taker_fee_bps":            r.taker_fee_bps,
            "maker_fee_bps":            r.maker_fee_bps,
            "is_long_tail":             r.is_long_tail,
            "is_hip3":                  r.is_hip3,
            "pair_age_days":            r.pair_age_days,
            "spread_decay_bps_per_day": r.spread_decay_bps_per_day,
            "oi_growth_pct_24h":        r.oi_growth_pct_24h,
            "crowding_verdict":         r.crowding_verdict,
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


# ── Web control panel v2 — bankroll, agent pages, session pages ─────────────
# Faithful to prompts/build_web_ui_refresh_v1.md. Funds stay partitioned to
# avoid double-counting: signal + arb are the real-money ledgers (Trade
# non-scalp / ArbTrade); scalp's authoritative record is scalp_observations.
# Executed scalp also writes Trade rows with strategy="scalp", so every query
# below reads each fund from exactly one place.

def get_alltime_realised_pnl() -> float:
    """Sum of net P&L from all closed trades across all funds, all-time.
    Signal + executed-scalp fills live in the Trade ledger (pnl_usd); arb in
    ArbTrade (net_pnl_usd). Never date-scoped — this drives the persistent
    bankroll, so crossing UTC midnight leaves it unchanged."""
    total = 0.0
    with get_session() as s:
        for (pnl,) in (
            s.query(Trade.pnl_usd).filter(Trade.pnl_usd.isnot(None)).all()
        ):
            total += float(pnl or 0.0)
        for (pnl,) in (
            s.query(ArbTrade.net_pnl_usd)
            .filter(ArbTrade.net_pnl_usd.isnot(None)).all()
        ):
            total += float(pnl or 0.0)
    return round(total, 2)


def get_daily_fees() -> float:
    """Total fees paid today (UTC) across funds, in USD. Signal fees from
    Trade.fees_usd (closed today, non-scalp); arb fees as gross-net on today's
    ArbTrade rows; scalp round-trip costs as round_trip_cost_bps converted to
    USD over SCALP_POSITION_SIZE_USD on today's closed observations. Scalp is
    excluded from the Trade sum so executed scalp isn't counted twice."""
    from config import settings
    today = datetime.utcnow().date()
    total = 0.0
    with get_session() as s:
        for (f,) in (
            s.query(Trade.fees_usd)
            .filter(func.date(Trade.timestamp_close) == today,
                    Trade.fees_usd.isnot(None),
                    (Trade.strategy != "scalp") | (Trade.strategy.is_(None)))
            .all()
        ):
            total += float(f or 0.0)
        for gross, net in (
            s.query(ArbTrade.gross_pnl_usd, ArbTrade.net_pnl_usd)
            .filter(func.date(ArbTrade.timestamp) == today,
                    ArbTrade.net_pnl_usd.isnot(None))
            .all()
        ):
            total += float(gross or 0.0) - float(net or 0.0)
        size = float(getattr(settings, "SCALP_POSITION_SIZE_USD", 0.0) or 0.0)
        for (rt,) in (
            s.query(ScalpObservationModel.round_trip_cost_bps)
            .filter(ScalpObservationModel.would_entry == True,   # noqa: E712
                    ScalpObservationModel.exit_price > 0,
                    func.date(ScalpObservationModel.created_at) == today)
            .all()
        ):
            total += float(rt or 0.0) / 10000.0 * size
    return round(total, 2)


def _scalp_obs_to_dict(r) -> dict:
    """One closed scalp observation as a plain dict. pnl_bps is GROSS; net is
    gross minus round_trip_cost_bps. Keeps the raw pnl_bps for win-rate checks
    and exposes HH:MM:SS close time (exit_time, falling back to entry)."""
    gross = float(r.pnl_bps or 0.0)
    fee   = float(r.round_trip_cost_bps or 0.0)
    exit_ts = r.exit_time if (r.exit_time or 0) > 0 else r.timestamp
    try:
        closed_at = (datetime.utcfromtimestamp(exit_ts).strftime("%H:%M:%S")
                     if exit_ts else "—")
    except Exception:
        closed_at = "—"
    return {
        "symbol":      r.symbol,
        "exchange":    r.exchange,
        "direction":   r.direction or "—",
        "entry_price": float(r.entry_price or 0.0),
        "exit_price":  float(r.exit_price or 0.0),
        "exit_reason": r.exit_reason or "—",
        "gross_bps":   round(gross, 2),
        "net_bps":     round(gross - fee, 2),
        "fees_bps":    round(fee, 2),
        "hold_sec":    int(r.hold_sec or 0),
        "pnl_bps":     gross,                       # raw, for win-rate checks
        "pnl_usd":     float(r.pnl_usd or 0.0),
        "outcome":     "WIN" if gross > 0 else "LOSS",
        "closed_at":   closed_at,
        "ts":          closed_at,
    }


def get_scalp_closed_today() -> list[dict]:
    """Today's (UTC) closed scalp observations (would_entry=1, exit_price>0).
    Drives the scalp win-rate fix. created_at dates the row (≈ entry time),
    matching get_scalp_realized_pnl(today=True)'s daily-reset semantics."""
    today = datetime.utcnow().date()
    with get_session() as s:
        rows = (
            s.query(ScalpObservationModel)
            .filter(ScalpObservationModel.would_entry == True,   # noqa: E712
                    ScalpObservationModel.exit_price > 0,
                    func.date(ScalpObservationModel.created_at) == today)
            .order_by(desc(ScalpObservationModel.timestamp))
            .all()
        )
    return [_scalp_obs_to_dict(r) for r in rows]


def get_scalp_trade_history(limit: int = 100) -> list[dict]:
    """Closed scalp observations newest first — for the scalp agent page trade
    log and the scalp feed's persisted closed-trades list."""
    with get_session() as s:
        rows = (
            s.query(ScalpObservationModel)
            .filter(ScalpObservationModel.would_entry == True,   # noqa: E712
                    ScalpObservationModel.exit_price > 0)
            .order_by(desc(ScalpObservationModel.timestamp))
            .limit(limit)
            .all()
        )
    return [_scalp_obs_to_dict(r) for r in rows]


def _trade_to_history_dict(t) -> dict:
    closed = t.timestamp_close
    opened = t.timestamp_open
    side = (t.side or "").upper()
    pnl = float(t.pnl_usd or 0.0)
    return {
        "id":          t.id,
        "ts":          (closed or opened).strftime("%H:%M:%S") if (closed or opened) else "—",
        "pair":        t.pair,
        "track":       t.signal_type or "—",
        "direction":   side or "—",
        "entry_price": float(t.entry_price or 0.0),
        "exit_price":  float(t.exit_price) if t.exit_price is not None else None,
        "stop_loss":   t.stop_loss,
        "take_profit": t.take_profit,
        "size_usd":    float(t.size_usd or 0.0),
        "pnl_usd":     round(pnl, 2),
        "pnl_pct":     round(float(t.pnl_pct or 0.0), 2),
        "fees_usd":    round(float(t.fees_usd or 0.0), 2),
        "exit_reason": t.exit_reason or "—",
        "outcome":     "WIN" if pnl > 0 else "LOSS",
    }


def get_signal_trade_history(limit: int = 100) -> list[dict]:
    """Closed signal-fund trades newest first (Trade ledger, scalp excluded).
    Schema matches existing trades-table fields; for the signal agent page."""
    with get_session() as s:
        rows = (
            s.query(Trade)
            .filter(Trade.timestamp_close.isnot(None),
                    (Trade.strategy != "scalp") | (Trade.strategy.is_(None)))
            .order_by(desc(Trade.timestamp_close))
            .limit(limit)
            .all()
        )
        return [_trade_to_history_dict(t) for t in rows]


def get_postmortems_by_agent(agent_id: str, n: int = 3) -> list[dict]:
    """Last n Claude postmortems for an agent. Postmortems are stored on
    Trade.claude_postmortem (there is no separate postmortems table), so we
    filter by the trade's fund: 'signal' = non-scalp trades, 'scalp' = scalp
    trades. Arb and the placeholder agents have none → empty list."""
    if agent_id == "signal":
        strat_filter = ((Trade.strategy != "scalp") | (Trade.strategy.is_(None)))
    elif agent_id == "scalp":
        strat_filter = (Trade.strategy == "scalp")
    else:
        return []
    out: list[dict] = []
    with get_session() as s:
        rows = (
            s.query(Trade)
            .filter(Trade.claude_postmortem.isnot(None), strat_filter)
            .order_by(desc(Trade.timestamp_close))
            .limit(n)
            .all()
        )
        for t in rows:
            ts = t.timestamp_close or t.timestamp_open
            out.append({
                "ts":          ts.strftime("%H:%M") if ts else "—",
                "trade_range": f"trade {t.id}",
                "body":        t.claude_postmortem or "",
                "tags":        [],
                "agent_id":    agent_id,
            })
    return out


def get_closed_trades_by_session(session: str, date: str = "today") -> list[dict]:
    """Closed trades for the given UTC trading session today, across every
    fund. Unions signal trades (Trade, non-scalp, by close time), arb fills
    (ArbTrade, by execution time) and closed scalp observations
    (scalp_observations, by created_at). Newest first. `date` is accepted for
    forward-compat; only 'today' is implemented."""
    session = (session or "").upper()
    if session not in _SESSIONS:
        return []
    from config import settings
    today = datetime.utcnow().date()
    scalp_size = float(getattr(settings, "SCALP_POSITION_SIZE_USD", 0.0) or 0.0)
    rows: list[dict] = []
    with get_session() as s:
        for t in (
            s.query(Trade)
            .filter(func.date(Trade.timestamp_close) == today,
                    Trade.pnl_usd.isnot(None),
                    (Trade.strategy != "scalp") | (Trade.strategy.is_(None)))
            .all()
        ):
            ts = t.timestamp_close or t.timestamp_open
            if not ts or _session_for_hour(ts.hour) != session:
                continue
            pnl = float(t.pnl_usd or 0.0)
            rows.append({
                "ts":          ts.strftime("%H:%M:%S"), "_sort": ts,
                "agent":       "signal",
                "pair":        t.pair,
                "direction":   (t.side or "—").upper(),
                "entry_price": float(t.entry_price or 0.0),
                "exit_price":  float(t.exit_price or 0.0) if t.exit_price is not None else 0.0,
                "size_usd":    float(t.size_usd or 0.0),
                "pnl_usd":     round(pnl, 2),
                "outcome":     "WIN" if pnl > 0 else "LOSS",
            })
        for r in (
            s.query(ArbTrade)
            .filter(func.date(ArbTrade.timestamp) == today,
                    ArbTrade.net_pnl_usd.isnot(None))
            .all()
        ):
            ts = r.timestamp
            if not ts or _session_for_hour(ts.hour) != session:
                continue
            pnl = float(r.net_pnl_usd or 0.0)
            rows.append({
                "ts":          ts.strftime("%H:%M:%S"), "_sort": ts,
                "agent":       "arb",
                "pair":        r.symbol,
                "direction":   "ARB",
                "entry_price": float(r.buy_price or 0.0),
                "exit_price":  float(r.sell_price or 0.0),
                "size_usd":    float(r.size_usd or 0.0),
                "pnl_usd":     round(pnl, 2),
                "outcome":     "WIN" if pnl > 0 else "LOSS",
            })
        for o in (
            s.query(ScalpObservationModel)
            .filter(ScalpObservationModel.would_entry == True,   # noqa: E712
                    ScalpObservationModel.exit_price > 0,
                    func.date(ScalpObservationModel.created_at) == today)
            .all()
        ):
            ts = o.created_at
            if not ts or _session_for_hour(ts.hour) != session:
                continue
            pnl = float(o.pnl_usd or 0.0)
            rows.append({
                "ts":          ts.strftime("%H:%M:%S"), "_sort": ts,
                "agent":       "scalp",
                "pair":        o.symbol,
                "direction":   o.direction or "—",
                "entry_price": float(o.entry_price or 0.0),
                "exit_price":  float(o.exit_price or 0.0),
                "size_usd":    scalp_size,
                "pnl_usd":     round(pnl, 2),
                "outcome":     "WIN" if (o.pnl_bps or 0) > 0 else "LOSS",
            })
    rows.sort(key=lambda d: d["_sort"] or datetime.min, reverse=True)
    for d in rows:
        d.pop("_sort", None)
    return rows


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


# ── Cross-chain arb (observation logs) ──────────────────────

def insert_xchain_observation(
    *,
    symbol:            str,
    buy_chain:         str,
    sell_chain:        str,
    buy_venue:         str,
    sell_venue:        str,
    notional_usd:      float,
    spread_bps:        float,
    rt_fee_bps:        float,
    gas_bps:           float,
    slip_bps:          float,
    bridge_bps:        float,
    net_edge_bps:      float,
    gas_breakeven_usd: float,
    would_entry:       bool,
    skip_reason:       str = "",
    observation_only:  bool = True,
) -> int:
    """Persist one cross-chain arb evaluation and return its new row id.

    Mirrors log_arb_trade in shape: one row per evaluation, would_entry
    distinguishes the candidates that cleared every gate from the skips.
    Synchronous on purpose — matches every other query helper; the engine
    can call this from asyncio.to_thread if it wants.
    """
    with get_session() as s:
        row = XChainObservation(
            symbol=symbol,
            buy_chain=buy_chain,
            sell_chain=sell_chain,
            buy_venue=buy_venue,
            sell_venue=sell_venue,
            notional_usd=notional_usd,
            spread_bps=spread_bps,
            rt_fee_bps=rt_fee_bps,
            gas_bps=gas_bps,
            slip_bps=slip_bps,
            bridge_bps=bridge_bps,
            net_edge_bps=net_edge_bps,
            gas_breakeven_usd=gas_breakeven_usd,
            would_entry=would_entry,
            skip_reason=skip_reason,
            observation_only=observation_only,
        )
        s.add(row)
        s.flush()
        return row.id


def get_xchain_summary(days: int = 7) -> dict:
    """Aggregate metrics across the last `days` of cross-chain observations.

    Keys: n_obs, n_would_entry, mean_net_edge_bps, median_net_edge_bps,
    best_pair (str like "arbitrum→base"), pct_blocked_by_gas,
    pct_blocked_by_min_edge. Returns zeros when the table is empty so
    callers don't carry the empty-case dance.
    """
    since = datetime.utcnow() - timedelta(days=days)
    out = {
        "n_obs":                   0,
        "n_would_entry":           0,
        "mean_net_edge_bps":       0.0,
        "median_net_edge_bps":     0.0,
        "best_pair":               None,
        "pct_blocked_by_gas":      0.0,
        "pct_blocked_by_min_edge": 0.0,
    }
    with get_session() as s:
        rows = (
            s.query(XChainObservation)
            .filter(XChainObservation.timestamp >= since)
            .all()
        )
    if not rows:
        return out

    out["n_obs"] = len(rows)
    entries = [r for r in rows if r.would_entry]
    out["n_would_entry"] = len(entries)

    edges = [r.net_edge_bps for r in rows if r.net_edge_bps is not None]
    if edges:
        out["mean_net_edge_bps"] = sum(edges) / len(edges)
        srt = sorted(edges)
        mid = len(srt) // 2
        out["median_net_edge_bps"] = (
            srt[mid] if len(srt) % 2 == 1 else (srt[mid - 1] + srt[mid]) / 2
        )

    # best_pair: directional buy→sell with the highest mean net edge,
    # computed only over rows that cleared the would_entry gate so the
    # pair ranking reflects realised opportunity not raw signal.
    from collections import defaultdict
    pair_acc: dict[str, list[float]] = defaultdict(list)
    for r in entries:
        if r.net_edge_bps is None:
            continue
        pair_acc[f"{r.buy_chain}→{r.sell_chain}"].append(r.net_edge_bps)
    if pair_acc:
        out["best_pair"] = max(
            pair_acc.items(), key=lambda kv: sum(kv[1]) / len(kv[1]),
        )[0]

    # Skip-reason breakdown — the two failure modes the operator most needs
    # to see. Anything with "gas" in the reason is gas-blocked; anything
    # mentioning the min-edge gate is min-edge-blocked.
    n = len(rows)
    gas_blocked = sum(
        1 for r in rows
        if r.skip_reason and "gas" in r.skip_reason.lower()
    )
    edge_blocked = sum(
        1 for r in rows
        if r.skip_reason and "min_edge" in r.skip_reason.lower()
    )
    out["pct_blocked_by_gas"]      = gas_blocked  / n * 100.0
    out["pct_blocked_by_min_edge"] = edge_blocked / n * 100.0
    return out


def get_xchain_observations(
    symbol: Optional[str] = None,
    would_entry_only: bool = False,
    limit: int = 500,
) -> list:
    """Newest-first XChainObservation rows as plain dicts. Used by the
    crosschain agent's inventory-target weighting + the dashboard."""
    with get_session() as s:
        q = s.query(XChainObservation)
        if symbol:
            q = q.filter(XChainObservation.symbol == symbol)
        if would_entry_only:
            q = q.filter(XChainObservation.would_entry.is_(True))
        rows = (
            q.order_by(desc(XChainObservation.timestamp))
            .limit(limit)
            .all()
        )
    return [
        {
            "id": r.id, "timestamp": r.timestamp,
            "symbol": r.symbol,
            "buy_chain": r.buy_chain, "sell_chain": r.sell_chain,
            "buy_venue": r.buy_venue, "sell_venue": r.sell_venue,
            "notional_usd": r.notional_usd,
            "spread_bps": r.spread_bps,
            "rt_fee_bps": r.rt_fee_bps,
            "gas_bps": r.gas_bps,
            "slip_bps": r.slip_bps,
            "bridge_bps": r.bridge_bps,
            "net_edge_bps": r.net_edge_bps,
            "gas_breakeven_usd": r.gas_breakeven_usd,
            "would_entry": r.would_entry,
            "skip_reason": r.skip_reason,
            "observation_only": r.observation_only,
        }
        for r in rows
    ]


# ── BalanceAgent: capital movements (audit + state machine) ─────────────────

def log_capital_movement(data: dict) -> int:
    """Insert a capital_movements row and return its id.

    Required keys: from_fund, to_fund, amount_usd.
    Optional: from_exchange, to_exchange, asset, mode, state, initiated_by,
    note, rail_id, network, error.
    """
    from .models import CapitalMovement
    with get_session() as s:
        row = CapitalMovement(**data)
        s.add(row)
        s.flush()
        return row.id


def update_capital_movement(movement_id: int, fields: dict) -> None:
    """Patch a capital_movements row in place. Unknown keys are ignored.
    Use for state transitions (pending → in_transit → completed/failed)
    and for backfilling transfer_tx_hash / error / completed_at.
    """
    from .models import CapitalMovement
    allowed = {"state", "transfer_tx_hash", "error", "note",
               "completed_at", "rail_id", "network"}
    with get_session() as s:
        row = s.get(CapitalMovement, movement_id)
        if row is None:
            return
        for k, v in fields.items():
            if k in allowed and v is not None:
                setattr(row, k, v)


def get_in_transit_movements() -> list:
    """All capital_movements rows in `pending` or `in_transit` state.
    Loaded on startup by CexTransferRail for reconciliation against ccxt.
    """
    from .models import CapitalMovement
    with get_session() as s:
        return (
            s.query(CapitalMovement)
            .filter(CapitalMovement.state.in_(("pending", "in_transit")))
            .order_by(CapitalMovement.timestamp)
            .all()
        )


def get_capital_movements_today() -> list:
    """All capital_movements rows from today (UTC), newest first.
    Backs the dashboard's "today's transfer count/fees" panel."""
    from .models import CapitalMovement
    today = datetime.utcnow().date()
    with get_session() as s:
        return (
            s.query(CapitalMovement)
            .filter(func.date(CapitalMovement.timestamp) == today)
            .order_by(desc(CapitalMovement.timestamp))
            .all()
        )


def get_capital_movement_history(hours: int = 168) -> list:
    """Movements in the lookback window — chronological, oldest first.
    7-day window by default, suitable for analysing rebalance cadence.
    """
    from .models import CapitalMovement
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as s:
        return (
            s.query(CapitalMovement)
            .filter(CapitalMovement.timestamp >= since)
            .order_by(CapitalMovement.timestamp)
            .all()
        )


# ── BalanceAgent: fund capital efficiency (policy edge estimates) ───────────

def log_fund_capital_efficiency(data: dict) -> int:
    """Insert a fund_capital_efficiency snapshot. Required: fund. All other
    fields default safely; pass realised_return_usd, deployed_usd, etc."""
    from .models import FundCapitalEfficiency
    with get_session() as s:
        row = FundCapitalEfficiency(**data)
        s.add(row)
        s.flush()
        return row.id


def get_fund_capital_efficiency(fund: str, hours: int = 720) -> list:
    """All efficiency rows for a fund in the lookback window (default 30d).
    Chronological. Used to compute depletion_variance + opportunity_cost
    for the Miller-Orr band, and edge estimates for the growth-optimal
    policy.
    """
    from .models import FundCapitalEfficiency
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as s:
        return (
            s.query(FundCapitalEfficiency)
            .filter(FundCapitalEfficiency.fund == fund,
                    FundCapitalEfficiency.timestamp >= since)
            .order_by(FundCapitalEfficiency.timestamp)
            .all()
        )


def get_latest_fund_efficiency(fund: str):
    """Most recent efficiency row for a fund, or None."""
    from .models import FundCapitalEfficiency
    with get_session() as s:
        return (
            s.query(FundCapitalEfficiency)
            .filter(FundCapitalEfficiency.fund == fund)
            .order_by(desc(FundCapitalEfficiency.timestamp))
            .first()
        )


# ── Web UI v2 — agent-panel snapshots ───────────────────────────────────────
#
# Each function is defensive (try/except returning empty/zero default), each
# returns plain dicts so the snapshot helper never leaks ORM objects to the
# WebSocket layer. The push loop runs every WEB_UI_PUSH_INTERVAL_S so these
# must stay cheap — no joins beyond what's strictly required.

def _today_utc_start():
    """First instant of today's UTC date — used to scope 'today' aggregates."""
    now = datetime.utcnow()
    return datetime(now.year, now.month, now.day)


def get_xchain_today_summary() -> dict:
    """Same shape as get_xchain_summary but scoped to today's UTC rows.

    Returns zeros for every field when no observations have been logged
    today — the panel renders zeros, not "no data". `pct_blocked_by_*`
    are percentages of `n_observations`, not of the blocked subset.
    """
    out = {
        "n_observations":          0,
        "n_would_entry":           0,
        "mean_net_edge_bps":       0.0,
        "median_net_edge_bps":     0.0,
        "pct_blocked_by_gas":      0.0,
        "pct_blocked_by_min_edge": 0.0,
    }
    try:
        since = _today_utc_start()
        with get_session() as s:
            rows = (
                s.query(XChainObservation)
                .filter(XChainObservation.timestamp >= since)
                .all()
            )
    except Exception:
        return out
    if not rows:
        return out

    out["n_observations"] = len(rows)
    out["n_would_entry"]  = sum(1 for r in rows if r.would_entry)

    edges = [r.net_edge_bps for r in rows if r.net_edge_bps is not None]
    if edges:
        out["mean_net_edge_bps"] = sum(edges) / len(edges)
        srt = sorted(edges)
        mid = len(srt) // 2
        out["median_net_edge_bps"] = (
            srt[mid] if len(srt) % 2 == 1 else (srt[mid - 1] + srt[mid]) / 2
        )

    n = len(rows)
    gas_blocked = sum(
        1 for r in rows
        if r.skip_reason and "gas" in r.skip_reason.lower()
    )
    edge_blocked = sum(
        1 for r in rows
        if r.skip_reason and "min_edge" in r.skip_reason.lower()
    )
    out["pct_blocked_by_gas"]      = gas_blocked  / n * 100.0
    out["pct_blocked_by_min_edge"] = edge_blocked / n * 100.0
    return out


def get_funding_today_summary() -> dict:
    """Today's FundingArbObservationModel aggregate for the v2 funding panel.

    `blended_apr_pct` is the conservative SOAK_CRITERIA read:
    `(n_would_enter / n_observations) * mean_projected_apr_pct`. It serves
    as a proxy for `deployed_fraction × in_deployment_apr` without needing
    the coordinator's live deployed state.

    `skip_reasons` is a four-bucket count keyed by case-insensitive string
    matching on the observation row's free-form `skip_reason` column:
      - "gate" / "below_gate" / "min_apr"  → below_gate
      - "basis"                            → basis_unfavourable
      - "depth"                            → depth_thin
      - everything else (including empty)  → other (but only counted when
        the row's would_enter is False; True rows contribute nothing here).
    """
    out = {
        "n_observations":           0,
        "n_would_enter":            0,
        "utilisation_pct":          0.0,
        "mean_projected_apr_pct":   0.0,
        "median_projected_apr_pct": 0.0,
        "blended_apr_pct":          0.0,
        "skip_reasons": {
            "below_gate":         0,
            "basis_unfavourable": 0,
            "depth_thin":         0,
            "other":              0,
        },
    }
    try:
        # FundingArbObservationModel.timestamp is Unix epoch (Float).
        since_ts = _today_utc_start().timestamp()
        with get_session() as s:
            rows = (
                s.query(FundingArbObservationModel)
                .filter(FundingArbObservationModel.timestamp >= since_ts)
                .all()
            )
    except Exception:
        return out
    if not rows:
        return out

    out["n_observations"] = len(rows)
    enter_rows = [r for r in rows if r.would_enter]
    out["n_would_enter"]  = len(enter_rows)
    if out["n_observations"]:
        out["utilisation_pct"] = (
            out["n_would_enter"] / out["n_observations"] * 100.0
        )

    if enter_rows:
        aprs = [float(r.projected_net_apr or 0.0) * 100.0 for r in enter_rows]
        out["mean_projected_apr_pct"] = sum(aprs) / len(aprs)
        srt = sorted(aprs)
        mid = len(srt) // 2
        out["median_projected_apr_pct"] = (
            srt[mid] if len(srt) % 2 == 1 else (srt[mid - 1] + srt[mid]) / 2
        )
        out["blended_apr_pct"] = (
            out["mean_projected_apr_pct"]
            * (out["n_would_enter"] / out["n_observations"])
        )

    # Skip-reason taxonomy: count only on rows that did NOT enter — the
    # operator wants to see WHY entries were rejected, not why winners won.
    for r in rows:
        if r.would_enter:
            continue
        reason = (r.skip_reason or "").lower()
        if any(tok in reason for tok in ("gate", "below_gate", "min_apr")):
            out["skip_reasons"]["below_gate"] += 1
        elif "basis" in reason:
            out["skip_reasons"]["basis_unfavourable"] += 1
        elif "depth" in reason:
            out["skip_reasons"]["depth_thin"] += 1
        else:
            out["skip_reasons"]["other"] += 1
    return out


def _capital_movement_to_dict(r) -> dict:
    """Stable dict shape the web layer renders. Used by both
    get_capital_movements_recent and get_capital_movements_in_transit.
    ts shows time-only for today's rows, date+time for older ones."""
    ts = r.timestamp
    if ts is None:
        ts_str = "—"
    elif ts.date() == datetime.utcnow().date():
        ts_str = ts.strftime("%H:%M:%S")
    else:
        ts_str = ts.strftime("%m-%d %H:%M")
    return {
        "id":            r.id,
        "ts":            ts_str,
        "from_fund":     r.from_fund,
        "to_fund":       r.to_fund,
        "from_exchange": r.from_exchange,
        "to_exchange":   r.to_exchange,
        "asset":         r.asset or "USDT",
        "amount_usd":    float(r.amount_usd or 0.0),
        "mode":          r.mode or "sim",
        "state":         r.state or "pending",
        "initiated_by":  r.initiated_by or "",
        "note":          r.note or "",
        "error":         r.error,
    }


def get_capital_movements_recent(limit: int = 50) -> list[dict]:
    """Newest-first capital_movements rows as plain dicts. Used by the
    Balance panel's in-transit + recent-history surfaces."""
    from .models import CapitalMovement
    try:
        with get_session() as s:
            rows = (
                s.query(CapitalMovement)
                .order_by(desc(CapitalMovement.timestamp))
                .limit(limit)
                .all()
            )
        return [_capital_movement_to_dict(r) for r in rows]
    except Exception:
        return []


def get_capital_movements_in_transit() -> list[dict]:
    """Subset of capital_movements with state in ('pending', 'in_transit'),
    newest first. Same dict shape as get_capital_movements_recent."""
    from .models import CapitalMovement
    try:
        with get_session() as s:
            rows = (
                s.query(CapitalMovement)
                .filter(CapitalMovement.state.in_(("pending", "in_transit")))
                .order_by(desc(CapitalMovement.timestamp))
                .all()
            )
        return [_capital_movement_to_dict(r) for r in rows]
    except Exception:
        return []


def get_fund_efficiency_summary(window_hours: int = 24) -> list[dict]:
    """One dict per fund summarising the efficiency rows in the window.

    `deployed_usd` and `return_on_deployed_pct` come from the most recent
    row in the window (latest snapshot). `realised_return_usd` is the SUM
    of returns logged in the window (so a 24h window approximates the
    fund's realised daily P&L). `starvation_events` counts rows where
    `starvation_event=True`.
    """
    from .models import FundCapitalEfficiency
    out: dict[str, dict] = {}
    try:
        since = datetime.utcnow() - timedelta(hours=int(window_hours))
        with get_session() as s:
            rows = (
                s.query(FundCapitalEfficiency)
                .filter(FundCapitalEfficiency.timestamp >= since)
                .order_by(FundCapitalEfficiency.timestamp)
                .all()
            )
    except Exception:
        return []
    for r in rows:
        fund = r.fund or "unknown"
        bucket = out.setdefault(fund, {
            "fund":                   fund,
            "deployed_usd":           0.0,
            "realised_return_usd":    0.0,
            "return_on_deployed_pct": 0.0,
            "starvation_events":      0,
        })
        # Latest deployed + return_on_deployed_pct (rows are ascending by ts).
        bucket["deployed_usd"]           = float(r.deployed_usd or 0.0)
        bucket["return_on_deployed_pct"] = float(r.return_on_deployed_pct or 0.0)
        bucket["realised_return_usd"]   += float(r.realised_return_usd or 0.0)
        if r.starvation_event:
            bucket["starvation_events"] += 1
    return list(out.values())


# ── OpportunityScannerAgent (observation mode — PROTOCOL_OPPORTUNITIES) ────

# Mutable core fields a re-detection / re-measurement may update. first_seen
# and created_at are DELIBERATELY absent — first_seen is immutable.
_OPPORTUNITY_CORE_MUTABLE = {
    "asset_class", "detector_id",
    "competitor_count", "competitor_trend", "competitor_count_history",
    "last_competition_check",
    "edge_annualized_pct", "edge_confidence",
    "risk_status", "risk_flags",
    "reachability_verdict", "shark_constraints", "window_status",
    "unconventional_score", "unconventional_factors", "unconventional_rationale",
    "as_of",
}

_OPPORTUNITY_DETAIL_MODELS = {
    "liquidation": OpportunityLiquidationDetail,
    "funding":     OpportunityFundingDetail,
    "lp":          OpportunityLpDetail,
    "launch":      OpportunityLaunchDetail,
}

# Standard-view ordering (mirrors agents/opportunities/ranker.py — kept
# local because queries.py cannot import the agents package without a
# circular import; the ranker's tests pin both stay in sync).
_TREND_RANK        = {"zero": 0, "rising_slow": 1, "rising_fast": 2, "saturated": 3}
_REACHABILITY_RANK = {"reachable": 0, "unknown": 1, "unreachable": 2}


def upsert_opportunity_core(row: dict) -> int:
    """Insert an opportunity_core row on first sight; UPDATE mutable
    fields on re-detection. Never touches first_seen or created_at after
    the first insert (first_seen immutability is load-bearing for the
    hypothesis log). Returns the core row id.

    Natural key: (opp_type, protocol, chain, market_key) — a real UNIQUE
    constraint backs it, so a concurrent duplicate insert raises
    IntegrityError; we catch, re-read, and update instead.
    """
    from sqlalchemy.exc import IntegrityError
    key = dict(
        opp_type=row["opp_type"], protocol=row["protocol"],
        chain=row["chain"], market_key=row["market_key"],
    )
    mutable = {k: v for k, v in row.items() if k in _OPPORTUNITY_CORE_MUTABLE}
    with get_session() as s:
        existing = s.query(OpportunityCore).filter_by(**key).first()
        if existing is None:
            core = OpportunityCore(
                **key,
                first_seen=row["first_seen"],
                **mutable,
            )
            # Seed competition defaults on INSERT only — the detection
            # path doesn't pass them, so a re-detection upsert can never
            # clobber the re-measurement loop's values back to zero.
            if core.competitor_count is None:
                core.competitor_count = 0
            if core.competitor_trend is None:
                core.competitor_trend = "zero"
            if core.window_status is None:
                core.window_status = "opening"
            if core.competitor_count_history is None:
                core.competitor_count_history = []
            s.add(core)
            try:
                s.flush()
                return int(core.id)
            except IntegrityError:
                # Lost a concurrent-insert race — the UNIQUE constraint
                # held; fall through to the update path on the winner row.
                s.rollback()
                existing = s.query(OpportunityCore).filter_by(**key).first()
                if existing is None:
                    raise
        for k, v in mutable.items():
            setattr(existing, k, v)
        return int(existing.id)


def save_opportunity_detail(opp_type: str, core_id: int, detail: dict) -> None:
    """Route a typed detail dict to the right per-type table. One detail
    row per core row — re-detection updates in place. Unknown keys are
    filtered (mirrors save_funding_observations' allowed-set style)."""
    model = _OPPORTUNITY_DETAIL_MODELS.get(opp_type)
    if model is None:
        return
    cols = {c.name for c in model.__table__.columns} - {"id", "core_id"}
    payload = {k: v for k, v in (detail or {}).items() if k in cols}
    with get_session() as s:
        existing = s.query(model).filter_by(core_id=core_id).first()
        if existing is None:
            s.add(model(core_id=core_id, **payload))
        else:
            for k, v in payload.items():
                setattr(existing, k, v)


def save_opportunity_observation(row: dict) -> None:
    """Write the immutable decision-time snapshot ONCE (hypothesis log).

    UNIQUE on core_id: a re-detection never rewrites the feature vector —
    no look-ahead may leak into it. Label columns stay null here; only
    update_opportunity_labels fills them.
    """
    with get_session() as s:
        existing = (
            s.query(OpportunityObservation)
            .filter_by(core_id=row["core_id"])
            .first()
        )
        if existing is not None:
            return
        s.add(OpportunityObservation(
            core_id=row["core_id"],
            detector_id=row["detector_id"],
            opp_type=row["opp_type"],
            first_seen=row["first_seen"],
            feature_vector_json=row.get("feature_vector_json"),
            detection_latency_ms=row.get("detection_latency_ms"),
            unconventional_score=row.get("unconventional_score"),
            unconventional_factors_json=row.get("unconventional_factors_json"),
            unconventional_rationale=row.get("unconventional_rationale"),
        ))


def update_opportunity_labels(core_id: int, horizon_h: int, labels: dict) -> None:
    """Backfill forward labels for one horizon — the SEPARATE scheduled
    pass (features-now / labels-later). Merges {horizon_h: value} into
    each label JSON column so multiple horizons accumulate in place.

    For disqualified rows the same pass also writes
    counterfactual_outcome_json (FEATURE BLOCK 6c) — LOGS ONLY; the gate
    verdict and both ranked views are unchanged by anything written here.
    """
    h = str(int(horizon_h))
    with get_session() as s:
        obs = (
            s.query(OpportunityObservation)
            .filter_by(core_id=core_id)
            .first()
        )
        if obs is None:
            return
        for col, key in (
            ("realized_competitor_count_json", "realized_competitor_count"),
            ("realized_edge_decay_json",       "realized_edge_decay"),
            ("window_status_at_horizon_json",  "window_status_at_horizon"),
        ):
            if key in labels:
                merged = dict(getattr(obs, col) or {})
                merged[h] = labels[key]
                setattr(obs, col, merged)
        if "counterfactual_outcome" in labels:
            merged = dict(obs.counterfactual_outcome_json or {})
            merged[h] = labels["counterfactual_outcome"]
            obs.counterfactual_outcome_json = merged
        obs.label_filled_at = datetime.utcnow()


def record_competition_check(core_id: int, count: int, trend: str,
                             history_append: list,
                             window_status: Optional[str] = None) -> None:
    """The re-measurement write: competitor_count / trend / history /
    last_competition_check. Never touches first_seen."""
    with get_session() as s:
        core = s.query(OpportunityCore).filter_by(id=core_id).first()
        if core is None:
            return
        core.competitor_count = int(count)
        core.competitor_trend = trend
        history = list(core.competitor_count_history or [])
        history.extend(history_append or [])
        core.competitor_count_history = history
        core.last_competition_check = datetime.utcnow()
        if window_status is not None:
            core.window_status = window_status


def get_open_opportunity_cores(limit: int = 200) -> list[dict]:
    """Core rows whose window is not closed — the re-measurement loop's
    work list. Plain dicts, detached from the session."""
    with get_session() as s:
        rows = (
            s.query(OpportunityCore)
            .filter(OpportunityCore.window_status != "closed")
            .order_by(OpportunityCore.last_competition_check.asc())
            .limit(limit)
            .all()
        )
        return [_opportunity_core_to_dict(r) for r in rows]


def get_opportunity_observations_needing_labels(horizon_h: int,
                                                limit: int = 200) -> list[dict]:
    """Observation rows whose first_seen is at least horizon_h old and
    whose label set for that horizon is still missing. Drives the
    label-backfill pass."""
    h = str(int(horizon_h))
    cutoff = datetime.utcnow() - timedelta(hours=int(horizon_h))
    out = []
    with get_session() as s:
        rows = (
            s.query(OpportunityObservation)
            .filter(OpportunityObservation.first_seen <= cutoff)
            .limit(limit * 4)   # coarse pre-filter; horizon check below
            .all()
        )
        for r in rows:
            filled = r.window_status_at_horizon_json or {}
            if h in filled:
                continue
            out.append({
                "core_id":     r.core_id,
                "detector_id": r.detector_id,
                "opp_type":    r.opp_type,
                "first_seen":  r.first_seen,
            })
            if len(out) >= limit:
                break
    return out


def _opportunity_core_to_dict(r) -> dict:
    return {
        "id":                       r.id,
        "opp_type":                 r.opp_type,
        "protocol":                 r.protocol,
        "chain":                    r.chain,
        "market_key":               r.market_key,
        "asset_class":              r.asset_class,
        "detector_id":              r.detector_id,
        "first_seen":               r.first_seen.isoformat() if r.first_seen else None,
        "competitor_count":         r.competitor_count,
        "competitor_trend":         r.competitor_trend,
        "competitor_count_history": r.competitor_count_history or [],
        "last_competition_check":   (r.last_competition_check.isoformat()
                                     if r.last_competition_check else None),
        "edge_annualized_pct":      r.edge_annualized_pct,
        "edge_confidence":          r.edge_confidence,
        "risk_status":              r.risk_status,
        "risk_flags":               r.risk_flags or [],
        "reachability_verdict":     r.reachability_verdict,
        "shark_constraints":        r.shark_constraints,
        "window_status":            r.window_status,
        "unconventional_score":     r.unconventional_score,
        "unconventional_factors":   r.unconventional_factors or [],
        "unconventional_rationale": r.unconventional_rationale,
    }


def get_ranked_opportunities(mode: str = "standard",
                             show_disqualified: bool = False,
                             limit: int = 50) -> list[dict]:
    """The actionable view read (dashboard + agent).

    mode="standard"    — trajectory (headline) → edge (secondary) →
                         reachability (tie-break). Time dominates
                         magnitude: a zero-competitor survivor outranks
                         a fatter edge whose competitor count jumped.
    mode="exploratory" — FEATURE BLOCK 6b: same survivor set, sorted by
                         unconventional_score then factor richness; rows
                         below OPPORTUNITY_UNCONVENTIONAL_MIN_SCORE are
                         excluded. The two views NEVER merge.

    Disqualified rows are excluded from BOTH views unless
    show_disqualified=True (record-vs-enforce: the gate filters the
    view, not the write). Every returned row carries competitor_trend —
    edge is never surfaced without trajectory adjacent.
    """
    from config import settings
    with get_session() as s:
        q = s.query(OpportunityCore)
        if not show_disqualified:
            q = q.filter(OpportunityCore.risk_status == "survivable")
        rows = [_opportunity_core_to_dict(r) for r in q.all()]

    if mode == "exploratory":
        floor = float(getattr(settings, "OPPORTUNITY_UNCONVENTIONAL_MIN_SCORE", 0.3))
        rows = [r for r in rows if (r["unconventional_score"] or 0.0) >= floor]
        rows.sort(key=lambda r: (
            -(r["unconventional_score"] or 0.0),
            -len(r["unconventional_factors"] or []),
        ))
    else:
        rows.sort(key=lambda r: (
            _TREND_RANK.get(r["competitor_trend"], len(_TREND_RANK)),
            -(r["edge_annualized_pct"]
              if r["edge_annualized_pct"] is not None else float("-inf")),
            _REACHABILITY_RANK.get(r["reachability_verdict"],
                                   _REACHABILITY_RANK["unknown"]),
        ))
    return rows[:limit]


def get_opportunity_summary() -> dict:
    """Aggregate stats for the dashboard panel. Zeroed defaults on any
    hiccup — panel render treats zeros as 'no data yet'."""
    out = {
        "n_core":          0,
        "n_survivable":    0,
        "n_disqualified":  0,
        "n_observations":  0,
        "n_labeled":       0,
        "by_trend":        {},
        "mean_detection_latency_ms": 0.0,
    }
    try:
        with get_session() as s:
            cores = s.query(OpportunityCore).all()
            out["n_core"] = len(cores)
            out["n_survivable"] = sum(
                1 for c in cores if c.risk_status == "survivable")
            out["n_disqualified"] = sum(
                1 for c in cores if c.risk_status == "disqualified")
            by_trend: dict[str, int] = {}
            for c in cores:
                if c.competitor_trend:
                    by_trend[c.competitor_trend] = by_trend.get(c.competitor_trend, 0) + 1
            out["by_trend"] = by_trend
            obs = s.query(OpportunityObservation).all()
            out["n_observations"] = len(obs)
            out["n_labeled"] = sum(1 for o in obs if o.label_filled_at is not None)
            lats = [o.detection_latency_ms for o in obs
                    if o.detection_latency_ms is not None]
            if lats:
                out["mean_detection_latency_ms"] = sum(lats) / len(lats)
                from statistics import median
                out["detection_latency_ms_p50"] = int(median(lats))
    except Exception:
        pass
    # Always-present key (None until any latency is observed) — the web
    # panel's "detection-layer grade" stat reads this.
    out.setdefault("detection_latency_ms_p50", None)
    return out


def get_opportunity_core_by_id(core_id: int) -> Optional[dict]:
    """One core row as a dict (None if missing) — the label-backfill
    pass reads current state through this."""
    with get_session() as s:
        r = s.query(OpportunityCore).filter_by(id=core_id).first()
        return _opportunity_core_to_dict(r) if r is not None else None


def get_disqualified_opportunity_cores(limit: int = 200) -> list[dict]:
    """Disqualified rows for the gate self-audit backfill (6c). Read
    only — nothing here can make a row actionable."""
    with get_session() as s:
        rows = (
            s.query(OpportunityCore)
            .filter(OpportunityCore.risk_status == "disqualified")
            .limit(limit)
            .all()
        )
        return [_opportunity_core_to_dict(r) for r in rows]


def get_opportunity_notes(noteworthy_only: bool = True,
                          limit: int = 50) -> list[dict]:
    """The agent's reasoning posts for the web panel feed — COMPOSED from
    the hypothesis log, not a new table (the agent build owns all
    opportunity writers; this is a pure read).

    A "post" is an opportunity_observation row whose decision-time
    unconventional_rationale is non-empty (the agent wrote prose).
    noteworthy = unconventional_score >= OPPORTUNITY_UNCONVENTIONAL_MIN_SCORE
    — the lane's noise floor; below it the post is a routine note. The
    distinction matters statistically: the noteworthy feed's hit-rate is
    the meaningful one, while across all posts some routine notes look
    prescient by chance.

    edge_pct / competitor_trend / reachability come from the joined core
    row — the closest available stand-in for the at-write snapshot until
    the agent stamps dedicated copies (the observation row predates any
    competition data, so the core's values ARE the post-write history).

    was_right is a deterministic outcome stamp derived from the earliest
    backfilled forward-label horizon: window still opening/open at the
    horizon → "correct" (the read flagged a window that stayed winnable),
    closed → "incorrect", closing → "inconclusive"; null until the
    backfill pass has resolved any horizon. Newest first, capped.
    """
    from config import settings
    floor = float(getattr(settings, "OPPORTUNITY_UNCONVENTIONAL_MIN_SCORE", 0.3))
    out: list[dict] = []
    with get_session() as s:
        rows = (
            s.query(OpportunityObservation, OpportunityCore)
            .join(OpportunityCore,
                  OpportunityObservation.core_id == OpportunityCore.id)
            .filter(OpportunityObservation.unconventional_rationale.isnot(None))
            .filter(OpportunityObservation.unconventional_rationale != "")
            .order_by(desc(OpportunityObservation.first_seen))
            .all()
        )
    for obs, core in rows:
        noteworthy = float(obs.unconventional_score or 0.0) >= floor
        if noteworthy_only and not noteworthy:
            continue
        was_right, outcome_summary = _opportunity_note_outcome(obs)
        out.append({
            "id":               obs.id,
            "ts":               (obs.first_seen.strftime("%m-%d %H:%M")
                                 if obs.first_seen else "—"),
            "body":             obs.unconventional_rationale,
            "factor_tags":      list(obs.unconventional_factors_json or []),
            "edge_pct":         core.edge_annualized_pct,
            "competitor_trend": core.competitor_trend,
            "reachability":     core.reachability_verdict,
            "noteworthy":       noteworthy,
            "was_right":        was_right,
            "outcome_summary":  outcome_summary,
        })
        if len(out) >= limit:
            break
    return out


def _opportunity_note_outcome(obs) -> tuple[Optional[str], Optional[str]]:
    """Outcome stamp for one note from its earliest resolved label
    horizon. (None, None) while the window hasn't resolved."""
    if obs.label_filled_at is None:
        return None, None
    statuses = obs.window_status_at_horizon_json or {}
    if not statuses:
        return None, None
    try:
        h = min(int(k) for k in statuses)
    except (TypeError, ValueError):
        return None, None
    status = statuses.get(str(h))
    counts = obs.realized_competitor_count_json or {}
    count = counts.get(str(h))
    if status in ("opening", "open"):
        was_right = "correct"
    elif status == "closed":
        was_right = "incorrect"
    else:
        was_right = "inconclusive"
    summary = f"{h}h: window {status}"
    if count is not None:
        summary += f", {count} competitor{'s' if count != 1 else ''}"
    return was_right, summary


def get_opportunity_detail(opp_type: str, core_id: int) -> dict:
    """Typed detail row as a dict; safe empty dict when missing
    (graceful degradation for the dashboard read)."""
    model = _OPPORTUNITY_DETAIL_MODELS.get(opp_type)
    if model is None:
        return {}
    try:
        with get_session() as s:
            r = s.query(model).filter_by(core_id=core_id).first()
            if r is None:
                return {}
            return {c.name: getattr(r, c.name)
                    for c in model.__table__.columns
                    if c.name not in ("id", "core_id")}
    except Exception:
        return {}


# ── Wallet + exchange-flow watcher (follow/) ─────────────────────────────────
#
# OBSERVER reads/writes. Flow events are SUGGESTIVE evidence (never confirmed
# intent). transition_provenance is the DB-layer enforcement of the HARD
# INVARIANT — it duplicates follow/provenance.py's rules on purpose (defence in
# depth) and is kept local rather than importing follow/ (which imports this
# module — the same circular-import avoidance the opportunity ranker uses).

# Provenance state machine — the single allowed-transition table. `by` is the
# actor class. The load-bearing rule: a "discovery" actor may ONLY ever produce
# "candidate"; there is no discovery->confirmed edge anywhere.
WALLET_PROVENANCE_STATES = ("candidate", "confirmed", "manual", "rejected")
# (from_state, to_state) -> set of actor classes permitted to make the move.
# from_state None == the address is not yet on the watchlist.
_WALLET_PROVENANCE_TRANSITIONS = {
    (None,        "candidate"): {"discovery", "operator"},
    (None,        "manual"):    {"operator"},
    ("candidate", "confirmed"): {"operator"},            # operator-only confirm
    ("candidate", "rejected"):  {"operator"},            # operator-only reject (permanent)
    ("candidate", "manual"):    {"operator"},
    ("confirmed", "candidate"): {"auto", "operator"},    # trust expiry / demote breaker
    ("manual",    "candidate"): {"auto", "operator"},
    ("manual",    "rejected"):  {"operator"},
    # NOTE: no (*, ...) edge OUT OF "rejected" exists — rejected is permanent,
    # so a rejected wallet is never re-proposed. And no (_, "confirmed") edge
    # admits "discovery": discovery can never confirm.
}


def _wallet_watchlist_to_dict(r) -> dict:
    return {
        "address":               r.address,
        "provenance":            r.provenance,
        "added_at":              r.added_at.isoformat() if r.added_at else None,
        "added_by":              r.added_by,
        "confirmed_at":          r.confirmed_at.isoformat() if r.confirmed_at else None,
        "expires_at":            r.expires_at.isoformat() if r.expires_at else None,
        "actions_since_confirm": int(r.actions_since_confirm or 0),
        "last_demote_reason":    r.last_demote_reason,
    }


def get_watchlist_entry(address: str) -> Optional[dict]:
    """One watchlist row as a dict (None if the address is unknown)."""
    with get_session() as s:
        r = s.query(WalletWatchlist).filter_by(address=address).first()
        return _wallet_watchlist_to_dict(r) if r is not None else None


def get_watchlist_by_provenance(provenance: str) -> list[dict]:
    """Every watchlist address in the given provenance state."""
    with get_session() as s:
        rows = (s.query(WalletWatchlist)
                .filter(WalletWatchlist.provenance == provenance)
                .order_by(WalletWatchlist.added_at.asc())
                .all())
        return [_wallet_watchlist_to_dict(r) for r in rows]


def get_signal_eligible_wallets() -> set[str]:
    """Addresses the watcher may emit/act on — ONLY manual + confirmed.
    Candidate and rejected wallets are excluded (the HARD INVARIANT)."""
    with get_session() as s:
        rows = (s.query(WalletWatchlist.address)
                .filter(WalletWatchlist.provenance.in_(("manual", "confirmed")))
                .all())
        return {r[0] for r in rows}


def transition_provenance(address: str, to_state: str, by: str,
                          reason: Optional[str] = None,
                          expires_at: Optional[datetime] = None) -> dict:
    """The ONE provenance write — enforces the HARD INVARIANT at the DB layer.

    Returns an envelope {"ok": bool, ...}; never raises to feature code. `by`
    is the actor class: "discovery" | "operator" | "auto". Refuses any move not
    in _WALLET_PROVENANCE_TRANSITIONS for that actor — in particular it can
    never take a discovery output to "confirmed", and never moves anything out
    of "rejected" (permanent)."""
    if to_state not in WALLET_PROVENANCE_STATES:
        return {"ok": False, "error": f"bad_state:{to_state}"}
    with get_session() as s:
        existing = s.query(WalletWatchlist).filter_by(address=address).first()
        frm = existing.provenance if existing is not None else None
        allowed = _WALLET_PROVENANCE_TRANSITIONS.get((frm, to_state), set())
        if by not in allowed:
            return {"ok": False, "error": "transition_forbidden",
                    "from": frm, "to": to_state, "by": by}
        if existing is None:
            existing = WalletWatchlist(address=address, provenance=to_state,
                                       added_by=by, actions_since_confirm=0)
            s.add(existing)
        else:
            existing.provenance = to_state
        if to_state == "confirmed":
            existing.confirmed_at = datetime.utcnow()
            existing.actions_since_confirm = 0
            existing.expires_at = expires_at
            existing.last_demote_reason = None
        elif to_state == "candidate" and frm in ("confirmed", "manual"):
            existing.last_demote_reason = reason
        return {"ok": True, "address": address, "from": frm, "to": to_state,
                "by": by}


def bump_wallet_actions(address: str, n: int = 1) -> None:
    """Increment a confirmed/manual wallet's actions_since_confirm counter
    (drives the action-count arm of trust expiry). No-op if unknown."""
    with get_session() as s:
        r = s.query(WalletWatchlist).filter_by(address=address).first()
        if r is None:
            return
        r.actions_since_confirm = int(r.actions_since_confirm or 0) + int(n)


# ── Flow events ──────────────────────────────────────────────────────────────

def insert_wallet_flow_event(row: dict) -> int:
    """Append one observed flow/action event. Returns the row id. Stored as
    suggestive evidence — `action` is the observed class, not an intent."""
    with get_session() as s:
        ev = WalletFlowEvent(
            source_id=row["source_id"],
            actor_id=row["actor_id"],
            action=row["action"],
            asset=row.get("asset"),
            venue=row.get("venue"),
            size_usd=row.get("size_usd"),
            occurred_at=row["occurred_at"],
            detected_at=row["detected_at"],
            resolved_at=row.get("resolved_at"),
            outcome=row.get("outcome"),
            meta=row.get("meta"),
        )
        s.add(ev)
        s.flush()
        return int(ev.id)


def resolve_wallet_flow_event(event_id: int, resolved_at: datetime,
                              outcome: str) -> None:
    """Stamp an event's outcome clock (the second clock the scorer reads)."""
    with get_session() as s:
        ev = s.query(WalletFlowEvent).filter_by(id=event_id).first()
        if ev is None:
            return
        ev.resolved_at = resolved_at
        ev.outcome = outcome


def _wallet_flow_event_to_dict(r, *, raw_times: bool = False) -> dict:
    def _t(v):
        if v is None:
            return None
        return v if raw_times else v.isoformat()
    return {
        "id":          r.id,
        "source_id":   r.source_id,
        "actor_id":    r.actor_id,
        "action":      r.action,
        "asset":       r.asset,
        "venue":       r.venue,
        "size_usd":    r.size_usd,
        "occurred_at": _t(r.occurred_at),
        "detected_at": _t(r.detected_at),
        "resolved_at": _t(r.resolved_at),
        "outcome":     r.outcome,
        "meta":        r.meta or {},
    }


def get_recent_wallet_flow_events(limit: int = 20) -> list[dict]:
    """Newest flow events for the live panel (low-volume push)."""
    with get_session() as s:
        rows = (s.query(WalletFlowEvent)
                .order_by(desc(WalletFlowEvent.detected_at))
                .limit(limit).all())
        return [_wallet_flow_event_to_dict(r) for r in rows]


def get_wallet_resolved_actions(address: str) -> list[dict]:
    """EVERY resolved action for one wallet, raw datetimes intact. This is the
    raw feed the SINGLE shared point-in-time function filters as-of a timestamp
    — it deliberately does NOT filter by time itself, so the one as-of
    implementation lives in follow/skill_scorer.py and nowhere else."""
    with get_session() as s:
        rows = (s.query(WalletFlowEvent)
                .filter(WalletFlowEvent.actor_id == address)
                .filter(WalletFlowEvent.resolved_at.isnot(None))
                .order_by(WalletFlowEvent.occurred_at.asc())
                .all())
        return [_wallet_flow_event_to_dict(r, raw_times=True) for r in rows]


def get_distinct_flow_actors() -> list[str]:
    """Distinct wallet addresses seen in the event log — discovery's candidate
    universe (it mines the watcher's OWN stream)."""
    with get_session() as s:
        rows = s.query(WalletFlowEvent.actor_id).distinct().all()
        return [r[0] for r in rows]


def get_wallet_net_flows(windows_h: list[int]) -> list[dict]:
    """Rolling per-token, per-exchange (inflow - outflow) over each window.
    transfer_in = flow TO a labelled exchange (pre-sell tell); transfer_out =
    withdrawal from one (accumulation tell). Suggestive, never a confirmed
    sell."""
    out: list[dict] = []
    now = datetime.utcnow()
    with get_session() as s:
        for window_h in windows_h:
            cutoff = now - timedelta(hours=int(window_h))
            rows = (s.query(WalletFlowEvent)
                    .filter(WalletFlowEvent.occurred_at >= cutoff)
                    .filter(WalletFlowEvent.action.in_(("transfer_in", "transfer_out")))
                    .filter(WalletFlowEvent.venue.isnot(None))
                    .all())
            agg: dict[tuple, dict] = {}
            for r in rows:
                key = (r.asset or "?", r.venue or "?")
                a = agg.setdefault(key, {"inflow_usd": 0.0, "outflow_usd": 0.0,
                                         "events": 0})
                if r.action == "transfer_in":
                    a["inflow_usd"] += float(r.size_usd or 0.0)
                else:
                    a["outflow_usd"] += float(r.size_usd or 0.0)
                a["events"] += 1
            for (asset, venue), a in agg.items():
                out.append({
                    "asset":       asset,
                    "venue":       venue,
                    "window_h":    int(window_h),
                    "inflow_usd":  round(a["inflow_usd"], 2),
                    "outflow_usd": round(a["outflow_usd"], 2),
                    "net_usd":     round(a["inflow_usd"] - a["outflow_usd"], 2),
                    "events":      a["events"],
                })
    return out


# ── Discovery candidates ─────────────────────────────────────────────────────

def _discovery_candidate_to_dict(r) -> dict:
    return {
        "id":                r.id,
        "address":           r.address,
        "discovered_at":     r.discovered_at.isoformat() if r.discovered_at else None,
        "skill_score":       r.skill_score,
        "resolved_sample":   r.resolved_sample,
        "first_mover_ratio": r.first_mover_ratio,
        "latency_delta_s":   r.latency_delta_s,
        "warnings":          r.warnings or [],
        "gate_passed":       bool(r.gate_passed),
        "review_state":      r.review_state,
    }


def insert_discovery_candidate(row: dict) -> int:
    """Write one auto-discovery PROPOSAL row. Discovery writes ONLY these; this
    function never touches the watchlist's confirmed set."""
    with get_session() as s:
        c = DiscoveryCandidate(
            address=row["address"],
            skill_score=row.get("skill_score"),
            resolved_sample=row.get("resolved_sample"),
            first_mover_ratio=row.get("first_mover_ratio"),
            latency_delta_s=row.get("latency_delta_s"),
            warnings=row.get("warnings") or [],
            gate_passed=bool(row.get("gate_passed", False)),
            review_state=row.get("review_state", "pending"),
        )
        s.add(c)
        s.flush()
        return int(c.id)


def get_pending_candidates(limit: int = 100) -> list[dict]:
    """Pending discovery candidates for the operator review queue."""
    with get_session() as s:
        rows = (s.query(DiscoveryCandidate)
                .filter(DiscoveryCandidate.review_state == "pending")
                .order_by(desc(DiscoveryCandidate.discovered_at))
                .limit(limit).all())
        return [_discovery_candidate_to_dict(r) for r in rows]


def set_candidate_review_state(address: str, state: str) -> None:
    """Mark every pending proposal for an address as approved/rejected once the
    operator acts on it (keeps the review queue from re-showing it)."""
    with get_session() as s:
        rows = (s.query(DiscoveryCandidate)
                .filter(DiscoveryCandidate.address == address)
                .filter(DiscoveryCandidate.review_state == "pending")
                .all())
        for r in rows:
            r.review_state = state


# ── Exchange-address label set (SHARED terminal stop-list) ───────────────────

def upsert_exchange_label(address: str, label: str,
                          exchange_name: Optional[str] = None,
                          source: str = "seed",
                          last_verified_at: Optional[datetime] = None) -> None:
    """Insert or refresh one exchange-address label."""
    with get_session() as s:
        r = s.query(ExchangeLabel).filter_by(address=address).first()
        if r is None:
            s.add(ExchangeLabel(
                address=address, label=label, exchange_name=exchange_name,
                source=source,
                last_verified_at=last_verified_at or datetime.utcnow()))
        else:
            r.label = label
            r.exchange_name = exchange_name
            r.source = source
            r.last_verified_at = last_verified_at or datetime.utcnow()


def get_exchange_label(address: str) -> Optional[dict]:
    """Label metadata for one address (None if not a labelled address)."""
    with get_session() as s:
        r = s.query(ExchangeLabel).filter_by(address=address).first()
        if r is None:
            return None
        return {
            "address":          r.address,
            "label":            r.label,
            "exchange_name":    r.exchange_name,
            "source":           r.source,
            "last_verified_at": r.last_verified_at.isoformat() if r.last_verified_at else None,
        }


def is_terminal_address(address: str) -> bool:
    """True if the address is a labelled exchange/router/bridge/multisig — the
    SHARED 'is this a terminal address' check (also the meme stop-list)."""
    with get_session() as s:
        return s.query(ExchangeLabel).filter_by(address=address).first() is not None


def get_exchange_label_addresses() -> list[str]:
    """Every labelled address — the watcher subscribes to these (plus the
    watchlist) so it sees flow INTO exchanges, not just wallet-side actions."""
    with get_session() as s:
        return [r[0] for r in s.query(ExchangeLabel.address).all()]


def label_set_staleness() -> dict:
    """Coverage + staleness metadata for the label set. is_stale is True when
    the most-recently-verified label is older than
    WALLETFLOW_LABEL_STALENESS_WARN_DAYS (the set drifts as exchanges rotate
    addresses, so a stale set silently misses flow)."""
    from config import settings
    warn_days = int(getattr(settings, "WALLETFLOW_LABEL_STALENESS_WARN_DAYS", 7))
    with get_session() as s:
        count = s.query(ExchangeLabel).count()
        newest = (s.query(ExchangeLabel)
                  .order_by(desc(ExchangeLabel.last_verified_at)).first())
    last_verified = (newest.last_verified_at if newest is not None else None)
    age_days = None
    if last_verified is not None:
        age_days = (datetime.utcnow() - last_verified).total_seconds() / 86400.0
    is_stale = (count == 0) or (age_days is not None and age_days > warn_days)
    return {
        "count":            count,
        "last_verified_at": last_verified.isoformat() if last_verified else None,
        "age_days":         (round(age_days, 2) if age_days is not None else None),
        "warn_days":        warn_days,
        "is_stale":         bool(is_stale),
    }
