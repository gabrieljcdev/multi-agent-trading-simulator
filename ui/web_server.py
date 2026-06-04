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
                 "opportunity_scanner")
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
            web.post("/action/approve",        self.handle_approve),
            web.post("/action/skip",           self.handle_skip),
            web.post("/action/kill",           self.handle_kill),
            web.post("/action/pause",          self.handle_pause),
            web.post("/action/set_mode",       self.handle_set_mode),
            web.post("/action/approve_window", self.handle_approve_window),
            web.post("/action/rebalance",      self.handle_rebalance),
            web.post("/action/agent/{agent_id}/halt",   self.handle_agent_halt),
            web.post("/action/agent/{agent_id}/resume", self.handle_agent_resume),
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
        return web.Response(text=self._html or self._load_html(),
                            content_type="text/html")

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
            "opportunity":    self._snap_opportunity(),
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

    def _snap_opportunity(self) -> dict:
        """Opportunity Scanner panel (READ-ONLY — surfaces reasoning,
        exposes no action control).

        Two clearly-fenced lists: `standard` is the trajectory-ranked
        actionable view (survivors only by default, every row leading
        with competitor_trend — edge never travels without it);
        `exploratory` is the FEATURE BLOCK 6b lane sorted by
        unconventional_score, carrying the free-text rationale the
        operator reads inline. The two never merge. Every read is
        defensive — one broken read never crashes render.
        """
        out = {
            "status":      "OFFLINE",
            "standard":    [],
            "exploratory": [],
            "summary": {
                "n_core": 0, "n_survivable": 0, "n_disqualified": 0,
                "n_observations": 0, "n_labeled": 0, "by_trend": {},
                "mean_detection_latency_ms": 0.0,
                "session_detection_latency_ms": 0.0,
                "detection_latency_sla_ms": float(getattr(
                    settings, "OPPORTUNITY_DETECTION_LATENCY_SLA_MS", 0.0)),
                "candidates_seen_session": 0,
            },
        }
        agent = self._get_agent("opportunity_scanner")
        out["status"] = self._status_from(agent)

        def _trim(row: dict) -> dict:
            # Keep the snapshot light; competitor_trend ALWAYS rides
            # beside the edge fields (render invariant).
            return {
                "opp_type":               row.get("opp_type"),
                "protocol":               row.get("protocol"),
                "chain":                  row.get("chain"),
                "market_key":             row.get("market_key"),
                "first_seen":             row.get("first_seen"),
                "competitor_trend":       row.get("competitor_trend"),
                "competitor_count":       row.get("competitor_count"),
                "edge_annualized_pct":    row.get("edge_annualized_pct"),
                "edge_confidence":        row.get("edge_confidence"),
                "edge_display":           row.get("edge_display"),
                "reachability_verdict":   row.get("reachability_verdict"),
                "window_status":          row.get("window_status"),
                "risk_status":            row.get("risk_status"),
                "unconventional_score":   row.get("unconventional_score"),
                "unconventional_factors": row.get("unconventional_factors") or [],
                "unconventional_rationale": row.get("unconventional_rationale"),
            }

        try:
            if agent is not None:
                std = agent.get_ranked_view(mode="standard") or []
                exp = agent.get_ranked_view(mode="exploratory") or []
            else:
                std = db_queries.get_ranked_opportunities(mode="standard")
                exp = db_queries.get_ranked_opportunities(mode="exploratory")
            out["standard"]    = [_trim(r) for r in std[:15]]
            out["exploratory"] = [_trim(r) for r in exp[:15]]
        except Exception as e:
            logger.debug("opportunity ranked views failed: %s", e)
        try:
            if agent is not None:
                out["summary"] = agent.get_observation_summary()
            else:
                out["summary"].update(db_queries.get_opportunity_summary())
        except Exception as e:
            logger.debug("opportunity summary failed: %s", e)
        return out

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
                capital = round(float(getattr(a, "capital_allocated", 0.0) or 0.0), 2)
                status = getattr(a, "status", "OFFLINE")
                # Funding sim-capital trial: the allocation attr stays 0 by
                # design (BalanceAgent ledger contract) — show the sim budget
                # on the card so the trial is visible. Display-only; the
                # coordinator's portfolio totals are untouched.
                if agent_id == "funding_arb":
                    live = self._get_agent("funding_arb")
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
