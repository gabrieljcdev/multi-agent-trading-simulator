# Cross-cutting concerns — CryptoBot 1.0

Concerns that span multiple packages. The per-package detail lives in `module_reports/*.md`; this file is the cross-cutting view.

## 1. Async / event-loop layout

The process runs a single asyncio loop. `main.py:_run` constructs the `Coordinator` and runs `coordinator.start()`. The Coordinator owns every agent; each agent owns its own background tasks.

### Background loops in `core/bot.py` (the SignalAgent's CryptoBot)

| Loop | Defined at | Cadence | Purpose |
|------|------------|--------:|---------|
| `_cycle_loop` | `core/bot.py:454` | `BOT_LOOP_INTERVAL_SEC` (setting) | Top-of-loop: regime refresh, scan signals, route through gate + Claude + execution. |
| `_heartbeat_loop` | `core/bot.py:659` | `HEARTBEAT_INTERVAL_SEC` | Periodic state sample for guards (discrete sample, not rolling). |
| `_position_watcher_loop` | `core/bot.py:682` | `POSITION_WATCHER_INTERVAL_SEC` | Watches open positions, hits SL/TP via the router. |
| `_self_review_loop` | `core/bot.py:700` | `60s` (hardcoded — flagged in `module_reports/core.md` and `settings_audit.md`) | Periodic self-review against recent trades. |
| `_future_price_tracker_loop` | `core/bot.py:722` | `FUTURE_PRICE_TRACKER_INTERVAL_SEC` | Updates `signals.price_1h / price_4h / price_24h` from market data. |

### Background loops in agents

| Agent | Loop | Defined at | Cadence |
|-------|------|------------|---------|
| `Coordinator` | `_monitor_loop` | `agents/coordinator.py:224` | Periodic agent-health check. |
| `ScalpingAgent` | `_loop` | `agents/scalping_agent.py:1054` | Settings-driven; the scalper's main observe/decide loop. |
| `ScalpingAgent` | `_micro_price_tracker_loop` | `agents/scalping_agent.py:1099` | Settings-driven; tracks per-position micro prices for the in-flight scalp. |
| `FundingArbAgent` | `_loop` | `agents/funding_arb_agent.py:201` | Settings-driven scan of funding rates. |
| `BalanceAgent` | `_loop` | `agents/balance_agent.py:443` | Settings-driven inventory + rebalance plan. |

### Refresh loops in cross-cutting infra

| Loop | Defined at | Cadence |
|------|------------|---------|
| `DataSources.run_refresh_loop` | `data_sources/__init__.py:265` | Per-call `interval_sec` arg; runs every plugin source's `refresh()` and dispatches subscriptions via `asyncio.create_task`. |
| `MacroMonitor.run_refresh_loop` | `macro/monitor.py:285` | Internal interval; refreshes calendar sources + recomputes regime. |
| `MarketData.start_orderbook_stream` (per-exchange) | `core/market_data.py` (multiple) | Backoff on errors via `ORDER_BOOK_ERROR_BACKOFF_S`; runs `gather` over exchanges. |

### Shutdown path

`main.py:_run` registers SIGINT/SIGTERM handlers that set a `stop_event`. On wake:

1. `await asyncio.wait([...component_tasks, stop_waiter], return_when=FIRST_COMPLETED)` — wakes on shutdown OR component crash.
2. Within `SHUTDOWN_TIMEOUT_SEC`:
   - `coordinator.stop()` stops every agent (each agent cancels its own tasks).
   - `web_server.stop()` if started.
   - `cancel()` any task still running; `gather(..., return_exceptions=True)` with timeout.

