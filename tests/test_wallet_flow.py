"""
tests/test_wallet_flow.py — Wallet + Exchange-Flow Watcher full suite.

Covers (mirrors the build spec's TESTS list):
 1. plugin compliance — capital-free observer, no submit/execute attribute
 2. streaming lifecycle — start/stop clean; is_available() false w/o key/labels
 3. INVARIANT — discovery can ONLY write candidate (never confirmed)
 4. INVARIANT — rejected never re-proposed
 5. signals only from manual/confirmed (a candidate's activity emits nothing)
 6. point-in-time no-leak (resolved_at >= T excluded; flip across T changes score)
 7. shared query — scorer + as-of endpoint use ONE function
 8. min-sample fatal gate -> NO-SIGNAL
 9. discovery stricter sample gate
10. promotion gate AND-logic (failing any one keeps it out)
11. bait warnings attach, don't auto-reject
12. trust expiry -> auto-demote
13. auto-demote breaker -> demote + flag
14. privacy guard — 403 off-loopback host
15. snapshot safety — complete dict when watcher raises / is None
16. exchange-flow events — transfer_in + net-flow aggregation
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config import settings
from database import queries as q
from follow import provenance, skill_scorer, discovery, labels
from follow.wallet_flow import WalletFlowWatcher


# ─────────────────────────────────────────────────────────────────────────
# Isolated SQLite for every test (never touches data/cryptobot.db)
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def _temp_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import db as db_module
    from database.models import Base

    path = tmp_path / "walletflow_test.db"
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


def _add_action(addr, *, outcome="win", occurred=None, detected=None,
                resolved=None, first_mover=True, action="swap", venue=None,
                asset="MINT", size_usd=100.0, funder=None):
    """Insert one resolved (or unresolved) flow event with both clocks set."""
    now = _now()
    occurred = occurred or now - timedelta(hours=2)
    detected = detected or (occurred + timedelta(seconds=5))
    meta = {"first_mover": first_mover}
    if funder:
        meta["funder"] = funder
    return q.insert_wallet_flow_event({
        "source_id": "wallet_flow", "actor_id": addr, "action": action,
        "asset": asset, "venue": venue, "size_usd": size_usd,
        "occurred_at": occurred, "detected_at": detected,
        "resolved_at": resolved, "outcome": (outcome if resolved else None),
        "meta": meta,
    })


def _seed_resolved(addr, n, *, wins=None, first_mover=True, latency_s=5,
                   resolved_before=None):
    """Seed n resolved actions for a wallet. `wins` of them win (default all)."""
    wins = n if wins is None else wins
    base = _now() - timedelta(days=10)
    rb = resolved_before or (_now() - timedelta(hours=1))
    for i in range(n):
        occ = base + timedelta(minutes=i)
        _add_action(
            addr, outcome=("win" if i < wins else "loss"),
            occurred=occ, detected=occ + timedelta(seconds=latency_s),
            resolved=rb - timedelta(minutes=(n - i)), first_mover=first_mover)


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
    # The host agent is registered as an observer.
    assert any(a.agent_id == "follow" for a in REGISTERED_AGENTS)

    # The source is a streaming source with NO execution method anywhere.
    assert REGISTERED_FOLLOW_SOURCES, "no follow sources registered"
    for s in REGISTERED_FOLLOW_SOURCES:
        assert isinstance(s, BaseStreamingDataSource)
        for forbidden in ("submit", "execute", "place_order", "trade",
                          "close_all_positions", "route_order"):
            assert not hasattr(s, forbidden), \
                f"observer source must not have {forbidden}"


def test_close_all_positions_is_noop():
    from agents.follow_agent import FollowAgent
    # The kill-switch path must be a clean no-op (nothing to close).
    asyncio.run(FollowAgent().close_all_positions())


# ─────────────────────────────────────────────────────────────────────────
# 2. Streaming lifecycle
# ─────────────────────────────────────────────────────────────────────────

def test_is_available_false_without_key_or_labels(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "WALLETFLOW_ENABLED", True)
    monkeypatch.delenv(settings.WALLETFLOW_HELIUS_KEY_ENV, raising=False)
    w = WalletFlowWatcher()
    # No key -> unavailable regardless of labels.
    assert w.is_available() is False
    # With a key but no labels loaded -> still unavailable.
    monkeypatch.setenv(settings.WALLETFLOW_HELIUS_KEY_ENV, "k")
    assert w.is_available() is False
    # Once the shared label set is loaded, available.
    labels.load_seed_labels()
    assert w.is_available() is True
    # Master switch off -> unavailable even fully configured.
    monkeypatch.setattr(settings, "WALLETFLOW_ENABLED", False)
    assert w.is_available() is False


def test_start_stop_clean_when_unavailable(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "WALLETFLOW_ENABLED", False)
    w = WalletFlowWatcher()

    async def _run():
        await w.start()           # must not raise even though unavailable
        assert w._running is False
        await w.stop()            # idempotent / clean
    asyncio.run(_run())


# ─────────────────────────────────────────────────────────────────────────
# 3. INVARIANT — discovery can only write candidate
# ─────────────────────────────────────────────────────────────────────────

def test_invariant_discovery_cannot_confirm(_temp_db):
    # DB-layer guard: a discovery actor can never reach "confirmed".
    res = q.transition_provenance("W", "confirmed", by="discovery")
    assert res["ok"] is False
    assert res["error"] == "transition_forbidden"
    # And there is no provenance helper that lets discovery confirm — confirm()
    # hard-wires by=BY_OPERATOR and exposes no actor parameter to override it.
    src = inspect.getsource(provenance.confirm)
    assert "by=BY_OPERATOR" in src
    assert "by" not in inspect.signature(provenance.confirm).parameters


def test_invariant_discovery_only_produces_candidate(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "WALLETFLOW_MIN_RESOLVED_SAMPLE", 3)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_SAMPLE", 3)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_SCORE", 0.5)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_FIRSTMOVER", 0.5)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MAX_DELTA_S", 60)
    addr = "GOODwallet"
    _seed_resolved(addr, 5)
    proposed = discovery.run_discovery(universe=[addr])
    assert any(p["address"] == addr for p in proposed)
    entry = q.get_watchlist_entry(addr)
    assert entry is not None and entry["provenance"] == "candidate"
    assert entry["added_by"] == "discovery"


# ─────────────────────────────────────────────────────────────────────────
# 4. INVARIANT — rejected never re-proposed
# ─────────────────────────────────────────────────────────────────────────

def test_invariant_rejected_never_reproposed(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "WALLETFLOW_MIN_RESOLVED_SAMPLE", 3)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_SAMPLE", 3)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_SCORE", 0.5)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_FIRSTMOVER", 0.5)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MAX_DELTA_S", 60)
    addr = "REJwallet"
    _seed_resolved(addr, 5)
    # First pass proposes it; operator rejects it.
    assert discovery.run_discovery(universe=[addr])
    assert provenance.reject(addr)["ok"] is True
    assert provenance.is_rejected(addr) is True
    # Re-run discovery — the rejected wallet must NOT come back.
    proposed = discovery.run_discovery(universe=[addr])
    assert all(p["address"] != addr for p in proposed)
    assert q.get_watchlist_entry(addr)["provenance"] == "rejected"


# ─────────────────────────────────────────────────────────────────────────
# 5. Signals only from manual/confirmed
# ─────────────────────────────────────────────────────────────────────────

def test_signals_only_from_manual_or_confirmed(_temp_db):
    cand, conf = "CANDwallet", "CONFwallet"
    provenance.propose_candidate(cand)
    provenance.propose_candidate(conf)
    provenance.confirm(conf)

    w = WalletFlowWatcher()
    w.refresh_watchlist()
    assert {cand, conf} <= w._watchlist

    now = datetime.utcnow().timestamp()

    def _swap_tx(payer):
        return {"type": "SWAP", "feePayer": payer, "timestamp": now,
                "events": {"swap": {"tokenOutputs": [{"mint": "X"}]}}}

    # Candidate activity: observed (persisted) but NO signal emitted.
    w._handle_raw_tx(_swap_tx(cand))
    assert w._signals_emitted == 0
    # Confirmed activity: a signal IS emitted.
    w._handle_raw_tx(_swap_tx(conf))
    assert w._signals_emitted == 1
    # Both events were persisted regardless.
    actors = {e["actor_id"] for e in q.get_recent_wallet_flow_events(50)}
    assert {cand, conf} <= actors


# ─────────────────────────────────────────────────────────────────────────
# 6. Point-in-time no-leak
# ─────────────────────────────────────────────────────────────────────────

def test_point_in_time_no_leak(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "WALLETFLOW_MIN_RESOLVED_SAMPLE", 3)
    monkeypatch.setattr(settings, "WALLETFLOW_MAX_FOLLOWABLE_DELTA_S", 60)
    addr = "PITwallet"
    T = _now()
    base = T - timedelta(days=5)
    # 4 wins resolved before T, 1 loss resolved before T.
    for i in range(4):
        _add_action(addr, outcome="win", occurred=base + timedelta(minutes=i),
                    detected=base + timedelta(minutes=i, seconds=5),
                    resolved=T - timedelta(hours=1))
    loss_id = _add_action(addr, outcome="loss",
                          occurred=base + timedelta(minutes=10),
                          detected=base + timedelta(minutes=10, seconds=5),
                          resolved=T - timedelta(hours=1))

    s1 = skill_scorer.score_wallet(addr, T)
    assert s1["resolved_sample"] == 5
    assert s1["skill_score"] == pytest.approx(4 / 5)

    # Move the loss's outcome clock to AFTER T -> it must drop out of the score.
    q.resolve_wallet_flow_event(loss_id, T + timedelta(hours=1), "loss")
    s2 = skill_scorer.score_wallet(addr, T)
    assert s2["resolved_sample"] == 4           # leak-free: resolved_at >= T excluded
    assert s2["skill_score"] == pytest.approx(1.0)
    assert s2["skill_score"] > s1["skill_score"]


# ─────────────────────────────────────────────────────────────────────────
# 7. Shared query — one definition, reused
# ─────────────────────────────────────────────────────────────────────────

def test_shared_point_in_time_function():
    # Discovery routes through the scorer's shared fn (import identity).
    assert discovery.resolved_actions_as_of is skill_scorer.resolved_actions_as_of
    # Exactly one definition of the as-of filter, and it lives in skill_scorer.
    scorer_src = inspect.getsource(skill_scorer)
    assert scorer_src.count("def resolved_actions_as_of") == 1
    assert "resolved_at < as_of" in scorer_src or "ra < as_of" in scorer_src
    # The web as-of inspector calls the shared fn — it does not reimplement it.
    from ui import web_server
    web_src = inspect.getsource(web_server)
    assert "skill_scorer.resolved_actions_as_of" in web_src
    assert "def resolved_actions_as_of" not in web_src


# ─────────────────────────────────────────────────────────────────────────
# 8. Min-sample fatal gate
# ─────────────────────────────────────────────────────────────────────────

def test_min_sample_gate_no_signal(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "WALLETFLOW_MIN_RESOLVED_SAMPLE", 30)
    addr = "THINwallet"
    _seed_resolved(addr, 5)                      # below the gate
    s = skill_scorer.score_wallet(addr)
    assert s["signal"] is False
    assert s["skill_score"] is None
    assert s["reason"] == "below_min_resolved_sample"


# ─────────────────────────────────────────────────────────────────────────
# 9. Discovery stricter sample gate
# ─────────────────────────────────────────────────────────────────────────

def test_discovery_stricter_than_manual(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "WALLETFLOW_MIN_RESOLVED_SAMPLE", 3)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_SAMPLE", 10)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_SCORE", 0.5)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_FIRSTMOVER", 0.5)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MAX_DELTA_S", 60)
    addr = "MIDwallet"
    _seed_resolved(addr, 5)                      # passes manual (3), below discovery (10)
    # Manual scorer would give a signal...
    assert skill_scorer.score_wallet(addr)["signal"] is True
    # ...but discovery's stricter gate does NOT promote it.
    proposed = discovery.run_discovery(universe=[addr])
    assert all(p["address"] != addr for p in proposed)
    assert q.get_watchlist_entry(addr) is None


# ─────────────────────────────────────────────────────────────────────────
# 10. Promotion gate AND-logic
# ─────────────────────────────────────────────────────────────────────────

def test_promotion_gate_and_logic(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_SAMPLE", 5)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_SCORE", 0.6)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_FIRSTMOVER", 0.6)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MAX_DELTA_S", 30)

    # fail sample
    _seed_resolved("failSample", 3, wins=3, first_mover=True, latency_s=5)
    # fail score (low win rate)
    _seed_resolved("failScore", 10, wins=2, first_mover=True, latency_s=5)
    # fail first-mover
    _seed_resolved("failFM", 10, wins=10, first_mover=False, latency_s=5)
    # fail latency-δ (stale)
    _seed_resolved("failDelta", 10, wins=10, first_mover=True, latency_s=120)

    universe = ["failSample", "failScore", "failFM", "failDelta"]
    proposed = {p["address"] for p in discovery.run_discovery(universe=universe)}
    assert proposed == set(), f"none should pass, got {proposed}"
    for a in universe:
        assert q.get_watchlist_entry(a) is None


# ─────────────────────────────────────────────────────────────────────────
# 11. Bait warnings attach, don't auto-reject
# ─────────────────────────────────────────────────────────────────────────

def test_bait_warnings_attach_not_reject(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_SAMPLE", 5)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_SCORE", 0.6)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MIN_FIRSTMOVER", 0.6)
    monkeypatch.setattr(settings, "WALLETFLOW_DISCOVERY_MAX_DELTA_S", 30)
    monkeypatch.setattr(settings, "WALLETFLOW_TOO_CLEAN_WINRATE", 0.98)
    addr = "TOOCLEANwallet"
    _seed_resolved(addr, 10, wins=10, first_mover=True, latency_s=5)  # 100% wins
    proposed = discovery.run_discovery(universe=[addr])
    # Still proposed (warnings never auto-reject).
    assert any(p["address"] == addr for p in proposed)
    cand = next(p for p in proposed if p["address"] == addr)
    flags = {w["flag"] for w in cand["warnings"]}
    assert "too_clean" in flags
    # Meme rug-rate hook is unavailable -> bad_cluster defaults to "unknown".
    bad = next(w for w in cand["warnings"] if w["flag"] == "bad_cluster")
    assert bad["severity"] == "unknown"
    # It appears in the operator review queue with warnings set.
    pending = q.get_pending_candidates()
    assert any(c["address"] == addr and c["warnings"] for c in pending)


# ─────────────────────────────────────────────────────────────────────────
# 12. Trust expiry -> auto-demote
# ─────────────────────────────────────────────────────────────────────────

def test_trust_expiry_demotes(_temp_db, monkeypatch):
    # Days arm: confirm with a horizon already in the past.
    monkeypatch.setattr(settings, "WALLETFLOW_TRUST_EXPIRY_DAYS", -1)
    monkeypatch.setattr(settings, "WALLETFLOW_TRUST_EXPIRY_ACTIONS", 9999)
    addr = "EXPIREwallet"
    provenance.propose_candidate(addr)
    provenance.confirm(addr)
    assert q.get_watchlist_entry(addr)["provenance"] == "confirmed"
    res = provenance.enforce_trust_expiry(addr)
    assert res is not None and res["ok"] is True
    e = q.get_watchlist_entry(addr)
    assert e["provenance"] == "candidate"
    assert e["last_demote_reason"] == "trust_expiry_days"

    # Actions arm: future days horizon, but action count exceeds the cap.
    monkeypatch.setattr(settings, "WALLETFLOW_TRUST_EXPIRY_DAYS", 30)
    monkeypatch.setattr(settings, "WALLETFLOW_TRUST_EXPIRY_ACTIONS", 2)
    addr2 = "ACTIONwallet"
    provenance.propose_candidate(addr2)
    provenance.confirm(addr2)
    q.bump_wallet_actions(addr2, 3)
    res2 = provenance.enforce_trust_expiry(addr2)
    assert res2 is not None and res2["ok"] is True
    assert q.get_watchlist_entry(addr2)["last_demote_reason"] == "trust_expiry_actions"


# ─────────────────────────────────────────────────────────────────────────
# 13. Auto-demote breaker
# ─────────────────────────────────────────────────────────────────────────

def test_auto_demote_breaker(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "WALLETFLOW_DEMOTE_BAD_STREAK", 3)
    addr = "STREAKwallet"
    provenance.propose_candidate(addr)
    provenance.confirm(addr)
    # Two losses is under the streak — no demote.
    assert provenance.enforce_demote_breaker(addr, ["loss", "loss"]) is None
    assert q.get_watchlist_entry(addr)["provenance"] == "confirmed"
    # Three consecutive losses trips it.
    res = provenance.enforce_demote_breaker(addr, ["loss", "loss", "loss", "win"])
    assert res is not None and res["ok"] is True
    e = q.get_watchlist_entry(addr)
    assert e["provenance"] == "candidate"
    assert e["last_demote_reason"].startswith("demote_breaker")


# ─────────────────────────────────────────────────────────────────────────
# 14. Privacy guard — 403 off-loopback host
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
        for path in ("/api/walletflow/candidates", "/api/walletflow/health",
                     "/api/walletflow/wallet/ABC"):
            r = await client.get(path)
            assert r.status == 403, f"{path} should 403 off-loopback"
        r = await client.post("/api/walletflow/candidate/confirm",
                              json={"address": "ABC"})
        assert r.status == 403
        # Loopback host -> not forbidden.
        monkeypatch.setattr(settings, "WEB_UI_HOST", "localhost")
        r = await client.get("/api/walletflow/candidates")
        assert r.status == 200
        body = await r.json()
        assert body["ok"] is True
    finally:
        await client.close()


# ─────────────────────────────────────────────────────────────────────────
# 15. Snapshot safety
# ─────────────────────────────────────────────────────────────────────────

def test_snapshot_complete_when_agent_none():
    from ui.web_server import WebServer
    ws = WebServer(coordinator=None, bot=None)
    block = ws._snap_walletflow()
    for k in ("enabled", "running", "flow_events", "netflow_spikes",
              "pending_candidates", "label_staleness", "sources"):
        assert k in block


def test_snapshot_complete_when_agent_raises():
    from ui.web_server import WebServer

    class _Boom:
        def get_walletflow_snapshot(self):
            raise RuntimeError("boom")

    coord = SimpleNamespace(get_agent=lambda _id: _Boom())
    ws = WebServer(coordinator=coord, bot=None)
    block = ws._snap_walletflow()                # must not raise
    assert isinstance(block, dict)
    assert "flow_events" in block and "label_staleness" in block


# ─────────────────────────────────────────────────────────────────────────
# 16. Exchange-flow events + net-flow aggregation
# ─────────────────────────────────────────────────────────────────────────

def test_exchange_flow_event_and_netflow(_temp_db):
    wallet = "FLOWwallet"
    exchange = "EXCHANGEaddr"
    q.upsert_exchange_label(exchange, "exchange", exchange_name="binance",
                            source="seed")

    w = WalletFlowWatcher()
    w._watchlist = {wallet}                       # observe this wallet
    now = datetime.utcnow().timestamp()
    tx = {
        "type": "TRANSFER", "feePayer": wallet, "timestamp": now,
        "signature": "sig1",
        "tokenTransfers": [{
            "fromUserAccount": wallet, "toUserAccount": exchange,
            "mint": "SOL", "tokenAmount": 10, "usdValue": 1500.0,
        }],
    }
    events = w._handle_raw_tx(tx)
    # A transfer TO a labelled exchange is classified transfer_in (pre-sell tell).
    assert any(e.action == "transfer_in" for e in events)
    rows = q.get_recent_wallet_flow_events(50)
    ti = [r for r in rows if r["action"] == "transfer_in"]
    assert ti and ti[0]["actor_id"] == wallet and ti[0]["venue"] == "binance"

    # Net-flow aggregates the inflow per (asset, venue, window).
    flows = q.get_wallet_net_flows([1, 24])
    match = [f for f in flows if f["venue"] == "binance" and f["asset"] == "SOL"]
    assert match and any(f["inflow_usd"] >= 1500.0 and f["net_usd"] >= 1500.0
                         for f in match)
