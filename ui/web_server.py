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
import math
import threading
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

# Agent ids that have a dedicated page. Matches REGISTERED_AGENTS exactly.
# Web UI v2 dropped the macro / sentiment_agent / onchain placeholders;
# added xchain / funding_arb / balance now that those agents are real.
_VALID_AGENTS = ("signal", "arb", "scalp", "xchain", "funding_arb", "balance",
                 "opportunity_scanner", "follow")
# Hosts treated as local for the wallet-flow privacy guard (the panel surfaces
# wallet addresses, so it 403s unless the server binds to a loopback host).
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
_SESSIONS = ("LONDON", "NEW_YORK", "ASIA", "OFF_HOURS")
# Anchor city per session for the local clock + session-page header.
_SESSION_TZ = {
    "LONDON":    ("Europe/London",    "LON"),
    "NEW_YORK":  ("America/New_York",  "NYC"),
    "ASIA":      ("Asia/Tokyo",        "TYO"),
    "OFF_HOURS": ("UTC",               "UTC"),
}

# LED price grid — per-venue registry for GET /api/ticker. Label + accepted
# quote currencies (preference order, used only to filter the bulk
# fetch_tickers payload to dollar-quoted pairs). All five venues support
# ccxt's bulk fetch_tickers (verified 2026-06-04), so one API call per
# refresh carries every pair with its 24h % included — no per-pair calls.
_TICKER_REGISTRY = {
    "kraken":      {"label": "Kraken",            "quotes": ("USD", "USDT", "USDC")},
    "binance":     {"label": "Binance",           "quotes": ("USDT", "USDC")},
    "coinbase":    {"label": "Coinbase",          "quotes": ("USD", "USDT", "USDC")},
    "bybit":       {"label": "Bybit",             "quotes": ("USDT", "USDC")},
    "hyperliquid": {"label": "Hyperliquid (DEX)", "quotes": ("USDC",)},
}


class _TickerWorker(threading.Thread):
    """Dedicated thread + private event loop for the LED grid's venue fetches.

    ccxt's load_markets / bulk fetch_tickers do heavy JSON parsing — run on
    the dashboard's event loop they starve the 0.5s WebSocket push and every
    HTTP request (observed: three concurrent load_markets stalled the whole
    UI). This worker owns its OWN ccxt clients on its OWN loop (async-ccxt
    clients are loop-bound, so none are borrowed from MarketData), serially
    round-robins every configured venue, and writes finished payloads into
    the server's _ticker_cache. The /api/ticker handler is then a pure cache
    read — zero network on the request path. A venue's last good payload is
    sticky: a failed refresh keeps showing it rather than blanking the grid.
    """

    daemon = True

    def __init__(self, server: "WebServer"):
        super().__init__(name="led-ticker-worker")
        self._server = server
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as e:
            logger.warning(f"ticker worker died: {e}")

    async def _main(self) -> None:
        import ccxt.async_support as ccxt_async
        clients: dict = {}
        try:
            while not self._stop_evt.is_set():
                venues = [v for v in getattr(settings, "TICKER_EXCHANGES", [])
                          if v in _TICKER_REGISTRY]
                for ex in venues:
                    if self._stop_evt.is_set():
                        break
                    await self._refresh_venue(ccxt_async, clients, ex)
                # Pace the cycle: each venue refreshes about every
                # TICKER_POLL_INTERVAL_S; 1s ticks keep shutdown snappy.
                for _ in range(int(getattr(settings, "TICKER_POLL_INTERVAL_S", 15) or 15)):
                    if self._stop_evt.is_set():
                        break
                    await asyncio.sleep(1)
        finally:
            for client in clients.values():
                try:
                    await client.close()
                except Exception:
                    pass

    async def _refresh_venue(self, ccxt_async, clients: dict, ex: str) -> None:
        reg = _TICKER_REGISTRY[ex]
        try:
            client = clients.get(ex)
            if client is None:
                klass = getattr(ccxt_async, ex, None)
                if klass is None:
                    return
                client = klass({"enableRateLimit": True})
                clients[ex] = client
            timeout = float(getattr(settings, "TICKER_FETCH_TIMEOUT_S", 10) or 10)
            # First fetch per venue includes load_markets — allow 3×.
            if not getattr(client, "markets", None):
                timeout *= 3
            tickers = await asyncio.wait_for(client.fetch_tickers(), timeout=timeout)
            # No cap — ALL usable pairs ship; the grid paginates client-side.
            rows = WebServer._ticker_rows_from_bulk(
                tickers, tuple(reg.get("quotes") or ("USD", "USDT", "USDC")))
            if not rows:
                raise RuntimeError("no usable tickers")
            self._server._ticker_cache[ex] = (time.time(), {
                "ok":       True,
                "exchange": ex,
                "label":    reg["label"],
                "ts":       datetime.utcnow().strftime("%H:%M:%S"),
                "rows":     rows,
            })
        except Exception as e:
            logger.debug(f"ticker worker {ex}: {type(e).__name__}: {e}")
            # Sticky last-good: only write an error payload when we have
            # nothing at all for this venue yet.
            if ex not in self._server._ticker_cache:
                self._server._ticker_cache[ex] = (time.time(), {
                    "ok": False, "exchange": ex, "label": reg["label"],
                    "error": (str(e) or type(e).__name__)[:120], "rows": [],
                })


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


