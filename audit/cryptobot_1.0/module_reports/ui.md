# Module Report: ui

## Purpose

The `ui/` package provides CryptoBot's two operator surfaces: a Rich-based terminal dashboard (`dashboard.py` + `prompts.py`) and an aiohttp-served browser dashboard (`web_server.py` + `web_dashboard.html`). Both render the same underlying bot/coordinator state — the terminal UI is driven by `rich.Live` and a stdin daemon thread (`ApprovalInputHandler`), while the web UI broadcasts a complete JSON snapshot over a WebSocket at `WEB_UI_PUSH_INTERVAL_S` and exposes operator actions (approve / skip / kill / pause / set_mode / approve_window / rebalance) as REST POST endpoints. Both surfaces are read-only on coordinator/bot internals and re-enter the bot only via its public approval/control methods. The web server is an explicit "LOCAL OPERATOR TOOL ONLY — no auth, no SSL, localhost/LAN."

## Files

| File | LOC | One-sentence summary |
|------|-----|----------------------|
| `ui/__init__.py` | 0 | Empty package marker. |
| `ui/dashboard.py` | 2007 | `Dashboard` — 13-row Rich terminal layout with ~24 panels (header, portfolio, status, regime, sentiment, macro, market, funds, breakers, performers, events, positions, signal/arb/scalp/funding feeds, exchanges, sessions, log, approval, insights, footer, cmd bar). |
| `ui/prompts.py` | 170 | `ApprovalInputHandler` — daemon stdin reader bridging keystrokes (`a/s/w/k/p/q/h`) to async bot methods via `run_coroutine_threadsafe`. |
| `ui/web_server.py` | 1363 | `WebServer` — aiohttp app that serves `web_dashboard.html`, pushes JSON snapshots over WebSocket, and handles REST operator actions; includes `WebLogHandler` (logging handler feeding the UI log panel) and `_jsonable` (NaN/Inf scrubber). |
| `ui/web_dashboard.html` | 1291 | Single-page browser frontend — inline CSS + JS, hash-routed views, WebSocket client. |

## Public surface

### ui/__init__.py
**Docstring**: none.
**Classes**: none.
**Functions**: none.
**Module constants**: none — empty file.

### ui/dashboard.py
**Docstring**: "Rich terminal dashboard — single pane of glass for CryptoBot. Thirteen stacked rows: header, portfolio bar, status/regime/sentiment/macro, market overview, agents/circuit-breakers/top-performers/events, positions table, signal feed + arb feed + arb opportunity stats, scalp feed, exchange health + session performance, log feed + approval panel, insights strip, footer, command bar."

**Module-level functions**
- `_pnl_colour(pnl: float) -> str`
- `_regime_colour(regime: str) -> str`
- `_status_colour(status: str) -> str`
- `_fear_greed_colour(value: int) -> str`

**Dataclass `_ExchangeHealth`**
- `latency_ms: float = 0.0`
- `connected: bool = False`
- `last_seen: Optional[datetime] = None`

