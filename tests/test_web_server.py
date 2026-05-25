"""
tests/test_web_server.py — WebServer snapshot, WebSocket push, REST actions.

Uses aiohttp's TestClient/TestServer (ephemeral ports, no fixed-port
conflicts) and mocks for coordinator/bot. The snapshot reads the real
queries layer defensively, so these run against the project DB without
special fixtures.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from config import settings
from ui.web_server import WebServer, WebLogHandler

_TOP_LEVEL_KEYS = (
    "ts", "session", "uptime_s", "mode", "paused", "approval_mode",
    "portfolio", "agents", "circuit_breakers", "regime", "sentiment",
    "exchanges", "signals", "pending_signal", "positions", "arb_feed",
    "session_pnl", "log", "top_pairs", "top_strategies", "insights",
    "arb_history",
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
    assert snap["portfolio"]["equity"] == 0.0


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
