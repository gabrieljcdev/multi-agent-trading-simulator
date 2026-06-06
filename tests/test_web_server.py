"""
tests/test_web_server.py — WebServer snapshot, WebSocket push, REST actions.

Uses aiohttp's TestClient/TestServer (ephemeral ports, no fixed-port
conflicts) and mocks for coordinator/bot. The snapshot reads the real
queries layer defensively, so these run against the project DB without
special fixtures.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from config import settings
from ui.web_server import WebServer, WebLogHandler

_TOP_LEVEL_KEYS = (
    "ts", "session", "uptime_s", "mode", "paused", "approval_mode",
    "portfolio", "agents", "circuit_breakers", "regime", "sentiment",
    "exchanges", "signals", "pending_signal", "positions", "scalp", "arb_feed",
    "session_pnl", "log", "top_pairs", "top_strategies", "insights",
    "arb_history",
    # Web UI v2 — dedicated agent-panel keys.
    "arb", "xchain", "funding", "balance",
    # Opportunity Scanner panel (read-only; $0 observation).
    "opportunities",
)


async def _client(ws: WebServer) -> TestClient:
    client = TestClient(TestServer(ws._make_app()))
    await client.start_server()
    return client


# ── Snapshot ──────────────────────────────────────────────────────────────

def test_snapshot_returns_complete_dict():
    ws = WebServer(coordinator=None, bot=None)
    snap = ws._build_snapshot()
    for key in _TOP_LEVEL_KEYS:
        assert key in snap, f"missing snapshot key {key}"
    # session_pnl always carries the four sessions.
    assert set(snap["session_pnl"]) == {"LONDON", "NEW_YORK", "ASIA", "OFF_HOURS"}


def test_snapshot_safe_when_bot_is_none():
    coord = SimpleNamespace(get_primary_bot=lambda: None)
    ws = WebServer(coordinator=coord, bot=None)
    snap = ws._build_snapshot()           # must not raise
    assert snap["pending_signal"] is None
    assert isinstance(snap["positions"], list)
    assert snap["paused"] is False


@pytest.mark.asyncio
async def test_snapshot_safe_when_coordinator_raises():
    async def boom():
        raise RuntimeError("coordinator down")

    coord = SimpleNamespace(
        get_portfolio_stats=boom, get_agent_stats=boom,
        get_primary_bot=lambda: None,
    )
    ws = WebServer(coordinator=coord, bot=None)
    await ws._refresh_coordinator()       # swallows the raise
    snap = ws._build_snapshot()           # still complete
    assert "portfolio" in snap
    # Change 1 replaced "equity" with the persistent "bankroll" field; the
    # snapshot must still be complete when the coordinator getter raised.
    assert "bankroll" in snap["portfolio"]
    assert "equity" not in snap["portfolio"]


# ── WebSocket ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ws_client_receives_snapshot():
    ws = WebServer(coordinator=None, bot=None)
    client = await _client(ws)
    try:
        conn = await client.ws_connect("/ws")
        msg = await conn.receive_json(timeout=5)
        assert "portfolio" in msg and "agents" in msg
        await conn.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_multiple_ws_clients_all_receive_broadcast():
    ws = WebServer(coordinator=None, bot=None)
    client = await _client(ws)
    try:
        c1 = await client.ws_connect("/ws")
        c2 = await client.ws_connect("/ws")
        await c1.receive_json(timeout=5)   # drain the on-connect snapshot
        await c2.receive_json(timeout=5)
        await ws._broadcast(json.dumps({"ping": 1}))
        m1 = await c1.receive_json(timeout=5)
        m2 = await c2.receive_json(timeout=5)
        assert m1["ping"] == 1 and m2["ping"] == 1
        await c1.close()
        await c2.close()
    finally:
        await client.close()


# ── REST actions ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kill_action_calls_coordinator_kill_all(monkeypatch):
    coord = MagicMock()
    coord.kill_all = AsyncMock(return_value={"agents_ok": 2})
    monkeypatch.setattr("ui.web_server.db_queries.log_agent_event",
                        lambda *a, **k: None)
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        r = await client.post("/action/kill")
        data = await r.json()
        assert data["ok"] is True
        coord.kill_all.assert_awaited_once()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_kill_action_safe_when_coordinator_none():
    ws = WebServer(coordinator=None, bot=None)
    client = await _client(ws)
    try:
        r = await client.post("/action/kill")
        data = await r.json()
        assert data["ok"] is False
        assert "error" in data
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_approve_action_drains_approval_queue():
    bot = MagicMock()
    bot.approve_next_pending = AsyncMock(return_value=True)
    ws = WebServer(coordinator=None, bot=bot)
    client = await _client(ws)
    try:
        r = await client.post("/action/approve")
        data = await r.json()
        assert data["ok"] is True
        bot.approve_next_pending.assert_awaited_once()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_skip_action_removes_pending_signal():
    bot = MagicMock()
    bot.skip_next_pending = AsyncMock(return_value=True)
    ws = WebServer(coordinator=None, bot=bot)
    client = await _client(ws)
    try:
        r = await client.post("/action/skip", json={"reason": "web_skip"})
        data = await r.json()
        assert data["ok"] is True
        bot.skip_next_pending.assert_awaited_once()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_set_mode_updates_bot_approval_mode(monkeypatch):
    monkeypatch.setattr(settings, "APPROVAL_MODE", "per_trade")  # restored at teardown
    ws = WebServer(coordinator=None, bot=None)
    client = await _client(ws)
    try:
        for mode in ("per_trade", "window", "autonomous"):
            r = await client.post("/action/set_mode", json={"mode": mode})
            assert (await r.json())["ok"] is True
            assert settings.APPROVAL_MODE == mode
        r = await client.post("/action/set_mode", json={"mode": "bogus"})
        assert (await r.json())["ok"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pause_action_sets_bot_paused():
    bot = SimpleNamespace(_paused=False)
    ws = WebServer(coordinator=None, bot=bot)
    client = await _client(ws)
    try:
        r = await client.post("/action/pause", json={"paused": True})
        assert (await r.json())["ok"] is True
        assert bot._paused is True
    finally:
        await client.close()


# ── Lifecycle + log handler ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_server_start_stop(monkeypatch):
    monkeypatch.setattr(settings, "WEB_UI_PORT", 0)          # ephemeral bind
    monkeypatch.setattr(settings, "WEB_UI_PUSH_INTERVAL_S", 0.05)
    ws = WebServer(coordinator=None, bot=None)
    await ws.start()
    try:
        assert ws._running is True
        assert ws._broadcast_task is not None
        await asyncio.sleep(0.12)        # let the broadcast loop tick
    finally:
        await ws.stop()
    assert ws._running is False
    assert ws._broadcast_task.done()
    # Route still serves via the app (handle_index).
    client = await _client(ws)
    try:
        r = await client.get("/")
        assert r.status == 200
    finally:
        await client.close()


def test_web_log_handler_appends_to_buffer():
    ws = WebServer(coordinator=None, bot=None)
    handler = WebLogHandler(ws._log_buffer)
    rec = logging.LogRecord(
        "core.bot", logging.INFO, __file__, 1,
        "Signal executed — BTC/USDT Long", None, None,
    )
    handler.emit(rec)
    snap = ws._build_snapshot()
    titles = [e["title"] for e in snap["log"]]
    assert any(t.startswith("Signal executed") for t in titles)
    assert snap["log"][0]["type"] == "exec"


# ════════════════════════════════════════════════════════════════════════════
# Web UI refresh v1 — bankroll, daily fees, live exposure, scalp WR + feed,
# agent pages, session pages.
#
# These seed a fresh SQLite per test and reload db + queries + ui.web_server
# against it (same pattern as tests/test_queries.py) so the snapshot and the
# REST endpoints read the seeded data. The 12 tests above keep running against
# the real project DB unchanged — they're defined first and don't reload.
# ════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def web_temp(monkeypatch, tmp_path):
    """(ui.web_server, database.db, database.queries) bound to a fresh temp DB."""
    db_file = tmp_path / "test_web.db"
    monkeypatch.setattr("config.settings.DB_PATH", db_file)

    import database.db as ddb
    import database.queries as dq
    import ui.web_server as wsm
    importlib.reload(ddb)
    importlib.reload(dq)
    importlib.reload(wsm)
    ddb.init_db()
    yield wsm, ddb, dq
    # Repoint the reloaded modules back at the real DB for anything that follows.
    monkeypatch.undo()
    importlib.reload(ddb)
    importlib.reload(dq)
    importlib.reload(wsm)


def _starting_capital_total():
    return (settings.FUND_SIGNAL_CAPITAL + settings.FUND_ARB_CAPITAL
            + settings.FUND_MEXC_SCALP_CAPITAL)


def _seed_trade(db, *, strategy="default", signal_type="momentum", pnl_usd=1.0,
                pnl_pct=1.0, fees_usd=0.0, size_usd=50.0, pair="BTC/USDT",
                side="long", entry=100.0, exit=101.0, close_dt=None, open_only=False):
    """Insert a Trade row directly. open_only=True leaves it open (no close)."""
    import database.models as m
    now = datetime.utcnow()
    with db.get_session() as s:
        t = m.Trade(
            pair=pair, exchange="binance", side=side, signal_type=signal_type,
            entry_price=entry, size_usd=size_usd, sim_mode=True, strategy=strategy,
            timestamp_open=now,
        )
        if not open_only:
            t.exit_price = exit
            t.pnl_usd = pnl_usd
            t.pnl_pct = pnl_pct
            t.fees_usd = fees_usd
            t.timestamp_close = close_dt or now
        s.add(t)
        s.flush()
        return t.id


def _seed_arb(db, *, gross=2.0, net=1.5, size_usd=25.0, ts=None, symbol="BTC/USDT"):
    import database.models as m
    with db.get_session() as s:
        r = m.ArbTrade(
            symbol=symbol, buy_exchange="bitget", sell_exchange="kraken",
            buy_price=100.0, sell_price=101.0, size_usd=size_usd,
            gross_pnl_usd=gross, net_pnl_usd=net, execution_ms=12.0,
            status="executed", sim_mode=True, success=True,
            timestamp=ts or datetime.utcnow(),
        )
        s.add(r)
        s.flush()
        return r.id


def _seed_scalp(db, *, pnl_bps=5.0, rt_bps=0.0, pnl_usd=0.5, exit_price=101.0,
                direction="LONG", created=None, ts=None, symbol="BTC/USDT",
                exchange="mexc", would_entry=True, exit_reason="TP", hold_sec=30.0):
    import database.models as m
    created = created or datetime.utcnow()
    ts = ts if ts is not None else time.time()
    with db.get_session() as s:
        o = m.ScalpObservationModel(
            symbol=symbol, exchange=exchange, timestamp=ts, direction=direction,
            would_entry=would_entry, entry_price=100.0, exit_price=exit_price,
            exit_time=ts + hold_sec, exit_reason=exit_reason, hold_sec=hold_sec,
            pnl_bps=pnl_bps, pnl_usd=pnl_usd, round_trip_cost_bps=rt_bps,
            created_at=created,
        )
        s.add(o)
        s.flush()
        return o.id


# ── Change 1 — bankroll ─────────────────────────────────────────────────────

def test_snapshot_bankroll_equals_starting_plus_realised_pnl(web_temp):
    wsm, db, q = web_temp
    _seed_trade(db, pnl_usd=10.0)                                   # signal
    _seed_trade(db, strategy="scalp", signal_type="scalp", pnl_usd=2.0)  # exec scalp
    _seed_arb(db, gross=5.0, net=3.0)                              # arb net 3.0
    ws = wsm.WebServer(coordinator=None, bot=None)
    port = ws._build_snapshot()["portfolio"]
    assert port["bankroll"] == pytest.approx(_starting_capital_total() + 15.0)
    assert port["bankroll_alltime_pnl"] == pytest.approx(15.0)


def test_snapshot_bankroll_does_not_reset_at_utc_midnight(web_temp):
    wsm, db, q = web_temp
    yesterday = datetime.utcnow() - timedelta(days=1)
    _seed_trade(db, pnl_usd=4.0, close_dt=yesterday)
    _seed_trade(db, pnl_usd=6.0)                                   # today
    ws = wsm.WebServer(coordinator=None, bot=None)
    snap = ws._build_snapshot()
    # Bankroll spans all dates → crossing midnight keeps yesterday's profit.
    assert snap["portfolio"]["bankroll"] == pytest.approx(_starting_capital_total() + 10.0)
    # daily_pnl comes from the coordinator (none here) and is independent.
    assert snap["portfolio"]["daily_pnl"] == 0.0


def test_snapshot_omits_removed_equity_and_total_pnl_fields(web_temp):
    wsm, db, q = web_temp
    ws = wsm.WebServer(coordinator=None, bot=None)
    port = ws._build_snapshot()["portfolio"]
    for gone in ("equity", "total_pnl", "total_pnl_pct"):
        assert gone not in port
    for present in ("bankroll", "bankroll_alltime_pnl", "daily_fees", "exposure_usd"):
        assert present in port


# ── Change 2 — daily fees ───────────────────────────────────────────────────

def test_daily_fees_aggregates_across_agents(web_temp):
    wsm, db, q = web_temp
    _seed_trade(db, fees_usd=0.50)                       # signal fee
    _seed_arb(db, gross=2.0, net=1.4)                    # arb fee 0.60
    _seed_scalp(db, rt_bps=10.0)                         # 10bps × $50 = $0.05
    expected = 0.50 + 0.60 + (10.0 / 10000.0 * settings.SCALP_POSITION_SIZE_USD)
    assert q.get_daily_fees() == pytest.approx(expected)


# ── Change 3 — live exposure ────────────────────────────────────────────────

def test_exposure_includes_scalp_positions(web_temp):
    wsm, db, q = web_temp
    # Executed scalp writes an open Trade row with strategy="scalp".
    _seed_trade(db, strategy="scalp", signal_type="scalp", pair="OP/USDT",
                size_usd=40.0, open_only=True)
    ws = wsm.WebServer(coordinator=None, bot=None)
    port = ws._build_snapshot()["portfolio"]
    assert port["exposure_usd"] == pytest.approx(40.0)
    assert port["exposure_pct"] > 0


def test_exposure_recalculated_each_snapshot(web_temp):
    wsm, db, q = web_temp
    ws = wsm.WebServer(coordinator=None, bot=None)
    assert ws._build_snapshot()["portfolio"]["exposure_usd"] == 0.0
    _seed_trade(db, size_usd=70.0, open_only=True)
    # No cache to reset — the next snapshot reflects the new open position.
    assert ws._build_snapshot()["portfolio"]["exposure_usd"] == pytest.approx(70.0)


# ── Change 4 — scalp win rate ───────────────────────────────────────────────

def test_scalp_win_rate_zero_when_no_trades(monkeypatch):
    import agents.scalping_agent as sa
    monkeypatch.setattr(sa.db_queries, "get_scalp_closed_today", lambda: [])
    assert sa.ScalpingAgent()._win_rate_today() == 0.0


def test_scalp_win_rate_computed_from_pnl_bps_positive(monkeypatch):
    import agents.scalping_agent as sa
    rows = [{"pnl_bps": b} for b in (5, 3, -2, 4, -1, 2)]   # 4 wins of 6
    monkeypatch.setattr(sa.db_queries, "get_scalp_closed_today", lambda: rows)
    assert sa.ScalpingAgent()._win_rate_today() == pytest.approx(4 / 6)


def test_scalp_win_rate_resets_at_utc_midnight(web_temp):
    wsm, db, q = web_temp
    today = datetime.utcnow()
    yest = today - timedelta(days=1)
    _seed_scalp(db, pnl_bps=5.0, created=yest)             # yesterday win
    _seed_scalp(db, pnl_bps=4.0, created=today)            # today win
    _seed_scalp(db, pnl_bps=-2.0, created=today)           # today loss
    rows = q.get_scalp_closed_today()
    assert len(rows) == 2                                  # yesterday excluded
    wins = sum(1 for r in rows if r["pnl_bps"] > 0)
    assert wins / len(rows) == pytest.approx(0.5)


# ── Change 5 — scalp feed persistence ───────────────────────────────────────

def test_scalp_closed_trades_persisted_in_snapshot(web_temp):
    wsm, db, q = web_temp
    _seed_scalp(db, pnl_bps=6.0, exit_reason="TP", symbol="OP/USDT")
    ws = wsm.WebServer(coordinator=None, bot=None)
    closed = ws._build_snapshot()["scalp"]["closed_trades"]
    assert len(closed) == 1
    assert closed[0]["symbol"] == "OP/USDT"
    assert closed[0]["outcome"] == "WIN"


def test_scalp_closed_trades_buffer_capped_at_setting(web_temp, monkeypatch):
    wsm, db, q = web_temp
    monkeypatch.setattr(wsm.settings, "WEB_UI_SCALP_FEED_HISTORY", 5)
    base = time.time()
    for i in range(10):
        _seed_scalp(db, pnl_bps=float(i + 1), ts=base + i, symbol=f"P{i}/USDT")
    ws = wsm.WebServer(coordinator=None, bot=None)
    closed = ws._build_snapshot()["scalp"]["closed_trades"]
    assert len(closed) == 5
    assert closed[0]["symbol"] == "P9/USDT"                # newest first


def test_scalp_live_trades_separate_from_closed(web_temp):
    wsm, db, q = web_temp
    _seed_scalp(db, pnl_bps=3.0, symbol="BTC/USDT")        # one closed
    pos = SimpleNamespace(symbol="ENA/USDT", exchange="mexc", direction="LONG",
                          entry_price=0.5, tp_price=0.51, sl_price=0.49,
                          entry_time=time.time(), size_usd=50.0)
    scalp_agent = SimpleNamespace(_positions={"k": pos})
    coord = SimpleNamespace(
        get_agent=lambda aid: scalp_agent if aid == "scalp" else None,
        get_primary_bot=lambda: None,
    )
    ws = wsm.WebServer(coordinator=coord, bot=None)
    scalp = ws._build_snapshot()["scalp"]
    assert len(scalp["live_trades"]) == 1
    assert scalp["live_trades"][0]["symbol"] == "ENA/USDT"
    assert len(scalp["closed_trades"]) == 1
    assert scalp["closed_trades"][0]["symbol"] == "BTC/USDT"


# ── Change 7 — agent pages ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_agent_signal_returns_trades_and_insights(web_temp):
    wsm, db, q = web_temp
    import database.models as m
    tid = _seed_trade(db, pnl_usd=3.0)
    with db.get_session() as s:
        s.get(m.Trade, tid).claude_postmortem = "good entry"
    client = await _client(wsm.WebServer(coordinator=None, bot=None))
    try:
        r = await client.get("/api/agent/signal")
        assert r.status == 200
        d = await r.json()
        assert len(d["trades"]) == 1
        assert len(d["insights"]) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_agent_arb_returns_trades_and_insights(web_temp):
    wsm, db, q = web_temp
    _seed_arb(db)
    client = await _client(wsm.WebServer(coordinator=None, bot=None))
    try:
        d = await (await client.get("/api/agent/arb")).json()
        assert len(d["trades"]) == 1
        assert d["insights"] == []                         # arb has no postmortems
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_agent_scalp_returns_trades_and_insights(web_temp):
    wsm, db, q = web_temp
    _seed_scalp(db, pnl_bps=7.0)
    client = await _client(wsm.WebServer(coordinator=None, bot=None))
    try:
        d = await (await client.get("/api/agent/scalp")).json()
        assert len(d["trades"]) == 1
        assert d["trades"][0]["outcome"] == "WIN"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_agent_placeholders_now_404(web_temp):
    """Web UI v2 dropped macro / sentiment_agent / onchain from
    _VALID_AGENTS — their detail endpoints now 404, matching the snapshot
    side (those ids are no longer in REGISTERED_AGENTS either)."""
    wsm, db, q = web_temp
    client = await _client(wsm.WebServer(coordinator=None, bot=None))
    try:
        for aid in ("macro", "sentiment_agent", "onchain"):
            r = await client.get("/api/agent/" + aid)
            assert r.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_agent_unknown_returns_404(web_temp):
    wsm, db, q = web_temp
    client = await _client(wsm.WebServer(coordinator=None, bot=None))
    try:
        assert (await client.get("/api/agent/nope")).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_agent_insights_filtered_by_agent_id(web_temp):
    wsm, db, q = web_temp
    import database.models as m
    sig = _seed_trade(db, strategy="default", pnl_usd=1.0)
    scl = _seed_trade(db, strategy="scalp", signal_type="scalp", pnl_usd=1.0)
    with db.get_session() as s:
        s.get(m.Trade, sig).claude_postmortem = "signal review"
        s.get(m.Trade, scl).claude_postmortem = "scalp review"
    client = await _client(wsm.WebServer(coordinator=None, bot=None))
    try:
        ds = await (await client.get("/api/agent/signal")).json()
        dc = await (await client.get("/api/agent/scalp")).json()
        assert [i["body"] for i in ds["insights"]] == ["signal review"]
        assert [i["body"] for i in dc["insights"]] == ["scalp review"]
    finally:
        await client.close()


# ── Change 8 — session pages ────────────────────────────────────────────────

def _today_at(hour):
    return datetime.utcnow().replace(hour=hour, minute=0, second=0, microsecond=0)


@pytest.mark.asyncio
async def test_api_session_returns_today_closed_trades_only(web_temp):
    wsm, db, q = web_temp
    _seed_trade(db, pnl_usd=2.0, close_dt=_today_at(8), pair="BTC/USDT")  # LONDON
    client = await _client(wsm.WebServer(coordinator=None, bot=None))
    try:
        d = await (await client.get("/api/session/london")).json()
        assert d["session"] == "LONDON"
        assert d["trade_count"] == 1
        assert d["trades"][0]["pair"] == "BTC/USDT"
        assert d["total_pnl"] == pytest.approx(2.0)
        assert "tz" in d and "local_time" in d
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_session_excludes_yesterdays_trades(web_temp):
    wsm, db, q = web_temp
    yest = (datetime.utcnow() - timedelta(days=1)).replace(
        hour=8, minute=0, second=0, microsecond=0)
    _seed_trade(db, pnl_usd=1.0, close_dt=yest)            # yesterday LONDON
    _seed_trade(db, pnl_usd=2.0, close_dt=_today_at(8))    # today LONDON
    client = await _client(wsm.WebServer(coordinator=None, bot=None))
    try:
        d = await (await client.get("/api/session/london")).json()
        assert d["trade_count"] == 1
        assert d["total_pnl"] == pytest.approx(2.0)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_session_includes_all_agents(web_temp):
    wsm, db, q = web_temp
    london = _today_at(8)
    _seed_trade(db, pnl_usd=1.0, close_dt=london)          # signal
    _seed_arb(db, gross=1.0, net=0.8, ts=london)           # arb
    _seed_scalp(db, pnl_bps=5.0, pnl_usd=0.5, created=london)  # scalp
    client = await _client(wsm.WebServer(coordinator=None, bot=None))
    try:
        d = await (await client.get("/api/session/london")).json()
        agents = {t["agent"] for t in d["trades"]}
        assert {"signal", "arb", "scalp"}.issubset(agents)
        assert d["trade_count"] == 3
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_session_unknown_returns_404(web_temp):
    wsm, db, q = web_temp
    client = await _client(wsm.WebServer(coordinator=None, bot=None))
    try:
        assert (await client.get("/api/session/atlantis")).status == 404
    finally:
        await client.close()


# ════════════════════════════════════════════════════════════════════════════
# Web UI v2 — dedicated agent panels (arb extension, xchain, funding, balance)
# + /action/rebalance three-action contract.
#
# Each new snapshot key must be present even with no agent wired up
# (defensive default). The rebalance endpoint mirrors /action/kill's
# two-step (arm → confirm) shape with an added cancel action.
# ════════════════════════════════════════════════════════════════════════════


def _build_balance_agent_mock(*, paused=False, transfers=None, arm_token=None,
                              arm_expires=None, execute_result=None,
                              execute_raises=None):
    """Reusable BalanceAgent mock with the v2-required public surface."""
    agent = MagicMock()
    agent._paused           = paused
    agent._arm_token        = arm_token
    agent._arm_expires_at   = (
        arm_expires if arm_expires is not None
        else (time.time() + 60 if arm_token else 0.0)
    )
    agent._running          = True
    agent._transfers_today  = 0
    agent._fees_today_usd   = 0.0
    agent._daily_rebalances = 0
    agent._last_computed_targets = {}
    agent._last_computed_bands   = {}

    def _arm():
        token = "tok-" + str(int(time.time() * 1000))
        agent._arm_token      = token
        agent._arm_expires_at = time.time() + 60
        return token, {"live": False, "mexc_warning": ""}
    agent.arm = _arm
    agent.consume_arm = lambda t: (t and t == agent._arm_token)
    agent.get_pending_proposal = lambda: {
        "proposed_at":  "12:00:00",
        "proposal_id":  1,
        "transfers":    transfers or [],
    }
    if execute_raises is not None:
        async def _exec_raise(token):
            raise execute_raises
        agent.execute_proposal = _exec_raise
    else:
        async def _exec(token):
            if execute_result is not None:
                return execute_result
            return {"ok": True, "transfer_ids": [101, 102]}
        agent.execute_proposal = _exec
    return agent


def _build_coordinator(*, agents_by_id=None):
    """Coordinator stub with the get_agent/get_primary_bot surface the
    web layer reads. agents_by_id maps agent_id → mock agent."""
    by_id = agents_by_id or {}
    coord = SimpleNamespace(
        get_agent=lambda aid: by_id.get(aid),
        get_primary_bot=lambda: None,
        get_portfolio_stats=AsyncMock(return_value={}),
        get_agent_stats=AsyncMock(return_value=[]),
    )
    return coord


# ── Snapshot — new top-level keys present + defaults safe ────────────────────

def test_snapshot_includes_arb_extended_keys():
    ws = WebServer(coordinator=None, bot=None)
    snap = ws._build_snapshot()
    arb = snap["arb"]
    for k in ("exchanges", "gap_distribution", "threshold", "semaphore"):
        assert k in arb, f"missing arb.{k}"
    assert arb["gap_distribution"]["bucket_edges_bps"] == [0, 5, 10, 20, 50, 100, 250]
    assert arb["semaphore"]["capacity"] >= 1


def test_snapshot_includes_xchain_keys():
    ws = WebServer(coordinator=None, bot=None)
    x = ws._build_snapshot()["xchain"]
    for k in ("status", "capital_usd", "chains", "best_pair",
              "inventory_targets", "today_summary"):
        assert k in x, f"missing xchain.{k}"
    # Empty defaults when no agent registered.
    assert x["chains"] == []
    assert x["inventory_targets"] == []
    assert x["status"] in ("OFFLINE", "OBSERVATION", "RUNNING", "ERROR")


def test_snapshot_includes_funding_keys():
    ws = WebServer(coordinator=None, bot=None)
    f = ws._build_snapshot()["funding"]
    for k in ("status", "capital_usd", "venue", "symbols",
              "positions", "today_summary"):
        assert k in f, f"missing funding.{k}"
    assert f["symbols"] == []
    assert f["positions"] == []
    for k in ("below_gate", "basis_unfavourable", "depth_thin", "other"):
        assert k in f["today_summary"]["skip_reasons"]


def test_snapshot_includes_balance_keys():
    ws = WebServer(coordinator=None, bot=None)
    b = ws._build_snapshot()["balance"]
    for k in ("status", "kill_blocked", "pool", "funds", "nodes",
              "in_transit", "halted_pairs", "today", "pending_plan"):
        assert k in b, f"missing balance.{k}"
    for k in ("equity_usd", "reserve_usd", "deployed_usd", "pool_usd"):
        assert k in b["pool"]
    assert b["pending_plan"]["confirm_token"] is None


def test_snapshot_safe_when_balance_agent_raises():
    bad = MagicMock()
    type(bad)._paused = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    bad.get_pending_proposal = MagicMock(side_effect=RuntimeError("boom"))
    coord = _build_coordinator(agents_by_id={"balance": bad})
    ws = WebServer(coordinator=coord, bot=None)
    snap = ws._build_snapshot()   # must not raise
    assert "balance" in snap
    assert snap["balance"]["pending_plan"]["transfers"] == []


def test_snapshot_safe_when_xchain_engine_raises():
    bad = MagicMock()
    bad.observation_mode = False
    bad.get_inventory_targets = MagicMock(side_effect=RuntimeError("engine down"))
    coord = _build_coordinator(agents_by_id={"xchain": bad})
    ws = WebServer(coordinator=coord, bot=None)
    snap = ws._build_snapshot()
    assert snap["xchain"]["status"] == "ERROR"
    assert snap["xchain"]["inventory_targets"] == []


def test_snapshot_safe_when_funding_engine_raises():
    bad = MagicMock()
    bad.observation_mode = False
    # property that raises whenever _positions is read
    type(bad)._positions = property(lambda self: (_ for _ in ()).throw(RuntimeError("oops")))
    coord = _build_coordinator(agents_by_id={"funding_arb": bad})
    ws = WebServer(coordinator=coord, bot=None)
    snap = ws._build_snapshot()
    assert snap["funding"]["status"] == "ERROR"
    assert snap["funding"]["positions"] == []


@pytest.mark.asyncio
async def test_placeholder_agents_not_in_snapshot():
    """The three placeholder agent_ids must not surface in snapshot.agents[]
    even when the coordinator returns AgentStats-like rows for them — the
    Web UI v2 dropped their registrations entirely."""
    agents = [
        SimpleNamespace(agent_id="signal",          status="RUNNING", capital_allocated=100.0,
                        daily_pnl=0.0, trades_today=0, win_rate_today=0.0),
        SimpleNamespace(agent_id="scalp",           status="RUNNING", capital_allocated=200.0,
                        daily_pnl=0.0, trades_today=0, win_rate_today=0.0),
    ]
    coord = SimpleNamespace(
        get_agent=lambda aid: None,
        get_primary_bot=lambda: None,
        get_portfolio_stats=AsyncMock(return_value={}),
        get_agent_stats=AsyncMock(return_value=agents),
    )
    ws = WebServer(coordinator=coord, bot=None)
    await ws._refresh_coordinator()
    ids = [a["id"] for a in ws._build_snapshot()["agents"]]
    for placeholder in ("macro", "sentiment_agent", "onchain"):
        assert placeholder not in ids


# ── /action/rebalance — three actions, error envelope ───────────────────────

@pytest.mark.asyncio
async def test_rebalance_arm_returns_token():
    transfers = [{"from_fund": "signal", "to_fund": "arb",
                  "from_exchange": "binance", "to_exchange": "kraken",
                  "asset": "USDT", "amount_usd": 25.0,
                  "est_fee_usd": 1.0, "est_time_s": 600,
                  "ring_fence_warning": None}]
    agent = _build_balance_agent_mock(transfers=transfers)
    coord = _build_coordinator(agents_by_id={"balance": agent})
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        r = await client.post("/action/rebalance", json={"action": "arm"})
        d = await r.json()
        assert d["ok"] is True
        assert d["confirm_token"].startswith("tok-")
        assert d["expires_in_s"] >= 1
        assert d["proposal"]["transfers"] == transfers
        assert isinstance(d["ring_fence_warnings"], list)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rebalance_confirm_executes_within_window(monkeypatch):
    monkeypatch.setattr(settings, "SIM_MODE", True)
    monkeypatch.setattr("ui.web_server.db_queries.log_agent_event",
                        lambda *a, **k: None)
    agent = _build_balance_agent_mock(
        transfers=[{"from_fund": "signal", "to_fund": "arb",
                    "from_exchange": "binance", "to_exchange": "kraken",
                    "asset": "USDT", "amount_usd": 25.0,
                    "est_fee_usd": 1.0, "est_time_s": 600,
                    "ring_fence_warning": None}],
        execute_result={"ok": True, "transfer_ids": [42]},
    )
    coord = _build_coordinator(agents_by_id={"balance": agent})
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        arm = await (await client.post("/action/rebalance", json={"action": "arm"})).json()
        token = arm["confirm_token"]
        r = await client.post("/action/rebalance",
                              json={"action": "confirm", "confirm_token": token})
        d = await r.json()
        assert d["ok"] is True
        assert d["transfer_ids"] == [42]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rebalance_confirm_expired_token(monkeypatch):
    monkeypatch.setattr(settings, "SIM_MODE", True)
    agent = _build_balance_agent_mock(
        execute_result={"ok": False, "error": "token_expired"},
    )
    coord = _build_coordinator(agents_by_id={"balance": agent})
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        await client.post("/action/rebalance", json={"action": "arm"})
        # Force the agent into "token expired" mode (mock returns directly).
        r = await client.post("/action/rebalance",
                              json={"action": "confirm", "confirm_token": "anything"})
        d = await r.json()
        assert d["ok"] is False
        assert d["error"] == "token_expired"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rebalance_confirm_mismatched_token(monkeypatch):
    monkeypatch.setattr(settings, "SIM_MODE", True)
    agent = _build_balance_agent_mock(
        execute_result={"ok": False, "error": "token_invalid"},
    )
    coord = _build_coordinator(agents_by_id={"balance": agent})
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        await client.post("/action/rebalance", json={"action": "arm"})
        r = await client.post("/action/rebalance",
                              json={"action": "confirm", "confirm_token": "wrong-token"})
        d = await r.json()
        assert d["ok"] is False
        assert d["error"] == "token_invalid"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rebalance_cancel_invalidates_token(monkeypatch):
    monkeypatch.setattr(settings, "SIM_MODE", True)
    agent = _build_balance_agent_mock(
        execute_result={"ok": False, "error": "token_invalid"},
    )
    coord = _build_coordinator(agents_by_id={"balance": agent})
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        arm = await (await client.post("/action/rebalance", json={"action": "arm"})).json()
        token = arm["confirm_token"]
        canc = await (await client.post(
            "/action/rebalance",
            json={"action": "cancel", "confirm_token": token},
        )).json()
        assert canc["ok"] is True
        # Subsequent confirm fails.
        r = await client.post("/action/rebalance",
                              json={"action": "confirm", "confirm_token": token})
        d = await r.json()
        assert d["ok"] is False
        assert d["error"] == "token_invalid"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rebalance_blocked_when_live_disabled(monkeypatch):
    """SIM_MODE=False + REBALANCE_LIVE_ENABLED=False → confirm refuses
    before even reaching the agent (defence-in-depth)."""
    monkeypatch.setattr(settings, "SIM_MODE", False)
    monkeypatch.setattr(settings, "REBALANCE_LIVE_ENABLED", False)
    agent = _build_balance_agent_mock()
    coord = _build_coordinator(agents_by_id={"balance": agent})
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        await client.post("/action/rebalance", json={"action": "arm"})
        r = await client.post("/action/rebalance",
                              json={"action": "confirm", "confirm_token": "anything"})
        d = await r.json()
        assert d["ok"] is False
        assert d["error"] == "live_rebalance_disabled"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rebalance_blocked_when_balance_agent_missing():
    """Coordinator returns None for the balance agent — every action
    returns the same envelope so the operator gets a clean error
    regardless of which button they pressed."""
    coord = _build_coordinator(agents_by_id={})   # no balance entry
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        for action_body in (
            {"action": "arm"},
            {"action": "confirm", "confirm_token": "anything"},
            {"action": "cancel",  "confirm_token": "anything"},
        ):
            r = await client.post("/action/rebalance", json=action_body)
            d = await r.json()
            assert d["ok"] is False
            assert d["error"] == "balance_agent_unavailable"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rebalance_logs_event_on_confirm(monkeypatch):
    """On confirm success, log_agent_event is called with the agent id,
    the REBALANCE_WEB event type, and a detail line containing
    'source=web_ui' + the transfer IDs."""
    monkeypatch.setattr(settings, "SIM_MODE", True)
    captured = []
    monkeypatch.setattr(
        "ui.web_server.db_queries.log_agent_event",
        lambda agent_id, event_type, detail="": captured.append(
            (agent_id, event_type, detail)
        ),
    )
    agent = _build_balance_agent_mock(
        execute_result={"ok": True, "transfer_ids": [7, 8]},
    )
    coord = _build_coordinator(agents_by_id={"balance": agent})
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        await client.post("/action/rebalance", json={"action": "arm"})
        await client.post("/action/rebalance",
                          json={"action": "confirm", "confirm_token": "anything"})
        # Two events fire: REBALANCE_ARM and REBALANCE_WEB. Match the WEB one.
        web_events = [e for e in captured if e[1] == "REBALANCE_WEB"]
        assert len(web_events) == 1
        agent_id, _evt, detail = web_events[0]
        assert agent_id == "balance"
        assert "source=web_ui" in detail
        assert "transfer_ids=[7, 8]" in detail
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rebalance_confirm_replan_mismatch(monkeypatch):
    """The handler propagates a 'plan_changed' error from the agent's
    re-plan-on-confirm guard verbatim — the operator sees the same error
    string the agent emitted, and the response stays {ok:false}."""
    monkeypatch.setattr(settings, "SIM_MODE", True)
    monkeypatch.setattr("ui.web_server.db_queries.log_agent_event",
                        lambda *a, **k: None)
    agent = _build_balance_agent_mock(
        execute_result={"ok": False, "error": "plan_changed"},
    )
    coord = _build_coordinator(agents_by_id={"balance": agent})
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        await client.post("/action/rebalance", json={"action": "arm"})
        r = await client.post("/action/rebalance",
                              json={"action": "confirm", "confirm_token": "anything"})
        d = await r.json()
        assert d["ok"] is False
        assert d["error"] == "plan_changed"
    finally:
        await client.close()


# ════════════════════════════════════════════════════════════════════════════
# Web UI v3.1 — per-agent halt toggle
#
# Snapshot field + the two new endpoints. The coordinator is the public seam:
# tests drive a stub that mirrors Coordinator.halt_agent / resume_agent /
# is_agent_halted, so we don't need the real BaseAgent here.
# ════════════════════════════════════════════════════════════════════════════


def _halt_coordinator(*, agents=("signal", "arb", "scalp")):
    """Coordinator stub with in-memory halt state across `agents` ids.

    Exposes get_agent_stats so _snap_agents has rows to project halt onto.
    halt_agent/resume_agent mirror the real Coordinator envelope shape; the
    snapshot read goes through is_agent_halted, also defined here."""
    halted: dict[str, bool] = {a: False for a in agents}
    log: list = []

    def _halt(agent_id):
        if agent_id not in halted:
            return {"ok": False, "error": "agent_not_found"}
        halted[agent_id] = True
        log.append(("HALT_MANUAL", agent_id))
        return {"ok": True, "agent_id": agent_id, "halted": True}

    def _resume(agent_id):
        if agent_id not in halted:
            return {"ok": False, "error": "agent_not_found"}
        halted[agent_id] = False
        log.append(("RESUME_MANUAL", agent_id))
        return {"ok": True, "agent_id": agent_id, "halted": False}

    rows = [
        SimpleNamespace(agent_id=a, status="RUNNING", capital_allocated=100.0,
                        daily_pnl=0.0, trades_today=0, win_rate_today=0.0)
        for a in agents
    ]
    coord = SimpleNamespace(
        get_agent=lambda aid: None,
        get_primary_bot=lambda: None,
        get_portfolio_stats=AsyncMock(return_value={}),
        get_agent_stats=AsyncMock(return_value=rows),
        halt_agent=_halt,
        resume_agent=_resume,
        is_agent_halted=lambda aid: bool(halted.get(aid, False)),
    )
    coord._halt_log = log   # exposed for the log-event assertion
    return coord


# ── Snapshot ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_includes_manually_halted_per_agent():
    coord = _halt_coordinator()
    ws = WebServer(coordinator=coord, bot=None)
    await ws._refresh_coordinator()
    agents = ws._build_snapshot()["agents"]
    # Every agent row carries the new field; default False.
    assert agents, "expected at least one agent row"
    for a in agents:
        assert "manually_halted" in a, f"missing manually_halted on {a['id']}"
        assert a["manually_halted"] is False


# ── Halt / resume endpoints — happy paths ─────────────────────────────────

@pytest.mark.asyncio
async def test_halt_endpoint_sets_state():
    coord = _halt_coordinator()
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        r = await client.post("/action/agent/signal/halt")
        assert r.status == 200
        d = await r.json()
        assert d == {"ok": True, "agent_id": "signal", "halted": True}
        # Subsequent snapshot reflects the new state for that agent only.
        await ws._refresh_coordinator()
        snap_agents = {a["id"]: a for a in ws._build_snapshot()["agents"]}
        assert snap_agents["signal"]["manually_halted"] is True
        assert snap_agents["arb"]["manually_halted"]    is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_resume_endpoint_clears_state():
    coord = _halt_coordinator()
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        await client.post("/action/agent/signal/halt")
        r = await client.post("/action/agent/signal/resume")
        assert r.status == 200
        d = await r.json()
        assert d == {"ok": True, "agent_id": "signal", "halted": False}
        await ws._refresh_coordinator()
        snap_agents = {a["id"]: a for a in ws._build_snapshot()["agents"]}
        assert snap_agents["signal"]["manually_halted"] is False
    finally:
        await client.close()


# ── Unknown agent ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_halt_endpoint_unknown_agent_returns_404():
    coord = _halt_coordinator()
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        r = await client.post("/action/agent/nope/halt")
        assert r.status == 404
        d = await r.json()
        assert d == {"ok": False, "error": "agent_not_found"}
        # Same shape on resume.
        r2 = await client.post("/action/agent/nope/resume")
        assert r2.status == 404
        assert (await r2.json())["error"] == "agent_not_found"
    finally:
        await client.close()


# ── Idempotency ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_halt_endpoint_idempotent():
    coord = _halt_coordinator()
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        first  = await (await client.post("/action/agent/arb/halt")).json()
        second = await (await client.post("/action/agent/arb/halt")).json()
        assert first  == {"ok": True, "agent_id": "arb", "halted": True}
        assert second == {"ok": True, "agent_id": "arb", "halted": True}
        # Same for resume.
        r1 = await (await client.post("/action/agent/arb/resume")).json()
        r2 = await (await client.post("/action/agent/arb/resume")).json()
        assert r1 == {"ok": True, "agent_id": "arb", "halted": False}
        assert r2 == {"ok": True, "agent_id": "arb", "halted": False}
    finally:
        await client.close()


# ── Event logging on halt ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_halt_logs_event(monkeypatch):
    """The coordinator (which the web handler delegates to) logs a
    HALT_MANUAL event with the agent id. Match the kill/rebalance pattern
    where source=web_ui is in the detail string; this test asserts the
    coordinator's _halt_log captured the call when the endpoint fired.
    The full coordinator → db_queries.log_agent_event linkage is covered
    in tests/test_coordinator.py."""
    coord = _halt_coordinator()
    ws = WebServer(coordinator=coord, bot=None)
    client = await _client(ws)
    try:
        r = await client.post("/action/agent/scalp/halt")
        assert r.status == 200
        assert ("HALT_MANUAL", "scalp") in coord._halt_log
    finally:
        await client.close()


# ── LED price grid (GET /api/ticker + _TickerWorker row builder) ───────────

def _bulk_tickers(n: int = 10, quote: str = "USD") -> dict:
    """fetch_tickers-shaped payload: n pairs, descending 24h volume
    (C0 highest), all +1.5% on the day."""
    return {
        f"C{i}/{quote}": {"last": 100.0 + i, "percentage": 1.5,
                          "quoteVolume": 1_000.0 * (n - i)}
        for i in range(n)
    }


def _seed_ticker_cache(ws: WebServer, exchange: str, rows: list) -> dict:
    payload = {"ok": True, "exchange": exchange, "label": exchange.title(),
               "ts": "12:00:00", "rows": rows}
    ws._ticker_cache[exchange] = (time.time(), payload)
    return payload


def test_ticker_rows_from_bulk_sorts_and_carries_volume():
    """Row builder: dollar-quoted pairs ranked by 24h volume. Default is
    UNCAPPED (all pairs ship; the grid paginates client-side), each row
    carrying its 24h quote volume; an explicit cap still truncates."""
    rows = WebServer._ticker_rows_from_bulk(_bulk_tickers(300), ("USD",))
    assert len(rows) == 300                                      # all pairs
    assert [r["coin"] for r in rows[:3]] == ["C0", "C1", "C2"]   # volume order
    assert rows[0]["price"] == pytest.approx(100.0)
    assert rows[0]["pct"] == pytest.approx(1.5)
    assert rows[0]["vol"] == pytest.approx(300_000.0)            # volume metric
    capped = WebServer._ticker_rows_from_bulk(_bulk_tickers(300), ("USD",), cap=80)
    assert len(capped) == 80


def test_ticker_rows_from_bulk_skips_unusable():
    """Entries with no price, a non-dollar quote, or junk shapes are
    skipped; a missing pct is kept as None (the grid renders it flat)."""
    tickers = {
        "GOOD/USD":  {"last": 5.0, "percentage": 2.0, "quoteVolume": 100.0},
        "NOPX/USD":  {"last": None, "percentage": 1.0, "quoteVolume": 999.0},
        "EURQ/EUR":  {"last": 9.0, "percentage": 1.0, "quoteVolume": 999.0},
        "JUNK/USD":  {"last": "not-a-number", "quoteVolume": "x"},
        "NOPCT/USD": {"last": 7.0, "quoteVolume": 50.0},
    }
    rows = WebServer._ticker_rows_from_bulk(tickers, ("USD",), 80)
    assert {r["coin"] for r in rows} == {"GOOD", "NOPCT"}
    by = {r["coin"]: r for r in rows}
    assert by["NOPCT"]["pct"] is None


def test_ticker_rows_from_bulk_dedupes_per_base():
    """A base listed against two quotes keeps only its higher-volume row."""
    tickers = {
        "BTC/USD":  {"last": 64_000.0, "percentage": 1.0, "quoteVolume": 500.0},
        "BTC/USDT": {"last": 64_010.0, "percentage": 1.1, "quoteVolume": 900.0},
    }
    rows = WebServer._ticker_rows_from_bulk(tickers, ("USD", "USDT"), 80)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTC/USDT"   # higher volume wins


@pytest.mark.asyncio
async def test_ticker_endpoint_serves_worker_snapshot():
    """GET /api/ticker is a pure cache read of the worker's payload —
    instant, no network on the request path."""
    ws = WebServer(coordinator=None, bot=None)
    payload = _seed_ticker_cache(ws, "kraken",
                                 [{"coin": "BTC", "symbol": "BTC/USD",
                                   "price": 64_000.0, "pct": 1.5}])
    client = await _client(ws)
    try:
        r = await client.get("/api/ticker?exchange=kraken")
        assert r.status == 200
        data = await r.json()
        assert data == payload
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ticker_unknown_exchange_falls_back():
    """An unknown ?exchange= falls back to TICKER_DEFAULT_EXCHANGE."""
    ws = WebServer(coordinator=None, bot=None)
    _seed_ticker_cache(ws, settings.TICKER_DEFAULT_EXCHANGE,
                       [{"coin": "BTC", "symbol": "BTC/USD",
                         "price": 64_000.0, "pct": 1.5}])
    client = await _client(ws)
    try:
        r = await client.get("/api/ticker?exchange=nonsense_venue")
        data = await r.json()
        assert data["ok"] is True
        assert data["exchange"] == settings.TICKER_DEFAULT_EXCHANGE
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ticker_warming_when_worker_has_no_snapshot():
    """Before the worker produces a venue's first payload the endpoint
    answers {ok: false, error: 'warming up'} — instantly, never hanging."""
    ws = WebServer(coordinator=None, bot=None)
    client = await _client(ws)
    try:
        r = await client.get("/api/ticker?exchange=kraken")
        assert r.status == 200
        data = await r.json()
        assert data["ok"] is False
        assert data["error"] == "warming up"
        assert data["rows"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ticker_endpoint_never_raises():
    """Even a poisoned cache returns a JSON {ok: false} — never a 500."""
    class _Boom(dict):
        def get(self, *a, **k):
            raise RuntimeError("poisoned cache")

    ws = WebServer(coordinator=None, bot=None)
    ws._ticker_cache = _Boom()
    client = await _client(ws)
    try:
        r = await client.get("/api/ticker?exchange=kraken")
        assert r.status == 200
        data = await r.json()
        assert data["ok"] is False
        assert "error" in data
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ticker_worker_refresh_writes_cache_and_sticky_on_error(monkeypatch):
    """_TickerWorker._refresh_venue writes a payload into the server cache
    from one bulk call; a later failing refresh keeps the last good payload
    (sticky) instead of blanking the grid."""
    from ui.web_server import _TickerWorker

    ws = WebServer(coordinator=None, bot=None)
    worker = _TickerWorker(ws)
    stub_client = MagicMock()
    stub_client.fetch_tickers = AsyncMock(return_value=_bulk_tickers(10))
    stub_client.markets = {"loaded": True}
    ccxt_stub = MagicMock()
    clients = {"kraken": stub_client}

    await worker._refresh_venue(ccxt_stub, clients, "kraken")
    ts1, payload = ws._ticker_cache["kraken"]
    assert payload["ok"] is True
    assert payload["rows"][0]["coin"] == "C0"

    # Refresh fails → last good payload stays.
    stub_client.fetch_tickers = AsyncMock(side_effect=RuntimeError("venue down"))
    await worker._refresh_venue(ccxt_stub, clients, "kraken")
    _, payload2 = ws._ticker_cache["kraken"]
    assert payload2 == payload


# ── Coin-logo proxy (GET /api/coinlogo/{coin}) ─────────────────────────────

@pytest.mark.asyncio
async def test_coinlogo_served_from_cache():
    """A cached logo is served as SVG with browser caching — no upstream
    fetch on the request path once cached."""
    ws = WebServer(coordinator=None, bot=None)
    ws._logo_cache["btc"] = (b"<svg>btc</svg>", "image/svg+xml")
    client = await _client(ws)
    try:
        r = await client.get("/api/coinlogo/btc")
        assert r.status == 200
        assert r.headers["Content-Type"].startswith("image/svg")
        assert "max-age" in r.headers.get("Cache-Control", "")
        assert (await r.read()) == b"<svg>btc</svg>"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_coinlogo_known_missing_404s_fast():
    """A coin cached as None (known-missing) 404s without refetching —
    the grid falls back to its letter avatar."""
    ws = WebServer(coordinator=None, bot=None)
    ws._logo_cache["nope"] = None
    client = await _client(ws)
    try:
        r = await client.get("/api/coinlogo/nope")
        assert r.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_coinlogo_rejects_junk_names():
    """Non-alphanumeric / oversized coin names 404 without any fetch."""
    ws = WebServer(coordinator=None, bot=None)
    client = await _client(ws)
    try:
        r = await client.get("/api/coinlogo/..%2F..%2Fetc")
        assert r.status == 404
        r = await client.get("/api/coinlogo/waytoolongcoinname")
        assert r.status == 404
    finally:
        await client.close()


# ════════════════════════════════════════════════════════════════════════════
# Opportunity Scanner panel — `opportunities` snapshot block (read-only;
# $0 observation) + GET /api/opportunity_notes (the reasoning feed's pull).
#
# Fixture-row tests seed a fresh SQLite via web_temp (same reload pattern as
# the refresh-v1 block above); the key-presence and safe-fallback tests run
# against the real project DB like the v2 panel tests.
# ════════════════════════════════════════════════════════════════════════════


def _seed_opportunity(q, mk, *, risk="survivable", trend="zero", count=0,
                      edge=None, conf="low", reach="unknown", window="opening",
                      score=None, factors=None, rationale=None,
                      first_seen=None, labels_24h=None):
    """One core row + its hypothesis-log observation (and optional 24h
    forward labels) through the agent build's own query helpers."""
    first_seen = first_seen or datetime.utcnow()
    core_id = q.upsert_opportunity_core({
        "opp_type": "liquidation", "protocol": "morpho", "chain": "base",
        "market_key": mk, "asset_class": "defi_lending",
        "detector_id": "createmarket", "first_seen": first_seen,
        "risk_status": risk, "risk_flags": [],
        "competitor_trend": trend, "competitor_count": count,
        "edge_annualized_pct": edge, "edge_confidence": conf,
        "reachability_verdict": reach, "window_status": window,
        "unconventional_score": score,
        "unconventional_factors": factors or [],
        "unconventional_rationale": rationale,
        "as_of": datetime.utcnow(),
    })
    q.save_opportunity_observation({
        "core_id": core_id, "detector_id": "createmarket",
        "opp_type": "liquidation", "first_seen": first_seen,
        "feature_vector_json": {}, "detection_latency_ms": 1200.0,
        "unconventional_score": score,
        "unconventional_factors_json": factors or [],
        "unconventional_rationale": rationale,
    })
    if labels_24h:
        q.update_opportunity_labels(core_id, 24, labels_24h)
    return core_id


