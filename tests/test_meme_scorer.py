"""
tests/test_meme_scorer.py — Meme-Coin Cluster-Pattern Rug-Rate Scorer suite.

Mirrors the build spec's TESTS list (prompts/build_meme_scorer.md):
 1. plugin compliance — capital-free observer, NO submit/execute attribute
 2. point-in-time no-leak — rate at T excludes resolved_at >= T; flip across T
 3. shared as-of fn — scorer + replay + UI as-of endpoint use ONE rug_rate_as_of
 4. censoring — a detected-but-unresolved launch is EXCLUDED from the denominator
 5. hard vs soft — irreversible -> hard instant; slow-death -> soft at close; dip recovers -> no label
 6. cluster stop-list — a trace reaching a terminal address stops; exchange-funded not clustered
 7. funder-only merge — wallets sharing a funder cluster; co-buying a token does NOT
 8. decision rule — AVOID needs high rate AND >= min resolved; one-rug abstains; unknown -> NO-SIGNAL
 9. NO-SIGNAL labelling — never represented/rendered as "safe"
10. replay harness — TP/FP/coverage from pre-T state; uses the shared as-of fn; held-out test set
11. sampling seam — LaunchSource interface, SamplingLaunchSource only impl, gap/best-effort surfaced
12. privacy guard — /api/meme/* 403 off-loopback host
13. snapshot safety — complete dict when the observer is None / raises
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config import settings
from database import queries as q
from follow import meme_scorer, sol_parse


# ─────────────────────────────────────────────────────────────────────────
# Isolated SQLite for every test (never touches data/cryptobot.db)
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def _temp_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import db as db_module
    from database.models import Base

    path = tmp_path / "meme_test.db"
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


def _seed_launch(mint, funder, *, detected, resolved=None, outcome=None, conf=None):
    """Insert one launch + a single early-buyer carrying `funder`, optionally
    resolved. Mirrors how the live scorer persists a launch + its cluster."""
    q.upsert_meme_launch({"mint": mint, "detected_at": detected,
                          "source": "sampling", "meta": {}})
    q.insert_meme_launch_buyer({"launch_mint": mint, "wallet": mint + "_buyer",
                                "funder": funder, "buy_at": detected})
    if funder:
        q.upsert_meme_funder(funder)
    if resolved is not None:
        q.resolve_meme_launch(mint, resolved, outcome, conf)


# ── Standard Solana RPC fixture builders (reuse sol_parse's expected shape) ──

def _rpc_tx(*, signers=None, instructions=None, signature="sig", block_time=None):
    bt = block_time if block_time is not None else int(_now().timestamp())
    keys = [{"pubkey": s, "signer": True} for s in (signers or [])]
    return {"blockTime": bt,
            "transaction": {"message": {"accountKeys": keys,
                                        "instructions": instructions or []},
                            "signatures": [signature]}}


def _sys_transfer_ix(src, dst, lamports=1_000_000):
    return {"program": "system", "programId": sol_parse.SYSTEM_PROGRAM_ID,
            "parsed": {"type": "transfer",
                       "info": {"source": src, "destination": dst,
                                "lamports": lamports}}}


def _launch_tx(mint, creator, sig="ls1"):
    """A launchpad create tx: an initializeMint (mint id) + the pump.fun
    launchpad instruction. Exercises sol_parse.launch_events reuse."""
    return {"blockTime": int(_now().timestamp()),
            "transaction": {"message": {
                "accountKeys": [{"pubkey": creator, "signer": True}],
                "instructions": [
                    {"program": "spl-token", "programId": sol_parse.TOKEN_PROGRAM_ID,
                     "parsed": {"type": "initializeMint", "info": {"mint": mint}}},
                    {"programId": sol_parse.LAUNCHPAD_PROGRAM_IDS["pumpfun"],
                     "accounts": [mint], "data": "x"}]},
                "signatures": [sig]}}


# ─────────────────────────────────────────────────────────────────────────
# 1. Plugin compliance — capital-free observer, NO execution path
# ─────────────────────────────────────────────────────────────────────────

def test_meme_plugin_compliance_observer_no_execution():
    from follow import REGISTERED_FOLLOW_SOURCES
    from follow.base import BaseStreamingDataSource
    from follow.meme_scorer import MemeScorer
    from agents.follow_agent import FollowAgent

    fa = FollowAgent()
    assert fa.capital_allocation == 0.0
    assert fa.observation_mode is True
    # The meme source is registered as a streaming observer.
    assert any(isinstance(s, MemeScorer) for s in REGISTERED_FOLLOW_SOURCES)

    m = MemeScorer()
    assert isinstance(m, BaseStreamingDataSource)
    for forbidden in ("submit", "execute", "place_order", "trade",
                      "route_order", "close_all_positions", "follow", "buy"):
        assert not hasattr(m, forbidden), f"observer must not have {forbidden}"
    # The LaunchSource seam also carries no execution method.
    for forbidden in ("submit", "execute", "trade", "place_order"):
        assert not hasattr(meme_scorer.SamplingLaunchSource, forbidden)


# ─────────────────────────────────────────────────────────────────────────
# 2. Point-in-time no-leak
# ─────────────────────────────────────────────────────────────────────────

def test_meme_point_in_time_no_leak(_temp_db):
    T = _now()
    base = T - timedelta(days=5)
    F = "funderF"
    # 4 rugs + 1 survived, ALL resolved before T -> rate 4/5.
    for i in range(4):
        _seed_launch(f"rug{i}", F, detected=base + timedelta(minutes=i),
                     resolved=T - timedelta(hours=1), outcome="rug", conf="hard")
    _seed_launch("surv0", F, detected=base + timedelta(minutes=10),
                 resolved=T - timedelta(hours=1), outcome="survived", conf="hard")

    rr1 = meme_scorer.rug_rate_as_of(F, T)
    assert rr1["resolved_sample"] == 5
    assert rr1["rate"] == pytest.approx(4 / 5)

    # Flip the survived launch's outcome clock to AFTER T -> it must drop out.
    q.resolve_meme_launch("surv0", T + timedelta(hours=1), "survived", "hard")
    rr2 = meme_scorer.rug_rate_as_of(F, T)
    assert rr2["resolved_sample"] == 4          # leak-free: resolved_at >= T excluded
    assert rr2["censored"] >= 1
    assert rr2["rate"] == pytest.approx(1.0)
    assert rr2["rate"] > rr1["rate"]


# ─────────────────────────────────────────────────────────────────────────
# 3. Shared as-of fn — one definition, reused
# ─────────────────────────────────────────────────────────────────────────

def test_meme_shared_as_of_function():
    src = inspect.getsource(meme_scorer)
    # Exactly ONE definition of the as-of rug-rate fn, with the strict boundary.
    assert src.count("def rug_rate_as_of") == 1
    assert "ra < as_of" in src
    # The live scorer (decide) + replay harness + funder_rug_rate all live in the
    # same module as the one definition — they call it, never fork it.
    assert meme_scorer.decide.__module__ == meme_scorer.rug_rate_as_of.__module__
    assert meme_scorer.run_replay.__module__ == meme_scorer.rug_rate_as_of.__module__
    assert meme_scorer.funder_rug_rate.__module__ == meme_scorer.rug_rate_as_of.__module__
    # The web as-of inspector calls the shared fn — it does NOT reimplement it.
    from ui import web_server
    web_src = inspect.getsource(web_server)
    assert "meme_scorer.rug_rate_as_of" in web_src
    assert "def rug_rate_as_of" not in web_src
    # The discovery bait-resistance hook routes through the same module's fn.
    from follow import discovery
    disc_src = inspect.getsource(discovery)
    assert "from follow.meme_scorer import funder_rug_rate" in disc_src


# ─────────────────────────────────────────────────────────────────────────
# 4. Censoring — unresolved launch excluded from the denominator
# ─────────────────────────────────────────────────────────────────────────

def test_meme_censoring_excludes_unresolved(_temp_db):
    T = _now()
    base = T - timedelta(days=3)
    F = "funderC"
    _seed_launch("res1", F, detected=base, resolved=T - timedelta(hours=1),
                 outcome="rug", conf="hard")
    _seed_launch("pending1", F, detected=base)        # detected, NOT resolved

    rr = meme_scorer.rug_rate_as_of(F, T)
    assert rr["resolved_sample"] == 1                 # pending excluded from denominator
    assert rr["censored"] == 1                        # counted as censored, NOT a non-rug
    assert rr["rugs"] == 1
    assert rr["rate"] == pytest.approx(1.0)


# ─────────────────────────────────────────────────────────────────────────
# 5. Hard vs soft labelling (+ momentary dip does not label)
# ─────────────────────────────────────────────────────────────────────────

def test_meme_hard_vs_soft_labelling():
    # Irreversible on-chain event -> label instantly, HARD.
    ev = [{"type": sol_parse.RUG_LP_REMOVE, "signature": "s"}]
    assert meme_scorer.label_from_rug_events(ev) == ("rug", "hard")
    assert meme_scorer.label_from_rug_events([]) is None

    # Slow-death: liquidity AND volume below floor held CONTINUOUSLY for the
    # window -> SOFT label at window close.
    series = [{"ts": i * 1800, "liq_usd": 1.0, "vol_usd": 1.0} for i in range(5)]
    lab = meme_scorer.label_slowdeath(series, window_h=1)
    assert lab is not None and lab[0] == "rug" and lab[1] == "soft"

    # A momentary dip that RECOVERS must NOT label (the sustained requirement
    # prevents wetness).
    floor_liq = settings.MEME_LIQ_FLOOR_USD
    floor_vol = settings.MEME_VOL_FLOOR_USD
    series2 = [
        {"ts": 0,    "liq_usd": 1.0, "vol_usd": 1.0},
        {"ts": 1800, "liq_usd": 1.0, "vol_usd": 1.0},
        {"ts": 3600, "liq_usd": floor_liq * 5, "vol_usd": floor_vol * 5},  # recovers
        {"ts": 5400, "liq_usd": 1.0, "vol_usd": 1.0},
    ]
    assert meme_scorer.label_slowdeath(series2, window_h=2) is None

    # Survival before horizon = CENSORED (None); at/after horizon = non-rug.
    now = 1_000_000.0
    assert meme_scorer.survival_resolution(now, now, horizon_h=24) is None
    matured = meme_scorer.survival_resolution(now - 25 * 3600, now, horizon_h=24)
    assert matured is not None and matured[0] == "survived" and matured[1] == "hard"


# ─────────────────────────────────────────────────────────────────────────
# 6. Cluster stop-list — terminal trace stops; exchange-funded not clustered
# ─────────────────────────────────────────────────────────────────────────

def test_meme_cluster_stop_list(_temp_db):
    from follow import funding
    funding.clear_cache()

    def _fund_rpc(funder, wallet):
        class FundRpc:
            def __init__(self):
                self._sigs = [{"signature": "new"}, {"signature": "old"}]
                self._txs = {
                    "old": _rpc_tx(signers=[funder],
                                   instructions=[_sys_transfer_ix(funder, wallet)]),
                    "new": _rpc_tx(signers=[wallet]),
                }

            async def get_signatures_for_address(self, address, *, before=None, limit=1000):
                return self._sigs if before is None else []

            async def get_transaction(self, sig):
                return self._txs.get(sig)
        return FundRpc()

    # Wallet funded FROM a terminal exchange -> trace STOPS -> not clusterable.
    term = "EXCHterm"
    f_term = asyncio.run(meme_scorer.cluster_funder(
        "WtermFunded", _fund_rpc(term, "WtermFunded"),
        is_terminal=lambda a: a == term))
    assert f_term is None

    # A NON-terminal funder IS clusterable.
    funding.clear_cache()
    nonterm = "FUNDERnt"
    f_ok = asyncio.run(meme_scorer.cluster_funder(
        "Wgood", _fund_rpc(nonterm, "Wgood"),
        is_terminal=lambda a: a == term))
    assert f_ok == nonterm

    # Two wallets funded ONLY via an exchange (both -> None) are NOT clustered.
    assert meme_scorer.cluster_buyers({"wa": None, "wb": None}) == {}


# ─────────────────────────────────────────────────────────────────────────
# 7. Funder-only merge — shared funder merges; co-buying does NOT
# ─────────────────────────────────────────────────────────────────────────

def test_meme_funder_only_merge():
    buyer_funders = {"w1": "F1", "w2": "F1", "w3": "F2", "w4": None}
    clusters = meme_scorer.cluster_buyers(buyer_funders)
    # Wallets sharing a funder land in one cluster.
    assert set(clusters["F1"]) == {"w1", "w2"}
    assert clusters["F2"] == ["w3"]
    # A None (unknown / terminal) funder is dropped — never merged.
    flat = [w for ws in clusters.values() for w in ws]
    assert "w4" not in flat
    # Co-membership in the same launch is NEVER a merge signal: w1 and w3 buy the
    # same token but have different funders -> separate clusters (no behavioural merge).
    assert clusters["F1"] != clusters["F2"]


# ─────────────────────────────────────────────────────────────────────────
# 8. Decision rule — AVOID needs rate AND sample; abstain otherwise
# ─────────────────────────────────────────────────────────────────────────

def test_meme_decision_rule(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "MEME_AVOID_MIN_RESOLVED", 3)
    monkeypatch.setattr(settings, "MEME_AVOID_MIN_RUGRATE", 0.7)
    T = _now()
    base = T - timedelta(days=4)

    # Funder BAD: 5 resolved, 4 rugs -> rate 0.8 (>=0.7) and sample 5 (>=3) -> AVOID.
    for i in range(4):
        _seed_launch(f"b{i}", "BAD", detected=base + timedelta(minutes=i),
                     resolved=T - timedelta(hours=1), outcome="rug", conf="hard")
    _seed_launch("bs", "BAD", detected=base + timedelta(minutes=9),
                 resolved=T - timedelta(hours=1), outcome="survived", conf="hard")
    d = meme_scorer.decide({"BAD"}, T)
    assert d["decision"] == meme_scorer.DECISION_AVOID
    assert d["funder"] == "BAD"
    assert d["resolved_sample"] == 5
    assert d["rate_as_of"] == pytest.approx(0.8)

    # One funder, one prior rug -> below min resolved -> ABSTAIN (NO-SIGNAL).
    _seed_launch("one0", "ONE", detected=base,
                 resolved=T - timedelta(hours=1), outcome="rug", conf="hard")
    assert meme_scorer.decide({"ONE"}, T)["decision"] == meme_scorer.DECISION_NO_SIGNAL

    # Unknown funder (zero resolved) -> NO-SIGNAL, NOT auto-suspect.
    assert meme_scorer.decide({"UNKNOWN"}, T)["decision"] == meme_scorer.DECISION_NO_SIGNAL

    # High sample but low rate -> no AVOID (rate gate fails).
    for i in range(5):
        _seed_launch(f"clean{i}", "CLEAN", detected=base + timedelta(minutes=i),
                     resolved=T - timedelta(hours=1), outcome="survived", conf="hard")
    assert meme_scorer.decide({"CLEAN"}, T)["decision"] == meme_scorer.DECISION_NO_SIGNAL


# ─────────────────────────────────────────────────────────────────────────
# 9. NO-SIGNAL is never "safe"
# ─────────────────────────────────────────────────────────────────────────

def test_meme_no_signal_is_not_safe():
    assert meme_scorer.NO_SIGNAL_LABEL == "no lazy manipulation detected"
    assert "safe" not in meme_scorer.NO_SIGNAL_LABEL.lower()
    d = meme_scorer.decide(set(), _now())
    assert d["decision"] == meme_scorer.DECISION_NO_SIGNAL
    assert d["reason"] == meme_scorer.NO_SIGNAL_LABEL
    assert "safe" not in (d["reason"] or "").lower()
    # The snapshot block carries the labelled meaning, never "safe".
    from ui.web_server import WebServer
    block = WebServer(coordinator=None, bot=None)._meme_empty_block()
    assert block["no_signal_label"] == "no lazy manipulation detected"


# ─────────────────────────────────────────────────────────────────────────
# 10. Replay harness — TP/FP/coverage from pre-T state; held-out test set
# ─────────────────────────────────────────────────────────────────────────

def test_meme_replay_harness(_temp_db, monkeypatch):
    monkeypatch.setattr(settings, "MEME_AVOID_MIN_RESOLVED", 2)
    monkeypatch.setattr(settings, "MEME_AVOID_MIN_RUGRATE", 0.7)
    T = _now()
    # Prior resolved history for funder RUGGY so AVOID can fire at detection.
    for i in range(3):
        _seed_launch(f"hist{i}", "RUGGY", detected=T - timedelta(days=2),
                     resolved=T - timedelta(days=1), outcome="rug", conf="hard")

    launches = []
    for i in range(4):                # rugs whose cluster funder has prior history
        launches.append({"mint": f"L{i}", "detected_at": T,
                         "outcome": "rug", "present_funders": ["RUGGY"]})
    for i in range(4):                # survived launches, unseen (clean) funder
        launches.append({"mint": f"S{i}", "detected_at": T,
                         "outcome": "survived", "present_funders": ["CLEANf"]})

    res = meme_scorer.run_replay(launches, test_fraction=0.5)
    assert res["uses_shared_as_of_fn"] is True
    assert res["train"]["n"] + res["test"]["n"] == len(launches)
    assert res["test"]["n"] > 0                       # held-out set is real + separate
    # Train half is the rugs (all flagged via pre-T history) -> TP=1.0, coverage=1.0.
    assert res["train"]["true_positive_rate"] == pytest.approx(1.0)
    assert res["train"]["coverage"] == pytest.approx(1.0)
    # Test half is survived with no prior history -> no false positives.
    assert res["test"]["false_positive_rate"] == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────────────────
# 11. Sampling seam — interface, one impl, gap/best-effort surfaced
# ─────────────────────────────────────────────────────────────────────────

def test_meme_sampling_seam_and_gap_flag():
    assert inspect.isabstract(meme_scorer.LaunchSource)
    assert issubclass(meme_scorer.SamplingLaunchSource, meme_scorer.LaunchSource)
    # SamplingLaunchSource is the ONLY concrete LaunchSource impl in v1.
    concrete = [c for c in meme_scorer.LaunchSource.__subclasses__()
                if not inspect.isabstract(c)]
    assert concrete == [meme_scorer.SamplingLaunchSource]

    # best_effort is set; a failing poll records a GAP and never raises.
    class BoomRpc:
        async def get_signatures_for_address(self, *a, **k):
            raise RuntimeError("boom")
        async def get_transaction(self, *a, **k):
            return None
    ls = meme_scorer.SamplingLaunchSource(rpc=BoomRpc())
    assert ls.best_effort is True
    assert asyncio.run(ls.get_new_launches()) == []
    assert ls.gaps >= 1

    # The gap + best-effort flag are SURFACED in the source stats.
    st = meme_scorer.MemeScorer(launch_source=ls).get_stats()
    assert st["best_effort"] is True
    assert "sampling_gaps" in st

    # A real launch tx is decoded into a Launch (sol_parse.launch_events reuse).
    class LaunchRpc:
        async def get_signatures_for_address(self, address, *, before=None, limit=1000):
            return [{"signature": "ls1"}]
        async def get_transaction(self, sig):
            return _launch_tx("MINTx", "CREATORx")
    got = asyncio.run(meme_scorer.SamplingLaunchSource(rpc=LaunchRpc()).get_new_launches())
    assert len(got) == 1
    assert got[0].mint == "MINTx" and got[0].creator == "CREATORx"


# ─────────────────────────────────────────────────────────────────────────
# 12. Privacy guard — 403 off-loopback host
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_meme_privacy_guard_403_off_localhost(_temp_db, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from ui.web_server import WebServer

    ws = WebServer(coordinator=None, bot=None)
    client = TestClient(TestServer(ws._make_app()))
    await client.start_server()
    try:
        monkeypatch.setattr(settings, "WEB_UI_HOST", "0.0.0.0")
        for path in ("/api/meme/launch/MINT", "/api/meme/asof/FUNDER",
                     "/api/meme/health"):
            r = await client.get(path)
            assert r.status == 403, f"{path} should 403 off-loopback"
        # Loopback host -> not forbidden.
        monkeypatch.setattr(settings, "WEB_UI_HOST", "localhost")
        r = await client.get("/api/meme/health")
        assert r.status == 200
        body = await r.json()
        assert body["ok"] is True
    finally:
        await client.close()


# ─────────────────────────────────────────────────────────────────────────
# 13. Snapshot safety
# ─────────────────────────────────────────────────────────────────────────

def test_meme_snapshot_complete_when_agent_none():
    from ui.web_server import WebServer
    ws = WebServer(coordinator=None, bot=None)
    block = ws._snap_meme()
    for k in ("enabled", "running", "launches", "decisions", "sources",
              "no_signal_label"):
        assert k in block
    assert block["no_signal_label"] == "no lazy manipulation detected"


def test_meme_snapshot_complete_when_agent_raises():
    from ui.web_server import WebServer

    class _Boom:
        def get_meme_snapshot(self):
            raise RuntimeError("boom")

    coord = SimpleNamespace(get_agent=lambda _id: _Boom())
    ws = WebServer(coordinator=coord, bot=None)
    block = ws._snap_meme()                          # must not raise
    assert isinstance(block, dict)
    assert "launches" in block and "decisions" in block
