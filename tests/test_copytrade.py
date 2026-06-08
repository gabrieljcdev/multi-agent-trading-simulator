"""
tests/test_copytrade.py — Copy-Trade Leaderboard Observer full suite.

Covers (mirrors the build spec's TESTS list):
 1. plugin compliance — capital-free observer, no execute/submit attribute
 2. venue abstraction — hyperliquid + drift active; gmx/dydx stubs not-implemented
 3. shared point-in-time fn — scorer + as-of endpoint import the SAME function
 4. min-sample (survival) gate — below COPYTRADE_MIN_CLOSED_TRADES -> NO-SIGNAL
 5. latency flag — δ past the window -> "real but unfollowable", not emitted
 6. THE DENOMINATOR LOG — a rejected actor is RECORDED with a reason
 7. point-in-time no-leak — outcomes resolved >= T excluded; flip one -> score changes
 8. corroboration hook — assessment readable by the view; NO spot logic in module
 9. privacy guard — /api/copytrade/* returns 403 off-loopback host
10. snapshot safety — complete dict when the observer is None / raises
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config import settings
from database import queries as q
from follow import copytrade_scorer, skill_scorer, corroboration
from follow.copytrade import (
    CopyTradeObserver, build_venues, Position,
    HyperliquidVenue, DriftVenue, GMXVenue, DyDxVenue,
)


# ─────────────────────────────────────────────────────────────────────────
# Isolated SQLite for every test (never touches data/cryptobot.db)
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _restore_event_loop():
    """Re-arm a live current event loop after each test.

    Several tests here call asyncio.run(), which sets the thread's current event
    loop to None on completion. Test files that follow alphabetically and use the
    deprecated asyncio.get_event_loop() (e.g. the cross-chain engine helpers)
    would otherwise fail with 'no current event loop' purely as a function of
    test ORDER. Restoring a loop on teardown keeps the suite order-independent —
    the same way the loop existed before this module was inserted."""
    yield
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture
def _temp_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import db as db_module
    from database.models import Base

    path = tmp_path / "copytrade_test.db"
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


def _now():
    return datetime.utcnow()


def _close(actor, *, outcome="win", occurred=None, detected=None, resolved=None,
           latency_s=5):
    """Insert one resolved CLOSE into the SHARED track-record log (source_id=
    'copytrade') — the substrate the shared point-in-time filter reads."""
    now = _now()
    occurred = occurred or now - timedelta(hours=2)
    detected = detected or (occurred + timedelta(seconds=latency_s))
    return q.insert_wallet_flow_event({
        "source_id": "copytrade", "actor_id": actor, "action": "close",
        "asset": "BTC", "venue": "hyperliquid", "size_usd": 1000.0,
        "occurred_at": occurred, "detected_at": detected,
        "resolved_at": resolved, "outcome": (outcome if resolved else None),
        "meta": {},
    })


def _seed_closes(actor, n, *, wins=None, latency_s=5, resolved_before=None):
    """Seed n resolved closed trades for an actor. `wins` of them win (default
    all). Wins precede losses so the drawdown proxy stays shallow."""
    wins = n if wins is None else wins
    base = _now() - timedelta(days=10)
    rb = resolved_before or (_now() - timedelta(hours=1))
    for i in range(n):
        occ = base + timedelta(minutes=i)
        _close(actor, outcome=("win" if i < wins else "loss"),
               occurred=occ, detected=occ + timedelta(seconds=latency_s),
               resolved=rb - timedelta(minutes=(n - i)))


# ─────────────────────────────────────────────────────────────────────────
# 1. Plugin compliance — capital-free observer, no execution path
# ─────────────────────────────────────────────────────────────────────────

def test_plugin_compliance_observer_no_execution():
    from agents.follow_agent import FollowAgent
    from agents import REGISTERED_AGENTS
    from follow import REGISTERED_FOLLOW_SOURCES
    from follow.base import BaseStreamingDataSource

    fa = FollowAgent()
    assert fa.capital_allocation == 0.0
    assert fa.observation_mode is True
    assert any(a.agent_id == "follow" for a in REGISTERED_AGENTS)

    # The copy-trade observer is registered as a streaming source...
    obs = [s for s in REGISTERED_FOLLOW_SOURCES if s.source_id == "copytrade"]
    assert obs, "copytrade observer not registered"
    o = obs[0]
    assert isinstance(o, BaseStreamingDataSource)
    # ...with NO execution / auto-follow method anywhere.
    for forbidden in ("submit", "execute", "place_order", "trade",
                      "route_order", "close_all_positions", "follow",
                      "auto_follow", "open_order"):
        assert not hasattr(o, forbidden), \
            f"observer source must not have {forbidden}"


# ─────────────────────────────────────────────────────────────────────────
# 2. Venue abstraction — hyperliquid + drift active; gmx/dydx stubbed
# ─────────────────────────────────────────────────────────────────────────

def test_venue_abstraction_active_and_stubs():
    venues = {v.venue_id: v for v in
              build_venues(["hyperliquid", "drift", "gmx", "dydx"])}
    # Implemented venues are available.
    assert venues["hyperliquid"].implemented is True
    assert venues["hyperliquid"].is_available() is True
    assert venues["drift"].implemented is True
    assert venues["drift"].is_available() is True
    # Stubs report NOT-implemented / unavailable.
    assert venues["gmx"].implemented is False
    assert venues["gmx"].is_available() is False
    assert venues["dydx"].implemented is False
    assert venues["dydx"].is_available() is False

    # Every backend shares the read-only seam — and NONE has an execute path.
    for v in venues.values():
        assert hasattr(v, "fetch_leaderboard") and hasattr(v, "fetch_positions")
        for forbidden in ("submit", "execute", "place_order", "order"):
            assert not hasattr(v, forbidden)

    # Stubs yield nothing (inert), never raise.
    assert asyncio.run(GMXVenue().fetch_leaderboard()) == []
    assert asyncio.run(DyDxVenue().fetch_positions("X")) == []


# ─────────────────────────────────────────────────────────────────────────
# 3. Shared point-in-time function — one definition, reused (identity)
# ─────────────────────────────────────────────────────────────────────────

def test_shared_point_in_time_function():
    # The scorer routes through the wallet scorer's shared fn (import identity).
    assert copytrade_scorer.resolved_actions_as_of is skill_scorer.resolved_actions_as_of
    # The copy-trade scorer does NOT re-define an as-of filter — it imports it.
    scorer_src = inspect.getsource(copytrade_scorer)
    assert scorer_src.count("def resolved_actions_as_of") == 0
    assert "from follow.skill_scorer import resolved_actions_as_of" in scorer_src
    # The web as-of inspector calls the SAME shared fn — not a reimplementation.
    from ui import web_server
    web_src = inspect.getsource(web_server)
    assert "copytrade_scorer.score_actor" in web_src
    assert "skill_scorer.resolved_actions_as_of" in web_src


# ─────────────────────────────────────────────────────────────────────────
# 4. Min-sample (survival) gate -> NO-SIGNAL
# ─────────────────────────────────────────────────────────────────────────

def test_min_sample_gate_no_signal(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "COPYTRADE_MIN_CLOSED_TRADES", 30)
    actor = "THINactor"
    _seed_closes(actor, 5)                        # below the survival gate
    s = copytrade_scorer.score_actor(actor)
    assert s["signal"] is False
    assert s["skill_score"] is None
    assert s["reason"] == copytrade_scorer.REASON_BELOW_MIN_CLOSED


# ─────────────────────────────────────────────────────────────────────────
# 5. Latency flag -> "real but unfollowable", not emitted
# ─────────────────────────────────────────────────────────────────────────

def test_latency_real_but_unfollowable(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "COPYTRADE_MIN_CLOSED_TRADES", 3)
    monkeypatch.setattr(settings, "COPYTRADE_MAX_FOLLOWABLE_DELTA_S", 10)
    monkeypatch.setattr(settings, "COPYTRADE_MAX_DRAWDOWN", 0.5)
    actor = "STALEactor"
    _seed_closes(actor, 5, latency_s=120)         # edge dies inside the window
    s = copytrade_scorer.score_actor(actor)
    assert s["skill_score"] is not None           # genuinely skilled...
    assert s["followable"] is False               # ...but latency-killed
    assert s["real_but_unfollowable"] is True
    assert s["signal"] is False                   # NOT emitted as followable
    assert s["reason"] == copytrade_scorer.REASON_DELTA_TOO_HIGH


# ─────────────────────────────────────────────────────────────────────────
# 6. THE DENOMINATOR LOG — a rejected actor is recorded with a reason
# ─────────────────────────────────────────────────────────────────────────

def test_denominator_records_rejected(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "COPYTRADE_MIN_CLOSED_TRADES", 30)
    obs = CopyTradeObserver(venues=[])            # no network needed
    rej = "REJECTEDactor"
    _seed_closes(rej, 5)                          # below the survival gate
    score = obs.evaluate_actor(rej, "hyperliquid")
    assert score["signal"] is False              # rejected by the gate

    den = q.get_copytrade_denominator()
    # The FULL evaluated population is recorded — including the rejected actor.
    assert den["evaluated"] >= 1
    assert den["rejected"] >= 1
    assert copytrade_scorer.REASON_BELOW_MIN_CLOSED in den["rejection_reasons"]
    # The specific evaluation row carries the reason.
    assert any(e["actor_id"] == rej and e["decision"] == "rejected"
               and e["reason"] == copytrade_scorer.REASON_BELOW_MIN_CLOSED
               for e in den["evaluations"])

    # A surfaced (skilled) actor is recorded too — the denominator is a
    # population view, not a winners-only highlight reel.
    monkeypatch.setattr(settings, "COPYTRADE_MIN_CLOSED_TRADES", 3)
    good = "GOODactor"
    _seed_closes(good, 5)
    obs.evaluate_actor(good, "hyperliquid")
    den2 = q.get_copytrade_denominator()
    assert den2["surfaced"] >= 1
    assert any(e["actor_id"] == good and e["decision"] == "surfaced"
               for e in den2["evaluations"])


def test_drawdown_gate_rejects_as_no_signal(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "COPYTRADE_MIN_CLOSED_TRADES", 3)
    monkeypatch.setattr(settings, "COPYTRADE_MAX_DRAWDOWN", 0.5)
    actor = "HOTactor"
    # 5 wins then 5 losses -> equity peaks at 5 and falls to 0 -> drawdown 1.0.
    _seed_closes(actor, 10, wins=5)
    s = copytrade_scorer.score_actor(actor)
    assert s["signal"] is False
    assert s["skill_score"] is None              # rejection carries no skill estimate
    assert s["reason"] == copytrade_scorer.REASON_DRAWDOWN_TOO_DEEP
    # And it lands in the denominator as REJECTED, not surfaced.
    obs = CopyTradeObserver(venues=[])
    obs.evaluate_actor(actor, "hyperliquid")
    den = q.get_copytrade_denominator()
    assert copytrade_scorer.REASON_DRAWDOWN_TOO_DEEP in den["rejection_reasons"]


# ─────────────────────────────────────────────────────────────────────────
# 7. Point-in-time no-leak — outcomes resolved >= T excluded
# ─────────────────────────────────────────────────────────────────────────

def test_point_in_time_no_leak(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "COPYTRADE_MIN_CLOSED_TRADES", 3)
    monkeypatch.setattr(settings, "COPYTRADE_MAX_FOLLOWABLE_DELTA_S", 60)
    monkeypatch.setattr(settings, "COPYTRADE_MAX_DRAWDOWN", 0.9)
    actor = "PITactor"
    T = _now()
    base = T - timedelta(days=5)
    # 4 wins resolved before T...
    for i in range(4):
        _close(actor, outcome="win", occurred=base + timedelta(minutes=i),
               detected=base + timedelta(minutes=i, seconds=5),
               resolved=T - timedelta(hours=1))
    # ...and 1 loss resolved before T.
    loss_id = _close(actor, outcome="loss", occurred=base + timedelta(minutes=10),
                     detected=base + timedelta(minutes=10, seconds=5),
                     resolved=T - timedelta(hours=1))

    s1 = copytrade_scorer.score_actor(actor, T)
    assert s1["closed_trades"] == 5
    assert s1["skill_score"] == pytest.approx(4 / 5)

    # Move the loss's outcome clock to AFTER T -> it drops out of the score.
    q.resolve_wallet_flow_event(loss_id, T + timedelta(hours=1), "loss")
    s2 = copytrade_scorer.score_actor(actor, T)
    assert s2["closed_trades"] == 4              # leak-free: resolved_at >= T excluded
    assert s2["skill_score"] == pytest.approx(1.0)
    assert s2["skill_score"] > s1["skill_score"]


# ─────────────────────────────────────────────────────────────────────────
# 8. Corroboration hook readable + NO spot logic in this module
# ─────────────────────────────────────────────────────────────────────────

def test_corroboration_hook_and_no_spot_logic(_temp_db):
    from follow import copytrade as copytrade_module

    actor = "CROSSactor"
    # Persist a skilled perp assessment (what evaluate_actor would write).
    q.upsert_copytrade_actor({
        "actor_id": actor, "venue": "hyperliquid", "closed_trades": 60,
        "skill_score": 0.7, "latency_delta_s": 8.0, "drawdown": 0.1,
        "followable": True, "sizing_interpretable": True,
    })
    obs = CopyTradeObserver(venues=[])
    perp = obs.skilled_actor_assessments()
    assert any(a["actor_id"] == actor for a in perp)

    # The corroboration VIEW (not the observer) does the cross-surface join.
    spot = [{"address": actor, "skill_score": 0.6}]   # same actor on spot
    view = corroboration.build_view(perp_source=obs, spot_assessments=spot)
    rows = {r["actor_id"]: r for r in view["rows"]}
    assert actor in rows
    assert rows[actor]["cross_surface"] is True       # skilled on BOTH surfaces
    assert view["n_cross"] >= 1

    # HARD: no spot smart-money detection lives in the copy-trade module — that
    # is the wallet watcher's job and stays there; corroboration crosses in the
    # view only.
    src = inspect.getsource(copytrade_module)
    for spot_token in ("transfer_in", "transfer_out", "accumulation",
                       "is_terminal", "smart_money", "exchange_label",
                       "load_seed_labels"):
        assert spot_token not in src, \
            f"copy-trade module must not contain spot logic ({spot_token})"


# ─────────────────────────────────────────────────────────────────────────
# 9. Privacy guard — 403 off-loopback host
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_privacy_guard_403_off_localhost(_temp_db, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from ui.web_server import WebServer

    ws = WebServer(coordinator=None, bot=None)
    client = TestClient(TestServer(ws._make_app()))
    await client.start_server()
    try:
        monkeypatch.setattr(settings, "WEB_UI_HOST", "0.0.0.0")
        for path in ("/api/copytrade/actor/ABC", "/api/copytrade/surfaced",
                     "/api/copytrade/denominator", "/api/copytrade/health"):
            r = await client.get(path)
            assert r.status == 403, f"{path} should 403 off-loopback"
        # Loopback host -> not forbidden.
        monkeypatch.setattr(settings, "WEB_UI_HOST", "localhost")
        r = await client.get("/api/copytrade/denominator")
        assert r.status == 200
        body = await r.json()
        assert body["ok"] is True
    finally:
        await client.close()


# ─────────────────────────────────────────────────────────────────────────
# 10. Snapshot safety — complete dict when observer is None / raises
# ─────────────────────────────────────────────────────────────────────────

def test_snapshot_complete_when_agent_none():
    from ui.web_server import WebServer
    ws = WebServer(coordinator=None, bot=None)
    block = ws._snap_copytrade()
    for k in ("enabled", "running", "events", "surfaced_actors",
              "denominator", "sources", "scope_note"):
        assert k in block


def test_snapshot_complete_when_agent_raises():
    from ui.web_server import WebServer

    class _Boom:
        def get_copytrade_snapshot(self):
            raise RuntimeError("boom")

    coord = SimpleNamespace(get_agent=lambda _id: _Boom())
    ws = WebServer(coordinator=coord, bot=None)
    block = ws._snap_copytrade()                  # must not raise
    assert isinstance(block, dict)
    assert "denominator" in block and "surfaced_actors" in block


# ─────────────────────────────────────────────────────────────────────────
# Extra — observer lifecycle clean when disabled / unavailable
# ─────────────────────────────────────────────────────────────────────────

def test_start_stop_clean_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "COPYTRADE_ENABLED", False)
    o = CopyTradeObserver()

    async def _run():
        await o.start()                           # must not raise though disabled
        assert o._running is False
        await o.stop()                            # idempotent / clean
    asyncio.run(_run())


def test_ingest_positions_persists_events(_temp_db):
    o = CopyTradeObserver(venues=[])
    actor = "POSactor"
    positions = [Position(actor_id=actor, venue="hyperliquid", asset="BTC",
                          side="long", notional_usd=50_000.0, leverage=5.0,
                          entry_price=100.0, liq_distance=0.2, pnl_usd=10.0)]
    events = o.ingest_positions("hyperliquid", actor, positions)
    assert any(e.action == "open" for e in events)
    rows = q.get_recent_copytrade_events(50)
    assert any(r["actor_id"] == actor and r["action"] == "open"
               and r["notional_usd"] == 50_000.0 for r in rows)