**Class `Dashboard`** — "Rich-Live terminal dashboard for CryptoBot."
- Class constants: `LOG_BUFFER_MAX = 40`, `SIGNAL_BUFFER_MAX = 20`, `ARB_BUFFER_MAX = 15`, `INSIGHT_BUFFER_MAX = 3`.
- `__init__(self, bot=None, coordinator=None)`
- `add_log(self, msg: str, level: str = "INFO") -> None`
- `add_signal(self, symbol: str, track: str, score: float, regime: str, action: str, agent_id: str = "") -> None`
- `add_arb(self, pair: str, buy_ex: str, sell_ex: str, gap_pct: float, pnl: float = 0.0) -> None`
- `add_insight(self, text: str) -> None`
- `update_exchange_health(self, exchange: str, latency_ms: float, connected: bool) -> None`
- `set_bot(self, bot) -> None` — late-bind primary bot for coordinator path.
- `render(self) -> Layout` — build the full 13-row Rich layout.
- `_safe(self, fn, name: str)` — wrap a panel-builder; on exception return a red error Panel.
- `async run(self) -> None` — drive `rich.Live` at 2 Hz until `stop()`.
- `async _refresh_coordinator_data(self) -> None` — await coordinator async getters into sync caches.
- `stop(self) -> None`
- Panel builders (all sync, return `Panel`): `_panel_header`, `_panel_portfolio`, `_panel_status`, `_panel_regime`, `_panel_sentiment`, `_panel_macro`, `_panel_market`, `_panel_agents`, `_panel_circuit_breakers`, `_panel_top_performers`, `_panel_pending_events`, `_panel_positions`, `_panel_signal_feed`, `_panel_arb_feed`, `_panel_arb_opportunities`, `_panel_scalp_feed`, `_panel_funding_feed`, `_panel_exchange_health`, `_panel_session_perf`, `_panel_log_feed`, `_panel_approval`, `_panel_insights`, `_panel_footer`, `_panel_cmd_bar`.
- Snapshot helpers: `_snapshot_scalp_agent(self) -> dict`, `_snapshot_funding_agent(self) -> dict`.
- Helpers: `_stack(self, *items)`, `_sentiment_latest(self) -> dict`, `_read_trade_stats(self)`, `_open_exposure_pct(self, equity: float) -> float`, `_signals_fired_count(self) -> int`, `_signals_skipped_count(self) -> int`, `_last_signal_time(self) -> str`, `_peek_pending(self) -> Optional[dict]`, `_current_session(self, utc: datetime) -> str`, `_session_for(self, dt: Optional[datetime]) -> str`, `_fmt_duration(self, delta: timedelta) -> str`, `_progress_bar(self, value: float, total: float, width: int) -> Text`.

**Module constants**: `logger = logging.getLogger(__name__)`.

### ui/prompts.py
**Docstring**: "ApprovalInputHandler — a daemon stdin reader that turns user keystrokes into actions on the running CryptoBot. Runs alongside Rich Live so the dashboard owns the alternate screen buffer while we own line-buffered stdin in a parallel thread."

**Class `ApprovalInputHandler`** — "Reads stdin in a daemon thread; dispatches actions onto the bot's asyncio loop. Construct after the loop is running and call start()."
- `__init__(self, bot, loop: Optional[asyncio.AbstractEventLoop] = None)`
- `start(self) -> None` — launch daemon reader thread.
- `stop(self) -> None` — signal exit on next iteration.
- `_run(self) -> None` — line-buffered stdin read loop.
- `_dispatch(self, cmd: str) -> Optional[str]` — map command → action; returns short result label.
- `_set_state(self, current: Optional[str] = None, last_result: Optional[str] = None) -> None` — mirror cmd-bar state onto bot for the dashboard panel.
- `_call_coro(self, coro) -> None` — submit coroutine to bot's loop via `run_coroutine_threadsafe`; blocks up to 15 s.

**Module constants**: `logger`, `_HELP` (one-line command vocabulary string).

### ui/web_server.py
**Docstring**: "Browser-based operator control panel. A lightweight aiohttp server that runs alongside the bot, pushes a full state snapshot over a WebSocket at WEB_UI_PUSH_INTERVAL_S, and exposes operator actions (approve / skip / kill / pause / set-mode / approve-window) as REST POST endpoints. LOCAL OPERATOR TOOL ONLY — no auth, no SSL, localhost/LAN. Never exposed to the internet."

**Module-level functions**
- `_session_for_hour(hour: int) -> str` — UTC-hour to LONDON/NEW_YORK/ASIA/OFF_HOURS.
- `_local_time(tz: str) -> str` — HH:MM in the given IANA zone, fallback UTC.
- `_jsonable(obj)` — recursively coerce NaN/Inf to None so `JSON.parse` doesn't reject the payload.

**Class `WebLogHandler(logging.Handler)`** — "Appends structured entries to a bounded deque the snapshot reads."
- `__init__(self, buffer: deque)`
- `@staticmethod _classify(record: logging.LogRecord, msg: str) -> str` — kill/err/warn/arb/skip/exec/info.
- `emit(self, record: logging.LogRecord) -> None`

