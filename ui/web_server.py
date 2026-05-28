"""
ui/web_server.py

Browser-based operator control panel. A lightweight aiohttp server that
runs alongside the bot, pushes a full state snapshot over a WebSocket at
WEB_UI_PUSH_INTERVAL_S, and exposes operator actions (approve / skip /
kill / pause / set-mode / approve-window) as REST POST endpoints.

LOCAL OPERATOR TOOL ONLY — no auth, no SSL, localhost/LAN. Never exposed
to the internet.

Design notes
------------
- Data access mirrors ui/dashboard.py exactly: the coordinator's getters
  are async (awaited in the broadcast loop and cached); everything else
  is read defensively via getattr / try-except so _build_snapshot never
  raises and always returns a complete dict.
- Neither `coordinator` nor `bot` is assumed non-None anywhere. The bot
  is created lazily by the coordinator, so it's resolved per-tick via
  coordinator.get_primary_bot() rather than captured once.
- The HTML frontend is read from disk once at start() and served from
  memory thereafter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from aiohttp import web

from config import settings
from database import queries as db_queries

logger = logging.getLogger(__name__)

_HTML_PATH = Path(__file__).parent / "web_dashboard.html"
_VALID_MODES = ("per_trade", "window", "autonomous")

# Agent ids that have a dedicated page. Matches REGISTERED_AGENTS exactly —
# note the sentiment placeholder registers as "sentiment_agent".
_VALID_AGENTS = ("signal", "arb", "scalp", "macro", "sentiment_agent", "onchain")
_SESSIONS = ("LONDON", "NEW_YORK", "ASIA", "OFF_HOURS")
# Anchor city per session for the local clock + session-page header.
_SESSION_TZ = {
    "LONDON":    ("Europe/London",    "LON"),
    "NEW_YORK":  ("America/New_York",  "NYC"),
    "ASIA":      ("Asia/Tokyo",        "TYO"),
    "OFF_HOURS": ("UTC",               "UTC"),
}


def _session_for_hour(hour: int) -> str:
    if 7 <= hour < 13:
        return "LONDON"
    if 13 <= hour < 20:
        return "NEW_YORK"
    if 0 <= hour < 7:
        return "ASIA"
    return "OFF_HOURS"


def _local_time(tz: str) -> str:
    """HH:MM local time for an IANA tz, computed server-side for the session
    endpoint. Falls back to UTC if the zone isn't available."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz)).strftime("%H:%M")
    except Exception:
        return datetime.utcnow().strftime("%H:%M")


# ─────────────────────────────────────────────────────────────────────────
# WebLogHandler — feeds the in-UI log panel
# ─────────────────────────────────────────────────────────────────────────

class WebLogHandler(logging.Handler):
    """Appends structured entries to a bounded deque the snapshot reads.

    Installed on the root logger in WebServer.start() so it captures the
    whole app's log stream (module loggers propagate to root). Classifies
    each record into one of the UI log types by level + keyword."""

    def __init__(self, buffer: deque):
        super().__init__(level=logging.INFO)
        self._buffer = buffer

    @staticmethod
    def _classify(record: logging.LogRecord, msg: str) -> str:
        m = msg.lower()
        if "kill" in m:
            return "kill"
        if record.levelno >= logging.ERROR:
            return "err"
        if record.levelno >= logging.WARNING:
            return "warn"
        if "arb" in m:
            return "arb"
        if "skip" in m:
            return "skip"
        if "execut" in m:
            return "exec"
        return "info"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            self._buffer.append({
                "ts":     datetime.utcnow().strftime("%H:%M"),
                "type":   self._classify(record, msg),
                "title":  msg[:160],
                "detail": f"{record.name} · {record.levelname}",
            })
        except Exception:
            # A logging handler must never raise.
            pass


# ─────────────────────────────────────────────────────────────────────────
# WebServer
# ─────────────────────────────────────────────────────────────────────────