# ── Snapshot block ──────────────────────────────────────────────────────────

def test_snapshot_includes_opportunities_keys():
    """`opportunities` present with all sub-keys even with coordinator=None."""
    ws = WebServer(coordinator=None, bot=None)
    o = ws._build_snapshot()["opportunities"]
    for k in ("enabled", "observation_count", "detection_latency_ms_p50",
              "standard", "exploratory", "notes"):
        assert k in o, f"missing opportunities.{k}"
    assert isinstance(o["standard"], list)
    assert isinstance(o["exploratory"], list)
    assert isinstance(o["notes"], list)
    assert isinstance(o["enabled"], bool)


def test_snapshot_safe_when_opportunity_query_raises(monkeypatch):
    """A raising DB read returns the complete empty-shape block — the
    snapshot never raises (mirror test_snapshot_safe_when_coordinator_raises)."""
    def boom(*a, **k):
        raise RuntimeError("opportunity tables down")
    monkeypatch.setattr("ui.web_server.db_queries.get_ranked_opportunities", boom)
    monkeypatch.setattr("ui.web_server.db_queries.get_opportunity_summary", boom)
    monkeypatch.setattr("ui.web_server.db_queries.get_opportunity_notes", boom)
    ws = WebServer(coordinator=None, bot=None)
    snap = ws._build_snapshot()           # must not raise
    o = snap["opportunities"]
    assert o["standard"] == [] and o["exploratory"] == [] and o["notes"] == []
    assert o["observation_count"] == 0
    assert o["detection_latency_ms_p50"] is None


