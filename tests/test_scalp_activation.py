"""Scalp activation-readiness queries (v1 + v2) and recalibration-SQL validity
(commit 5). Uses a fresh temp SQLite per test (mirrors test_queries.py) and
restores the real DB binding on teardown."""
import importlib
import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest

from agents.scalping_agent import ScalpObservation


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "scalp_v2.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_file)
    import database.db
    importlib.reload(database.db)
    import database.queries as q
    importlib.reload(q)
    database.db.init_db()
    yield q, db_file
    # Restore the real DB binding so later test modules aren't left pointed
    # at this (now-deleted) temp database.
    monkeypatch.undo()
    importlib.reload(database.db)
    importlib.reload(q)


def _obs(would_entry, **kw):
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


def test_activation_readiness_empty_not_ready(temp_db):
    q, _ = temp_db
    r = q.get_scalp_activation_readiness()
    assert r["ready"] is False
    assert r["stats"]["n_closed"] == 0
    assert any("n_closed" in c for c in r["reasons_failing"])


def test_activation_stats_and_v1_v2(temp_db):
    q, _ = temp_db
    obs = [
        _obs(True, timestamp=time.time() + i, exit_price=50100.0,
             exit_reason="TP", hold_sec=30.0, pnl_bps=20.0, pnl_usd=1.0,
             price_1m=50100.0)
        for i in range(4)
    ]
    q.save_scalp_observations(obs)
    stats = q.get_scalp_activation_stats()
    assert stats["n_closed"] == 4
    assert stats["win_rate"] == 1.0
    assert stats["directional_accuracy_1m"] == 1.0
    # 4 closed << 200 (v1) / 300 (v2) → both gate on n_closed.
    assert q.get_scalp_activation_readiness()["ready"] is False
    assert q.get_scalp_activation_readiness_v2()["ready"] is False


def test_recalibration_statements_execute(temp_db):
    q, db_file = temp_db
    q.save_scalp_observations([
        _obs(True, exit_price=50100.0, exit_reason="TP", hold_sec=30.0,
             pnl_bps=20.0, pnl_usd=1.0, price_1m=50100.0,
             strength_label="STRONG", vwap_aligned=True, htf_aligned=True,
             volume_adequate=False, atr_adjusted=True, sl_clamped=""),
        _obs(False, timestamp=time.time() + 1,
             skip_reason="V2:adverse: mid dropped", price_1m=49900.0),
    ])
    # Reuse the runner's extractor (the cookbook is the single source).
    runner = Path(__file__).resolve().parent.parent / "scripts" / "run_recalibration.py"
    spec = importlib.util.spec_from_file_location("_recal_runner", runner)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    statements = mod.extract_statements(mod.DOC.read_text(encoding="utf-8"))
    assert len(statements) >= 9
    con = sqlite3.connect(str(db_file))
    try:
        for stmt in statements:
            con.execute(stmt).fetchall()    # must not raise against the schema
    finally:
        con.close()
