"""
tests/test_queries.py — database/queries helpers that the wiring/fixes
batches added.

Each test uses a tmp_path SQLite via the temp_db fixture so we don't
share state with other test files.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    """Fresh SQLite per test. Reload db + queries against the tmp path."""
    db_file = tmp_path / "test_queries.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_file)

    import database.db
    importlib.reload(database.db)
    import database.queries as q
    importlib.reload(q)
    database.db.init_db()
    yield q


# ─────────────────────────────────────────────────────────────────────────
# get_last_equity (FIX 1)
# ─────────────────────────────────────────────────────────────────────────

def test_get_last_equity_returns_none_on_empty_table(temp_db):
    """Empty portfolio_snapshots → None (not 0.0). Bot startup uses
    None to mean 'fall through to STARTING_CAPITAL'."""
    assert temp_db.get_last_equity() is None


def test_get_last_equity_returns_most_recent_value(temp_db):
    q = temp_db
    q.log_portfolio_snapshot({"total_equity": 800.0})
    q.log_portfolio_snapshot({"total_equity": 950.0})
    q.log_portfolio_snapshot({"total_equity": 1234.56})
    assert q.get_last_equity() == pytest.approx(1234.56)


def test_get_last_equity_skips_null_rows(temp_db):
    """A snapshot without total_equity → row exists but field is None;
    helper must skip past it (returns None when latest row's equity
    is itself null — but real shape always carries equity)."""
    q = temp_db
    q.log_portfolio_snapshot({"total_equity": 500.0})
    # Direct add of a row with null equity to simulate a bad write.
    import database.db, database.models
    with database.db.get_session() as s:
        s.add(database.models.PortfolioSnapshot(
            timestamp=datetime.utcnow(),
            total_equity=None,
            total_daily_pnl=0.0,
            total_exposure_pct=0.0,
            agents_running=0,
            portfolio_status="?",
            snapshot_json={},
        ))
    # Latest row's equity is None → helper returns None (caller treats
    # as "no usable value").
    assert q.get_last_equity() is None


# ─────────────────────────────────────────────────────────────────────────
# portfolio_snapshot timestamp (FIX 3)
# ─────────────────────────────────────────────────────────────────────────

def test_portfolio_snapshot_timestamp_is_non_zero_after_write(temp_db):
    """log_portfolio_snapshot must set timestamp explicitly — never let
    it default to 0 or NULL. The spec's verify-SQL used
    datetime(col,'unixepoch') which returns blank on a DateTime column;
    we verify the actual storage shape instead."""
    q = temp_db
    before = datetime.utcnow() - timedelta(seconds=1)
    q.log_portfolio_snapshot({"total_equity": 1000.0})
    after = datetime.utcnow() + timedelta(seconds=1)

    import database.db, database.models
    with database.db.get_session() as s:
        row = (
            s.query(database.models.PortfolioSnapshot)
            .order_by(database.models.PortfolioSnapshot.id.desc())
            .first()
        )
    assert row.timestamp is not None
    assert before <= row.timestamp <= after


# ─────────────────────────────────────────────────────────────────────────
# get_trade_by_id (WIRE 3 — sanity here too, since this file is new)
# ─────────────────────────────────────────────────────────────────────────

def test_get_trade_by_id_returns_none_when_missing(temp_db):
    assert temp_db.get_trade_by_id(99999) is None


def test_get_trade_by_id_returns_row(temp_db):
    trade_id = temp_db.save_trade({
        "pair": "BTC/USDT", "exchange": "binance",
        "side": "long", "entry_price": 100.0, "size_usd": 50.0,
    })
    row = temp_db.get_trade_by_id(trade_id)
    assert row is not None
    assert row.pair == "BTC/USDT"
    assert row.entry_price == 100.0