class WebServer:
    def __init__(self, coordinator=None, bot=None):
        self._coordinator = coordinator
        self._bot = bot                      # may be None — resolved lazily
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site = None
        self._ws_clients: set = set()
        self._broadcast_task: Optional[asyncio.Task] = None
        self._running = False
        self._start_ts = time.time()
        self._html: Optional[str] = None

        self._log_buffer: deque = deque(maxlen=50)
        self._log_handler: Optional[WebLogHandler] = None

        # Caches for async coordinator getters, refreshed by _broadcast_loop.
        self._portfolio_cache: dict = {}
        self._agents_cache: list = []

    # ── Lifecycle ────────────────────────────────────────────────────────

    def _make_app(self) -> web.Application:
        """Build the aiohttp app + routes. Shared by start() and tests."""
        app = web.Application()
        app.add_routes([
            web.get("/",   self.handle_index),
            web.get("/ws", self.handle_ws),
            web.get("/api/agent/{agent_id}",       self.handle_agent_detail),
            web.get("/api/session/{session_name}", self.handle_session_detail),
            web.post("/action/approve",        self.handle_approve),
            web.post("/action/skip",           self.handle_skip),
            web.post("/action/kill",           self.handle_kill),
            web.post("/action/pause",          self.handle_pause),
            web.post("/action/set_mode",       self.handle_set_mode),
            web.post("/action/approve_window", self.handle_approve_window),
            web.post("/action/rebalance",      self.handle_rebalance),
        ])
        return app

    def _load_html(self) -> str:
        try:
            return _HTML_PATH.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"web_dashboard.html not readable: {e}")
            return (
                "<!doctype html><meta charset=utf-8>"
                "<title>CryptoBot</title>"
                "<body style='font-family:monospace'>web_dashboard.html "
                "missing — server is up; connect a WS client to /ws.</body>"
            )

    async def start(self) -> None:
        self._html = self._load_html()
        self._app = self._make_app()

        # Capture the whole app's log stream for the UI log panel.
        self._log_handler = WebLogHandler(self._log_buffer)
        logging.getLogger().addHandler(self._log_handler)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, settings.WEB_UI_HOST, settings.WEB_UI_PORT)
        await self._site.start()

        self._running = True
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        logger.info(
            f"Web UI started at http://{settings.WEB_UI_HOST}:{settings.WEB_UI_PORT}"
        )

    async def stop(self) -> None:
        self._running = False
        if self._broadcast_task is not None and not self._broadcast_task.done():
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except (asyncio.CancelledError, Exception):
                pass
        for ws in list(self._ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._ws_clients.clear()
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        logger.info("Web UI stopped")

    # ── Broadcast loop ───────────────────────────────────────────────────

    async def _broadcast_loop(self) -> None:
        while self._running:
            try:
                await self._refresh_coordinator()
                payload = json.dumps(self._build_snapshot(), default=str)
                await self._broadcast(payload)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"web broadcast loop: {e}")
            try:
                await asyncio.sleep(settings.WEB_UI_PUSH_INTERVAL_S)
            except asyncio.CancelledError:
                break

    async def _refresh_coordinator(self) -> None:
        """Await the coordinator's async getters into sync-readable caches."""
        coord = self._coordinator
        if coord is None:
            return
        getter = getattr(coord, "get_portfolio_stats", None)
        if callable(getter):
            try:
                self._portfolio_cache = await getter() or {}
            except Exception as e:
                logger.debug(f"web portfolio refresh: {e}")
        getter = getattr(coord, "get_agent_stats", None)
        if callable(getter):
            try:
                self._agents_cache = await getter() or []
            except Exception as e:
                logger.debug(f"web agent refresh: {e}")

    async def _broadcast(self, payload: str) -> None:
        dead = []
        for ws in list(self._ws_clients):
            try:
                await ws.send_str(payload)
            except Exception as e:
                logger.debug(f"ws send failed: {e}")
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)

    # ── Bot resolution ───────────────────────────────────────────────────

    def _resolve_bot(self):
        """Prefer an injected bot (tests); else the coordinator's signal-agent
        bot (created lazily once the coordinator starts). None when neither."""
        if self._bot is not None:
            return self._bot
        coord = self._coordinator
        if coord is None:
            return None
        getter = getattr(coord, "get_primary_bot", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    # ── WebSocket + static handlers ──────────────────────────────────────

    async def handle_index(self, request) -> web.Response:
        return web.Response(text=self._html or self._load_html(),
                            content_type="text/html")

    async def handle_ws(self, request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        # Send an immediate snapshot so a fresh client paints without waiting.
        try:
            await ws.send_str(json.dumps(self._build_snapshot(), default=str))
        except Exception as e:
            logger.debug(f"ws initial snapshot: {e}")
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.ERROR:
                    break
        finally:
            self._ws_clients.discard(ws)
        return ws

    # ── REST action handlers ─────────────────────────────────────────────

    @staticmethod
    async def _body(request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    async def handle_approve(self, request) -> web.Response:
        bot = self._resolve_bot()
        if bot is None:
            return web.json_response({"ok": False, "error": "no bot"})
        try:
            ok = await bot.approve_next_pending()
            return web.json_response({"ok": bool(ok)})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_skip(self, request) -> web.Response:
        bot = self._resolve_bot()
        if bot is None:
            return web.json_response({"ok": False, "error": "no bot"})
        body = await self._body(request)
        reason = body.get("reason", "web_skip")
        try:
            ok = await bot.skip_next_pending(reason)
            return web.json_response({"ok": bool(ok)})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_kill(self, request) -> web.Response:
        coord = self._coordinator
        if coord is None or not hasattr(coord, "kill_all"):
            return web.json_response({"ok": False, "error": "no coordinator"})
        try:
            result = await coord.kill_all(reason="web")
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})
        try:
            db_queries.log_agent_event("portfolio", "KILL_WEB", "kill via web UI")
        except Exception as e:
            logger.debug(f"log KILL_WEB: {e}")
        return web.json_response({"ok": True, "result": result})

    async def handle_pause(self, request) -> web.Response:
        bot = self._resolve_bot()
        if bot is None:
            return web.json_response({"ok": False, "error": "no bot"})
        body = await self._body(request)
        paused = bool(body.get("paused", True))
        try:
            bot._paused = paused
            return web.json_response({"ok": True, "paused": paused})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_set_mode(self, request) -> web.Response:
        body = await self._body(request)
        mode = body.get("mode")
        if mode not in _VALID_MODES:
            return web.json_response(
                {"ok": False, "error": f"invalid mode (use {_VALID_MODES})"})
        try:
            settings.APPROVAL_MODE = mode
            bot = self._resolve_bot()
            if bot is not None and hasattr(bot, "approval_mode"):
                bot.approval_mode = mode   # harmless if the attr isn't used
            return web.json_response({"ok": True, "mode": mode})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_rebalance(self, request) -> web.Response:
        """Two-step arm → confirm with token. The BalanceAgent owns the
        token state; this endpoint dispatches by request body shape.

        Body shapes:
          { "action": "arm" }                        — issue + return token
          { "action": "confirm", "token": "<hex>" }  — consume token,
              return summary on success
        """
        coord = self._coordinator
        if coord is None or not hasattr(coord, "get_agent"):
            return web.json_response({"ok": False, "error": "no coordinator"})
        balance_agent = coord.get_agent("balance")
        if balance_agent is None:
            return web.json_response({"ok": False, "error": "no balance agent"})
        body = await self._body(request)
        action = body.get("action")
        if action == "arm":
            try:
                token, notice = balance_agent.arm()
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)})
            try:
                db_queries.log_agent_event(
                    "balance", "REBALANCE_ARM", "armed via web UI",
                )
            except Exception:
                pass
            return web.json_response({
                "ok": True, "token": token, "ring_fence": notice,
            })
        if action == "confirm":
            token = body.get("token", "")
            try:
                ok = balance_agent.consume_arm(token)
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)})
            if not ok:
                return web.json_response({
                    "ok": False, "error": "invalid or expired token",
                })
            try:
                db_queries.log_agent_event(
                    "balance", "REBALANCE_CONFIRM", "confirmed via web UI",
                )
            except Exception:
                pass
            # The agent's own scan loop performs the planned moves —
            # the confirm here just consumes the token and authorises
            # the next cycle. Returns immediately; the operator watches
            # the balance panel for the move.
            return web.json_response({"ok": True})
        return web.json_response({
            "ok": False, "error": "action must be 'arm' or 'confirm'",
        })

    async def handle_approve_window(self, request) -> web.Response:
        bot = self._resolve_bot()
        if bot is None:
            return web.json_response({"ok": False, "error": "no bot"})
        body = await self._body(request)
        minutes = int(body.get("minutes", 60))
        try:
            until = bot.approve_window(minutes)
            return web.json_response({"ok": True, "until": str(until)})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    # ── REST detail endpoints (agent + session pages) ────────────────────

    def _agent_trades(self, agent_id: str) -> list:
        """Trade log for an agent page. Each fund reads from its own ledger;
        the placeholder agents (macro / sentiment_agent / onchain) have none."""
        if agent_id == "signal":
            return self._safe(lambda: db_queries.get_signal_trade_history(100), [])
        if agent_id == "arb":
            return self._safe(db_queries.get_arb_trades_all, [])
        if agent_id == "scalp":
            return self._safe(lambda: db_queries.get_scalp_trade_history(100), [])
        return []

    async def handle_agent_detail(self, request) -> web.Response:
        """GET /api/agent/{agent_id} → {trades, insights} for the agent page.
        Unknown ids 404. Placeholders return empty lists."""
        agent_id = request.match_info.get("agent_id", "")
        if agent_id not in _VALID_AGENTS:
            return web.json_response({"error": "unknown agent"}, status=404)
        trades = self._agent_trades(agent_id)
        insights = self._safe(
            lambda: db_queries.get_postmortems_by_agent(agent_id, 3), [])
        return web.json_response(
            {"trades": trades, "insights": insights},
            dumps=lambda o: json.dumps(o, default=str),
        )

    async def handle_session_detail(self, request) -> web.Response:
        """GET /api/session/{session_name} → today's closed trades for that
        session, aggregated across funds. Unknown sessions 404."""
        session = request.match_info.get("session_name", "").upper()
        if session not in _SESSIONS:
            return web.json_response({"error": "unknown session"}, status=404)
        trades = self._safe(
            lambda: db_queries.get_closed_trades_by_session(session, "today"), [])
        try:
            total_pnl = sum(float(t.get("pnl_usd", 0.0) or 0.0) for t in trades)
        except Exception:
            total_pnl = 0.0
        tz, _label = _SESSION_TZ.get(session, ("UTC", "UTC"))
        return web.json_response({
            "session":     session,
            "local_time":  _local_time(tz),
            "tz":          tz,
            "total_pnl":   round(total_pnl, 2),
            "trade_count": len(trades),
            "trades":      trades,
        }, dumps=lambda o: json.dumps(o, default=str))

    # ── Snapshot ─────────────────────────────────────────────────────────

    def _build_snapshot(self) -> dict:
        """Complete state snapshot. Every field has a safe fallback — this
        must never raise. Coordinator data comes from caches refreshed by
        the async loop; everything else is read defensively here."""
        now = datetime.utcnow()
        bot = self._resolve_bot()
        arb = self._safe(db_queries.get_arb_trades_all, [])
        # Positions are read once per push: live exposure (Change 3) is the sum
        # of every open position's size, recomputed here — never cached. Scalp
        # spiking up/down as positions open/close in seconds is expected.
        positions = self._snap_positions(bot)
        try:
            exposure_usd = sum(float(p.get("size_usd", 0.0) or 0.0) for p in positions)
        except Exception:
            exposure_usd = 0.0
        return {
            "ts":            now.strftime("%H:%M:%S"),
            "session":       _session_for_hour(now.hour),
            "uptime_s":      int(time.time() - self._start_ts),
            "mode":          "SIM" if settings.SIM_MODE else "LIVE",
            "paused":        bool(getattr(bot, "_paused", False)),
            "approval_mode": getattr(settings, "APPROVAL_MODE", "per_trade"),
            "portfolio":         self._snap_portfolio(exposure_usd),
            "agents":            self._snap_agents(),
            "circuit_breakers":  self._snap_circuit_breakers(bot),
            "regime":            self._snap_regime(),
            "sentiment":         self._snap_sentiment(bot),
            "exchanges":         self._snap_exchanges(bot),
            "signals":           self._snap_signals(),
            "pending_signal":    self._snap_pending(bot),
            "positions":         positions,
            "scalp":             self._snap_scalp(bot),
            "arb_feed":          arb[:10],
            "session_pnl":       self._safe(db_queries.get_session_pnl_today, {
                s: {"pnl": 0.0, "trades": 0}
                for s in ("LONDON", "NEW_YORK", "ASIA", "OFF_HOURS")
            }),
            "log":            list(self._log_buffer)[::-1],
            "top_pairs":      self._safe(lambda: db_queries.get_top_pairs(5), []),
            "top_strategies": self._safe(db_queries.get_strategy_performance, []),
            "insights":       self._safe(lambda: db_queries.get_recent_postmortems(3), []),
            "arb_history":    arb,
            "balance":        self._snap_balance(),
        }

    def _snap_balance(self) -> dict:
        """Balance panel snapshot: per-fund allocation, per-node
        effective balance, in-transit transfers, halted routes,
        today's transfer count + fees, per-fund return-on-deployed.
        Degrades gracefully — every field has a safe fallback."""
        out = {
            "allocations":          {},
            "in_transit":           [],
            "paused_routes":        [],
            "transfers_today":      0,
            "fees_today_usd":       0.0,
            "fund_efficiency":      {},
        }
        try:
            from agents.balance.inventory_state import inventory_state
            snap = inventory_state.get_snapshot()
            out["allocations"]   = snap.get("allocations", {})
            out["in_transit"]    = snap.get("pending", [])
            out["paused_routes"] = snap.get("paused_routes", [])
        except Exception as e:
            logger.debug("balance snapshot inventory_state failed: %s", e)
        try:
            moves = db_queries.get_capital_movements_today()
            out["transfers_today"] = len(moves)
            # Sim fee is a flat constant; live tracks per-tx fees later.
            fee = float(getattr(settings, "SIM_WITHDRAWAL_FEE_USD", 1.0))
            out["fees_today_usd"] = sum(
                fee for m in moves if (m.state or "") == "completed"
            )
        except Exception as e:
            logger.debug("balance snapshot capital_movements failed: %s", e)
        try:
            eff = {}
            for fund in ("signal", "arb", "mexc_scalp"):
                row = db_queries.get_latest_fund_efficiency(fund)
                if row is None:
                    continue
                eff[fund] = {
                    "deployed_usd": float(row.deployed_usd or 0.0),
                    "return_pct":   float(row.return_on_deployed_pct or 0.0),
                    "starved":      bool(row.starvation_event),
                }
            out["fund_efficiency"] = eff
        except Exception as e:
            logger.debug("balance snapshot efficiency failed: %s", e)
        return out

    @staticmethod
    def _safe(fn, fallback):
        try:
            out = fn()
            return out if out is not None else fallback
        except Exception as e:
            logger.debug(f"web snapshot field failed: {e}")
            return fallback

    def _snap_portfolio(self, exposure_usd: float = 0.0) -> dict:
        """Bankroll is persistent (Change 1): STARTING_CAPITAL_TOTAL + all-time
        realised P&L, recomputed from the ledger every snapshot so it can never
        drift or reset. Daily P&L is the only metric that resets at UTC
        midnight; daily_pnl_pct + exposure_pct are now expressed against
        bankroll. exposure_usd is passed in from the live positions array."""
        p = self._portfolio_cache or {}
        starting = (
            float(getattr(settings, "FUND_SIGNAL_CAPITAL", 0.0) or 0.0)
            + float(getattr(settings, "FUND_ARB_CAPITAL", 0.0) or 0.0)
            + float(getattr(settings, "FUND_MEXC_SCALP_CAPITAL", 0.0) or 0.0)
        )
        realised = float(self._safe(db_queries.get_alltime_realised_pnl, 0.0) or 0.0)
        bankroll = starting + realised
        daily_pnl = float(p.get("total_daily_pnl", 0.0) or 0.0)
        daily_fees = float(self._safe(db_queries.get_daily_fees, 0.0) or 0.0)
        daily_pnl_pct = (daily_pnl / bankroll * 100.0) if bankroll > 0 else 0.0
        exposure_pct = (exposure_usd / bankroll * 100.0) if bankroll > 0 else 0.0
        wr_all = self._safe(lambda: db_queries.get_signal_win_rate(
            days=365, exclude_strategy="scalp"), {})
        return {
            "bankroll":             round(bankroll, 2),
            "bankroll_alltime_pnl": round(realised, 2),
            "daily_pnl":            round(daily_pnl, 2),
            "daily_pnl_pct":        round(daily_pnl_pct, 2),
            "daily_fees":           round(daily_fees, 2),
            "exposure_pct":         round(exposure_pct, 1),
            "exposure_usd":         round(exposure_usd, 2),
            "win_rate_today":       round(float(p.get("overall_win_rate_today", 0.0) or 0.0) * 100, 1),
            "win_rate_alltime":     round(float(wr_all.get("win_rate", 0.0) or 0.0) * 100, 1),
            "trades_today":         int(p.get("total_trades_today", 0) or 0),
        }

    def _snap_scalp(self, bot) -> dict:
        """Scalp feed (Change 5): live (open) scalp positions on top, persisted
        closed trades below — newest first, capped at WEB_UI_SCALP_FEED_HISTORY.
        The DB backs closed_trades, so they survive restarts and don't vanish
        when newer trades arrive."""
        limit = int(getattr(settings, "WEB_UI_SCALP_FEED_HISTORY", 30) or 30)
        return {
            "live_trades":   self._snap_scalp_live(bot),
            "closed_trades": self._safe(
                lambda: db_queries.get_scalp_trade_history(limit), []),
        }

    def _snap_scalp_live(self, bot) -> list:
        """Open scalp positions from the scalp agent's in-memory book, each with
        a running unrealised-bps figure. Defensive: returns [] when the
        coordinator, the scalp agent, or its positions aren't reachable."""
        coord = self._coordinator
        agent = None
        if coord is not None:
            getter = getattr(coord, "get_agent", None)
            if callable(getter):
                try:
                    agent = getter("scalp")
                except Exception:
                    agent = None
        positions = getattr(agent, "_positions", None) or {}
        md = getattr(bot, "_market_data", None)
        now = time.time()
        out = []
        for pos in list(positions.values()):
            try:
                entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
                direction = getattr(pos, "direction", "?")
                current = entry
                if md is not None:
                    try:
                        pr = md.get_price(getattr(pos, "exchange", None),
                                          getattr(pos, "symbol", None))
                        if pr:
                            current = float(pr)
                    except Exception:
                        pass
                if entry > 0 and current > 0:
                    raw_bps = (current - entry) / entry * 10000.0
                    unreal = raw_bps if direction != "SHORT" else -raw_bps
                else:
                    unreal = 0.0
                entry_time = float(getattr(pos, "entry_time", 0.0) or 0.0)
                hold = int(now - entry_time) if entry_time else 0
                out.append({
                    "symbol":         getattr(pos, "symbol", "?"),
                    "exchange":       getattr(pos, "exchange", "?"),
                    "direction":      direction,
                    "entry_price":    round(entry, 6),
                    "tp_price":       round(float(getattr(pos, "tp_price", 0.0) or 0.0), 6),
                    "sl_price":       round(float(getattr(pos, "sl_price", 0.0) or 0.0), 6),
                    "unrealised_bps": round(unreal, 1),
                    "hold_sec":       hold,
                })
            except Exception:
                continue
        return out

    def _snap_agents(self) -> list:
        out = []
        for a in (self._agents_cache or []):
            try:
                out.append({
                    "id":           getattr(a, "agent_id", "?"),
                    "status":       getattr(a, "status", "OFFLINE"),
                    "capital":      round(float(getattr(a, "capital_allocated", 0.0) or 0.0), 2),
                    "daily_pnl":    round(float(getattr(a, "daily_pnl", 0.0) or 0.0), 2),
                    "trades_today": int(getattr(a, "trades_today", 0) or 0),
                    "win_rate":     round(float(getattr(a, "win_rate_today", 0.0) or 0.0) * 100, 1),
                })
            except Exception:
                continue
        return out

    def _snap_circuit_breakers(self, bot) -> dict:
        cb = getattr(bot, "_cb_state", None)
        breakers = getattr(settings, "CIRCUIT_BREAKERS", {})

        def _limit(key, field, default):
            try:
                return float(breakers.get(key, {}).get(field, default))
            except Exception:
                return float(default)

        daily_pnl_pct = float(getattr(cb, "daily_pnl_pct", 0.0) or 0.0) if cb else 0.0
        drawdown_pct = float(getattr(cb, "drawdown_pct", 0.0) or 0.0) if cb else 0.0
        return {
            "daily_loss_pct":     round(max(0.0, -daily_pnl_pct), 2),
            "daily_loss_limit":   _limit("daily_loss", "threshold_pct", 2.0),
            "consecutive_losses": int(getattr(cb, "consecutive_losses", 0) or 0) if cb else 0,
            "consecutive_limit":  int(_limit("consecutive_loss", "count", 5)),
            "drawdown_pct":       round(max(0.0, -drawdown_pct), 2),
            "drawdown_limit":     _limit("drawdown", "threshold_pct", 5.0),
        }

    def _snap_regime(self) -> dict:
        out = {"dominant": "—", "adx": None, "hurst": None,
               "stable_minutes": None, "pairs": []}
        try:
            from core.regime_detector import regime_detector
            snaps = regime_detector.all_regimes() or []
        except Exception:
            return out
        if not snaps:
            return out
        slow_tf = getattr(settings, "SLOW_TIMEFRAME", "1h")
        slow = [s for s in snaps if getattr(s, "timeframe", None) == slow_tf]
        if slow:
            counts = Counter(s.regime for s in slow)
            out["dominant"] = str(counts.most_common(1)[0][0]).upper()
            first = slow[0]
            out["adx"] = round(float(first.adx), 1) if getattr(first, "adx", None) else None
            out["hurst"] = round(float(first.hurst), 2) if getattr(first, "hurst", None) else None
            out["pairs"] = [
                {"pair": s.pair, "regime": str(s.regime).upper()} for s in slow
            ]
        return out

    def _snap_sentiment(self, bot) -> dict:
        out = {"fear_greed": None, "fear_greed_label": "—", "reddit_score": None,
               "news_guard": "CLEAR", "btc_30m_delta": None, "composite": None,
               "modifier_applied": None}
        sent = getattr(bot, "_sentiment", None)
        latest = getattr(sent, "latest", {}) if sent is not None else {}
        if not latest:
            return out
        out["fear_greed"]       = latest.get("fear_greed_value")
        out["fear_greed_label"] = latest.get("fear_greed_label", "—")
        out["reddit_score"]     = latest.get("reddit_score")
        out["news_guard"]       = "ACTIVE" if latest.get("news_guard_active") else "CLEAR"
        out["btc_30m_delta"]    = latest.get("btc_change_30m")
        out["composite"]        = latest.get("composite_score")
        out["modifier_applied"] = latest.get("sentiment_modifier")
        return out

    def _snap_exchanges(self, bot) -> list:
        md = getattr(bot, "_market_data", None)
        live = {}
        try:
            live = getattr(md, "_exchanges", {}) or {}
        except Exception:
            live = {}
        out = []
        for name in getattr(settings, "ENABLED_EXCHANGES", []):
            out.append({
                "name":       name.title(),
                "status":     "live" if name in live else "nokey",
                "ping_ms":    None,
                "last_feed_s": None,
            })
        return out

    def _snap_signals(self) -> list:
        try:
            rows = db_queries.get_signal_history(days=1) or []
        except Exception:
            return []
        out = []
        for r in rows[:15]:
            try:
                ts = getattr(r, "timestamp", None)
                out.append({
                    "ts":          ts.strftime("%H:%M") if ts else "—",
                    "pair":        getattr(r, "pair", "?"),
                    "track":       getattr(r, "signal_type", "?"),
                    "score":       round(float(getattr(r, "score", 0) or 0), 0),
                    "regime":      getattr(r, "regime", "—") or "—",
                    "action":      getattr(r, "decision", None) or getattr(r, "action", None) or "—",
                    "skip_reason": getattr(r, "skip_reason", "") or "",
                    "agent":       "signal",
                })
            except Exception:
                continue
        return out

    def _snap_pending(self, bot):
        peek = getattr(bot, "peek_pending", None)
        if not callable(peek):
            return None
        try:
            sig = peek()
        except Exception:
            return None
        if sig is None:
            return None
        return {
            "ts":            datetime.utcnow().strftime("%H:%M"),
            "pair":          getattr(sig, "pair", "?"),
            "track":         getattr(sig, "signal_type", "?"),
            "score":         round(float(getattr(sig, "score", 0) or 0), 0),
            "regime":        "—",
            "action":        "pending",
            "skip_reason":   "",
            "agent":         "signal",
            "direction":     getattr(sig, "direction", "?"),
            "entry":         getattr(sig, "suggested_entry", None),
            "sl":            getattr(sig, "suggested_sl", None),
            "tp":            getattr(sig, "suggested_tp", None),
            "size":          getattr(sig, "suggested_size_pct", None),
            "rr":            getattr(sig, "risk_reward", None),
            "claude_summary": getattr(sig, "claude_reasoning", "") or "",
        }

    def _snap_positions(self, bot) -> list:
        try:
            open_trades = db_queries.get_open_trades() or []
        except Exception:
            return []
        md = getattr(bot, "_market_data", None)
        out = []
        now = datetime.utcnow()
        for t in open_trades:
            try:
                entry = float(getattr(t, "entry_price", 0.0) or 0.0)
                side = (getattr(t, "side", "") or "").upper()
                current = entry
                if md is not None:
                    try:
                        p = md.get_price(getattr(t, "exchange", None), getattr(t, "pair", None))
                        if p:
                            current = float(p)
                    except Exception:
                        pass
                size_usd = float(getattr(t, "size_usd", 0.0) or 0.0)
                if entry > 0 and current > 0:
                    raw = (current - entry) / entry
                    pnl_pct = raw * 100 * (1 if side != "SHORT" else -1)
                    pnl_usd = (raw if side != "SHORT" else -raw) * size_usd
                else:
                    pnl_pct = pnl_usd = 0.0
                opened = getattr(t, "timestamp_open", None)
                age_s = int((now - opened).total_seconds()) if opened else 0
                out.append({
                    "agent":       getattr(t, "strategy", None) or "signal",
                    "pair":        getattr(t, "pair", "?"),
                    "direction":   side or "—",
                    "entry":       round(entry, 4),
                    "current":     round(current, 4),
                    "size_usd":    round(size_usd, 2),
                    "pnl_usd":     round(pnl_usd, 2),
                    "pnl_pct":     round(pnl_pct, 2),
                    "stop_loss":   getattr(t, "stop_loss", None),
                    "take_profit": getattr(t, "take_profit", None),
                    "age_s":       age_s,
                })
            except Exception:
                continue
        return out