def test_opportunities_disabled_returns_placeholder_shape(monkeypatch):
    monkeypatch.setattr(settings, "OPPORTUNITY_SCANNER_ENABLED", False)
    ws = WebServer(coordinator=None, bot=None)
    o = ws._build_snapshot()["opportunities"]
    assert o["enabled"] is False
    assert o["standard"] == [] and o["exploratory"] == [] and o["notes"] == []


def test_opportunities_standard_and_exploratory_separate(web_temp):
    """Same survivor set, two lists: standard ordered by trajectory (NOT by
    unconventional_score), exploratory by score and carrying the rationale."""
    wsm, db, q = web_temp
    _seed_opportunity(q, "0xa", trend="zero", edge=10.0,
                      score=0.4, factors=["factor_stack"], rationale="early read")
    _seed_opportunity(q, "0xb", trend="rising_fast", edge=500.0,
                      score=0.9, factors=["contrarian_reachability"],
                      rationale="contrarian read")
    o = wsm.WebServer(coordinator=None, bot=None)._build_snapshot()["opportunities"]
    std_keys = [r["market_key"] for r in o["standard"]]
    exp_keys = [r["market_key"] for r in o["exploratory"]]
    assert std_keys == ["0xa", "0xb"]       # trajectory wins despite fatter edge
    assert exp_keys == ["0xb", "0xa"]       # score order — the separate lane
    assert set(std_keys) == set(exp_keys)   # same survivor set, never merged
    for r in o["exploratory"]:
        assert r["unconventional_rationale"]
        # Edge never relays without trajectory adjacent — in both lists.
        assert "competitor_trend" in r
    for r in o["standard"]:
        assert "competitor_trend" in r


