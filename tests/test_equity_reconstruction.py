"""Equity reconstruction on startup.

The funds track P&L in in-memory counters that reset every launch (and a hard
kill never writes the clean-shutdown snapshot). These tests cover the ledger
sums that let each agent resume from its accumulated figure, plus the per-agent
wiring that consumes them.

The query-helper tests use a fresh temp SQLite per test (mirrors
test_queries.py) and restore the real DB binding on teardown. The agent-wiring
tests don't touch the DB — they monkeypatch the helpers.
"""
import importlib
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from agents import SignalAgentWrapper, ArbAgentWrapper
from agents.scalping_agent import ScalpingAgent, ScalpObservation
from config import settings


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "equity.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_file)
    import database.db
    importlib.reload(database.db)
    import database.queries as q
    importlib.reload(q)
    database.db.init_db()
    yield q, database.db
    monkeypatch.undo()
    importlib.reload(database.db)
    importlib.reload(q)


# ── Query helpers ───────────────────────────────────────────────────────────

def _add_trade(ddb, *, pnl_usd, strategy=None, days_ago=0):
    from database.models import Trade
    with ddb.get_session() as s:
        s.add(Trade(
            pair="BTC/USDT", exchange="binance", side="long",
            entry_price=100.0, size_usd=50.0, pnl_usd=pnl_usd,
            timestamp_close=datetime.utcnow() - timedelta(days=days_ago),
            strategy=strategy,
        ))


def test_trade_realized_pnl_all_time_today_and_strategy(temp_db):
    q, ddb = temp_db
    _add_trade(ddb, pnl_usd=5.0,  strategy=None,    days_ago=0)   # signal, today
    _add_trade(ddb, pnl_usd=3.0,  strategy=None,    days_ago=2)   # signal, old
    _add_trade(ddb, pnl_usd=-1.0, strategy="scalp", days_ago=0)   # scalp, today

    # All-time, everything: 5 + 3 - 1 = 7
    assert q.get_trade_realized_pnl() == pytest.approx(7.0)
    # Signal fund (exclude scalp), all-time: 5 + 3 = 8
    assert q.get_trade_realized_pnl(exclude_strategy="scalp") == pytest.approx(8.0)
    # Signal fund, today only: 5
    assert q.get_trade_realized_pnl(exclude_strategy="scalp", today=True) == pytest.approx(5.0)
    # Only scalp, all-time: -1
    assert q.get_trade_realized_pnl(only_strategy="scalp") == pytest.approx(-1.0)


def test_trade_realized_pnl_empty(temp_db):
    q, _ = temp_db
    assert q.get_trade_realized_pnl() == 0.0
    assert q.get_trade_realized_pnl(today=True) == 0.0


def test_arb_realized_pnl(temp_db):
    q, ddb = temp_db
    from database.models import ArbTrade
    with ddb.get_session() as s:
        s.add(ArbTrade(symbol="ETH/USDT", net_pnl_usd=2.0,
                       timestamp=datetime.utcnow()))
        s.add(ArbTrade(symbol="ETH/USDT", net_pnl_usd=4.0,
                       timestamp=datetime.utcnow() - timedelta(days=3)))
        s.add(ArbTrade(symbol="ETH/USDT", net_pnl_usd=None,
                       timestamp=datetime.utcnow()))   # unsettled → ignored
    assert q.get_arb_realized_pnl() == pytest.approx(6.0)
    assert q.get_arb_realized_pnl(today=True) == pytest.approx(2.0)


def _scalp_obs(would_entry, **kw):
    base = dict(
        symbol="BTC/USDT", exchange="mexc", timestamp=time.time(),
        ofi_z=2.5, direction="LONG", strength="strong", tfi_confirms=True,
        raw_tfi=1.0, spread_bps=1.0, regime="TRENDING",
        round_trip_cost_bps=0.0, min_win_rate_required=0.4,
        tp_bps=3.0, sl_bps=1.9, would_entry=would_entry, skip_reason="",
        entry_price=50000.0,
    )
    base.update(kw)
    return ScalpObservation(**base)


