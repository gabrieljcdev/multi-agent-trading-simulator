"""
tests/test_observer_viz.py — Unified observer UI (on-demand visualizations).

Covers the build spec's TESTS list for the UI-unification pass over the three
$0 observer panels (wallet-flow / copy-trade / meme). The pass is PRESENTATION
ONLY: it adds read-side aggregation endpoints + SVG charts; it must not touch
any observer's logic, scoring, invariants, or the 2Hz snapshot.

 1. precondition — all three observer sources importable
 2. snapshot UNCHANGED — heavy viz data is never folded into the 2Hz snapshot
 3. each new /viz/ endpoint returns derived data + 403s off-loopback
 4. as-of view reuses the ONE shared point-in-time fn (no second path)
 5. empty-state — every viz endpoint answers ok + zeroed with no stored data
 6. uncertainty markers present — sample/δ, resolved-vs-censored, denominator
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from config import settings
from database import queries as q


# ─────────────────────────────────────────────────────────────────────────
# Isolated SQLite per test (never touches data/cryptobot.db) — mirrors the
# fixture used across the observer suites.
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def _temp_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import db as db_module
    from database.models import Base

    path = tmp_path / "observer_viz_test.db"
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


async def _client():
    from aiohttp.test_utils import TestClient, TestServer
    from ui.web_server import WebServer
    ws = WebServer(coordinator=None, bot=None)
    client = TestClient(TestServer(ws._make_app()))
    await client.start_server()
    return ws, client


def _seed_wallet_flow():
    now = datetime.utcnow()
    # two inflows (pre-sell tell) + one outflow (accumulation) inside the window
    for i, (act, usd) in enumerate([("transfer_in", 1000.0),
                                    ("transfer_in", 500.0),
                                    ("transfer_out", 300.0)]):
        q.insert_wallet_flow_event({
            "source_id": "test", "actor_id": f"W{i}", "action": act,
            "asset": "SOL", "venue": "binance", "size_usd": usd,
            "occurred_at": now - timedelta(hours=1),
            "detected_at": now - timedelta(hours=1) + timedelta(seconds=5),
        })


def _seed_watchlist():
    from database.db import get_session
    from database.models import WalletWatchlist
    now = datetime.utcnow()
    rows = [
        ("conf_soon", "confirmed", now + timedelta(hours=2)),   # expiring soon
        ("conf_far",  "confirmed", now + timedelta(days=30)),   # not soon
        ("cand_1",    "candidate", None),
        ("manual_1",  "manual",    None),
        ("rej_1",     "rejected",  None),
    ]
    with get_session() as s:
        for addr, prov, exp in rows:
            s.add(WalletWatchlist(address=addr, provenance=prov,
                                  added_by="test", expires_at=exp))


def _seed_copytrade():
    # one skilled+followable, one skilled-but-unfollowable, one low-sample
    q.upsert_copytrade_actor({"actor_id": "A_good", "venue": "hyperliquid",
                              "skill_score": 0.8, "closed_trades": 40,
                              "latency_delta_s": 2.0, "drawdown": 0.1,
                              "followable": True})
    q.upsert_copytrade_actor({"actor_id": "A_unfoll", "venue": "hyperliquid",
                              "skill_score": 0.7, "closed_trades": 30,
                              "latency_delta_s": 90.0, "drawdown": 0.2,
                              "followable": False})
    q.upsert_copytrade_actor({"actor_id": "A_lown", "venue": "drift",
                              "skill_score": 0.6, "closed_trades": 3,
                              "latency_delta_s": 5.0, "drawdown": 0.05,
                              "followable": False})
    # denominator: surfaced + rejected-with-reason
    for a in ("A_good", "A_unfoll", "A_lown"):
        q.insert_copytrade_evaluation({"actor_id": a, "venue": "hyperliquid",
                                       "decision": "surfaced", "sample_size": 30})
    for a, reason in (("R1", "below_min_sample"), ("R2", "below_min_sample"),
                      ("R3", "drawdown_too_deep")):
        q.insert_copytrade_evaluation({"actor_id": a, "venue": "hyperliquid",
                                       "decision": "rejected", "reason": reason,
                                       "sample_size": 8})


# ─────────────────────────────────────────────────────────────────────────
# 1. Precondition — the pass only runs because all three observers exist
# ─────────────────────────────────────────────────────────────────────────

def test_precondition_three_observers_importable():
    from follow.wallet_flow import WalletFlowWatcher          # noqa: F401
    from follow.copytrade import CopyTradeObserver            # noqa: F401
    from follow.meme_scorer import MemeScorer                 # noqa: F401


# ─────────────────────────────────────────────────────────────────────────
# 1b. Reachability — every REGISTERED agent appears in the grid's AGENT_ORDER.
# The observer panels (and their viz drawers) are reached by clicking the
# `follow` card; a registered agent missing from AGENT_ORDER renders no card
# and is unreachable in the UI (the exact gap that hid the observer tabs).
# ─────────────────────────────────────────────────────────────────────────

def test_every_registered_agent_is_in_grid_order():
    import re
    from pathlib import Path
    from agents import REGISTERED_AGENTS

    html = Path("ui/web_dashboard.html").read_text(encoding="utf-8")
    m = re.search(r"const AGENT_ORDER\s*=\s*\[([^\]]*)\]", html)
    assert m, "AGENT_ORDER array not found in web_dashboard.html"
    order = set(re.findall(r'"([^"]+)"', m.group(1)))
    registered = {a.agent_id for a in REGISTERED_AGENTS}
    missing = registered - order
    assert not missing, f"registered agents missing from AGENT_ORDER (unreachable): {missing}"
    # the follow observer host specifically must be reachable
    assert "follow" in order


# ─────────────────────────────────────────────────────────────────────────
# 2. Snapshot UNCHANGED — the heavy viz data must never enter the 2Hz push
# ─────────────────────────────────────────────────────────────────────────

def test_snapshot_excludes_viz_data():
    from ui.web_server import WebServer
    ws = WebServer(coordinator=None, bot=None)
    # The light observer blocks carry only their live keys — no chart payloads.
    forbidden = {"series", "buckets", "points", "cluster", "scatter",
                 "provenance", "replay", "viz"}
    for block in (ws._snap_walletflow(), ws._snap_copytrade(), ws._snap_meme()):
        assert not (set(block) & forbidden), \
            f"viz data leaked into snapshot: {set(block) & forbidden}"


# ─────────────────────────────────────────────────────────────────────────
# 3. Privacy guard — both new /viz/ endpoints 403 off-loopback, 200 on it
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_viz_endpoints_privacy_guard(_temp_db, monkeypatch):
    ws, client = await _client()
    try:
        monkeypatch.setattr(settings, "WEB_UI_HOST", "0.0.0.0")
        for path in ("/api/walletflow/viz/flow_series",
                     "/api/copytrade/viz/skill"):
            r = await client.get(path)
            assert r.status == 403, f"{path} should 403 off-loopback"
        monkeypatch.setattr(settings, "WEB_UI_HOST", "localhost")
        for path in ("/api/walletflow/viz/flow_series",
                     "/api/copytrade/viz/skill"):
            r = await client.get(path)
            assert r.status == 200
            assert (await r.json())["ok"] is True
    finally:
        await client.close()


# ─────────────────────────────────────────────────────────────────────────
# 3b. Each /viz/ endpoint returns DERIVED data from stored events
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_walletflow_viz_derives_from_events(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "WEB_UI_HOST", "localhost")
    _seed_wallet_flow()
    _seed_watchlist()
    ws, client = await _client()
    try:
        d = await (await client.get("/api/walletflow/viz/flow_series")).json()
        assert d["ok"] is True
        # series derived from the three seeded flow events
        assert d["series"]["n_events"] == 3
        assert any(b["events"] for b in d["series"]["buckets"])
        # net of the populated bucket = 1000 + 500 inflow − 300 outflow = +1200
        net_total = sum(b["net_usd"] for b in d["series"]["buckets"])
        assert round(net_total, 2) == 1200.0
        # provenance counts derived from the watchlist rows
        c = d["provenance"]["counts"]
        assert c["confirmed"] == 2 and c["candidate"] == 1
        assert c["manual"] == 1 and c["rejected"] == 1
        assert d["provenance"]["expiring_soon"] == 1   # only conf_soon
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_copytrade_viz_derives_from_population(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "WEB_UI_HOST", "localhost")
    _seed_copytrade()
    ws, client = await _client()
    try:
        d = await (await client.get("/api/copytrade/viz/skill")).json()
        assert d["ok"] is True
        # one point per skilled actor, carrying sample + δ + followable
        assert len(d["points"]) == 3
        p = {pt["actor_id"]: pt for pt in d["points"]}
        assert p["A_good"]["followable"] is True
        assert p["A_unfoll"]["followable"] is False
        for pt in d["points"]:
            assert "sample" in pt and "delta_s" in pt   # uncertainty fields
        # denominator is the FULL evaluated population, not a winners reel
        den = d["denominator"]
        assert den["evaluated"] == 6 and den["surfaced"] == 3
        assert den["rejected"] == 3
        assert den["rejection_reasons"].get("below_min_sample") == 2
    finally:
        await client.close()


# ─────────────────────────────────────────────────────────────────────────
# 4. As-of view reuses the ONE shared point-in-time fn — no second path
# ─────────────────────────────────────────────────────────────────────────

def test_asof_reuses_single_shared_fn():
    import inspect
    from follow import meme_scorer
    from follow.meme_scorer import rug_rate_as_of as direct
    # the module attribute IS the single definition
    assert meme_scorer.rug_rate_as_of is direct
    # the replay harness routes through that same fn (not a copy)
    assert "rug_rate_as_of" in inspect.getsource(meme_scorer.run_replay)
    # the web layer introduces NO second rug-rate implementation
    import ui.web_server as wsmod
    assert not [n for n in dir(wsmod) if "rug_rate" in n.lower()], \
        "web_server must not define its own rug-rate path"
    # likewise the wallet/copytrade as-of routes through skill_scorer's one fn
    from follow import skill_scorer
    assert callable(skill_scorer.resolved_actions_as_of)


# ─────────────────────────────────────────────────────────────────────────
# 5. Empty-state — every viz endpoint answers ok + zeroed with no data
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_viz_empty_state(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "WEB_UI_HOST", "localhost")
    ws, client = await _client()
    try:
        d = await (await client.get("/api/walletflow/viz/flow_series")).json()
        assert d["ok"] is True
        assert d["series"]["n_events"] == 0
        # buckets still present (zeroed) so the chart renders an empty frame
        assert len(d["series"]["buckets"]) == int(
            getattr(settings, "WEB_UI_VIZ_FLOW_BUCKETS", 24))
        assert all(b["net_usd"] == 0 for b in d["series"]["buckets"])
        assert d["provenance"]["total"] == 0

        d2 = await (await client.get("/api/copytrade/viz/skill")).json()
        assert d2["ok"] is True
        assert d2["points"] == []
        assert d2["denominator"]["evaluated"] == 0
    finally:
        await client.close()


# ─────────────────────────────────────────────────────────────────────────
# 6. Uncertainty markers — the data the charts consume carries them
# ─────────────────────────────────────────────────────────────────────────

def test_meme_asof_carries_resolved_vs_censored(_temp_db):
    # rug_rate_as_of (the shared fn the meme as-of view renders) always exposes
    # resolved-vs-censored + hard/soft so the chart can show uncertainty as
    # prominently as the rate — even for an unknown funder.
    from follow import meme_scorer
    rr = meme_scorer.rug_rate_as_of("unknown_funder", datetime.utcnow())
    for k in ("rate", "resolved_sample", "censored", "hard_rugs",
              "soft_rugs", "confidence"):
        assert k in rr