def test_opportunities_exploratory_respects_min_score(web_temp, monkeypatch):
    wsm, db, q = web_temp
    monkeypatch.setattr(settings, "OPPORTUNITY_UNCONVENTIONAL_MIN_SCORE", 0.3)
    _seed_opportunity(q, "0xquiet", score=0.1, rationale="routine")
    _seed_opportunity(q, "0xloud",  score=0.8, rationale="bold")
    o = wsm.WebServer(coordinator=None, bot=None)._build_snapshot()["opportunities"]
    assert [r["market_key"] for r in o["exploratory"]] == ["0xloud"]
    # The standard view is untouched by the lane's noise floor.
    assert {r["market_key"] for r in o["standard"]} == {"0xquiet", "0xloud"}


def test_opportunities_disqualified_absent_by_default(web_temp, monkeypatch):
    wsm, db, q = web_temp
    monkeypatch.setattr(settings, "OPPORTUNITY_SHOW_DISQUALIFIED", False)
    _seed_opportunity(q, "0xbad", risk="disqualified", edge=999.0, score=0.9,
                      rationale="would have been juicy")
    _seed_opportunity(q, "0xok", score=0.9, rationale="fine")
    o = wsm.WebServer(coordinator=None, bot=None)._build_snapshot()["opportunities"]
    assert [r["market_key"] for r in o["standard"]]    == ["0xok"]
    assert [r["market_key"] for r in o["exploratory"]] == ["0xok"]