**Class `WebServer`**
- `__init__(self, coordinator=None, bot=None)`
- `_make_app(self) -> web.Application` — build aiohttp app + route table.
- `_load_html(self) -> str`
- `async start(self) -> None` — install log handler, start AppRunner + TCPSite, kick off broadcast loop.
- `async stop(self) -> None` — cancel broadcast task, close WS clients, tear down runner, remove log handler.
- `async _broadcast_loop(self) -> None`
- `async _refresh_coordinator(self) -> None`
- `async _broadcast(self, payload: str) -> None`
- `_resolve_bot(self)` — injected bot → coordinator.get_primary_bot() → None.
- `async handle_index(self, request) -> web.Response`
- `async handle_ws(self, request) -> web.WebSocketResponse` — heartbeat=30, immediate snapshot on connect.
- `@staticmethod async _body(request) -> dict`
- `async handle_approve(self, request) -> web.Response`
- `async handle_skip(self, request) -> web.Response`
- `async handle_kill(self, request) -> web.Response`
- `async handle_pause(self, request) -> web.Response`
- `async handle_set_mode(self, request) -> web.Response`
- `async handle_rebalance(self, request) -> web.Response` — three-action arm/confirm/cancel dispatcher with confirm-token TTL.
- `async handle_approve_window(self, request) -> web.Response`
- `_agent_trades(self, agent_id: str) -> list`
- `async handle_agent_detail(self, request) -> web.Response`
- `async handle_session_detail(self, request) -> web.Response`
- `_build_snapshot(self) -> dict` — complete per-tick state; never raises.
- `_get_agent(self, agent_id: str)`
- `@staticmethod _status_from(agent, *, observation_attr: str = "observation_mode") -> str`
- `_snap_arb_v2(self) -> dict`, `_snap_xchain(self) -> dict`, `_snap_funding(self) -> dict`, `_snap_balance(self) -> dict`
- `@staticmethod _safe(fn, fallback)`
- `_snap_portfolio(self, exposure_usd: float = 0.0) -> dict`
- `_snap_scalp(self, bot) -> dict`, `_snap_scalp_live(self, bot) -> list`
- `_snap_agents(self) -> list`
- `_snap_circuit_breakers(self, bot) -> dict`
- `_snap_regime(self) -> dict`
- `_snap_sentiment(self, bot) -> dict`
- `_snap_exchanges(self, bot) -> list`
- `_snap_signals(self) -> list`
- `_snap_pending(self, bot)`
- `_snap_positions(self, bot) -> list`

**Module constants**
- `logger = logging.getLogger(__name__)`
- `_HTML_PATH = Path(__file__).parent / "web_dashboard.html"`
- `_VALID_MODES = ("per_trade", "window", "autonomous")`
- `_VALID_AGENTS = ("signal", "arb", "scalp", "xchain", "funding_arb", "balance")`
- `_SESSIONS = ("LONDON", "NEW_YORK", "ASIA", "OFF_HOURS")`
- `_SESSION_TZ = {"LONDON": ("Europe/London", "LON"), "NEW_YORK": ("America/New_York", "NYC"), "ASIA": ("Asia/Tokyo", "TYO"), "OFF_HOURS": ("UTC", "UTC")}`

### ui/web_dashboard.html

- File size: 61,967 bytes (1291 lines).
- Top-level structure (line ranges approximate):
  - Lines 1-6: doctype, `<head>` meta + `<title>CryptoBot — Control Panel</title>`.
  - Lines 7-102: single inline `<style>` block (~96 lines of CSS — light/dark CSS-variable theme, top bar, tabs, metric grid, agent grid, tables, bars, dots, chips, session cards, log filters, agent/session pagehead).
  - Lines 104-269: `<body>` with `<div class="wrap" id="root">` containing:
    - Topbar with status badge, mode badges, action buttons (kill, window, pause, mode chips).
    - Live banner.
    - Tab strip (`dashboard`, `arb`, `log`).
    - View `#view-dashboard`: metrics grid (bankroll, daily P&L, exposure, win rate), agents grid (6 cards), regime/sentiment, signal feed + approval, circuit breakers + exchanges, positions, session P&L (4 cards), agent detail subview.
    - View `#view-agent`: pagehead + live area + trade log + insights.
    - View `#view-session`: pagehead + closed-trades table.
    - Panel `#panel-arb`: arb metrics + arb trade table.
    - Panel `#panel-log`: filter chips + log feed.
  - Lines 270-1289: single inline `<script>` block (~1020 lines of JS — DOM helpers, action POSTers, hash-routed view switcher, WebSocket client and snapshot renderer, agent/session detail fetchers, rebalance arm/confirm/cancel flow).
  - Lines 1290-1291: `</body></html>`.