def _jsonable(obj):
    """Recursively coerce a snapshot into JSON-spec-compliant Python.

    json.dumps writes float('inf') / -inf / nan as the literal tokens
    Infinity / -Infinity / NaN, which the browser's JSON.parse rejects
    (RFC 8259 doesn't permit them). One stray non-finite float anywhere
    in the snapshot would silently take the whole dashboard down. This
    walker replaces them with None so the UI renders a placeholder
    instead of imploding. Cheap — runs once per push tick."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


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

        # LED ticker — per-venue payload cache {exchange_id: (ts, payload)},
        # written by the dedicated _TickerWorker thread (started in start());
        # the /api/ticker handler only ever READS it.
        self._ticker_cache: dict = {}
        self._ticker_worker: Optional[_TickerWorker] = None
        # Coin-logo proxy cache {sym: (bytes, content_type) | None}. None =
        # definitively missing from EVERY source (404 fast, no refetch);
        # the browser only ever talks to the bot. Session/semaphore/imgmap
        # are lazy (loop-bound primitives can't be built off-loop here).
        self._logo_cache: dict = {}
        self._logo_session = None
        self._logo_sem: Optional[asyncio.Semaphore] = None
        self._cc_imgmap: Optional[dict] = None
        self._cc_imgmap_lock: Optional[asyncio.Lock] = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def _make_app(self) -> web.Application:
        """Build the aiohttp app + routes. Shared by start() and tests."""
        app = web.Application()
        app.add_routes([
            web.get("/",   self.handle_index),
            web.get("/ws", self.handle_ws),
            web.get("/api/agent/{agent_id}",       self.handle_agent_detail),
            web.get("/api/session/{session_name}", self.handle_session_detail),
            web.get("/api/ticker",                 self.handle_ticker),
            web.get("/api/coinlogo/{coin}",        self.handle_coinlogo),
            web.get("/api/opportunity_notes",      self.handle_opportunity_notes),
            # Wallet-flow watcher — wallet-identifying, so localhost-guarded (403
            # otherwise). On-demand historical/heavy reads, NOT in the snapshot.
            web.get("/api/walletflow/wallet/{address}", self.handle_walletflow_wallet),
            web.get("/api/walletflow/candidates",       self.handle_walletflow_candidates),
            web.post("/api/walletflow/candidate/confirm", self.handle_walletflow_confirm),
            web.post("/api/walletflow/candidate/reject",  self.handle_walletflow_reject),
            web.get("/api/walletflow/health",           self.handle_walletflow_health),
            # On-demand viz aggregation (charts) — read-only, derived from stored
            # events, NEVER in the 2Hz snapshot. Same localhost privacy guard.
            web.get("/api/walletflow/viz/flow_series",  self.handle_walletflow_viz_flow),
            # Copy-trade leaderboard observer — surfaces TRADER IDENTITIES, so
            # localhost-guarded (403 otherwise), same privacy rule as walletflow.
            web.get("/api/copytrade/actor/{actor_id}", self.handle_copytrade_actor),
            web.get("/api/copytrade/surfaced",         self.handle_copytrade_surfaced),
            web.get("/api/copytrade/denominator",      self.handle_copytrade_denominator),
            web.get("/api/copytrade/health",           self.handle_copytrade_health),
            web.get("/api/copytrade/viz/skill",        self.handle_copytrade_viz_skill),
            # Meme-coin rug-rate scorer — surfaces wallet/funder IDENTITIES, so
            # localhost-guarded (403 otherwise), same privacy rule as walletflow.
            web.get("/api/meme/launch/{mint}", self.handle_meme_launch),
            web.get("/api/meme/asof/{funder}", self.handle_meme_asof),
            web.get("/api/meme/health",        self.handle_meme_health),
            web.post("/action/approve",        self.handle_approve),
            web.post("/action/skip",           self.handle_skip),
            web.post("/action/kill",           self.handle_kill),
            web.post("/action/pause",          self.handle_pause),
            web.post("/action/set_mode",       self.handle_set_mode),
            web.post("/action/approve_window", self.handle_approve_window),
            web.post("/action/rebalance",      self.handle_rebalance),
            web.post("/action/agent/{agent_id}/halt",   self.handle_agent_halt),
            web.post("/action/agent/{agent_id}/resume", self.handle_agent_resume),
            web.post("/action/agent_pause",             self.handle_agent_pause),
        ])
        return app

    def _load_html(self) -> str:
        try:
            html = _HTML_PATH.read_text(encoding="utf-8")
            # Inject the settings-driven ticker config (venues / default /
            # poll cadence) into the page so the frontend never hardcodes
            # them. The placeholder sits inside a single-quoted JS string;
            # JSON only contains double quotes, so the substitution is safe.
            try:
                cfg = json.dumps({
                    "exchanges": [v for v in getattr(settings, "TICKER_EXCHANGES", [])
                                  if v in _TICKER_REGISTRY],
                    "labels": {v: _TICKER_REGISTRY[v]["label"]
                               for v in getattr(settings, "TICKER_EXCHANGES", [])
                               if v in _TICKER_REGISTRY},
                    "default": getattr(settings, "TICKER_DEFAULT_EXCHANGE", "kraken"),
                    "poll_s": int(getattr(settings, "TICKER_POLL_INTERVAL_S", 15) or 15),
                    "boxes": int(getattr(settings, "TICKER_GRID_BOXES", 16) or 16),
                    "rows_per_box": int(getattr(settings, "TICKER_ROWS_PER_BOX", 5) or 5),
                })
                html = html.replace("__TICKER_CFG_JSON__", cfg)
            except Exception as e:
                logger.debug(f"ticker cfg injection skipped: {e}")
            return html
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
        # LED grid worker — its own thread + event loop so venue fetching
        # never competes with the dashboard loop.
        self._ticker_worker = _TickerWorker(self)
        self._ticker_worker.start()
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
        # Stop the LED grid worker; it closes its own clients on its own
        # loop. Bounded join off-loop so shutdown can't hang on it.
        if self._ticker_worker is not None:
            self._ticker_worker.stop()
            try:
                await asyncio.to_thread(self._ticker_worker.join, 3)
            except Exception:
                pass
            self._ticker_worker = None
        if self._logo_session is not None:
            try:
                await self._logo_session.close()
            except Exception:
                pass
            self._logo_session = None
        logger.info("Web UI stopped")

    # ── Broadcast loop ───────────────────────────────────────────────────

    async def _broadcast_loop(self) -> None:
        while self._running:
            try:
                await self._refresh_coordinator()
                payload = json.dumps(
                    _jsonable(self._build_snapshot()),
                    default=str,
                    allow_nan=False,
                )
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
        # Re-read the HTML from disk on every GET and forbid browser caching.
        # The old startup-cached self._html meant UI edits silently required a
        # bot restart AND a hard refresh to show up — a stale tab's WebSocket
        # reconnects after a restart, so the page looks live while running old
        # markup. Disk read is ~85KB on localhost; cost is negligible.
        return web.Response(text=self._load_html(),
                            content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def handle_ws(self, request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        # Send an immediate snapshot so a fresh client paints without waiting.
        try:
            await ws.send_str(json.dumps(
                _jsonable(self._build_snapshot()),
                default=str,
                allow_nan=False,
            ))
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
        """Web UI v2 three-action /action/rebalance.

        Bodies:
          {"action": "arm"}
              → {"ok": True, "confirm_token": str,
                 "expires_in_s": int, "proposal": {...},
                 "ring_fence_warnings": [str, ...]}
          {"action": "confirm", "confirm_token": str}
              → {"ok": True, "transfer_ids": [int, ...]}
                | {"ok": False, "error": "<reason>"}
          {"action": "cancel", "confirm_token": str}
              → {"ok": True}

        Errors:
          balance_agent_unavailable, live_rebalance_disabled,
          token_invalid, token_expired, no_pending_plan, plan_changed,
          kill_blocked, replan_failed:<msg>, invalid_action.

        The handler never raises; agent exceptions are wrapped in the
        error envelope.
        """
        coord = self._coordinator
        if coord is None or not hasattr(coord, "get_agent"):
            return web.json_response({
                "ok": False, "error": "balance_agent_unavailable",
            })
        balance_agent = coord.get_agent("balance")
        if balance_agent is None:
            return web.json_response({
                "ok": False, "error": "balance_agent_unavailable",
            })
        body = await self._body(request)
        action = body.get("action")

        if action == "arm":
            try:
                token, _notice = balance_agent.arm()
                proposal = balance_agent.get_pending_proposal()
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)})
            warnings = [
                t.get("ring_fence_warning") for t in (proposal.get("transfers") or [])
                if t.get("ring_fence_warning")
            ]
            try:
                db_queries.log_agent_event(
                    "balance", "REBALANCE_ARM", "armed via web UI",
                )
            except Exception:
                pass
            return web.json_response({
                "ok":                  True,
                "confirm_token":       token,
                "expires_in_s":        int(
                    getattr(settings, "REBALANCE_ARM_TIMEOUT_S", 10) or 10
                ),
                "proposal":            proposal,
                "ring_fence_warnings": warnings,
            })

        if action == "confirm":
            # Defence-in-depth: the agent also gates on this, but failing
            # fast here keeps the error stream identical even if the agent
            # is mocked in tests.
            if (not settings.SIM_MODE and
                    not bool(getattr(settings, "REBALANCE_LIVE_ENABLED", False))):
                return web.json_response({
                    "ok": False, "error": "live_rebalance_disabled",
                })
            token = body.get("confirm_token", "")
            try:
                executor = balance_agent.execute_proposal(token)
                if asyncio.iscoroutine(executor):
                    result = await executor
                else:
                    result = executor
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)})
            if not isinstance(result, dict) or not result.get("ok"):
                return web.json_response(
                    result if isinstance(result, dict)
                    else {"ok": False, "error": "agent_returned_non_dict"}
                )
            transfer_ids = result.get("transfer_ids", []) or []
            try:
                db_queries.log_agent_event(
                    "balance", "REBALANCE_WEB",
                    f"source=web_ui transfer_ids={transfer_ids}",
                )
            except Exception:
                pass
            return web.json_response({
                "ok": True, "transfer_ids": transfer_ids,
            })

        if action == "cancel":
            try:
                balance_agent.consume_arm(body.get("confirm_token", ""))
            except Exception:
                pass
            return web.json_response({"ok": True})

        return web.json_response({"ok": False, "error": "invalid_action"})

    async def handle_agent_halt(self, request) -> web.Response:
        """Web UI v3.1 — POST /action/agent/{agent_id}/halt.

        Body: empty. Single-click (no arm/confirm). Halt is reversible —
        the cost of a misclick is at most a single skipped scan cycle, so
        the operator friction of an arm/confirm would defeat the point.
        Always {ok:bool, ...}; agent exceptions are wrapped in the envelope.
        """
        return await self._handle_agent_halt_action(request, halt=True)

    async def handle_agent_resume(self, request) -> web.Response:
        """Web UI v3.1 — POST /action/agent/{agent_id}/resume.
        Same shape as halt; halted=False on success."""
        return await self._handle_agent_halt_action(request, halt=False)

    async def _handle_agent_halt_action(self, request, *, halt: bool) -> web.Response:
        coord = self._coordinator
        agent_id = request.match_info.get("agent_id", "")
        if coord is None:
            return web.json_response({"ok": False, "error": "no coordinator"})
        method_name = "halt_agent" if halt else "resume_agent"
        method = getattr(coord, method_name, None)
        if not callable(method):
            return web.json_response(
                {"ok": False, "error": f"coordinator has no {method_name}"})
        try:
            result = method(agent_id)
        except Exception as e:
            logger.error("%s(%s) failed: %s", method_name, agent_id, e,
                         exc_info=True)
            return web.json_response({"ok": False, "error": str(e)})
        if (isinstance(result, dict)
                and not result.get("ok")
                and result.get("error") == "agent_not_found"):
            return web.json_response(result, status=404)
        return web.json_response(result)

    @staticmethod
    def _breaker_state(agent) -> tuple:
        """(halted_by_breaker, reason) for an agent. A circuit-breaker halt
        is distinct from an operator PAUSE: signal carries it on
        bot._cb_state (with the reason string the breaker fired with), arb
        on the engine's HALTED status (reason synthesised from its
        counters), anything else on a literal HALTED lifecycle status.
        Never raises."""
        try:
            # Signal agent — CryptoBot's CircuitBreakerState is authoritative.
            bot = getattr(agent, "bot", None) or getattr(agent, "_bot", None)
            cb = getattr(bot, "_cb_state", None)
            if cb is not None and bool(getattr(cb, "halted", False)):
                return True, str(getattr(cb, "halt_reason", None)
                                 or "circuit_breaker")
            # Arb agent — engine flips to HALTED while _cb_triggered() holds.
            # No reason string on the engine; synthesise from the counters.
            engine = getattr(agent, "_engine", None)
            if (engine is not None
                    and str(getattr(engine, "_status", "")).upper() == "HALTED"):
                losses = int(getattr(engine, "_consecutive_losses", 0) or 0)
                pnl = float(getattr(engine, "_daily_pnl_usd", 0.0) or 0.0)
                if losses >= int(getattr(settings, "ARB_CONSECUTIVE_LOSS_HALT",
                                         10**9) or 10**9):
                    return True, f"consecutive_loss {losses}"
                return True, f"daily_loss ${pnl:.2f}"
            # Generic — any agent whose lifecycle status reads HALTED.
            if str(getattr(agent, "status", "")).upper() == "HALTED":
                return True, "circuit_breaker"
        except Exception:
            pass
        return False, ""

    async def handle_agent_pause(self, request) -> web.Response:
        """Web UI v2 fixes item 3 — POST /action/agent_pause
        body: {"agent_id": str, "paused": bool, "override_breaker": bool}

        paused=true  → await agent.pause()                (AGENT_PAUSE)
        paused=false → await agent.resume()               (AGENT_RESUME)
          - breaker-halted + override_breaker=false →
              {"ok": false, "error": "halted_by_breaker",
               "breaker_reason": "<reason>"} WITHOUT resuming. The frontend
              then prompts the explicit arm→confirm override.
          - breaker-halted + override_breaker=true → clear the breaker for
            this agent, resume, and log a distinct AGENT_BREAKER_OVERRIDE
            event. The breakers themselves are untouched — this is the
            audited operator escape hatch the RUNBOOK warns about, never a
            silent bypass.
        Always {"ok": bool, ...}; never raises to aiohttp."""
        coord = self._coordinator
        if coord is None or not hasattr(coord, "get_agent"):
            return web.json_response({"ok": False, "error": "no coordinator"})
        body = await self._body(request)
        agent_id = str(body.get("agent_id", "") or "")
        try:
            agent = coord.get_agent(agent_id)
        except Exception:
            agent = None
        if agent is None:
            return web.json_response({"ok": False, "error": "unknown agent"})
        paused = bool(body.get("paused", True))
        override = bool(body.get("override_breaker", False))

        def _log(event_type: str, detail: str = "") -> None:
            try:
                db_queries.log_agent_event(
                    agent_id, event_type,
                    "source=web_ui" + (f" {detail}" if detail else ""))
            except Exception as e:
                logger.debug("log %s: %s", event_type, e)

        try:
            if paused:
                await agent.pause()
                _log("AGENT_PAUSE")
                return web.json_response(
                    {"ok": True, "status": getattr(agent, "status", "PAUSED")})

            # Resume / play.
            halted, reason = self._breaker_state(agent)
            if halted and not override:
                return web.json_response({
                    "ok": False, "error": "halted_by_breaker",
                    "breaker_reason": reason,
                })
            if halted and override:
                # Deliberate operator override — clear this agent's CB state
                # plus the coordinator-level trackers (same set resume_agent
                # clears), logged loudly and as its own event type.
                try:
                    agent.clear_circuit_breakers()
                except Exception as e:
                    logger.debug("clear_circuit_breakers(%s): %s", agent_id, e)
                try:
                    fund_halted = getattr(coord, "_fund_halted", None)
                    if fund_halted is not None:
                        fund_halted.discard(agent_id)
                    if getattr(coord, "_halted_by_portfolio_cb", False):
                        coord._halted_by_portfolio_cb = False
                except Exception:
                    pass
                logger.critical(
                    "BREAKER OVERRIDE — operator resumed %s past '%s' via "
                    "web UI", agent_id, reason)
                _log("AGENT_BREAKER_OVERRIDE", f"breaker_reason={reason}")
            await agent.resume()
            _log("AGENT_RESUME")
            return web.json_response(
                {"ok": True, "status": getattr(agent, "status", "RUNNING")})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

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
            dumps=lambda o: json.dumps(_jsonable(o), default=str, allow_nan=False),
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
        }, dumps=lambda o: json.dumps(_jsonable(o), default=str, allow_nan=False))

    async def handle_opportunity_notes(self, request) -> web.Response:
        """GET /api/opportunity_notes?noteworthy={true|false}&limit={int}
        → {"ok": true, "notes": [...]} — the reasoning feed's "all posts"
        view and pagination (a PULL; the 2Hz snapshot only pushes the
        noteworthy default). READ-ONLY: writes nothing; the kill switch
        remains the web UI's only DB-writing endpoint. Errors come back
        as {"ok": false, "error": ...}, never as a raise."""
        try:
            noteworthy = (request.query.get("noteworthy", "true").lower()
                          != "false")
            try:
                limit = int(request.query.get(
                    "limit", getattr(settings, "OPPORTUNITY_PANEL_MAX_ROWS", 12)))
            except (TypeError, ValueError):
                limit = int(getattr(settings, "OPPORTUNITY_PANEL_MAX_ROWS", 12))
            limit = max(1, min(limit, 500))
            notes = db_queries.get_opportunity_notes(
                noteworthy_only=noteworthy, limit=limit)
            return web.json_response(
                {"ok": True, "notes": notes},
                dumps=lambda o: json.dumps(_jsonable(o), default=str,
                                           allow_nan=False),
            )
        except Exception as e:
            logger.debug("opportunity_notes endpoint failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)})

    # ── Wallet-flow watcher (/api/walletflow/*) ──────────────────────────
    # PRIVACY HARD RULE: this panel serves wallet addresses/identities. Every
    # endpoint 403s unless the server binds to a loopback host — wallet
    # identities must never leave the operator's machine over a LAN bind.

    def _walletflow_guard(self) -> Optional[web.Response]:
        """Return a 403 response when WEB_UI_HOST is not loopback, else None.
        Keys off the configured bind host per the privacy rule."""
        host = str(getattr(settings, "WEB_UI_HOST", "localhost") or "").lower()
        if host not in _LOCAL_HOSTS:
            return web.json_response(
                {"ok": False, "error": "forbidden_non_local_host",
                 "detail": "wallet-flow data is localhost-only"}, status=403)
        return None

    async def handle_walletflow_wallet(self, request) -> web.Response:
        """GET /api/walletflow/wallet/{address} → point-in-time skill score +
        evidence + as-of reconstruction. The as-of reconstruction calls the
        SAME shared point-in-time function as the scorer (one code path)."""
        guard = self._walletflow_guard()
        if guard is not None:
            return guard
        from follow import skill_scorer
        address = request.match_info.get("address", "")
        as_of = None
        raw = request.query.get("as_of")
        if raw:
            try:
                as_of = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                as_of = None
        try:
            score = skill_scorer.score_wallet(address, as_of)
            as_of_dt = as_of or datetime.utcnow()
            # SAME shared as-of query the scorer uses — not a second copy.
            evidence = skill_scorer.resolved_actions_as_of(address, as_of_dt)
            return web.json_response(
                {"ok": True, "wallet": address, "score": score,
                 "evidence": evidence[-50:], "as_of": as_of_dt.isoformat()},
                dumps=lambda o: json.dumps(_jsonable(o), default=str,
                                           allow_nan=False))
        except Exception as e:
            logger.debug("walletflow wallet endpoint failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_walletflow_candidates(self, request) -> web.Response:
        """GET /api/walletflow/candidates → pending candidates WITH warnings
        (bait-resistance flags surfaced, never auto-applied)."""
        guard = self._walletflow_guard()
        if guard is not None:
            return guard
        try:
            cands = db_queries.get_pending_candidates(limit=200)
            return web.json_response(
                {"ok": True, "candidates": cands},
                dumps=lambda o: json.dumps(_jsonable(o), default=str,
                                           allow_nan=False))
        except Exception as e:
            logger.debug("walletflow candidates endpoint failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_walletflow_confirm(self, request) -> web.Response:
        """POST /api/walletflow/candidate/confirm {address} → candidate ->
        confirmed (operator action — the ONLY path to confirmed)."""
        guard = self._walletflow_guard()
        if guard is not None:
            return guard
        from follow import provenance as fp
        body = await self._body(request)
        address = str(body.get("address", "") or "")
        if not address:
            return web.json_response({"ok": False, "error": "missing_address"})
        try:
            res = fp.confirm(address)
            if res.get("ok"):
                db_queries.set_candidate_review_state(address, "approved")
                db_queries.log_agent_event("follow", "WALLET_CONFIRM",
                                           f"source=web_ui address={address}")
            return web.json_response(res)
        except Exception as e:
            logger.error("walletflow confirm(%s) failed: %s", address, e)
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_walletflow_reject(self, request) -> web.Response:
        """POST /api/walletflow/candidate/reject {address} → candidate ->
        rejected (operator action; permanent, never re-proposed)."""
        guard = self._walletflow_guard()
        if guard is not None:
            return guard
        from follow import provenance as fp
        body = await self._body(request)
        address = str(body.get("address", "") or "")
        if not address:
            return web.json_response({"ok": False, "error": "missing_address"})
        try:
            res = fp.reject(address)
            if res.get("ok"):
                db_queries.log_agent_event("follow", "WALLET_REJECT",
                                           f"source=web_ui address={address}")
            return web.json_response(res)
        except Exception as e:
            logger.error("walletflow reject(%s) failed: %s", address, e)
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_walletflow_health(self, request) -> web.Response:
        """GET /api/walletflow/health → label-set staleness, coverage, and the
        latency-δ distribution over recent flow events."""
        guard = self._walletflow_guard()
        if guard is not None:
            return guard
        out = {"ok": True, "label_staleness": {}, "coverage": {},
               "latency_delta_s": {"p50": None, "p90": None, "n": 0}}
        try:
            out["label_staleness"] = db_queries.label_set_staleness()
        except Exception as e:
            logger.debug("walletflow health staleness: %s", e)
        try:
            agent = self._get_agent("follow")
            snap = (agent.get_walletflow_snapshot()
                    if agent is not None and hasattr(agent, "get_walletflow_snapshot")
                    else {})
            out["coverage"] = {
                "sources": snap.get("sources", []),
                "pending_candidates": snap.get("pending_candidates", 0),
                "running": snap.get("running", False),
            }
        except Exception as e:
            logger.debug("walletflow health coverage: %s", e)
        try:
            out["latency_delta_s"] = self._walletflow_delta_distribution()
        except Exception as e:
            logger.debug("walletflow health delta: %s", e)
        return web.json_response(out)

    @staticmethod
    def _walletflow_delta_distribution() -> dict:
        """p50/p90 of (detected_at - occurred_at) over recent flow events —
        the followability grade. Empty-safe."""
        rows = db_queries.get_recent_wallet_flow_events(limit=200)
        deltas = []
        for r in rows:
            occ, det = r.get("occurred_at"), r.get("detected_at")
            if not occ or not det:
                continue
            try:
                d = (datetime.fromisoformat(det) - datetime.fromisoformat(occ)).total_seconds()
                deltas.append(d)
            except (TypeError, ValueError):
                continue
        if not deltas:
            return {"p50": None, "p90": None, "n": 0}
        deltas.sort()
        def _pct(p):
            idx = min(len(deltas) - 1, int(p * len(deltas)))
            return round(deltas[idx], 2)
        return {"p50": _pct(0.5), "p90": _pct(0.9), "n": len(deltas)}

    async def handle_walletflow_viz_flow(self, request) -> web.Response:
        """GET /api/walletflow/viz/flow_series → ON-DEMAND chart data: exchange
        inflow-vs-outflow net flow bucketed over time, plus the watchlist
        provenance breakdown (candidate/confirmed/manual/rejected + trust
        expiring soon). Pure read-side aggregation over already-stored events —
        adds NOTHING to the 2Hz snapshot. Localhost-guarded (wallet-identifying).
        Empty-safe: zeroed buckets/counts when nothing is stored."""
        guard = self._walletflow_guard()
        if guard is not None:
            return guard
        out = {"ok": True,
               "series":     {"window_h": 0, "buckets": [], "n_events": 0},
               "provenance": {"counts": {"candidate": 0, "confirmed": 0,
                                         "manual": 0, "rejected": 0},
                              "expiring_soon": 0, "total": 0},
               "best_effort": False, "gaps_24h": 0}
        try:
            out["series"] = db_queries.get_wallet_flow_series(
                buckets=int(getattr(settings, "WEB_UI_VIZ_FLOW_BUCKETS", 24)),
                window_h=int(getattr(settings, "WEB_UI_VIZ_FLOW_WINDOW_H", 24)),
                scan_limit=int(getattr(settings, "WEB_UI_VIZ_EVENT_SCAN_LIMIT", 500)))
        except Exception as e:
            logger.debug("walletflow viz series failed: %s", e)
        try:
            out["provenance"] = db_queries.get_watchlist_provenance_counts(
                expiry_soon_h=int(getattr(settings, "WEB_UI_VIZ_TRUST_EXPIRY_SOON_H", 24)))
        except Exception as e:
            logger.debug("walletflow viz provenance failed: %s", e)
        # Best-effort / gap markers — same honesty surface as the live panel.
        try:
            agent = self._get_agent("follow")
            snap = (agent.get_walletflow_snapshot()
                    if agent is not None and hasattr(agent, "get_walletflow_snapshot")
                    else {})
            srcs = snap.get("sources", []) or []
            out["best_effort"] = any(s.get("best_effort") for s in srcs)
            out["gaps_24h"] = sum(int(s.get("gaps_24h", 0) or 0) for s in srcs)
        except Exception as e:
            logger.debug("walletflow viz health markers failed: %s", e)
        return web.json_response(
            out, dumps=lambda o: json.dumps(_jsonable(o), default=str,
                                            allow_nan=False))

    # ── Copy-trade leaderboard observer (/api/copytrade/*) ───────────────
    # PRIVACY HARD RULE: this panel serves trader identities. Every endpoint
    # 403s unless the server binds to a loopback host — identical to the
    # wallet-flow rule (identities must never leave the operator's machine).

    def _copytrade_guard(self) -> Optional[web.Response]:
        """Return a 403 response when WEB_UI_HOST is not loopback, else None."""
        host = str(getattr(settings, "WEB_UI_HOST", "localhost") or "").lower()
        if host not in _LOCAL_HOSTS:
            return web.json_response(
                {"ok": False, "error": "forbidden_non_local_host",
                 "detail": "copy-trade data is localhost-only"}, status=403)
        return None

    async def handle_copytrade_actor(self, request) -> web.Response:
        """GET /api/copytrade/actor/{actor_id} → point-in-time survival/latency
        skill + evidence + as-of reconstruction. The as-of reconstruction calls
        the SAME shared point-in-time function as the scorer (one code path) —
        sample size + δ shown as prominently as the score."""
        guard = self._copytrade_guard()
        if guard is not None:
            return guard
        from follow import copytrade_scorer, skill_scorer
        actor_id = request.match_info.get("actor_id", "")
        as_of = None
        raw = request.query.get("as_of")
        if raw:
            try:
                as_of = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                as_of = None
        try:
            score = copytrade_scorer.score_actor(actor_id, as_of)
            as_of_dt = as_of or datetime.utcnow()
            # SAME shared as-of query the scorer uses — not a second copy.
            evidence = skill_scorer.resolved_actions_as_of(actor_id, as_of_dt)
            return web.json_response(
                {"ok": True, "actor": actor_id, "score": score,
                 "evidence": evidence[-50:], "as_of": as_of_dt.isoformat()},
                dumps=lambda o: json.dumps(_jsonable(o), default=str,
                                           allow_nan=False))
        except Exception as e:
            logger.debug("copytrade actor endpoint failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_copytrade_surfaced(self, request) -> web.Response:
        """GET /api/copytrade/surfaced → currently surfaced (skilled) actors +
        latest position changes. "Skilled but unfollowable" actors are present,
        flagged as such (followable=False) — surfacing them honestly is the
        point, not hiding them."""
        guard = self._copytrade_guard()
        if guard is not None:
            return guard
        try:
            actors = db_queries.get_skilled_copytrade_actors()
            events = db_queries.get_recent_copytrade_events(limit=25)
            return web.json_response(
                {"ok": True, "actors": actors, "events": events},
                dumps=lambda o: json.dumps(_jsonable(o), default=str,
                                           allow_nan=False))
        except Exception as e:
            logger.debug("copytrade surfaced endpoint failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_copytrade_denominator(self, request) -> web.Response:
        """GET /api/copytrade/denominator → THE DENOMINATOR VIEW: evaluated N,
        surfaced M, and WHY the N−M were rejected. A population view, not a
        highlight reel — so skill is never read off a survivor-only pool."""
        guard = self._copytrade_guard()
        if guard is not None:
            return guard
        try:
            den = db_queries.get_copytrade_denominator(limit=500)
            return web.json_response(
                {"ok": True, "denominator": den},
                dumps=lambda o: json.dumps(_jsonable(o), default=str,
                                           allow_nan=False))
        except Exception as e:
            logger.debug("copytrade denominator endpoint failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_copytrade_health(self, request) -> web.Response:
        """GET /api/copytrade/health → venue coverage (implemented vs stubbed),
        the denominator headline, and the honest-scope caveat."""
        guard = self._copytrade_guard()
        if guard is not None:
            return guard
        out = {"ok": True, "venues": [], "venues_stubbed": [], "denominator": {},
               "scope_note": ("latency-limited: surfaces SKILL (corroboration), "
                              "rarely followable entries")}
        try:
            agent = self._get_agent("follow")
            snap = (agent.get_copytrade_snapshot()
                    if agent is not None and hasattr(agent, "get_copytrade_snapshot")
                    else {})
            srcs = snap.get("sources", []) or []
            if srcs:
                out["venues"] = srcs[0].get("venues", [])
                out["venues_stubbed"] = srcs[0].get("venues_stubbed", [])
            out["denominator"] = snap.get("denominator", {})
            out["running"] = snap.get("running", False)
        except Exception as e:
            logger.debug("copytrade health failed: %s", e)
        return web.json_response(out)

    async def handle_copytrade_viz_skill(self, request) -> web.Response:
        """GET /api/copytrade/viz/skill → ON-DEMAND chart data for the luck-vs-
        skill scatter and THE DENOMINATOR view: surfaced actors as points
        (skill vs sample-size vs latency-δ, followable flagged) over the FULL
        evaluated population (denominator: evaluated N / surfaced M / rejection
        reasons). Population view, never a winners-only reel. Pure read-side
        aggregation — adds NOTHING to the snapshot. Localhost-guarded.
        Empty-safe: empty points + zeroed denominator when nothing's stored."""
        guard = self._copytrade_guard()
        if guard is not None:
            return guard
        out = {"ok": True, "points": [],
               "denominator": {"evaluated": 0, "surfaced": 0, "rejected": 0,
                               "rejection_reasons": {}}}
        try:
            actors = db_queries.get_skilled_copytrade_actors()
            out["points"] = [{
                "actor_id":   a.get("actor_id"),
                "venue":      a.get("venue"),
                "skill":      a.get("skill_score"),
                "sample":     a.get("closed_trades", 0),
                "delta_s":    a.get("latency_delta_s"),
                "drawdown":   a.get("drawdown"),
                "followable": bool(a.get("followable")),
            } for a in actors]
        except Exception as e:
            logger.debug("copytrade viz points failed: %s", e)
        try:
            den = db_queries.get_copytrade_denominator(limit=500)
            out["denominator"] = {
                "evaluated":         den.get("evaluated", 0),
                "surfaced":          den.get("surfaced", 0),
                "rejected":          den.get("rejected", 0),
                "rejection_reasons": den.get("rejection_reasons", {}),
            }
        except Exception as e:
            logger.debug("copytrade viz denominator failed: %s", e)
        return web.json_response(
            out, dumps=lambda o: json.dumps(_jsonable(o), default=str,
                                            allow_nan=False))

    # ── Meme-coin rug-rate scorer (/api/meme/*) ──────────────────────────
    # PRIVACY HARD RULE: this panel serves wallet/funder identities. Every
    # endpoint 403s unless the server binds to a loopback host — identical to
    # the wallet-flow rule (identities must never leave the operator's machine).

    def _meme_guard(self) -> Optional[web.Response]:
        """Return a 403 response when WEB_UI_HOST is not loopback, else None."""
        host = str(getattr(settings, "WEB_UI_HOST", "localhost") or "").lower()
        if host not in _LOCAL_HOSTS:
            return web.json_response(
                {"ok": False, "error": "forbidden_non_local_host",
                 "detail": "meme rug-rate data is localhost-only"}, status=403)
        return None

    async def handle_meme_launch(self, request) -> web.Response:
        """GET /api/meme/launch/{mint} → per-launch drill-in: the decision, the
        funder-cluster, and each present funder's rug-rate WITH resolved-vs-censored
        counts and the hard/soft mix shown AS PROMINENTLY as the rate. Routes the
        rate through the SAME shared meme_scorer.rug_rate_as_of as the scorer."""
        guard = self._meme_guard()
        if guard is not None:
            return guard
        from follow import meme_scorer
        mint = request.match_info.get("mint", "")
        try:
            launch = db_queries.get_meme_launch(mint)
            decision = db_queries.get_meme_decision(mint)
            buyers = db_queries.get_meme_launch_buyers(mint)
            as_of_dt = datetime.utcnow()
            # Decision time is what the scorer used; show rates as-of THAT moment.
            if decision and decision.get("decided_at"):
                try:
                    as_of_dt = datetime.fromisoformat(decision["decided_at"])
                except (TypeError, ValueError):
                    pass
            funders = sorted({b["funder"] for b in buyers if b.get("funder")})
            # SAME shared as-of fn the scorer + replay use — not a second copy.
            cluster = [meme_scorer.rug_rate_as_of(f, as_of_dt) for f in funders]
            return web.json_response(
                {"ok": True, "launch": launch, "decision": decision,
                 "buyers": buyers, "cluster": cluster,
                 "no_signal_label": meme_scorer.NO_SIGNAL_LABEL},
                dumps=lambda o: json.dumps(_jsonable(o), default=str,
                                           allow_nan=False))
        except Exception as e:
            logger.debug("meme launch endpoint failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_meme_asof(self, request) -> web.Response:
        """GET /api/meme/asof/{funder}?as_of=ISO → THE AS-OF INSPECTOR. Reconstructs
        a funder-cluster's rug-rate at any timestamp through the SAME shared
        meme_scorer.rug_rate_as_of the live scorer and replay harness use (one code
        path). Resolved-vs-censored counts + hard/soft mix are returned alongside
        the rate — hiding uncertainty is the failure mode."""
        guard = self._meme_guard()
        if guard is not None:
            return guard
        from follow import meme_scorer
        funder = request.match_info.get("funder", "")
        as_of = None
        raw = request.query.get("as_of")
        if raw:
            try:
                as_of = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                as_of = None
        try:
            as_of_dt = as_of or datetime.utcnow()
            rr = meme_scorer.rug_rate_as_of(funder, as_of_dt)
            return web.json_response(
                {"ok": True, "funder": funder, "as_of": as_of_dt.isoformat(),
                 "rug_rate": rr},
                dumps=lambda o: json.dumps(_jsonable(o), default=str,
                                           allow_nan=False))
        except Exception as e:
            logger.debug("meme as-of endpoint failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_meme_health(self, request) -> web.Response:
        """GET /api/meme/health → the HEALTH LAYER: sampling best-effort/gap flag,
        coverage, and the latest replay TP/FP/coverage. Surfaces the known bias
        (sampling under-catches fast rugs; v1 misses sophisticated operators)
        rather than implying completeness."""
        guard = self._meme_guard()
        if guard is not None:
            return guard
        from follow import meme_scorer
        out = {"ok": True, "sources": [], "replay": {},
               "no_signal_label": meme_scorer.NO_SIGNAL_LABEL,
               "scope_note": ("LOW-HANGING FRUIT only; sampling MISSES fast rugs; "
                              "misses sophisticated operators by design. "
                              "NO-SIGNAL != safe.")}
        try:
            agent = self._get_agent("follow")
            snap = (agent.get_meme_snapshot()
                    if agent is not None and hasattr(agent, "get_meme_snapshot")
                    else {})
            out["sources"] = snap.get("sources", [])
            out["running"] = snap.get("running", False)
        except Exception as e:
            logger.debug("meme health snapshot: %s", e)
        try:
            out["replay"] = meme_scorer.run_replay()
        except Exception as e:
            logger.debug("meme health replay: %s", e)
        return web.json_response(out)

    # ── LED ticker (GET /api/ticker?exchange=<id>) ───────────────────────

    async def handle_ticker(self, request) -> web.Response:
        """Prices for the LED grid — a PURE CACHE READ. The _TickerWorker
        thread refreshes every venue on its own loop and writes payloads
        into _ticker_cache; this handler never touches the network, so it
        answers instantly regardless of venue health. NEVER raises: an
        unknown venue falls back to the default, a venue the worker hasn't
        produced yet returns {ok: false, error: "warming up"}."""
        try:
            venues = list(getattr(settings, "TICKER_EXCHANGES", []) or [])
            venues = [v for v in venues if v in _TICKER_REGISTRY]
            if not venues:
                return web.json_response({"ok": False, "error": "no ticker venues configured"})
            default = getattr(settings, "TICKER_DEFAULT_EXCHANGE", venues[0])
            if default not in venues:
                default = venues[0]
            exchange_id = request.query.get("exchange", "")
            if exchange_id not in venues:
                exchange_id = default

            cached = self._ticker_cache.get(exchange_id)
            if cached is None:
                return web.json_response({
                    "ok": False, "exchange": exchange_id,
                    "label": _TICKER_REGISTRY[exchange_id]["label"],
                    "error": "warming up", "rows": [],
                })
            return web.json_response(cached[1], dumps=lambda o: json.dumps(
                _jsonable(o), default=str, allow_nan=False))
        except Exception as e:
            logger.debug(f"ticker endpoint failed: {e}")
            return web.json_response({"ok": False, "error": str(e)[:200]})

    @staticmethod
    def _logo_response(hit) -> web.Response:
        if hit is None:
            return web.Response(status=404)
        body, ctype = hit
        return web.Response(body=body, content_type=ctype,
                            headers={"Cache-Control": "max-age=86400"})

    async def _logo_http_get(self, url: str) -> tuple:
        """(status, body, content_type) via a shared session with a browser
        UA (cryptocompare's media host bot-blocks default agents). Raises on
        transport errors — callers treat those as transient."""
        import aiohttp
        if self._logo_session is None or self._logo_session.closed:
            self._logo_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                                        "AppleWebKit/537.36")})
        async with self._logo_session.get(url) as resp:
            body = await resp.read() if resp.status == 200 else b""
            return resp.status, body, resp.headers.get("Content-Type", "")

    async def _cc_image_path(self, sym: str) -> Optional[str]:
        """CryptoCompare ImageUrl path for a symbol, from its coinlist
        (~20k symbols → excellent modern-coin coverage), lazily loaded once
        and parsed OFF-loop. None when unknown; the map stays unset on a
        failed load so it's retried later rather than poisoning everything."""
        if self._cc_imgmap is not None:
            return self._cc_imgmap.get(sym) or None
        if self._cc_imgmap_lock is None:
            self._cc_imgmap_lock = asyncio.Lock()
        async with self._cc_imgmap_lock:
            if self._cc_imgmap is not None:
                return self._cc_imgmap.get(sym) or None
            import os
            url = "https://min-api.cryptocompare.com/data/all/coinlist"
            key = os.getenv("CRYPTOCOMPARE_API_KEY", "")
            if key:
                url += f"?api_key={key}"
            status, raw, _ = await self._logo_http_get(url)
            if status != 200 or not raw:
                return None

            def _parse(blob: bytes) -> dict:
                data = json.loads(blob).get("Data") or {}
                return {k.lower(): (v.get("ImageUrl") or "")
                        for k, v in data.items() if isinstance(v, dict)}

            # Multi-MB JSON — parse in a thread so the loop never stalls.
            self._cc_imgmap = await asyncio.to_thread(_parse, raw)
            logger.info(f"coinlogo: cryptocompare image map loaded "
                        f"({len(self._cc_imgmap)} symbols)")
        return self._cc_imgmap.get(sym) or None

    async def handle_coinlogo(self, request) -> web.Response:
        """Coin-logo proxy for the LED grid — the browser only ever talks to
        the bot. Source chain per coin, cached in memory after the first hit:
          1. cryptocurrency-icons SVG (tiny, pretty; old set — majors only)
          2. CryptoCompare media PNG (modern coverage via its coinlist map)
        Only a DEFINITIVE miss from every source caches as None (fast 404 →
        the grid's letter avatar). Throttle responses (403/429) and transport
        hiccups are never cached — they retry on a later request — and a
        small semaphore stops a page load's ~80-logo burst from triggering
        upstream throttling in the first place. Never raises."""
        try:
            sym = (request.match_info.get("coin") or "").strip().lower()
            if not sym.isalnum() or len(sym) > 12:
                return web.Response(status=404)
            if sym in self._logo_cache:
                return self._logo_response(self._logo_cache[sym])
            if self._logo_sem is None:
                self._logo_sem = asyncio.Semaphore(4)
            async with self._logo_sem:
                if sym in self._logo_cache:      # raced: filled while we waited
                    return self._logo_response(self._logo_cache[sym])
                definitive = 0
                # 1. cryptocurrency-icons SVG
                try:
                    status, body, _ = await self._logo_http_get(
                        "https://cdn.jsdelivr.net/npm/cryptocurrency-icons"
                        f"@0.18.1/svg/color/{sym}.svg")
                    if status == 200 and body:
                        self._logo_cache[sym] = (body, "image/svg+xml")
                        return self._logo_response(self._logo_cache[sym])
                    if status == 404:
                        definitive += 1
                except Exception as e:
                    logger.debug(f"coinlogo icons {sym}: {e}")
                # 2. CryptoCompare media
                try:
                    path = await self._cc_image_path(sym)
                    if path:
                        status, body, ctype = await self._logo_http_get(
                            "https://www.cryptocompare.com" + path)
                        if status == 200 and body:
                            self._logo_cache[sym] = (body, ctype or "image/png")
                            return self._logo_response(self._logo_cache[sym])
                        if status == 404:
                            definitive += 1
                    elif self._cc_imgmap is not None:
                        definitive += 1          # map loaded, symbol absent
                except Exception as e:
                    logger.debug(f"coinlogo cc {sym}: {e}")
                if definitive >= 2:
                    self._logo_cache[sym] = None
                return web.Response(status=404)
        except Exception as e:
            logger.debug(f"coinlogo endpoint: {e}")
            return web.Response(status=404)

    @staticmethod
    def _ticker_rows_from_bulk(tickers: dict, quotes: tuple,
                               cap: Optional[int] = None) -> list:
        """Filter a fetch_tickers payload to dollar-quoted pairs, keep the
        highest-volume listing per base, sort by 24h quote volume. cap=None
        returns ALL usable pairs (the grid paginates client-side).

        Each row carries `vol` (24h quote volume, ~USD since the quote is a
        dollar stable) — surfaced as the grid's volume metric. pct is ccxt's
        24h `percentage` (computed from `open` when absent; None when
        neither exists — the UI renders those flat/dim). Rows with no
        usable last/close price are skipped — the bulk analogue of the
        per-coin error isolation."""
        best: dict = {}
        for symbol, t in (tickers or {}).items():
            try:
                base, _, rest = symbol.partition("/")
                quote = rest.split(":", 1)[0]
                if not base or quote not in quotes:
                    continue
                price = t.get("last") or t.get("close")
                if not price:
                    continue
                price = float(price)
                pct = t.get("percentage")
                if pct is None:
                    op = t.get("open")
                    if op:
                        pct = (price - float(op)) / float(op) * 100.0
                vol = t.get("quoteVolume")
                if vol is None:
                    bv = t.get("baseVolume")
                    vol = float(bv) * price if bv else 0.0
                vol = float(vol or 0.0)
                cur = best.get(base)
                if cur is None or vol > cur["vol"]:
                    best[base] = {
                        "coin":   base,
                        "symbol": symbol,
                        "price":  price,
                        "pct":    None if pct is None else round(float(pct), 2),
                        "vol":    round(vol, 2),
                    }
            except Exception:
                continue
        rows = sorted(best.values(), key=lambda r: -r["vol"])
        return rows if cap is None else rows[:cap]

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
            # Web UI v2 — dedicated agent panels read from these top-level
            # keys. The existing arb_feed / arb_history above stay for
            # backwards-compatible consumers (arb history tab); the new
            # `arb` dict is the v2 panel surface.
            "arb":            self._snap_arb_v2(),
            "xchain":         self._snap_xchain(),
            "funding":        self._snap_funding(),
            "balance":        self._snap_balance(),
            "opportunities":  self._snap_opportunities(),
            # Wallet + exchange-flow watcher (read-only; $0 observer). Live,
            # low-volume only — heavy/historical reads are on-demand REST.
            "walletflow":     self._snap_walletflow(),
            # Copy-trade leaderboard observer (read-only; $0 OBSERVER). Live,
            # low-volume only — per-actor as-of + full denominator are REST.
            "copytrade":      self._snap_copytrade(),
            "meme":           self._snap_meme(),
            # Web UI v2 fixes — capital deployment + movements visibility.
            "capital":        self._snap_capital(),
        }

    # ── Web UI v2 panel snapshots ───────────────────────────────────────

    def _get_agent(self, agent_id: str):
        """Coordinator.get_agent lookup that never raises."""
        coord = self._coordinator
        if coord is None:
            return None
        getter = getattr(coord, "get_agent", None)
        if not callable(getter):
            return None
        try:
            return getter(agent_id)
        except Exception:
            return None

    @staticmethod
    def _status_from(agent, *, observation_attr: str = "observation_mode") -> str:
        """Map a (possibly None) agent's state to the panel's
        RUNNING / SIM-TRADING / OBSERVATION / OFFLINE / ERROR taxonomy.

        SIM-TRADING = the agent is still observation-gated for live but is
        running a bounded sim-capital trial (e.g. funding Phase 2a) — shown
        distinctly so the operator can see the trial is actually trading."""
        if agent is None:
            return "OFFLINE"
        try:
            if bool(getattr(agent, "_sim_trading", False)):
                return "SIM-TRADING"
            if bool(getattr(agent, observation_attr, False)):
                return "OBSERVATION"
            running = bool(getattr(agent, "_running", False))
            return "RUNNING" if running else "OFFLINE"
        except Exception:
            return "ERROR"

    def _snap_arb_v2(self) -> dict:
        """Web UI v2 arb-fund panel. Engine-backed fields default to
        empty/zero per the hybrid scope — a later build can fill them by
        exposing per-exchange health + gap distribution on the engine."""
        out = {
            "exchanges": [],
            "gap_distribution": {
                "bucket_edges_bps": [0, 5, 10, 20, 50, 100, 250],
                "bucket_counts":    [0, 0, 0, 0, 0, 0, 0],
                "median_bps":       0.0,
                "p90_bps":          0.0,
            },
            "threshold": {
                "min_net_gap_bps": (
                    float(getattr(settings, "ARB_MIN_GAP_PCT", 0.003)) * 100.0
                ),
                "rationale": "default fallback — engine threshold not surfaced",
            },
            "semaphore": {
                "active":   0,
                "capacity": int(getattr(settings, "ARB_MAX_CONCURRENT", 3) or 3),
            },
        }
        try:
            agent = self._get_agent("arb")
            engine = getattr(agent, "_engine", None) if agent is not None else None
            if engine is not None:
                out["semaphore"]["active"] = int(
                    getattr(engine, "_active_arbs", 0) or 0
                )
        except Exception as e:
            logger.debug("arb_v2 semaphore read failed: %s", e)
        return out

    def _snap_xchain(self) -> dict:
        """Web UI v2 cross-chain panel.

        `chains` and `best_pair.would_entry` are not surfaced under the
        hybrid scope (requires PoolState timestamp/error + engine
        best-evaluation buffer). Real data appears for inventory_targets
        and today_summary, sourced from the agent + DB respectively.
        """
        out = {
            "status":            "OFFLINE",
            "capital_usd":       float(getattr(settings, "XCHAIN_CAPITAL", 0.0) or 0.0),
            "chains":            [],
            "best_pair": {
                "buy_chain":         None,
                "sell_chain":        None,
                "spread_bps":        0.0,
                "net_edge_bps":      0.0,
                "gas_breakeven_usd": 0.0,
                "would_entry":       False,
                "skip_reason":       "not_surfaced",
            },
            "inventory_targets": [],
            "today_summary": {
                "n_observations":          0,
                "n_would_entry":           0,
                "mean_net_edge_bps":       0.0,
                "median_net_edge_bps":     0.0,
                "pct_blocked_by_gas":      0.0,
                "pct_blocked_by_min_edge": 0.0,
            },
        }
        agent = self._get_agent("xchain")
        out["status"] = self._status_from(agent)
        try:
            if agent is not None:
                targets = agent.get_inventory_targets() or []
                out["inventory_targets"] = [
                    {
                        "fund":             getattr(t, "fund", "xchain"),
                        "chain":            getattr(t, "exchange", ""),
                        "asset":            getattr(t, "asset", "USDT"),
                        "current_usd":      float(
                            getattr(t, "current_usd", 0.0) or 0.0
                        ) if hasattr(t, "current_usd") else 0.0,
                        "target_usd":       float(getattr(t, "target_usd", 0.0) or 0.0),
                        "drift_pct":        float(getattr(t, "drift_pct", 0.0) or 0.0),
                        "needs_rebalance":  bool(getattr(t, "needs_rebalance", False)),
                    }
                    for t in targets
                ]
        except Exception as e:
            logger.debug("xchain inventory_targets failed: %s", e)
            out["status"] = "ERROR"
        try:
            out["today_summary"] = db_queries.get_xchain_today_summary()
        except Exception as e:
            logger.debug("xchain today_summary failed: %s", e)
        return out

    def _snap_funding(self) -> dict:
        """Web UI v2 funding-rate panel.

        `symbols` is empty under the hybrid scope — surfacing per-symbol
        live state requires a new engine method. `positions` reads the
        agent's in-memory book; today_summary aggregates the
        FundingArbObservationModel rows logged today.
        """
        out = {
            "status":         "OFFLINE",
            "capital_usd":    float(getattr(settings, "FUNDING_CAPITAL_USD", 0.0) or 0.0),
            "sim_capital_usd": 0.0,
            "margin_in_use_usd": 0.0,
            "venue":          "binance",
            "symbols":        [],
            "positions":      [],
            "today_summary": {
                "n_observations":           0,
                "n_would_enter":            0,
                "utilisation_pct":          0.0,
                "mean_projected_apr_pct":   0.0,
                "median_projected_apr_pct": 0.0,
                "blended_apr_pct":          0.0,
                "skip_reasons": {
                    "below_gate":         0,
                    "basis_unfavourable": 0,
                    "depth_thin":         0,
                    "other":              0,
                },
            },
        }
        agent = self._get_agent("funding_arb")
        out["status"] = self._status_from(agent)
        # Sim-capital trial (Phase 2a): the working capital the panel should
        # show is the sim budget — FUNDING_CAPITAL_USD stays 0 by design
        # (BalanceAgent ledger contract), so without this the panel reads $0
        # while the agent is actively sim-trading.
        try:
            if bool(getattr(agent, "_sim_trading", False)):
                sim_cap = float(getattr(settings, "FUNDING_SIM_CAPITAL_USD", 0.0) or 0.0)
                out["sim_capital_usd"] = sim_cap
                out["capital_usd"] = max(out["capital_usd"], sim_cap)
                out["margin_in_use_usd"] = round(sum(
                    float(getattr(p, "margin_used", 0.0) or 0.0)
                    for p in (getattr(agent, "_positions", {}) or {}).values()), 2)
        except Exception:
            pass
        try:
            if agent is not None:
                positions = getattr(agent, "_positions", None) or {}
                for sym, pos in positions.items():
                    try:
                        opened = float(getattr(pos, "opened_at", 0.0) or 0.0)
                        entry_ts = (
                            datetime.utcfromtimestamp(opened).strftime("%H:%M:%S")
                            if opened else "—"
                        )
                        notional = float(getattr(pos, "notional_usd", 0.0) or 0.0)
                        out["positions"].append({
                            "symbol":   sym,
                            "side":     "SHORT_PERP_LONG_SPOT",
                            "spot_qty": notional,
                            "perp_qty": notional,
                            "delta_usd": 0.0,                       # not tracked Phase 1
                            "entry_ts":  entry_ts,
                            "funding_collected_usd": float(
                                getattr(pos, "funding_collected", 0.0) or 0.0
                            ),
                            "realised_apr_pct": 0.0,                # not tracked Phase 1
                            "next_exit_check": "—",
                        })
                    except Exception:
                        continue
        except Exception as e:
            logger.debug("funding positions read failed: %s", e)
            out["status"] = "ERROR"
        try:
            out["today_summary"] = db_queries.get_funding_today_summary()
        except Exception as e:
            logger.debug("funding today_summary failed: %s", e)
        return out

    @staticmethod
    def _opportunity_empty_block() -> dict:
        """The complete safe-fallback shape — every sub-key present even
        when a read raises or the tables are empty (snapshot iron rule)."""
        return {
            "enabled": bool(getattr(settings, "OPPORTUNITY_SCANNER_ENABLED",
                                    False)),
            "observation_count": 0,
            "detection_latency_ms_p50": None,
            "standard": [],
            "exploratory": [],
            "notes": [],
        }

    @staticmethod
    def _opportunity_row(row: dict, exploratory: bool) -> dict:
        """One panel row. competitor_trend ALWAYS rides beside the edge
        fields — edge is never relayed without trajectory adjacent."""
        first_seen = row.get("first_seen") or ""
        try:
            ts = datetime.fromisoformat(first_seen).strftime("%m-%d %H:%M")
        except (TypeError, ValueError):
            ts = "—"
        out = {
            "opp_type":            row.get("opp_type"),
            "protocol":            row.get("protocol"),
            "chain":               row.get("chain"),
            "market_key":          row.get("market_key"),
            "first_seen":          ts,
            "competitor_trend":    row.get("competitor_trend"),
            "competitor_count":    row.get("competitor_count"),
            "edge_annualized_pct": row.get("edge_annualized_pct"),
            "edge_confidence":     row.get("edge_confidence"),
            "reachability":        row.get("reachability_verdict"),
            "window_status":       row.get("window_status"),
        }
        if exploratory:
            out.update(
                unconventional_score=row.get("unconventional_score"),
                unconventional_factors=row.get("unconventional_factors") or [],
                unconventional_rationale=row.get("unconventional_rationale"),
            )
        return out

    def _snap_opportunities(self) -> dict:
        """Opportunity Scanner panel block (READ-ONLY — monitoring, not
        control; the agent is $0 observation-mode and so is its UI).

        `standard` is the trajectory-ranked actionable view; `exploratory`
        is the FEATURE BLOCK 6b lane sorted by unconventional_score with
        the free-text rationale the operator reads inline. Same survivor
        set, separate lists — they NEVER merge. Disqualified rows appear
        in neither while OPPORTUNITY_SHOW_DISQUALIFIED=False; the 6c
        counterfactual self-audit is deliberately NOT surfaced here (DB-
        only, for later analysis — a future toggle would slot in via
        show_disqualified).

        `notes` carries the noteworthy reasoning posts only — the full
        feed is a pull via GET /api/opportunity_notes, not a 2Hz push.
        """
        out = self._opportunity_empty_block()
        if not out["enabled"]:
            return out
        max_rows = int(getattr(settings, "OPPORTUNITY_PANEL_MAX_ROWS", 12))
        show_dq = bool(getattr(settings, "OPPORTUNITY_SHOW_DISQUALIFIED", False))
        try:
            std = db_queries.get_ranked_opportunities(
                mode="standard", show_disqualified=show_dq)
            out["standard"] = [self._opportunity_row(r, False)
                               for r in std[:max_rows]]
        except Exception as e:
            logger.debug("opportunities standard view failed: %s", e)
        try:
            exp = db_queries.get_ranked_opportunities(
                mode="exploratory", show_disqualified=show_dq)
            # The helper already applies the lane's noise floor; re-filter
            # defensively so the panel can never regress below it.
            floor = float(getattr(settings,
                                  "OPPORTUNITY_UNCONVENTIONAL_MIN_SCORE", 0.3))
            exp = [r for r in exp
                   if float(r.get("unconventional_score") or 0.0) >= floor]
            out["exploratory"] = [self._opportunity_row(r, True)
                                  for r in exp[:max_rows]]
        except Exception as e:
            logger.debug("opportunities exploratory view failed: %s", e)
        try:
            summary = db_queries.get_opportunity_summary()
            out["observation_count"] = int(summary.get("n_observations", 0))
            out["detection_latency_ms_p50"] = summary.get(
                "detection_latency_ms_p50")
        except Exception as e:
            logger.debug("opportunities summary failed: %s", e)
        try:
            out["notes"] = db_queries.get_opportunity_notes(
                noteworthy_only=True, limit=max_rows)
        except Exception as e:
            logger.debug("opportunities notes failed: %s", e)
        return out

    @staticmethod
    def _walletflow_empty_block() -> dict:
        """Complete safe-fallback shape — every sub-key present even when the
        agent is None or a read raises (snapshot iron rule)."""
        return {
            "enabled":            bool(getattr(settings, "WALLETFLOW_ENABLED", False)),
            "running":            False,
            "flow_events":        [],
            "netflow_spikes":     [],
            "pending_candidates": 0,
            "label_staleness":    {"is_stale": True, "count": 0},
            "sources":            [],
        }

    def _snap_walletflow(self) -> dict:
        """Wallet + exchange-flow watcher panel block (READ-ONLY live view; the
        agent is a $0 observer and so is its push surface). Heavy/wallet-
        identifying reads (skill score, candidate detail, as-of reconstruction)
        are on-demand REST under /api/walletflow/* and are NOT pushed here.

        Defensive: a None agent or any failing read falls back to the complete
        empty block — the snapshot must never raise."""
        agent = self._get_agent("follow")
        if agent is None:
            return self._walletflow_empty_block()
        getter = getattr(agent, "get_walletflow_snapshot", None)
        if not callable(getter):
            return self._walletflow_empty_block()
        try:
            block = getter()
            # Belt-and-braces: guarantee every key the frontend reads exists.
            base = self._walletflow_empty_block()
            base.update(block or {})
            return base
        except Exception as e:
            logger.debug("walletflow snapshot failed: %s", e)
            return self._walletflow_empty_block()

    @staticmethod
    def _copytrade_empty_block() -> dict:
        """Complete safe-fallback shape — every sub-key present even when the
        agent is None or a read raises (snapshot iron rule)."""
        return {
            "enabled":         bool(getattr(settings, "COPYTRADE_ENABLED", False)),
            "running":         False,
            "events":          [],
            "surfaced_actors": [],
            "denominator":     {"evaluated": 0, "surfaced": 0, "rejected": 0,
                                "rejection_reasons": {}},
            "sources":         [],
            "scope_note":      ("latency-limited: surfaces SKILL (corroboration), "
                                "rarely followable entries"),
        }

    def _snap_copytrade(self) -> dict:
        """Copy-trade leaderboard observer panel block (READ-ONLY live view; the
        agent is a $0 observer). Heavy/identifying reads (per-actor as-of, the
        full denominator) are on-demand REST under /api/copytrade/* and are NOT
        pushed here. Defensive: a None agent or any failing read falls back to the
        complete empty block — the snapshot must never raise."""
        agent = self._get_agent("follow")
        if agent is None:
            return self._copytrade_empty_block()
        getter = getattr(agent, "get_copytrade_snapshot", None)
        if not callable(getter):
            return self._copytrade_empty_block()
        try:
            block = getter()
            base = self._copytrade_empty_block()
            base.update(block or {})
            return base
        except Exception as e:
            logger.debug("copytrade snapshot failed: %s", e)
            return self._copytrade_empty_block()

    @staticmethod
    def _meme_empty_block() -> dict:
        """Complete safe-fallback shape — every sub-key present even when the
        agent is None or a read raises (snapshot iron rule). NO-SIGNAL is carried
        labelled, NEVER as 'safe'."""
        return {
            "enabled":         bool(getattr(settings, "MEME_ENABLED", False)),
            "running":         False,
            "launches":        [],
            "decisions":       [],
            "sources":         [],
            "no_signal_label": "no lazy manipulation detected",
            "scope_note":      ("LOW-HANGING FRUIT only (single-hop funder bundles); "
                                "sampling MISSES fast rugs; misses sophisticated "
                                "operators by design. NO-SIGNAL != safe."),
        }

    def _snap_meme(self) -> dict:
        """Meme-coin rug-rate scorer panel block (READ-ONLY live view; the agent
        is a $0 observer). Heavy/identifying reads (per-launch cluster drill-in,
        the as-of inspector) are on-demand REST under /api/meme/* and are NOT
        pushed here. Defensive: a None agent or any failing read falls back to the
        complete empty block — the snapshot must never raise."""
        agent = self._get_agent("follow")
        if agent is None:
            return self._meme_empty_block()
        getter = getattr(agent, "get_meme_snapshot", None)
        if not callable(getter):
            return self._meme_empty_block()
        try:
            block = getter()
            base = self._meme_empty_block()
            base.update(block or {})
            return base
        except Exception as e:
            logger.debug("meme snapshot failed: %s", e)
            return self._meme_empty_block()

    def _snap_balance(self) -> dict:
        """Web UI v2 balance panel — wholesale replacement of the v1 shape.

        Reads the BalanceAgent's buffered targets + bands + pending plan
        (populated each scan), the inventory_state ledger view, and a
        small set of DB queries. Every read defensive — one source
        failing must not cascade to the whole block.
        """
        empty = {
            "status":        "OFFLINE",
            "kill_blocked":  False,
            "pool": {
                "equity_usd":   0.0,
                "reserve_usd":  0.0,
                "deployed_usd": 0.0,
                "pool_usd":     0.0,
            },
            "funds":         [],
            "nodes":         [],
            "in_transit":    [],
            "halted_pairs":  [],
            "today": {
                "transfers_count":     0,
                "transfers_fees_usd":  0.0,
                "transfers_remaining": int(
                    getattr(settings, "REBALANCE_DAILY_LIMIT", 3) or 3
                ),
            },
            "pending_plan": {
                "proposed_at":   None,
                "confirm_token": None,
                "transfers":     [],
            },
        }
        agent = self._get_agent("balance")
        if agent is None:
            return empty

        out = dict(empty)
        try:
            kill_blocked = bool(getattr(agent, "_paused", False))
            out["kill_blocked"] = kill_blocked
            if kill_blocked:
                out["status"] = "HALTED"
            elif bool(getattr(agent, "_running", False)):
                out["status"] = "RUNNING"
        except Exception:
            out["status"] = "ERROR"

        # Pool aggregates — equity from FUND_* + realised P&L; reserve from
        # COMPOUND_RESERVE_PCT; deployed from agent stats.
        try:
            starting = (
                float(getattr(settings, "FUND_SIGNAL_CAPITAL", 0.0) or 0.0)
                + float(getattr(settings, "FUND_ARB_CAPITAL", 0.0) or 0.0)
                + float(getattr(settings, "FUND_MEXC_SCALP_CAPITAL", 0.0) or 0.0)
            )
            realised = float(self._safe(
                db_queries.get_alltime_realised_pnl, 0.0) or 0.0)
            equity = starting + realised
            reserve_pct = float(getattr(settings, "COMPOUND_RESERVE_PCT", 0.0) or 0.0)
            reserve = equity * reserve_pct
            deployed = 0.0
            for a in (self._agents_cache or []):
                try:
                    deployed += float(getattr(a, "capital_deployed", 0.0) or 0.0)
                except Exception:
                    continue
            out["pool"] = {
                "equity_usd":   round(equity, 2),
                "reserve_usd":  round(reserve, 2),
                "deployed_usd": round(deployed, 2),
                "pool_usd":     round(max(0.0, equity - reserve - deployed), 2),
            }
        except Exception as e:
            logger.debug("balance pool aggregates failed: %s", e)

        # Per-fund table.
        try:
            eff_24h_idx = {
                r["fund"]: r for r in
                self._safe(lambda: db_queries.get_fund_efficiency_summary(24), [])
            }
            eff_7d_idx = {
                r["fund"]: r for r in
                self._safe(lambda: db_queries.get_fund_efficiency_summary(168), [])
            }
            cap_by_agent = {
                getattr(a, "agent_id", ""): float(getattr(a, "capital_allocated", 0.0) or 0.0)
                for a in (self._agents_cache or [])
            }
            dep_by_agent = {
                getattr(a, "agent_id", ""): float(getattr(a, "capital_deployed", 0.0) or 0.0)
                for a in (self._agents_cache or [])
            }
            # Sum target_usd from the buffered InventoryTargets by fund.
            target_by_fund: dict[str, float] = {}
            for (fund, _ex, _as), t in (
                getattr(agent, "_last_computed_targets", {}) or {}
            ).items():
                target_by_fund[fund] = (
                    target_by_fund.get(fund, 0.0) + float(getattr(t, "target_usd", 0.0) or 0.0)
                )

            # Map agent_id → fund_id. "scalp" agent stewards the "mexc_scalp" fund.
            fund_of = {"signal": "signal", "arb": "arb", "scalp": "mexc_scalp"}
            funds_rows = []
            for agent_id in ("signal", "arb", "scalp"):
                fund_id = fund_of[agent_id]
                allocation = cap_by_agent.get(agent_id, 0.0)
                target = float(target_by_fund.get(fund_id, 0.0))
                drift = (
                    (allocation - target) / target * 100.0
                    if target > 0 else 0.0
                )
                eff_24 = eff_24h_idx.get(fund_id, {})
                eff_7  = eff_7d_idx.get(fund_id, {})
                funds_rows.append({
                    "id":                    fund_id,
                    "allocation_usd":        round(allocation, 2),
                    "target_usd":            round(target, 2),
                    "deployed_usd":          round(dep_by_agent.get(agent_id, 0.0), 2),
                    "drift_pct":             round(drift, 2),
                    "return_24h_pct":        eff_24.get("return_on_deployed_pct"),
                    "return_7d_pct":         eff_7.get("return_on_deployed_pct"),
                    "starvation_events_24h": int(eff_24.get("starvation_events", 0)),
                })
            out["funds"] = funds_rows
        except Exception as e:
            logger.debug("balance funds table failed: %s", e)

        # Nodes — one per buffered target. Cold-start fallback uses the
        # inventory_state allocations dict so the panel still shows
        # something before the first scan.
        try:
            from agents.balance.inventory_state import inventory_state
            nodes_rows = []
            targets = getattr(agent, "_last_computed_targets", {}) or {}
            bands   = getattr(agent, "_last_computed_bands",   {}) or {}
            if targets:
                for (fund, exchange, asset), tgt in targets.items():
                    try:
                        eff = float(
                            inventory_state.effective_balance(fund, exchange, asset)
                        )
                        physical = float(
                            getattr(settings, "EXCHANGE_BALANCES", {}).get(exchange, 0.0)
                            or 0.0
                        )
                        floor_v = float(getattr(tgt, "floor_usd", 0.0) or 0.0)
                        cap_v   = float(getattr(tgt, "cap_usd",   0.0) or 0.0)
                        band = bands.get((fund, exchange, asset)) or (0.0, 0.0)
                        lower, upper = float(band[0]), float(band[1])
                        in_band = True
                        if upper > lower > 0:
                            in_band = (lower <= eff <= upper)
                        nodes_rows.append({
                            "fund":             fund,
                            "exchange":         exchange,
                            "asset":            asset,
                            "physical_usd":     round(physical, 2),
                            "effective_usd":    round(eff, 2),
                            "floor_usd":        round(floor_v, 2),
                            "cap_usd":          round(cap_v, 2),
                            "band_lower_usd":   round(lower, 2),
                            "band_upper_usd":   round(upper, 2),
                            "in_band":          in_band,
                        })
                    except Exception:
                        continue
            else:
                # Cold-start: synthesise rows from inventory_state allocations
                # so the panel always has SOMETHING to render.
                snap = inventory_state.get_snapshot() or {}
                allocations = snap.get("allocations", {}) or {}
                for key, amount in allocations.items():
                    # keys look like "fund:exchange"
                    if ":" in key:
                        fund, exchange = key.split(":", 1)
                    else:
                        fund, exchange = key, ""
                    try:
                        eff = float(inventory_state.effective_balance(
                            fund, exchange, "USDT",
                        ))
                    except Exception:
                        eff = 0.0
                    nodes_rows.append({
                        "fund":             fund,
                        "exchange":         exchange,
                        "asset":            "USDT",
                        "physical_usd":     0.0,
                        "effective_usd":    round(eff, 2),
                        "floor_usd":        0.0,
                        "cap_usd":          0.0,
                        "band_lower_usd":   0.0,
                        "band_upper_usd":   0.0,
                        "in_band":          True,
                    })
            out["nodes"] = nodes_rows
        except Exception as e:
            logger.debug("balance nodes table failed: %s", e)

        # In-transit transfers from the DB.
        try:
            out["in_transit"] = (
                self._safe(db_queries.get_capital_movements_in_transit, []) or []
            )
        except Exception as e:
            logger.debug("balance in_transit failed: %s", e)

        # Today counters from the agent's own tallies.
        try:
            count = int(getattr(agent, "_transfers_today", 0) or 0)
            fees  = float(getattr(agent, "_fees_today_usd", 0.0) or 0.0)
            limit = int(getattr(settings, "REBALANCE_DAILY_LIMIT", 3) or 3)
            used  = int(getattr(agent, "_daily_rebalances", 0) or 0)
            out["today"] = {
                "transfers_count":     count,
                "transfers_fees_usd":  round(fees, 2),
                "transfers_remaining": max(0, limit - used),
            }
        except Exception as e:
            logger.debug("balance today counters failed: %s", e)

        # Pending plan + confirm token.
        try:
            getter = getattr(agent, "get_pending_proposal", None)
            if callable(getter):
                proposal = getter() or {}
                token = getattr(agent, "_arm_token", None)
                expires = float(getattr(agent, "_arm_expires_at", 0.0) or 0.0)
                # Snapshot consumer only sees the token while it's actually
                # valid — past expiry it's already as good as gone.
                surfaced_token = token if (token and time.time() <= expires) else None
                out["pending_plan"] = {
                    "proposed_at":   proposal.get("proposed_at"),
                    "confirm_token": surfaced_token,
                    "transfers":     proposal.get("transfers", []) or [],
                }
        except Exception as e:
            logger.debug("balance pending_plan failed: %s", e)

        return out

    # Agent_id → fund_id for fund_capital_efficiency lookups. The scalp
    # agent stewards the "mexc_scalp" fund (same mapping as _snap_balance).
    _FUND_OF_AGENT = {"signal": "signal", "arb": "arb", "scalp": "mexc_scalp"}

    def _snap_capital(self) -> dict:
        """Web UI v2 fixes item 1 — where capital is deployed + the history
        of moves. Snapshot iron rule: every key always present, every read
        wrapped, safe fallback on failure — never raises.

        per_agent reads the LIVE get_capital_allocation() /
        get_open_position_notional() per registered agent (not the cached
        stats row) so the panel tracks BalanceAgent rebalances as they land.
        """
        out = {
            "total_equity":     0.0,
            "total_deployed":   0.0,
            "total_idle":       0.0,
            "per_agent":        [],
            "in_transit":       [],
            "recent_movements": [],
        }
        try:
            out["total_equity"] = round(float(
                (self._portfolio_cache or {}).get("total_equity", 0.0) or 0.0), 2)
        except Exception as e:
            logger.debug("capital total_equity read failed: %s", e)

        # Latest return-on-deployed% per fund (None until a row exists).
        eff_by_fund: dict = {}
        try:
            for r in (db_queries.get_fund_efficiency_summary(24) or []):
                eff_by_fund[r.get("fund")] = r.get("return_on_deployed_pct")
        except Exception as e:
            logger.debug("capital fund efficiency read failed: %s", e)

        total_deployed = 0.0
        for a in (self._agents_cache or []):
            try:
                agent_id = getattr(a, "agent_id", "?")
                live = self._get_agent(agent_id)
                # Live allocation; cached stats row is the fallback.
                try:
                    allocation = float(live.get_capital_allocation()) \
                        if live is not None else \
                        float(getattr(a, "capital_allocated", 0.0) or 0.0)
                except Exception:
                    allocation = float(getattr(a, "capital_allocated", 0.0) or 0.0)
                try:
                    deployed = float(live.get_open_position_notional()) \
                        if live is not None else \
                        float(getattr(a, "capital_deployed", 0.0) or 0.0)
                except Exception:
                    deployed = float(getattr(a, "capital_deployed", 0.0) or 0.0)
                fund = self._FUND_OF_AGENT.get(agent_id, agent_id)
                rod = eff_by_fund.get(fund)
                out["per_agent"].append({
                    "id":                      agent_id,
                    "allocation":              round(allocation, 2),
                    "deployed":                round(deployed, 2),
                    "idle":                    round(allocation - deployed, 2),
                    "return_on_deployed_pct":  (None if rod is None
                                                else round(float(rod), 2)),
                })
                total_deployed += deployed
            except Exception:
                continue
        out["total_deployed"] = round(total_deployed, 2)
        out["total_idle"]     = round(out["total_equity"] - total_deployed, 2)

        try:
            out["in_transit"] = [
                {
                    "from_fund":     m.get("from_fund"),
                    "to_fund":       m.get("to_fund"),
                    "from_exchange": m.get("from_exchange"),
                    "to_exchange":   m.get("to_exchange"),
                    "amount_usd":    m.get("amount_usd", 0.0),
                    "state":         m.get("state", "in_transit"),
                }
                for m in (db_queries.get_capital_movements_in_transit() or [])
            ]
        except Exception as e:
            logger.debug("capital in_transit read failed: %s", e)

        try:
            n = int(getattr(settings, "WEB_UI_CAPITAL_MOVEMENTS_N", 15) or 15)
            out["recent_movements"] = \
                db_queries.get_capital_movements_recent(n) or []
        except Exception as e:
            logger.debug("capital recent_movements read failed: %s", e)
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
            "fee_viability": self._snap_scalp_fee_viability(),
            # Which fee leg gate 4 prices the round trip on — the taker-to-maker
            # pivot. "maker" → round trip uses the 0% maker fee, so MEXC stays
            # viable; "taker" would stand down at the real 5 bps.
            "fee_basis":     "maker" if getattr(
                settings, "SCALP_USE_MAKER_EXECUTION", False) else "taker",
        }

    def _snap_scalp_fee_viability(self) -> dict:
        """Per-venue maker-aware fee viability for the scalp page — the same
        `fee_viability` block the entry gate (gate 4 / ScalpingAgent
        ._fee_viability) and the Rich dashboard use, via the agent's
        get_observation_summary(). {} when the scalp agent isn't reachable."""
        coord = self._coordinator
        agent = None
        if coord is not None:
            getter = getattr(coord, "get_agent", None)
            if callable(getter):
                try:
                    agent = getter("scalp")
                except Exception:
                    agent = None
        if agent is None:
            return {}
        try:
            summary = agent.get_observation_summary() or {}
            return summary.get("fee_viability", {}) or {}
        except Exception:
            return {}

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
        coord = self._coordinator
        halted_check = getattr(coord, "is_agent_halted", None) if coord else None
        for a in (self._agents_cache or []):
            try:
                agent_id = getattr(a, "agent_id", "?")
                manually_halted = False
                # Web UI v3.1 — orthogonal to `status` (the engine-state
                # taxonomy). UI uses this to swap the per-agent Halt/Resume
                # button label and show the HALTED badge. Defaults False
                # when no coordinator is wired (tests with bot-only setups).
                if callable(halted_check):
                    try:
                        manually_halted = bool(halted_check(agent_id))
                    except Exception:
                        manually_halted = False
                # Web UI v2 fixes item 2 — the card shows the LIVE deployable
                # allocation (BalanceAgent rebalances land between stat
                # refreshes), not the cached stats row. Cached value stays
                # the fallback when the live agent isn't reachable.
                capital = round(float(getattr(a, "capital_allocated", 0.0) or 0.0), 2)
                live = self._get_agent(agent_id)
                if live is not None:
                    try:
                        capital = round(float(live.get_capital_allocation()), 2)
                    except Exception:
                        pass
                status = getattr(a, "status", "OFFLINE")
                # Funding sim-capital trial: the allocation attr stays 0 by
                # design (BalanceAgent ledger contract) — show the sim budget
                # on the card so the trial is visible. Display-only; the
                # coordinator's portfolio totals are untouched.
                if agent_id == "funding_arb":
                    if bool(getattr(live, "_sim_trading", False)):
                        capital = max(capital, round(float(getattr(
                            settings, "FUNDING_SIM_CAPITAL_USD", 0.0) or 0.0), 2))
                        status = "SIM-TRADING" if status == "RUNNING" else status
                out.append({
                    "id":              agent_id,
                    "status":          status,
                    "capital":         capital,
                    "daily_pnl":       round(float(getattr(a, "daily_pnl", 0.0) or 0.0), 2),
                    "trades_today":    int(getattr(a, "trades_today", 0) or 0),
                    "win_rate":        round(float(getattr(a, "win_rate_today", 0.0) or 0.0) * 100, 1),
                    "manually_halted": manually_halted,
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

    @staticmethod
    def _fmt_exit_px(v) -> str:
        """Compact price for the exit_target string: thousands get commas,
        small prices keep 4 significant decimals."""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return "—"
        if abs(f) >= 1000:
            return f"{f:,.0f}"
        return (f"{f:.4f}".rstrip("0").rstrip(".")) or "0"

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
                # Deploying agent (item 5). Trade.strategy is only an agent
                # id for the non-signal funds (scalp / funding_arb write
                # their own names); the OrderRouter writes the ACTIVE
                # strategy-PROFILE name ("default", "arb_only", …) for
                # signal-fund trades, so anything that isn't a registered
                # agent id maps to "signal" rather than leaking the profile
                # name into the agent column / per-agent position filters.
                strategy = getattr(t, "strategy", None) or ""
                agent_id = strategy if strategy in _VALID_AGENTS else "signal"
                # Exit the watcher is looking for (item 5): TP/SL from the
                # trade row; funding/no-target rows fall back to "—".
                tp = getattr(t, "take_profit", None)
                sl = getattr(t, "stop_loss", None)
                exit_parts = []
                if tp:
                    exit_parts.append(f"TP {self._fmt_exit_px(tp)}")
                if sl:
                    exit_parts.append(f"SL {self._fmt_exit_px(sl)}")
                out.append({
                    "agent":       agent_id,
                    # Originating strategy/track (funding_arb | momentum |
                    # reversion | arb | ...). signal_type is the true track;
                    # strategy (the active strategy-profile name) is only a
                    # fallback for legacy rows written before signal_type.
                    "track":       getattr(t, "signal_type", None)
                                   or getattr(t, "strategy", None) or "signal",
                    "exit_target": " / ".join(exit_parts) or "—",
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