def test_opportunities_rows_capped(web_temp, monkeypatch):
    wsm, db, q = web_temp
    monkeypatch.setattr(settings, "OPPORTUNITY_PANEL_MAX_ROWS", 3)
    for i in range(6):
        _seed_opportunity(q, f"0x{i}", score=0.9, rationale=f"read {i}")
    o = wsm.WebServer(coordinator=None, bot=None)._build_snapshot()["opportunities"]
    assert len(o["standard"]) == 3
    assert len(o["exploratory"]) == 3
    assert len(o["notes"]) <= 3


# ── Reasoning-feed notes ────────────────────────────────────────────────────

def test_snapshot_notes_noteworthy_only(web_temp, monkeypatch):
    """The 2Hz snapshot pushes only noteworthy posts, newest first, capped;
    routine posts are a pull via the endpoint."""
    wsm, db, q = web_temp
    monkeypatch.setattr(settings, "OPPORTUNITY_UNCONVENTIONAL_MIN_SCORE", 0.3)
    t0 = datetime.utcnow()
    _seed_opportunity(q, "0xold", score=0.8, rationale="older noteworthy",
                      first_seen=t0 - timedelta(hours=2))
    _seed_opportunity(q, "0xroutine", score=0.1, rationale="routine note",
                      first_seen=t0 - timedelta(hours=1))
    _seed_opportunity(q, "0xnew", score=0.9, rationale="newer noteworthy",
                      first_seen=t0)
    o = wsm.WebServer(coordinator=None, bot=None)._build_snapshot()["opportunities"]
    bodies = [n["body"] for n in o["notes"]]
    assert bodies == ["newer noteworthy", "older noteworthy"]   # newest first
    assert all(n["noteworthy"] for n in o["notes"])