- Embedded API/WS endpoints called from inline JS:
  - `POST /action/approve` — approve button (line 671).
  - `POST /action/skip` body `{reason:"web_skip"}` (line 672).
  - `POST /action/set_mode` body `{mode}` (line 331).
  - `POST /action/approve_window` body `{minutes:60}` (line 332).
  - `POST /action/pause` body `{paused}` (line 334).
  - `POST /action/kill` (line 343).
  - `POST /action/rebalance` body `{action: arm|confirm|cancel, confirm_token?}` (line 372).
  - `GET /api/agent/{id}` (line 1187).
  - `GET /api/session/{name}` (line 1225).
  - `new WebSocket("ws://" + location.host + "/ws")` (line 1275).

## API routes (web_server.py)

Registered in `WebServer._make_app` (aiohttp). All POST handlers return `{"ok": bool, ...}` and never raise — exceptions are caught and serialised as `{"ok": False, "error": str(e)}`.

| Method | Path | Handler | Purpose |
|--------|------|---------|---------|
| GET    | `/` | `handle_index` | Serve the cached `web_dashboard.html` text. |
| GET    | `/ws` | `handle_ws` | WebSocket upgrade (heartbeat=30); sends immediate snapshot, joins broadcast set. |
| GET    | `/api/agent/{agent_id}` | `handle_agent_detail` | `{trades, insights}` for an agent page; 404 if id not in `_VALID_AGENTS`. |
| GET    | `/api/session/{session_name}` | `handle_session_detail` | Today's closed trades + total PnL + local clock for a session; 404 if unknown. |
| POST   | `/action/approve` | `handle_approve` | Drain next pending signal via `bot.approve_next_pending()`. |
| POST   | `/action/skip` | `handle_skip` | `bot.skip_next_pending(reason)` with `reason` from JSON body (default `"web_skip"`). |
| POST   | `/action/kill` | `handle_kill` | `coordinator.kill_all(reason="web")`; logs `KILL_WEB` agent event. |
| POST   | `/action/pause` | `handle_pause` | Sets `bot._paused = bool(body["paused"])`. |
| POST   | `/action/set_mode` | `handle_set_mode` | Validate mode ∈ `_VALID_MODES`, set `settings.APPROVAL_MODE` + `bot.approval_mode`. |
| POST   | `/action/approve_window` | `handle_approve_window` | `bot.approve_window(minutes)` (default 60). |
| POST   | `/action/rebalance` | `handle_rebalance` | Three actions (`arm`/`confirm`/`cancel`) over the BalanceAgent's confirm-token flow; logs `REBALANCE_ARM` and `REBALANCE_WEB` agent events. Gates `confirm` on `SIM_MODE or REBALANCE_LIVE_ENABLED`. |

## Imports graph

**Imports from project**
- `dashboard.py`: `from config import settings`; lazy in panels: `core.regime_detector`, `macro.macro_monitor`, `database.queries`.
- `prompts.py`: lazy `from config import settings as _s` inside the `window` command handler. No top-level project imports — the bot is passed in.
- `web_server.py`: `from config import settings`, `from database import queries as db_queries`; lazy `from agents.balance.inventory_state import inventory_state` inside `_snap_balance`. `core.regime_detector` is lazily imported inside `_snap_regime`.
- `web_dashboard.html`: no Python imports (browser asset).

**Imported by (project)**
- `core/bot.py:267` — `from ui.prompts import ApprovalInputHandler` (per-trade approval CLI).
- `main.py:117` — `from ui.dashboard import Dashboard`.
- `main.py:128` — `from ui.web_server import WebServer`.
- Tests: `tests/test_dashboard.py`, `tests/test_prompts.py`, `tests/test_macro.py` (`from ui.dashboard import Dashboard`), `tests/test_web_server.py` (`from ui.web_server import WebServer, WebLogHandler` + `import ui.web_server as wsm`).

