"""
tests/test_funding_frontier_db.py — funding-frontier schema, summary,
migration, and observer report (Phase 4).

Covers the extended funding_arb_observations columns round-trip, the new
get_funding_summary rollups (cross-venue / long-tail / hip3 counts, crowding
mix, best net APR among OPEN pairs only), the idempotent migration against a
pre-extension DB, and the tools.observer_report funding section + --candidates.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from database import queries as q
from tools import observer_report as rpt


# ─────────────────────────────────────────────────────────────────────────
# Isolated SQLite for every test (never touches data/cryptobot.db)
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def _temp_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import db as db_module
    from database.models import Base

    path = tmp_path / "frontier_test.db"
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False}, echo=False,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False,
                               autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    return path


def _row(symbol, *, ts, legs="single", net=0.10, crowding=None,
         long_tail=False, hip3=None, would_enter=True, age=None,
         venue_long="binance", venue_short="binance", omit_obs_only=False):
    r = {
        "timestamp": ts, "symbol": symbol, "variant": "delta_neutral",
        "venue_long": venue_long, "venue_short": venue_short,
        "funding_apr": net, "spread_apr": net, "oi_usd": 1e7, "depth_ok": True,
        "notional_usd": 250.0, "margin_used": 125.0, "basis_at_entry": 0.0,
        "projected_funding_per_interval": 0.1, "projected_fees": 0.1,
        "projected_net_apr": net, "would_enter": would_enter, "skip_reason": "",
        "legs": legs, "funding_interval_sec": 3600.0,
        "taker_fee_bps": 4.5, "maker_fee_bps": 1.5,
        "is_long_tail": long_tail, "is_hip3": hip3, "pair_age_days": age,
        "spread_decay_bps_per_day": 1.0, "oi_growth_pct_24h": 10.0,
        "crowding_verdict": crowding,
    }
    if not omit_obs_only:
        r["observation_only"] = True
    return r


# ─────────────────────────────────────────────────────────────────────────
# 1. Extended columns round-trip + observation_only default
# ─────────────────────────────────────────────────────────────────────────

def test_frontier_columns_persist(_temp_db):
    now = time.time()
    q.save_funding_observations([
        _row("FART/USDC:USDC", ts=now - 60, legs="cross_venue", net=0.30,
             crowding="OPEN", long_tail=True, hip3=True, age=9.0,
             venue_long="hyperliquid", venue_short="binance"),
    ])
    rows = q.get_funding_observations(limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["legs"] == "cross_venue"
    assert r["is_long_tail"] is True
    assert r["is_hip3"] is True
    assert r["pair_age_days"] == pytest.approx(9.0)
    assert r["crowding_verdict"] == "OPEN"
    assert r["funding_interval_sec"] == pytest.approx(3600.0)
    assert r["taker_fee_bps"] == pytest.approx(4.5)


def test_observation_only_defaults_true_when_omitted(_temp_db):
    now = time.time()
    q.save_funding_observations([_row("BTC/USDT", ts=now, omit_obs_only=True)])
    r = q.get_funding_observations(limit=1)[0]
    assert r["observation_only"] is True


# ─────────────────────────────────────────────────────────────────────────
# 2. Summary rollups
# ─────────────────────────────────────────────────────────────────────────

def test_summary_rollups_counts_and_mix(_temp_db):
    now = time.time()
    q.save_funding_observations([
        _row("BTC/USDT",        ts=now - 10, legs="single",      crowding="UNKNOWN"),
        _row("SOL/USDT",        ts=now - 20, legs="cross_venue", crowding="OPEN",
             venue_long="hyperliquid", venue_short="binance"),
        _row("FART/USDC:USDC",  ts=now - 30, legs="single",      crowding="CROWDED",
             long_tail=True, hip3=True),
        _row("MOON/USDC:USDC",  ts=now - 40, legs="single",      crowding="COMPRESSING",
             long_tail=True),
    ])
    s = q.get_funding_summary(days=1)
    assert s["total"] == 4
    assert s["n_cross_venue"] == 1
    assert s["n_long_tail"] == 2
    assert s["n_hip3"] == 1
    # Crowding mix: 4 verdicts, one of each → 25% each.
    assert s["pct_crowding_OPEN"] == pytest.approx(25.0)
    assert s["pct_crowding_CROWDED"] == pytest.approx(25.0)
    assert s["pct_crowding_COMPRESSING"] == pytest.approx(25.0)
    assert s["pct_crowding_UNKNOWN"] == pytest.approx(25.0)


def test_best_open_considers_only_open_pairs(_temp_db):
    now = time.time()
    q.save_funding_observations([
        _row("A/USDT", ts=now - 10, crowding="OPEN",    net=0.20),
        _row("B/USDT", ts=now - 20, crowding="CROWDED", net=0.90),   # richer but crowded
        _row("C/USDT", ts=now - 30, crowding="OPEN",    net=0.15),
    ])
    s = q.get_funding_summary(days=1)
    assert s["best_projected_net_apr_open"] == pytest.approx(0.20)   # best OPEN only
    assert s["best_projected_net_apr"] == pytest.approx(0.90)        # best overall


def test_summary_empty_returns_zeroed_frontier_keys(_temp_db):
    s = q.get_funding_summary(days=1)
    for k in ("n_cross_venue", "n_long_tail", "n_hip3",
              "pct_crowding_OPEN", "best_projected_net_apr_open"):
        assert s[k] == 0 or s[k] == 0.0


# ─────────────────────────────────────────────────────────────────────────
# 3. Migration idempotent + safe on a pre-extension DB
# ─────────────────────────────────────────────────────────────────────────

def test_migration_adds_columns_and_is_idempotent(tmp_path):
    from scripts.migrate_funding_frontier import migrate, FRONTIER_COLUMNS

    path = str(tmp_path / "pre_extension.db")
    con = sqlite3.connect(path)
    # Minimal pre-frontier table (original columns only).
    con.execute(
        "CREATE TABLE funding_arb_observations ("
        "id INTEGER PRIMARY KEY, symbol TEXT, timestamp REAL, "
        "variant TEXT, would_enter BOOLEAN)"
    )
    con.commit()
    con.close()

    added = migrate(path)
    assert sorted(added) == sorted(name for name, _ in FRONTIER_COLUMNS)
    # Second run is a no-op.
    assert migrate(path) == []

    # Columns really exist now.
    con = sqlite3.connect(path)
    cols = {row[1] for row in con.execute("PRAGMA table_info(funding_arb_observations)")}
    con.close()
    assert "crowding_verdict" in cols and "is_long_tail" in cols


def test_migration_noop_when_table_absent(tmp_path):
    from scripts.migrate_funding_frontier import migrate
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()         # no tables at all
    assert migrate(path) == []


# ─────────────────────────────────────────────────────────────────────────
# 4. Observer report — rollup + candidates
# ─────────────────────────────────────────────────────────────────────────

def test_parse_since():
    assert rpt.parse_since("24h") == pytest.approx(1.0)
    assert rpt.parse_since("7d") == pytest.approx(7.0)
    assert rpt.parse_since("90m") == pytest.approx(90 / 1440)
    assert rpt.parse_since("garbage") == pytest.approx(1.0)


def test_report_rollup_leads_with_crowding(_temp_db):
    now = time.time()
    q.save_funding_observations([
        _row("SOL/USDT", ts=now - 10, legs="cross_venue", crowding="OPEN", net=0.25,
             venue_long="hyperliquid", venue_short="binance"),
        _row("FART/USDC:USDC", ts=now - 20, crowding="CROWDED", long_tail=True),
    ])
    text = rpt.funding_report(days=1, candidates=False)
    assert "crowding mix" in text
    assert "OPEN" in text and "CROWDED" in text
    assert "best net APR (OPEN)" in text


def test_report_candidates_open_would_enter_ranked(_temp_db):
    now = time.time()
    q.save_funding_observations([
        _row("A/USDT", ts=now - 10, crowding="OPEN",    net=0.15, would_enter=True),
        _row("B/USDT", ts=now - 20, crowding="OPEN",    net=0.30, would_enter=True),
        _row("C/USDT", ts=now - 30, crowding="CROWDED", net=0.40, would_enter=True),
        _row("D/USDT", ts=now - 40, crowding="OPEN",    net=0.50, would_enter=False),
    ])
    text = rpt.funding_report(days=1, candidates=True, now=now)
    lines = text.splitlines()
    # Only A and B qualify (OPEN & would_enter); B ranks above A by net APR.
    body = [ln for ln in lines if ln.startswith(("A/USDT", "B/USDT", "C/USDT", "D/USDT"))]
    assert body[0].startswith("B/USDT")
    assert body[1].startswith("A/USDT")
    assert not any(ln.startswith("C/USDT") for ln in body)   # crowded excluded
    assert not any(ln.startswith("D/USDT") for ln in body)   # not would_enter


def test_report_candidates_empty(_temp_db):
    text = rpt.funding_report(days=1, candidates=True)
    assert "none" in text.lower()