def test_scalp_realized_pnl(temp_db):
    q, _ = temp_db
    q.save_scalp_observations([
        _scalp_obs(True, exit_price=50100.0, exit_reason="TP", hold_sec=30.0,
                   pnl_bps=20.0, pnl_usd=0.30),
        _scalp_obs(True, exit_price=49900.0, exit_reason="SL", hold_sec=20.0,
                   pnl_bps=-10.0, pnl_usd=-0.10),
        _scalp_obs(False, skip_reason="V2:depth thin"),   # not entered → ignored
    ])
    # Closed entries only: 0.30 - 0.10 = 0.20; obs are created today.
    assert q.get_scalp_realized_pnl() == pytest.approx(0.20)
    assert q.get_scalp_realized_pnl(today=True) == pytest.approx(0.20)


# ── Agent wiring (monkeypatched helpers — no DB) ─────────────────────────────

@pytest.mark.asyncio
async def test_signal_get_stats_reads_ledger(monkeypatch):
    monkeypatch.setattr("database.queries.get_today_trades", lambda: [])
    monkeypatch.setattr("database.queries.get_open_trades", lambda: [])
    monkeypatch.setattr("database.queries.get_signal_win_rate",
                        lambda **k: {"total": 0})
    monkeypatch.setattr("database.queries.get_trade_realized_pnl",
                        lambda **k: 12.0 if k.get("today") else 30.0)

    agent = SignalAgentWrapper()
    agent._bot = SimpleNamespace(_cb_state=SimpleNamespace(
        consecutive_losses=0, halted=False, daily_pnl_pct=0.0))
    stats = await agent.get_stats()

    assert stats.daily_pnl == pytest.approx(12.0)
    assert stats.total_pnl == pytest.approx(30.0)
    cap = settings.SIGNAL_AGENT_CAPITAL
    assert stats.daily_pnl_pct == pytest.approx(12.0 / cap * 100.0)


@pytest.mark.asyncio
async def test_signal_get_stats_falls_back_when_db_unreachable(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("database.queries.get_today_trades", boom)
    monkeypatch.setattr("database.queries.get_trade_realized_pnl", boom)

    agent = SignalAgentWrapper()
    cap = settings.SIGNAL_AGENT_CAPITAL
    agent._bot = SimpleNamespace(_cb_state=SimpleNamespace(
        consecutive_losses=0, halted=False, daily_pnl_pct=2.0))
    stats = await agent.get_stats()
    # Falls back to the in-memory daily figure: 2.0% of capital.
    assert stats.daily_pnl == pytest.approx(2.0 / 100.0 * cap)
    assert stats.total_pnl == 0.0


def test_scalp_reconstruct_pnl(monkeypatch):
    monkeypatch.setattr("database.queries.get_scalp_realized_pnl",
                        lambda **k: 0.75 if k.get("today") else 4.20)
    agent = ScalpingAgent()
    agent._reconstruct_pnl()
    assert agent._daily_pnl == pytest.approx(0.75)
    assert agent._stats["total_pnl"] == pytest.approx(4.20)


def test_scalp_reconstruct_pnl_survives_db_error(monkeypatch):
    def boom(**k):
        raise RuntimeError("db down")
    monkeypatch.setattr("database.queries.get_scalp_realized_pnl", boom)
    agent = ScalpingAgent()
    agent._daily_pnl = 1.0          # pre-existing in-memory value
    agent._reconstruct_pnl()         # must not raise
    assert agent._daily_pnl == 1.0   # left untouched on failure


def test_arb_reconstruct_engine_pnl(monkeypatch):
    monkeypatch.setattr("database.queries.get_arb_realized_pnl",
                        lambda **k: 3.0 if k.get("today") else 11.0)
    engine = SimpleNamespace(_total_pnl_usd=0.0, _daily_pnl_usd=0.0)
    ArbAgentWrapper._reconstruct_engine_pnl(engine)
    assert engine._total_pnl_usd == pytest.approx(11.0)
    assert engine._daily_pnl_usd == pytest.approx(3.0)