## Tests

### tests/test_dashboard.py (39 tests)
- `test_render_returns_layout_without_error` — `Dashboard(bot).render()` returns a `Layout` instance.
- `test_render_can_be_drawn_to_console` — Layout can be rendered through a Rich Console with no exception.
- `test_render_with_coordinator_none_degrades` — Render succeeds with `coordinator=None`.
- `test_render_survives_broken_bot_attribute` — Panels that touch a deliberately bad attribute still render via `_safe`.
- `test_add_log_respects_maxlen` — Log buffer caps at `LOG_BUFFER_MAX`.
- `test_add_signal_stores_fields` — Signal buffer captures supplied fields.
- `test_add_signal_respects_maxlen` — Signal buffer caps at `SIGNAL_BUFFER_MAX`.
- `test_add_arb_stores_and_caps` — Arb buffer stores entries and caps at `ARB_BUFFER_MAX`.
- `test_add_insight_caps_at_three` — Insight buffer caps at 3.
- `test_update_exchange_health_records_latest` — Exchange-health dict stores latest snapshot.
- `test_approval_panel_idle_when_queue_empty` — Approval panel renders idle placeholder.
- `test_approval_panel_shows_pending_signal` — Approval panel renders pair/score/direction for a peeked signal.
- `test_session_classification` — `_current_session` boundary correctness.
- `test_signals_skipped_count_uses_new_query` — Uses `queries.get_today_skipped_signals`.
- `test_query_module_exports_get_today_skipped_signals` — Required query exists in `database.queries`.
- `test_duration_format` — `_fmt_duration` formats h/m/s.
- `test_panels_do_not_call_async_coordinator_directly` — Panels read caches, never call coroutines synchronously.
- `test_refresh_coordinator_data_caches_async_results` — Async getters populate the caches.
- `test_refresh_coordinator_data_survives_async_errors` — Errors are swallowed; render continues.
- `test_macro_panel_renders_no_raw_markup_tags` — No literal `[dim]` markup leaks into rendered output.
- `test_approval_panel_idle_shows_command_vocab` — Idle approval panel lists keystroke vocab.
- `test_approval_panel_pending_shows_pair_score_and_commands` — Pending panel surfaces pair, score, command vocab.
- `test_cmd_bar_idle_shows_hint` — Cmd bar shows hint when idle.
- `test_cmd_bar_echoes_current_input_and_last` — Cmd bar reflects `_cmd_current` / `_cmd_last_result`.
- `test_full_dashboard_renders_with_cmd_bar` — Full layout includes the cmd bar row.
- `test_arb_opportunity_panel_renders_with_stats` — Funnel renders with non-zero detection count.
- `test_arb_opportunity_panel_placeholder_on_empty_stats` — Placeholder when no detections.
- `test_arb_opportunity_panel_amber_when_exec_rate_below_50` — Colour ladder amber threshold.
- `test_arb_opportunity_panel_red_when_exec_rate_below_20` — Colour ladder red threshold.
- `test_dashboard_does_not_crash_on_stats_query_failure` — Query failure → degraded panel.
- `test_arb_panel_shows_missed_balance_checks_count` — Missed-balance counter rendered.
- `test_arb_panel_balance_miss_amber_at_threshold` — Amber styling at `DASHBOARD_BALANCE_MISS_AMBER`.
- `test_arb_panel_balance_miss_red_at_threshold` — Red styling at `DASHBOARD_BALANCE_MISS_RED`.
- `test_arb_panel_handles_missing_missed_balance_attr` — Defaults to 0 when attr absent.
- `test_scalp_panel_placeholder_when_no_agent` — Placeholder when scalp agent not registered.
- `test_scalp_panel_renders_header_and_sections_when_available` — All five scalp sections render.
- `test_scalp_panel_sim_and_live_badges` — Header badge switches between SIM and LIVE.
- `test_scalp_panel_handles_missing_agent_state` — Missing internal attrs don't crash.
- `test_scalp_snapshot_handles_observation_summary_failure` — Stats failure → empty stats.
- `test_scalp_snapshot_sorts_ofi_by_abs_z` — OFI ranking sort by `|z|`.
- `test_full_dashboard_includes_scalp_row` — Full layout includes scalp row.