The teardown was previously broken (SIGINT only stopped the signal bot's loop, leaving the coordinator and other agents alive → exit 9). The fix is recorded in the `main.py:135-141` block.

## 2. Plugin pattern usage — comprehensive table

The pattern (see `PLUGIN_PATTERN.md`): abstract base + registry list + discovery layer that picks up new entries with no caller-side changes.

| Place | Base class | Registry list | Discovery | Where consumed |
|-------|-----------|---------------|-----------|----------------|
| Sentiment sources | `sentiment.base.BaseSentimentSource` | `sentiment.sources.REGISTERED_SOURCES` (5 entries) | `sentiment.aggregator.SentimentAggregator.__init__` | `sentiment/aggregator.py` singleton `sentiment` |
| Data sources | `data_sources.base.BaseDataSource` | `data_sources.sources.REGISTERED_SOURCES` (13 entries) | `data_sources.__init__.DataSources` (lazy import) | `data_sources` singleton, polled by `data_sources.run_refresh_loop` |
| Macro calendar sources | `macro.sources.base.BaseCalendarSource` | `macro.sources.REGISTERED_CALENDAR_SOURCES` (2 entries) | `macro.monitor.MacroMonitor.__init__` | `macro_monitor` singleton |
| Agents | `agents.base.BaseAgent` | `agents.REGISTERED_AGENTS` (multiple wrappers; see `module_reports/agents.md`) | `agents/__init__.py` builds list at import time; gated on `is_available()` which itself reads env vars | `agents.coordinator.Coordinator` |
| Chain connectors | `execution.chains.base_connector.BaseChainConnector` | `execution.chains.REGISTERED_CONNECTORS` (4: arbitrum, base, optimism, solidly_volatile) | `execution.crosschain_engine.CrossChainArbEngine.__init__` | `CrossChainArbEngine` |
| Strategies | `strategies.base_strategy.BaseStrategy` | `strategies.STRATEGIES` (4: default, arb_only, scalper, custom) | `strategies.get_strategy(name)` | `main.py` selects active strategy; `core/bot.py` consults it |
| Balance rails | `agents.balance.rails.base.BaseTransferRail` | `agents.balance.rails.REGISTERED_RAILS` (2 entries: `SimTransferRail`, `CexTransferRail`) | `agents/balance_agent.py:91` falls back to `list(REGISTERED_RAILS)` when no `rails=…` injected | `BalanceAgent.plan_and_execute` |
| Balance policies | `agents.balance.policy.base.BasePolicy` | `agents.balance.policy.REGISTERED_POLICIES` (1 entry: `GrowthOptimalPolicy`) | `agents/balance_agent.py:90` falls back to `list(REGISTERED_POLICIES)` | `BalanceAgent.plan_and_execute` |

Notes:
- All 8 plugin sites follow the full pattern (abstract base + `REGISTERED_*` list + discovery layer that lets a new entry show up automatically with no caller change).
- The `is_available()` gating on agents and sources means a registered entry can silently be inert if its env var isn't set — `module_reports/sentiment.md` flags that 3 of 5 sentiment sources fall into this category in a vanilla install.

## 3. Approval modes (end-to-end)

`settings.APPROVAL_MODE ∈ {"per_trade", "window", "autonomous"}`. Dispatched in `core/bot.py:_route_for_approval` (`core/bot.py:577`).

### `per_trade`
- Signal hits `core/bot.py:_route_for_approval` → queued on `Bot._approval_queue`.
- `ui/prompts.py:ApprovalInputHandler` (or the web UI) drains the queue: operator presses `A` (approve) / `S` (skip) / `K` (kill) etc.
- On approve → `OrderRouter._sim_execute` / `_live_execute`.
- **Known gap:** `core/bot.py:604` carries a TODO — *"ui/prompts.py is a stub; until it exists, per_trade signals…"*. The terminal `ui/prompts.py` does exist (170 LOC) but the web-UI approval path is the production one; that note pre-dates the web UI.

### `window`
- Operator calls `Bot.approve_window(duration_minutes)` (`core/bot.py:335`) which clamps to `[WINDOW_MIN, WINDOW_MAX]` and sets `_window_until`.
- Within the window, signals auto-execute up to `_window_rate_limit_ok()` cap (`core/bot.py:635`).
- After expiry, signals are skipped until a new `approve_window` call.
- Web UI exposes this via `POST /action/approve_window`.

### `autonomous`
- Signals auto-execute, gated by `_autonomous_rate_limit_ok()` (hourly + daily caps).
- Skipped signals are logged with reason `autonomous_rate_limit`.

In all three modes the kill switch is the only thing that bypasses the gate — see §5.

## 4. Circuit breakers

Every breaker logs to the `circuit_breaker_log` table (317 rows at snapshot). The writer is `database.queries.log_circuit_breaker(reason, detail, auto_resume_at=None)`.

| Breaker | Defined at | Threshold setting | Effect when tripped |
|---------|------------|-------------------|---------------------|
| Per-agent error rate / drawdown | `agents/coordinator.py` (see `_monitor_loop` and `coordinator.py:halt_*`) | Per-agent settings | Coordinator halts the agent's start cycle; logs `circuit_breaker`. **Note:** `module_reports/agents.md` flags the portfolio-exposure breaker as log-only (`block_new_entries` enforcement is an outstanding TODO). |
| Bot daily-loss | `core/bot.py:441` calls `log_circuit_breaker(...)` then halts | `MAX_DAILY_LOSS_PCT` (settings) | Sets `_cb_state.halt(...)` → blocks new signal execution. |
| Bot consecutive losses | `core/bot.py:485` | `MAX_CONSECUTIVE_LOSSES` (settings) | Same halt mechanism. |
| Arb engine breaker | `execution/arb_engine.py:283`, `:911` | Internal stress thresholds (see arb engine code) | Pauses arb scanning. |
| Cross-chain breaker | `execution/crosschain_engine.py:342` | `XCHAIN_*` settings | Pauses cross-chain engine. |
| Position manager breaker | `execution/position_manager.py:80` | Per-call logic | Logs and halts. |
| Kill-switch event | `execution/kill_switch.py:48` | n/a — operator-driven | Logs `circuit_breaker` with reason `kill_switch` (closes positions; see §5). |

`CircuitBreakerLog.reason` is one of `daily_loss | consecutive_loss | drawdown | kill_switch` per the column comment at `database/models.py:748`.

## 5. Kill switch — full path

Operator action → all positions closed in parallel.

1. **Trigger.** One of:
   - Terminal: operator presses `K` in `ui/prompts.py:112` → `await self._bot.trigger_kill_switch("user_command")`.
   - Web UI: `POST /action/kill` route in `ui/web_server.py` calls `bot.trigger_kill_switch("web_ui")`.
   - Coordinator-driven: `agents/__init__.py:121` calls `await self._bot.trigger_kill_switch(reason="coordinator")` when the signal-agent wrapper decides to escalate.
   - Any agent: documented contract requires honouring kill switch — e.g. `agents/funding_arb_agent.py:137` cites it.

2. **`Bot.trigger_kill_switch(reason)`** at `core/bot.py:329`:
   - `await self._kill_switch.engage(reason=reason)`.
   - `self._cb_state.halt(f"kill_switch: {reason}")` — pre-empts any new signal execution.
   - Returns the `engage` result dict.

3. **`KillSwitch.engage(reason)`** at `execution/kill_switch.py:23`:
   - Loads open trades via `database.queries.get_open_trades()`.
   - Closes every trade in parallel via `asyncio.gather`.
   - Each per-trade close routes through `_close_position` which:
     - In sim: writes the close row with `exit_reason="kill_switch"`. **Note:** uses `trade.entry_price` as exit price — sim kills record `pnl_pct=0.0`. Flagged in `module_reports/execution.md`.
     - In live: calls `self._exchange_manager.market_close(...)` — **but** the `Bot` constructs `KillSwitch(exchange_manager=None)`, so the live path swallows `AttributeError` silently per-trade. Flagged in `module_reports/execution.md`.
   - Logs `circuit_breaker` with reason `kill_switch` via `log_circuit_breaker`.
   - Returns `{"closed": n, ...}`.

4. **Aftermath.** `_cb_state.halt(...)` blocks new entries until manually cleared.

Files involved in order: `ui/prompts.py` or `ui/web_server.py` → `core/bot.py` → `execution/kill_switch.py` → `database/queries.py`. Plus the breaker log row.

## 6. Sim vs Live mode

`settings.SIM_MODE` (bool). Set by `main.py` from `--sim` / `--live` flags or `settings.py` default. Every consumer reads it through `from config import settings; settings.SIM_MODE`.

Places `SIM_MODE` is consulted (20 files):

| File | Behaviour difference |
|------|----------------------|
| `main.py` | Logs "LIVE MODE ENABLED — real money at risk" warning; sets `settings.SIM_MODE` from flags. |
| `core/bot.py` | Constructs `KillSwitch(sim_mode=settings.SIM_MODE)`. |
| `agents/__init__.py` | Same. Wraps the signal agent's bot. |
| `agents/scalping_agent.py` | Sim path writes `scalp_observations` with `would_entry=...`; live path places orders via the router. |
| `agents/funding_arb_agent.py` | Sim path writes `funding_arb_observations`; live path posts perp+spot legs. |
| `agents/balance_agent.py` | Sim path uses `SimTransferRail` (synthetic settles); live path uses `CexTransferRail` (calls `_call_ccxt_withdraw` — currently a NotImplementedError stub gated by `REBALANCE_LIVE_ENABLED`). |
| `agents/balance/planner.py` | No execution; planner is mode-agnostic. |
| `agents/balance/rails/sim_rail.py` | Sim implementation. |
| `execution/router.py` | Dispatches `_sim_execute` vs `_live_execute`. The live path is a stub returning `None` — flipping `SIM_MODE=False` would make signal-track silently no-op every trade. **Critical finding** per `module_reports/execution.md`. |
| `execution/arb_engine.py` | Sim path simulates fills; live path (`_live_fills`) is the **only fully-implemented live order path** in the codebase. |
| `execution/crosschain_engine.py` | Sim path simulates settlement; live path requires inventory + chain RPCs. |
| `execution/funding_engine.py` | Sim path writes obs; live path places legs. |
| `execution/kill_switch.py` | Sim closes via entry_price (broken); live calls `exchange_manager.market_close` (also broken — see §5). |
| `execution/position_manager.py` | Sim writes close rows; live path placeholder. |
| `config/settings.py` | Defines the constant; provides REBALANCE_LIVE_ENABLED and other secondary flags. |
| `agents/balance/rails/sim_rail.py` | n/a — already sim-only. |
| `ui/dashboard.py` | Shows "SIM" / "LIVE" badge. |
| `ui/web_server.py` | Same. |
| `tests/*` | Multiple tests `monkeypatch.setattr(settings, "SIM_MODE", True/False)` for branch coverage. |

**Summary:** Sim is the working path. Live is partially built: arb engine has a complete live path; everything else (signal-track router, kill switch, balance rebalance, funding arb, cross-chain) ranges from "live path is a stub" to "live path is wired but broken / never tested". Flipping `SIM_MODE=False` is **not** safe today.

## 7. Logging

Single setup point: `utils.logger.setup_logging(debug: bool)`.

- **Format:** `%(asctime)s  %(levelname)-8s  %(name)-25s  %(message)s` (HH:MM:SS only — no date in the line, dates come from rotation suffixes).
- **Root level:** `DEBUG` if `--debug`, else `getattr(logging, settings.LOG_LEVEL)`.
- **Console handler:** `StreamHandler` at root level.
- **File handler:** `TimedRotatingFileHandler` at `LOGS_DIR / "cryptobot.log"`, rotated at midnight, `backupCount=30`, `utf-8` encoding, fixed at `DEBUG` level. Gated on `settings.LOG_TO_FILE`.
- **Noisy libs silenced** at WARNING: `ccxt`, `asyncio`, `urllib3`, `telethon`, `praw`.

Live state at snapshot:
- `logs/cryptobot.log` (today's active log)
- `logs/cryptobot.log.2026-05-20` … `2026-05-28` (9 rotated days retained — well within the 30 cap).
- `logs/smoke_test_stdout.log` — an extra log captured ad-hoc, not from the rotating handler.

Nothing is routed to external services (no syslog, no journald, no SaaS aggregator). All operational visibility is local files + the dashboard / web UI buffer (`WebLogHandler` in `ui/web_server.py`).