@pytest.mark.asyncio
async def test_opportunity_notes_endpoint_all_posts(web_temp, monkeypatch):
    wsm, db, q = web_temp
    monkeypatch.setattr(settings, "OPPORTUNITY_UNCONVENTIONAL_MIN_SCORE", 0.3)
    _seed_opportunity(q, "0xnote", score=0.8, rationale="noteworthy read")
    _seed_opportunity(q, "0xroutine", score=0.1, rationale="routine note")
    client = await _client(wsm.WebServer(coordinator=None, bot=None))
    try:
        d_all = await (await client.get(
            "/api/opportunity_notes?noteworthy=false&limit=50")).json()
        assert d_all["ok"] is True
        assert {n["body"] for n in d_all["notes"]} == {"noteworthy read",
                                                       "routine note"}
        d_nw = await (await client.get(
            "/api/opportunity_notes?noteworthy=true&limit=50")).json()
        assert d_nw["ok"] is True
        assert [n["body"] for n in d_nw["notes"]] == ["noteworthy read"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_opportunity_notes_endpoint_safe_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("ui.web_server.db_queries.get_opportunity_notes", boom)
    ws = WebServer(coordinator=None, bot=None)
    client = await _client(ws)
    try:
        r = await client.get("/api/opportunity_notes?noteworthy=false")
        assert r.status == 200
        d = await r.json()
        assert d["ok"] is False
        assert "error" in d
    finally:
        await client.close()


# ════════════════════════════════════════════════════════════════════════════
# Web UI v2 fixes item 4 — win-rate convention pinning.
#
# Verdict of the source→screen trace: NOT inverted. The convention is
#   - query / agent layer:  FRACTION 0–1, win = pnl > 0
#       get_signal_win_rate → wins/total          (queries.py)
#       get_arb_stats       → wins/total          (queries.py)
#       ScalpingAgent._win_rate_today → wins/len  (scalping_agent.py)
#   - AgentStats.win_rate_today / win_rate_alltime: fraction (base.py)
#   - coordinator.get_portfolio_stats overall_win_rate_today:
#       trade-weighted mean of fractions → fraction
#   - ui/web_server.py multiplies by 100 EXACTLY ONCE
#       (_snap_portfolio + _snap_agents)
#   - frontend renders toFixed(1)+"%" with no further transform.
# These tests pin that contract so a future 1-x / double-×100 regression
# fails loudly.
# ════════════════════════════════════════════════════════════════════════════


def test_win_rate_convention_query_fraction_snapshot_percent(web_temp):
    """3 wins of 4 closed trades → 0.75 at the query layer, 75.0 in the box."""
    wsm, db, q = web_temp
    for pnl in (1.0, 2.0, 0.5):
        _seed_trade(db, pnl_usd=pnl, pnl_pct=pnl)
    _seed_trade(db, pnl_usd=-1.0, pnl_pct=-1.0)
    wr = q.get_signal_win_rate(days=1, exclude_strategy="scalp")
    assert wr["total"] == 4 and wr["wins"] == 3 and wr["losses"] == 1
    assert wr["win_rate"] == pytest.approx(0.75)      # fraction at this layer
    snap = wsm.WebServer(coordinator=None, bot=None)._build_snapshot()
    # ×100 exactly once on the way to the Win Rate box — 75.0, not 25.0,
    # not 0.75, not 7500.0.
    assert snap["portfolio"]["win_rate_alltime"] == pytest.approx(75.0)


@pytest.mark.asyncio
async def test_win_rate_today_and_agent_cards_not_inverted():
    """portfolio.win_rate_today and agents[].win_rate are percent renderings
    of the coordinator's 0–1 fractions — no 1−x, no second ×100."""
    rows = [SimpleNamespace(agent_id="signal", status="RUNNING",
                            capital_allocated=100.0, daily_pnl=0.0,
                            trades_today=4, win_rate_today=0.75)]
    coord = SimpleNamespace(
        get_agent=lambda aid: None,
        get_primary_bot=lambda: None,
        get_portfolio_stats=AsyncMock(
            return_value={"overall_win_rate_today": 0.75}),
        get_agent_stats=AsyncMock(return_value=rows),
    )
    ws = WebServer(coordinator=coord, bot=None)
    await ws._refresh_coordinator()
    snap = ws._build_snapshot()
    assert snap["portfolio"]["win_rate_today"] == pytest.approx(75.0)
    agents = {a["id"]: a for a in snap["agents"]}
    assert agents["signal"]["win_rate"] == pytest.approx(75.0)


def test_note_outcome_badge_field_present(web_temp):
    """A note whose window resolved carries the was_right stamp + summary;
    an unresolved note has them null (the UI renders no badge)."""
    wsm, db, q = web_temp
    _seed_opportunity(q, "0xresolved", score=0.8, rationale="paid off",
                      first_seen=datetime.utcnow() - timedelta(hours=30),
                      labels_24h={"realized_competitor_count": 0,
                                  "realized_edge_decay": 12.0,
                                  "window_status_at_horizon": "open"})
    _seed_opportunity(q, "0xpending", score=0.8, rationale="jury's out")
    notes = {n["body"]: n for n in q.get_opportunity_notes(
        noteworthy_only=True, limit=10)}
    resolved, pending = notes["paid off"], notes["jury's out"]
    assert resolved["was_right"] == "correct"
    assert "window open" in resolved["outcome_summary"]
    assert pending["was_right"] is None
    assert pending["outcome_summary"] is None