### tests/test_prompts.py (6 tests)
- `test_help_command_logs_help_and_records_state` — `h` logs `_HELP` and records "help".
- `test_pause_command_toggles_and_returns_label` — `p` returns "paused"/"resumed".
- `test_window_command_calls_approve_window_with_settings_default` — `w` uses `settings.WINDOW_DEFAULT_DURATION_MINUTES`.
- `test_unknown_command_returns_unknown_label` — Unknown commands log + return "unknown: …".
- `test_run_loop_writes_cmd_state_per_line` — `_run` populates `_cmd_current` and `_cmd_last_result` per line.
- `test_run_loop_exits_on_quit` — `q`/`quit` exits the loop.

### tests/test_web_server.py (~50 tests, key ones)
- `test_snapshot_returns_complete_dict` — `_build_snapshot` returns a dict with every documented top-level key.
- `test_snapshot_safe_when_bot_is_none` — Snapshot never raises with no bot.
- `test_snapshot_safe_when_coordinator_raises` — Coordinator errors don't propagate.
- `test_ws_client_receives_snapshot` — Fresh WS client gets an immediate snapshot.
- `test_multiple_ws_clients_all_receive_broadcast` — Broadcast fans out.
- `test_kill_action_calls_coordinator_kill_all` / `test_kill_action_safe_when_coordinator_none`.
- `test_approve_action_drains_approval_queue` / `test_skip_action_removes_pending_signal`.
- `test_set_mode_updates_bot_approval_mode` / `test_pause_action_sets_bot_paused`.
- `test_server_start_stop` — Lifecycle starts and stops cleanly.
- `test_web_log_handler_appends_to_buffer` — `WebLogHandler.emit` writes to the buffer.
- Bankroll / exposure / scalp fidelity: `test_snapshot_bankroll_equals_starting_plus_realised_pnl`, `test_snapshot_bankroll_does_not_reset_at_utc_midnight`, `test_snapshot_omits_removed_equity_and_total_pnl_fields`, `test_daily_fees_aggregates_across_agents`, `test_exposure_includes_scalp_positions`, `test_exposure_recalculated_each_snapshot`, `test_scalp_win_rate_zero_when_no_trades`, `test_scalp_win_rate_computed_from_pnl_bps_positive`, `test_scalp_win_rate_resets_at_utc_midnight`, `test_scalp_closed_trades_persisted_in_snapshot`, `test_scalp_closed_trades_buffer_capped_at_setting`, `test_scalp_live_trades_separate_from_closed`.
- Agent/session detail endpoints: `test_api_agent_signal_returns_trades_and_insights`, `test_api_agent_arb_returns_trades_and_insights`, `test_api_agent_scalp_returns_trades_and_insights`, `test_api_agent_placeholders_now_404`, `test_api_agent_unknown_returns_404`, `test_api_agent_insights_filtered_by_agent_id`, `test_api_session_returns_today_closed_trades_only`, `test_api_session_excludes_yesterdays_trades`, `test_api_session_includes_all_agents`, `test_api_session_unknown_returns_404`.
- Web UI v2 panel snapshots: `test_snapshot_includes_arb_extended_keys`, `test_snapshot_includes_xchain_keys`, `test_snapshot_includes_funding_keys`, `test_snapshot_includes_balance_keys`, `test_snapshot_safe_when_balance_agent_raises`, `test_snapshot_safe_when_xchain_engine_raises`, `test_snapshot_safe_when_funding_engine_raises`, `test_placeholder_agents_not_in_snapshot`.
- Rebalance flow: `test_rebalance_arm_returns_token`, `test_rebalance_confirm_executes_within_window`, `test_rebalance_confirm_expired_token`, `test_rebalance_confirm_mismatched_token`, `test_rebalance_cancel_invalidates_token`, `test_rebalance_blocked_when_live_disabled`, `test_rebalance_blocked_when_balance_agent_missing`, `test_rebalance_logs_event_on_confirm`, `test_rebalance_confirm_replan_mismatch`.

### tests/test_macro.py
- Three Dashboard-related tests at lines 396/416/436 importing `ui.dashboard.Dashboard` and rendering the macro panel against various macro_monitor states (regime present, missing, and the dim-placeholder path) — verifying the macro panel degrades cleanly.

## TODOs / FIXMEs / stubs

- `ui/dashboard.py:1037` — `# TODO: hook into the engine's daily reset once a reset hook is exposed (today the counter survives until process restart).`
- `ui/dashboard.py:1941` — comment in `_peek_pending` docstring: "Falls back to None on missing method (e.g. test stub) or any error." (acceptance of stub case, not an open TODO.)

No FIXME / XXX / HACK markers found in `ui/`.

## Known issues observed

- **`_panel_footer` advertises legacy keys that don't exist in the handler.** The footer's second row lists `[G] Go`, `[M] Modify`, `[I] Info`, `[1-5] Select agent` (dashboard.py:1831-1836), but `ApprovalInputHandler._dispatch` only knows `a/s/w/k/p/q/h`. The approval panel and cmd bar correctly show the working vocabulary, but the footer is stale and misleads the operator.
- **`/action/pause` writes a private bot attribute (`bot._paused = paused`).** No method on `Bot` is invoked, so circuit-breaker / coordinator state has no opportunity to react. Compare with `_dispatch`'s `p` command in prompts which calls `bot.toggle_pause()` — the web action is asymmetric.
- **`/action/set_mode` mutates `settings.APPROVAL_MODE` directly and then mirrors to `bot.approval_mode`.** Settings is the single tuning instrument per CLAUDE.md, but mutating it at runtime from a route is not propagated to any profile and only persists until the process restarts.
- **`Dashboard._panel_footer` references obsolete approval shortcuts; `_panel_approval` and `_panel_cmd_bar` use the current ones.** The codebase has two competing documents for the input vocabulary; only the latter is in sync with `prompts.py`.
- **Web server has no auth/SSL.** Explicitly stated in the module docstring; not a defect, but it must never be exposed beyond localhost/LAN.
- **`WebServer._build_snapshot` and `Dashboard._refresh_coordinator_data` both reach into `coordinator._agents` (a private attribute).** Fragile to coordinator refactors. Same pattern with `agent._positions`, `agent._engine`, `agent._observations`, `agent._last_opps`, `agent._capital`, `agent._daily_pnl`, `agent._last_computed_targets`, `agent._arm_token`, `agent._arm_expires_at`, `agent._transfers_today`, `agent._fees_today_usd`, `agent._daily_rebalances`, `agent._paused`, `agent._running`. The UI is heavily coupled to private state of multiple agent classes.
- **Web UI v2 placeholder fields acknowledged in code.** `_snap_arb_v2.exchanges`/`gap_distribution`, `_snap_xchain.chains`/`best_pair.would_entry`, `_snap_funding.symbols`, and "Phase 1 not tracked" fields (`delta_usd`, `realised_apr_pct`, `next_exit_check`) are intentionally empty — engine-side surface required to populate them.
- **`Dashboard` and `WebServer` each maintain their own coordinator caches and snapshot-builder code paths** — significant duplication between `_refresh_coordinator_data` (dashboard) and `_refresh_coordinator` (web), and between the dashboard's `_snapshot_scalp_agent` / `_snapshot_funding_agent` and the web's `_snap_scalp` / `_snap_funding`. Drift risk.
- **`dashboard.py` is 2007 lines and almost certainly past the comfortable single-file size** — every panel lives on the `Dashboard` class. Splitting per panel or per row would help.
- **`Dashboard._panel_market` calls `next(iter(prices.values()))` after `prices = {}`** — guarded by `if not prices: continue`, so safe, but the picked price is "first exchange" non-deterministically; cross-exchange disagreement is silently hidden.
- **`Dashboard._signals_fired_count` returns `len(q.get_today_trades())`** despite the panel label "Signals fired" — counts executed trades, not signals; correct for the intended display but the naming is misleading.
- **The `_panel_market` `_panel_session_perf` `_panel_approval` and others lazily import `database.queries` inside each render call** — every tick re-runs the import lookup. Minor, but a hot path.
