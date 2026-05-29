# CryptoBot 1.0 — Full Project Audit

> **Snapshot label:** CryptoBot 1.0
> **Snapshot date (UTC):** 2026-05-29
> **Git SHA:** `8e3efd1ae4c1f4b268e4ecfa7a4bdc9b9aca8b7f`
> **Branch:** `feat/web-ui-v2-agent-panels`
> **Total files inventoried:** 193 (excluding `venv/`, `.git/`, `__pycache__/`, `*.pyc`, `node_modules/`, `.pytest_cache/`, `logs/`, `backups/`, `audit/`)
> **Total lines across inventoried files:** 52,587 (Python: 39,738; Markdown: 10,151)
> **Tests at snapshot:** 552 passed, 1 warning, 0 failures (≈12s wallclock)
> **Author:** Auto-generated audit (Claude Code, per `prompts/cryptobot_audit.md`)

This document is the **single source of truth** for the CryptoBot 1.0 snapshot. Every other artefact under `audit/cryptobot_1.0/` is referenced from here. Read this document to understand the entire system at the level of *what's here, what it does, how the pieces connect, and what's deliberately not built yet*.

---

## Executive summary

CryptoBot 1.0 is a **situational, multi-agent crypto trading assistant**. The bot itself does not freely trade — Claude reasons about each signal and the operator chooses how much autonomy to grant via an `APPROVAL_MODE` (`per_trade` / `window` / `autonomous`). A kill switch closes every open position on demand.

The codebase has matured into a coordinator-of-agents shape:

- **`agents.coordinator.Coordinator`** owns every agent. The `SignalAgent` wraps the original `core.bot.CryptoBot` (the signal track), and four sibling agents handle dedicated domains: `ScalpingAgent`, `FundingArbAgent`, `CrossChainArbAgent`, `BalanceAgent`. Each is registered in `agents.REGISTERED_AGENTS`, each has its own background loop, and each honours the shared kill-switch contract.
- **`SignalEngine` → `QualityGate` → `ClaudeAgent` → `OrderRouter`** is the heart of the signal track, scanning a configured pair universe at `BOT_LOOP_INTERVAL_SEC`. Score composition is additive: `Signal.score = clamp(raw_score + regime + session + guard + OFI + sentiment, 0, 100)`.
- **`config/settings.py`** is the single tuning instrument — 443 module-level constants, 234 (52.8 %) annotated with `# test:` sweep ranges. Profiles (`profiles/*.json`) overlay it at runtime.
- **Plugin pattern** (`PLUGIN_PATTERN.md`) repeats eight times: sentiment sources, data sources, macro calendar sources, agents, chain connectors, strategies, balance rails, balance policies. Each is an abstract base + `REGISTERED_*` list + discovery layer; a new entry shows up automatically with no caller change.

Current status by package:

| Package | LOC | State |
|---------|----:|-------|
| `core/` | 2,330 | **Production.** Bot loop, regime detector, guards, Claude agent, market data. |
| `agents/` | 6,626 | **Production.** Coordinator + 5 agents + balance subtree. |
| `execution/` | 3,605 | **Mostly production; live paths are partial.** Arb engine has the only fully-implemented live order path. Signal-track `_live_execute` is a stub returning `None`. |
| `signals/` | 1,022 | **Production.** Scanners (arb/momentum/reversion) + OFI + QualityGate. |
| `database/` | 3,302 | **Production.** 21 ORM models, 91 query helpers, **no Alembic** — five model TODOs flag schema gaps. |
| `ui/` | 3,540 | **Production (active feature branch).** Terminal dashboard (Rich) + web control panel (aiohttp). Web UI v2 agent panels are the current branch (`feat/web-ui-v2-agent-panels`). |
| `sentiment/` | 1,018 | **Production.** 5 sources registered; 3 inert in a vanilla install (missing optional deps or env vars). |
| `data_sources/` | 2,565 | **Production.** 13 sources registered, all wired to `data_sources` singleton. |
| `macro/` | 1,077 | **Production for the inputs; output partially unwired.** `MacroSignal` pipeline exists but no consumer imports it. |
| `strategies/` | 272 | **Production.** 4 strategies; zero unit tests. |
| `profiles/` | 90 | **Production.** 4 JSON profiles overlay `Profile` dataclass fields. |
| `utils/` | 174 | **Production.** Logger + hurst exponent. |
| `config/` | 1,269 | **Production.** Single tuning instrument. |
| `scalping_v2/` | 1,791 | **Reference bundle.** Docs + recalibration cookbook + demo + standalone tests; three .py files are byte-identical duplicates of the live copies in `agents/`. |
| `predictive/` | 0 | **Empty stub.** `__init__.py` only. CLAUDE.md mentions `python -m predictive.trainer`; nothing exists. |

Deliberately deferred (see `module_reports/scalping_v2.md` and `scalping_v2/SCALPING_V3_ROADMAP.md`):

- Maker/taker fee-side selection in v3.
- Multi-symbol OFI normalisation across the whole universe (not just BTC/ETH).
- Live exchange-side WebSocket book streaming for non-MEXC venues (`ccxt.async_support`'s `watch_*` is currently dead per `MEMORY.md → project_scalper_no_observations`).
- 7 other items catalogued in V3-1 … V3-10.

Failure modes worth knowing before running anything (the surface, full detail under "Known TODOs, stubs, and placeholders"):

1. **`SIM_MODE = False` is not safe in 1.0.** `OrderRouter._live_execute` returns `None`; `KillSwitch` live path AttributeErrors silently; `BalanceAgent` live withdrawal is a NotImplementedError. Only `ArbEngine._live_fills` is complete.
2. **`Bot._cb_state` is the only thing that turns a kill-switch trigger into "block new entries".** Per `module_reports/agents.md`, the coordinator's portfolio-exposure breaker is log-only — the `block_new_entries` enforcement TODO is open.
3. **`requirements.txt` and the running `venv/` are deeply out of sync** — re-installing from `requirements.txt` would downgrade numpy, pandas, pytest, anthropic, xgboost, scikit-learn, etc. by major versions and break the surface the tests pass against. `pip_freeze.txt` is the reproducible source.
4. **`predictions` table is empty (0 rows)** because `predictive/` is empty. `Prediction` model has no `queries.py` helpers.
5. **`scalp_observations` is 214,570 rows** — by far the largest write source. Adverse-selection tuning and the recalibration cookbook target this table.

The audit ran read-only over source. No files were modified. Tests were executed (552 / 552 pass).

---

## System architecture

### Seven layers (top-down)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. Operator interfaces                                                      │
│    ui/dashboard.py (Rich terminal)        ui/web_server.py (aiohttp WS+REST)│
│    ui/web_dashboard.html (browser HTML)   ui/prompts.py (keyboard handler)  │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │ approval, kill, set_mode, approve_window
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. Multi-agent coordinator                                                  │
│    agents/coordinator.py                                                    │
│    agents/__init__.py:REGISTERED_AGENTS                                     │
│    ┌─────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────┐ ┌────────┐│
│    │SignalAgent  │ │ScalpingAgent │ │FundingArbAgt │ │XChainArb │ │Balance ││
│    │(wraps Bot)  │ │              │ │              │ │   Agt    │ │  Agt   ││
│    └─────────────┘ └──────────────┘ └──────────────┘ └──────────┘ └────────┘│
└────┬───────────────┬───────────────────┬───────────────┬─────────┬──────────┘
     │               │                   │               │         │
     ▼               ▼                   ▼               ▼         ▼
┌─────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────┐ ┌─────────────┐
│ 3. Signal   │ │ Scalp loop   │ │ Funding loop │ │ XChain   │ │ Balance     │
│  track:     │ │ (OFI primary)│ │ (Phase 1 obs)│ │ loop     │ │ planner     │
│ SignalEngine│ │              │ │              │ │          │ │ + rails     │
│  ↓          │ │              │ │              │ │          │ │             │
│ QualityGate │ │              │ │              │ │          │ │             │
│  ↓          │ │              │ │              │ │          │ │             │
│ ClaudeAgent │ │              │ │              │ │          │ │             │
│  ↓          │ │              │ │              │ │          │ │             │
│ OrderRouter │ │              │ │              │ │          │ │             │
└──────┬──────┘ └──────┬───────┘ └──────┬───────┘ └────┬─────┘ └─────┬───────┘
       │               │                │              │             │
       ▼               ▼                ▼              ▼             ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 4. Execution                                                                │
│    execution/router.py    execution/arb_engine.py    funding_engine.py      │
│    crosschain_engine.py   execution/kill_switch.py   position_manager.py    │
│    execution/inventory.py mexc_key_router.py         chains/*.py            │
└────┬─────────────────────┬─────────────────────────────────┬────────────────┘
     │                     │                                 │
     ▼                     ▼                                 ▼
┌─────────────┐  ┌────────────────────────┐  ┌────────────────────────────────┐
│ 5. Market   │  │ 6. Inputs              │  │ 7. Persistence                 │
│  data       │  │ data_sources/* (13)    │  │ database/* — SQLite + SA 2.x   │
│ core/market │  │ sentiment/sources (5)  │  │ 21 ORM models, 91 query fns    │
│  _data.py   │  │ macro/sources/* (2)    │  │ WAL mode + FK enforce          │
│ ccxt + WS   │  │ data_sources/__init__  │  │ No Alembic — init_db creates   │
│             │  │  refresh loop + subs   │  │  missing tables; never ALTERs  │
└─────────────┘  └────────────────────────┘  └────────────────────────────────┘
```

### Signal track flow (the heart of `core/bot.py`)

```
       ┌──────────────────┐
       │  MarketData      │  ccxt fetches + (planned) ccxt.pro WS streams
       └────────┬─────────┘
                │ OHLCV + order book
                ▼
       ┌──────────────────┐    Strategy.should_run_{arb,momentum,reversion}
       │  SignalEngine    │◄── decides which scanners run
       └────────┬─────────┘
                │ raw Signal
                ▼
       ┌──────────────────┐    regime_modifier + session_modifier +
       │  QualityGate     │    guard_modifier + OFI + sentiment_mod
       └────────┬─────────┘    score = clamp(raw + mods, 0, 100)
                │ gated Signal (passed_gate=True)
                ▼
       ┌──────────────────┐    Anthropic SDK call;
       │  ClaudeAgent     │    parses GO/SKIP + entry/SL/TP
       └────────┬─────────┘
                │
                ▼
       ┌──────────────────┐    per_trade → queue for keyboard / web approval
       │  _route_for_     │    window     → execute if window open + rate ok
       │   approval       │    autonomous → execute if rate ok
       └────────┬─────────┘
                │ approved
                ▼
       ┌──────────────────┐    sim → _sim_execute writes trades row
       │  OrderRouter     │    live → _live_execute (currently STUB returns None)
       └────────┬─────────┘
                │
                ▼
       ┌──────────────────┐
       │  Database        │    Signal + Trade + 1:1 relationships
       └──────────────────┘
```

### Module-level singletons

These are the shared-state hand-offs. Use by import, not by re-instantiation:

| Singleton | Defined in | Used by |
|-----------|------------|---------|
| `agent` (ClaudeAgent) | `core/agent.py` | `core/bot.py` |
| `profile_manager` | `profiles/profile_manager.py` | `main.py`, `core/bot.py`, agents |
| `quality_gate` | `signals/quality_gate.py` | `core/bot.py`, `signals/engine.py`, agents |
| `regime_detector` | `core/regime_detector.py` | `core/bot.py`, `agents/scalping_agent.py` |
| `guard_runner` | `core/guards.py` | `core/bot.py` |
| `ofi_scorer` | `signals/ofi.py` | `signals/{momentum,reversion}.py`, `quality_gate.py` |
| `sentiment` | `sentiment/aggregator.py` | `signals/quality_gate.py` |
| `data_sources` | `data_sources/__init__.py` | `macro/monitor.py`, several agents |
| `macro_monitor` | `macro/monitor.py` | `core/bot.py`, agents |

---

## Directory map

Annotated tour of the tracked tree (excluding `venv/`, `.git/`, `__pycache__/`, log/backup/cache/db files):

```
cryptobot/
├── CLAUDE.md, README.md, RUNBOOK.md, GLOSSARY.md,
│   PLUGIN_PATTERN.md, WEB_UI.md          docs for humans + Claude Code
├── main.py                               CLI entry (click) + async _run + shutdown
├── run.sh                                4-line "balanced + default" launcher
├── pytest.ini                            testpaths = tests
├── requirements.txt                      33 direct deps (deeply drifted vs venv)
├── agents/                               Coordinator + 5 agents + balance subtree
│   ├── balance/                          inventory_state, planner, policy/, rails/
│   └── ... (top-level agents)
├── config/                               settings.py (1269 LOC, single tuning instrument)
├── core/                                 Bot loop, market data, regime, guards, Claude agent
├── data_sources/                         Pluggable data feeds (13 sources)
│   └── sources/
├── database/                             SQLAlchemy 2.x models + queries
├── execution/                            Router, arb, funding, xchain, kill, MEXC routing
│   └── chains/                           Web3 chain connectors (Arbitrum, Base, Optimism)
├── macro/                                Macro regime monitor + calendar sources
│   └── sources/
├── predictive/                           EMPTY STUB — __init__.py only
├── profiles/                             Profile dataclass + 4 JSON profiles
├── prompts/                              25 Claude Code build briefs (24 + 1 empty)
├── scalping_v2/                          Reference bundle — docs + duplicated .py
├── scripts/                              4 operator tools (probe, migrate, recalibrate, watch)
├── sentiment/                            Plugin sentiment aggregator (5 sources)
│   └── sources/
├── signals/                              Signal dataclass, engine, scanners, OFI, QualityGate
├── strategies/                           BaseStrategy + 4 registered strategies
├── tests/                                30 test files; pytest scope
├── ui/                                   Rich terminal + aiohttp web control panel
└── utils/                                Logger + Hurst exponent
```

Live state (untracked / gitignored) sitting alongside the tracked tree:

```
backups/cryptobot_20260528_225642.db      a recent DB snapshot
config/keys.env                           secrets (NEVER read by this audit)
config/keys.env.pre-restore.20260528-211120  pre-restore backup of keys.env
config/settings.py.backup                 a pre-rewire settings backup
data/cryptobot.db (+ -wal, -shm)          live DB; 271,769 total rows across 21 tables
data/cryptobot.db.pre_fix_backup          older backup
data/cryptobot.db.pre_wiring_backup       older backup
logs/cryptobot.log (+ 9 rotated days)     active logs
logs/smoke_test_stdout.log                ad-hoc capture
```

---

## Module-by-module reports

The next sections inline each `module_reports/*.md` produced in Phase 2. They are reproduced verbatim — read them as the canonical per-package surface.

---


## Module-by-module reports


---

# Module Report: core

## Purpose
The `core/` package is the central nervous system of CryptoBot. It owns the main async trading loop (`CryptoBot` in `bot.py`), the exchange-facing market data layer (REST + WebSocket streaming via `ccxt.pro`), the regime classifier that labels every pair/timeframe as TRENDING/RANGING/HIGH_VOL/CHOPPY, the three protective signal guards (BTC crash, position correlation, news), and the Claude agent that turns raw signals into GO/SKIP recommendations with entry/SL/TP suggestions. It is the orchestration layer between market data inputs (ccxt, sentiment, macro, data_sources), the signal pipeline (`signals/*`), and execution (`execution/router`, `execution/position_manager`, `execution/kill_switch`).

## Files
| File | LOC | One-sentence summary |
| --- | --- | --- |
| core/__init__.py | 1 | Empty package marker. |
| core/agent.py | 188 | Claude agent — builds markdown briefs, calls Anthropic SDK, parses GO/SKIP + numeric suggestions, exposes a module-level `agent` singleton. |
| core/bot.py | 864 | `CryptoBot` async orchestrator: main `_cycle`, circuit breaker state, approval-mode routing (per_trade/window/autonomous), background loops for heartbeat / position watcher / self-review / future price tracker / VIX subscriber. |
| core/guards.py | 290 | Three signal guards (BTC crash, correlation, news) with a `GuardRunner` aggregator and module-level `guard_runner` singleton. |
| core/market_data.py | 652 | Exchange connections via ccxt.pro: REST history fetch, WebSocket candle + order-book streaming (with per-symbol loops and sharded connections), candle indicator computation, scalp-v2 accessors (mid/EMA/ATR/VWAP/volume/order book). |
| core/regime_detector.py | 335 | Per-(pair,timeframe) regime classification using ADX, Hurst, ATR percentile and BB width; emits `RegimeSnapshot` with strategy-activation flags and score modifiers. |

## Public surface

### core/__init__.py
**Docstring:** none

**Classes:** none.

**Functions:** none.

**Module constants:** none.

### core/agent.py
**Docstring:** `core/agent.py — Claude agent — evaluates signals and generates trade recommendations.`

**Classes:**
- `ClaudeAgent` — wraps the Anthropic SDK; produces and parses the per-signal evaluation prompt and a post-trade self-review.
  - `__init__(self)` — instantiates `anthropic.Anthropic` client from `ANTHROPIC_API_KEY` env var; zeros daily cost and call counters.
  - `async evaluate_signal(self, signal: Signal, regime_snap=None, ofi_snap=None) -> Signal` — synchronously calls `client.messages.create` inside the async method, parses recommendation + numeric fields, sets `signal.claude_api_cost`, increments daily cost.
  - `_build_brief(self, signal: Signal, regime_snap=None, ofi_snap=None) -> str` — composes a markdown analyst brief with signal, indicators, timeframe checks, regime, OFI, sentiment, optional historical performance, task instructions and profile footer.
  - `_parse_response(self, signal: Signal, text: str) -> Signal` — sets `signal.claude_reasoning`; regex-extracts `RECOMMENDATION: GO/SKIP` into `signal.indicators["claude_rec"]`; regex-extracts entry/stop loss/take profit numbers if not already populated.
  - `_estimate_cost(self, response) -> float` — uses `response.usage.input_tokens` / `output_tokens` with hardcoded `0.000003` and `0.000015` USD/token rates (Sonnet); falls back to `0.001` on any error.
  - `async self_review(self, trade) -> str` — gated by `settings.SELF_REVIEW_ENABLED`; sends a brief WIN/LOSS post-mortem prompt to Claude with `SELF_REVIEW_MAX_TOKENS`.
  - `daily_cost: float` (property) — read-only accessor for accumulated daily cost.
  - `reset_daily_cost(self)` — zeros `_daily_cost` and `_call_count`.

**Functions:** none (module-level).

**Module constants:**
- `logger = logging.getLogger(__name__)`
- `agent = ClaudeAgent()` — module-level singleton instantiated at import (will call `anthropic.Anthropic(...)` even with no API key, returning a client that errors only on first request).

### core/bot.py
**Docstring:** Multi-line: describes the main `_cycle` loop running every `BOT_LOOP_INTERVAL_SEC`, the five background loops (`_cycle_loop`, `_heartbeat_loop`, `_position_watcher_loop`, `_self_review_loop`, `_future_price_tracker_loop`) launched via `asyncio.gather`, and lists the public API (`start`, `stop`, `trigger_kill_switch`, `approve_window`, `record_trade_result`).

**Classes:**
- `CircuitBreakerState` — tracks daily P&L, consecutive losses, drawdown, peak equity; the authoritative kill source for the bot's main cycle.
  - `__init__(self, starting_equity: Optional[float] = None)` — resolves starting equity in order: explicit arg → `settings.STARTING_CAPITAL` → `sum(settings.EXCHANGE_BALANCES.values())`. Initialises `daily_pnl_pct=0.0`, `consecutive_losses=0`, `current_equity`, `daily_peak_equity`, `last_reset_date`, `halted=False`, `halt_reason=None`, `halt_at=None`.
  - `drawdown_pct -> float` (property) — `(current_equity - daily_peak_equity) / daily_peak_equity * 100`; zero when peak is non-positive.
  - `reset_if_new_day(self)` — when UTC date rolls over, zero `daily_pnl_pct` and re-seed `daily_peak_equity` from current equity.
  - `update_after_trade(self, pnl_pct: float)` — pnl_pct as fraction; updates `daily_pnl_pct`, consecutive-loss streak, current_equity, peak.
  - `evaluate(self) -> tuple[bool, str]` — checks each of `daily_loss`, `consecutive_loss`, `drawdown` rules from `settings.CIRCUIT_BREAKERS`; returns `(should_halt, reason)`.
  - `halt(self, reason: str)` — sets halted flag, reason, and `halt_at = datetime.utcnow()`.

- `CryptoBot` — async trading bot orchestrator. Constructor accepts injectable collaborators for testability.
  - `__init__(self, profile=None, strategy=None, kill_switch: Optional[KillSwitch] = None, market_data: Optional[MarketData] = None, signal_engine: Optional[SignalEngine] = None, position_mgr: Optional[PositionManager] = None, router: Optional[OrderRouter] = None)` — lazy-resolves defaults from `profile_manager`, `strategies.get_strategy`, `KillSwitch`, `MarketData`, `SignalEngine`, `PositionManager`, `OrderRouter`. Calls `signal_engine.setup_scanners()` if present. Imports `sentiment` singleton. Seeds `_cb_state` from `db_queries.get_last_equity()`. Sets up window-mode and autonomous-mode rate-limit lists, `_pending_signals: asyncio.Queue`, closed-trade count, BTC 30m snapshot tracker, command-bar state, pause flag. Wires `signal_engine.on_signal(self._on_signal)` and subscribes `_on_vix_crisis` to `data_sources.subscribe("fred.vix", ...)` with a `DATA_VIX_CRISIS_MIN` filter.
  - `async start(self)` — sets `_running=True`, calls `init_db()`, logs mode/profile/strategy/approval, starts `ApprovalInputHandler` if stdin is a tty, launches `data_sources.run_refresh_loop` and `macro_monitor.run_refresh_loop` lazily, then `await asyncio.gather(market_data.start(), _cycle_loop(), _heartbeat_loop(), _position_watcher_loop(), _self_review_loop(), _future_price_tracker_loop(), data_sources_loop, macro_loop, return_exceptions=True)`.
  - `_on_vix_crisis(self, new_point, prev_point) -> None` — fires CRITICAL log when VIX clears `DATA_VIX_CRISIS_MIN`.
  - `async stop(self)` — flags `_running=False`, awaits `market_data.stop()`.
  - `async trigger_kill_switch(self, reason: str = "manual") -> dict` — engages the kill switch then halts circuit breaker.
  - `approve_window(self, duration_minutes: int) -> datetime` — clamps to `[WINDOW_MIN_DURATION_MINUTES, WINDOW_MAX_DURATION_MINUTES]`, sets `_window_until`.
  - `async approve_next_pending(self) -> bool` — drains one pending signal and calls `_execute_signal`; returns False when queue empty.
  - `async skip_next_pending(self, reason: str = "user_skipped") -> bool` — drains one pending signal and records it skipped.
  - `async shutdown(self, reason: str = "user_quit") -> None` — idempotent; writes final portfolio snapshot, logs `SHUTDOWN` agent_event when `SHUTDOWN_LOG_EVENT`, stops market_data.
  - `peek_pending(self)` — returns next pending signal without removing it; reaches into `asyncio.Queue._queue` deque internal.
  - `record_trade_result(self, pnl_pct: float)` — resets day, updates state, evaluates CB; on first trip logs to `db_queries.log_circuit_breaker` and critically logs.
  - `async _cycle_loop(self)` — `while self._running: try _cycle except log; await asyncio.sleep(BOT_LOOP_INTERVAL_SEC)`.
  - `toggle_pause(self) -> bool` — flips `_paused`.
  - `async _cycle(self)` — pause guard → reset_if_new_day → dead-zone guard → CB re-evaluation → sentiment session_floor (best-effort) → macro hard block / pre-event pause → `signal_engine.run_scan()`.
  - `async _on_signal(self, signal)` — fetches regime + OFI snapshots, calls `agent.evaluate_signal`, takes price snapshot, persists Claude verdict via `db_queries.update_signal_claude`, dispatches to `_route_for_approval`.
  - `async _route_for_approval(self, signal, price_at_signal: Optional[float])` — branches on `settings.APPROVAL_MODE`: claude `SKIP` short-circuits; `autonomous` checks rate limit then executes; `window` checks window open + rate limit; default (`per_trade`) queues to `_pending_signals`.
  - `_record_skip(self, signal, reason: str, price_at_signal: Optional[float])` — calls `db_queries.update_signal_skip` and logs.
  - `async _execute_signal(self, signal)` — refuses when halted; calls `router.execute(signal, profile)`; appends timestamps to auto/window rate-limit buffers; logs.
  - `_window_open(self) -> bool` — `_window_until is not None and now < _window_until`.
  - `_window_rate_limit_ok(self) -> bool` — caps via `settings.WINDOW_MAX_TRADES_PER_HOUR`.
  - `_auto_rate_limit_ok(self) -> bool` — enforces `AUTO_MAX_TRADES_PER_HOUR` and `AUTO_MAX_TRADES_PER_DAY`.
  - `async _heartbeat_loop(self)` — sleeps `HEARTBEAT_INTERVAL_SEC`; refreshes sentiment, updates `guard_runner.btc_guard.update_price`, calls `sentiment.set_btc_change_30m` on mature snapshot.
  - `async _position_watcher_loop(self)` — sleeps `POSITION_WATCHER_INTERVAL_SEC`; calls `position_mgr.check_positions()`; per closed trade pulls the row and calls `record_trade_result(trade.pnl_pct)`.
  - `async _self_review_loop(self)` — sleeps 60s; gated by `SELF_REVIEW_ENABLED`; every `SELF_REVIEW_EVERY_N_TRADES` closes calls `agent.self_review` and persists via `db_queries.save_postmortem`.
  - `async _future_price_tracker_loop(self)` — sleeps `FUTURE_PRICE_TRACKER_INTERVAL_SEC`; fills `price_1h`/`price_4h`/`price_24h` columns on past signals via `db_queries.update_signal_future_prices`.
  - `_in_dead_zone(self) -> bool` — compares UTC HH:MM against `settings.SESSION_WINDOWS["dead_zone"]`.
  - `_price_for(self, signal) -> Optional[float]` — prefers `signal.suggested_entry`, falls back to `_price_for_pair`.
  - `_price_for_pair(self, pair: str, exchange: Optional[str] = None) -> Optional[float]` — best-effort via `market_data.get_price` then `get_all_prices`.
  - `_btc_price_with_fallback(self) -> Optional[float]` — priority: market_data → `data_sources.cryptocompare.get_price("BTC")` → `data_sources.bybit_derivs.get_mark_price("BTC/USDT")` → None.
  - `_update_btc_snapshot(self, current_price: float) -> Optional[float]` — discrete 30m tracker; returns None on first call or before `(BTC_GUARD_LOOKBACK_MINUTES - BTC_GUARD_LOOKBACK_DRIFT_MINUTES)` mins old; returns percent delta and replaces snapshot otherwise.
  - `_get_trade_by_id(self, trade_id: int)` — delegates to `db_queries.get_trade_by_id`.
  - `async _fetch_sentiment(self) -> dict` — pulls `sentiment.get_current()`; translates aggregator's -100..+100 to 0..100; returns `{"MARKET": {"composite", "velocity", "fear_greed", "hard_block"}}`.

**Functions:** none (module-level).

**Module constants:**
- `logger = logging.getLogger(__name__)`

### core/guards.py
**Docstring:** `core/guards.py — Three protective guards that run on every signal before it reaches Claude. BTC Guard / Correlation Guard / News Guard.`

**Classes:**
- `BTCGuard` — detects BTC crash and penalises alt signals.
  - `__init__(self)` — empty `_price_history: list[tuple[float, float]]`, `_triggered=False`, `_trigger_time: Optional[float]=None`.
  - `update_price(self, price: float)` — appends `(now, price)` to history, trims to `settings.BTC_CRASH_WINDOW_MINUTES`, calls `_check`.
  - `_check(self)` — flips `_triggered` when drop `>= settings.BTC_CRASH_PCT`; records trigger time on transition.
  - `apply(self, signal: Signal) -> float` — returns 0 when disabled / not triggered / pair contains BTC and is arb; otherwise `-settings.BTC_GUARD_SCORE_PENALTY`.
  - `is_triggered: bool` (property).
  - `change_pct_30m(self) -> Optional[float]` — percent change between oldest and newest price in window; consumed by sentiment aggregator.
  - `status(self) -> str` — `"⚠ TRIGGERED Nm ago"` or `"OK"`.

- `CorrelationGuard` — blocks/penalises over-correlated exposure against open positions.
  - `apply(self, signal: Signal, open_positions: list) -> float` — returns 0 when disabled / arb-exempt / no positions; reads `_KNOWN_HIGH_CORR`; returns `-999.0` above `settings.CORR_BLOCK_THRESHOLD`, `-settings.CORR_PENALTY_AMOUNT` above `CORR_HIGH_THRESHOLD`, else 0.
  - `update_correlations(self, correlation_matrix: dict)` — mutates the module-level `_KNOWN_HIGH_CORR` dict in place.

- `NewsGuard` — scans recent headlines for block/warn keywords.
  - `__init__(self)` — empty `_events: list[dict]`, `_last_scan=0.0`.
  - `ingest(self, headline: str, timestamp: Optional[float] = None)` — case-insensitive scan against `settings.NEWS_GUARD_BLOCK_KEYWORDS` then `NEWS_GUARD_WARN_KEYWORDS`; stores `{"text", "ts", "level"}`.
  - `apply(self, signal: Signal) -> float` — within `NEWS_GUARD_LOOKBACK_MINUTES`, returns `-NEWS_GUARD_PENALTY_BLOCK` if any block event, `-NEWS_GUARD_PENALTY_WARN` if any warn, else 0.
  - `active_events(self) -> list[dict]` — events within lookback.
  - `is_clear(self) -> bool` — `not active_events()`.
  - `status(self) -> str` — formatted summary of block/warn counts.
  - `purge_old(self)` — drops events older than 2× lookback.

- `GuardRunner` — applies all three guards.
  - `__init__(self)` — instantiates `btc_guard: BTCGuard`, `corr_guard: CorrelationGuard`, `news_guard: NewsGuard`.
  - `apply_all(self, signal: Signal, open_positions: list) -> tuple[float, list[str]]` — sums penalties; returns `(total, reasons)`. `total <= -999` indicates block.
  - `status_summary(self) -> dict` — `{"btc_guard": ..., "news_guard": ...}`.

**Functions:** none.

**Module constants:**
- `logger = logging.getLogger(__name__)`
- `_KNOWN_HIGH_CORR: dict[frozenset, float]` — hardcoded static correlation table for 9 pair-pairs (ETH/SOL=0.88, ETH/AVAX=0.85, ETH/BNB=0.82, BTC/ETH=0.80, SOL/AVAX=0.83, BTC/SOL=0.78, MATIC/ETH=0.81, DOT/ETH=0.79, LINK/ETH=0.77).
- `guard_runner = GuardRunner()` — module-level singleton.

### core/market_data.py
**Docstring:** `core/market_data.py — Exchange connections, WebSocket streaming, and candle management.`

**Classes:**
- `MarketData` — main exchange manager; owns ccxt clients, candle DataFrames, last prices, last books, short-window price history.
  - `PRICE_HISTORY_WINDOW_SEC = 300` (class attr) — sample buffer size used by `get_change_pct` and `get_mid_price_at_offset`.
  - `__init__(self, dashboard=None)` — dicts: `_exchanges`, `_candles` (keyed by `(exchange, pair, tf)`), `_last_price` (keyed by `(exchange, pair)`), `_last_book`, `_price_history` (deques); lists: `_callbacks`, `_book_callbacks`, `_ob_conns`, `_active_pairs`; `_running=False`; optional `_dashboard`.
  - `set_dashboard(self, dashboard) -> None` — late-binds dashboard.
  - `_report_health(self, exchange: str, latency_ms: float, connected: bool) -> None` — best-effort push to dashboard.
  - `on_candle_close(self, fn)` — registers candle-close callback.
  - `on_book_update(self, fn)` — registers `fn(exchange, pair, bids, asks)` callback fired on every book tick.
  - `get_candles(self, exchange, pair, timeframe)` — returns DataFrame or None.
  - `get_latest_candle(self, exchange, pair, timeframe)` — last row or None.
  - `get_price(self, exchange, pair)` — last cached close, or None.
  - `get_all_prices(self, pair)` — `{exchange: price}` across all venues for a pair.
  - `get_book(self, exchange, pair)` — most recent ccxt-shape book, or None.
  - `get_spread_bps(self, exchange, pair)` — top-of-book spread in bps; None on missing/malformed book.
  - `get_change_pct(self, exchange, pair, window_sec)` — % change from current mid vs the sample nearest `window_sec` ago; None when insufficient history.
  - `_record_price_sample(self, exchange, pair, mid_price)` — append to bounded per-pair sample deque.
  - `get_mid_price(self, symbol, exchange)` — top-of-book mid, falls back to last price.
  - `get_mid_price_at_offset(self, symbol, exchange, offset_ms)` — mid `offset_ms` ago from sample buffer.
  - `_candle_tf(self, exchange, symbol, preferred)` — returns preferred tf if cached, else fastest from `settings.TIMEFRAMES`.
  - `_last_finite(series)` (static) — last non-NaN float or None.
  - `get_session_vwap(self, symbol, exchange)` — latest VWAP from fastest candle frame.
  - `get_ema(self, symbol, exchange, timeframe, period)` — computes `ta.trend.EMAIndicator(period)` on close; None on missing frame.
  - `get_atr(self, symbol, exchange, period, timeframe)` — computes `ta.volatility.AverageTrueRange(period)`; None on missing frame.
  - `get_current_minute_volume(self, symbol, exchange)` — latest candle volume from fastest available frame (prefers `"1m"`).
  - `get_rolling_median_volume(self, symbol, exchange, timeframe, lookback)` — median of last `lookback` volumes.
  - `get_order_book(self, symbol, exchange, levels=5)` — returns `OrderBook(bids=[OBLevel(price,size)], asks=[…])`; None on missing book.
  - `active_pairs(self)` — `_active_pairs` list.
  - `async start(self)` — boots every `settings.ENABLED_EXCHANGES` via `_make_exchange`; calls `load_markets`; resolves pairs; loads history; runs `_stream_candles` + `_stream_orderbooks` per exchange via `asyncio.gather`.
  - `async stop(self)` — flags `_running=False`; closes every primary client and every `_ob_conns` shard.
  - `async _resolve_pairs(self)` — if `PAIR_UNIVERSE == "manual"` returns `FALLBACK_PAIRS`; else uses first exchange's `fetch_tickers`, sorts by quoteVolume, takes top `PAIR_UNIVERSE_TOP_N`.
  - `async _load_history(self)` — fetches OHLCV for first 20 active pairs × all timeframes via `asyncio.gather`.
  - `async _fetch_candles(self, exchange_name, ex, pair, tf)` — pulls history, builds DataFrame, runs `_compute_indicators`, seeds `_last_price`, feeds `regime_detector` for every row.
  - `_update_regime(self, pair, tf, row)` — wraps `regime_detector.update(...)` with None-safe column reads.
  - `async _stream_candles(self, exchange_name, ex)` — gated on `ex.has["watchOHLCV"]`; launches `_stream_one_candle` for first `ORDER_BOOK_STREAM_PAIRS` × `TIMEFRAMES`.
  - `async _stream_one_candle(self, exchange_name, ex, pair, tf)` — awaits `watch_ohlcv` with `ORDER_BOOK_WATCH_TIMEOUT_S` timeout; reports health; backs off on errors via `ORDER_BOOK_ERROR_BACKOFF_S`.
  - `async _process_candle(self, exchange_name, pair, tf, raw)` — updates `_last_price`, appends/upserts row in DataFrame (trims to `CANDLE_LOOKBACK + 50`), recomputes indicators, updates regime, fires `_callbacks`.
  - `async _stream_orderbooks(self, exchange_name, ex)` — gated on `ex.has["watchOrderBook"]`; resolves pairs via `_orderbook_pairs_for`; if pairs > `ORDER_BOOK_MAX_STREAMS_PER_CONN` shards across dedicated clients.
  - `_orderbook_pairs_for(self, exchange_name, ex)` — base = `_active_pairs[:ORDER_BOOK_STREAM_PAIRS]`; for scalp venues from `settings.STRATEGY_EXCHANGE_MAP["scalp"]` also adds `settings.SCALP_PAIRS`; filters to listed markets.
  - `async _stream_orderbook_shard(self, exchange_name, pairs)` — creates dedicated ccxt.pro client, tracks in `_ob_conns`, calls `load_markets`, runs per-symbol `_stream_one_orderbook`.
  - `async _stream_one_orderbook(self, exchange_name, ex, pair)` — awaits `watch_order_book(pair, ORDER_BOOK_DEPTH)`; persists book, samples mid into history deque, calls `ofi_scorer.update_book`, fans tick to `_book_callbacks`; on `NotSupported` stops the symbol's loop; on other errors backs off.

**Functions:**
- `_make_exchange(name)` — builds a ccxt.pro client with per-exchange config (Binance public-only with `fetchCurrencies=False` + `adjustForTimeDifference` + `recvWindow=10000`; Kraken; Bybit `defaultType=spot`; OKX with passphrase); always sets `enableRateLimit=True`.
- `_compute_indicators(df)` — uses `ta.*` to add RSI (`RSI_PERIOD`), MACD (`MACD_FAST/SLOW/SIGNAL`), Bollinger (`BB_PERIOD/STDDEV`), EMA fast/slow/trend (`EMA_FAST/SLOW/TREND`), ATR (window 14, hardcoded), ADX (window 14, hardcoded), volume SMA (window 20, hardcoded), VWAP with fallback to 20-period close rolling mean.

**Module constants:**
- `logger = logging.getLogger(__name__)`
- `OBLevel = namedtuple("OBLevel", ["price", "size"])`
- `OrderBook = namedtuple("OrderBook", ["bids", "asks"])`

### core/regime_detector.py
**Docstring:** `core/regime_detector.py — Classifies the current market regime for every pair on every timeframe. Runs continuously — updates on each candle close. Regimes: TRENDING / RANGING / HIGH_VOL / CHOPPY.`

**Classes:**
- `RegimeSnapshot` (dataclass) — full regime picture for one pair at one timeframe.
  - Fields: `pair: str`, `timeframe: str`, `timestamp: datetime`, `regime: str = UNKNOWN`, `adx: Optional[float] = None`, `atr: Optional[float] = None`, `atr_percentile: Optional[float] = None`, `bb_width: Optional[float] = None`, `bb_width_avg: Optional[float] = None`, `hurst: Optional[float] = None`, `hurst_label: str = "unknown"`, `is_trending/is_ranging/is_high_vol/is_choppy: bool = False`, `momentum_ok/reversion_ok/grid_ok/sweep_ok: bool = False`, `arb_ok: bool = True`, `score_modifier: float = 0.0`.
  - `summary(self) -> str` — formatted single-line summary.

- `RegimeDetector` — keeps regime state for every `(pair, timeframe)`.
  - `__init__(self)` — `_regimes: dict[tuple, RegimeSnapshot]`, `_hurst: dict[tuple, RollingHurst]`, `_atr_history: dict[tuple, list[float]]`, `_bb_width_history: dict[tuple, list[float]]`.
  - `update(self, pair: str, timeframe: str, close: float, high: float, low: float, adx: Optional[float], atr: Optional[float], bb_upper: Optional[float], bb_lower: Optional[float], bb_mid: Optional[float]) -> RegimeSnapshot` — lazily allocates `RollingHurst(lookback=settings.HURST_LOOKBACK_BARS)`, appends ATR (capped at `settings.ATR_PERCENTILE_LOOKBACK`), computes BB width and rolling mean (capped at 50), classifies, computes strategy-OK flags and score modifier, caches and returns snapshot.
  - `get(self, pair: str, timeframe: str) -> Optional[RegimeSnapshot]` — keyed lookup.
  - `get_primary(self, pair: str) -> Optional[RegimeSnapshot]` — regime on `settings.SLOW_TIMEFRAME`.
  - `all_regimes(self) -> list[RegimeSnapshot]` — values.
  - `pairs_by_regime(self, regime: str) -> list[str]` — unique pairs in regime on slow TF.
  - `_classify(self, adx, atr_pct, hurst, bb_width, bb_width_avg) -> str` — order of precedence: HIGH_VOL when `atr_pct >= ATR_HIGH_VOL_PERCENTILE`, CHOPPY when `adx < ADX_CHOPPY_MAX`, TRENDING when ADX strong + Hurst persistent (or ADX strong + Hurst absent), RANGING when ADX weak, fallback to Hurst-based call, else UNKNOWN.
  - `_momentum_ok(self, regime, adx, hurst) -> bool` — only TRENDING/HIGH_VOL; respects `MOMENTUM_REQUIRE_HURST` and `MOMENTUM_MIN_ADX`.
  - `_reversion_ok(self, regime, adx, hurst) -> bool` — blocks TRENDING/CHOPPY/UNKNOWN; respects `REVERSION_REQUIRE_HURST` and `REVERSION_MAX_ADX`.
  - `_grid_ok(self, regime, adx) -> bool` — only when `GRID_ENABLED` and `ADX_GRID_MIN <= adx <= ADX_GRID_MAX`.
  - `_score_modifier(self, regime, adx, hurst, atr_pct) -> float` — returns `-30.0` for CHOPPY, `+5.0` for TRENDING (`+5.0` extra above `ADX_STRONG_TREND`), `+3.0` for HIGH_VOL; Hurst bonuses: `+5.0` if `>=0.60`, `+3.0` if `<=0.42`, `-5.0` if `0.47 <= hurst <= 0.53`.

**Functions:** none (module-level).

**Module constants:**
- `logger = logging.getLogger(__name__)`
- `TRENDING = "trending"`
- `RANGING = "ranging"`
- `HIGH_VOL = "high_vol"`
- `CHOPPY = "choppy"`
- `UNKNOWN = "unknown"`
- `regime_detector = RegimeDetector()` — module-level singleton.

## Imports graph

### This package imports from elsewhere in the project
- From `config`: `settings`
- From `signals.base`: `Signal`
- From `signals.ofi`: `ofi_scorer`
- From `database.queries`: `get_signal_history`, `get_signal_win_rate`, `save_candle`, and via `from database import queries as db_queries` in `bot.py` (used: `get_last_equity`, `update_signal_claude`, `update_signal_skip`, `log_circuit_breaker`, `log_portfolio_snapshot`, `log_agent_event`, `get_recent_closed_trades`, `save_postmortem`, `get_signals_needing_price_update`, `update_signal_future_prices`, `get_trade_by_id`)
- From `database.db`: `init_db`
- From `execution.kill_switch`: `KillSwitch`
- From `execution.position_manager`: `PositionManager`
- From `execution.router`: `OrderRouter`
- From `signals.engine`: `SignalEngine`
- From `utils.hurst`: `RollingHurst`
- Lazy imports inside `core/bot.py` (deferred to avoid early boot / circular import): `from profiles.profile_manager import profile_manager`, `from strategies import get_strategy`, `from sentiment import sentiment`, `from data_sources import data_sources`, `from macro import macro_monitor`, `from ui.prompts import ApprovalInputHandler`.
- `core/market_data.py` lazy: imports `core.regime_detector.regime_detector` (in-package).

### This package is imported by
- `core/market_data.py` (intra-package: imports `core.regime_detector`)
- `core/bot.py` (intra-package: imports `core.agent`, `core.guards`, `core.market_data`, `core.regime_detector`)
- `signals/momentum.py` — imports `core.regime_detector.regime_detector`
- `signals/reversion.py` — imports `core.regime_detector.regime_detector`
- `signals/quality_gate.py` — imports `core.regime_detector.regime_detector`, `CHOPPY`, and `core.guards.guard_runner`
- `ui/web_server.py` — imports `core.regime_detector` (line 1215, runtime)
- `ui/dashboard.py` — imports `core.regime_detector.regime_detector` (lines 512, 703)
- `agents/__init__.py` — imports `core.bot.CryptoBot` (line 96)
- `agents/scalping_agent.py` — imports `core.regime_detector` as `_rd` (line 768)
- `tests/test_bot.py` — imports `core.bot.CryptoBot`, `CircuitBreakerState`
- `tests/test_market_data_stream.py` — imports `core.market_data.MarketData`
- `tests/test_scalp_v2_accessors.py` — imports `core.market_data.MarketData`, `OrderBook`, `OBLevel`
- `tests/test_scalping_agent.py` — imports `core.market_data.MarketData` (multiple test setups)

## Plugin registrations
None. The `core/` package does not define or register any `REGISTERED_*` list. It exposes three module-level singletons (`core.agent.agent`, `core.guards.guard_runner`, `core.regime_detector.regime_detector`) that other modules import directly.

## Tests
- `tests/test_bot.py` — imports `CryptoBot`, `CircuitBreakerState`.
  - `test_peek_pending_returns_none_when_empty` — `peek_pending()` on empty queue returns None.
  - `test_peek_pending_returns_front_without_consuming` — peek twice does not drain the queue.
  - `test_cb_state_trips_on_daily_loss` — `-2.5%` trade trips `daily_loss` rule.
  - `test_cb_state_trips_on_consecutive_losses` — three tiny losses trip streak rule.
  - `test_cb_state_resets_streak_on_win` — winning trade resets `consecutive_losses` to 0.
  - `test_cycle_skips_dead_zone` — `_cycle` at 03:00 UTC skips `run_scan`.
  - `test_cycle_runs_outside_dead_zone` — `_cycle` at 13:00 UTC calls `run_scan` once.
  - `test_record_trade_result_halts_on_daily_loss` — `-3%` triggers halt and logs CB row.
  - `test_cycle_skips_when_halted` — halted CB causes `_cycle` to skip `run_scan`.
  - `test_per_trade_queues_signal` — per_trade mode queues, does not execute.
  - `test_autonomous_executes_within_limits` — autonomous mode executes immediately.
  - `test_autonomous_skip_when_rate_limited` — saturated hourly cap prevents execution.
  - `test_window_open_executes` — window open + within rate limit executes.
  - `test_window_closed_queues` — window closed queues the signal.
  - `test_claude_skip_short_circuits` — `claude_rec == "SKIP"` blocks execution.
  - `test_btc_snapshot_first_call_stores_returns_none` — first call seeds tracker, returns None.
  - `test_btc_snapshot_immature_window_returns_none` — pre-maturity call returns None and keeps snapshot.
  - `test_btc_snapshot_mature_window_emits_delta` — mature window returns ~`-2%` and resets snapshot.
  - `test_btc_price_fallback_uses_data_sources_when_market_data_empty` — falls back to `data_sources.cryptocompare`.
  - `test_btc_price_fallback_returns_none_when_no_source` — all sources empty → None, no crash.
  - `test_startup_reads_last_equity_from_db` — seeds `current_equity` from `get_last_equity`.
  - `test_startup_uses_starting_capital_when_db_empty` — falls through to `STARTING_CAPITAL`.
  - `test_capital_allocation_matches_settings` — `SIGNAL_AGENT_CAPITAL`/`ARB_AGENT_CAPITAL` propagate to agent wrappers.
  - `test_clean_shutdown_writes_final_snapshot` — `shutdown` writes a portfolio snapshot with `SHUTDOWN` status.
  - `test_clean_shutdown_logs_agent_event` — `shutdown` logs `("portfolio", "SHUTDOWN", reason)`.
  - `test_shutdown_is_idempotent` — second `shutdown` is a no-op.
  - `test_approve_next_pending_drains_queue_and_executes` — `a` command path drains and executes.
  - `test_approve_next_pending_when_empty_is_noop` — empty queue returns False.
  - `test_skip_next_pending_records_skip` — `skip_next_pending` calls `update_signal_skip`.
  - `test_toggle_pause_flips_flag_and_returns_state` — `toggle_pause` flips `_paused`.
  - `test_paused_cycle_skips_scan` — paused state causes `_cycle` to skip scan.
- `tests/test_market_data_stream.py` — imports `MarketData`.
  - `test_stream_one_orderbook_processes_then_backs_off` — processes book, feeds OFI, backs off on error (no spin).
  - `test_stream_one_orderbook_fires_book_callbacks` — registered `on_book_update` subscribers receive every tick.
  - `test_orderbook_pairs_for_scalp_venue_streams_full_universe` — scalp venue streams the full `SCALP_PAIRS` (filtered to listed markets); signal venue gets only top-N.
  - `test_stream_orderbooks_shards_over_cap` — over per-conn cap, sharding creates dedicated clients (10 pairs ÷ 4/conn = 3 shards).
  - `test_stream_one_orderbook_skips_when_not_running` — non-running state skips immediately.
  - `test_stream_orderbooks_noop_without_watch` — REST-only ccxt client returns without raising.
  - `test_stream_one_candle_processes_then_backs_off` — candle stream processes bar, backs off on error.
  - `test_stream_candles_noop_without_watch` — REST-only ccxt client returns without raising.
- `tests/test_scalp_v2_accessors.py` — imports `MarketData`, `OrderBook`, `OBLevel`.
  - `test_get_mid_price_from_book` — mid from top-of-book.
  - `test_get_mid_price_falls_back_to_last_price` — last_price fallback when no book.
  - `test_get_order_book_shape` — returns `OrderBook` of `OBLevel` per side.
  - `test_get_ema_and_atr_from_candles` — EMA and ATR computed from cached DataFrame.
  - `test_get_vwap_and_volume` — VWAP + minute-volume + rolling-median accessors.
  - `test_get_mid_price_at_offset` — mid from sample buffer at offset.
  - `test_ofi_get_z_score_none_until_bucket_closes` — `OFIEngine` test (scalp agent module, not core).
  - `test_ofi_get_exchanges_for_symbol` — `OFIEngine` test (scalp agent module, not core).
- `tests/test_scalping_agent.py` — imports `MarketData` in five test setups (uses it as a stub for the scalp agent's market data needs; tests are not asserting `core` behaviour).

## TODOs / FIXMEs / stubs
- `core/bot.py:604` — `# TODO: ui/prompts.py is a stub; until it exists, per_trade signals only land in the queue and never auto-execute.`
- `core/bot.py:677` — `# TODO: recompute equity from market_data + open positions and feed self._cb_state.current_equity so drawdown stays accurate.`

No FIXME, XXX, HACK, NotImplementedError or `pass  #` markers found anywhere in the package. The comment in `bot.py:604` is partly out of date — `ui/prompts.py` is actually present and used (see `from ui.prompts import ApprovalInputHandler` at `bot.py:267`), and `approve_next_pending` / `skip_next_pending` drain the queue. The TODO text predates that wiring.

## Known issues observed

- `core/agent.py` — `agent = ClaudeAgent()` runs at import time, which calls `anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))`. If the env var is missing the SDK accepts None and only fails at first request, but it still couples module import to the SDK being installed. There is no test stub for this singleton; tests that import `core.bot` transitively run this.
- `core/agent.py:153` — Per-token cost rates (`0.000003` input, `0.000015` output, i.e. Claude 3.5 Sonnet) are hardcoded and would silently drift if `settings.CLAUDE_MODEL` is changed to a different model. The CLAUDE.md invariant says "no magic numbers outside settings.py" — these should live in `settings.CLAUDE_COST_PER_INPUT_TOKEN` etc.
- `core/agent.py:128` — `import re` happens inside `_parse_response` on every call instead of at module top. Minor; the import is cached but the lookup runs each time.
- `core/agent.py:104-105`, `core/agent.py:133-146`, `core/agent.py:150-155`, `core/agent.py:176-178` — multiple bare `except:` / `except Exception:` clauses that swallow all errors silently. The catch in `evaluate_signal` (line 43) sets `claude_reasoning` but does not set `indicators["claude_rec"]`, so a failed Claude call lands as the default — i.e. the signal goes into "UNCLEAR" status only if `_parse_response` ran. After an exception, `claude_rec` is never set at all and `_route_for_approval` reads `(signal.indicators or {}).get("claude_rec", "")`, treating the missing key as "neither GO nor SKIP" → still executes on autonomous/window. Failure-mode is therefore "trade through" rather than "skip on uncertainty".
- `core/agent.py:65` — `if signal.bb_position is not None:` uses `is not None` but other indicators above (`signal.rsi`, `signal.volume_ratio`, `signal.arb_gap_pct`, `signal.macd_hist`) use truthiness — so a zero RSI or zero arb gap is silently dropped from the brief.
- `core/bot.py:418-427` — `peek_pending` reaches into `asyncio.Queue._queue` private attr; flagged in its own comment but is a CPython-internal coupling.
- `core/bot.py:677` (TODO) — `_heartbeat_loop` never recomputes equity from live market data, so `_cb_state.current_equity` stays exactly at the post-last-trade value forever between trades. This means `drawdown_pct` only reflects realised P&L, not unrealised mark-to-market drawdown.
- `core/bot.py:599-600` — Window-closed signals are pushed to `_pending_signals` but never explicitly drained when the window opens; if the user runs `approve_window` while signals already sit in the queue, those signals will not auto-execute (they wait for an `a` keystroke).
- `core/bot.py` mode dispatch — `_route_for_approval` only knows the three approval modes by string literal (`"autonomous"`, `"window"`, default = per_trade). There is no validation of `settings.APPROVAL_MODE`, so a typo silently degrades to per_trade.
- `core/guards.py:102-112` — `_KNOWN_HIGH_CORR` is a hardcoded static table. `CorrelationGuard.update_correlations` mutates this module-level dict (not the instance), so updates persist across the module's lifetime but are not scoped to the guard instance. Also, the table is keyed by `frozenset([pair_a, pair_b])` of exact USDT-quoted strings — does not handle `BTC/USD`, perp suffixes, or any non-USDT quote.
- `core/guards.py:67` — The BTC guard exemption is `"BTC" in signal.pair and signal.signal_type == "arb"`. This matches `WBTC`, `BTCUP`, `1000BTCSHIB`, etc. — the substring check is too loose. Should be a proper symbol/base check.
- `core/guards.py:135-149` — Correlation guard only reads `_KNOWN_HIGH_CORR`; pairs missing from the table are treated as 0.0 correlation. No fallback to a computed correlation from price history.
- `core/guards.py:175-193` — `NewsGuard.ingest` does substring matching in `headline.lower()`. With keywords like `"hack"` this will match `"Hackathon"` headlines. There is no anchor/word-boundary check.
- `core/guards.py:236-239` — `NewsGuard.purge_old` uses 2× the lookback as its expiry — there is no scheduler that calls it; the dispatcher relies on `apply` filtering. Without a periodic call the `_events` list grows unbounded over a long run.
- `core/market_data.py:67-68` — Hardcoded `window=14` for ATR and ADX in `_compute_indicators`; everywhere else uses `settings.*_PERIOD` constants. Violates the "no magic numbers" CLAUDE.md invariant.
- `core/market_data.py:69-73` — `volume_sma` window 20 is hardcoded; VWAP rolling fallback uses 20 again; both should be settings constants.
- `core/market_data.py:418` — `_load_history` hardcodes `self._active_pairs[:20]`; ignores `ORDER_BOOK_STREAM_PAIRS` or any setting. So if the bot is configured to monitor 96 scalp pairs, only the first 20 ever get historical OHLCV preloaded — the rest must wait until the WS stream fills in.
- `core/market_data.py:519-521` — In `_process_candle`, the candle DataFrame is trimmed only when a new bar arrives (`new_row.index[0] not in df.index` branch); in the upsert branch the DataFrame is never re-trimmed. Slow-growing key edge but bounded.
- `core/market_data.py:639-640` — On `asyncio.TimeoutError` the orderbook stream just `continue`s without backing off; this means a perma-stalled WS that lets `wait_for` time out cleanly will spin without any delay. The error path does back off, but timeouts do not.
- `core/market_data.py:386-390` — `start` calls `asyncio.gather(*tasks, return_exceptions=True)` for all streams. There is no supervisor loop — if a per-symbol streamer exits permanently (e.g. `NotSupported` at `_stream_one_orderbook`), nothing relaunches it. The bot continues without book data for that symbol silently except the warning log.
- `core/regime_detector.py:73-79` — `RegimeSnapshot.summary` uses f-strings that put a conditional expression *inside* the format spec (e.g. `f"ADX={self.adx:.1f if self.adx else '?'}"`). This is invalid: the part after `:` is a format-spec, not a Python expression. Calling `summary()` raises `ValueError: Invalid format specifier`. It is currently invoked at `core/regime_detector.py:197` as `logger.debug(snap.summary())` — at default INFO level this is suppressed and the bug stays hidden; flipping the bot to DEBUG would surface it on every candle close.
- `core/regime_detector.py:131-132` — `_atr_history` and `_bb_width_history` use `list.pop(0)` for ring trim, which is O(n). Should be a `collections.deque(maxlen=…)`.
- `core/regime_detector.py:122-124` — `_hurst[key].update(close)` is called even when `close` is the same bar's update; there is no de-duplication. The `regime_detector.update` is called once per fetched candle row in `_load_history` (line 438 in market_data) and once per closed candle in `_process_candle`, so on history backfill the rolling Hurst sees the same series of closes both ways. Acceptable but worth noting.
- `core/regime_detector.py:298-331` — `_score_modifier` returns `-30.0` for CHOPPY, but `BTCGuard.apply` returns `-settings.BTC_GUARD_SCORE_PENALTY`, etc. — all penalty magnitudes are inconsistent across the codebase (some hardcoded constants here, some settings constants in guards). Should be unified through `settings`.
- `core/__init__.py` — Empty (just `# core`), so consumers must always import the submodule directly (e.g. `from core.bot import CryptoBot`). No re-exports of the three singletons (`agent`, `guard_runner`, `regime_detector`). Not necessarily a bug but increases coupling to internal module paths.

---

# Module Report: agents

## Purpose
The `agents/` package is the project's trading-agent layer: it defines the `BaseAgent` ABC, an agent-agnostic `Coordinator` that orchestrates a roster of registered agents, six concrete agents (Signal/Arb wrappers around the existing CryptoBot/ArbEngine plus Scalping, Cross-Chain Arb, Funding-Rate Arb, and Balance), and the BalanceAgent's internal `balance/` sub-tree (ledger state, planner, policy plugins, and transfer rails). Agents share state through module-level singletons, are picked up automatically through `REGISTERED_AGENTS`, and must satisfy a small async lifecycle contract (`start`, `stop`, `get_stats`, `close_all_positions`).

## Subpackages
- `agents/balance/` — fund-aware ledger view (`InventoryState`), the rebalance `Planner` (Greedy/Miller-Orr), and the policy + rails plugin sub-trees consumed by `BalanceAgent`.
- `agents/balance/policy/` — `BasePolicy` contract, `InventoryTarget` dataclass, and the default `GrowthOptimalPolicy` (fractional Kelly blended with risk-parity).
- `agents/balance/rails/` — `BaseTransferRail` + `TransferResult` contract and two rails: `SimTransferRail` (atomic with simulated fee/delay/failure) and `CexTransferRail` (live-stubbed full state machine).

## Files
| File | LOC | One-sentence summary |
| --- | --- | --- |
| agents/__init__.py | 416 | Concrete `SignalAgentWrapper` + `ArbAgentWrapper` plus the `REGISTERED_AGENTS` roster the Coordinator iterates. |
| agents/base.py | 231 | `AgentStats` dataclass, lifecycle constants (`RUNNING/PAUSED/HALTED/OFFLINE/STOPPED`), `BaseAgent` ABC with BalanceAgent compounding hooks, and `PlaceholderAgent`. |
| agents/coordinator.py | 379 | `Coordinator` — starts agents concurrently, runs per-fund + portfolio circuit breakers, propagates dashboard, exposes kill-all/stats. |
| agents/balance_agent.py | 765 | The `BalanceAgent` operational fund-controller: realised-P&L compounding, policy/planner/rails dispatch, six safety rails, web-UI arm/confirm two-step. |
| agents/crosschain_agent.py | 232 | Thin wrapper over `execution.crosschain_engine.CrossChainArbEngine`; observation-mode by default and exposes `get_inventory_targets()`. |
| agents/funding_arb_agent.py | 400 | Funding-rate arb Phase-1 observation agent wrapping `execution.funding_engine`. |
| agents/scalping_agent.py | 1779 | OFI-driven scalping agent — `FeeManager`, multi-level `OFIEngine`, 13 entry gates + v2 selectivity layer, sim ordering, micro-price tracker. |
| agents/scalping_agent_v2_integration.py | 246 | Reference/demo module documenting the v2 integration and offering `evaluate_signal_v2()` + `is_ready_for_live_v2()` helpers — not wired into production. |
| agents/scalping_atr_sl.py | 109 | `ATRStopCalculator` + `TpSlV2` — volatility-aware TP/SL with floor/ceiling clamps. |
| agents/scalping_confluence.py | 410 | `ConfluenceChecker` (VWAP/HTF/Volume/CrossEx/BTC/Adverse/Depth gates) and result dataclasses for scalp v2. |
| agents/balance/__init__.py | 19 | Re-exports `InventoryState` and the `inventory_state` singleton. |
| agents/balance/inventory_state.py | 358 | Fund-aware ledger view; `InventoryState` with `effective_balance`, `can_arb`, in-flight transfer + paused-route tracking. |
| agents/balance/planner.py | 358 | `BaseRebalancePlanner` ABC + default `GreedyNetPlanner` (internalize → net → Miller-Orr band → greedy match); `Transfer` + `PlannerConstraints` dataclasses. |
| agents/balance/policy/__init__.py | 36 | Re-exports `BasePolicy`, `InventoryTarget`, and `REGISTERED_POLICIES` (`[GrowthOptimalPolicy()]`). |
| agents/balance/policy/base.py | 75 | `BasePolicy` ABC and `InventoryTarget` dataclass. |
| agents/balance/policy/growth_optimal.py | 269 | `GrowthOptimalPolicy` — fractional Kelly × risk-parity blend; emits one `InventoryTarget` per `(fund, exchange, USDT)`. |
| agents/balance/rails/__init__.py | 48 | Re-exports `BaseTransferRail`, `TransferResult`, and `REGISTERED_RAILS` (`[SimTransferRail(), CexTransferRail()]`). |
| agents/balance/rails/base.py | 63 | `BaseTransferRail` ABC and `TransferResult` dataclass. |
| agents/balance/rails/cex_rail.py | 287 | `CexTransferRail` — full pending→in_transit→completed/failed state machine; `ccxt.withdraw` stubbed behind `REBALANCE_LIVE_ENABLED`. |
| agents/balance/rails/sim_rail.py | 146 | `SimTransferRail` — atomic sim transfers with simulated fee, delay, and injectable failure. |

## Public surface

### agents/__init__.py
**Docstring:** Registry of trading agents the Coordinator manages by default; documents the plugin pattern for adding new agents and the rationale for keeping concrete wrappers out of the Coordinator.

**Classes:**
- `SignalAgentWrapper(BaseAgent)` — wraps the existing `core.bot.CryptoBot`; primary (non-optional) signal agent.
  - class attrs: `agent_id: str = "signal"`, `display_name: str = "Signal Agent"`, `optional: bool = False`
  - `__init__(self)` — pulls `settings.SIGNAL_AGENT_CAPITAL`; bot and dashboard set lazily.
  - `bot` (property) — exposes the underlying `CryptoBot`.
  - `set_dashboard(self, dashboard) -> None`
  - `is_available(self) -> bool` — True in sim mode; in live mode requires at least one `<EX>_API_KEY` env var across `ENABLED_EXCHANGES`.
  - `async start(self) -> None` — instantiates `CryptoBot(kill_switch=KillSwitch(sim_mode=SIM_MODE))` and runs `_bot.start()`.
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None` — triggers the bot's kill switch.
  - `get_open_position_notional(self) -> float`
  - `set_capital_allocation(self, amount: float) -> bool` — propagates to `OrderRouter.update_portfolio_value` if available.
  - `async get_stats(self) -> AgentStats` — DB-derived from `signal` strategy trades; falls back to in-memory CB state when DB unreachable.
- `ArbAgentWrapper(BaseAgent)` — wraps `execution.arb_engine.ArbEngine`.
  - class attrs: `agent_id: str = "arb"`, `display_name: str = "Arb Agent"`, `optional: bool = True`
  - `__init__(self)` — pulls `settings.ARB_AGENT_CAPITAL`.
  - `is_available(self) -> bool` — needs `ArbEngine` importable and ≥2 exchanges (sim) or ≥2 keyed exchanges (live).
  - `set_dashboard(self, dashboard) -> None`
  - `async start(self) -> None` — boots `ArbEngine(dashboard=..., fund_id="arb", exchanges=STRATEGY_EXCHANGE_MAP["arb"])` and reconstructs persisted P&L.
  - `_reconstruct_engine_pnl(engine) -> None` (staticmethod)
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None`
  - `get_open_position_notional(self) -> float` — approximates as `active_arbs × ARB_BASE_POSITION_USD`.
  - `set_capital_allocation(self, amount: float) -> bool`
  - `async get_stats(self) -> AgentStats`

**Functions:** none.

**Module constants:**
- `REGISTERED_AGENTS: list[BaseAgent] = [SignalAgentWrapper(), ArbAgentWrapper(), ScalpingAgent(), CrossChainArbAgent(), FundingArbAgent(), BalanceAgent()]`
- `__all__` lists the wrappers + concrete agents + dataclasses.

### agents/base.py
**Docstring:** Defines the two contracts every trading agent satisfies (`AgentStats`, `BaseAgent`) plus `PlaceholderAgent`. Mirrors the BaseSentimentSource plugin pattern.

**Classes:**
- `AgentStats` — `@dataclass` capturing one agent's snapshot.
  - fields: `agent_id: str`, `status: str`, `capital_allocated: float`, `capital_deployed: float`, `daily_pnl: float`, `daily_pnl_pct: float`, `total_pnl: float`, `trades_today: int`, `win_rate_today: float`, `win_rate_alltime: float`, `consecutive_losses: int`, `last_trade_time: Optional[str]`, `error: Optional[str]`.
- `BaseAgent(ABC)` — every concrete trading agent.
  - class attrs: `agent_id: str = "base"`, `display_name: str = "Base Agent"`, `capital_allocation: float = 0.0`, `optional: bool = True`
  - `__init__(self)` — initialises `_status=OFFLINE`, `_start_time=None`, `_error=None`.
  - `@abstractmethod async start(self) -> None`
  - `@abstractmethod async stop(self) -> None`
  - `@abstractmethod async get_stats(self) -> AgentStats`
  - `@abstractmethod async close_all_positions(self) -> None`
  - `is_available(self) -> bool` — default True.
  - `get_capital_allocation(self) -> float`
  - `set_capital_allocation(self, amount: float) -> bool` — refuses below `get_open_position_notional`.
  - `get_open_position_notional(self) -> float` — default 0.0.
  - `async pause(self) -> None`
  - `async resume(self) -> None`
  - `status` (property) -> `str`
  - `uptime_seconds` (property) -> `float`
- `PlaceholderAgent(BaseAgent)` — base for dashboard-only agents.
  - class attrs: `optional: bool = True`
  - `is_available(self) -> bool` — always False.
  - `async start(self) -> None`
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None`
  - `async get_stats(self) -> AgentStats` — zeroed OFFLINE stats.

**Functions:** none.

**Module constants:** lifecycle strings — `RUNNING = "RUNNING"`, `PAUSED = "PAUSED"`, `HALTED = "HALTED"`, `OFFLINE = "OFFLINE"`, `STOPPED = "STOPPED"`.

### agents/coordinator.py
**Docstring:** Multi-agent coordinator. Agent-agnostic by design — only the `BaseAgent` contract. Starts agents concurrently, aggregates portfolio stats, runs portfolio-wide CBs, and persists snapshots/events.

**Classes:**
- `Coordinator` — orchestrates a roster of `BaseAgent` instances.
  - `__init__(self, agents: Optional[list[BaseAgent]] = None, dashboard=None)` — defaults to `REGISTERED_AGENTS` (lazy import); calls `_check_capital_sum`.
  - `async start(self) -> None` — kicks each available agent + the monitor loop concurrently.
  - `async stop(self) -> None`
  - `async kill_all(self, reason: str = "manual") -> dict`
  - `async get_agent_stats(self) -> list[AgentStats]` — sorted by capital descending.
  - `async get_portfolio_stats(self) -> dict` — aggregates equity, daily PnL%, exposure %, weighted win rate, portfolio status.
  - `get_agent(self, agent_id: str) -> Optional[BaseAgent]`
  - `get_primary_bot(self)` — returns `signal` agent's `bot`.
  - `set_dashboard(self, dashboard) -> None`
  - `async _monitor_loop(self) -> None`
  - `async _check_fund_circuit_breakers(self, agent_stats: list[AgentStats]) -> None` — per-fund halt at `-FUND_DAILY_LOSS_HALT_PCT`.
  - `async _check_portfolio_circuit_breakers(self, stats: dict) -> None` — global halt at `-PORTFOLIO_DAILY_LOSS_HALT_PCT`.
  - `async _safe_call(self, agent: BaseAgent, method_name: str) -> bool`
  - `async _safe_close(self, agent: BaseAgent) -> bool`
  - `async _safe_pause(self, agent: BaseAgent) -> bool`
  - `async _safe_get_stats(self, agent: BaseAgent) -> AgentStats`
  - `_check_capital_sum(self) -> None`
  - `_log_event(self, agent_id: str, event_type: str, detail: str) -> None`
  - `_log_snapshot(self, stats: dict) -> None`
  - `async _propagate_dashboard(self) -> None`

**Functions:** none.

**Module constants:** none.

### agents/balance_agent.py
**Docstring:** The `BalanceAgent` — operational (no alpha). Owns fund × exchange capital position; each scan refreshes per-fund equity, compounds via `set_capital_allocation`, asks the active policy for targets, hands to the planner, dispatches via rails, and logs efficiency rows. Documents the seven safety rails and the kill-switch contract (block new transfers; in-flight settles).

**Classes:**
- `BalanceAgent(BaseAgent)` — operational fund controller.
  - class attrs: `agent_id: str = "balance"`, `display_name: str = "Balance Agent"`, `optional: bool = True`
  - `__init__(self, *, policies: Optional[list[BasePolicy]] = None, rails: Optional[list[BaseTransferRail]] = None, planner: Optional[BaseRebalancePlanner] = None)` — pulls `BALANCE_AGENT_CAPITAL`, defaults to `REGISTERED_POLICIES`, `REGISTERED_RAILS`, `GreedyNetPlanner()`.
  - `is_available(self) -> bool` — always True.
  - `async start(self) -> None` — kicks `_loop` task and runs cex_rail `load_in_transit` reconciliation.
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None` — sets `_paused=True`.
  - `async get_stats(self) -> AgentStats` — `daily_pnl`/`total_pnl` always 0; reuses `trades_today`/`consecutive_losses` to count dispatched / failed transfers.
  - `arm(self) -> tuple[str, dict]` — issues a `secrets.token_hex(8)` token + ring-fence notice; valid for `REBALANCE_CONFIRM_WINDOW_S`.
  - `consume_arm(self, token: str) -> bool` — fail-closed token check.
  - `_ring_fence_notice(self) -> dict`
  - `get_pending_proposal(self) -> dict` — web-UI snapshot of the latest buffered plan.
  - `async execute_proposal(self, confirm_token: str) -> dict` — re-plans on confirm; documented error codes include `kill_blocked`, `token_invalid`, `token_expired`, `live_rebalance_disabled`, `no_pending_plan`, `plan_changed`, `replan_failed:<err>`, `all_blocked_by_safety`.
  - `_plans_substantially_equal(old: list, new: list, tol: float = 0.10) -> bool` (staticmethod)
  - `async _loop(self) -> None`
  - `async _scan_once(self) -> None`
  - `_compound_realised_into_funds(self) -> float`
  - `_safety_clear(self, t: Transfer) -> bool` — rail 2/3 (open-position floor / strict block).
  - `async _dispatch(self, t: Transfer) -> Optional[int]`
  - `_pick_policy(self) -> Optional[BasePolicy]`
  - `_pick_rail(self, t: Transfer) -> Optional[BaseTransferRail]`
  - `_count_in_flight(self) -> int`
  - `_cost_matrix() -> dict` (staticmethod)
  - `_find_sibling(agent_id: str) -> Optional[BaseAgent]` (staticmethod)
  - `_find_sibling_for_fund(self, fund_id: str) -> Optional[BaseAgent]`
  - `_check_daily_reset(self) -> None`

**Functions:** none.

**Module constants:** none.

### agents/crosschain_agent.py
**Docstring:** Thin `BaseAgent` wrapper over `execution.crosschain_engine.CrossChainArbEngine`. Documents the observation-mode invariant (`XCHAIN_CAPITAL == 0`) and how `get_inventory_targets()` exposes the future BalanceAgent interface.

**Classes:**
- `CrossChainArbAgent(BaseAgent)`
  - class attrs: `agent_id: str = "xchain"`, `display_name: str = "Cross-Chain Arb"`, `optional: bool = True`
  - `__init__(self)` — `capital_allocation = float(settings.XCHAIN_CAPITAL)`.
  - `is_available(self) -> bool` — requires ≥2 available connectors from `execution.chains.REGISTERED_CONNECTORS`.
  - `observation_mode` (property) -> bool — True when `XCHAIN_CAPITAL <= 0`.
  - `set_dashboard(self, dashboard) -> None`
  - `async start(self) -> None`
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None`
  - `async get_stats(self) -> AgentStats`
  - `_zero_stats(self, *, status: str, error: Optional[str] = None) -> AgentStats`
  - `get_inventory_targets(self, current_balances: Optional[dict[str, dict[str, float]]] = None, *, lookback_observations: int = 500) -> list[InventoryTarget]` — reads `db_queries.get_xchain_observations` and delegates to `execution.inventory.compute_inventory_targets`.

**Functions:** none.

**Module constants:** `__all__ = ["CrossChainArbAgent"]`.

### agents/funding_arb_agent.py
**Docstring:** Funding-rate arb agent (Phase 1 observation-mode only). Routes nothing while `FUNDING_OBSERVATION_MODE`; uses Binance public CCXT.

**Classes:**
- `FundingArbAgent(BaseAgent)`
  - class attrs: `agent_id: str = "funding_arb"`, `display_name: str = "Funding-Rate Arb"`, `optional: bool = True`
  - `__init__(self, engine: Optional[FundingEngine] = None)` — pulls `FUNDING_CAPITAL_USD`; default engine is the module-level `funding_engine` singleton.
  - `is_available(self) -> bool` — always True.
  - `observation_mode` (property) -> bool — reads `settings.FUNDING_OBSERVATION_MODE`.
  - `async start(self) -> None`
  - `async stop(self) -> None` — also closes the engine's binance ccxt client.
  - `async close_all_positions(self) -> None`
  - `async get_stats(self) -> AgentStats`
  - `get_observation_summary(self) -> dict`
  - `async _loop(self) -> None`
  - `async _manage_open_positions(self) -> None`
  - `async _close(self, symbol: str, pos: FundingPosition, reason: str) -> None`
  - `async _log_observation(self, opp: FundingOpportunity) -> None`
  - `_check_daily_reset(self) -> None`
  - `_check_circuit_breakers(self) -> None`

**Functions:** none.

**Module constants:** module logger `log = logging.getLogger(__name__)`.

### agents/scalping_agent.py
**Docstring:** OFI-primary scalping agent; documents exchange routing, fee-aware TP/SL formula, observation mode, live-wiring expectations for market_data accessors, and Phase 2 dashboard TODO.

**Classes:**
- `_LazyMarketData` (`__slots__ = ("_resolve",)`) — proxy that re-resolves the agent's `MarketData` on every attribute access so the v2 checkers (built in `__init__`) reach the live feed.
  - `__init__(self, resolver)`
  - `__getattr__(self, name)`
- `BookSnap` — `@dataclass` with `ts: float`, `bids: list`, `asks: list`.
- `ScalpPosition` — `@dataclass` carrying entry/exit metadata and `trade_id: Optional[int] = None`.
- `ScalpObservation` — `@dataclass` recording every evaluation (entry or skip) plus v2 selectivity diagnostics (confluence_score, strength_label, cross_exchange_agrees, btc_compatible, adverse_selection_ok, depth_ok, vwap_aligned, htf_aligned, volume_adequate, atr_bps, atr_adjusted, sl_clamped, rr_actual — all `Optional` so older rows stay valid).
- `FeeManager` — caches per-(exchange,symbol) maker/taker fees, applies overrides, and derives dynamic TP/SL + breakeven win rate.
  - `__init__(self, overrides: dict)`
  - `async load_exchange(self, exchange_id: str, ccxt_exchange: Any) -> None`
  - `get_fees(self, exchange_id: str, symbol: str) -> dict`
  - `round_trip_bps(self, exchange_id: str, symbol: str) -> float`
  - `compute_tp_sl(self, exchange_id: str, symbol: str) -> tuple[float, float]`
  - `breakeven_win_rate(self, exchange_id: str, symbol: str, tp_bps: float, sl_bps: float) -> float`
  - `is_viable(self, exchange_id: str, symbol: str) -> tuple[bool, str]`
- `OFIEngine` — Cont-Kukanov-Stoikov multi-level OFI estimator keyed by `(symbol, exchange)`.
  - `__init__(self, levels: int, window_sec: float, zscore_window: int)`
  - `_key(symbol: str, exchange: str) -> str` (staticmethod)
  - `_maybe_close_bucket(self, key: str, now: float) -> None`
  - `_compute_e_n(self, prev: BookSnap, curr: BookSnap) -> float`
  - `on_book(self, symbol: str, exchange: str, bids: list, asks: list) -> None`
  - `on_trade(self, symbol: str, exchange: str, side: str, qty: float) -> None`
  - `get(self, symbol: str, exchange: str) -> dict` — `{z, direction, strength, tfi_confirms, raw_tfi, age_sec, stale}`.
  - `update_direction_ticks(self, symbol: str, exchange: str, entry_z: float) -> int`
  - `get_z_score(self, symbol: str, exchange: str)` — Optional[float].
  - `get_exchanges_for_symbol(self, symbol: str) -> list`
- `ScalpingAgent(BaseAgent)` — main scalper.
  - class attrs: `agent_id: str = "scalp"`, `display_name: str = "Scalping Agent (OFI)"`, `optional: bool = True`
  - `__init__(self, sentiment_source: Optional[Any] = None, market_data: Optional[Any] = None, regime_detector: Optional[Any] = None)`
  - `is_available(self) -> bool`
  - `get_capital_allocation(self) -> float` — `max(capital_allocation, _capital)`.
  - `set_capital_allocation(self, amount: float) -> bool`
  - `get_open_position_notional(self) -> float`
  - `set_market_data(self, market_data) -> None`
  - `set_regime_detector(self, regime_detector) -> None`
  - `set_sentiment_source(self, sentiment_source) -> None`
  - `_resolve_market_data(self)`
  - `_ensure_book_subscription(self, md) -> None`
  - `_resolve_regime_detector(self)`
  - `_reconstruct_pnl(self) -> None`
  - `async start(self) -> None`
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None`
  - `_win_rate_today(self) -> float`
  - `_win_rate_alltime(self) -> float`
  - `async get_stats(self) -> AgentStats`
  - `on_book(self, symbol: str, exchange: str, bids: list, asks: list) -> None`
  - `on_trade(self, symbol: str, exchange: str, side: str, qty: float) -> None`
  - `async _get_mid_price(self, symbol: str, exchange: str) -> float`
  - `async _get_spread_bps(self, symbol: str, exchange: str) -> float`
  - `async _get_regime(self, symbol: str) -> str`
  - `async _get_ccxt_exchange(self, exchange_id: str, symbol: Optional[str] = None)` — MEXC routes through `mexc_key_router`.
  - `async _get_btc_1m_change(self) -> float`
  - `async _place_order(self, pos: "ScalpPosition") -> Optional[int]` — sim path persists a Trade row.
  - `async _loop(self) -> None`
  - `_pos_key(symbol: str, exchange: str) -> str` (staticmethod)
  - `async _micro_price_tracker_loop(self) -> None`
  - `async _micro_price_tracker_pass(self) -> None`
  - `async _evaluate_entry(self, symbol: str, exchange: str) -> None` — runs the 13 gates plus v2 selectivity (when `SCALP_USE_CONFLUENCE`) and ATR-aware TP/SL.
  - `_unpack_confluence(conf) -> dict` (staticmethod)
  - `_annotate_v2(obs, fields: dict, tpsl=None) -> None` (staticmethod)
  - `_log_skip(self, symbol: str, exchange: str, ts: float, ofi: Optional[dict], reason: str, rt_bps: float, min_wr: float, spread_bps: float, regime: str) -> None`
  - `_make_observation(self, symbol: str, exchange: str, ts: float, ofi: Optional[dict], would_entry: bool, skip_reason: str, entry_price: float, tp_bps: float, sl_bps: float, rt_bps: float, min_wr: float, spread_bps: float, regime: str) -> ScalpObservation`
  - `async _manage_position(self, pos_key: str) -> None`
  - `async _exit_position(self, pos_key: str, pos: ScalpPosition, reason: str, exit_price: float = 0.0) -> None`
  - `_halt(self, reason: str) -> None`
  - `_check_daily_reset(self) -> None`
  - `async _flush_observations(self) -> None`
  - `get_observation_summary(self) -> dict`

**Functions:** none.

**Module constants:** logger `log = logging.getLogger(__name__)`.

### agents/scalping_agent_v2_integration.py
**Docstring:** Documents the additive v2 integration with the 13-gate flow, the DB columns needed, and the deployment recipe. Not imported in production aside from the `is_ready_for_live_v2()` helper used by tests.

**Classes:** none.

**Functions:**
- `evaluate_signal_v2(agent, signal, confluence_checker, atr_calc) -> Optional[Dict[str, Any]]` — reference replacement for `_evaluate_signal`.
- `_initial_observation(signal) -> Dict[str, Any]`
- `_run_legacy_gates(agent, signal, obs) -> Optional[Dict[str, Any]]`
- `_compute_position_size_usd(agent, signal) -> float`
- `is_ready_for_live_v2(stats: Dict[str, Any], settings) -> Dict[str, Any]` — activation gate using `SCALP_*_FOR_LIVE_V2` thresholds (min observations 300, min win_rate 0.55, min avg_net_bps 0.5, max_hold_pct 0.25, min directional_acc_1m 0.57).

**Module constants:**
- `DB_COLUMNS_V2: str` — multi-line `ALTER TABLE` statements documenting the v2 columns.

### agents/scalping_atr_sl.py
**Docstring:** ATR-aware stop loss calculator replacing the fixed-bps SL with `max(base_sl, ATR×multiplier)` clamped to `[floor, ceiling]`.

**Classes:**
- `TpSlV2` — `@dataclass` returning `tp_bps`, `sl_bps`, `rr_actual`, `base_sl_bps`, `atr_bps`, `atr_adjusted`, `sl_clamped`.
- `ATRStopCalculator`
  - `__init__(self, market_data, settings)`
  - `compute_tp_sl_v2(self, symbol: str, exchange: str, round_trip_bps: float) -> TpSlV2`
  - `_safe_atr_bps(self, symbol: str, exchange: str)` — returns `Optional[float]` (bps of mid).

**Functions:** none.

**Module constants:** logger `logger = logging.getLogger("scalping_v2.atr_sl")` (note: logger name uses the `scalping_v2.` prefix even when imported from `agents.`).

### agents/scalping_confluence.py
**Docstring:** Stateless confluence/selectivity gates (VWAP, HTF EMA, Volume, Cross-Exchange OFI, BTC directional, Adverse selection, Depth) wrapped in `ConfluenceChecker` for DI.

**Classes:**
- `ConfluenceResult` — `@dataclass` (`passed`, `reason`, `score`, `gate_name`, `metadata`).
  - `__str__(self)` — `[✓ NAME] reason (score=X.XX)`.
- `CombinedConfluenceResult` — `@dataclass` carrying overall verdict, `confluence_score`, `strength_label`, per-gate booleans, `individual_results`, `metadata`.
- `ConfluenceChecker`
  - `__init__(self, market_data, ofi_engine, settings)`
  - `check_vwap_alignment(self, symbol: str, exchange: str, direction: str) -> ConfluenceResult`
  - `check_htf_trend(self, symbol: str, exchange: str, direction: str) -> ConfluenceResult`
  - `check_volume(self, symbol: str, exchange: str) -> ConfluenceResult`
  - `check_cross_exchange_ofi(self, symbol: str, primary_exchange: str, direction: str, primary_z: float) -> ConfluenceResult`
  - `check_btc_directional(self, symbol: str, exchange: str, direction: str) -> ConfluenceResult`
  - `check_adverse_selection(self, symbol: str, exchange: str, direction: str) -> ConfluenceResult`
  - `check_depth(self, symbol: str, exchange: str, position_size_usd: float) -> ConfluenceResult`
  - `run_all_gates(self, symbol: str, exchange: str, direction: str, primary_z: float, position_size_usd: float) -> CombinedConfluenceResult`
  - `_build_combined(self, passed, reason, results, adverse, depth, btc, cross, soft_passed=0) -> CombinedConfluenceResult`
  - `_label_strength(soft_passed: int, cross: Optional[ConfluenceResult]) -> str` (staticmethod) — WEAK / MODERATE / STRONG / VERY_STRONG.

**Functions:** none.

**Module constants:** logger `logger = logging.getLogger("scalping_v2.confluence")`.

### agents/balance/__init__.py
**Docstring:** Documents the four strictly-separated layers — policy → planner → rails → state — and the plugin pattern.

**Classes:** none defined here (re-exports only).

**Functions:** none.

**Module constants:** `__all__ = ["InventoryState", "inventory_state"]`.

### agents/balance/inventory_state.py
**Docstring:** Fund-aware ledger view. Documents why per-fund claims are needed on shared venues, the two-band `can_arb` gate, and the singleton pattern.

**Classes:**
- `_PendingTransfer` — `@dataclass` with `movement_id, from_fund, to_fund, from_exchange: Optional[str], to_exchange: Optional[str], amount_usd, asset, state, opened_at: float = field(default_factory=time.time)`.
- `InventoryState` — singleton.
  - `__init__(self)` — initialises `_lock=RLock()`, `_claims`, `_allocations`, `_transfers`, `_paused_routes` maps.
  - `effective_balance(self, fund: str, exchange: str, asset: str = "USDT") -> float` — physical − other funds' claims − pending out + pending in (oversubscribed branch scales pro-rata).
  - `get_allocation(self, fund: str, exchange: str) -> float`
  - `is_arb_halted(self, pair: str, side: str = "buy") -> bool`
  - `can_arb(self, pair: str, side: str, size: float, *, buy_exchange: Optional[str] = None, sell_exchange: Optional[str] = None, fund: str = "arb") -> bool`
  - `get_snapshot(self) -> dict`
  - `apply_allocation(self, fund: str, exchange: str, asset: str, target_usd: float) -> None`
  - `open_transfer(self, movement_id: int, from_fund: str, to_fund: str, amount_usd: float, *, from_exchange: Optional[str] = None, to_exchange: Optional[str] = None, asset: str = "USDT", state: str = "in_transit") -> None`
  - `settle_transfer(self, movement_id: int) -> None`
  - `fail_transfer(self, movement_id: int, reason: str = "") -> None`
  - `pause_route(self, buy_exchange: str, sell_exchange: str, reason: str) -> None`
  - `clear_route_pause(self, buy_exchange: str, sell_exchange: str) -> None`
  - `reset(self) -> None` — test-only.

**Functions:** none.

**Module constants:** `inventory_state = InventoryState()` (singleton).

### agents/balance/planner.py
**Docstring:** Documents the 4-step planning pipeline (internalize → net → derived Miller-Orr band → greedy match) and the seam for a future min-cost-flow solver.

**Classes:**
- `Transfer` — `@dataclass` with `from_fund, to_fund, from_exchange, to_exchange: str`, `amount_usd: float`, `asset: str = "USDT"`, `cost_usd: float = 0.0`, `note: str = ""`.
- `PlannerConstraints` — `@dataclass` with `daily_limit: int = 3`, `daily_used: int = 0`, `in_flight: int = 0`, `floor_overrides: dict = field(default_factory=dict)`.
- `BaseRebalancePlanner(ABC)`
  - class attrs: `planner_id: str = "base"`, `display_name: str = "Base Planner"`
  - `@abstractmethod plan(self, inv: "InventoryState", targets: list["InventoryTarget"], cost_matrix: dict, constraints: PlannerConstraints) -> list[Transfer]`
- `GreedyNetPlanner(BaseRebalancePlanner)`
  - class attrs: `planner_id: str = "greedy_net"`, `display_name: str = "Greedy Net Planner (Miller-Orr)"`
  - `plan(self, inv, targets, cost_matrix, constraints) -> list[Transfer]` — try/except wrapper returning `[]` on failure.
  - `_plan_inner(self, inv, targets, cost_matrix, constraints) -> list[Transfer]`
  - `_filter_to_structural(targets) -> list["InventoryTarget"]` (staticmethod)
  - `_net_residual(inv, targets) -> dict[tuple[str, str], float]` (staticmethod)
  - `_miller_orr_band(tgt) -> float` (staticmethod) — returns full spread; caller takes `half = band/2.0`.
  - `_edge_cost(cost_matrix, edge, amount_usd, *, src_cur, floor) -> float` (staticmethod) — doubles cost when headroom < 10%.

**Functions:** none.

**Module constants:** none.

### agents/balance/policy/__init__.py
**Docstring:** Policy plugin entry point; documents how to add a new policy.

**Module constants:** `REGISTERED_POLICIES: list[BasePolicy] = [GrowthOptimalPolicy()]`; `__all__` re-exports `BasePolicy`, `InventoryTarget`, `GrowthOptimalPolicy`, `REGISTERED_POLICIES`.

### agents/balance/policy/base.py
**Docstring:** Defines `BasePolicy` + `InventoryTarget` as the universal contract.

**Classes:**
- `InventoryTarget` — `@dataclass` with `fund: str`, `exchange: str`, `asset: str`, `target_usd: float`, `floor_usd: float`, `cap_usd: float`, `drift_pct: float`, `needs_rebalance: bool`.
- `BasePolicy(ABC)`
  - class attrs: `policy_id: str = "base"`, `display_name: str = "Base Policy"`
  - `is_available(self) -> bool` — default True.
  - `@abstractmethod compute_targets(self, inv: "InventoryState", equity: float) -> list[InventoryTarget]`

**Functions:** none.

**Module constants:** none.

### agents/balance/policy/growth_optimal.py
**Docstring:** Growth-optimal allocation blended with risk-parity by `ALLOCATION_CONFIDENCE`; uses fractional Kelly (`KELLY_FRACTION`).

**Classes:**
- `GrowthOptimalPolicy(BasePolicy)`
  - class attrs: `policy_id: str = "growth_optimal"`, `display_name: str = "Growth-Optimal (Kelly · Risk-Parity blend)"`
  - `compute_targets(self, inv: "InventoryState", equity: float) -> list[InventoryTarget]`
  - `_compute_targets_inner(self, inv, equity) -> list[InventoryTarget]`

**Functions:**
- `_fund_capital_constant(fund: str) -> float`
- `_fund_edge_estimates() -> dict[str, float]`
- `_blend(growth_w: dict[str, float], parity_w: dict[str, float], alpha: float) -> dict[str, float]`
- `_venue_weights_for_fund(fund: str) -> dict[str, float]`
- `_open_position_floor(fund: str, exchange: str) -> float` — currently returns 0.0 hard-coded; documented sentinel.
- `_capacity_cap(fund: str, exchange: str) -> float`

**Module constants:**
- `_FUNDS_AND_VENUES: dict[str, list[str]] = {"signal": ["binance", "kraken", "bybit", "kucoin"], "arb": ["kraken", "bybit", "bitget", "bitstamp", "gateio", "bitfinex", "mexc"], "mexc_scalp": ["mexc"]}`

### agents/balance/rails/__init__.py
**Docstring:** Rail plugin entry point; documents how to add a new rail and notes the future `CrossChainTransferRail` plug-in slot.

**Module constants:** `REGISTERED_RAILS: list[BaseTransferRail] = [SimTransferRail(), CexTransferRail()]`; `__all__` re-exports `BaseTransferRail`, `TransferResult`, `SimTransferRail`, `CexTransferRail`, `REGISTERED_RAILS`.

### agents/balance/rails/base.py
**Docstring:** Rail contract; rails never raise — always return `TransferResult(success=False, error=...)`.

**Classes:**
- `TransferResult` — `@dataclass` with `success: bool`, `state: str`, `movement_id: Optional[int] = None`, `fee_usd: float = 0.0`, `network: Optional[str] = None`, `tx_hash: Optional[str] = None`, `error: Optional[str] = None`.
- `BaseTransferRail(ABC)`
  - class attrs: `rail_id: str = "base"`, `display_name: str = "Base Rail"`
  - `@abstractmethod is_available(self) -> bool`
  - `@abstractmethod async execute(self, transfer: "Transfer") -> TransferResult`

**Functions:** none.

**Module constants:** none.

### agents/balance/rails/cex_rail.py
**Docstring:** CEX withdrawal rail — full state machine with network selection, hardcoded address allowlist, and `ccxt.withdraw` stub behind `REBALANCE_LIVE_ENABLED`.

**Classes:**
- `CexTransferRail(BaseTransferRail)`
  - class attrs: `rail_id: str = "cex"`, `display_name: str = "CEX Transfer Rail"`
  - `is_available(self) -> bool` — requires `REBALANCE_LIVE_ENABLED`, importable `ccxt.async_support`, populated `WITHDRAWAL_ROUTES`.
  - `_pick_network(from_exchange: str, to_exchange: str, asset: str) -> Optional[str]` (staticmethod)
  - `async execute(self, transfer: "Transfer") -> TransferResult`
  - `async _execute_inner(self, transfer: "Transfer") -> TransferResult`
  - `async _call_ccxt_withdraw(self, transfer: "Transfer", *, network: str, address: str) -> str` — raises `NotImplementedError`.
  - `load_in_transit(self) -> int` — restart reconciliation.
  - `_log_pending(t: "Transfer", *, network: str) -> int` (staticmethod)
  - `_update(movement_id: int, fields: dict) -> None` (staticmethod)

**Functions:** none.

**Module constants:**
- `_WITHDRAWAL_ADDRESSES: dict[tuple[str, str], str] = {}` — hardcoded operator-provisioned address allowlist; empty by default.

### agents/balance/rails/sim_rail.py
**Docstring:** Atomic sim transfer rail with simulated fee, delay, and injectable failure for testing the auto-pause path.

**Classes:**
- `SimTransferRail(BaseTransferRail)`
  - class attrs: `rail_id: str = "sim"`, `display_name: str = "Sim Transfer Rail"`
  - `is_available(self) -> bool` — True iff `SIM_MODE`.
  - `async execute(self, transfer: "Transfer") -> TransferResult`
  - `async _execute_inner(self, transfer: "Transfer") -> TransferResult`
  - `_log(state: str, t: "Transfer", *, fee: float, error: str = None) -> int` (staticmethod)
  - `_update(movement_id: int, fields: dict) -> None` (staticmethod)

**Functions:** none.

**Module constants:** none.

## Imports graph
**This package imports from elsewhere in the project:**
- `config.settings` — everywhere.
- `database.queries` — `__init__.py`, `balance_agent.py`, `coordinator.py`, `crosschain_agent.py`, `funding_arb_agent.py`, `scalping_agent.py`, `balance/policy/growth_optimal.py`, `balance/planner.py`, `balance/rails/cex_rail.py`, `balance/rails/sim_rail.py`.
- `core.bot.CryptoBot` — `__init__.py` (lazy, inside `SignalAgentWrapper.start`).
- `core.regime_detector.regime_detector` — `scalping_agent.py` (lazy resolver).
- `execution.kill_switch.KillSwitch` — `__init__.py`.
- `execution.arb_engine.ArbEngine` — `__init__.py` (`ArbAgentWrapper`).
- `execution.crosschain_engine.CrossChainArbEngine` — `crosschain_agent.py`.
- `execution.chains.REGISTERED_CONNECTORS` — `crosschain_agent.py` (inside `is_available`).
- `execution.inventory.InventoryTarget, compute_inventory_targets` — `crosschain_agent.py`.
- `execution.funding_engine.{funding_engine, FundingEngine, FundingOpportunity, FundingPosition}` — `funding_arb_agent.py`.
- `execution.mexc_key_router.mexc_key_router` — `scalping_agent.py` (lazy).
- Self-imports: `agents.base`, `agents.scalping_agent`, `agents.crosschain_agent`, `agents.balance_agent`, `agents.funding_arb_agent`, `agents.scalping_confluence`, `agents.scalping_atr_sl`, and the balance sub-tree's internal cross-imports.

**This package is imported by:**
- `main.py`
- `ui/web_server.py`
- `core/bot.py` (indirect — receives `Coordinator` injection)
- `execution/arb_engine.py`
- Tests: `tests/test_arb_engine.py`, `tests/test_balance_agent.py`, `tests/test_bot.py`, `tests/test_coordinator.py`, `tests/test_crosschain_agent.py`, `tests/test_crosschain_engine.py`, `tests/test_dashboard.py`, `tests/test_equity_reconstruction.py`, `tests/test_funding_arb.py`, `tests/test_funds.py`, `tests/test_scalp_activation.py`, `tests/test_scalp_v2_accessors.py`, `tests/test_scalp_v2_integration.py`, `tests/test_scalping_agent.py`, `tests/test_scalping_v2.py`, `tests/test_web_server.py`.

## Plugin registrations
**`agents/__init__.py::REGISTERED_AGENTS`** (instances; coordinator iterates and calls `is_available()` per agent):
- `SignalAgentWrapper()` — `agent_id="signal"`, `optional=False`, `capital_allocation=settings.SIGNAL_AGENT_CAPITAL`. `is_available()` True in sim mode, else True iff any `{EX}_API_KEY` env var is set for an exchange in `ENABLED_EXCHANGES`.
- `ArbAgentWrapper()` — `agent_id="arb"`, `optional=True`, `capital_allocation=settings.ARB_AGENT_CAPITAL`. `is_available()` requires `execution.arb_engine.ArbEngine` importable AND ≥2 venues in `ARB_FEE_MAP` (with API key+secret when not in sim).
- `ScalpingAgent()` — `agent_id="scalp"`, `optional=True`, `capital_allocation=settings.FUND_MEXC_SCALP_CAPITAL`, with trading-budget `_capital=settings.SCALP_CAPITAL`. `is_available()` always True.
- `CrossChainArbAgent()` — `agent_id="xchain"`, `optional=True`, `capital_allocation=settings.XCHAIN_CAPITAL`. `is_available()` requires ≥2 available connectors in `execution.chains.REGISTERED_CONNECTORS`.
- `FundingArbAgent()` — `agent_id="funding_arb"`, `optional=True`, `capital_allocation=settings.FUNDING_CAPITAL_USD`. `is_available()` always True.
- `BalanceAgent()` — `agent_id="balance"`, `optional=True`, `capital_allocation=settings.BALANCE_AGENT_CAPITAL` (default 0). `is_available()` always True.

**`agents/balance/policy/__init__.py::REGISTERED_POLICIES`:**
- `GrowthOptimalPolicy()` — `policy_id="growth_optimal"`. `is_available()` inherits the default True.

**`agents/balance/rails/__init__.py::REGISTERED_RAILS`:**
- `SimTransferRail()` — `rail_id="sim"`. `is_available()` True iff `SIM_MODE`.
- `CexTransferRail()` — `rail_id="cex"`. `is_available()` True iff `REBALANCE_LIVE_ENABLED` + `ccxt.async_support` importable + non-empty `WITHDRAWAL_ROUTES`.

No `REGISTERED_*` for `BaseRebalancePlanner` — `GreedyNetPlanner` is the single default instantiated by `BalanceAgent.__init__`.

## Tests
**tests/test_balance_agent.py** — covers `BalanceAgent`, `InventoryState`, `GreedyNetPlanner`, rails, `GrowthOptimalPolicy`.
- `test_rails_registry_minimum` — verifies sim + cex are both registered.
- `test_policies_registry_minimum` — verifies growth-optimal is registered.
- `test_aggregator_imports_only_base_and_registry` — enforces Plugin Rule 1.
- `test_fake_rail_is_picked_up` — registry injection.
- `test_crashing_rail_does_not_break_agent` — error isolation.
- `test_unavailable_rail_is_skipped` — falls through.
- `test_defaults_dont_change_unconfigured_agents` — `BaseAgent.set_capital_allocation` no-op safety.
- `test_set_capital_refuses_below_open_position` — rail-2 floor.
- `test_effective_balance_fails_open_with_no_claims` — fail-open default.
- `test_claims_partition_shared_venue` — fund partitioning.
- `test_undersubscribed_venue_returns_physical_minus_other_claims` — confirms the 2026-05-29 FIX described in `InventoryState.effective_balance` docstring.
- `test_partially_subscribed_two_funds_each_get_slack_plus_claim`
- `test_oversubscribed_venue_scales_proportionally`
- `test_pending_out_subtracts_from_source`
- `test_can_arb_refuses_paused_route`
- `test_targets_respect_reserve` — `COMPOUND_RESERVE_PCT`.
- `test_risk_parity_fallback_when_edge_absent`
- `test_capacity_cap_caps_and_cascades`
- `test_zero_plan_when_nodes_at_target`
- `test_in_flight_lockout_in_live` — rail 4.
- `test_daily_rate_limit` — rail 5.
- `test_atomic_completion_fee_and_state` — sim rail completion.
- `test_injected_failure_path` — `SIM_REBALANCE_FAILURE_RATE`.
- `test_dormant_without_live_flag` — cex rail `is_available` gating.
- `test_available_when_live_and_routes`
- `test_refuses_unknown_network`
- `test_kill_blocks_new_transfers`
- `test_arm_confirm_two_step` — arm/confirm token.
- `test_arm_expiry`
- `test_dynamic_base_position_scales_with_allocation`
- `test_dynamic_base_position_clamped`
- `test_get_capital_allocation_takes_max`
- `test_drift_hint_below_threshold_internalized`
- `test_drift_hint_above_threshold_structural`
- `test_drift_hint_can_be_swept`
- `test_no_020_literal_in_planner` — guard against magic numbers.
- `test_node_at_0_4_spread_does_not_breach` — Miller-Orr band semantics.
- `test_node_at_0_6_spread_breaches`
- `test_gross_cost_net_round_trip` — fee accounting.
- `test_raw_rows_unmutated`
- `test_excludes_old_rows_outside_window`
- `test_get_true_pnl_importable` — smoke import.

**tests/test_coordinator.py** — covers `Coordinator`.
- `test_kill_all_calls_close_on_every_agent`
- `test_kill_all_runs_close_concurrently`
- `test_unavailable_agent_skipped_on_start`
- `test_portfolio_halt_when_daily_loss_exceeds_threshold`
- `test_portfolio_healthy_below_threshold`
- `test_get_portfolio_stats_aggregates_correctly`
- `test_get_agent_stats_sorted_by_capital_desc`
- `test_crashing_agent_does_not_break_others_on_kill_all`
- `test_crashing_agent_get_stats_returns_error_field`
- `test_new_agent_class_plugs_in_via_coordinator` — plugin pattern.
- `test_get_agent_lookup`
- `test_set_dashboard_propagates_to_agents`
- `test_per_fund_circuit_breaker_halts_only_breaching_fund`
- `test_per_fund_circuit_breaker_clears_when_pnl_recovers`
- `test_total_equity_is_dynamic_sum_of_fund_equities`

**tests/test_crosschain_agent.py** — covers `CrossChainArbAgent`.
- `test_xchain_agent_registered`, `test_registered_xchain_is_crosschain_agent` — registry hookup.
- `test_subclasses_base_agent`, `test_required_class_attrs`
- `test_observation_mode_capital_zero_by_default`
- `test_get_stats_returns_offline_before_start`
- `test_get_stats_reports_observation_capital_when_xchain_capital_zero`
- `test_close_all_positions_is_safe_with_no_engine`, `test_stop_is_safe_with_no_engine`
- `test_start_offline_without_two_connectors`
- `test_get_inventory_targets_returns_inventory_target_list`
- `test_get_inventory_targets_swallows_db_errors`
- `test_get_inventory_targets_uses_current_balances_for_drift`

**tests/test_funding_arb.py** — covers `FundingArbAgent` (and the underlying engine). Tests relevant to the agent class:
- `test_observation_mode_zero_routing` — Phase-1 invariant.
- `test_daily_loss_circuit_breaker_halts`, `test_daily_loss_circuit_breaker_zero_alloc_noop`, `test_daily_loss_halt_scales_with_allocation`, `test_consecutive_loss_circuit_breaker_halts` — `_check_circuit_breakers`.
- `test_get_stats_and_availability`
- `test_close_all_positions_drains_via_gather`
- `test_db_roundtrip_observations_and_summary`
- `test_coordinator_picks_up_via_registry`
- Remaining tests (e.g. funding APR annualisation, depth gate, exit-reason ordering) exercise the underlying `FundingEngine`.

**tests/test_scalping_agent.py** — covers `ScalpingAgent` + `OFIEngine` + `FeeManager`.
- `test_ofi_event_increment_bid_improved`, `..._ask_improved`, `..._neutral` — Cont-Kukanov-Stoikov increment math.
- `test_ofi_multilevel_weights`, `test_ofi_zscore_after_min_buckets`, `test_ofi_depth_weights_ten_levels`, `test_ofi_uses_available_levels_when_book_shorter`.
- `test_fee_manager_override_mexc`, `test_fee_manager_dynamic_tp_sl_mexc`, `test_fee_manager_dynamic_tp_sl_bitget`, `test_fee_manager_viability_mexc_passes`, `test_fee_manager_viability_high_fee_fails`.
- `test_tfi_confirms_matching_direction`, `test_tfi_normalized_imbalance_ratio`, `test_direction_persistence_resets`.
- `test_entry_observation_logged_mexc`, `test_stale_feed_guard_skips_and_logs`, `test_entry_blocked_unapproved_exchange`.
- `test_exit_ofi_exhausted`, `test_circuit_breaker_daily_loss`, `test_scalp_daily_loss_halt_scales_with_allocation`, `test_scalp_zero_alloc_daily_loss_noop`.
- `test_place_order_records_sim_trade`, `test_exit_closes_sim_trade_and_tracks_net_equity`.
- `test_daily_reset_clears_circuit_breaker`, `test_session_gate_blocks_outside_window`, `test_news_guard_blocks_entry`.
- `test_market_data_wired_mid_price`, `test_market_data_falls_back_to_all_prices`, `test_market_data_stub_when_unwired`.
- `test_regime_detector_wired_returns_uppercase`, `test_regime_choppy_blocks_entry`.
- `test_market_data_get_spread_bps_math`, `test_market_data_get_change_pct_returns_pct_over_window`, `..._none_with_no_history`, `..._none_when_window_predates_history`, `test_market_data_record_price_sample_trims_old_entries`.
- `test_get_btc_1m_change_wired_to_market_data`, `..._returns_zero_when_no_data`, `..._zero_when_market_data_missing`.
- `test_micro_price_tracker_backfills_30s`.

**tests/test_scalp_v2_integration.py** — covers the v2 integration with the agent.
- `test_v2_block_logs_prefixed_skip_with_entry_price`
- `test_v2_pass_uses_atr_tp_sl_and_labels`
- `test_confluence_disabled_falls_back_to_v1_entry`

**tests/test_scalp_v2_accessors.py** — covers `OFIEngine` v2 accessors + market_data v2 helpers.
- `test_get_mid_price_from_book`, `test_get_mid_price_falls_back_to_last_price`, `test_get_order_book_shape`, `test_get_ema_and_atr_from_candles`, `test_get_vwap_and_volume`, `test_get_mid_price_at_offset`, `test_ofi_get_z_score_none_until_bucket_closes`, `test_ofi_get_exchanges_for_symbol`.

**tests/test_scalp_activation.py** — covers v2 activation gating.
- `test_activation_readiness_empty_not_ready`, `test_activation_stats_and_v1_v2`, `test_recalibration_statements_execute`.

**tests/test_scalping_v2.py** — exercises `ConfluenceChecker`, `ATRStopCalculator`, and `is_ready_for_live_v2` via direct imports from `agents.scalping_confluence`, `agents.scalping_atr_sl`, `agents.scalping_agent_v2_integration`. The file body has no `def test_*` discovered by the standard pattern (it may rely on pytest-collected fixtures or class methods); review the source for the actual test layout.

**tests/test_equity_reconstruction.py** — `test_signal_get_stats_reads_ledger`, `test_signal_get_stats_falls_back_when_db_unreachable`, `test_scalp_reconstruct_pnl`, `test_scalp_reconstruct_pnl_survives_db_error`, `test_arb_reconstruct_engine_pnl` — cover the wrapper restart-resume P&L paths.

**tests/test_funds.py** — `test_fund_constants_present_and_positive`, `test_mexc_arb_fund_constant_present`, `test_starting_capital_is_sum_of_funds_plus_reserve`, `test_legacy_aliases_track_fund_constants`, `test_mexc_folded_into_arb_routing_and_fee_map`, `test_registered_funds_carry_their_allocation`, `test_mexc_arb_wrapper_removed`, `test_scalp_fund_size_decoupled_from_trading_budget`, `test_sim_balance_ledger_sums_to_starting_capital_and_includes_mexc`, `test_sim_balance_ledger_ring_fence_coverage_per_fund`, `test_order_router_sizes_off_signal_fund_not_total`.

**tests/test_arb_engine.py** — primarily covers `execution.arb_engine.ArbEngine` but includes integration tests touching `agents.funding_arb_agent`: `test_funding_arb_circuit_breaker_halts_on_daily_loss`, `..._zero_alloc_noop`, `test_funding_arb_initial_allocation_from_fund_constant`, `test_funding_arb_set_capital_allocation_changes_field`, `test_funding_arb_breaker_scales_with_live_allocation`, `test_funding_arb_breaker_zero_allocation_no_divide_by_zero`, `test_funding_arb_fund_claim_visible_to_balance_agent`.

**tests/test_dashboard.py** — exercises dashboard panels that depend on the Coordinator-injected `Bot`/`ScalpingAgent`; non-agent panels skipped above. Relevant cases: `test_scalp_panel_*`, `test_scalp_snapshot_*`, `test_full_dashboard_includes_scalp_row`.

**tests/test_web_server.py** — exercises the web layer's interaction with the BalanceAgent + roster snapshot. Relevant: `test_snapshot_includes_balance_keys`, `test_snapshot_safe_when_balance_agent_raises`, `test_rebalance_arm_returns_token`, `test_rebalance_confirm_executes_within_window`, `test_rebalance_confirm_expired_token`, `test_rebalance_confirm_mismatched_token`, `test_rebalance_cancel_invalidates_token`, `test_rebalance_blocked_when_live_disabled`, `test_rebalance_blocked_when_balance_agent_missing`, `test_rebalance_logs_event_on_confirm`, `test_rebalance_confirm_replan_mismatch`, `test_placeholder_agents_not_in_snapshot`, `test_api_agent_signal_returns_trades_and_insights`, `test_api_agent_arb_returns_trades_and_insights`, `test_api_agent_scalp_returns_trades_and_insights`, `test_api_agent_placeholders_now_404`, `test_api_agent_unknown_returns_404`, `test_api_agent_insights_filtered_by_agent_id`, `test_scalp_win_rate_zero_when_no_trades`, `test_scalp_win_rate_computed_from_pnl_bps_positive`, `test_scalp_win_rate_resets_at_utc_midnight`, `test_scalp_closed_trades_persisted_in_snapshot`, `test_scalp_closed_trades_buffer_capped_at_setting`, `test_scalp_live_trades_separate_from_closed`, `test_exposure_includes_scalp_positions`, `test_snapshot_includes_arb_extended_keys`, `test_snapshot_includes_xchain_keys`, `test_snapshot_includes_funding_keys`, `test_snapshot_safe_when_xchain_engine_raises`, `test_snapshot_safe_when_funding_engine_raises`.

**tests/test_bot.py** — exercises `core.bot.CryptoBot` (which the `SignalAgentWrapper` instantiates) but doesn't import from `agents` directly aside from injecting the coordinator. Listed here for completeness; tests not enumerated since they target `core.bot`.

## TODOs / FIXMEs / stubs
- `agents/scalping_agent.py:62` — `# TODO (dashboard, Phase 2): expose self._observations[-15:] as a` (scalp-feed dashboard panel).
- `agents/scalping_agent.py:661` — `# the wiring TODOs, both checked at evaluation time.` (referential).
- `agents/scalping_agent.py:871` — `# TODO Phase 2: call from market_data WebSocket handler.` (`on_book`).
- `agents/scalping_agent.py:881` — `# TODO Phase 2: call from market_data WebSocket handler.` (`on_trade`).
- `agents/coordinator.py:286` — `# enforcement is a TODO once agents grow that hook.` (portfolio exposure soft block).
- `agents/__init__.py:382` — `# Web UI v2 removed the macro / sentiment_agent / onchain placeholder` (documents removed placeholders; `PlaceholderAgent` kept as base class).
- `agents/__init__.py:26` — `placeholders) live in this module rather than the Coordinator, so the` (docstring referencing PlaceholderAgent).
- `agents/base.py:161` — `Default 0.0 — placeholder / operational agents have no open` (docstring).
- `agents/base.py:206` — `logger.debug(f"placeholder agent {self.agent_id}: start (no-op)")`.
- `agents/crosschain_agent.py:20` — `connectors' submit_swap raises NotImplementedError until that path is` (docstring documenting the connector-level stub).
- `agents/balance/rails/cex_rail.py:214` — `raise NotImplementedError("live ccxt.withdraw integration pending operator authorisation")` (the production live withdraw path is intentionally stubbed).

## Known issues observed
- **Duplicate scalp-v2 source between `agents/` and `scalping_v2/`.** `agents/scalping_atr_sl.py`, `agents/scalping_confluence.py`, and `agents/scalping_agent_v2_integration.py` are byte-identical to `scalping_v2/scalping_atr_sl.py`, `scalping_v2/scalping_confluence.py`, and `scalping_v2/scalping_agent_v2_integration.py` respectively (`diff` returns empty). Production code, tests, and `agents/scalping_agent.py` import the `agents.*` copies; `scripts/run_recalibration.py` and `scalping_v2/demo_simulate_v1_vs_v2.py` are the only consumers of the `scalping_v2/` copies, both via path-based or local imports. The `scalping_v2/` tree should be treated as a docs+demo bundle; the duplicated `.py` files are dead code or risk silent drift if either copy is edited.
- **Logger naming leak.** `agents/scalping_atr_sl.py` and `agents/scalping_confluence.py` use `logging.getLogger("scalping_v2.atr_sl")` and `"scalping_v2.confluence"` — these strings reflect the original module location and won't follow if the file is renamed/moved or filtered in logging configuration that targets `agents.*` paths.
- **`agents/scalping_agent_v2_integration.py` is a reference module, not production wiring.** It documents the v2 integration recipe; the only production usage is `is_ready_for_live_v2()` (consumed by tests). `evaluate_signal_v2`, `_initial_observation`, `_run_legacy_gates`, and `_compute_position_size_usd` are reference/demo code never called from `ScalpingAgent`. Risk: it carries `DB_COLUMNS_V2` as the canonical schema-change doc but the actual ALTER statements live elsewhere.
- **Dual sources of truth for capital.** `ScalpingAgent` keeps both `self.capital_allocation` (fund pool, drives BalanceAgent compounding) and `self._capital` (trading budget — drives observation-vs-sim toggle); the documented invariant is that `set_capital_allocation` only mirrors to `_capital` when `settings.SCALP_CAPITAL > 0`. Operators flipping the fund allocation via the BalanceAgent will NOT auto-flip observation mode — that's documented intent but easy to forget.
- **Theta hint is a hard-coded magic number.** `agents/balance/policy/growth_optimal.py:258` hard-codes `theta_hint = 0.20` rather than reading a settings constant; the planner-side guard `BALANCE_STRUCTURAL_DRIFT_HINT` is the real gate, but the producer-side hint diverging from settings is a latent inconsistency the docstring acknowledges without resolving (and `test_no_020_literal_in_planner` only guards the planner side).
- **`_open_position_floor` is a placeholder.** `agents/balance/policy/growth_optimal.py:117-127` always returns `0.0` with a docstring acknowledging the coupling problem; the rail-2 floor enforcement therefore relies entirely on `BalanceAgent._safety_clear` reading the sibling agent's `get_open_position_notional()` at dispatch time, not at policy-target time. A divergence between target and floor is possible in principle.
- **`_WITHDRAWAL_ADDRESSES` is empty by default.** `agents/balance/rails/cex_rail.py:51-54` ships an empty allowlist; even with `REBALANCE_LIVE_ENABLED=True` every live transfer fails with `no whitelisted address for ...`. This is intentional fail-closed behaviour but is a hard prerequisite that must be operator-provisioned before live transfers are possible.
- **Coordinator portfolio-exposure CB is observational only.** `agents/coordinator.py:286-293` logs a warning when `total_exposure_pct >= PORTFOLIO_MAX_EXPOSURE_PCT` but does not block new entries; the comment explicitly flags that `block_new_entries` enforcement is a TODO.
- **`SignalAgentWrapper.start` calls `_bot.start()` which never returns.** The implicit lifecycle is "start blocks forever" — the Coordinator's `gather` pattern handles this, but any caller awaiting `start()` directly will hang. Behaviour is documented in the docstring but worth noting.
- **`agents/balance_agent.py` reaches into sibling agents via `REGISTERED_AGENTS` walk.** `_find_sibling` re-imports `REGISTERED_AGENTS` inside its method body to avoid circular import — fine but couples the BalanceAgent to the package-level registry implicitly. The fund→agent mapping (`signal→signal`, `arb→arb`, `mexc_scalp→scalp`) is hard-coded in `_find_sibling_for_fund` rather than coming from settings.
- **`agents/scalping_agent.py:1180-1190` debounces stale-feed logging with the same `_last_mid` dict it uses to detect motion** — re-arming after a hit means a persistently frozen feed will log once per `SCALP_STALE_MID_THRESHOLD_SEC`, but if the threshold is misconfigured to 0 the debounce collapses. Defensive constants in `settings.py` cover this.
- **`scalping_agent_v2_integration._compute_position_size_usd`** references `agent.scalp_capital`, `agent.scalp_fund_capital`, and `agent.s` — none of these attributes exist on the real `ScalpingAgent` class. Confirms this module is reference-only; calling it against a live `ScalpingAgent` would `AttributeError`.

---

# Module Report: execution

## Purpose

The `execution/` package is the order-routing and trade-lifecycle layer. It owns: (a) the synchronous-from-async `OrderRouter` that the main signal-track `Bot` calls in all three approval modes (`per_trade`, `window`, `autonomous`) to write a sim Trade row via `_sim_execute` or (stub) live execution via `_live_execute`; (b) the `KillSwitch` that bypasses every queue/approval and closes all open positions in parallel via `asyncio.gather`; (c) the `PositionManager` that ticks SL/TP exits and trips portfolio-level circuit breakers; (d) three standalone, rule-based arb engines that intentionally live OUTSIDE the Bot/agent-loop dispatch (`ArbEngine` for CEX cross-exchange arb, `FundingRateArbEngine` for funding-rate carry inside `arb_engine.py`, `FundingEngine` for the Phase-1 Binance delta-neutral funding-arb, `CrossChainArbEngine` for L2 cross-chain arb in observation mode); plus (e) supporting infrastructure: per-pair MEXC key router, cross-chain DEX connector plugin layer, and an inventory-target producer for the BalanceAgent. The arb engines deliberately do NOT import `core/bot.py` — they own their circuit breakers, capital allocation, and approval bypass because arb gaps close in seconds.

## Subpackages

- `execution/chains/` — chain-connector plugin layer for the CrossChainArbEngine. One file per L2 (Arbitrum/Base/Optimism) registers a `BaseChainConnector` subclass in `REGISTERED_CONNECTORS`; reads WETH-USDC pool state + per-swap gas cost; submit_swap raises `NotImplementedError` (observation mode).

## Files

| File | LOC | One-sentence summary |
|---|---:|---|
| `execution/__init__.py` | 0 | Empty package marker. |
| `execution/arb_engine.py` | 1026 | Standalone CEX cross-exchange `ArbEngine` + sibling `FundingRateArbEngine`, with their own circuit breakers, per-symbol locks, depth-aware slippage model, and balance/inventory gates. |
| `execution/crosschain_engine.py` | 679 | Cross-CHAIN observation-mode arb engine: scans REGISTERED_CONNECTORS, computes net edge bps, logs every (buy_chain, sell_chain) evaluation to `xchain_observations`. |
| `execution/funding_engine.py` | 483 | Phase-1 Binance delta-neutral funding-rate carry engine — sim-only, observation-mode hard-gated. |
| `execution/inventory.py` | 187 | Pure `compute_inventory_targets()` producer for the BalanceAgent — chain-weight allocation from xchain observations, drift gate baked into the dataclass. |
| `execution/kill_switch.py` | 98 | `KillSwitch.engage()` closes every open trade in parallel via `asyncio.gather`; logs to circuit_breakers. |
| `execution/mexc_key_router.py` | 164 | Per-pair MEXC API key router — lazy ccxt.mexc client pool keyed by `MEXC_PAIR_KEY_MAP` index. |
| `execution/position_manager.py` | 104 | Polls open trades, hits SL/TP exits, runs portfolio circuit-breaker checks (daily loss, consecutive losses). |
| `execution/router.py` | 85 | `OrderRouter` — main signal-track sim/live dispatch; sizes off `FUND_SIGNAL_CAPITAL`. |
| `execution/chains/__init__.py` | 58 | Registers `REGISTERED_CONNECTORS = [ArbitrumConnector(), BaseChainConnectorInstance(), OptimismConnector()]`. |
| `execution/chains/_solidly_volatile.py` | 238 | Shared partial impl for Solidly-fork volatile (x*y=k) pools — reused by Base + Optimism connectors. |
| `execution/chains/arbitrum.py` | 274 | `ArbitrumConnector` — Uniswap v3 WETH-USDC pool reader with virtual-reserve derivation from sqrtPriceX96. |
| `execution/chains/base_chain.py` | 30 | `BaseChainConnectorInstance` — Aerodrome on Base (subclass of `SolidlyVolatilePoolConnector`). |
| `execution/chains/base_connector.py` | 154 | ABC + dataclasses: `BaseChainConnector`, `PoolState`, `VenueConfig`. |
| `execution/chains/optimism.py` | 25 | `OptimismConnector` — Velodrome on Optimism (subclass of `SolidlyVolatilePoolConnector`). |

## Public surface

### execution/__init__.py
**Docstring:** none

Empty (0 bytes).

### execution/router.py
**Docstring:** `"""execution/router.py — Order router — handles both sim and live order execution."""`

**Classes:**
- `OrderRouter` — main signal-track router; sim writes Trade row, live is a stub.
  - `__init__(self, exchange_manager=None)`
  - `async execute(self, signal: Signal, profile) -> Optional[dict]` — compute size/SL/TP, dispatch to sim or live execute, return trade dict.
  - `_sim_execute(self, signal, entry, sl, tp, size_usd) -> int` — build trade_data dict and call `save_trade()`; tags `sim_mode=True`, `strategy=settings.ACTIVE_STRATEGY`.
  - `async _live_execute(self, signal, entry, sl, tp, size_usd) -> Optional[int]` — STUB: logs warning, returns None.
  - `_get_price(self, signal: Signal) -> Optional[float]` — stub returning None (MarketData provides via callback).
  - `update_portfolio_value(self, value: float)` — setter for `_portfolio_value` (currently sized off `FUND_SIGNAL_CAPITAL`).

**Module constants:** none.

### execution/kill_switch.py
**Docstring:** `"""execution/kill_switch.py — Emergency kill switch. One call closes everything immediately. Bypasses all logic, queues, and approvals. Called by [K] keypress or circuit breaker."""`

**Classes:**
- `KillSwitch` — closes all open positions in parallel.
  - `__init__(self, exchange_manager=None, sim_mode: bool = True)`
  - `async engage(self, reason: str = "manual") -> dict` — flips `_active`, fetches `get_open_trades()`, runs `_close_position` for each via `asyncio.gather(..., return_exceptions=True)`, logs to circuit_breakers, returns `{closed, timestamp, reason, trades}`.
  - `async _close_position(self, trade) -> dict` — sim path uses entry_price as exit (TODO: live price); live path calls `exchange_manager.market_close()` and computes pnl_pct.

**Module constants:** none.

### execution/position_manager.py
**Docstring:** `"""execution/position_manager.py — Monitors open positions and triggers SL/TP exits."""`

**Classes:**
- `PositionManager` — polls open trades for SL/TP hits + portfolio circuit-breaker logic.
  - `__init__(self, market_data=None)`
  - `async check_positions(self) -> list` — iterates `get_open_trades()`, evaluates SL/TP per side, closes via `close_trade(...)`, returns list of closed trade ids; then awaits `_check_circuit_breakers()`.
  - `_get_price(self, trade)` — proxies `market_data.get_price(exchange, pair)`.
  - `_calc_pnl(self, trade, exit_price) -> float` — long/short pnl_pct.
  - `async _check_circuit_breakers(self)` — reads `settings.CIRCUIT_BREAKERS`, checks daily_loss vs `get_today_pnl_pct()` and consecutive_loss vs `get_consecutive_losses()`; calls `_trigger(action, reason)`.
  - `_trigger(self, action: str, reason: str)` — logs CB event, sets `_halted` / `_paused` flag.
  - `resume(self)` — clears halt/pause flags.
  - `is_halted` (property `-> bool`), `is_paused` (property `-> bool`), `halt_reason` (property `-> str`)

**Module constants:** none.

### execution/arb_engine.py
**Docstring:** Standalone CEX cross-exchange arb engine + sibling funding-rate engine. Standalone — only imports ccxt, config.settings, database.queries.

**Module constants:**
- `STATUS_OFFLINE = "OFFLINE"`, `STATUS_RUNNING = "RUNNING"`, `STATUS_HALTED = "HALTED"`, `STATUS_STOPPED = "STOPPED"`

**Dataclasses:**
- `ArbOpportunity` — `symbol`, `buy_exchange`, `sell_exchange`, `buy_price`, `sell_price`, `gross_gap_pct`, `net_gap_pct`, `max_size_usd`, `detected_at` (time.monotonic), plus optional `spread_buy_pct=0.0`, `spread_sell_pct=0.0`, `depth_buy_usd=0.0`, `depth_sell_usd=0.0`, `opportunity_log_id: Optional[int] = None`, `funding_rate_pct: Optional[float] = None`.
- `ArbResult` — `opportunity`, `success`, `buy_fill`, `sell_fill`, `gross_pnl_usd`, `net_pnl_usd`, `execution_ms`, `error: Optional[str] = None`, `status: str = "executed"`, `slippage_buy_pct: Optional[float] = None`, `slippage_sell_pct: Optional[float] = None`.

**Functions:**
- `gross_gap_pct(buy_price: float, sell_price: float) -> float` — `(sell-buy)/buy * 100`.
- `net_gap_pct(buy_price: float, sell_price: float, buy_ex: str, sell_ex: str, fee_map: dict) -> tuple[float, float]` — returns (gross_pct, net_pct after both legs' fees).
- `min_gap_threshold(buy_ex: str, sell_ex: str) -> float` — Bitget unlocks `ARB_MIN_GAP_PCT`; otherwise `ARB_MIN_GAP_PCT_FALLBACK`.
- `_liquidity_usd(levels: list, depth: int = 3) -> float` — sums top N price×size.
- `slippage_pct(base_spread_pct: float, size_usd: float, depth_usd: float) -> float` — `base_spread * sqrt(size/depth)`, clamped to `[ARB_SLIPPAGE_MIN_PCT, ARB_SLIPPAGE_MAX_PCT]`.

**Classes:**
- `ArbEngine` — fully async cross-exchange arb engine.
  - `__init__(self, exchange_clients: Optional[dict] = None, dashboard=None, sim_mode: Optional[bool] = None, *, fund_id: str = "arb", exchanges: Optional[list] = None)`
  - `async start(self) -> None`
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None`
  - `get_stats(self) -> dict`
  - `async _scan_loop(self) -> None`
  - `async _find_best_opportunity(self) -> Optional[ArbOpportunity]`
  - `@staticmethod _log_opportunity(**kwargs) -> Optional[int]`
  - `async _safe_fetch_book(self, ex, sym: str)`
  - `async _execute_arb(self, opp: ArbOpportunity) -> None`
  - `_dynamic_base_position(self) -> float`
  - `async _check_balances(self, opp: ArbOpportunity, size_base: float) -> tuple[bool, Optional[str]]`
  - `@staticmethod async _fetch_free_balance(ex, ccy: str) -> Optional[float]`
  - `@staticmethod _mark_opportunity_executed(opp_id: Optional[int], trade_id: Optional[int]) -> None`
  - `_sim_fills(self, opp: ArbOpportunity) -> tuple[float, float, float, float]` — depth-aware slippage on both legs.
  - `async _live_fills(self, opp: ArbOpportunity, size_base: float) -> tuple[float, float]` — both legs via single `asyncio.gather`.
  - `_update_stats(self, result: ArbResult) -> None`
  - `_notify_dashboard(self, result: ArbResult) -> None`
  - `_log_to_db(self, result: ArbResult) -> Optional[int]`
  - `_cb_triggered(self) -> bool` — %-based daily loss + consecutive-loss halt.
  - `async _daily_reset_loop(self) -> None`
  - `_build_clients(self) -> dict`
  - class attrs / instance state: `dashboard`, `sim_mode`, `fund_id`, `_exchange_filter`, `_exchanges`, `_running`, `_status`, `_scan_task`, `_reset_task`, `_daily_pnl_usd`, `_total_pnl_usd`, `_total_trades`, `_consecutive_losses`, `_last_opportunity`, `_last_trade_time`, `missed_balance_checks`, `_capital_allocation`, `_symbol_locks`, `_semaphore`, `_active_arbs`.

- `FundingRateArbEngine` — funding-rate carry engine (data_sources aggregator).
  - `__init__(self, sim_mode: Optional[bool] = None, dashboard=None)`
  - `async start(self) -> None`
  - `@staticmethod _log_coinglass_status() -> None`
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None`
  - `get_stats(self) -> dict`
  - `set_capital_allocation(self, amount: float) -> None`
  - `async fetch_funding_rates(self) -> dict[str, float]`
  - `async _scan_loop(self) -> None`
  - `_cb_triggered(self) -> bool`
  - `async _daily_reset_loop(self) -> None`

### execution/crosschain_engine.py
**Docstring:** Cross-CHAIN arb engine — non-atomic, inventory-pre-positioned, observation-mode only. NOT the cross-exchange engine.

**Module constants:** `STATUS_OFFLINE`, `STATUS_RUNNING`, `STATUS_HALTED`, `STATUS_STOPPED`.

**Dataclasses:**
- `XChainEvaluation` — fields: `symbol, buy_chain, sell_chain, buy_venue, sell_venue, notional_usd, spread_bps, rt_fee_bps, gas_bps, slip_bps, bridge_bps, net_edge_bps, gas_breakeven_usd, would_entry, skip_reason="", buy_block=0, sell_block=0, detected_at=field(default_factory=time.monotonic)`.
- `CircuitBreakerState` — `daily_pnl_usd=0.0, consecutive_losses=0, halted=False, halt_reason=""`.

**Functions:**
- `gas_breakeven_usd(b_gas_usd: float, s_gas_usd: float, gas_budget_bps: float) -> float` — smallest notional where round-trip gas <= budget.
- `optimal_notional_constant_product(buy_state: PoolState, sell_state: PoolState, fee_bps: float) -> float` — Angeris et al. (2019) `dx* = (sqrt(γxyP_ext) - x)/γ`.
- `capped_optimal_notional(buy_state, sell_state, *, fee_bps: float, slippage_tolerance_bps: float, max_position_usd: float) -> float` — caps raw optimal by depth + tolerance + position cap.
- `estimated_two_leg_slippage_bps(notional_usd: float, buy_state: PoolState, sell_state: PoolState) -> float` — two-leg additive slippage.

**Classes:**
- `CrossChainArbEngine` — scan loop, edge math, circuit breakers, observation logger.
  - `__init__(self, connectors: Optional[list[BaseChainConnector]] = None, *, sim_mode: Optional[bool] = None)`
  - `async start(self) -> None` — refuses to come online with <2 connectors.
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None` — no-op in observation mode + log to circuit_breakers.
  - `get_stats(self) -> dict`
  - `set_capital_allocation(self, amount: float) -> None`
  - `async _scan_loop(self) -> None`
  - `async _scan_symbol(self, symbol: str) -> None`
  - `async _evaluate_symbol(self, symbol: str) -> None`
  - `async _fetch_states(self, symbol: str) -> list[PoolState]`
  - `async _fetch_gas_costs(self) -> dict[str, float]`
  - `_evaluate_pair(self, *, symbol: str, buy: PoolState, sell: PoolState, gas_costs: dict[str, float]) -> Optional[XChainEvaluation]`
  - `@staticmethod _block_is_fresh(buy_block: int, sell_block: int) -> bool`
  - `_persist(self, e: XChainEvaluation) -> None` — writes `xchain_observations` row; `observation_only=(self._capital_allocation == 0)`.
  - `_cb_triggered(self) -> bool`
  - `async _daily_reset_loop(self) -> None`

### execution/funding_engine.py
**Docstring:** Funding-rate arbitrage engine for FundingArbAgent (Phase 1). Single venue Binance, delta-neutral. Observation mode hard-gated.

**Dataclasses:**
- `FundingOpportunity` — `symbol, variant, venue_long, venue_short, funding_apr, spread_apr, oi_usd, depth_ok`.
- `FundingPosition` — `opp, notional_usd, margin_used, basis_at_entry, funding_collected=0.0, fees_paid=0.0, opened_at=field(default_factory=time.time)`.

**Classes:**
- `FundingEngine` — scan + sim execution for funding-rate carry.
  - `__init__(self, ccxt_factory=None)`
  - `async scan(self) -> list[FundingOpportunity]`
  - `async open(self, opp: FundingOpportunity) -> None` — hard `assert settings.SIM_MODE` under `FUNDING_OBSERVATION_MODE`; rejects non-delta_neutral; both legs via `asyncio.gather`.
  - `exit_reason(self, pos: FundingPosition) -> Optional[str]` — priority: funding_decay > basis_blowout > margin_breach > venue_health > max_hold.
  - `async close(self) -> None` — close ccxt client.
  - `_get_exchange(self)` — lazy ccxt.binance with `options={"defaultType": "future"}` (REQUIRED).
  - `async _fetch_native_funding(self, venue: str, symbol: str) -> tuple[Optional[float], float]`
  - `_build_opportunities(self, symbol: str, venue: str, funding_apr: float, oi_usd: float) -> list[FundingOpportunity]`
  - `@staticmethod _filter(opps: list[FundingOpportunity]) -> list[FundingOpportunity]`
  - `async _place(self, pos: FundingPosition, leg: str) -> Optional[int]` — sim route; live is a stub log.
  - `@staticmethod _basis(opp: FundingOpportunity) -> float` (returns 0.0 Phase 1)
  - `@staticmethod _basis_blowout(pos: FundingPosition) -> bool` (returns False Phase 1)
  - `@staticmethod _margin_breach(pos: FundingPosition) -> bool` (returns False Phase 1)
  - `_venue_unhealthy(self, pos: FundingPosition) -> bool`

**Module-level singleton:** `funding_engine = FundingEngine()`

### execution/inventory.py
**Docstring:** Inventory targets for the cross-chain arb agent — "publish target, let the BalanceAgent move USDC". DO NOT implement transfers here.

**Dataclasses:**
- `InventoryTarget` — `connector_id, symbol, target_base_usd, target_quote_usd, current_base_usd, current_quote_usd, drift_pct, needs_rebalance`.

**Functions:**
- `_chain_weights_from_observations(observations: list[dict], chains: list[str]) -> dict[str, float]` — proportional to would_entry frequency with `1/(2N)` floor.
- `compute_inventory_targets(observations: list[dict], current_balances: dict[str, dict[str, float]] | None = None, *, capital_usd: float | None = None, chains: list[str] | None = None, symbol: str | None = None, drift_threshold: float | None = None) -> list[InventoryTarget]` — one InventoryTarget per enabled chain; 50/50 base/quote split.

**Module constants:** `__all__ = ["InventoryTarget", "compute_inventory_targets"]`.

### execution/mexc_key_router.py
**Docstring:** MEXC per-pair key router. One account can hold many API keys each restricted to a different pair subset. Module-level singleton.

**Classes:**
- `MexcKeyRouter` — lazily-built pool of ccxt.mexc clients keyed by per-pair key index.
  - `__init__(self)`
  - `get_client_for(self, symbol: str)` — returns ccxt client or None.
  - `any_client(self)` — any constructed/constructable MEXC client (FeeManager pre-warm); falls back to scanning indices 1..30.
  - `has_route_for(self, symbol: str) -> bool` — pure sync, no client construction.
  - `async close_all(self) -> None` — idempotent close of every constructed client.
  - `_client_for_index(self, key_index: int)` — lazy construct from `MEXC_KEY_{N}_API_KEY/SECRET`.

**Module-level singleton:** `mexc_key_router = MexcKeyRouter()`

### execution/chains/__init__.py
**Docstring:** Registry of chain connectors. To-add-a-new-chain instructions inline.

**Module constants:**
- `REGISTERED_CONNECTORS: list[BaseChainConnector] = [ArbitrumConnector(), BaseChainConnectorInstance(), OptimismConnector()]`
- `__all__ = ["REGISTERED_CONNECTORS", "BaseChainConnector", "PoolState", "VenueConfig", "ArbitrumConnector", "BaseChainConnectorInstance", "OptimismConnector"]`

### execution/chains/base_connector.py
**Docstring:** The three contracts every chain connector must satisfy: VenueConfig / PoolState / BaseChainConnector. submit_swap raises NotImplementedError at this layer.

**Dataclasses:**
- `VenueConfig` — `venue: str`, `pool_address: str`, `fee_bps: float`.
- `PoolState` — `connector_id, symbol, venue, reserve_base, reserve_quote, fee_bps, spot_price, depth_usd_1pct, block_number, timestamp, error: Optional[str] = None`.

**Classes:**
- `BaseChainConnector(ABC)` — chain-connector ABC.
  - class attrs: `connector_id = "base"`, `display_name = "Base Chain"`, `rpc_env_var = ""`, `optional = True`, `venues: dict`.
  - `@abstractmethod async get_pool_state(self, symbol: str) -> PoolState`
  - `@abstractmethod async gas_cost_usd(self) -> float`
  - `is_available(self) -> bool` — env var present AND no `<FILL>` pool addresses.
  - `async submit_swap(self, *args, **kwargs)` — raises `NotImplementedError("live execution is a separate build (XCHAIN_LIVE_ENABLED + web3 signing)")`.

**Module constants:** `__all__ = ["VenueConfig", "PoolState", "BaseChainConnector"]`.

### execution/chains/_solidly_volatile.py
**Docstring:** Shared partial implementation for Solidly-fork volatile pools (x*y=k). Used by Aerodrome/Velodrome.

**Module constants:** `_SOLIDLY_POOL_ABI`, `_ERC20_ABI` (minimal ABI fragments).

**Functions:**
- `_try_import_web3()` — lazy import; returns Web3 class or None.

**Classes:**
- `SolidlyVolatilePoolConnector(BaseChainConnector)` — partial impl for Solidly-volatile chains.
  - class attr: `SWAP_GAS_UNITS = 120_000`.
  - `__init__(self)` — builds `self.venues` from `settings.XCHAIN_VENUES[connector_id]`, `_w3=None`, `_decimals_cache={}`.
  - `is_available(self) -> bool` — extends base with web3 importability.
  - `_resolve_w3(self)` — lazy `Web3(Web3.HTTPProvider(rpc))`.
  - `async _decimals(self, addr: str, default: int) -> int`
  - `async get_pool_state(self, symbol: str) -> PoolState` — reads `getReserves`, `token0`, `token1`; orients base=WETH, quote=USDC by decimal count.
  - `_error_state(self, symbol: str, reason: str, *, venue: Optional[VenueConfig] = None, block: int = 0) -> PoolState`
  - `async gas_cost_usd(self) -> float` — `gasPrice * SWAP_GAS_UNITS * (ETH/USD) / 1e18`; inf on failure.

**Module constants:** `__all__ = ["SolidlyVolatilePoolConnector"]`.

### execution/chains/arbitrum.py
**Docstring:** ArbitrumConnector — reads WETH-USDC from Uniswap v3 pool on Arbitrum.

**Module constants:** `_UNISWAP_V3_POOL_ABI`, `_ERC20_ABI`.

**Functions:**
- `_try_import_web3()` — lazy import.

**Classes:**
- `ArbitrumConnector(BaseChainConnector)` — Uniswap v3 reader.
  - class attrs: `connector_id = "arbitrum"`, `display_name = "Arbitrum"`, `rpc_env_var = "ARBITRUM_RPC_URL"`, `optional = True`, `SWAP_GAS_UNITS = 150_000`.
  - `__init__(self)`
  - `is_available(self) -> bool` — extends base with web3 importability.
  - `_resolve_w3(self)`
  - `async _decimals(self, addr: str, default: int) -> int`
  - `async get_pool_state(self, symbol: str) -> PoolState` — slot0 + liquidity → virtual reserves via sqrtPriceX96.
  - `_error_state(self, symbol: str, reason: str, *, venue: Optional[VenueConfig] = None, block: int = 0) -> PoolState`
  - `async gas_cost_usd(self) -> float`

### execution/chains/base_chain.py
**Docstring:** BaseChainConnectorInstance — reads WETH-USDC from Aerodrome on Base. File name is base_chain.py (NOT base.py) to avoid collision with base_connector.py.

**Classes:**
- `BaseChainConnectorInstance(SolidlyVolatilePoolConnector)`
  - class attrs: `connector_id = "base"`, `display_name = "Base (Aerodrome)"`, `rpc_env_var = "BASE_RPC_URL"`, `optional = True`.

**Module constants:** `__all__ = ["BaseChainConnectorInstance"]`.

### execution/chains/optimism.py
**Docstring:** OptimismConnector — reads WETH-USDC from Velodrome on Optimism.

**Classes:**
- `OptimismConnector(SolidlyVolatilePoolConnector)`
  - class attrs: `connector_id = "optimism"`, `display_name = "Optimism (Velodrome)"`, `rpc_env_var = "OPTIMISM_RPC_URL"`, `optional = True`.

**Module constants:** `__all__ = ["OptimismConnector"]`.

## Imports graph

**This package imports from project:**
- `signals.base.Signal` (router.py)
- `database.queries` — `save_trade`, `close_trade`, `get_open_trades`, `log_circuit_breaker`, `get_today_pnl_pct`, `get_consecutive_losses`, `log_arb_opportunity`, `log_arb_trade`, `log_arb_balance_fail`, `mark_arb_opportunity_executed`, `insert_xchain_observation`
- `config.settings` (every non-__init__ file)
- `execution.chains` → `REGISTERED_CONNECTORS`, `BaseChainConnector`, `PoolState`, `VenueConfig`, concrete connectors
- `agents.balance.inventory_state.inventory_state` (lazy import inside `ArbEngine._check_balances`)
- `data_sources.data_sources` (lazy import inside `FundingRateArbEngine` + `fetch_funding_rates`)
- External: `ccxt.async_support` (arb_engine.py, funding_engine.py, mexc_key_router.py — all guarded by try/except), `web3` (chain connectors — lazy via `_try_import_web3`)

**This package is imported by:**
- `core.bot` — imports `KillSwitch`, `PositionManager`, `OrderRouter`.
- `main.py` — imports `KillSwitch`.
- `agents/__init__.py` — imports `KillSwitch` and `ArbEngine`.
- `agents/scalping_agent.py` — imports `mexc_key_router`.
- `agents/funding_arb_agent.py` — imports from `execution.funding_engine`.
- `agents/crosschain_agent.py` — imports `CrossChainArbEngine`, `InventoryTarget`, `compute_inventory_targets`, `REGISTERED_CONNECTORS`.
- Tests: `test_arb_engine.py`, `test_funding_arb.py`, `test_crosschain_engine.py`, `test_chain_connectors.py`, `test_inventory_targets.py`, `test_mexc_key_router.py`, `test_balance_agent.py`, `test_funds.py`, `test_crosschain_agent.py`.

## Plugin registrations

`execution/chains/__init__.py:REGISTERED_CONNECTORS`:
- `ArbitrumConnector()` — Arbitrum (Uniswap v3 WETH-USDC pool).
- `BaseChainConnectorInstance()` — Base (Aerodrome volatile WETH-USDC pool).
- `OptimismConnector()` — Optimism (Velodrome volatile WETH-USDC pool).

The CrossChainArbEngine filters this list to those whose `is_available()` returns True (env var set + no `<FILL>` pool addresses + web3 importable).

## Sim vs live behaviour

`SIM_MODE` is consulted in:
- `execution/router.py:17` — `self._sim_mode = settings.SIM_MODE`; `execute()` dispatches sim vs live at `router.py:44`. `_sim_execute` writes a Trade row with `sim_mode=True`. **`_live_execute` is a stub** that logs a warning and returns `None` — live order execution is unimplemented in the main signal-track router.
- `execution/arb_engine.py:178` — `ArbEngine.sim_mode = settings.SIM_MODE` unless overridden. `_execute_arb` branches at line 512: sim uses `_sim_fills` (depth-aware slippage model), live uses `_live_fills` which actually fires `create_market_buy_order` + `create_market_sell_order` via `asyncio.gather`. **The live path is implemented** here (unlike the signal-track router); `_check_balances` enforces a hard capital gate first.
- `execution/arb_engine.py:841` — `FundingRateArbEngine.sim_mode = settings.SIM_MODE`. The execution path is not yet wired (`_scan_loop` reads funding rates but does not open positions — TODO).
- `execution/crosschain_engine.py:254` — `CrossChainArbEngine.sim_mode = settings.SIM_MODE`. Engine is OBSERVATION-ONLY: `submit_swap` on every connector raises `NotImplementedError` and the engine never calls it; only logs to `xchain_observations`. Live build gated by `settings.XCHAIN_LIVE_ENABLED` (a future PR).
- `execution/funding_engine.py:155` — `FundingEngine.open()` runs `assert settings.SIM_MODE` when `settings.FUNDING_OBSERVATION_MODE` is True (defence in depth: even if the agent misroutes a call, no live order can be placed). `_place` at line 400 logs a stub for live mode and returns None.
- `execution/kill_switch.py` — sim path uses `trade.entry_price` as exit price (TODO line 69); live path calls `exchange_manager.market_close(...)`.

Status of `_live_execute` stubs:
- `OrderRouter._live_execute` (router.py:77-79) — STUB, just `logger.warning("LIVE execution — real money"); return None`.
- `FundingEngine._place` live branch (funding_engine.py:400-405) — STUB, logs and returns None.
- `BaseChainConnector.submit_swap` (base_connector.py:140-151) — raises `NotImplementedError` (deliberate).
- `ArbEngine._live_fills` — **implemented** (real ccxt market order calls).
- `KillSwitch._close_position` live branch — **implemented** (real `exchange_manager.market_close` call) but depends on an exchange_manager that supplies `market_close`.

## Kill switch wiring

Full path from `KillSwitch.engage()` to all-positions-closed:

1. `core/bot.py:39` imports `KillSwitch`. The Bot keypress handler / circuit-breaker chain calls `await kill_switch.engage(reason)`.
2. `execution/kill_switch.py:23` `KillSwitch.engage(reason)`:
   - Guards on `self._active` (re-entry block).
   - Logs CRITICAL `"KILL SWITCH ENGAGED — reason: {reason}"`.
   - Calls `database.queries.get_open_trades()` to enumerate everything currently open.
   - Builds `tasks = [self._close_position(trade) for trade in open_trades]`.
   - `await asyncio.gather(*tasks, return_exceptions=True)` — closes all in parallel.
   - Calls `database.queries.log_circuit_breaker(reason="kill_switch", detail=...)`.
   - Returns `{closed, timestamp, reason, trades}`.
3. `KillSwitch._close_position(trade)` (line 64) per trade:
   - sim: `exit_price = trade.entry_price` (TODO — no live price yet), `pnl_pct = 0.0`.
   - live: `await self._exchange_manager.market_close(exchange, pair, side, size)` → `exit_price = result.get("price", trade.entry_price)`; pnl_pct computed for long/short.
   - Calls `database.queries.close_trade(trade_id, exit_price, exit_reason="kill_switch", pnl_usd, pnl_pct)`.
4. The agent-side arb engines (`ArbEngine.close_all_positions`, `FundingRateArbEngine.close_all_positions`, `CrossChainArbEngine.close_all_positions`) are SEPARATE — they're not called by `KillSwitch`. The agent wrappers invoke their own `close_all_positions()` paths. The `KillSwitch.engage()` path closes only the main-bot Trade rows.

Files involved, in order: `core/bot.py` (caller) → `execution/kill_switch.py:KillSwitch.engage` → `database/queries.get_open_trades` → `execution/kill_switch.py:KillSwitch._close_position` (per trade in parallel) → (live only) `exchange_manager.market_close` → `database/queries.close_trade` → `database/queries.log_circuit_breaker`.

## Tests

Test files in `tests/` that import from `execution/`:

### tests/test_arb_engine.py
- `test_gross_gap_calculation` — pure `gross_gap_pct` math.
- `test_net_gap_after_fees_subtracts_both_legs` — `net_gap_pct` subtracts both legs' fees from gross.
- `test_min_gap_threshold_bitget_special_case` — Bitget unlocks `ARB_MIN_GAP_PCT`; others use fallback.
- `test_opportunity_below_threshold_not_returned` — sub-threshold gap returns None from `_find_best_opportunity`.
- `test_opportunity_above_threshold_returned` — above-threshold gap is returned.
- `test_live_fills_runs_both_legs_concurrently` — `_live_fills` fires both legs via gather.
- `test_per_symbol_lock_blocks_second_attempt` — `_symbol_locks` blocks re-entry on same pair.
- `test_circuit_breaker_halts_on_daily_loss` — `_cb_triggered` flips on daily-loss threshold.
- `test_circuit_breaker_halts_on_consecutive_losses` — `_cb_triggered` flips on streak.
- `test_circuit_breaker_clear_when_under_thresholds` — baseline no-halt.
- `test_sim_fills_apply_slippage_model` — `_sim_fills` applies slippage.
- `test_execute_arb_uses_sim_fills_in_sim_mode` — `_execute_arb` routes to sim path in sim mode.
- `test_new_exchange_in_fee_map_evaluated` — added venue auto-discovered.
- `test_dashboard_add_arb_called_on_completed_trade` — dashboard notified on success.
- `test_dashboard_not_called_on_failed_trade` — dashboard skipped on failure.
- `test_balance_check_blocks_execution_when_insufficient_funds` — `_check_balances` returns False.
- `test_balance_check_logs_miss_to_db` — `log_arb_balance_fail` written.
- `test_slippage_model_increases_with_position_size` — `slippage_pct` monotonic in size.
- `test_slippage_model_clamps_to_min_max` — `[ARB_SLIPPAGE_MIN_PCT, ARB_SLIPPAGE_MAX_PCT]` clamp.
- `test_dynamic_sizing_scales_with_gap_width` — `_dynamic_base_position * gap_ratio`.
- `test_dynamic_sizing_caps_at_multiplier_cap` — capped at `ARB_SIZE_MULTIPLIER_CAP`.
- `test_opportunity_log_records_unexecuted_gaps` — sub-threshold gaps still logged.
- `test_opportunity_log_records_executed_gaps_with_trade_id` — executed flag + trade_id linked.
- `test_funding_arb_engine_pulls_from_data_sources` — FundingRateArbEngine.fetch_funding_rates returns aggregator dict.
- `test_funding_arb_engine_returns_empty_when_no_coinglass_data` — empty {} when aggregator empty.
- `test_funding_arb_circuit_breaker_halts_on_daily_loss` — FundingRateArbEngine breaker.
- `test_funding_arb_circuit_breaker_zero_alloc_noop` — zero alloc skips % rule.
- `test_funding_arb_initial_allocation_from_fund_constant` — `_capital_allocation` initialised from `FUND_ARB_CAPITAL`.
- `test_funding_arb_set_capital_allocation_changes_field` — setter writes.
- `test_funding_arb_breaker_scales_with_live_allocation` — halt scales with live alloc.
- `test_funding_arb_breaker_zero_allocation_no_divide_by_zero` — guard.
- `test_funding_arb_fund_claim_visible_to_balance_agent` — fund claim visible in InventoryState.

### tests/test_funding_arb.py
- `test_funding_apr_annualisation_is_1095` — `funding_apr = rate_8h * 1095`.
- `test_scan_builds_delta_neutral_per_symbol` — scan emits one delta_neutral opp per symbol.
- `test_filter_drops_below_min_apr` — `_filter` floor.
- `test_negative_funding_builds_reverse_carry_variant` — negative APR → variant="reverse_carry".
- `test_filter_uses_abs_funding_apr` — symmetric filter.
- `test_filter_drops_subthreshold_negative` — sub-threshold negative skipped.
- `test_engine_binance_client_uses_future_market_type` — `defaultType=future` required.
- `test_open_refuses_reverse_carry_variant` — observation-only guard.
- `test_depth_gate_rejects_thin_oi` — `depth_ok` gate.
- `test_open_fires_legs_concurrently` — both legs via gather.
- `test_per_symbol_lock_prevents_double_open` — re-entry guard.
- `test_semaphore_caps_concurrent_opens` — semaphore enforced.
- `test_sim_fill_applies_slippage` — `_place` sim slippage.
- `test_exit_reason_funding_decay_first` — priority ordering.
- `test_exit_reason_basis_blowout` / `test_exit_reason_margin_breach` / `test_exit_reason_venue_health` / `test_exit_reason_max_hold` — each rung.
- `test_exit_reason_priority_order` — full ordering.
- `test_observation_mode_zero_routing` — observation mode bypasses open().
- `test_daily_loss_circuit_breaker_halts` — daily loss breaker.
- `test_daily_loss_circuit_breaker_zero_alloc_noop` — zero alloc no-op.
- `test_daily_loss_halt_scales_with_allocation` — scales with allocation.
- `test_consecutive_loss_circuit_breaker_halts` — streak breaker.
- `test_get_stats_and_availability` — get_stats shape.
- `test_close_all_positions_drains_via_gather` — close path.
- `test_db_roundtrip_observations_and_summary` — DB roundtrip.
- `test_coordinator_picks_up_via_registry` — registered with agent coordinator.

### tests/test_crosschain_engine.py
- `test_evaluate_pair_known_positive_edge` — `_evaluate_pair` returns positive net edge.
- `test_evaluate_pair_known_negative_edge` — negative spread caught.
- `test_optimal_notional_positive_when_spread_exists` — `optimal_notional_constant_product` positive.
- `test_capped_optimal_notional_respects_slippage_tolerance` — tolerance cap.
- `test_capped_optimal_notional_respects_max_position_cap` — position cap.
- `test_estimated_slippage_scales_linearly` — two-leg slippage linear.
- `test_gas_breakeven_at_specified_floor` — `gas_breakeven_usd` math.
- `test_evaluate_pair_refuses_300_at_5bps_budget` — below-breakeven floor blocks entry.
- `test_evaluate_pair_allows_600_at_5bps_budget` — above-breakeven allowed.
- `test_circuit_breaker_halts_on_daily_loss` — `_cb_triggered`.
- `test_circuit_breaker_zero_alloc_noop` — zero alloc no-op.
- `test_circuit_breaker_halts_on_consecutive_losses` — streak.
- `test_circuit_breaker_not_triggered_at_baseline` — baseline clean.
- `test_xchain_initial_allocation_from_settings` — initial allocation from XCHAIN_CAPITAL.
- `test_xchain_set_capital_allocation_changes_field` — setter writes.
- `test_xchain_breaker_reads_live_allocation` — breaker uses live value.
- `test_xchain_does_not_register_cex_fund_claim` — InventoryState gets no claim.
- `test_xchain_observation_behaviour_unchanged_under_zero_alloc` — zero-alloc behaves as observation.
- `test_each_evaluation_writes_exactly_one_observation_row` — 1:1 evaluation→row.
- `test_three_connector_evaluations_all_persist` — N×(N-1) pairs persisted.
- `test_skip_observation_records_skip_reason` — skip_reason written.
- `test_gas_breakeven_handles_infinite_gas` — inf handled.
- `test_optimal_notional_zero_when_no_spread` — no spread → 0.

### tests/test_chain_connectors.py
- `test_registry_lists_all_three_chains` — REGISTERED_CONNECTORS has 3 entries.
- `test_every_connector_subclasses_base` — each is a `BaseChainConnector`.
- `test_every_connector_declares_required_attrs` — connector_id, display_name, rpc_env_var set.
- `test_is_available_false_without_rpc_env_var` — env-var gate.
- `test_is_available_false_with_fill_pool_sentinel` — `<FILL>` blocks.
- `test_base_connector_submit_swap_raises_in_observation_mode` — NotImplementedError.
- `test_pool_state_roundtrip_fields` — PoolState fields preserved.
- `test_venue_config_carries_documented_tier` — VenueConfig.fee_bps preserved.

### tests/test_inventory_targets.py
- `test_needs_rebalance_true_when_drift_exceeds_theta` — drift gate.
- `test_needs_rebalance_false_just_under_theta` — under-theta clean.
- `test_weighting_favours_chains_with_more_entries` — weight ordering.
- `test_weighting_floor_prevents_zero_allocation` — `1/(2N)` floor.
- `test_cold_start_falls_back_to_equal_split` — no observations → equal weights.
- `test_observation_mode_capital_zero_emits_zero_targets` — capital=0 → 0 targets.
- `test_targets_emitted_per_enabled_chain` — one target per chain.
- `test_inventory_target_dataclass_carries_symbol` — InventoryTarget.symbol round-trips.

### tests/test_mexc_key_router.py
- `test_returns_none_when_symbol_not_in_map` — unmapped symbol.
- `test_returns_none_when_env_var_missing` — missing creds.
- `test_returns_client_when_key_provisioned` — happy path.
- `test_one_client_cached_per_index` — caching.
- `test_missing_env_caches_negative_lookup` — `_missing_indices`.
- `test_any_client_picks_lowest_configured_index` — `any_client` ordering.
- `test_any_client_falls_back_to_unmapped_key_scan` — fallback scan 1..30.
- `test_any_client_returns_none_with_no_keys` — none usable.
- `test_has_route_for_does_not_construct_client` — pure sync check.
- `test_has_route_for_missing_symbol` — unmapped returns False.
- `test_close_all_clears_pool` — `close_all` idempotent.

### tests/test_funds.py (only one execution-related test)
- `test_order_router_sizes_off_signal_fund_not_total` — `OrderRouter._portfolio_value == FUND_SIGNAL_CAPITAL` ring-fence.

### tests/test_balance_agent.py (touches `from execution.arb_engine import ArbEngine`)
- Indirect — used to construct an ArbEngine for BalanceAgent interaction tests; specific test functions are scoped to BalanceAgent behaviour and out of scope here.

## TODOs / FIXMEs / stubs

- `execution/kill_switch.py:69` — `exit_price = trade.entry_price  # TODO: use live price from market data`
- `execution/router.py:77-79` — `_live_execute` is a stub: `logger.warning("LIVE execution — real money"); return None  # Implement when ready for live`
- `execution/router.py:82` — `_get_price` stub: `return None  # MarketData provides this via callback`
- `execution/funding_engine.py:400-405` — `_place` live path is a stub: logs and returns None (per design — Phase 1).
- `execution/funding_engine.py:447-449` — `_basis` returns 0.0 unconditionally (Phase 1 placeholder for Phase 2 cross-venue).
- `execution/funding_engine.py:457-459` — `_basis_blowout` returns False (Phase 1 placeholder).
- `execution/funding_engine.py:468-470` — `_margin_breach` returns False (Phase 1 placeholder; observation never posts margin).
- `execution/arb_engine.py:986-989` — `FundingRateArbEngine._scan_loop` reads funding rates but the execution route is unwired (`# Execution path lands in a follow-up — for now the rate fetch keeps the cache warm`).
- `execution/chains/base_connector.py:149-151` — `submit_swap` raises `NotImplementedError("live execution is a separate build (XCHAIN_LIVE_ENABLED + web3 signing)")` (deliberate observation-mode invariant).
- `execution/chains/base_connector.py:126` — comment about `<FILL>` placeholder sentinel (gate, not a stub).

Total: ~10 TODO/stub/placeholder sites; most are deliberate Phase-1 / observation-mode invariants, only two are flagged as wiring-incomplete (`router._live_execute`, `kill_switch._close_position` sim exit price).

## Known issues observed

- **`OrderRouter._live_execute` is a stub** — the signal-track router has no live order placement implementation. Anything that flips `SIM_MODE = False` and routes through the main signal-track will write nothing to the exchange, but `Bot._on_new_signal` calls `execute()` which logs success only if a `trade_id` is returned (None here) → silent no-op for live signals. The ArbEngine's `_live_fills` path is the only fully live order-placement code in this package.
- **`KillSwitch._close_position` sim path uses `trade.entry_price` as the exit price**, yielding `pnl_pct = 0.0` on every kill-switch closure in sim mode (TODO line 69). This makes kill-switch P&L attribution useless for sim runs.
- **`KillSwitch._close_position` live path** depends on an `exchange_manager` that supplies `market_close(exchange, pair, side, size)`. No exchange_manager class in the codebase exposes that interface (Bot constructs `KillSwitch(exchange_manager=None)` based on `core/bot.py:39`), so the live kill path would raise `AttributeError` on `None.market_close(...)`. The `_close_position` exception handler catches it and the kill swallows silently with `"Failed to close trade {id}: {error}"`.
- **`FundingRateArbEngine._scan_loop` reads but never executes** (line 988). The funding-rate carry inside `arb_engine.py` is a permanently-OFFLINE-for-routing engine until a follow-up phase wires the spot-long/perp-short opener. Status / dashboard / breakers tick correctly but no trades will ever flow from it.
- **`FundingRateArbEngine` has no agent wrapper in `REGISTERED_AGENTS`** (commented at arb_engine.py:858-867); `set_capital_allocation` is the only seam for compounding.
- **`CrossChainArbEngine` is observation-only by construction** — `submit_swap` raises everywhere, gated behind `XCHAIN_LIVE_ENABLED` (a flag that's not yet implemented or honoured anywhere in the engine). The observation rows go to `xchain_observations` only.
- **Magic numbers:**
  - `mexc_key_router.py:88` — hardcoded `range(1, 31)` fallback scan limit (the docstring at line 87 explains why = 30 = realistic MEXC key cap, but it's still a literal in code; CLAUDE.md says no hardcoded constants outside `settings.py`).
  - `_solidly_volatile.py:78` — `SWAP_GAS_UNITS = 120_000` (operationally measured per docstring, but a class-attr literal).
  - `arbitrum.py:90` — `SWAP_GAS_UNITS = 150_000` (same comment).
  - `arb_engine.py:441` — `min(ask_liq, bid_liq) * 0.10` (10% of depth cap) is hardcoded; not in `settings.py`.
  - `arb_engine.py:566` — `factor = min(factor, 10.0)` (10× position-size clamp) is hardcoded.
- **Lazy import inside hot path** — `arb_engine.py:596` imports `agents.balance.inventory_state.inventory_state` inside `_check_balances` (per-execution import). Wrapped in try/except so it fails-open if the import errors — meaning the InventoryState scoped-pause gate silently doesn't fire if the import path breaks.
- **`execution/__init__.py` is 0 bytes** — no exports; everything is imported via fully-qualified paths.
- **`router.py:_get_price`** returns None unconditionally and the comment says "MarketData provides this via callback" — but no callback mechanism exists in `OrderRouter`. `execute()` falls back to `signal.suggested_entry` and errors out with "No price for {pair}" when that's missing.
- **`PositionManager._check_circuit_breakers` reads `settings.CIRCUIT_BREAKERS`** (a dict-of-dicts) while the main signal-Bot path independently maintains its own `CBState` — duplicated breaker logic with no shared source of truth.
- **`CrossChainArbEngine._evaluate_pair` returns `gas_bps=-1.0` and `net_edge_bps=-1.0` when math values are inf** (lines 575-579) — sentinel-by-magic-number; consumers (DB, dashboard) must know to interpret -1.0 as "infinite/unavailable".
- **Submit-swap NotImplementedError vs is_available()** — `is_available()` can return True for a connector (env var set, pool pinned) yet `submit_swap` still raises. This is intentional (observation invariant) but a future operator who pulls a connector for direct use could trip the assertion.

---

# Module Report: signals

## Purpose
The `signals/` package defines the universal `Signal` dataclass (signals/base.py) that every downstream consumer (quality gate, agent, router, DB) operates on; orchestrates three scanners (arbitrage, momentum, reversion) via `SignalEngine` (signals/engine.py) on each scan tick; runs every candidate through `QualityGate.evaluate` (signals/quality_gate.py), which adds regime, session, guard, OFI, sentiment, and macro modifiers in a strict order before checking the score threshold, multi-TF confirmation, dedup, max-active, and R/R floor; and maintains an order-book imbalance scorer (`OFIScorer` / `ofi_scorer` in signals/ofi.py) that produces `OFISnapshot` objects consumed both by individual scanners and by the gate for confluence scoring.

## Files
| File | LOC | One-sentence summary |
| --- | --- | --- |
| signals/__init__.py | 0 | Empty package marker (0 bytes). |
| signals/base.py | 114 | The `Signal` dataclass, score property, expiry/summary helpers, and `to_db_dict()` serializer. |
| signals/engine.py | 118 | `SignalEngine` orchestrates per-strategy arb/momentum/reversion scanners, runs the quality gate, ranks survivors, saves to DB, and fires callbacks. |
| signals/arbitrage.py | 77 | `ArbScanner` walks pair × exchange-pair combinations, finds cross-venue gaps above `ARB_MIN_GAP_PCT_FALLBACK` net of fees, emits arb signals with 3-min dedup/expiry. |
| signals/momentum.py | 132 | `MomentumScanner` checks regime fit, RSI band, volume ratio, breakout detection, multi-TF EMA confirmation, OFI confluence, ATR-based SL/TP. |
| signals/reversion.py | 153 | `ReversionScanner` checks Bollinger band touches, RSI extremes, optional RSI divergence, VWAP stretch, multi-TF RSI confirmation, ATR-based SL with BB-mid TP. |
| signals/ofi.py | 206 | `OFIScorer` singleton tracks EMA-smoothed order-flow imbalance per (pair, exchange), exposes `OFISnapshot.signal_modifier(direction)`, and computes VPIN for jump-risk detection. |
| signals/quality_gate.py | 222 | `QualityGate` singleton runs 10 ordered checks (regime, session, guards, OFI, sentiment, macro, multi-TF, threshold, max-active, dedup, R/R) and returns `(passed, reasons, final_score)`. |

## Public surface

### signals/__init__.py
Empty file. No exports.

### signals/base.py
Docstring: "Signal dataclass — the standard object that flows through the entire system. Every signal type (arb, momentum, reversion) produces one of these."

Class `Signal` (dataclass) — every field with default:
- `pair: str` (no default — required)
- `signal_type: str` (no default — required) — arb | momentum | reversion
- `direction: str` (no default — required) — long | short | arb
- `exchange: str` (no default — required)
- `exchange_b: Optional[str] = None`
- `raw_score: float = 0.0`
- `sentiment_mod: float = 0.0`
- `rsi: Optional[float] = None`
- `macd_hist: Optional[float] = None`
- `bb_position: Optional[float] = None`
- `volume_ratio: Optional[float] = None`
- `arb_gap_pct: Optional[float] = None`
- `tf_5m: bool = False`
- `tf_15m: bool = False`
- `tf_1h: bool = False`
- `sentiment_score: float = 50.0`
- `sentiment_velocity: float = 0.0`
- `indicators: dict = field(default_factory=dict)`
- `created_at: datetime = field(default_factory=datetime.utcnow)`
- `expires_at: Optional[datetime] = None`
- `claude_reasoning: Optional[str] = None`
- `suggested_entry: Optional[float] = None`
- `suggested_sl: Optional[float] = None`
- `suggested_tp: Optional[float] = None`
- `suggested_size_pct: Optional[float] = None`
- `risk_reward: Optional[float] = None`
- `claude_api_cost: float = 0.0`
- `win_probability: Optional[float] = None`
- `db_id: Optional[int] = None`

Properties / methods:
- `score -> float` (property) — `min(100.0, max(0.0, self.raw_score + self.sentiment_mod))`
- `timeframe_confirmations -> int` (property) — sum of the three tf_* booleans
- `is_expired(self) -> bool`
- `summary(self) -> str`
- `to_db_dict(self) -> dict`

### signals/engine.py
Docstring: "Orchestrates all signal scanners."

Class `SignalEngine`:
- `__init__(self, market_data, strategy_name: str = "default")`
- `setup_scanners(self)` — imports + constructs `ArbScanner`, `MomentumScanner`, `ReversionScanner`
- `on_signal(self, fn: Callable)`
- `update_sentiment(self, scores: dict)`
- `update_positions(self, positions: list)`
- `async run_scan(self)`
- `get_active_signals(self) -> list`
- `remove_signal(self, signal)`
- `_expire_signals(self)`
- `switch_strategy(self, name: str)`

Module-level: `logger = logging.getLogger(__name__)`.

### signals/arbitrage.py
Docstring: "Cross-exchange arbitrage scanner — Track A."

Class `ArbScanner`:
- `__init__(self, market_data)`
- `async scan(self, sentiment_scores: dict) -> list`
- `_evaluate_gap(self, pair, ex_buy, ex_sell, prices, sentiment_scores)`

Module-level: `logger`.

### signals/momentum.py
Docstring: "Momentum scanner — Track B."

Class `MomentumScanner`:
- `__init__(self, market_data)`
- `async scan(self, sentiment_scores: dict) -> list`
- `async _evaluate_pair(self, pair, exchange, sentiment_scores)`
- `_detect_breakout(self, df)`
- `_confirms(self, df, direction)`

Module-level: `logger`.

### signals/reversion.py
Docstring: "Mean reversion scanner — Track C."

Class `ReversionScanner`:
- `__init__(self, market_data)`
- `async scan(self, sentiment_scores: dict) -> list`
- `async _evaluate_pair(self, pair, exchange, sentiment_scores)`
- `_detect_divergence(self, df, direction)`
- `_confirms(self, df, direction)`

Module-level: `logger`.

### signals/ofi.py
Docstring summarises Cont/Kukanov/Stoikov (2014) OFI, EMA smoothing, 0.65+ bullish / 0.35- bearish.

Class `OFISnapshot` (dataclass):
- `pair: str`, `exchange: str`, `raw_ofi: float`, `ema_ofi: float`, `direction: str`, `score_mod: float`, `bid_depth: float`, `ask_depth: float`, `vpin: Optional[float] = None`
- `confirms_long(self) -> bool`
- `confirms_short(self) -> bool`
- `signal_modifier(self, trade_direction: str) -> float`

Class `OFIScorer`:
- `__init__(self)` — initialises `_ema`, `_buy_vol`, `_sell_vol`, `_latest` dicts, hard-coded `_vpin_window = 50`
- `update_book(self, pair: str, exchange: str, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> OFISnapshot`
- `update_trades(self, pair: str, exchange: str, buy_vol: float, sell_vol: float) -> Optional[float]`
- `get(self, pair: str, exchange: str) -> Optional[OFISnapshot]`
- `get_best(self, pair: str) -> Optional[OFISnapshot]`
- `is_jump_risk(self, pair: str, exchange: str) -> bool`

Module constants/singletons: `logger`, `ofi_scorer = OFIScorer()` (line 206).

### signals/quality_gate.py
Docstring: "The quality gate is the last filter before a signal reaches Claude. It enforces score threshold, regime fit, session timing, multi-TF confirmation, guard penalties, and deduplication."

Functions:
- `_session_modifier() -> float` — returns session-window score boost or 0.0

Class `QualityGate`:
- `__init__(self)` — initialises `_recent_passed: list[Signal] = []` dedup buffer
- `evaluate(self, signal: Signal, open_positions: list, active_signals: list[Signal]) -> tuple[bool, list[str], float]`
- `_is_duplicate(self, signal: Signal) -> bool`
- `_prune_dedup_buffer(self)`

Module constants/singletons: `logger`, `quality_gate = QualityGate()` (line 222).

## Score composition
`QualityGate.evaluate` (signals/quality_gate.py:51) applies modifiers to a running `score` (seeded from `signal.raw_score` at L67) in this exact order:

1. **Regime fit** (L70-84). If `regime_snap.is_choppy` → hard-return `(False, ["regime is choppy — all signals blocked"], score)` at L74. If signal_type is "momentum" and not `momentum_ok` → hard-return at L78. Same for "reversion"/`reversion_ok` at L81. Otherwise `score += regime_snap.score_modifier` (L84).
2. **Session timing** (L87-90). `score += _session_modifier()` (L88). If `session_mod < -10`, appends a soft "dead zone" reason but does NOT block (L89-90).
3. **Guards** (L93-98). `guard_penalty, guard_reasons = guard_runner.apply_all(signal, open_positions)`; `score += guard_penalty`. If `guard_penalty <= -999` → hard-return at L96 (sentinel for hard-block). Otherwise reasons are merged into the soft list.
4. **OFI** (L101-107). `ofi = ofi_scorer.get_best(signal.pair)`. If present, `score += ofi.signal_modifier(signal.direction)` (L105), and the EMA + modifier are stamped into `signal.indicators`.
5. **Sentiment composite + hard block** (L115-133). Tries to import the `sentiment.sentiment` aggregator; if `is_hard_blocked()` returns truthy → hard-return at L120 with `SENTIMENT_HARD_BLOCK_SKIP_REASON: <reason>`. Otherwise `score += sentiment_aggregator.get_signal_modifier()` and the live modifier is written back to `signal.sentiment_mod` (L128). On any exception the gate falls through to `score += signal.sentiment_mod` (L133) — the scanner's pre-populated value.
6. **5b. Macro modifier** (L141-153). `score += macro_monitor.get_signal_modifier()`. Any exception is swallowed silently; never blocks.
7. **Multi-timeframe confirmation** (L156-161). If `settings.REQUIRE_MULTI_TF_CONFIRM` and `signal.timeframe_confirmations < settings.MIN_TF_CONFIRMATIONS` → hard-return.
8. **Score threshold** (L164-167). If `score < settings.SIGNAL_SCORE_THRESHOLD` → hard-return.
9. **Max active signals** (L170-174). If non-expired active count `>= settings.MAX_ACTIVE_SIGNALS` → hard-return.
10. **Deduplication** (L177-178). If `_is_duplicate(signal)` → hard-return.
11. **Min R/R floor** (L181-196). Only runs when SL/TP/entry are all set on the signal. Computes `rr` (pct-based, not absolute), writes it to `signal.risk_reward`; if `rr < settings.MIN_RISK_REWARD_RATIO` → hard-return.

On success (L199): `signal.raw_score = score`, the signal is appended to the dedup buffer, the buffer is pruned, and `(True, [], score)` is returned.

**Clamping.** The running `score` is NOT clamped inside `evaluate` — it can drift below 0 or above 100 before the threshold check. The only clamp is in `Signal.score` (signals/base.py:27), which is `min(100, max(0, raw_score + sentiment_mod))` — applied later when anything reads the property (e.g., `to_db_dict()` line 92). Scanners also self-clamp their own `raw_score` ceilings: arb at 95 (arbitrage.py:54), momentum at 95 (momentum.py:76), reversion at 92 (reversion.py:97).

## Signal.to_db_dict() coverage

### Fields on `Signal` (dataclass)
pair, signal_type, direction, exchange, exchange_b, raw_score, sentiment_mod, rsi, macd_hist, bb_position, volume_ratio, arb_gap_pct, tf_5m, tf_15m, tf_1h, sentiment_score, sentiment_velocity, indicators, created_at, expires_at, claude_reasoning, suggested_entry, suggested_sl, suggested_tp, suggested_size_pct, risk_reward, claude_api_cost, win_probability, db_id.

Derived properties: `score`, `timeframe_confirmations`.

### Keys emitted by `to_db_dict()`
pair, exchange, exchange_b, signal_type, direction, score (the clamped property — NOT raw_score), passed_gate=True (hard-coded), rsi, macd_hist, bb_position, volume_ratio, arb_gap_pct, sentiment_score, sentiment_mod, sentiment_velocity, tf_5m_confirm, tf_15m_confirm, tf_1h_confirm, indicators_json, claude_reasoning, claude_suggested_entry, claude_suggested_sl, claude_suggested_tp, claude_suggested_size, claude_risk_reward, claude_api_cost_usd, timestamp.

### Signal fields NOT mirrored to DB via to_db_dict()
- `raw_score` — only the clamped `score` property is sent; the unclamped pre-clamp value is lost.
- `expires_at` — never serialised.
- `win_probability` — not in the dict (Prediction model owns it via FK).
- `db_id` — intentional (set after insert).

### `database.models.Signal` columns NOT populated by `to_db_dict()`
- `id` (autoincrement)
- `user_action`, `user_action_at`, `skip_reason` — populated later by the bot's approval flow.
- `price_at_signal`, `price_1h`, `price_4h`, `price_24h` — backfilled by the future-price tracker loop.
- `outcome`, `outcome_pnl_pct` — set when the trade closes.
- `profile`, `strategy` — must be patched in by `save_signal()` (not by the dataclass).

`passed_gate=True` is hardcoded in `to_db_dict()` (L93) — so any caller that uses `to_db_dict()` is implicitly asserting the signal already passed; failing-gate rows would need a different serialiser.

## Imports graph

### Imports from project (other modules pulled in by signals/*.py)
- signals/engine.py → `signals.base`, `strategies` (`get_strategy`), `database.queries.save_signal`, `config.settings`, plus lazy imports of `signals.arbitrage.ArbScanner`, `signals.momentum.MomentumScanner`, `signals.reversion.ReversionScanner`, `signals.quality_gate.quality_gate`.
- signals/arbitrage.py → `signals.base`, `config.settings`.
- signals/momentum.py → `signals.base`, `core.regime_detector.regime_detector`, `signals.ofi.ofi_scorer`, `config.settings`.
- signals/reversion.py → `signals.base`, `core.regime_detector.regime_detector`, `signals.ofi.ofi_scorer`, `config.settings`.
- signals/ofi.py → `config.settings` only.
- signals/quality_gate.py → `signals.base`, `core.regime_detector.regime_detector` (+ `CHOPPY` constant, unused after import), `core.guards.guard_runner`, `config.settings`, plus lazy `signals.ofi.ofi_scorer`, `sentiment.sentiment` aggregator, `macro.macro_monitor`.

### Imported by (grep `from signals`)
- core/bot.py (lines 42-43): `SignalEngine`, `ofi_scorer`.
- core/market_data.py (line 22): `ofi_scorer`.
- core/agent.py (line 12): `Signal`.
- core/guards.py (line 17): `Signal`.
- execution/router.py (line 8): `Signal`.
- strategies/{base_strategy, default, custom, arb_only, scalper}.py: `Signal`.
- tests/test_quality_gate.py: `Signal`, `QualityGate`.
- tests/test_signal_arbitrage.py: `ArbScanner`.
- tests/test_data_sources.py (lazy lines 756, 770, 793): `Signal`, `QualityGate`.

## Tests
- `tests/test_quality_gate.py` — three tests for QualityGate:
  - `test_sentiment_hard_block_skips_with_skip_reason` — aggregator `is_hard_blocked()` truthy → gate short-circuits with `SENTIMENT_HARD_BLOCK_SKIP_REASON` reason.
  - `test_sentiment_composite_modifier_applied` — aggregator returns −10 → final score = raw 80 − 10 = 70 and the signal still passes.
  - `test_sentiment_default_zero_when_aggregator_blows_up` — aggregator raises → gate falls through to the scanner-set `signal.sentiment_mod` (−5), final score = 75.
- `tests/test_signal_arbitrage.py` — three async tests for `ArbScanner.scan`:
  - `test_evaluate_gap_coerces_string_prices_without_raising` — regression for ccxt string-priced returns; 100.0 vs "101.5" still produces an arb signal.
  - `test_evaluate_gap_skips_unparseable_prices_silently` — None / garbage strings yield empty list, no exception.
  - `test_evaluate_gap_emits_nothing_below_threshold` — 0.1% raw gap below 0.35% fallback threshold → no signal.
- `tests/test_data_sources.py` — two lightweight gate ↔ macro integration tests (defined at lines 765 / 791):
  - `test_quality_gate_consumes_macro_modifier` — `macro_monitor.get_signal_modifier` stubbed to −10; gate must subtract from score.
  - `test_quality_gate_missing_macro_does_not_block` — macro raising must not block the gate.

## TODOs / FIXMEs / stubs
- `signals/__init__.py` is a 0-byte empty file (no docstring, no exports) — effectively a stub.
- No `TODO` / `FIXME` / `XXX` / `HACK` comments anywhere in `signals/*.py`.

## Known issues observed
- **`raw_score` is not idempotent across gate calls.** `QualityGate.evaluate` mutates `signal.raw_score = score` on success (L199), so a second call on the same Signal would compound regime/session/guard/OFI/sentiment/macro modifiers a second time. The dedup buffer normally prevents this, but anything that re-enters the gate bypasses that.
- **`Signal.score` clamps to [0, 100] but `raw_score` does not, and the gate compares the unclamped `score` against `SIGNAL_SCORE_THRESHOLD`** (quality_gate.py:164). A signal whose modifiers push `raw_score` above 100 (or below 0) will pass/fail by the unclamped value, then be persisted at the clamped value via `to_db_dict()` — historical analysis sees a different number than the decision was made on.
- **Lost field on persist: `raw_score`** — `to_db_dict()` writes only the clamped `score` property, so the unclamped post-modifier value (the one the gate actually compared) is unrecoverable from the DB row.
- **`expires_at` never persisted.** The dataclass tracks it but `to_db_dict()` omits it, so on a process restart any pending signals lose their expiry timestamps.
- **Sentiment modifier is double-applied for legacy callers.** When the aggregator works the gate writes its modifier into `signal.sentiment_mod` (quality_gate.py:128). When the aggregator raises, the gate adds `signal.sentiment_mod` (the scanner-set value) to `score` (L133). But arbitrage/momentum/reversion scanners already set `sentiment_mod` themselves and `Signal.score` adds it again via the property — so any consumer that reads `Signal.score` after the gate gets sentiment counted twice unless the gate succeeded.
- **`CHOPPY` constant imported but unused** (quality_gate.py:16). The code instead reads `regime_snap.is_choppy`, so the named constant is dead.
- **`QualityGate.evaluate` returns `(False, [...], score)` on most blocks, but the score it returns has partial modifiers applied** (whatever ran before the block). Callers that log the failing score are seeing a different number depending on which check tripped — there is no normalisation.
- **`SignalEngine.setup_scanners()` must be called manually** before `run_scan()`; `__init__` only sets `_scanners = []` and never wires the three scanner attributes. Any caller that forgets `setup_scanners()` will `AttributeError` on `_arb_scanner` the first time `should_run_arb()` is true.
- **`ofi_scorer` is a module-level singleton with no eviction.** `_ema`, `_buy_vol`, `_sell_vol`, `_latest` grow unbounded as new (pair, exchange) keys appear; nothing reaps stale keys. Long-running processes accumulate memory.
- **`OFI` is consumed twice when a momentum/reversion signal reaches the gate.** Scanners pre-add `ofi_mod` into `sentiment_mod` (momentum.py:89, reversion.py:110); the gate then independently calls `ofi.signal_modifier(signal.direction)` again and adds it to `score` (quality_gate.py:104-105). Same OFI contribution applied twice.
- **Arb dedup uses a single-direction key** (`f"{pair}_{ex_buy}_{ex_sell}"`, arbitrage.py:50) but the scanner emits both directions (L29-30). The reverse-direction signal is emitted at most once per 3 min only by virtue of its own key — fine in isolation, but ordering means a `binance→kraken` opportunity at T+0 and a `kraken→binance` opportunity at T+30s both fire even though the gap inverted within seconds (likely the same noisy print).
- **`SignalEngine.update_sentiment` stores into `_sentiment_scores` but the gate path reads sentiment from the `sentiment.sentiment` aggregator singleton** (quality_gate.py:116) — the dict passed into `run_scan` is only consumed by the three scanner `.scan(sentiment_scores)` calls. The two sentiment paths are not guaranteed to agree.
- **Empty `signals/__init__.py`** — nothing is re-exported, so consumers must use the longer fully-qualified import (e.g., `from signals.base import Signal`). Minor ergonomics issue; not a bug.

---

# Module Report: strategies

## Purpose

The `strategies/` package implements the strategy registry pattern called out in
`CLAUDE.md`. An `ABC` base class (`BaseStrategy`) declares the contract every
strategy must implement; concrete subclasses live in sibling modules
(`default.py`, `arb_only.py`, `scalper.py`, `custom.py`); a module-level
`STRATEGIES` dict in `strategies/__init__.py` maps a string key to the class,
and `get_strategy(name)` constructs an instance on demand.

A strategy is a thin, declarative policy object — it does **not** compute
indicators. It controls four things:

1. Which scanners run on each tick (`should_run_arb / momentum / reversion`).
2. How a per-signal score is adjusted before the QualityGate threshold is
   applied (`score_signal`).
3. How the surviving signals are ordered for Claude / the user
   (`rank_signals`).
4. Optional hooks to hard-filter (`filter_signal`), resize
   (`get_position_size_pct`), or retune SL/TP (`get_stop_loss_pct`,
   `get_take_profit_pct`).

`SignalEngine` and `Bot` look the strategy up by name (from the active profile
or `settings.ACTIVE_STRATEGY`) and dispatch through these methods.

## Files

| File                          | LOC | One-sentence summary                                                                 |
|-------------------------------|----:|--------------------------------------------------------------------------------------|
| `strategies/__init__.py`      |  23 | Registry: imports the four concrete strategies, exposes `STRATEGIES` and `get_strategy(name)`. |
| `strategies/base_strategy.py` |  74 | Abstract `BaseStrategy` ABC declaring the five required methods and three optional override hooks. |
| `strategies/arb_only.py`      |  32 | Arbitrage-only strategy — disables momentum/reversion scanners, ranks by `arb_gap_pct`, allows 1.5× position size up to 5%. |
| `strategies/custom.py`        |  65 | User-edit template — all hooks are stubs that return defaults, with inline examples in docstrings. |
| `strategies/default.py`       |  30 | Balanced strategy — all three scanners on, arb signals get +5 score and rank-priority tiebreaker. |
| `strategies/scalper.py`       |  48 | 5m momentum scalp strategy — rewards `tf_5m` + high `volume_ratio`, requires 5m confirmation and `volume_ratio>=2.5` for momentum, tightens SL to 0.7× and TP to 0.75×. |

(LOC counts include docstrings and blank lines.)

## Public surface

### `strategies/__init__.py`

Module docstring: `"Strategy registry. Add new strategies here."`

Constants:
- `STRATEGIES: dict[str, type[BaseStrategy]]` — registry mapping name → class
  (see [Plugin registrations](#plugin-registrations)).

Functions:
- `get_strategy(name: str) -> BaseStrategy` — factory; raises
  `ValueError(f"Unknown strategy '{name}'. Available: {list(STRATEGIES.keys())}")`
  if not registered.

Re-exports: `BaseStrategy`, `DefaultStrategy`, `ArbOnlyStrategy`,
`ScalperStrategy`, `CustomStrategy` are all imported at module top, but only
`STRATEGIES` and `get_strategy` are intended as the public API.

### `strategies/base_strategy.py`

Module docstring: `"Abstract base class all strategies must implement. Plug in
any strategy by inheriting this and registering it."`

```python
class BaseStrategy(ABC):
    name:        str = "base"
    description: str = "Base strategy — do not use directly"

    @abstractmethod
    async def should_run_arb(self) -> bool: ...
    @abstractmethod
    async def should_run_momentum(self) -> bool: ...
    @abstractmethod
    async def should_run_reversion(self) -> bool: ...
    @abstractmethod
    def score_signal(self, signal: Signal) -> float: ...
    @abstractmethod
    def rank_signals(self, signals: list[Signal]) -> list[Signal]: ...

    def filter_signal(self, signal: Signal) -> bool: ...                     # default True
    def get_position_size_pct(self, signal: Signal, base_pct: float) -> float: ...  # default base_pct
    def get_stop_loss_pct(self, signal: Signal, base_sl: float) -> float: ...       # default base_sl
    def get_take_profit_pct(self, signal: Signal, base_tp: float) -> float: ...     # default base_tp
```

`Optional` from `typing` is imported but unused.

### `strategies/arb_only.py`

```python
class ArbOnlyStrategy(BaseStrategy):
    name        = "arb_only"
    description = "Cross-exchange arbitrage only. No directional trades."

    async def should_run_arb(self)       -> bool   # True
    async def should_run_momentum(self)  -> bool   # False
    async def should_run_reversion(self) -> bool   # False
    def score_signal(self, signal: Signal) -> float                          # passthrough
    def rank_signals(self, signals: list[Signal]) -> list[Signal]            # sort by (arb_gap_pct, score) desc
    def get_position_size_pct(self, signal: Signal, base_pct: float) -> float  # min(base*1.5, 0.05)
```

### `strategies/custom.py`

```python
class CustomStrategy(BaseStrategy):
    name        = "custom"
    description = "User-defined strategy. Edit strategies/custom.py."

    async def should_run_arb(self)       -> bool                             # True
    async def should_run_momentum(self)  -> bool                             # True
    async def should_run_reversion(self) -> bool                             # True
    def score_signal(self, signal: Signal) -> float                          # passthrough
    def rank_signals(self, signals: list[Signal]) -> list[Signal]            # sort by score desc
    def filter_signal(self, signal: Signal) -> bool                          # True
    def get_position_size_pct(self, signal: Signal, base_pct: float) -> float  # passthrough
    def get_stop_loss_pct(self, signal: Signal, base_sl: float) -> float     # passthrough
    def get_take_profit_pct(self, signal: Signal, base_tp: float) -> float   # passthrough
```

All methods are template stubs with worked-example docstrings (e.g. "boost
signals with RSI divergence", "only trade BTC and ETH", "go bigger on
high-conviction signals").

### `strategies/default.py`

```python
class DefaultStrategy(BaseStrategy):
    name        = "default"
    description = "All signal types active. Ranked by score. Arb gets slight priority."

    async def should_run_arb(self)       -> bool                             # True
    async def should_run_momentum(self)  -> bool                             # True
    async def should_run_reversion(self) -> bool                             # True
    def score_signal(self, signal: Signal) -> float                          # +5 if signal_type == "arb", capped at 100
    def rank_signals(self, signals: list[Signal]) -> list[Signal]            # sort by (type_priority, score) desc
```

`type_priority` map: `{"arb": 2, "momentum": 1, "reversion": 0}`.

### `strategies/scalper.py`

```python
class ScalperStrategy(BaseStrategy):
    name        = "scalper"
    description = "5m momentum scalps. Tight SL/TP. High volume confirmation required."

    async def should_run_arb(self)       -> bool                             # True
    async def should_run_momentum(self)  -> bool                             # True
    async def should_run_reversion(self) -> bool                             # False
    def score_signal(self, signal: Signal) -> float
        # +8 if signal.tf_5m
        # -10 if signal.tf_1h and not signal.tf_5m
        # +5  if signal.volume_ratio >= 3.0
        # capped at 100
    def rank_signals(self, signals: list[Signal]) -> list[Signal]            # sort by score desc
    def filter_signal(self, signal: Signal) -> bool
        # momentum requires tf_5m AND volume_ratio >= 2.5
    def get_stop_loss_pct(self, signal: Signal, base_sl: float) -> float     # base_sl * 0.7
    def get_take_profit_pct(self, signal: Signal, base_tp: float) -> float   # base_tp * 0.75
```

## Plugin registrations

`strategies/__init__.py:12-17` defines the registry:

```python
STRATEGIES: dict[str, type[BaseStrategy]] = {
    "default":  DefaultStrategy,
    "arb_only": ArbOnlyStrategy,
    "scalper":  ScalperStrategy,
    "custom":   CustomStrategy,
}
```

| Key       | Class                 | Behaviour                                                                                     |
|-----------|-----------------------|-----------------------------------------------------------------------------------------------|
| `default` | `DefaultStrategy`     | All three scanners on; arb gets +5 score and rank-tie priority over momentum/reversion.        |
| `arb_only`| `ArbOnlyStrategy`     | Only arb scanner runs; signals ranked by gap%, then score; position size up to 5% (1.5× base). |
| `scalper` | `ScalperStrategy`     | Arb + momentum on, reversion off; momentum requires `tf_5m` + `volume_ratio>=2.5`; SL×0.7, TP×0.75; bonuses for `tf_5m` and high volume. |
| `custom`  | `CustomStrategy`      | Editable template — defaults behave identically to `default` minus the arb bonus.              |

Note: the registry uses lowercase keys (e.g. `"arb_only"`, `"scalper"`) but the
class `name` attribute on `ScalperStrategy` is `"scalper"` while the registry
key is also `"scalper"` — consistent. See **Known issues** below for the
profile-key discrepancy.

## Strategy comparison

| Strategy   | should_run_arb | should_run_momentum | should_run_reversion | should_run_scalp\* | score_signal effect                                                       | rank_signals override                                                |
|------------|----------------|---------------------|----------------------|--------------------|----------------------------------------------------------------------------|----------------------------------------------------------------------|
| `default`  | True           | True                | True                 | n/a                | `+5` if `signal_type == "arb"`, capped at 100                              | `(type_priority[arb=2, mom=1, rev=0], score)` desc                   |
| `arb_only` | True           | False               | False                | n/a                | passthrough                                                                | `(arb_gap_pct or 0, score)` desc                                     |
| `scalper`  | True           | True                | False                | n/a                | `+8` tf_5m, `-10` tf_1h-only, `+5` volume_ratio>=3.0, capped at 100        | `score` desc                                                         |
| `custom`   | True           | True                | True                 | n/a                | passthrough                                                                | `score` desc                                                         |

\* `should_run_scalp` is **not** part of the `BaseStrategy` contract — no
strategy defines it and no caller queries it. Scalping is run by a separate
agent (`agents/scalping_agent.py`) keyed off `settings.STRATEGY_EXCHANGE_MAP`,
not by these strategy classes.

Optional hook overrides:

| Strategy   | filter_signal                                  | get_position_size_pct        | get_stop_loss_pct | get_take_profit_pct |
|------------|------------------------------------------------|------------------------------|-------------------|---------------------|
| `default`  | inherited (True)                               | inherited (passthrough)      | inherited         | inherited           |
| `arb_only` | inherited (True)                               | `min(base_pct * 1.5, 0.05)`  | inherited         | inherited           |
| `scalper`  | momentum requires `tf_5m` and `volume_ratio>=2.5` | inherited                 | `base_sl * 0.7`   | `base_tp * 0.75`    |
| `custom`   | overridden but returns True                    | overridden but returns base  | overridden, passthrough | overridden, passthrough |

## Imports graph

### Imports from project

All four concrete strategies plus `base_strategy.py` import only one
project-internal symbol:

```python
from signals.base import Signal
```

`strategies/__init__.py` imports the four sibling classes and `BaseStrategy`.
No imports from `config/`, `core/`, `database/`, `execution/`, `profiles/`,
`signals/quality_gate.py`, or any agent module. The strategies are pure-data
classifiers over a `Signal`.

### Imported by (`grep "from strategies"`)

| Importer file              | Line | What it pulls in            | Use                                                              |
|---------------------------|-----:|-----------------------------|------------------------------------------------------------------|
| `main.py`                  |   36 | `get_strategy`              | Resolves the CLI `--strategy` flag → instance for the bot.       |
| `signals/engine.py`        |    9 | `get_strategy`              | Looks up the active strategy per scan to gate scanner execution. |
| `core/bot.py`              |  153 | `get_strategy` (lazy import) | Re-resolves strategy when profile/runtime switch fires.          |

No other importers. `audit/cryptobot_1.0/module_reports/core.md:234` documents
the lazy import in `core/bot.py` (deliberate to avoid early-boot import
cycles).

## Tests

A grep of `tests/` for `from strategies`, `import strategies`,
`get_strategy`, `BaseStrategy`, `DefaultStrategy`, `ArbOnlyStrategy`,
`ScalperStrategy`, `CustomStrategy`, and `STRATEGIES` returned **no matches**.

The strategy *names* show up as plain strings in several tests (as DB row
values, `SimpleNamespace` stubs, or fixture parameters) but no test exercises
the strategy classes themselves:

| Test file                          | What it does with the string "strategy"                                                          |
|------------------------------------|---------------------------------------------------------------------------------------------------|
| `tests/test_bot.py`                | `_stub_strategy()` fixture builds a `SimpleNamespace` that mimics the interface (no import).      |
| `tests/test_dashboard.py`          | Stubs `_strategy=SimpleNamespace(name="default")`; references `STRATEGY_EXCHANGE_MAP["scalp"]`.    |
| `tests/test_macro.py`              | Same `_strategy=SimpleNamespace(name="default")` stub.                                            |
| `tests/test_queries.py`            | Asserts `get_signal_win_rate(exclude_strategy="scalp")` segregates fund accounting.               |
| `tests/test_equity_reconstruction.py` | Asserts `get_trade_realized_pnl(exclude_strategy / only_strategy)` filters by strategy column.  |
| `tests/test_web_server.py`         | Seeds trade rows with `strategy="default"` / `strategy="scalp"` to verify dashboard segregation.   |
| `tests/test_funds.py`              | Asserts shape of `settings.STRATEGY_EXCHANGE_MAP` (lives in `config/settings.py`, not here).      |
| `tests/test_scalping_agent.py`     | Asserts the scalping agent honours `STRATEGY_EXCHANGE_MAP["scalp"]` venue list.                   |

**Net coverage of `strategies/`: zero direct unit tests.** No test instantiates
`DefaultStrategy`, `ArbOnlyStrategy`, `ScalperStrategy`, or `CustomStrategy`,
and no test calls `get_strategy()`. The contract is exercised only
indirectly via `signals/engine.py` and `core/bot.py` in integration paths
that themselves stub the strategy out (`tests/test_bot.py::_stub_strategy`).

## TODOs / FIXMEs / stubs

A grep for `TODO|FIXME|XXX|HACK|stub|STUB` across `strategies/` returned
**zero matches**. The module is TODO-clean.

The whole of `strategies/custom.py` is intentionally a stub template (it says
so in its docstring) — every method returns the default value and the
docstrings carry worked examples for the user to copy. It is not flagged as
TODO but is functionally inert.

## Known issues observed

- **No `should_run_scalp` in the contract.** The audit task spec lists a
  `should_run_scalp` column, but `BaseStrategy` only declares
  `should_run_arb / should_run_momentum / should_run_reversion`. Scalping is
  not gated by the strategy at all — it is run by `agents/scalping_agent.py`
  and gated by `settings.STRATEGY_EXCHANGE_MAP["scalp"]` (see
  `tests/test_funds.py:49`, `tests/test_scalping_agent.py:330`). If the design
  intent is for the strategy class to own that switch, the abstract method is
  missing on `BaseStrategy` and on all four subclasses.

- **Class `name` does not match registry key for `scalper`/`ScalperStrategy`.**
  Actually consistent — both are `"scalper"`. *However* `tests/test_queries.py`
  and `tests/test_equity_reconstruction.py` persist trades with
  `strategy="scalp"` (no `-er`), and `STRATEGY_EXCHANGE_MAP` keys on `"scalp"`.
  So the `Trade.strategy` column uses `"scalp"`, the registry key and class
  `name` use `"scalper"`. They are different namespaces (one is the DB
  segregation label written by the scalping agent, the other is the
  user-pickable signal-strategy name), but the near-identical spellings invite
  bugs — e.g. someone calling `get_strategy("scalp")` will get
  `ValueError: Unknown strategy 'scalp'`.

- **`CustomStrategy` duplicates `DefaultStrategy` behaviour** (minus the +5 arb
  bonus). It exists as a template, so this is intentional, but two of the four
  registered strategies are effectively the same policy out of the box. A new
  user setting `ACTIVE_STRATEGY = "custom"` without editing the file gets
  near-default behaviour silently.

- **Dead import:** `from typing import Optional` in
  `strategies/base_strategy.py:8` is unused.

- **Re-exports without `__all__`:** `strategies/__init__.py` imports all four
  concrete classes and `BaseStrategy` at module top, but does not declare
  `__all__`. The public-vs-private contract is implicit; star-imports will pull
  the concrete classes into the consumer namespace.

- **No unit tests.** Every strategy method is uncovered. The arb-rank tiebreak
  (`(arb_gap_pct or 0, score)`), the scalper score-math
  (`+8 / -10 / +5`, capped at 100), and the SL/TP multipliers (`0.7` / `0.75`)
  have no regression test. The only safeguard is that the `Signal` schema
  changes would surface in `to_db_dict()` callers — not in these classes.

- **Hardcoded magic numbers vs `CLAUDE.md` invariant.** `CLAUDE.md` states
  *"`config/settings.py` is the single tuning instrument. Nothing is hardcoded
  elsewhere"*. These strategies violate that: `ArbOnlyStrategy` hardcodes the
  `1.5` size multiplier and the `0.05` cap; `DefaultStrategy` hardcodes the
  `+5` arb bonus and the `type_priority` map; `ScalperStrategy` hardcodes
  `+8`, `-10`, `+5`, `3.0`, `2.5`, `0.7`, `0.75`. None of these are referenced
  from `config/settings.py`. Either the invariant has drifted, or these
  constants should be promoted to `settings.py` slots with the standard
  `# test:` sweep-range comment.

---

# Module Report: database

## Purpose
SQLAlchemy 2.x ORM layer over a single SQLite file (`data/cryptobot.db`, path from `config.settings.DB_PATH`). `db.py` owns the engine, applies WAL mode + `foreign_keys=ON` + `synchronous=NORMAL` via a `connect` event listener, exposes a `get_session()` context manager, and runs `init_db()` (idempotent `create_all`) on every startup. `models.py` defines every ORM table; `queries.py` is the single chokepoint for all feature code — modules call helpers there rather than opening sessions directly. `expire_on_commit=False` is explicitly set so query helpers can return ORM rows that callers continue to access after the `with get_session()` block exits.

## Files
| File | LOC | One-sentence summary |
| --- | --- | --- |
| database/__init__.py | 0 | Empty package marker. |
| database/db.py | 66 | Engine, SQLite PRAGMAs, `init_db()`, `get_session()` context manager. |
| database/models.py | 751 | 22 ORM table definitions (Base + 21 mapped classes). |
| database/queries.py | 2485 | ~90 query helpers — every read/write goes through here. |

## Public surface

### database/__init__.py
Empty (zero bytes). No re-exports.

### database/db.py
**Docstring**: "Database connection, session management, and initialisation."

**Functions**
- `set_sqlite_pragma(dbapi_conn, _)` — `@event.listens_for(engine, "connect")`; executes `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `PRAGMA synchronous=NORMAL` on every new SQLite connection.
- `init_db() -> None` — `Base.metadata.create_all(bind=engine)`; safe to call on every startup. Logs the DB path.
- `get_session() -> Session` — `@contextmanager`; yields a `SessionLocal()`, commits on clean exit, rolls back on exception, always closes.

**Module constants**
- `engine` — `create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False}, echo=False)`.
- `SessionLocal` — `sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)`.
- `logger = logging.getLogger(__name__)`.

Side effect at import: `DB_PATH.parent.mkdir(parents=True, exist_ok=True)`.

### database/models.py
**Docstring**: "All database table definitions using SQLAlchemy ORM."

**Class**: `Base(DeclarativeBase)` — declarative base.

#### `Candle` — `__tablename__ = "candles"`
OHLCV per (exchange, pair, timeframe).

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| exchange | String(20) | NOT NULL | — | |
| pair | String(20) | NOT NULL | — | |
| timeframe | String(5) | NOT NULL | — | |
| timestamp | DateTime | NOT NULL | — | |
| open, high, low, close, volume | Float | NOT NULL | — | |
| num_trades | Integer | nullable | — | |
| rsi, macd, macd_signal, macd_hist, bb_upper, bb_mid, bb_lower, ema_fast, ema_slow, volume_sma | Float | nullable | — | pre-computed indicators |
| created_at | DateTime | nullable | `datetime.utcnow` | |

Indexes: `ix_candles_lookup` on (exchange, pair, timeframe, timestamp).
Relationships: none.

#### `Signal` — `__tablename__ = "signals"`
Every signal emitted by the scan, pass or fail.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| pair | String(20) | NOT NULL | — | |
| exchange | String(20) | nullable | — | |
| exchange_b | String(20) | nullable | — | second venue for arb |
| signal_type | String(20) | NOT NULL | — | arb / momentum / reversion |
| direction | String(5) | nullable | — | long / short / arb |
| score | Float | NOT NULL | — | |
| passed_gate | Boolean | nullable | False | |
| rsi, macd_hist, bb_position, volume_ratio, arb_gap_pct | Float | nullable | — | indicator snapshot |
| sentiment_score, sentiment_mod, sentiment_velocity | Float | nullable | — | |
| tf_5m_confirm, tf_15m_confirm, tf_1h_confirm | Boolean | nullable | — | |
| indicators_json | JSON | nullable | — | |
| claude_reasoning | Text | nullable | — | |
| claude_suggested_entry, claude_suggested_sl, claude_suggested_tp, claude_suggested_size, claude_risk_reward, claude_api_cost_usd | Float | nullable | — | |
| user_action | String(10) | nullable | — | go / skip / modify / expired |
| user_action_at | DateTime | nullable | — | |
| skip_reason | Text | nullable | — | free text |
| price_at_signal, price_1h, price_4h, price_24h | Float | nullable | — | future-price tracker |
| outcome | String(10) | nullable | — | win / loss / breakeven / open |
| outcome_pnl_pct | Float | nullable | — | |
| profile, strategy | String(30) | nullable | — | active at signal time |

Indexes: `ix_signals_timestamp`, `ix_signals_pair`, `ix_signals_type`.
Relationships: `trade -> Trade` (back-populates, uselist=False), `prediction -> Prediction` (back-populates, uselist=False).

#### `Trade` — `__tablename__ = "trades"`
Executed trades (sim + live).

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| signal_id | Integer | nullable | — | FK → `signals.id` |
| timestamp_open | DateTime | NOT NULL | `datetime.utcnow` | |
| timestamp_close | DateTime | nullable | — | |
| pair | String(20) | NOT NULL | — | |
| exchange | String(20) | NOT NULL | — | |
| exchange_b | String(20) | nullable | — | arb closing venue |
| side | String(5) | NOT NULL | — | long / short / arb |
| signal_type | String(20) | nullable | — | |
| entry_price | Float | NOT NULL | — | |
| exit_price | Float | nullable | — | |
| size_usd | Float | NOT NULL | — | |
| size_base | Float | nullable | — | |
| stop_loss, take_profit | Float | nullable | — | |
| fees_usd | Float | nullable | 0.0 | |
| pnl_usd, pnl_pct, hold_minutes | Float | nullable | — | |
| exit_reason | String(20) | nullable | — | tp_hit / sl_hit / manual / kill_switch |
| claude_postmortem | Text | nullable | — | |
| sim_mode | Boolean | NOT NULL | True | |
| profile, strategy | String(30) | nullable | — | |
| order_id_open, order_id_close | String(80) | nullable | — | live exchange ids |

Indexes: `ix_trades_timestamp` (timestamp_open), `ix_trades_pair`.
Relationships: `signal -> Signal` (back-populates).

#### `SentimentSnapshot` — `__tablename__ = "sentiment"`
Aggregated per-coin 5-minute snapshot.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| coin | String(10) | NOT NULL | — | BTC / ETH / SOL / MARKET |
| reddit_score, telegram_score, news_score, fear_greed, google_trends | Float | nullable | — | |
| composite | Float | nullable | — | weighted blend |
| velocity | Float | nullable | — | vs 2h ago |
| post_count | Integer | nullable | — | |
| mention_count | Integer | nullable | — | |

Indexes: `ix_sentiment_lookup` on (coin, timestamp).

#### `SentimentLog` — `__tablename__ = "sentiment_log"`
Per-source raw fetch result.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| source_id | String(30) | NOT NULL | — | |
| score | Float | nullable | — | -100..+100 |
| composite_score | Float | nullable | — | aggregator output at log time |
| hard_block | Boolean | nullable | False | |
| block_reason | Text | nullable | — | |
| confidence | Float | nullable | — | 0–1 |
| raw_data | JSON | nullable | — | |

Indexes: `ix_sentiment_log_lookup` on (source_id, timestamp).

#### `MacroLog` — `__tablename__ = "macro_log"`
One row per `MacroMonitor` regime computation.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| scenario | String(25) | nullable | — | GOLDILOCKS / RISK_OFF / … |
| macro_score | Float | nullable | — | -100..+100 |
| dollar_strength | String(10) | nullable | — | STRONG/NEUTRAL/WEAK |
| risk_appetite | String(10) | nullable | — | RISK_ON/NEUTRAL/RISK_OFF |
| rate_environment | String(12) | nullable | — | TIGHTENING/NEUTRAL/EASING |
| vol_regime | String(10) | nullable | — | CALM/ELEVATED/CRISIS |
| dxy, vix, yield_10y, yield_2y, yield_curve, fed_funds_rate, cpi_yoy | Float | nullable | — | |
| confidence | Float | nullable | — | 0..1 |
| raw_data | JSON | nullable | — | |

Indexes: `ix_macro_log_ts` on (timestamp).

#### `CalendarEvent` — `__tablename__ = "calendar_events"`
Scheduled economic-calendar item.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| event_id | String(80) | NOT NULL, UNIQUE | — | natural key for upserts |
| title | String(120) | NOT NULL | — | |
| country | String(10) | nullable | — | |
| scheduled_utc | DateTime | NOT NULL | — | |
| impact | String(8) | nullable | — | HIGH/MEDIUM/LOW |
| actual, forecast, previous | Float | nullable | — | |
| source_id | String(30) | nullable | — | |
| fetched_at | DateTime | nullable | `datetime.utcnow` | |

Indexes: `ix_calendar_events_lookup` on (scheduled_utc, impact). Plus unique on `event_id`.

#### `ScalpObservationModel` — `__tablename__ = "scalp_observations"`
Per-evaluated scalp candidate (entered or skipped).

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK, autoincrement |
| symbol | String(20) | NOT NULL | — | column-level `index=True` |
| exchange | String(20) | NOT NULL | — | `index=True` |
| timestamp | Float | NOT NULL | — | unix epoch; `index=True` |
| ofi_z | Float | nullable | — | |
| direction | String(10) | nullable | — | |
| strength | String(10) | nullable | — | |
| tfi_confirms | Boolean | nullable | — | |
| raw_tfi | Float | nullable | — | |
| spread_bps | Float | nullable | — | |
| regime | String(20) | nullable | — | |
| round_trip_cost_bps | Float | nullable | — | |
| min_win_rate_required | Float | nullable | — | |
| tp_bps, sl_bps | Float | nullable | — | |
| would_entry | Boolean | nullable | — | `index=True` |
| skip_reason | String(160) | nullable | — | |
| entry_price | Float | nullable | — | |
| exit_price | Float | nullable | 0.0 | |
| exit_time | Float | nullable | 0.0 | unix epoch |
| exit_reason | String(30) | nullable | — | |
| hold_sec | Float | nullable | 0.0 | |
| pnl_bps | Float | nullable | 0.0 | GROSS pre-fee |
| pnl_usd | Float | nullable | 0.0 | GROSS |
| observation_only | Boolean | nullable | True | |
| price_30s, price_1m, price_3m, price_5m | Float | nullable | 0.0 | micro-tracker backfill |
| confluence_score | Integer | nullable | — | v2 diagnostics |
| strength_label | String(16) | nullable | — | v2 |
| cross_exchange_agrees, btc_compatible, adverse_selection_ok, depth_ok, vwap_aligned, htf_aligned, volume_adequate | Boolean | nullable | — | v2 |
| atr_bps | Float | nullable | — | v2 |
| atr_adjusted | Boolean | nullable | — | v2 |
| sl_clamped | String(8) | nullable | — | v2 |
| rr_actual | Float | nullable | — | v2 |
| created_at | DateTime | nullable | `datetime.utcnow` | |

Indexes: column-level on `symbol`, `exchange`, `timestamp`, `would_entry`; composite `ix_scalp_obs_lookup` on (symbol, exchange, timestamp).

#### `DataLog` — `__tablename__ = "data_log"`
Generic DataPoint sink from `data_sources/`.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| source_id | String(30) | NOT NULL | — | |
| metric | String(40) | NOT NULL | — | |
| symbol | String(20) | nullable | — | null = global metric |
| value | Float | nullable | — | |
| raw_data | JSON | nullable | — | |
| error | Text | nullable | — | |

Indexes: `ix_data_log_lookup` on (source_id, metric, symbol, timestamp).

#### `Prediction` — `__tablename__ = "predictions"`
ML model output per signal (phase-2 feature).

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| signal_id | Integer | nullable | — | FK → `signals.id` |
| timestamp | DateTime | nullable | `datetime.utcnow` | |
| model_version | String(20) | nullable | — | |
| win_probability | Float | nullable | — | 0.0–1.0 |
| expected_pnl_pct | Float | nullable | — | |
| confidence | Float | nullable | — | |
| features_json | JSON | nullable | — | |
| correct | Boolean | nullable | — | filled at trade close |

Relationships: `signal -> Signal` (back-populates).
Indexes: none declared.

#### `DailyStats` — `__tablename__ = "daily_stats"`
End-of-day aggregate.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| date | String(10) | NOT NULL, UNIQUE | — | YYYY-MM-DD |
| total_signals, signals_passed_gate, signals_acted_on | Integer | nullable | 0 | |
| total_trades, wins, losses | Integer | nullable | 0 | |
| win_rate | Float | nullable | — | |
| pnl_usd | Float | nullable | 0.0 | |
| pnl_pct | Float | nullable | 0.0 | |
| fees_usd | Float | nullable | 0.0 | |
| best_trade_pnl_pct, worst_trade_pnl_pct, avg_hold_minutes | Float | nullable | — | |
| best_signal_type | String(20) | nullable | — | |
| avg_score_winners, avg_score_losers | Float | nullable | — | |
| portfolio_value_usd, portfolio_peak_usd | Float | nullable | — | |
| claude_api_cost_usd | Float | nullable | 0.0 | |
| sim_mode | Boolean | nullable | True | |
| profile, strategy | String(30) | nullable | — | |

Indexes: implicit from UNIQUE on `date`.

#### `ArbTrade` — `__tablename__ = "arb_trades"`
One row per arb attempt by `execution/arb_engine.py`.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | nullable | `datetime.utcnow` | |
| symbol | String(20) | NOT NULL | — | |
| buy_exchange, sell_exchange | String(20) | nullable | — | |
| buy_price, sell_price, buy_fill, sell_fill | Float | nullable | — | |
| gross_gap_pct, net_gap_pct | Float | nullable | — | |
| size_usd, gross_pnl_usd, net_pnl_usd | Float | nullable | — | |
| execution_ms | Float | nullable | — | |
| status | String(20) | nullable | "executed" | "executed" / "balance_fail" |
| slippage_buy_pct, slippage_sell_pct | Float | nullable | — | |
| sim_mode | Boolean | nullable | True | |
| success | Boolean | nullable | False | |
| error | Text | nullable | — | |

Indexes: `ix_arb_trades_lookup` on (symbol, timestamp).

#### `ArbOpportunity` — `__tablename__ = "arb_opportunities"`
Every above-liquidity gap detected by the arb scan.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| detected_at | DateTime | nullable | `datetime.utcnow` | |
| symbol | String(20) | NOT NULL | — | |
| buy_exchange, sell_exchange | String(20) | nullable | — | |
| gap_pct, threshold_pct | Float | nullable | — | |
| above_threshold | Boolean | nullable | False | |
| depth_buy_usd, depth_sell_usd | Float | nullable | — | |
| executed | Boolean | nullable | False | |
| arb_trade_id | Integer | nullable | — | FK → `arb_trades.id` |

Indexes: `ix_arb_opportunities_lookup` on (symbol, detected_at).

#### `FundingArbTrade` — `__tablename__ = "funding_arb_trades"`
Funding-rate arb attempts by `FundingRateArbEngine`.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | nullable | `datetime.utcnow` | |
| symbol | String(20) | NOT NULL | — | |
| buy_exchange, sell_exchange | String(20) | nullable | — | |
| buy_price, sell_price, buy_fill, sell_fill | Float | nullable | — | |
| gross_gap_pct, net_gap_pct, size_usd, gross_pnl_usd, net_pnl_usd | Float | nullable | — | |
| execution_ms | Float | nullable | — | |
| status | String(20) | nullable | "executed" | |
| slippage_buy_pct, slippage_sell_pct | Float | nullable | — | |
| funding_rate_pct | Float | nullable | — | |
| sim_mode | Boolean | nullable | True | |
| success | Boolean | nullable | False | |
| error | Text | nullable | — | |

Indexes: `ix_funding_arb_trades_lookup` on (symbol, timestamp).

#### `FundingArbObservationModel` — `__tablename__ = "funding_arb_observations"`
Per-evaluated funding arb opportunity (observation-mode).

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | Float | NOT NULL | — | epoch; `index=True` |
| symbol | String(20) | NOT NULL | — | `index=True` |
| variant | String(20) | NOT NULL | — | delta_neutral |
| venue_long, venue_short | String(20) | nullable | — | |
| funding_apr, spread_apr | Float | nullable | — | |
| oi_usd | Float | nullable | — | |
| depth_ok | Boolean | nullable | True | |
| notional_usd, margin_used | Float | nullable | — | |
| basis_at_entry | Float | nullable | 0.0 | |
| projected_funding_per_interval, projected_fees, projected_net_apr | Float | nullable | 0.0 | |
| would_enter | Boolean | nullable | False | `index=True` |
| skip_reason | String(160) | nullable | — | |
| exit_time | Float | nullable | 0.0 | |
| exit_reason | String(30) | nullable | — | |
| hold_sec | Float | nullable | 0.0 | |
| funding_collected, fees_paid, pnl_usd | Float | nullable | 0.0 | realised net |
| observation_only | Boolean | nullable | True | |
| created_at | DateTime | nullable | `datetime.utcnow` | |

Indexes: column-level on `timestamp`, `symbol`, `would_enter`; composite `ix_funding_arb_obs_lookup` on (symbol, timestamp).

#### `XChainObservation` — `__tablename__ = "xchain_observations"`
Per-evaluated cross-chain arb (observation-only).

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | nullable | `datetime.utcnow` | `index=True` |
| symbol | String(20) | NOT NULL | — | `index=True` |
| buy_chain, sell_chain | String(20) | NOT NULL | — | |
| buy_venue, sell_venue | String(30) | nullable | — | |
| notional_usd | Float | nullable | — | |
| spread_bps, rt_fee_bps, gas_bps, slip_bps | Float | nullable | — | |
| bridge_bps | Float | nullable | 0.0 | |
| net_edge_bps, gas_breakeven_usd | Float | nullable | — | |
| would_entry | Boolean | nullable | False | `index=True` |
| skip_reason | String(160) | nullable | — | |
| observation_only | Boolean | nullable | True | |

Indexes: column-level on `timestamp`, `symbol`, `would_entry`; composite `ix_xchain_obs_lookup` on (symbol, timestamp). No P&L columns by design (observation mode).

#### `PortfolioSnapshot` — `__tablename__ = "portfolio_snapshots"`
Cross-agent state every `PORTFOLIO_MONITOR_INTERVAL_SEC`.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| total_equity, total_daily_pnl, total_exposure_pct | Float | nullable | — | |
| agents_running | Integer | nullable | — | |
| portfolio_status | String(20) | nullable | — | HEALTHY/WARNING/HALTED |
| snapshot_json | JSON | nullable | — | full stats dict |

Indexes: `ix_portfolio_snapshots_ts` on (timestamp).

#### `AgentEvent` — `__tablename__ = "agent_events"`
Lifecycle event log per agent.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | nullable | `datetime.utcnow` | |
| agent_id | String(30) | NOT NULL | — | |
| event_type | String(20) | nullable | — | STARTED/STOPPED/HALTED/KILLED/ERROR/SKIPPED |
| detail | Text | nullable | — | |

Indexes: `ix_agent_events_lookup` on (agent_id, timestamp).

#### `CapitalMovement` — `__tablename__ = "capital_movements"`
BalanceAgent rebalance attempts + lifecycle state machine.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| from_fund, to_fund | String(30) | NOT NULL | — | |
| from_exchange, to_exchange | String(30) | nullable | — | schema extension |
| asset | String(10) | nullable | "USDT" | |
| amount_usd | Float | NOT NULL | — | |
| mode | String(8) | nullable | "sim" | sim / live |
| state | String(12) | nullable | "pending" | pending/in_transit/completed/failed |
| initiated_by | String(30) | nullable | — | policy/web_ui/auto/… |
| note | Text | nullable | — | |
| rail_id | String(30) | nullable | — | sim/cex/… |
| network | String(20) | nullable | — | ccxt network |
| transfer_tx_hash | String(120) | nullable | — | live only |
| error | Text | nullable | — | |
| completed_at | DateTime | nullable | — | |

Indexes: `ix_capital_movements_state` on (state, timestamp); `ix_capital_movements_lookup` on (from_fund, to_fund, timestamp).

#### `FundCapitalEfficiency` — `__tablename__ = "fund_capital_efficiency"`
Per-fund return-on-deployed-capital snapshot.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| fund | String(30) | NOT NULL | — | |
| deployed_usd | Float | nullable | 0.0 | |
| realised_return_usd | Float | nullable | 0.0 | |
| return_on_deployed_pct | Float | nullable | 0.0 | |
| starvation_event | Boolean | nullable | False | |
| starvation_detail | String(200) | nullable | — | |

Indexes: `ix_fund_capital_efficiency_lookup` on (fund, timestamp).

#### `CircuitBreakerLog` — `__tablename__ = "circuit_breaker_log"`
One row per circuit-breaker firing.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | nullable | `datetime.utcnow` | |
| reason | String(50) | nullable | — | daily_loss/consecutive_loss/drawdown/kill_switch |
| detail | Text | nullable | — | |
| auto_resume_at | DateTime | nullable | — | |
| manually_resolved | Boolean | nullable | False | |

Indexes: none declared.

### database/queries.py
**Docstring**: "Reusable query functions. Everything that touches the DB goes through here."

**Module constants**
- `_SESSIONS = ("LONDON", "NEW_YORK", "ASIA", "OFF_HOURS")` — trading-session bucket names.

**Functions** (signature + one-line summary)

Candles:
- `get_recent_candles(exchange, pair, timeframe, limit=200)` — read: newest-first Candle rows.
- `save_candle(candle_data: dict)` — write: `session.merge()` on Candle.

Signals:
- `save_signal(signal_data: dict) -> int` — insert Signal, return new id.
- `update_signal_decision(signal_id, action)` — patch user_action + user_action_at.
- `update_signal_outcome(signal_id, outcome, pnl_pct)` — patch outcome + outcome_pnl_pct.
- `update_signal_claude(signal_id, fields: dict)` — patch any of Claude evaluation fields (whitelisted).
- `update_signal_skip(signal_id, reason, price_at_signal=None)` — mark skip with reason + optional price snapshot.
- `update_signal_future_prices(signal_id, fields: dict)` — patch price_1h/4h/24h.
- `get_signals_needing_price_update(hours=24) -> list` — signals with any null future-price slot.
- `get_today_skipped_signals() -> int` — count of today's skips (counts `user_action='skip'` or `skip_reason IS NOT NULL`).
- `get_trade_by_id(trade_id) -> Trade | None`.
- `get_signal_history(days=30, signal_type=None)` — historical GO-action signals.

Trades:
- `save_trade(trade_data: dict) -> int`.
- `close_trade(trade_id, exit_price, exit_reason, pnl_usd, pnl_pct)` — set close fields + hold_minutes.
- `get_open_trades()` — Trade rows with `timestamp_close IS NULL`.
- `get_today_trades()` — today's trades by `timestamp_open`.
- `get_today_pnl_pct() -> float` — sum of today's closed pnl_pct.
- `get_recent_closed_trades(limit=5) -> list`.
- `save_postmortem(trade_id, text)`.
- `get_consecutive_losses() -> int` — scans the 10 most recent closed trades.

Sentiment:
- `save_sentiment(data: dict)` — insert SentimentSnapshot.
- `get_latest_sentiment(coin='MARKET')`.
- `log_sentiment_result(result, composite_score=0.0)` — write SentimentLog from duck-typed SourceResult.
- `get_sentiment_history(source_id, hours=24)`.
- `get_composite_history(hours=24)` — (timestamp, composite_score) tuples.

Daily stats:
- `upsert_daily_stats(date_str, data: dict)`.

Arb engine:
- `log_arb_trade(result, sim_mode=True) -> int`.
- `log_arb_balance_fail(symbol, buy_exchange, sell_exchange, detail, sim_mode=True) -> int` — status='balance_fail'.
- `get_arb_trades(hours=24)`.
- `get_arb_pnl_today() -> float`.
- `get_arb_stats() -> dict` — counts/win_rate/avg_pnl/best_pair/best_combo (all-time successful).

Arb opportunities:
- `log_arb_opportunity(...) -> int`.
- `mark_arb_opportunity_executed(opp_id, arb_trade_id)`.
- `get_arb_opportunities_today() -> list`.
- `get_arb_opportunity_stats() -> dict`.

Funding arb:
- `log_funding_arb_trade(result, sim_mode=True) -> int`.
- `get_true_pnl(days=7) -> dict` — arb gross P&L net of CapitalMovement sim-fees in window.
- `get_funding_arb_pnl_today() -> float`.

Portfolio + agents:
- `log_portfolio_snapshot(stats: dict)`.
- `log_agent_event(agent_id, event_type, detail='')`.
- `get_last_equity() -> Optional[float]` — most recent total_equity or None on empty.
- `get_trade_realized_pnl(*, only_strategy=None, exclude_strategy=None, today=False) -> float`.
- `get_arb_realized_pnl(*, today=False) -> float`.
- `get_scalp_realized_pnl(*, today=False) -> float` — sums pnl_usd of closed scalp_observations.
- `get_portfolio_history(hours=24)`.
- `get_agent_events(agent_id=None, limit=50)`.

Circuit breakers:
- `log_circuit_breaker(reason, detail, auto_resume_at=None)`.

Data sources:
- `log_data_point(point)` — DataPoint → DataLog.
- `get_data_history(source_id, metric, symbol=None, hours=24)`.
- `get_latest_data_point(source_id, metric, symbol=None)`.
- `get_data_at_time(source_id, metric, timestamp, symbol=None)`.

Macro:
- `save_macro_regime(regime)`.
- `get_macro_history(hours=24)`.
- `save_calendar_events(events: list)` — upsert keyed on event_id.
- `get_pending_events(hours_ahead=48)`.

Scalp observations:
- `save_scalp_observations(obs_list: list)` — upsert keyed on (symbol, exchange, timestamp).
- `get_scalp_summary(days=7) -> dict`.
- `get_scalp_observations(symbol=None, exchange=None, would_entry_only=True, closed_only=True, limit=500) -> list[dict]`.

Funding observations:
- `save_funding_observations(rows: list)` — upsert keyed on (symbol, timestamp, variant).
- `get_funding_summary(days=7) -> dict`.
- `get_funding_observations(symbol=None, would_enter_only=False, limit=500) -> list[dict]`.

Analytics:
- `get_signal_win_rate(signal_type=None, days=30, exclude_strategy=None) -> dict`.

Web UI v1:
- `_session_for_hour(hour) -> str` (private).
- `get_session_pnl_today() -> dict` — combines Trade + ArbTrade by UTC hour.
- `get_top_pairs(n=5) -> list[dict]`.
- `get_strategy_performance() -> list[dict]`.
- `get_recent_postmortems(n=3) -> list[dict]`.
- `_arb_trade_to_dict(r)` (private).
- `get_arb_trades_all() -> list[dict]`.

Web UI v2:
- `get_alltime_realised_pnl() -> float`.
- `get_daily_fees() -> float` — Trade.fees_usd (non-scalp) + arb gross-net + scalp round-trip cost USD.
- `_scalp_obs_to_dict(r)` (private).
- `get_scalp_closed_today() -> list[dict]`.
- `get_scalp_trade_history(limit=100) -> list[dict]`.
- `_trade_to_history_dict(t)` (private).
- `get_signal_trade_history(limit=100) -> list[dict]`.
- `get_postmortems_by_agent(agent_id, n=3) -> list[dict]`.
- `get_closed_trades_by_session(session, date='today') -> list[dict]` — unions Trade + ArbTrade + scalp_observations.

Scalp activation:
- `get_scalp_activation_stats() -> dict`.
- `_scalp_readiness(stats, *, min_obs, min_wr, min_net, max_hold, min_dir)` (private).
- `get_scalp_activation_readiness() -> dict` (v1 thresholds).
- `get_scalp_activation_readiness_v2() -> dict`.

Cross-chain:
- `insert_xchain_observation(...) -> int`.
- `get_xchain_summary(days=7) -> dict`.
- `get_xchain_observations(symbol=None, would_entry_only=False, limit=500) -> list[dict]`.

Capital movements:
- `log_capital_movement(data: dict) -> int`.
- `update_capital_movement(movement_id, fields: dict)` (whitelisted).
- `get_in_transit_movements()`.
- `get_capital_movements_today()`.
- `get_capital_movement_history(hours=168)`.

Fund capital efficiency:
- `log_fund_capital_efficiency(data: dict) -> int`.
- `get_fund_capital_efficiency(fund, hours=720)`.
- `get_latest_fund_efficiency(fund)`.

Web UI v2 agent-panel snapshots (defensive — return defaults on Exception):
- `_today_utc_start()` (private).
- `get_xchain_today_summary() -> dict`.
- `get_funding_today_summary() -> dict` — includes `skip_reasons` taxonomy.
- `_capital_movement_to_dict(r)` (private).
- `get_capital_movements_recent(limit=50) -> list[dict]`.
- `get_capital_movements_in_transit() -> list[dict]`.
- `get_fund_efficiency_summary(window_hours=24) -> list[dict]`.

## ORM models (summary table)
| Model | Table name | Primary key | Notable indexes | FKs | Relationships |
| --- | --- | --- | --- | --- | --- |
| Candle | candles | id | ix_candles_lookup(exchange,pair,timeframe,timestamp) | — | — |
| Signal | signals | id | ix_signals_timestamp / _pair / _type | — | Trade (1:1), Prediction (1:1) |
| Trade | trades | id | ix_trades_timestamp(open) / _pair | signal_id→signals.id | Signal |
| SentimentSnapshot | sentiment | id | ix_sentiment_lookup(coin,timestamp) | — | — |
| SentimentLog | sentiment_log | id | ix_sentiment_log_lookup(source_id,timestamp) | — | — |
| MacroLog | macro_log | id | ix_macro_log_ts | — | — |
| CalendarEvent | calendar_events | id | ix_calendar_events_lookup(scheduled_utc,impact); UNIQUE(event_id) | — | — |
| ScalpObservationModel | scalp_observations | id | col-idx on symbol/exchange/timestamp/would_entry; ix_scalp_obs_lookup | — | — |
| DataLog | data_log | id | ix_data_log_lookup(source_id,metric,symbol,timestamp) | — | — |
| Prediction | predictions | id | — | signal_id→signals.id | Signal |
| DailyStats | daily_stats | id | UNIQUE(date) | — | — |
| ArbTrade | arb_trades | id | ix_arb_trades_lookup(symbol,timestamp) | — | — |
| ArbOpportunity | arb_opportunities | id | ix_arb_opportunities_lookup(symbol,detected_at) | arb_trade_id→arb_trades.id | — |
| FundingArbTrade | funding_arb_trades | id | ix_funding_arb_trades_lookup(symbol,timestamp) | — | — |
| FundingArbObservationModel | funding_arb_observations | id | col-idx on timestamp/symbol/would_enter; ix_funding_arb_obs_lookup | — | — |
| XChainObservation | xchain_observations | id | col-idx on timestamp/symbol/would_entry; ix_xchain_obs_lookup | — | — |
| PortfolioSnapshot | portfolio_snapshots | id | ix_portfolio_snapshots_ts | — | — |
| AgentEvent | agent_events | id | ix_agent_events_lookup(agent_id,timestamp) | — | — |
| CapitalMovement | capital_movements | id | ix_capital_movements_state(state,timestamp); ix_capital_movements_lookup | — | — |
| FundCapitalEfficiency | fund_capital_efficiency | id | ix_fund_capital_efficiency_lookup(fund,timestamp) | — | — |
| CircuitBreakerLog | circuit_breaker_log | id | — | — | — |

Total: 21 mapped models.

## Queries summary
| Function | Tables touched | R/W | Brief purpose |
| --- | --- | --- | --- |
| get_recent_candles | candles | R | newest-first OHLCV |
| save_candle | candles | W | merge upsert |
| save_signal | signals | W | insert, return id |
| update_signal_decision | signals | W | user_action + timestamp |
| update_signal_outcome | signals | W | outcome + pnl_pct |
| update_signal_claude | signals | W | whitelisted Claude fields |
| update_signal_skip | signals | W | skip + reason + price |
| update_signal_future_prices | signals | W | price_1h/4h/24h |
| get_signals_needing_price_update | signals | R | rows with null future-price |
| get_today_skipped_signals | signals | R | count |
| get_trade_by_id | trades | R | PK lookup |
| get_signal_history | signals | R | go-action history |
| save_trade | trades | W | insert, return id |
| close_trade | trades | W | exit + pnl + hold_minutes |
| get_open_trades | trades | R | timestamp_close NULL |
| get_today_trades | trades | R | today by open ts |
| get_today_pnl_pct | trades | R | sum today pnl_pct |
| get_recent_closed_trades | trades | R | last N closed |
| save_postmortem | trades | W | claude_postmortem |
| get_consecutive_losses | trades | R | scans last 10 |
| save_sentiment | sentiment | W | insert |
| get_latest_sentiment | sentiment | R | newest by coin |
| log_sentiment_result | sentiment_log | W | insert |
| get_sentiment_history | sentiment_log | R | window by source |
| get_composite_history | sentiment_log | R | (ts,composite) |
| upsert_daily_stats | daily_stats | W | insert-or-update by date |
| log_arb_trade | arb_trades | W | insert, return id |
| log_arb_balance_fail | arb_trades | W | status='balance_fail' row |
| get_arb_trades | arb_trades | R | window |
| get_arb_pnl_today | arb_trades | R | sum net_pnl_usd today |
| get_arb_stats | arb_trades | R | aggregate (all-time success) |
| log_arb_opportunity | arb_opportunities | W | insert |
| mark_arb_opportunity_executed | arb_opportunities | W | flip executed + link |
| get_arb_opportunities_today | arb_opportunities | R | today |
| get_arb_opportunity_stats | arb_opportunities | R | aggregate |
| log_funding_arb_trade | funding_arb_trades | W | insert, return id |
| get_true_pnl | arb_trades, capital_movements | R | gross arb − rebalance cost |
| get_funding_arb_pnl_today | funding_arb_trades | R | sum today |
| log_portfolio_snapshot | portfolio_snapshots | W | insert |
| log_agent_event | agent_events | W | insert |
| get_last_equity | portfolio_snapshots | R | latest total_equity |
| get_trade_realized_pnl | trades | R | sum pnl_usd (filtered) |
| get_arb_realized_pnl | arb_trades | R | sum net_pnl_usd |
| get_scalp_realized_pnl | scalp_observations | R | sum closed-entry pnl_usd |
| get_portfolio_history | portfolio_snapshots | R | window |
| get_agent_events | agent_events | R | newest by agent |
| log_circuit_breaker | circuit_breaker_log | W | insert |
| log_data_point | data_log | W | insert |
| get_data_history | data_log | R | (source,metric,symbol) window |
| get_latest_data_point | data_log | R | newest |
| get_data_at_time | data_log | R | latest ≤ ts |
| save_macro_regime | macro_log | W | insert |
| get_macro_history | macro_log | R | window |
| save_calendar_events | calendar_events | W | upsert by event_id |
| get_pending_events | calendar_events | R | upcoming window |
| save_scalp_observations | scalp_observations | W | upsert (sym,ex,ts) |
| get_scalp_summary | scalp_observations | R | aggregate |
| get_scalp_observations | scalp_observations | R | filtered → dicts |
| save_funding_observations | funding_arb_observations | W | upsert (sym,ts,variant) |
| get_funding_summary | funding_arb_observations | R | aggregate |
| get_funding_observations | funding_arb_observations | R | newest → dicts |
| get_signal_win_rate | trades | R | model-training stats |
| get_session_pnl_today | trades, arb_trades | R | by-session P&L |
| get_top_pairs | trades | R | top-N by realised pnl |
| get_strategy_performance | trades | R | per-track aggregate |
| get_recent_postmortems | trades | R | last N postmortems |
| get_arb_trades_all | arb_trades | R | all (dicts) |
| get_alltime_realised_pnl | trades, arb_trades | R | bankroll sum |
| get_daily_fees | trades, arb_trades, scalp_observations | R | today's fees USD |
| get_scalp_closed_today | scalp_observations | R | today's closed (dicts) |
| get_scalp_trade_history | scalp_observations | R | newest closed (dicts) |
| get_signal_trade_history | trades | R | newest non-scalp (dicts) |
| get_postmortems_by_agent | trades | R | last N per agent |
| get_closed_trades_by_session | trades, arb_trades, scalp_observations | R | union by session |
| get_scalp_activation_stats | scalp_observations | R | activation aggregate |
| get_scalp_activation_readiness | scalp_observations | R | v1 gate vs thresholds |
| get_scalp_activation_readiness_v2 | scalp_observations | R | v2 gate |
| insert_xchain_observation | xchain_observations | W | insert |
| get_xchain_summary | xchain_observations | R | aggregate |
| get_xchain_observations | xchain_observations | R | newest → dicts |
| log_capital_movement | capital_movements | W | insert |
| update_capital_movement | capital_movements | W | whitelisted patch |
| get_in_transit_movements | capital_movements | R | pending+in_transit |
| get_capital_movements_today | capital_movements | R | today |
| get_capital_movement_history | capital_movements | R | window |
| log_fund_capital_efficiency | fund_capital_efficiency | W | insert |
| get_fund_capital_efficiency | fund_capital_efficiency | R | window per fund |
| get_latest_fund_efficiency | fund_capital_efficiency | R | newest per fund |
| get_xchain_today_summary | xchain_observations | R | today aggregate (defensive) |
| get_funding_today_summary | funding_arb_observations | R | today + skip taxonomy |
| get_capital_movements_recent | capital_movements | R | newest → dicts |
| get_capital_movements_in_transit | capital_movements | R | state filter → dicts |
| get_fund_efficiency_summary | fund_capital_efficiency | R | per-fund window summary |

## Table → write/read functions
| Table | Write functions | Read functions |
| --- | --- | --- |
| candles | save_candle | get_recent_candles |
| signals | save_signal, update_signal_decision, update_signal_outcome, update_signal_claude, update_signal_skip, update_signal_future_prices | get_signals_needing_price_update, get_today_skipped_signals, get_signal_history |
| trades | save_trade, close_trade, save_postmortem | get_trade_by_id, get_open_trades, get_today_trades, get_today_pnl_pct, get_recent_closed_trades, get_consecutive_losses, get_trade_realized_pnl, get_signal_win_rate, get_session_pnl_today, get_top_pairs, get_strategy_performance, get_recent_postmortems, get_alltime_realised_pnl, get_daily_fees, get_signal_trade_history, get_postmortems_by_agent, get_closed_trades_by_session |
| sentiment | save_sentiment | get_latest_sentiment |
| sentiment_log | log_sentiment_result | get_sentiment_history, get_composite_history |
| macro_log | save_macro_regime | get_macro_history |
| calendar_events | save_calendar_events | get_pending_events |
| scalp_observations | save_scalp_observations | get_scalp_summary, get_scalp_observations, get_scalp_realized_pnl, get_scalp_closed_today, get_scalp_trade_history, get_daily_fees, get_closed_trades_by_session, get_scalp_activation_stats, get_scalp_activation_readiness, get_scalp_activation_readiness_v2 |
| data_log | log_data_point | get_data_history, get_latest_data_point, get_data_at_time |
| predictions | — (no helper; no inserts via queries.py) | — |
| daily_stats | upsert_daily_stats | — |
| arb_trades | log_arb_trade, log_arb_balance_fail | get_arb_trades, get_arb_pnl_today, get_arb_stats, get_true_pnl, get_arb_realized_pnl, get_session_pnl_today, get_arb_trades_all, get_alltime_realised_pnl, get_daily_fees, get_closed_trades_by_session |
| arb_opportunities | log_arb_opportunity, mark_arb_opportunity_executed | get_arb_opportunities_today, get_arb_opportunity_stats |
| funding_arb_trades | log_funding_arb_trade | get_funding_arb_pnl_today |
| funding_arb_observations | save_funding_observations | get_funding_summary, get_funding_observations, get_funding_today_summary |
| xchain_observations | insert_xchain_observation | get_xchain_summary, get_xchain_observations, get_xchain_today_summary |
| portfolio_snapshots | log_portfolio_snapshot | get_last_equity, get_portfolio_history |
| agent_events | log_agent_event | get_agent_events |
| capital_movements | log_capital_movement, update_capital_movement | get_in_transit_movements, get_capital_movements_today, get_capital_movement_history, get_true_pnl, get_capital_movements_recent, get_capital_movements_in_transit |
| fund_capital_efficiency | log_fund_capital_efficiency | get_fund_capital_efficiency, get_latest_fund_efficiency, get_fund_efficiency_summary |
| circuit_breaker_log | log_circuit_breaker | — |

## Migrations
No `alembic/` directory exists at the repo root. There is no Alembic migration history checked in.

Schema evolution relies entirely on `init_db()` calling `Base.metadata.create_all` (which is additive — it only creates missing tables, never alters existing ones). Five `# TODO` comments in `models.py` explicitly note that existing DBs need a manual Alembic migration to pick up new tables or new columns (see "TODOs" section below). The de-facto migration story is "delete `data/cryptobot.db` and let init_db rebuild" or hand-rolled ALTER statements.

## Imports graph

### Imports from project
- `database/db.py` → `config.settings.DB_PATH`, `database.models.Base`.
- `database/models.py` → only SQLAlchemy stdlib.
- `database/queries.py` → `database.db.get_session`, `database.models.*`. Lazy imports inside functions: `config.settings`, `database.models.CapitalMovement` / `FundCapitalEfficiency` (to avoid circular cost), `datetime` re-import in `log_data_point`, `time` module locally.

### Imported by (grep `from database` / `import database`)
- Source modules (37 matches): `main.py`, `core/bot.py`, `core/market_data.py`, `core/agent.py`, `signals/engine.py`, `execution/{arb_engine,funding_engine,crosschain_engine,router,position_manager,kill_switch}.py`, `agents/{coordinator,scalping_agent,balance_agent,funding_arb_agent,crosschain_agent,__init__}.py`, `agents/balance/{planner.py,rails/cex_rail.py,rails/sim_rail.py,policy/growth_optimal.py}`, `data_sources/__init__.py`, `sentiment/aggregator.py`, `macro/monitor.py`, `ui/{web_server,dashboard}.py`.
- Doc/prompts that reference (informational): `RUNBOOK.md`, `prompts/{5kfund,fix_pre_soak,build_funding_arb,build_scalping_agent,build_fixes,build_wiring}.md`.

## Tests
Tests that import from `database/`:
- `tests/test_queries.py` — `test_get_last_equity_returns_none_on_empty_table`, `..._returns_most_recent_value`, `..._skips_null_rows`, `test_portfolio_snapshot_timestamp_is_non_zero_after_write`, `test_get_trade_by_id_returns_none_when_missing`, `..._returns_row`, `test_get_signal_win_rate_excludes_strategy`, `test_get_today_pnl_pct_treats_trade_pnl_pct_as_fraction`. Covers the equity-recovery + single-row lookup + win-rate filter paths.
- `tests/test_equity_reconstruction.py` — `test_trade_realized_pnl_all_time_today_and_strategy`, `..._empty`, `test_arb_realized_pnl`, `test_session_pnl_includes_arb_and_trade`, `test_scalp_realized_pnl`, plus 3 monkeypatch tests for engine-side reconstruction. Validates per-fund realised-P&L sums match the ledger.
- `tests/test_scalp_activation.py` — `test_activation_readiness_empty_not_ready`, `test_activation_stats_and_v1_v2`, `test_recalibration_statements_execute`. Exercises `get_scalp_activation_*` against in-memory observations.
- `tests/test_macro.py` — uses `database.db` + `database.queries` to round-trip `save_macro_regime` / `get_macro_history`.
- `tests/test_funding_arb.py` — recreates `Base.metadata`, exercises `save_funding_observations` + `get_funding_summary` against a temp DB.
- `tests/test_balance_agent.py` — exercises `log_capital_movement`, `update_capital_movement`, `get_true_pnl`, plus checks `from database.queries import get_true_pnl` import path.
- `tests/test_dashboard.py` — imports `database.queries` for dashboard panel reads.
- `tests/test_web_server.py` — uses `database.db`, `database.queries`, `database.models` extensively against a temp DB for the v2 panel snapshots.

## TODOs / FIXMEs / stubs
- `database/models.py:441` — `TODO: existing databases need an Alembic migration to add the status / slippage_buy_pct / slippage_sell_pct columns. Fresh init_db creates them automatically.` (ArbTrade)
- `database/models.py:481` — `TODO: new table — fresh init_db creates it; existing databases need an Alembic migration.` (ArbOpportunity)
- `database/models.py:511` — `TODO: new table — fresh init_db creates it; existing databases need an Alembic migration.` (FundingArbTrade)
- `database/models.py:559` — `TODO: new table — fresh init_db creates it; existing databases need an Alembic migration.` (FundingArbObservationModel)
- `database/models.py:610` — `TODO: new table — fresh init_db creates it; existing databases need an Alembic migration.` (XChainObservation)
- `database/queries.py:607` — `TODO: wire once live transfers carry a fee_usd column.` (inside `get_true_pnl` — live rebalance fees are currently counted as zero.)

No `FIXME` / `XXX` / `HACK` markers under `database/`.

## Known issues observed

1. **No Alembic; five tables / one column-set need hand migration.** `init_db()` only creates missing tables — it never adds columns to existing ones. The five TODOs in `models.py` flag this explicitly, but there is no migration script, runbook step, or guard to enforce the ALTERs. A bot started against an older `data/cryptobot.db` will silently lose the new columns on `ArbTrade` (`status`, `slippage_buy_pct`, `slippage_sell_pct`) and writes will fail with SQLite's "no such column" error.
2. **`Predictions` table has no query helpers.** Although the `predictions` table is created and `Signal.prediction` is a 1:1 relationship, **no function in `queries.py` writes or reads it**. The model is dead code at the moment — the `predictive/trainer` workflow described in CLAUDE.md would need to be wired up via this layer, but currently any predictive code must open a session directly (violating the documented invariant).
3. **`Signal` columns missing from typical write paths.** `outcome` / `outcome_pnl_pct` are only set via `update_signal_outcome` (one call site), and the `tf_*_confirm`, `indicators_json`, `claude_api_cost_usd` columns rely on the scanner shoving them into the dict passed to `save_signal`. There is no compile-time check that `signals/base.py:Signal.to_db_dict()` keeps pace with `models.Signal` — the CLAUDE.md invariant about that synchronisation is enforced only by convention.
4. **`CircuitBreakerLog` has no index on `timestamp` or `reason`.** Queries scoped to "circuit breakers in the last hour" or "today's halt reasons" would scan the whole table. Currently no read helper exists, but the table is being written without retrieval ever planned.
5. **`expire_on_commit=False` is load-bearing.** `db.py` comments call this out. Multiple query helpers return ORM rows out of the `with get_session()` block (`get_open_trades`, `get_in_transit_movements`, `get_capital_movement_history`, …); a future move back to the SQLAlchemy default would silently raise `DetachedInstanceError` across many consumers including the dashboard and tests.
6. **Mixed-type `timestamp` columns.** `ScalpObservationModel.timestamp` and `FundingArbObservationModel.timestamp` are `Float` (epoch), while every other timestamp is `DateTime`. Aggregations that union these (e.g. `get_closed_trades_by_session` uses `o.created_at` for scalp, not `o.timestamp`) must be careful — `get_funding_today_summary` correctly converts via `_today_utc_start().timestamp()`, but the asymmetry is a footgun for new joins.
7. **`get_consecutive_losses` early-exits the day's snapshot.** It scans only the latest 10 trades and stops on the first non-loss — a 30-trade losing streak is invisible.
8. **`get_arb_stats` and `get_top_pairs` are all-time, unbounded.** They load every row of `arb_trades` / `trades` into memory; on a busy soak the dashboard latency degrades linearly.
9. **No explicit unique constraint on `(symbol, exchange, timestamp)` for scalp_observations** even though `save_scalp_observations` treats it as a natural key. A race could insert duplicates; the existing index is non-unique.
10. **`get_true_pnl` understates live-mode rebalance cost.** TODO at `queries.py:607` — live rows always charge 0 because there's no `fee_usd` column on `CapitalMovement`. Reported net P&L is optimistic once live transfers run.

---

# Module Report: sentiment

## Purpose

`sentiment/` is the pluggable sentiment-aggregation layer of the bot. It owns a small set of plugin "sources" (Fear & Greed Index, CryptoPanic/RSS news, Reddit, Google Trends, Telegram) that each return a `-100..+100` score plus optional metadata. The `SentimentAggregator` blends these into:

- a single **composite score** (`-100..+100`),
- a step-function **modifier** (`-20..+20`) added to every Signal's score by the quality gate,
- a set of **session-floor checks** (extreme fear, news guard, BTC dump guard) that can suppress trading entirely,
- a **hard-block** flag any source can raise (e.g. catastrophic news keywords) to short-circuit the system.

Per CLAUDE.md, scores feed in at the quality gate (`signals/quality_gate.py:116`) where `QualityGate.evaluate` adds the sentiment modifier to `raw_score` before applying the score threshold. The `core/bot.py` startup path also lazy-imports the singleton (`core/bot.py:178, 501, 851`) for session-floor evaluation and BTC-change updates. The module-level singleton instance is `sentiment.aggregator.sentiment`.

## Subpackages

- `sentiment/sources/` — plugin-style sources. Adding a new source means subclassing `BaseSentimentSource`, setting class-level attrs, implementing `fetch()` / `is_available()`, and appending the class to `REGISTERED_SOURCES` in `sentiment/sources/__init__.py`. The aggregator never needs to change.

## Files

| File | LOC | One-sentence summary |
|---|---|---|
| `sentiment/__init__.py` | 27 | Public re-exports: `SentimentAggregator`, `SentimentData`, `sentiment` singleton, `BaseSentimentSource`, `SourceResult`. |
| `sentiment/base.py` | 111 | Defines `SourceResult` dataclass and `BaseSentimentSource` ABC with cache-or-fetch `get()` method. |
| `sentiment/aggregator.py` | 322 | `SentimentAggregator`, `SentimentData`, `_composite_to_modifier`, module-level `sentiment` singleton. |
| `sentiment/sources/__init__.py` | 47 | Plugin registry — `REGISTERED_SOURCES` list of 5 source classes, with a "how to add" docstring. |
| `sentiment/sources/cryptopanic.py` | 179 | News scorer: CryptoPanic API or RSS fallback + keyword scorer; raises hard_block on catastrophic-event keywords. |
| `sentiment/sources/fear_greed.py` | 60 | Alternative.me Fear & Greed Index poller; the only non-optional source. |
| `sentiment/sources/google_trends.py` | 85 | pytrends-based search-interest signal, wrapped in run_in_executor (slow, light weight). |
| `sentiment/sources/reddit.py` | 145 | PRAW-based hot-post bull/bear keyword classifier across configured subreddits. |
| `sentiment/sources/telegram.py` | 42 | STUB — declares itself unavailable, returns neutral; TODO for Telethon. |

Total: 1018 LOC.

## Public surface

### `sentiment/__init__.py`
- Module docstring: "sentiment/ — pluggable sentiment aggregator." with public surface listed.
- Re-exports: `SentimentAggregator`, `SentimentData`, `sentiment` (singleton), `BaseSentimentSource`, `SourceResult` via `__all__`.

### `sentiment/base.py`
- Module docstring: explains the two contracts (`SourceResult`, `BaseSentimentSource`), `-100..+100` score range, `hard_block` short-circuit semantics, and that base `get()` handles caching/retry so subclasses only implement `fetch()`.
- `@dataclass class SourceResult`:
  - Fields: `source_id: str`, `score: float`, `raw_data: dict = field(default_factory=dict)`, `hard_block: bool = False`, `block_reason: str = ""`, `confidence: float = 1.0`, `timestamp: float = 0.0`, `error: Optional[str] = None`.
  - `__post_init__(self)` — defaults `timestamp` to `time.time()`, clamps `score` to `[-100, 100]`, clamps `confidence` to `[0, 1]`.
- `class BaseSentimentSource(ABC)`:
  - Class-level attrs (override in subclass): `source_id: str = "base"`, `weight: float = 0.0`, `refresh_interval: int = 300`, `optional: bool = True`.
  - `__init__(self)` — initialises `_last_result: Optional[SourceResult] = None`, `_last_fetch_time: float = 0.0`.
  - `@abstractmethod async def fetch(self) -> SourceResult` — must never raise; subclass returns a SourceResult with `error` set on failure.
  - `def is_available(self) -> bool` — default `True`; subclasses override to gate on env vars / deps.
  - `async def get(self) -> Optional[SourceResult]` — cache-or-fetch wrapper. Uses `refresh_interval` as TTL. On exception returns previous cached result (or `None` if none).
  - `def last(self) -> Optional[SourceResult]` — for tests + dashboard.

### `sentiment/aggregator.py`
- Module docstring describes orchestration + composite + session-floor responsibilities.
- `@dataclass class SentimentData` — aggregated picture for dashboard/engine. Fields:
  - `composite_score: float = 0.0`, `sentiment_modifier: float = 0.0`, `hard_block: bool = False`, `block_reason: str = ""`.
  - F&G surfaces: `fear_greed_value: Optional[int] = None`, `fear_greed_label: str = "unknown"`.
  - News surfaces: `news_score`, `news_guard_active`, `blocking_headline`, `top_headlines: list = field(default_factory=list)`.
  - Reddit: `reddit_score`, `reddit_bullish_ratio`.
  - Other: `google_trends_score`, `btc_change_30m`, `sources_active: int = 0`, `sources_available: int = 0`, `last_updated: Optional[datetime] = None`.
- `def _composite_to_modifier(composite: float) -> float` — step-function mapping composite `-100..+100` to additive modifier `-20..+20` with breakpoints at ±20/±40/±60.
- `class SentimentAggregator`:
  - `__init__(self, sources: Optional[list[BaseSentimentSource]] = None)` — defaults to `[cls() for cls in REGISTERED_SOURCES]`; tests can inject pre-instantiated sources.
  - `async def refresh(self) -> SentimentData` — fans out `s.get()` over `is_available()` sources via `asyncio.gather(return_exceptions=True)`, computes composite, populates `SentimentData`, persists each `SourceResult` via `db_queries.log_sentiment_result(result, composite_score=composite)`.
  - `async def get_current(self) -> SentimentData` — returns cached `_data`, refreshing if older than `settings.SENTIMENT_REFRESH_INTERVAL_SEC`.
  - `def get_signal_modifier(self) -> int` — returns `int(self._data.sentiment_modifier or 0)`. Mirrors `macro_monitor.get_signal_modifier()`. Zero before first refresh.
  - `def is_hard_blocked(self) -> tuple[bool, str]` — true if any source raised `hard_block` or if `news_guard_active`.
  - `def passes_session_floor(self) -> tuple[bool, str]` — three independent floors: hard_block; news_guard_active; `fear_greed_value < SENTIMENT_FEAR_GREED_FLOOR`; `composite_score < SENTIMENT_COMPOSITE_FLOOR`.
  - `def set_btc_change_30m(self, pct: float) -> None` — stores a 30m BTC % change for the BTC-dump guard.
  - `def btc_guard_penalty(self, signal_type: str, pair: str) -> float` — returns `0` for `arb`, `0` when no data, `0` if `_btc_change_30m > SENTIMENT_BTC_GUARD_PCT`, otherwise `SENTIMENT_BTC_GUARD_PENALTY`.
  - `@property def fear_greed(self) -> Optional[BaseSentimentSource]` — returns the `FearGreedSource` instance for direct access from `bot.py`.
  - Private: `_compute_composite(pairs)` — weighted average `sum(score*weight*conf)/sum(weight*conf)`, clamped ±100. `_hard_block_from(pairs)` — first source with `hard_block=True` wins. `_populate_per_source_fields(data, pairs)` — fans `source_id` to specific `SentimentData` fields. `_to_dashboard_dict(d)` — dashboard mirror.
- Module singleton: `sentiment = SentimentAggregator()` (constructed at import time, so REGISTERED_SOURCES are instantiated immediately on first `from sentiment import sentiment`).

### `sentiment/sources/__init__.py`
- Docstring is a "how to add a new sentiment source" recipe.
- Imports: `FearGreedSource`, `CryptoPanicSource`, `RedditSource`, `GoogleTrendsSource`, `TelegramSource`.
- `REGISTERED_SOURCES: list[type] = [FearGreedSource, CryptoPanicSource, RedditSource, GoogleTrendsSource, TelegramSource]`.

### `sentiment/sources/fear_greed.py`
- Module docstring: "Alternative.me Fear & Greed Index — the only source the aggregator treats as non-optional."
- `ENDPOINT = "https://api.alternative.me/fng/?limit=1"`.
- `class FearGreedSource(BaseSentimentSource)`:
  - `source_id = "fear_greed"`, `refresh_interval = 900`, `optional = False`.
  - `__init__` reads `weight = getattr(settings, "SENTIMENT_WEIGHT_FEAR_GREED", 0.4)`.
  - `async def fetch(self) -> SourceResult` — `aiohttp` GET, maps `value (0..100)` to `(value-50)*2.0`, returns score + `raw_data={value, label, timestamp}`. On exception logs warning and returns neutral SourceResult with `error` set.
  - No `is_available()` override — always available.

### `sentiment/sources/cryptopanic.py`
- Module docstring explains API/RSS dual path and catastrophic-event hard blocks.
- Constants: `POSITIVE_KEYWORDS` (10), `NEGATIVE_KEYWORDS` (15), `BLOCKING_KEYWORDS` (6 catastrophic phrases). `RSS_FEEDS` (cointelegraph, coindesk, decrypt). `CRYPTOPANIC_ENDPOINT` URL template.
- `def _score_headlines(headlines: list[str]) -> tuple[float, float, bool, str, list[str]]` — returns `(score, confidence, hard_block, block_reason, top)`. Score is `net * 100 * matched_fraction + net*30`. Confidence is `min(1, len(headlines)/25)`. First blocking keyword match wins.
- `class CryptoPanicSource(BaseSentimentSource)`:
  - `source_id = "cryptopanic"`, `refresh_interval = 300`, `optional = True`.
  - `__init__` reads `weight = getattr(settings, "SENTIMENT_WEIGHT_CRYPTOPANIC", 0.25)`.
  - `def is_available(self) -> bool` — returns `True` (RSS fallback always works).
  - `async def fetch(self) -> SourceResult` — picks API path if `CRYPTOPANIC_API_KEY` env var is set, otherwise RSS fallback; runs keyword scorer; returns SourceResult with `hard_block`, `block_reason`, `raw_data={feed, headlines, article_count}`.
  - `async def _fetch_api(self, api_key)` — `aiohttp` GET, returns titles from `results`.
  - `async def _fetch_rss(self)` — fetches each RSS URL via aiohttp, parses bytes with `feedparser`, returns up to 20 titles per feed.

### `sentiment/sources/reddit.py`
- Module docstring: PRAW wrapped in `run_in_executor`.
- Constants: `DEFAULT_SUBREDDITS = ["CryptoCurrency", "Bitcoin", "ethtrader"]`, `BULLISH_KEYWORDS` (11), `BEARISH_KEYWORDS` (11).
- `def _classify(text: str) -> str` — returns `"bull"`, `"bear"`, or `"neutral"` based on first keyword hit.
- `class RedditSource(BaseSentimentSource)`:
  - `source_id = "reddit"`, `refresh_interval = 600`, `optional = True`.
  - `__init__` reads `weight = getattr(settings, "SENTIMENT_WEIGHT_REDDIT", 0.2)`.
  - `def is_available(self) -> bool` — returns `True` only if `praw` importable AND `REDDIT_CLIENT_ID` env var set.
  - `async def fetch(self) -> SourceResult` — runs `_scrape_sync` in executor.
  - `def _scrape_sync(self) -> dict` — uses `praw.Reddit` (with `REDDIT_CLIENT_ID/SECRET/USER_AGENT` env vars), pulls 25 hot posts per subreddit from `settings.REDDIT_SUBREDDITS` or `DEFAULT_SUBREDDITS`, computes `bull_ratio` and centered-on-zero score `(bull_ratio - 0.5) * 200`. Confidence `min(1, post_count/50)`.

### `sentiment/sources/google_trends.py`
- Module docstring notes pytrends is sync; signal is "rising-vs-baseline"; lightly weighted at 0.1.
- Constants: `KEYWORDS = ["bitcoin", "crypto", "ethereum"]`, `TIMEFRAME = "now 7-d"`.
- `class GoogleTrendsSource(BaseSentimentSource)`:
  - `source_id = "google_trends"`, `refresh_interval = 3600`, `optional = True`.
  - `__init__` reads `weight = getattr(settings, "SENTIMENT_WEIGHT_GOOGLE_TRENDS", 0.1)`.
  - `def is_available(self) -> bool` — `True` iff `pytrends` importable.
  - `async def fetch(self)` — runs `_fetch_sync` in executor, sets `confidence=0.6` (fixed per spec).
  - `def _fetch_sync(self) -> dict` — `pytrends.request.TrendReq`, `build_payload(KEYWORDS, ...)`, takes mean across KEYWORDS of latest interest reading, scores `(latest-50)*2`, clamped ±100.

### `sentiment/sources/telegram.py`
- Module docstring: "Telegram channel sentiment — STUB" with TODO implementation notes for Telethon.
- `class TelegramSource(BaseSentimentSource)`:
  - `source_id = "telegram"`, `refresh_interval = 0`, `optional = True`.
  - `__init__` reads `weight = getattr(settings, "SENTIMENT_WEIGHT_TELEGRAM", 0.05)`.
  - `def is_available(self) -> bool` — returns `False` (stub).
  - `async def fetch(self) -> SourceResult` — returns neutral SourceResult with `error="Telegram source not yet implemented"`.

## Plugin registrations

`REGISTERED_SOURCES` in `sentiment/sources/__init__.py:41`. Each entry, with API key env var and `is_available()` behaviour:

| source_id | class | weight (default) | refresh_interval | optional | env vars / deps | is_available() |
|---|---|---|---|---|---|---|
| `fear_greed` | `FearGreedSource` | `SENTIMENT_WEIGHT_FEAR_GREED` (0.4) | 900s | False (core) | none | always True (no override) |
| `cryptopanic` | `CryptoPanicSource` | `SENTIMENT_WEIGHT_CRYPTOPANIC` (0.25) | 300s | True | `CRYPTOPANIC_API_KEY` (optional; RSS fallback) | True (always — RSS fallback) |
| `reddit` | `RedditSource` | `SENTIMENT_WEIGHT_REDDIT` (0.2) | 600s | True | `praw` package + `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | True iff `praw` importable AND `REDDIT_CLIENT_ID` set |
| `google_trends` | `GoogleTrendsSource` | `SENTIMENT_WEIGHT_GOOGLE_TRENDS` (0.1) | 3600s | True | `pytrends` package | True iff `pytrends` importable |
| `telegram` | `TelegramSource` | `SENTIMENT_WEIGHT_TELEGRAM` (0.05) | 0 (event-driven) | True | (would be `telethon`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`) | False (stub) |

Sum of default weights = 1.00. With `telegram` always unavailable and `google_trends`/`reddit` dependent on optional deps, in a vanilla install the aggregator typically runs with only `fear_greed` + `cryptopanic` (weight sum 0.65 of the documented ladder).

## Imports graph

### Imports from project

- `sentiment/aggregator.py` imports `config.settings`, `database.queries as db_queries`, `sentiment.base.{BaseSentimentSource, SourceResult}`, `sentiment.sources.REGISTERED_SOURCES`, `sentiment.sources.fear_greed.FearGreedSource`.
- `sentiment/__init__.py` imports `sentiment.aggregator.{SentimentAggregator, SentimentData, sentiment}`, `sentiment.base.{BaseSentimentSource, SourceResult}`.
- `sentiment/sources/__init__.py` imports each source class.
- Every `sentiment/sources/*.py` imports `config.settings` and `sentiment.base.{BaseSentimentSource, SourceResult}`. No other project imports.
- `sentiment/base.py` imports only stdlib.

### Imported by (`grep "from sentiment"`)

- `signals/quality_gate.py:116` — `from sentiment import sentiment as sentiment_aggregator` (lazy import inside method).
- `core/bot.py:178, 501, 851` — `from sentiment import sentiment as sentiment_singleton` / `as sentiment_agg` (three lazy import sites).
- `agents/base.py:9` — references the BaseSentimentSource pattern in its docstring (no import).
- `tests/test_sentiment.py:18, 19, 287, 298` — test imports.
- `prompts/build_sentiment.md`, `prompts/build_wiring.md` — design docs referencing the public surface.

## Tests

Only `tests/test_sentiment.py` imports from `sentiment/`. 382 LOC. Other tests touched by grep (`test_quality_gate`, `test_bot`, `test_macro`, etc.) merely mention the word "sentiment" in comments/fixtures.

Tests (one-liner each):

- `test_composite_weighted_average` — composite is `Σ(score·weight·conf) / Σ(weight·conf)`, clamped to ±100; +44 maps to modifier 10.
- `test_composite_clamps_to_bounds` — extreme inputs stay within ±100, +100 maps to modifier 20.
- `test_hard_block_propagates_from_any_source` — any source with `hard_block=True` sets `data.hard_block` and `block_reason`.
- `test_hard_block_blocks_session_floor` — `hard_block` causes `passes_session_floor()` to return False with reason containing `"hard_block"`.
- `test_unavailable_source_excluded_from_composite` — `is_available()==False` sources are skipped before fetch.
- `test_crashing_source_does_not_break_aggregator` — a source whose fetch raises is dropped, other sources still score.
- `test_passes_session_floor_blocks_on_extreme_fear` — F&G value below floor blocks session.
- `test_passes_session_floor_blocks_below_composite_floor` — composite below floor blocks session.
- `test_passes_session_floor_allows_normal_conditions` — neutral/positive conditions pass.
- `test_btc_guard_penalty_applies_below_threshold` — 30m BTC drop ≤ guard threshold yields penalty.
- `test_btc_guard_no_penalty_above_threshold` — small drops yield no penalty.
- `test_btc_guard_exempts_arb` — `signal_type=="arb"` always 0.
- `test_btc_guard_no_change_data_yet` — no `set_btc_change_30m()` call → 0.
- `test_new_source_plugs_into_aggregator` — a custom `BaseSentimentSource` subclass works without aggregator changes.
- `test_registered_sources_list_is_extensible` — asserts `len(REGISTERED_SOURCES) >= 5`.
- `test_fear_greed_property_returns_instance` — `aggregator.fear_greed` returns the live `FearGreedSource` instance.
- `test_dashboard_dict_populates` — `agg.latest` mirrors `_data` with the right keys after refresh.
- `test_get_signal_modifier_defaults_to_zero_before_refresh` — modifier is 0 before any refresh.
- `test_is_hard_blocked_false_before_refresh` — `is_hard_blocked()` is `(False, "")` before refresh.
- `test_hard_block_propagates_to_aggregator` — `hard_block=True` source surfaces via `is_hard_blocked()`.
- `test_get_signal_modifier_returns_step_value_after_refresh` — composite ≥+60 maps to modifier 20 (ladder boundary check).

Test fixtures rely on a `_patch_db_log` monkeypatch fixture that nulls `sentiment.aggregator.db_queries.log_sentiment_result` to avoid SQLite during tests.

## TODOs / FIXMEs / stubs

- `sentiment/sources/telegram.py:4` — module docstring: `"Telegram channel sentiment — STUB."`
- `sentiment/sources/telegram.py:6` — `"TODO: implement via Telethon. The Telethon client is event-driven, so this source should subscribe to TELEGRAM_CHANNELS at start-up and push incoming messages into a rolling buffer. fetch() would then score the buffer's recent contents rather than poll. For now the source declares itself unavailable and returns a neutral result."`
- `sentiment/sources/telegram.py:33` — `return False  # stub`
- `sentiment/sources/telegram.py:40` — `raw_data={"status": "stub"}`

Telegram source is the only declared stub in the package.

## Known issues observed

- **Singleton instantiated at import time.** `sentiment = SentimentAggregator()` at `sentiment/aggregator.py:322` runs at first import; this constructs every `REGISTERED_SOURCES` class. Side-effect-free today, but if any source class's `__init__` ever touches the network or files it would do so at module import. `import sentiment` from a test that monkeypatches settings *after* import will miss the override.
- **Composite floor vs F&G floor: F&G floor checked first.** `passes_session_floor` checks `fear_greed_value < SENTIMENT_FEAR_GREED_FLOOR` before `composite_score < SENTIMENT_COMPOSITE_FLOOR` — intentional per the comment, but it means an extreme F&G reading is reported as the reason even when composite also failed.
- **Stale settings constants.** `config/settings.py` contains *two* sets of sentiment knobs: an older block at lines 371–388 (`SENTIMENT_ENABLED`, `SENTIMENT_WEIGHTS` dict, `SENTIMENT_BOOST_THRESHOLD`, `SENTIMENT_BLOCK_THRESHOLD`, `SENTIMENT_BOOST_AMOUNT`, `SENTIMENT_SUPPRESS_AMOUNT`, `SENTIMENT_VELOCITY_WINDOW`, `SENTIMENT_VELOCITY_BOOST`, `SESSION_MIN_SENTIMENT_SCORE`, `SENTIMENT_HARD_BLOCK_SKIP_REASON`) and the *actually used* block at lines 1029–1042 (`SENTIMENT_COMPOSITE_FLOOR`, `SENTIMENT_FEAR_GREED_FLOOR`, `SENTIMENT_BTC_GUARD_PCT`, `SENTIMENT_BTC_GUARD_PENALTY`, `SENTIMENT_REFRESH_INTERVAL_SEC`, `SENTIMENT_HTTP_TIMEOUT_SEC`, `SENTIMENT_WEIGHT_*`). Nothing in `sentiment/` reads from the older block — it appears to be vestigial.
- **`prompts/build_wiring.md:72` references `sentiment_aggregator` import name** that doesn't exist (`from sentiment.aggregator import sentiment_aggregator`). The actual export is `sentiment`. Wiring doc is out-of-date.
- **In a vanilla install most sources are unavailable.** Without `praw` / `pytrends` installed and without `REDDIT_CLIENT_ID` set, only Fear & Greed (weight 0.4) and CryptoPanic via RSS (weight 0.25) contribute, leaving a composite driven mostly by F&G. The composite-floor and modifier-ladder tuning was set assuming all five sources active.
- **`asyncio.get_event_loop()` is deprecated for getting the running loop in 3.10+.** `reddit.py:76` and `google_trends.py:50` both use `asyncio.get_event_loop().run_in_executor(...)`. Should be `asyncio.get_running_loop()` or just `asyncio.to_thread()` on 3.11.
- **`datetime.utcnow()` deprecated.** `aggregator.py:143, 165` use `datetime.utcnow()`, which is deprecated in 3.12 in favour of `datetime.now(timezone.utc)`. Project targets 3.11 so this is a forward-compat concern only.
- **Singleton's `latest` dict is mutated in place.** `set_btc_change_30m` writes to both `_data.btc_change_30m` and `self.latest["btc_change_30m"]`, but `latest` is otherwise only rebuilt during `refresh()` — slightly asymmetric and could confuse a reader who expects `latest` to be a snapshot.
- **Confidence multiplier double-discounts on quiet days.** Composite uses `score * weight * confidence` and `weight_sum = sum(weight * confidence)`. A single source with low confidence pulls the denominator down too, so its weight-share in the average actually doesn't change — meaning low-confidence sources are not really down-weighted vs other sources, only the *total* signal. This may not match intuition for "confidence."
- **`asyncio.gather(return_exceptions=True)` swallows exceptions silently at DEBUG level** (`aggregator.py:124-126`), but the base `get()` method already catches and returns the previous cached result, so a true exception bubbling to `gather` indicates a bug in `get()` itself — worth a higher log level.
- **`log_sentiment_result` is called per-source per-refresh.** With 5 sources every 300s that's an SQLite write hot-path; check `database/queries.py` for batching.

---

# Module Report: data_sources

## Purpose
Pluggable aggregator for raw quantitative time-series feeds — crypto derivatives (funding, OI, liquidations, long/short), broad crypto market context (dominance, mcap), traditional-market equities/bonds/FX, central-bank rates, sovereign yield curves, and slow-moving global macro indicators (GDP, inflation, unemployment). The package owns a per-key cache, source-level staleness, error fallback, and a fnmatch-based pub/sub bus. Concrete sources never touch caching or notification — they only emit `DataPoint` objects from `fetch_all()`.

How this differs from the two sibling source packages:

- `sentiment/sources/` — text/news/social feeds (cryptopanic, reddit, telegram, fear_greed, google_trends). Emits a `SentimentScore` (-1..+1 with confidence) per source via `sentiment.aggregator`. About narrative.
- `macro/sources/` — economic calendar events only (FRED release calendar + stub). Emits `CalendarEvent` objects with scheduled timestamps consumed by `macro.monitor`. About *when* an event will happen.
- `data_sources/sources/` — numeric *readings* (price, rate, level, ratio). About *what the number is right now*. Consumed downstream by `macro.monitor`, the funding-rate arb engine, the dashboard, and the quality gate.

## Subpackages
- `data_sources/sources/` — plugin-style external feeds. One file per remote API, each implementing `BaseDataSource.fetch_all()` + `list_metrics()`. Registry is `data_sources/sources/__init__.py:REGISTERED_SOURCES`.

## Files
| File | LOC | One-sentence summary |
|---|---:|---|
| `data_sources/__init__.py` | 288 | `DataSources` orchestrator + module-level singleton `data_sources`; pub/sub bus, refresh loop, `get_funding_rates()` aggregation helper, DB logging of every refreshed point. |
| `data_sources/base.py` | 213 | Abstract `BaseDataSource` + `DataPoint` dataclass; owns cache, staleness, error-state fallback, and change-detection notification. |
| `data_sources/sources/__init__.py` | 70 | Plugin registry — imports each `*Source` class and appends an instance to `REGISTERED_SOURCES`. |
| `data_sources/sources/alpha_vantage.py` | 170 | SPY/QQQ/GLD/TLT via Alpha Vantage `GLOBAL_QUOTE`; daily-call budget gate + inter-call pacing for the 25/day free tier. |
| `data_sources/sources/binance_futures.py` | 141 | Binance USDT-M perp open interest, latest funding rate, and top-trader long/short ratio per pair; no auth. |
| `data_sources/sources/bybit_derivs.py` | 137 | Bybit v5 `/market/tickers` for funding, OI, mark price, 24h %; one HTTP call per symbol. |
| `data_sources/sources/cftc_cot.py` | 134 | CFTC Commitments of Traders disaggregated report — non-commercial Bitcoin futures long/short/net, weekly. |
| `data_sources/sources/coingecko.py` | 140 | CoinGecko `/global` — BTC/ETH dominance, total market cap/volume, mcap 24h %; supports public/demo/pro key tiers. |
| `data_sources/sources/coinglass.py` | 189 | Coinglass funding/OI/liquidations/L-S ratio per pair; semaphore caps concurrent HTTP for free-tier quota. |
| `data_sources/sources/cryptocompare.py` | 139 | CryptoCompare `pricemultifull` — price, 24h volume, mcap, 24h % per coin (cross-check vs CoinGecko). |
| `data_sources/sources/ecb.py` | 119 | ECB SDMX-JSON — refi rate, deposit-facility rate, EUR/USD spot; one HTTP call per series. |
| `data_sources/sources/frankfurter.py` | 237 | FX via frankfurter.dev (ECB reference rates) + ICE-style geometric DXY proxy; pulls today and yesterday for 24h change. |
| `data_sources/sources/fred.py` | 214 | FRED observations API for CPI, yields (2y/10y/30y), fed funds, M2, unemployment, 2-10 spread, VIX; uses `units=pc1` for CPI YoY; carries the VIX-based `get_risk_sentiment()` classifier. |
| `data_sources/sources/imf.py` | 118 | IMF Datamapper — GDP per cap, inflation YoY, unemployment per country; one call per indicator. |
| `data_sources/sources/us_treasury.py` | 133 | US Treasury daily yield-curve CSV — tenors 1m..30y + derived 2y/10y spread (FRED backup). |
| `data_sources/sources/world_bank.py` | 123 | World Bank Open Data — GDP growth, inflation, unemployment per (country × indicator); 5-year lookback for last published value. |

## Public surface

### `data_sources/__init__.py`
- Module docstring: pluggable macro+on-chain+FX aggregator; PULL / PUSH / REFRESH usage examples.
- `class _Subscription` — `__slots__=("sub_id","pattern","callback","filter")`; `__init__(sub_id, pattern, callback, filter=None)`; `matches(key) -> bool` (fnmatchcase).
- `class DataSources`
  - `__init__(sources: Optional[list[BaseDataSource]] = None)` — defaults to deferred `from data_sources.sources import REGISTERED_SOURCES`; sets each source as attribute by `source_id` and wires `_notify_callback`.
  - `async refresh_all() -> None` — filters by `is_available()`, gathers per-source `_safe_refresh`, then `db_queries.log_data_point(point)` for every cached point.
  - `async _safe_refresh(source) -> None` — wraps `refresh_if_stale()` in try/except.
  - `async get(source_id, metric, symbol=None) -> Optional[DataPoint]`.
  - `latest_snapshot() -> dict[str, DataPoint]` — flatten every cache by key.
  - `get_latest(source_id, metric, symbol=None) -> Optional[DataPoint]` — sync cached read.
  - `get_funding_rates() -> dict[str, float]` — `{symbol: rate}` across all available sources, newest-timestamp wins; skips error points.
  - `subscribe(pattern, callback, filter=None) -> str` — fnmatch wildcards; returns sub_id.
  - `unsubscribe(subscription_id) -> bool`.
  - `async _on_source_change(new, previous) -> None` — wired into base; fires matching subs via `asyncio.create_task`.
  - `async _invoke(sub, new, previous) -> None` — awaits coroutine callbacks.
  - `async run_refresh_loop(interval_sec) -> None` — infinite loop.
- Module-level singleton: `data_sources = DataSources()`.
- `__all__ = ["DataSources", "BaseDataSource", "DataPoint", "data_sources"]`.

### `data_sources/base.py`
- `@dataclass DataPoint`: fields `source_id: str`, `metric: str`, `value: float`, `symbol: Optional[str]=None`, `timestamp: float=0.0` (auto-filled `__post_init__`), `raw_data: dict=field(default_factory=dict)`, `error: Optional[str]=None`. Property `key` → `"{source_id}.{metric}"` or `"{source_id}.{metric}.{symbol}"`.
- `class BaseDataSource(ABC)`. Class attrs (override): `source_id="base"`, `display_name="Base"`, `refresh_interval=300`, `optional=True`, `requires_api_key=False`, `api_key_env_var=""`.
  - `__init__()` — instance-level `_cache: dict[str, DataPoint]`, `_last_fetch_time=0.0`, `_notify_callback=None`.
  - `@abstractmethod async fetch_all() -> list[DataPoint]` — must never raise.
  - `@abstractmethod list_metrics() -> list[str]`.
  - `is_available() -> bool` — default: True unless `requires_api_key` and env var unset.
  - `async get(metric, symbol=None) -> Optional[DataPoint]`.
  - `async refresh_if_stale() -> None`.
  - `async _do_fetch() -> None` — runs `fetch_all`, merges, does NOT clobber a good cached value with an error placeholder, fires `_notify_callback` only on value/error change.
  - `_make_key(metric, symbol=None) -> str`.
  - `cached(metric, symbol=None) -> Optional[DataPoint]` — sync read.
  - `cached_value(metric, symbol=None, default=None) -> Optional[float]` — None/default if point missing or has error.
  - `latest_points() -> list[DataPoint]`.

### `data_sources/sources/__init__.py`
- Constants: `REGISTERED_SOURCES: list` — 13 source instances (see §Plugin registrations).

### `data_sources/sources/alpha_vantage.py`
- Constants: `ENDPOINT = "https://www.alphavantage.co/query"`.
- `class AlphaVantageSource(BaseDataSource)`: `source_id="alpha_vantage"`, `display_name="Alpha Vantage"`, `refresh_interval=settings.ALPHA_VANTAGE_REFRESH_SEC`, `optional=True`, `requires_api_key=True`, `api_key_env_var="ALPHA_VANTAGE_API_KEY"`.
  - `__init__()` — `_call_date: Optional[str]=None`, `_calls_today: int=0`.
  - `list_metrics() -> list[str]` — `["spy","spy_change_pct","qqq","qqq_change_pct","gld","tlt"]`.
  - `async fetch_all() -> list[DataPoint]` — budget gate against `ALPHA_VANTAGE_DAILY_CALL_BUDGET`; `asyncio.sleep(ALPHA_VANTAGE_PACE_SEC)` between symbols (not before first).
  - `async _fetch_symbol(session, symbol, api_key) -> list[DataPoint]` — parses `"05. price"`, `"10. change percent"`.
  - `_metric_for(symbol) -> str`.
  - `_reset_budget_if_new_day() -> None`.
  - `get_spy() / get_spy_change_pct() / get_qqq() / get_gold() -> Optional[float]`.

### `data_sources/sources/binance_futures.py`
- `class BinanceFuturesSource(BaseDataSource)`: `source_id="binance_futures"`, `refresh_interval=settings.BINANCE_FUTURES_REFRESH_SEC`, `requires_api_key=False`.
  - `list_metrics()` → `["open_interest","funding_rate","long_short_ratio"]`.
  - `async fetch_all()` — gathers `_fetch_for_symbol` per `settings.BINANCE_FUTURES_SYMBOLS`.
  - `async _fetch_for_symbol(session, symbol)` — hits `/fapi/v1/openInterest`, `/fapi/v1/fundingRate?limit=1`, `/futures/data/topLongShortAccountRatio?period=1h&limit=1`.
  - `_fmt(symbol)` → `"BTC/USDT"`; `_float(v)`.
  - `get_open_interest(pair="BTC/USDT")`, `get_funding(...)`, `get_long_short_ratio(...)`.

### `data_sources/sources/bybit_derivs.py`
- `class BybitDerivsSource(BaseDataSource)`: `source_id="bybit_derivs"`, `refresh_interval=settings.BYBIT_REFRESH_SEC`.
  - `list_metrics()` → `["open_interest","funding_rate","mark_price","change_pct_24h"]`.
  - `async fetch_all()` — gathers per `settings.BYBIT_SYMBOLS`.
  - `async _fetch_for_symbol(session, symbol)` — `/v5/market/tickers?category=linear&symbol=...`; converts `price24hPcnt` decimal to %.
  - `_fmt`, `_float`; `get_funding`, `get_open_interest`, `get_mark_price`.

### `data_sources/sources/cftc_cot.py`
- `class CFTCCOTSource(BaseDataSource)`: `source_id="cftc_cot"`, `refresh_interval=settings.CFTC_REFRESH_SEC`.
  - `list_metrics()` → `["spec_net","spec_long","spec_short","open_interest","long_short_ratio"]`.
  - `async fetch_all()` — SODA query on `settings.CFTC_BASE_URL` filtered by `market_and_exchange_names=settings.CFTC_CONTRACT`, ordered desc by date, limit 1.
  - `get_spec_net / get_spec_long / get_spec_short / get_long_short_ratio`.

### `data_sources/sources/coingecko.py`
- Constants: `PUBLIC_BASE = "https://api.coingecko.com/api/v3"`, `PRO_BASE = "https://pro-api.coingecko.com/api/v3"`.
- `class CoinGeckoSource(BaseDataSource)`: `source_id="coingecko"`, `refresh_interval=settings.COINGECKO_REFRESH_SEC`, `requires_api_key=False`, `api_key_env_var="COINGECKO_API_KEY"`.
  - `list_metrics()` → `["btc_dominance","eth_dominance","total_market_cap","total_volume_24h","market_cap_change_pct_24h"]`.
  - `async fetch_all()` — calls `/global`.
  - `_auth() -> tuple[str, dict]` — picks base+header by `settings.COINGECKO_USE_PRO` and key presence (`x-cg-pro-api-key` / `x-cg-demo-api-key`).
  - `get_btc_dominance / get_eth_dominance / get_total_market_cap / get_total_volume_24h / get_market_cap_change_pct_24h`.

### `data_sources/sources/coinglass.py`
- Constants: `BASE = "https://open-api.coinglass.com/public/v2"`.
- `class CoinglassSource(BaseDataSource)`: `source_id="coinglass"`, `refresh_interval=settings.COINGLASS_REFRESH_SEC`.
  - `__init__()` — `_rate_limit = asyncio.Semaphore(settings.COINGLASS_RATE_LIMIT_PER_MIN)`.
  - `list_metrics()` → `["funding_rate","open_interest","liquidations_long_24h","liquidations_short_24h","long_short_ratio"]`.
  - `async fetch_all()` — gathers per `settings.COINGLASS_WATCH_PAIRS`.
  - `async _fetch_for_symbol(session, pair)` — strips `/USDT` to base symbol, hits `/funding_rates_chart`, `/open_interest_chart`, `/liquidation_chart`, `/long_short_ratio`.
  - `async _safe_get(session, path, params) -> dict` — semaphore-guarded.
  - `_extract_latest(payload, key) -> Optional[float]` — tolerates dict/list shapes.
  - `get_funding(pair) / get_open_interest / get_long_short_ratio / get_liquidations_24h(pair) -> dict` (long/short/total).

### `data_sources/sources/cryptocompare.py`
- `class CryptoCompareSource(BaseDataSource)`: `source_id="cryptocompare"`, `refresh_interval=settings.CRYPTOCOMPARE_REFRESH_SEC`, `requires_api_key=True`, `api_key_env_var="CRYPTOCOMPARE_API_KEY"`.
  - `list_metrics()` → `["price","volume_24h","market_cap","change_pct_24h"]`.
  - `async fetch_all()` — `/pricemultifull?fsyms=...&tsyms=...`; parses `RAW[fsym][tsym]`.
  - `get_price(fsym="BTC") / get_volume_24h / get_market_cap / get_change_pct_24h`.

### `data_sources/sources/ecb.py`
- `class ECBSource(BaseDataSource)`: `source_id="ecb"`, `refresh_interval=settings.ECB_REFRESH_SEC`.
  - `list_metrics()` — derived from `settings.ECB_SERIES` (tuples `(dataflow, key, metric)`).
  - `async fetch_all()` — gathers `_fetch_one` per series tuple.
  - `async _fetch_one(session, dataflow, key) -> Optional[float]` — SDMX-JSON `lastNObservations=1`.
  - `get_refinancing_rate / get_deposit_facility_rate / get_eur_usd`.

### `data_sources/sources/frankfurter.py`
- Constants: `DXY_LEGS: list[tuple[str, float]]` (6 pairs with signed exponents), `DXY_BASE = 50.14348112`, `_PAIR_FETCH: dict[str, tuple[str, str]]` (pair → base/quote).
- `class FrankfurterSource(BaseDataSource)`: `source_id="frankfurter"`, `refresh_interval=settings.FRANKFURTER_REFRESH_SEC`.
  - Class attr `METRICS = ("fx_rate","fx_change_24h","dxy","dxy_change_24h")`.
  - `list_metrics()` → list(METRICS).
  - `async fetch_all()` — `/latest` and `/<prev_date>` for 24h delta.
  - `get_dxy / get_dxy_change_24h / get_eur_usd / get_gbp_usd / get_usd_jpy`.
  - `is_dxy_strong() -> bool` — `dxy >= settings.DATA_DXY_STRONG_THRESHOLD`.
  - `is_dxy_weak() -> bool` — `dxy <= settings.DATA_DXY_WEAK_THRESHOLD`.
  - `async _fetch_rates(pairs, target_date=None) -> dict` — groups by base currency for fewer round trips.
  - `_dxy_proxy(rates) -> Optional[float]` — `DXY_BASE * Π rate^exp`; None if any leg missing.

### `data_sources/sources/fred.py`
- Constants: `OBS_ENDPOINT = "https://api.stlouisfed.org/fred/series/observations"`, `_SERIES_TO_METRIC` (9 entries: CPIAUCSL, DGS10, DGS2, DGS30, DFF, M2SL, UNRATE, T10Y2Y, VIXCLS), `_YOY_SERIES_TO_METRIC = {"CPIAUCSL": "cpi_yoy"}`.
- `class FREDSource(BaseDataSource)`: `source_id="fred"`, `refresh_interval=settings.FRED_REFRESH_SEC`, `requires_api_key=True`, `api_key_env_var="FRED_API_KEY"`.
  - `list_metrics()` — base + YoY metrics filtered by `settings.FRED_SERIES`.
  - `async fetch_all()` — per-series fetch + YoY (`units=pc1`) fetch.
  - `async _fetch_latest(session, series_id, api_key, units=None) -> tuple[Optional[float], str]` — skips `"."` placeholder, limit=5.
  - Accessors: `get_cpi / get_cpi_yoy / get_10y_yield / get_2y_yield / get_30y_yield / get_fed_funds / get_m2 / get_unemployment / get_yield_curve_spread` (falls back to 10y-2y subtraction), `is_yield_curve_inverted() -> bool`, `get_vix() -> Optional[float]`.
  - `get_risk_sentiment() -> str` — `"RISK_ON"|"NEUTRAL"|"RISK_OFF"|"CRISIS"|"UNKNOWN"` against `settings.DATA_VIX_RISK_ON_MAX / DATA_VIX_RISK_OFF_MIN / DATA_VIX_CRISIS_MIN`.

### `data_sources/sources/imf.py`
- `class IMFSource(BaseDataSource)`: `source_id="imf"`, `refresh_interval=settings.IMF_REFRESH_SEC`.
  - `list_metrics()` — `settings.IMF_INDICATORS.keys()`.
  - `async fetch_all()` — one HTTP per indicator; emits one DataPoint per (indicator × country) symbol.
  - `async _fetch_indicator(session, metric, imf_code) -> dict`.
  - `_latest(series) -> tuple[Optional[str], Optional[float]]` — most recent year with a value.
  - `get_gdp_per_capita(country="USA") / get_inflation_yoy / get_unemployment`.

### `data_sources/sources/us_treasury.py`
- Constants: `_HEADER_TO_METRIC` (9 tenors 1m..30y).
- `class USTreasurySource(BaseDataSource)`: `source_id="us_treasury"`, `refresh_interval=settings.US_TREASURY_REFRESH_SEC`.
  - `list_metrics()` — header values + `"yield_curve_2_10_spread"`.
  - `async fetch_all()` — fetches current YYYYMM CSV; parses first data row; derives 2y/10y spread.
  - `get_10y_yield / get_2y_yield / get_30y_yield / get_yield_curve_spread`.

### `data_sources/sources/world_bank.py`
- `class WorldBankSource(BaseDataSource)`: `source_id="world_bank"`, `refresh_interval=settings.WORLD_BANK_REFRESH_SEC`.
  - `list_metrics()` — `settings.WORLD_BANK_INDICATORS.keys()`.
  - `async fetch_all()` — gathers one HTTP per (country × indicator) pair.
  - `async _fetch_one(session, country, wb_code) -> tuple[Optional[float], Optional[str]]` — 5-year lookback, walks page for first non-null value.
  - `get_gdp_growth(country="USA") / get_inflation / get_unemployment`.

## Plugin registrations
`data_sources/sources/__init__.py:REGISTERED_SOURCES` — 13 entries instantiated at import time:

| Order | source_id | Class | refresh setting | API key env var | `is_available()` |
|---:|---|---|---|---|---|
| 1 | `coinglass` | `CoinglassSource` | `COINGLASS_REFRESH_SEC` | — | always True (no key) |
| 2 | `coingecko` | `CoinGeckoSource` | `COINGECKO_REFRESH_SEC` | `COINGECKO_API_KEY` (optional; raises rate limits only) | always True (`requires_api_key=False`) |
| 3 | `cryptocompare` | `CryptoCompareSource` | `CRYPTOCOMPARE_REFRESH_SEC` | `CRYPTOCOMPARE_API_KEY` | True iff key set |
| 4 | `binance_futures` | `BinanceFuturesSource` | `BINANCE_FUTURES_REFRESH_SEC` | — | always True |
| 5 | `bybit_derivs` | `BybitDerivsSource` | `BYBIT_REFRESH_SEC` | — | always True |
| 6 | `cftc_cot` | `CFTCCOTSource` | `CFTC_REFRESH_SEC` | — | always True |
| 7 | `fred` | `FREDSource` | `FRED_REFRESH_SEC` | `FRED_API_KEY` | True iff key set |
| 8 | `alpha_vantage` | `AlphaVantageSource` | `ALPHA_VANTAGE_REFRESH_SEC` | `ALPHA_VANTAGE_API_KEY` | True iff key set |
| 9 | `frankfurter` | `FrankfurterSource` | `FRANKFURTER_REFRESH_SEC` | — | always True |
| 10 | `ecb` | `ECBSource` | `ECB_REFRESH_SEC` | — | always True |
| 11 | `us_treasury` | `USTreasurySource` | `US_TREASURY_REFRESH_SEC` | — | always True |
| 12 | `world_bank` | `WorldBankSource` | `WORLD_BANK_REFRESH_SEC` | — | always True |
| 13 | `imf` | `IMFSource` | `IMF_REFRESH_SEC` | — | always True |

`is_available()` is the base implementation everywhere — no source overrides it. The base returns `True` when `requires_api_key=False`, else `bool(os.getenv(api_key_env_var, ""))`. CoinGecko explicitly leaves `requires_api_key=False` even though it exposes `api_key_env_var="COINGECKO_API_KEY"` so the source still runs on the public tier when no key is configured.

## API key env vars required

| Source | Env var | Required? |
|---|---|---|
| `alpha_vantage` | `ALPHA_VANTAGE_API_KEY` | yes (source disabled without it) |
| `cryptocompare` | `CRYPTOCOMPARE_API_KEY` | yes (source disabled without it) |
| `fred` | `FRED_API_KEY` | yes (source disabled without it) |
| `coingecko` | `COINGECKO_API_KEY` | optional — only switches header/base to demo/pro tier |
| `coinglass`, `binance_futures`, `bybit_derivs`, `cftc_cot`, `frankfurter`, `ecb`, `us_treasury`, `world_bank`, `imf` | — | none |

## Imports graph

### Imports from project
- `data_sources/__init__.py` → `database.queries` (for `log_data_point`), `data_sources.base`, lazy `data_sources.sources.REGISTERED_SOURCES`.
- `data_sources/base.py` → stdlib only (`abc`, `dataclasses`, `logging`, `time`, `typing`).
- `data_sources/sources/__init__.py` → all 13 concrete source modules.
- Every concrete source → `config.settings`, `data_sources.base`, `aiohttp`, stdlib.

### Imported by (project-wide)
- `core/bot.py` — `from data_sources import data_sources as ds` (3 lazy imports inside methods).
- `macro/monitor.py` — `from data_sources import data_sources as ds` (lazy inside `_refresh_inputs`).
- `execution/arb_engine.py` — `from data_sources import data_sources as ds` (2 lazy imports).
- `tests/test_data_sources.py`, `tests/test_arb_engine.py` (`import data_sources as ds_mod`).

Documentation references: `prompts/build_data_sources.md`, `prompts/build_data_sources_coinglass.md`, `prompts/build_funding_arb.md`, `prompts/build_macro.md`.

## Tests
Only `tests/test_data_sources.py` imports directly from `data_sources/`. Test breakdown (one line each):

- `test_base_caches_within_refresh_interval` — second `get` reuses cache within `refresh_interval`.
- `test_base_refreshes_when_stale` — `refresh_interval=0` forces a fresh fetch each call.
- `test_base_fetch_failure_keeps_cached_value` — `fetch_all` raising must not clobber the cache.
- `test_cached_value_is_sync` — `cached_value` returns the float; default kicks in on miss.
- `test_datapoint_key_with_and_without_symbol` — key format `source.metric[.symbol]`.
- `test_frankfurter_is_always_available` — keyless source is always available.
- `test_alpha_vantage_requires_key` — gated by `ALPHA_VANTAGE_API_KEY`.
- `test_fred_requires_key` — gated by `FRED_API_KEY`.
- `test_coinglass_no_key_required` — always available.
- `test_coingecko_works_without_key` — public-tier availability.
- `test_coingecko_auth_picks_right_header` — empty key → no header; demo key → `x-cg-demo`; pro flag → `x-cg-pro` on pro base.
- `test_coingecko_metrics_and_convenience_methods` — accessor wires through `_cache`.
- `test_coingecko_parses_global_payload` — happy-path payload → expected DataPoints.
- `test_coingecko_returns_error_point_on_empty_payload` — empty/error payload → DataPoint with `.error`.
- `test_each_source_lists_expected_metrics` — `list_metrics` sanity per registered source.
- `test_cryptocompare_requires_key` — gated by key env var.
- `test_no_key_sources_are_available` — eight keyless sources all True.
- `test_binance_futures_symbol_format` — `BTCUSDT` → `BTC/USDT`.
- `test_us_treasury_csv_parsing` — first data row parsed, spread derived.
- `test_cftc_parses_latest_row` — non-comm long/short/net/ratio extracted.
- `test_fred_risk_sentiment_thresholds` — VIX 10/20/28/40 → RISK_ON/NEUTRAL/RISK_OFF/CRISIS.
- `test_alpha_vantage_does_not_carry_vix` — explicitly verifies VIX moved to FRED.
- `test_alpha_vantage_paces_calls_between_symbols` — `PACE_SEC` sleep between consecutive symbols, not before first.
- `test_frankfurter_dxy_strength` — `is_dxy_strong/weak` thresholds.
- `test_frankfurter_dxy_proxy_formula` — geometric DXY against May-2026-ish snapshot lands in 95-105.
- `test_frankfurter_dxy_proxy_missing_leg_returns_none` — partial input → None.
- `test_fred_yield_curve_inversion_falls_back_to_legs` — when spread series absent, subtract 10y-2y.
- `test_aggregator_refresh_all_succeeds_concurrently` — concurrent refresh + DB log.
- `test_aggregator_isolates_one_failing_source` — bad source doesn't break good source.
- `test_aggregator_unavailable_source_is_skipped` — `is_available=False` → `fetch_all` not called.
- `test_aggregator_get_returns_datapoint` — `get` returns cached DataPoint; unknown source → None.
- `test_latest_snapshot_flattens_every_cache` — snapshot keys include every source.metric.
- `test_aggregator_never_imports_concrete_sources` — `inspect.getsource(data_sources)` must not name any concrete source class.
- `test_registered_sources_picked_up_automatically` — passing a new instance to `DataSources` exposes it as an attribute.
- `test_subscribe_fires_on_value_change` — first fire has `previous=None`; subsequent has populated `previous`.
- `test_subscribe_wildcard_matches_any_symbol` — `cg.funding_rate.*` matches both BTC and ETH symbols.
- `test_subscribe_filter_blocks_callback` — filter predicate False → callback skipped.
- `test_callback_exception_does_not_break_source` — throwing callback doesn't prevent the safe one.
- `test_unsubscribe_stops_notifications` — `unsubscribe(sub_id)` removes the listener.
- `test_multiple_subscribers_same_pattern` — both fire.
- `test_quality_gate_consumes_macro_modifier` — score = 80 raw − 10 macro = 70.
- `test_quality_gate_missing_macro_does_not_block` — macro raising doesn't block gate.
- `test_log_data_point_round_trip` — `log_data_point` + `get_latest_data_point` symmetry.
- `test_get_data_history_returns_chronological` — three logged points come back in chrono order.
- `test_get_data_at_time` — picks the row immediately preceding the cutoff.
- `test_get_funding_rates_empty_when_nothing_cached` — empty dict, not None.
- `test_get_funding_rates_returns_symbol_to_rate_map` — `{symbol: rate}` shape.
- `test_get_funding_rates_skips_errored_points` — error-state points excluded.
- `test_get_funding_rates_prefers_newest_when_multiple_sources` — newest timestamp wins.
- `test_get_latest_returns_cached_datapoint` — sync read, no fetch.
- `test_get_latest_returns_none_for_unknown_source` — unknown source → None.
- `test_coinglass_source_has_rate_limit_semaphore` — semaphore sized to `COINGLASS_RATE_LIMIT_PER_MIN`.

`tests/test_arb_engine.py` imports `data_sources as ds_mod` to monkeypatch the singleton when verifying the funding-rate arb engine; not a `data_sources` unit test.

## TODOs / FIXMEs / stubs
No `TODO`, `FIXME`, `XXX`, or `HACK` comments anywhere in `data_sources/`. (The string "stub" appears once at `data_sources/__init__.py:70` — `"Deferred import so test harnesses can stub REGISTERED_SOURCES"` — which is a docstring describing the deferred-import design and is not a stub marker.)

## Known issues observed
- `data_sources/sources/us_treasury.py:58` uses `datetime.utcnow()` (deprecated in Python 3.12+; the project pins 3.11 so it still works but will warn on future bumps). `world_bank.py:97` does the same via the locally-aliased `_dt`.
- `frankfurter.py` `_fetch_rates()` loop variable shadows the outer `base` URL: `for base, quotes in bases.items()` reuses the name of the URL `base = settings.FRANKFURTER_BASE_URL` defined a few lines earlier. Works because `path` is computed before the loop, but it's a footgun if anyone later moves URL construction inside the loop.
- `frankfurter.py` opens a fresh `aiohttp.ClientSession` inside `_fetch_rates` and `fetch_all` calls `_fetch_rates` twice (today + yesterday) → two sessions per refresh instead of one shared session. Minor: sessions are short-lived and the source-level cache means this fires once per `FRANKFURTER_REFRESH_SEC`.
- `binance_futures.py`/`bybit_derivs.py`/`coinglass.py` all use `or 0.0` / `or 1.0` defaults on numeric fields — masks the difference between "the API returned 0" and "the API returned nothing". The error flag is set correctly when the parse fails, but `cached_value()` returns `default` only when `error is not None`, so a genuine 0.0 reading and a missing-with-fallback 0.0 are indistinguishable to readers that don't inspect `point.error`.
- `coinglass.py:30` hardcodes `BASE = "https://open-api.coinglass.com/public/v2"` instead of going through `settings`. Every other source pulls its host from settings.
- `frankfurter.py` hardcodes `DXY_BASE = 50.14348112` and the leg exponents — by `CLAUDE.md` convention these are magic numbers that should live in `config/settings.py`. Defensible since they're ICE-spec constants and not tunable, but the file is the only place in `data_sources/` doing this.
- `coingecko.py`'s `_auth()` always picks `PRO_BASE` only when *both* `COINGECKO_USE_PRO` and a key are set, but the demo header is sent on the public base with any key — there's no validation that a pro-format key (`CG-…`) isn't being sent with `x-cg-demo-api-key`.
- `frankfurter.py:202` calls `base = settings.FRANKFURTER_BASE_URL` once and notes in the comment "v2 ... currently 404s on /latest" — implicit dependency on a settings choice that ties this source to a frozen v1 API.
- No source overrides `is_available()` beyond the default key-presence check — no network/import-reachability probe, so a configured source that can't actually reach its host will be silently treated as available and fail per-refresh through the base error path.
- `imf.py:73` and `world_bank.py:71` insert a DataPoint with `value=0.0, error="no_data"` when no observation exists. Because `BaseDataSource._do_fetch` won't clobber an existing good point with an error placeholder, this works on second runs — but on the *first* run for a brand-new country×indicator pair the cache will contain a `value=0.0` point that `cached_value()` then returns as `None` (because `error` is set). Consistent with intent but a future change to `cached_value()` would need to remember this contract.
- `data_sources/__init__.py:248` schedules subscriber callbacks with `asyncio.create_task` but no reference is kept — long-running callbacks can be garbage-collected mid-flight on Python 3.10+ if the loop has no other reference. Practical risk low because the tasks finish quickly, but worth noting.

---

# Module Report: macro

## Purpose
The `macro/` package is the project's macro-economy awareness layer. It turns external macro inputs (VIX, DXY, the 10y-2y yield curve, fed funds rate, CPI YoY) into:

- A **three-layer regime model**: dimensional flag enums (`DollarStrength`, `RiskAppetite`, `RateEnvironment`, `VolRegime`) → a single-label `MacroScenario` from a priority-ordered ladder (`CRISIS` first, `NEUTRAL` last) → a clamped numeric `macro_score` in `[-100, +100]`.
- A **step-ladder signal modifier** (`get_signal_modifier()`) returning one of `{-15, -10, -5, 0, +5, +10, +15}`, intentionally mirroring `sentiment._composite_to_modifier`.
- A **hard-block flag** (`is_hard_blocked()` / `MacroRegime.is_hard_block`) that fires on `MacroScenario.CRISIS`, mirroring the sentiment news guard.
- An **economic-calendar awareness layer** through a plugin registry of `BaseCalendarSource` subclasses; the monitor exposes upcoming `PendingEvent`s for the dashboard's pending-events panel and the bot's pre-event pause.
- A list of discrete `MacroSignal`s (e.g. `VIX_CRISIS`, `YIELD_CURVE_INVERSION`, `DOLLAR_STRENGTH`) generated each refresh for a future `MacroAgent` consumer (currently computed but unused).

**Feed into QualityGate.** In `signals/quality_gate.py:142` (section "5b. Macro modifier"), the gate calls `macro_monitor.get_signal_modifier()`, adds it to `score`, and stamps `signal.indicators["macro_modifier"]` plus `signal.indicators["macro_scenario"]` when a regime is cached. This sits **after** the regime modifier (5a) and **before** OFI/sentiment in the additive composition described by `CLAUDE.md`. The CRISIS hard-block (`MACRO_HARD_BLOCK_SKIP_REASON = "MACRO_HARD_BLOCK"`) is wired separately. The session/pre-event pause referenced in the spec is sourced from `MACRO_PRE_EVENT_PAUSE_MINUTES` against `get_pending_events()`. Macro never owns the `session_modifier` — that's a separate concern; macro contributes only the score modifier and CRISIS short-circuit.

## Subpackages
- `macro/sources/` — calendar/event feeds. `BaseCalendarSource` ABC + concrete sources (`FREDCalendarSource`, `StubCalendarSource`) + an ordered `REGISTERED_CALENDAR_SOURCES` registry. Same 5-step plugin pattern as `sentiment/` and `data_sources/` (documented in `macro/sources/__init__.py`).

## Files
| File | LOC | One-sentence summary |
|---|---|---|
| `macro/__init__.py` | 48 | Re-exports the public surface (`MacroMonitor`, `macro_monitor` singleton, regime enums, signal dataclasses, `BaseCalendarSource`) and instantiates the module-level singleton. |
| `macro/monitor.py` | 517 | `MacroMonitor` orchestrator: per-component score curves, dimensional classifiers, scenario priority ladder, `refresh()`/`refresh_calendar()`/`run_refresh_loop()`, signal modifier, hard-block flag, pending-events read-through with DB fallback, derived `MacroSignal` list. |
| `macro/regime.py` | 98 | Dimensional enums (`DollarStrength`, `RiskAppetite`, `RateEnvironment`, `VolRegime`), the `MacroScenario` enum (priority-ordered), and the `MacroRegime` dataclass with clamped post-init invariants and `is_hard_block` property. |
| `macro/signals.py` | 110 | `EventImpact` enum + `CalendarEvent` (plugin-emitted), `PendingEvent` (dashboard view with computed `minutes_until`), and `MacroSignal` (discrete event for the future `MacroAgent`). |
| `macro/sources/__init__.py` | 42 | Ordered `REGISTERED_CALENDAR_SOURCES` registry + 5-step plugin docstring for adding a source. |
| `macro/sources/base.py` | 46 | `BaseCalendarSource` ABC: class attrs (`source_id`, `display_name`, `refresh_interval`, `optional`, `requires_api_key`, `api_key_env_var`), default `is_available()` that env-var-checks when `requires_api_key`, abstract `fetch_events()`. |
| `macro/sources/fred_calendar.py` | 165 | Real source: FRED `/fred/releases/dates` endpoint, keyword→`EventImpact` mapping, default 13:30 UTC release time with FOMC override to 18:00, drops `LOW`-impact entries. |
| `macro/sources/stub_calendar.py` | 51 | Placeholder source — always `is_available() == False`, exists in the registry to keep the plugin docstring discoverable. |

## Public surface

### `macro/__init__.py`
- Module docstring: lists the public surface and points at `macro/sources/__init__.py` for the plugin docstring.
- Module-level singleton: `macro_monitor = MacroMonitor()` (CLAUDE.md singleton pattern).
- `__all__`: `MacroMonitor`, `macro_monitor`, `MacroRegime`, `MacroScenario`, `DollarStrength`, `RiskAppetite`, `RateEnvironment`, `VolRegime`, `CalendarEvent`, `PendingEvent`, `EventImpact`, `MacroSignal`, `BaseCalendarSource`.

### `macro/monitor.py`
Module docstring: "reads data_sources singleton + calendar plugin, computes a MacroRegime each tick, exposes a step-ladder signal modifier". Notes that the monitor never fetches external APIs directly except calendar events.

Module-level functions (per-component score curves, all returning floats in `[-100, +100]`):
- `_vix_score(vix: Optional[float]) -> float` — bands keyed off `settings.MACRO_VIX_CALM / MACRO_VIX_ELEVATED / MACRO_VIX_CRISIS`; returns 100 (<calm), 30 (calm-elevated), -60 (elevated-crisis), -100 (>=crisis), 0 if None.
- `_dollar_score(dxy: Optional[float]) -> float` — 100 if `dxy <= MACRO_DXY_WEAK`, -100 if `dxy >= MACRO_DXY_STRONG`, 0 otherwise (and 0 if None).
- `_yield_curve_score(spread: Optional[float]) -> float` — -100 if `spread < MACRO_YIELD_CURVE_INVERSION`, 60 if `spread > 0.5`, 0 otherwise.
- `_rate_env_score(rates: RateEnvironment) -> float` — 100 for `EASING`, -100 for `TIGHTENING`, 0 for `NEUTRAL`.
- `_inflation_score(cpi_yoy: Optional[float]) -> float` — 100 if `cpi_yoy < MACRO_INFLATION_LOW`, -100 if `cpi_yoy >= MACRO_INFLATION_HIGH`, 0 otherwise.
- `_score_to_modifier(macro_score: float) -> int` — step ladder: `>=60`→+15, `>=40`→+10, `>=20`→+5, `<=-60`→-15, `<=-40`→-10, `<=-20`→-5, else 0. Docstring: "mirrors sentiment._composite_to_modifier".

Class `MacroMonitor`:
- Docstring: "Reads macro inputs from data_sources, computes a MacroRegime, exposes pull API for the bot loop + dashboard. Construct with no args to use REGISTERED_CALENDAR_SOURCES. Tests inject their own list to pin behaviour."
- Instance attrs: `_calendar_sources: list`, `_regime: Optional[MacroRegime]`, `_signals: list[MacroSignal]`, `_events: list[CalendarEvent]`, `_last_calendar_fetch: float`, `_running: bool`.
- `__init__(self, calendar_sources: Optional[list] = None)` — instantiates each class in `REGISTERED_CALENDAR_SOURCES` when no list is given.
- `async refresh(self) -> MacroRegime` — pulls inputs via deferred `from data_sources import data_sources`, derives flags + scenario + score + confidence, persists via `db_queries.save_macro_regime` (best-effort), caches `_regime` and `_signals`. Always returns a regime even when everything fails.
- `get_current_regime(self) -> Optional[MacroRegime]` — latest cached regime or `None`.
- `get_signal_modifier(self) -> int` — `_score_to_modifier(self._regime.macro_score)` or `0` when no regime is cached.
- `is_hard_blocked(self) -> bool` — `True` iff cached regime's scenario is `CRISIS`.
- `get_macro_signals(self) -> list[MacroSignal]` — copy of last derived `MacroSignal` list.
- `async refresh_calendar(self) -> list[CalendarEvent]` — iterates `_calendar_sources`, skips when `is_available() == False`, swallows per-source exceptions, persists via `db_queries.save_calendar_events`, updates `_last_calendar_fetch`.
- `get_pending_events(self, n: int = 3) -> list[PendingEvent]` — in-memory future events first, then DB fallback via `db_queries.get_pending_events(hours_ahead=72)`, sorted ascending by `scheduled_utc`, capped at `n`.
- `async run_refresh_loop(self) -> None` — refreshes the calendar once before the first regime read, then loops `refresh()` + hourly `refresh_calendar()` with `await asyncio.sleep(settings.MACRO_REFRESH_INTERVAL_SEC)`. Caller cancels.
- `stop(self) -> None` — flips `_running = False`.
- `_classify_dollar(self, dxy) -> DollarStrength`, `_classify_vol(self, vix) -> VolRegime`, `async _classify_rates(self, fed_funds) -> RateEnvironment` (looks up `db_queries.get_data_at_time("fred", "fed_funds", ref_time)` for delta vs `MACRO_RATE_LOOKBACK_DAYS` ago), `_classify_risk(self, vol, dollar) -> RiskAppetite`.
- `_scenario_for(self, dollar, risk, rates, vol, yield_curve, cpi_yoy) -> MacroScenario` — priority ladder: CRISIS → RISK_OFF (elevated vol + inverted-or-strong-dollar) → STAGFLATION → TIGHTENING_CYCLE → EASING_CYCLE → REFLATION → GOLDILOCKS → NEUTRAL.
- `_safe(self, ds, source_id, method) -> Optional[float]` — wraps `getattr(ds, source_id).method()` in try/except.
- `_confidence(self, **fields) -> float` — fraction of macro inputs present; halved when `db_queries.get_latest_data_point("fred", "vix")` is older than `MACRO_CONFIDENCE_STALE_HOURS`.
- `_derive_signals(self, regime) -> list[MacroSignal]` — emits `VIX_CRISIS`/`VIX_ELEVATED`, `YIELD_CURVE_INVERSION`, `DOLLAR_STRENGTH` events.

Module constants (used as score-band reference for tests that pin behaviour): all bands route through `settings.MACRO_*` — there are no in-module magic numbers in the score curves.

### `macro/regime.py`
Module docstring: documents the three-layer model (dimensional flags → scenario → numeric score).
- `class DollarStrength(enum.Enum)`: `STRONG`, `NEUTRAL`, `WEAK`.
- `class RiskAppetite(enum.Enum)`: `RISK_ON`, `NEUTRAL`, `RISK_OFF`.
- `class RateEnvironment(enum.Enum)`: `TIGHTENING`, `NEUTRAL`, `EASING`.
- `class VolRegime(enum.Enum)`: `CALM`, `ELEVATED`, `CRISIS`.
- `class MacroScenario(enum.Enum)`: priority-ordered `CRISIS`, `RISK_OFF`, `STAGFLATION`, `TIGHTENING_CYCLE`, `EASING_CYCLE`, `REFLATION`, `GOLDILOCKS`, `NEUTRAL`. Docstring: "most severe scenarios are tested first in MacroMonitor.scenario_for() so they win when multiple labels could apply."
- `@dataclass class MacroRegime`:
  - Required fields: `scenario: MacroScenario`, `dollar: DollarStrength`, `risk: RiskAppetite`, `rates: RateEnvironment`, `vol: VolRegime`, `macro_score: float`.
  - Optional fields (default `None`): `dxy`, `vix`, `yield_10y`, `yield_2y`, `yield_curve`, `fed_funds_rate`, `cpi_yoy`.
  - `last_updated: float = 0.0`, `confidence: float = 0.0`, `raw_data: dict = field(default_factory=dict)`.
  - `__post_init__(self)` — defaults `last_updated` to `time.time()` and clamps `macro_score` to `[-100, 100]`, `confidence` to `[0, 1]`.
  - `@property is_hard_block(self) -> bool` — `self.scenario is MacroScenario.CRISIS`.

### `macro/signals.py`
Module docstring: "Dataclasses for calendar events + macro signal generation." Notes that `CalendarEvent` lives in the macro module rather than `database/models` so plugins don't need a SQLAlchemy dependency.
- `class EventImpact(enum.Enum)`: `HIGH`, `MEDIUM`, `LOW`.
- `@dataclass class CalendarEvent`:
  - Fields: `event_id: str`, `title: str`, `country: str`, `scheduled_utc: datetime`, `impact: EventImpact`, `source_id: str`, optional `actual / forecast / previous: Optional[float] = None`.
  - Docstring: `event_id` must be stable across refreshes for DB upserts (e.g. `"fred:release_<id>:2026-05-21"`).
  - `minutes_until(self, now: Optional[datetime] = None) -> int` — handles both tz-aware and naive `scheduled_utc`, returns negative when past.
- `@dataclass class PendingEvent`:
  - Fields: `title`, `country`, `scheduled_utc`, `minutes_until: int`, `impact: EventImpact`.
  - `@classmethod from_calendar_event(cls, ev: CalendarEvent, now: Optional[datetime] = None) -> PendingEvent`.
- `@dataclass class MacroSignal`:
  - Fields: `signal_id: str`, `signal_type: str` (e.g. `"VIX_ELEVATED"`, `"YIELD_CURVE_INVERSION"`), `direction: str` (`"RISK_ON" | "RISK_OFF"`), `strength: float` (0..1), `description: str`, `source_metrics: dict = field(default_factory=dict)`, `timestamp: float = 0.0`.
  - `__post_init__(self)` — defaults `timestamp` to `time.time()` and clamps `strength` to `[0, 1]`.

### `macro/sources/__init__.py`
Module docstring is the 5-step plugin recipe.
- Module constant: `REGISTERED_CALENDAR_SOURCES: list[type] = [FREDCalendarSource, StubCalendarSource]`. Stub stays in the list so its docstring is greppable.

### `macro/sources/base.py`
Module docstring: ABC mirrors `BaseSentimentSource` / `BaseDataSource`; intentionally omits cache/staleness because calendar events are discrete.
- `class BaseCalendarSource(ABC)`:
  - Class attrs (override per source): `source_id: str = "base_calendar"`, `display_name: str = "Base Calendar"`, `refresh_interval: int = 3600`, `optional: bool = True`, `requires_api_key: bool = False`, `api_key_env_var: str = ""`.
  - `@abstractmethod async fetch_events(self) -> list` — docstring requires returning `[]` on failure, never raising.
  - `is_available(self) -> bool` — default: `True` if `not requires_api_key`, else `bool(os.getenv(self.api_key_env_var, ""))`.

### `macro/sources/fred_calendar.py`
Module docstring lists caveats: FRED publishes dates only (defaults to 13:30 UTC, 18:00 UTC for FOMC), `include_release_dates_with_no_data=true`, all releases are hard-coded `country = "US"`.

Module constants:
- `RELEASES_ENDPOINT = "https://api.stlouisfed.org/fred/releases/dates"`.
- `_HIGH_KEYWORDS = ("fomc", "consumer price index", "employment situation", "personal consumption expenditures", "gross domestic product")`.
- `_MEDIUM_KEYWORDS = ("producer price index", "retail", "initial claims", "unemployment insurance", "housing starts", "existing home sales", "new residential sales", "industrial production", "ism manufacturing", "consumer sentiment")`.
- `_DEFAULT_HOUR_UTC = 13`, `_DEFAULT_MINUTE_UTC = 30`, `_FOMC_HOUR_UTC = 18`.

`class FREDCalendarSource(BaseCalendarSource)`:
- Class attrs: `source_id = "fred_calendar"`, `display_name = "FRED Release Calendar"`, `refresh_interval = 3600`, `optional = True`, `requires_api_key = True`, `api_key_env_var = "FRED_API_KEY"`.
- `__init__(self, lookahead_days: int = 14)`.
- `async fetch_events(self) -> list[CalendarEvent]` — uses `aiohttp` with timeout `settings.DATA_SOURCES_HTTP_TIMEOUT_SEC`; catches any HTTP failure to `return []` and log a warning.
- `_parse_entry(self, entry: dict) -> Optional[CalendarEvent]` — drops `EventImpact.LOW` events to keep the panel focused; emits `event_id=f"fred:{release_id}:{date_str}"`.
- `_impact_for(self, name: str) -> EventImpact` — lowercase substring match; HIGH checked before MEDIUM by design.
- `_time_for(self, name: str) -> tuple[int, int]` — FOMC → 18:00 UTC, everything else → 13:30 UTC.

### `macro/sources/stub_calendar.py`
Module docstring: placeholder + 5-step "to implement a real source against this slot" recipe (Trading Economics / Investing.com / Forex Factory / econoday / MarketAux).
- `class StubCalendarSource(BaseCalendarSource)`:
  - Class attrs: `source_id = "stub_calendar"`, `display_name = "Stub Calendar (placeholder)"`, `refresh_interval = 86400`, `optional = True`.
  - `is_available(self) -> bool` — always `False`.
  - `async fetch_events(self) -> list[CalendarEvent]` — returns `[]`.

## Plugin registrations
`REGISTERED_CALENDAR_SOURCES` in `macro/sources/__init__.py` (ordered):

| # | Class | `source_id` | API key env var | `is_available()` | Notes |
|---|---|---|---|---|---|
| 1 | `FREDCalendarSource` | `fred_calendar` | `FRED_API_KEY` | default — `True` iff env var set | Real source. Hourly refresh, 14-day lookahead, US-only, drops LOW impact. |
| 2 | `StubCalendarSource` | `stub_calendar` | — (no `api_key_env_var`) | overridden — always `False` | Placeholder; documented plugin slot. |

Both subclasses set `optional = True`. No source sets `requires_api_key = True` other than FRED.

## Imports graph

**Imports from project (within `macro/`):**
- `macro/__init__.py` → `macro.monitor`, `macro.regime`, `macro.signals`, `macro.sources.base`.
- `macro/monitor.py` → `config.settings`, `database.queries` (as `db_queries`), `macro.regime`, `macro.signals`, `macro.sources` (for `REGISTERED_CALENDAR_SOURCES`). **Deferred import** inside `refresh()`: `from data_sources import data_sources` (avoids `macro→data_sources→…` cycles at module load).
- `macro/regime.py` → stdlib only (`enum`, `time`, `dataclasses`, `typing`).
- `macro/signals.py` → stdlib only.
- `macro/sources/__init__.py` → `macro.sources.fred_calendar`, `macro.sources.stub_calendar`.
- `macro/sources/base.py` → stdlib only.
- `macro/sources/fred_calendar.py` → `config.settings`, `macro.signals`, `macro.sources.base`, third-party `aiohttp`.
- `macro/sources/stub_calendar.py` → `macro.signals`, `macro.sources.base`.

**Imported by (grep `from macro` / `import macro`):**
- `core/bot.py:287` and `core/bot.py:514` — `from macro import macro_monitor` (lazy; spawns `run_refresh_loop()` and consults `is_hard_blocked()` / `get_pending_events()` in the bot loop).
- `signals/quality_gate.py:142` — `from macro import macro_monitor` inside `evaluate()`; the QualityGate consumer (see Purpose).
- `ui/dashboard.py:604`, `ui/dashboard.py:873` — `from macro import macro_monitor` for the macro panel and pending-events panel.
- `prompts/build_macro.md:398` — documentation reference (`python -c "from macro import macro_monitor; ..."`).
- `tests/test_macro.py` — exercises every public symbol; see Tests.
- `tests/test_dashboard.py:363` — imports `macro.regime` enums for the dashboard fixture.

No circular-import cycles observed at import time; the `data_sources` dependency is lazily imported inside `refresh()`, which the module docstring explicitly calls out.

## Tests
Source: `tests/test_macro.py`. All tests import from `macro/`. (Also: `tests/test_dashboard.py` imports `macro.regime` for dashboard fixtures but isn't a macro-focused test.)

Score curves:
- `test_vix_score_curve` — pins the four `_vix_score` bands (100 / 30 / -60 / -100) plus the `None`→0 sentinel.
- `test_dollar_score_curve` — pins `_dollar_score` at WEAK / NEUTRAL / STRONG plus `None`→0.
- `test_yield_curve_score` — pins inverted (-100), flat (0), positive (60) bands plus `None`→0.
- `test_inflation_score` — pins low (100), mid (0), high (-100) bands plus `None`→0.
- `test_score_to_modifier_step_ladder` — pins the seven-rung modifier ladder; docstring notes "mirrors sentiment._composite_to_modifier".

Dimensional classifiers + scenario priority (`@pytest.mark.asyncio`):
- `test_crisis_fires_on_high_vix` — VIX=40 → `MacroScenario.CRISIS` + `is_hard_blocked() == True`.
- `test_goldilocks_fires_on_supportive_inputs` — weak DXY + calm VIX + low CPI → `GOLDILOCKS`, `RISK_ON`, modifier `+15`.
- `test_risk_off_fires_on_inverted_curve_plus_elevated_vix` — elevated VIX + inverted 10y-2y → `RISK_OFF`; verifies the `yield_curve` arithmetic at `-1.0`.
- `test_confidence_degrades_with_missing_data` — 3-of-6 inputs `None` → confidence in `(0.4, 0.6)`.
- `test_missing_data_source_does_not_crash` — even with `data_sources` import poisoned, `refresh()` still emits a regime with `confidence == 0.0`.

Calendar plugin:
- `test_stub_calendar_is_unavailable` — `StubCalendarSource().is_available() is False`.
- `test_stub_calendar_returns_empty` — `await fetch_events() == []`.
- `test_fred_calendar_requires_api_key` — `FREDCalendarSource().is_available()` flips with `FRED_API_KEY`.
- `test_fred_calendar_impact_keyword_mapping` — pins HIGH/MEDIUM/LOW keyword routing.
- `test_fred_calendar_parses_fomc_time_correctly` — FOMC → 18:00 UTC, HIGH impact.
- `test_fred_calendar_drops_low_impact_releases` — Commercial Paper returns `None` (LOW filter).
- `test_calendar_event_minutes_until` — positive when future.
- `test_new_calendar_source_picked_up_via_registry` — drop-in plugin gets included in `_calendar_sources`.

Pre-event pause:
- `test_get_pending_events_filters_past_and_orders_by_time` — past dropped, future sorted ascending.

DB round-trips (uses a `temp_db` fixture that swaps `settings.DB_PATH` and reloads `database.db` + `database.queries`):
- `test_save_macro_regime_round_trip` — `save_macro_regime` → `get_macro_history(hours=1)` returns the row with strings for the enums.
- `test_save_calendar_events_upserts_on_event_id` — same `event_id` overwrites the row (forecast populated post-release).
- `test_get_pending_events_filters_window` — `hours_ahead` window respected.

Dashboard panels:
- `test_macro_panel_renders_without_regime` — `_panel_macro()` renders to `/dev/null` console without raising when `get_current_regime()` returns `None`.
- `test_macro_panel_renders_with_regime` — same panel with a `REFLATION` sample regime.
- `test_pending_events_panel_renders_with_high_impact_soon` — `_panel_pending_events()` renders with FOMC+CPI pending.

## TODOs / FIXMEs / stubs
A `grep "TODO|FIXME|XXX|HACK"` of `macro/` returns no matches. There are no inline TODO/FIXME/XXX/HACK markers in the package.

Implicit stubs (documented but not real):
- `macro/sources/stub_calendar.py:40` — `class StubCalendarSource` exists only as a documented placeholder; `is_available()` always returns `False`.
- `macro/monitor.py:463 _derive_signals` (docstring) — "Discrete events for the future MacroAgent. Computed but not consumed — the agent picks these up when it lands." → `MacroSignal`s are produced on every refresh but no consumer exists yet (the `agents/` package doesn't import them).
- `macro/signals.py:91 MacroSignal` (class docstring) — "These are generated by MacroMonitor (…), not consumed yet — the agent is a placeholder."

## Known issues observed
- **Calendar-event timezones are silently coerced.** `CalendarEvent.minutes_until` accepts both tz-aware and naive `scheduled_utc`. `FREDCalendarSource._parse_entry` builds naive datetimes via `datetime.strptime(...).replace(hour=…)`, and `MacroMonitor.get_pending_events` compares with `datetime.utcnow()` (naive). Mixing tz-aware events from a future source with the naive `now` from the monitor will work by coincidence (the `astimezone(...).replace(tzinfo=None)` branch in `minutes_until` strips it) but the convention is implicit, not enforced. Documented at `macro/signals.py:55-58` ("source guarantees scheduled_utc is in UTC by convention").
- **`country` field of FRED events is hard-coded `"US"`.** Acknowledged in the source docstring (`fred_calendar.py:14`), but until a non-US source lands, the dashboard's country column will always be `US`.
- **FRED scheduled times are best-effort.** `_DEFAULT_HOUR_UTC = 13`, `_DEFAULT_MINUTE_UTC = 30`, `_FOMC_HOUR_UTC = 18` — magic numbers, documented in the module header but not configurable via `settings`. The `MACRO_PRE_EVENT_PAUSE_MINUTES` window therefore operates on times that may be off by hours for non-08:30-ET releases.
- **`_yield_curve_score` `0.5` boundary is a magic number.** `_yield_curve_score` returns `60` when `spread > 0.5` (line 70). The 0.5 threshold is not in `settings.MACRO_*` (which only exposes `MACRO_YIELD_CURVE_INVERSION`). Per `CLAUDE.md` ("Don't introduce magic numbers in other modules; add a constant in settings.py"), this is a small but real settings-discipline gap.
- **`_derive_signals` `strength=min(1.0, abs(yield_curve)/1.0)`** — dividing by `1.0` is a no-op; presumably a placeholder for a future settings-driven normaliser.
- **`MacroSignal` is dead-on-arrival**. `_derive_signals` runs every refresh, the result is cached in `self._signals` and surfaced via `get_macro_signals()`, but no code anywhere imports / consumes `get_macro_signals()` (no grep hits outside `macro/` and `tests/test_macro.py`). It's documented as "for the future MacroAgent" but right now is dead code in production.
- **`_confidence` staleness probe assumes the `fred` data source.** The check hardcodes `db_queries.get_latest_data_point("fred", "vix")` (line 453). If FRED is unreachable but other sources are fresh, confidence stays full. This is reasonable given VIX's weight in the composite, but is undocumented.
- **`refresh()`'s deferred import error path returns `None` from `getattr(ds, source_id, None)`.** If `data_sources` is importable but lacks one of the expected attribute names (`frankfurter`, `fred`), `_safe` silently returns `None` — feeds neutralised inputs into the regime without a warning. The fallback is debug-logged inside `_safe`, but a misnamed attribute will degrade the regime without an obvious operator signal.
- **`MacroMonitor.run_refresh_loop` swallows `refresh()` failures with `exc_info=True` but never backs off.** A persistently failing data source will log a traceback every `MACRO_REFRESH_INTERVAL_SEC` (default 300s) with no rate-limiting beyond that interval.
- **No tests exercise `_classify_rates` with non-None history.** `test_*` always patches `get_data_at_time` to return `None`, so `RateEnvironment.TIGHTENING`/`EASING` branches against `MACRO_RATE_CHANGE_TIGHTENING / EASING` thresholds are uncovered. Coverage for `TIGHTENING_CYCLE`, `EASING_CYCLE`, and `STAGFLATION` scenarios is therefore indirect.
- **`run_refresh_loop` uses `time.time()` directly** rather than an injectable clock, complicating deterministic timing tests. Minor.

---

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

---

# Module Report: `profiles/`

## Purpose

The `profiles/` package provides a **runtime override layer** over `config/settings.py`. A `Profile` is a frozen-style dataclass holding a small subset of risk/sizing/strategy/sentiment knobs. `ProfileManager` loads every `*.json` file under the package directory into `Profile` instances at import time and exposes a module-level singleton (`profile_manager`) so any module can read the currently-active profile via `profile_manager.current`. Profiles are selected at startup via `main.py --profile <name>` (default `balanced`) and consumed inside `core.bot` (and downstream sizing / gate logic) to override settings.py defaults without code edits. The custom profile is intended to be edited by the user; `save_custom()` allows the manager to persist a new `Profile` back to `custom.json`.

## Files

| File | Lines | Purpose |
|---|---|---|
| `profiles/__init__.py` | 0 | Empty package marker. No re-exports. |
| `profiles/profile_manager.py` | 90 | `Profile` dataclass, `ProfileManager` class, singleton `profile_manager`. |
| `profiles/aggressive.json` | 17 | Higher-risk preset (5% sizing, momentum/scalper focus, threshold 55). |
| `profiles/balanced.json` | 17 | Default preset (3% sizing, threshold 65, multi-TF on). |
| `profiles/conservative.json` | 17 | Low-risk preset (1% sizing, arb-priority, threshold 72). |
| `profiles/custom.json` | 17 | User-editable preset; ships as a clone of balanced. |

## Public Surface — `profile_manager.py`

### `Profile` dataclass (lines 17–45)

All fields are required (no defaults — the dataclass relies on JSON files providing every key). Order matches the source:

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | `str` | (none) | Profile key; used by `ProfileManager._profiles` dict. |
| `description` | `str` | (none) | Human-readable description. |
| `max_position_size_pct` | `float` | (none) | Max % of portfolio per trade. |
| `max_open_positions` | `int` | (none) | Cap on concurrent positions. |
| `signal_score_threshold` | `float` | (none) | Gate threshold replacement. |
| `require_multi_tf` | `bool` | (none) | Force multi-timeframe confirmation. |
| `default_stop_loss_pct` | `float` | (none) | Fallback SL when agent gives none. |
| `default_take_profit_pct` | `float` | (none) | Fallback TP when agent gives none. |
| `daily_loss_limit_pct` | `float` | (none) | Daily DD circuit-breaker. |
| `consecutive_loss_limit` | `int` | (none) | Streak halt threshold. |
| `preferred_strategy` | `str` | (none) | Strategy registry key (e.g. `default`, `arb_only`, `scalper`). |
| `arb_priority` | `bool` | (none) | Boost ranking for arb signals. |
| `sentiment_boost_amount` | `float` | (none) | Additive bullish-sentiment score modifier. |
| `sentiment_suppress_amount` | `float` | (none) | Subtractive bearish-sentiment score modifier. |
| `claude_temperature` | `float` | (none) | Per-profile override of `settings.CLAUDE_TEMPERATURE`. |

> Note: no field has a Python-side default; every `Profile(**data)` construction requires the JSON to contain all 15 keys, otherwise `_load_all` swallows the `TypeError` as a warning.

### `ProfileManager` class (lines 48–86)

| Member | Signature | Purpose |
|---|---|---|
| `__init__` | `() -> None` | Initialises empty `_current` / `_profiles` and calls `_load_all()`. |
| `_load_all` | `() -> None` | Globs `PROFILES_DIR/*.json`, parses each into a `Profile`, keys by `profile.name`. Failures logged as warnings, not raised. |
| `load` | `(name: str) -> Profile` | Sets `_current` to the named profile. Raises `ValueError` listing available profiles if name missing. |
| `current` | `@property -> Profile` | Returns `_current`; if unset, lazily calls `self.load("balanced")` (silent fallback). |
| `list_profiles` | `() -> list[str]` | Lists loaded profile names. |
| `save_custom` | `(profile: Profile) -> None` | Writes `profile.__dict__` to `custom.json` (indent=2) and re-registers under key `"custom"`. Overwrites the on-disk template. |

### Module-level singleton

- `PROFILES_DIR = Path(__file__).parent` (line 14).
- `profile_manager = ProfileManager()` instantiated at import (line 90). Side effect: synchronous JSON I/O on import.

## Profile Comparison

| Field | aggressive | balanced | conservative | custom |
|---|---|---|---|---|
| `name` | `aggressive` | `balanced` | `conservative` | `custom` |
| `description` | "Higher risk. 5% per trade. Lower signal threshold. Momentum focus." | "Default. 3% per trade. All signal types. 65 gate threshold." | "Low risk. Small positions. Arb priority. High signal threshold." | "Edit this file to define your own risk parameters." |
| `max_position_size_pct` | 0.05 | 0.03 | 0.01 | 0.03 |
| `max_open_positions` | 4 | 3 | 2 | 3 |
| `signal_score_threshold` | 55 | 65 | 72 | 65 |
| `require_multi_tf` | false | true | true | true |
| `default_stop_loss_pct` | 0.015 | 0.01 | 0.008 | 0.01 |
| `default_take_profit_pct` | 0.03 | 0.02 | 0.016 | 0.02 |
| `daily_loss_limit_pct` | 0.035 | 0.02 | 0.01 | 0.02 |
| `consecutive_loss_limit` | 4 | 3 | 2 | 3 |
| `preferred_strategy` | `scalper` | `default` | `arb_only` | `default` |
| `arb_priority` | false | false | true | false |
| `sentiment_boost_amount` | 20 | 15 | 10 | 15 |
| `sentiment_suppress_amount` | 15 | 20 | 25 | 20 |
| `claude_temperature` | 0.3 | 0.2 | 0.1 | 0.2 |

Observations:
- `custom.json` is a byte-for-byte clone of `balanced.json` except for `name` and `description` — it ships as a template.
- Risk-vs-reward scaling is internally consistent (TP = 2× SL for balanced/conservative, exactly 2× for aggressive too).
- `sentiment_boost_amount` is strictly decreasing from aggressive to conservative (20→15→10); `sentiment_suppress_amount` strictly increasing (15→20→25). Inverse correlation is intentional.
- `signal_score_threshold` in aggressive (55) is **below** the `settings.MIN_SIGNAL_SCORE` baseline of 65; profile overrides loosen the gate.

## Setting Coverage

Every JSON profile defines all 15 `Profile` dataclass fields. No missing keys, no extra keys.

| Dataclass field | aggressive | balanced | conservative | custom |
|---|---|---|---|---|
| `name` | yes | yes | yes | yes |
| `description` | yes | yes | yes | yes |
| `max_position_size_pct` | yes | yes | yes | yes |
| `max_open_positions` | yes | yes | yes | yes |
| `signal_score_threshold` | yes | yes | yes | yes |
| `require_multi_tf` | yes | yes | yes | yes |
| `default_stop_loss_pct` | yes | yes | yes | yes |
| `default_take_profit_pct` | yes | yes | yes | yes |
| `daily_loss_limit_pct` | yes | yes | yes | yes |
| `consecutive_loss_limit` | yes | yes | yes | yes |
| `preferred_strategy` | yes | yes | yes | yes |
| `arb_priority` | yes | yes | yes | yes |
| `sentiment_boost_amount` | yes | yes | yes | yes |
| `sentiment_suppress_amount` | yes | yes | yes | yes |
| `claude_temperature` | yes | yes | yes | yes |

- Fields in dataclass but absent from any JSON: **none**.
- JSON keys not present in dataclass: **none**.
- Schema is currently in sync across all four JSON files. Adding a new field to `Profile` without updating all four JSON files would cause `Profile(**data)` to raise `TypeError`, which `_load_all` swallows as a warning — meaning the profile silently disappears from `_profiles`, and a subsequent `load("balanced")` would also fail (see Known Issues).

## Imports Graph (`from profiles ...`)

Codebase-wide consumers (excluding the audit reports themselves):

| File | Line | Import |
|---|---|---|
| `main.py` | 35 | `from profiles.profile_manager import profile_manager` |
| `core/bot.py` | 150 | `from profiles.profile_manager import profile_manager` (lazy, inside method, to avoid early-boot / circular import) |

No other module imports from `profiles`. The package is consumed via the singleton only; nothing imports the `Profile` dataclass or `ProfileManager` class directly.

## Tests That Import `profiles`

No tests currently import `profiles` or `Profile`. Several tests use a `SimpleNamespace(name="balanced")` mock in place of a real `Profile`:

- `tests/test_dashboard.py:73` — `_profile=SimpleNamespace(name="balanced")`.
- `tests/test_macro.py:389` — `_profile=SimpleNamespace(name="balanced")`.
- `tests/test_bot.py:28` — `_stub_profile()` helper, used at `tests/test_bot.py:69`.

Implication: there is **zero direct test coverage** of `ProfileManager`, JSON loading, custom-profile save round-trip, or the fallback property — all are exercised only through integration.

## TODOs / FIXMEs / Stubs

- TODO/FIXME/XXX/HACK count in `profiles/`: **0**.
- `profiles/__init__.py` is a 0-byte empty package marker (no re-exports of `Profile` or `profile_manager`). Consumers must import from the submodule path `profiles.profile_manager`.

## Known Issues Observed

1. **Silent failure on JSON load errors.** `_load_all` (lines 54–63) wraps each load in a broad `except Exception as e: logger.warning(...)`. A malformed JSON file, a missing key, or a type mismatch all degrade to a debug-level warning. The profile simply does not appear in `_profiles`. If `balanced.json` is the one that fails, the `current` property's lazy fallback (`return self.load("balanced")`, line 75) will raise `ValueError` on first read of `profile_manager.current` — a runtime crash deferred from import time to first use. There is no integrity check that the canonical `balanced` profile loaded.

2. **`current` lazy-loads `balanced` by name, not by config.** The fallback name is hard-coded (`"balanced"` at line 75). There is no `settings.DEFAULT_PROFILE` constant — the default profile name is duplicated in `main.py` arg parsing and here. Renaming the default profile requires changes in two places.

3. **No schema versioning / validation.** Adding a new field to the `Profile` dataclass requires editing all 4 JSON files in lock-step or every load fails. There is no JSON Schema, Pydantic model, or migration mechanism. `aggressive.json`'s `signal_score_threshold: 55` and `claude_temperature: 0.3` are not range-validated against `settings.py`'s `# test:` ranges.

4. **`save_custom` overwrites the on-disk template silently.** `custom.json` doubles as both a user-editable template and the persistence target for programmatic saves. There is no backup, no atomic write (`json.dump` directly into `open(path, "w")`), and `profile.__dict__` is used instead of `dataclasses.asdict()` — fine for current flat fields but fragile if `Profile` ever gains nested dataclass fields.

5. **Singleton instantiated at import-time with disk I/O.** `profile_manager = ProfileManager()` at line 90 reads from disk during module import. This makes `from profiles.profile_manager import profile_manager` an I/O-performing import, which is why `core/bot.py:150` imports it lazily — but `main.py:35` imports it at module top-level, eagerly.

6. **`Profile` is not frozen.** The dataclass is mutable (`@dataclass` without `frozen=True`). Any code holding a reference to `profile_manager.current` can mutate fields in place, with no broadcast or change notification. There is no observer pattern around active-profile changes; downstream consumers must re-read `profile_manager.current` on each use.

7. **`require_multi_tf=false` on aggressive plus `signal_score_threshold=55` doubly loosens the gate** below the typical `settings.MIN_SIGNAL_SCORE=65`. Worth confirming downstream (in `signals/quality_gate.py`) that profile overrides actually take precedence over `settings.py` — not audited here.

---

# Module Report: `utils/`

## Purpose

Cross-cutting utility helpers used throughout the bot. Currently provides two
self-contained capabilities:

1. **Rolling Hurst exponent** (`utils/hurst.py`) — Rescaled-Range (R/S) analysis
   for regime classification (trending / reverting / random). Consumed by
   `core/regime_detector.py` to bias the score modifier applied inside
   `QualityGate`.
2. **Structured logging setup** (`utils/logger.py`) — root-logger configuration
   with console + rotating-file handlers and noisy-library silencing. Called
   exactly once from `main.py` at startup.

The package has no shared state, no re-exports, and the `__init__.py` is empty
(zero bytes), so `utils` is purely a namespace for individually-imported
sub-modules.

## Files

| Path | LOC | Summary |
|---|---:|---|
| `utils/__init__.py` | 0 | Empty package marker. No exports, no docstring. |
| `utils/hurst.py` | 130 | R/S Hurst exponent: `hurst_rs()` function + `RollingHurst` stateful wrapper + private `_compute_rs()` helper. |
| `utils/logger.py` | 44 | `setup_logging(debug)` — configures root logger with console + `TimedRotatingFileHandler` and silences ccxt/asyncio/urllib3/telethon/praw. |
| **Total** | **174** | |

## Public Surface

### `utils/__init__.py`

Empty file (0 bytes). No docstring, no `__all__`, no re-exports. Acts purely as
a package marker so `utils.hurst` and `utils.logger` are importable.

### `utils/hurst.py`

**Module docstring** (lines 1–12):

> Rolling Hurst exponent via Rescaled Range (R/S) analysis.
>
> H > 0.55 → trending (persistent, momentum favoured)
> H < 0.48 → reverting (anti-persistent, mean reversion favoured)
> H ≈ 0.50 → random walk (no edge, avoid directional trades)
>
> Academic basis: Lo (1991) R/S analysis. Widely validated on crypto time
> series showing regime-dependent Hurst behaviour.

Note: the 0.55 / 0.48 numbers in the docstring are illustrative — actual
thresholds come from `config/settings.py` (`HURST_TRENDING_MIN`,
`HURST_REVERTING_MAX`) read lazily inside `RollingHurst.classify`.

**Module constants**: none.

**Module-level logger**: `logger = logging.getLogger(__name__)` (line 18).

#### Functions

- `hurst_rs(series: np.ndarray) -> Optional[float]` (lines 21–71)
  Compute Hurst exponent via R/S analysis on a price series. Minimum 50 values
  required. Internally converts to log returns (`np.diff(np.log(series + 1e-12))`),
  builds a list of sub-period lags starting at `min_lag = 10` and growing by
  `int(lag * 1.5) + 1` up to `n // 2`. Computes mean R/S per lag, then fits
  `log(R/S) ~ slope * log(n)` via `np.polyfit` of degree 1. Result clamped to
  `[0.0, 1.0]`. Returns `None` if `< 50` samples, `< 4` valid lag points, or on
  any exception (which is logged at DEBUG).

- `_compute_rs(returns: np.ndarray, lag: int) -> Optional[float]` (lines 74–93)
  Private helper. Chunks `returns` into `n // lag` non-overlapping segments;
  for each chunk computes range of mean-centred cumulative sum divided by
  sample std (`ddof=1`). Returns mean R/S across chunks, or `None` if no valid
  chunks. Skips chunks with `s == 0` and chunks shorter than 2.

#### Classes

- **`RollingHurst`** (lines 96–130)

  > Maintains a rolling Hurst exponent for a single price series. Call
  > `update()` on each new candle close.

  | Method | Signature | Notes |
  |---|---|---|
  | `__init__` | `(self, lookback: int = 200)` | Stores `self._prices: list[float] = []` and `self._current: Optional[float] = None`. |
  | `update` | `(self, price: float) -> Optional[float]` | Appends `price`, pops front when over `lookback`, recomputes Hurst once buffer holds ≥ 50 prices. Returns current value. |
  | `value` (property) | `-> Optional[float]` | Cached `_current` value without recomputation. |
  | `classify` | `(self) -> str` | Returns `"trending"` / `"reverting"` / `"random"` / `"unknown"`. **Lazy import** of `HURST_TRENDING_MIN` and `HURST_REVERTING_MAX` from `config.settings` inside the method body (line 125). |

### `utils/logger.py`

**Module docstring** (lines 1–4):

> Structured logging setup. Logs to console and rotating file.

**Module constants**: none. Pulls `LOGS_DIR`, `LOG_LEVEL`, `LOG_TO_FILE` from
`config.settings` at import time.

#### Functions

- `setup_logging(debug: bool = False) -> None` (lines 12–44)
  Idempotency note: there is **no guard** against repeated calls — each
  invocation appends new handlers to the root logger. Behaviour:
  1. `LOGS_DIR.mkdir(parents=True, exist_ok=True)`
  2. Level = `DEBUG` if `debug` else `getattr(logging, LOG_LEVEL)`. No
     validation of `LOG_LEVEL`; an unknown string raises `AttributeError`.
  3. Formatter: `"%(asctime)s  %(levelname)-8s  %(name)-25s  %(message)s"` with
     `datefmt="%H:%M:%S"` (date is dropped from console output — file rotation
     by day still records date in filename).
  4. Console handler (`StreamHandler`) set to `level`.
  5. If `LOG_TO_FILE`: `TimedRotatingFileHandler` writing
     `LOGS_DIR / "cryptobot.log"`, rotating at midnight, keeping 30 backups,
     UTF-8 encoded, fixed at `DEBUG` regardless of console level. Comment on
     line 24 says "INFO+ only" but the code actually applies the parameterised
     `level`.
  6. Silences `ccxt`, `asyncio`, `urllib3`, `telethon`, `praw` to `WARNING`.

## Imports Graph

### Imports from project

- `utils/hurst.py`:
  - `from config.settings import HURST_TRENDING_MIN, HURST_REVERTING_MAX`
    (lazy, inside `RollingHurst.classify`, line 125).
- `utils/logger.py`:
  - `from config.settings import LOGS_DIR, LOG_LEVEL, LOG_TO_FILE` (line 9,
    top-level — settings must import cleanly before logging is set up).
- `utils/__init__.py`: no imports.

### Third-party / stdlib imports

- `hurst.py`: `numpy`, `logging`, `typing.Optional`.
- `logger.py`: `logging`, `logging.handlers`, `pathlib.Path` (imported but not
  used directly — `LOGS_DIR` is already a `Path` from settings).

### Imported by (project-wide grep of `from utils` / `import utils`)

- `core/regime_detector.py:24` — `from utils.hurst import RollingHurst`
- `main.py:38` — `from utils.logger import setup_logging`

No other project module imports anything from `utils`. `hurst_rs` (the standalone
function) and `_compute_rs` have no external callers; only `RollingHurst` is
consumed downstream.

## Tests

- No tests reference `utils.*`. A repository-wide grep for `utils` under
  `tests/` returns only one unrelated hit:
  `tests/test_web_server.py:22` — `from aiohttp.test_utils import TestClient, TestServer`
  (third-party aiohttp helper, not this `utils` package).
- Per CLAUDE.md the `tests/` directory currently contains only `__init__.py`
  fixtures; the audited `utils` module has zero direct test coverage.

## TODOs / FIXMEs / Stubs

A case-insensitive grep for `TODO|FIXME|XXX|HACK|stub` across `utils/` returned
no matches. The package has no explicit deferred-work markers.

Implicit stub: `utils/__init__.py` is a 0-byte file — not strictly a TODO, but
worth flagging if the project expects re-exports here.

## Known Issues Observed

1. **`setup_logging` is not idempotent.** Each call unconditionally appends a
   new `StreamHandler` (and optionally a `TimedRotatingFileHandler`) to the
   root logger. If anything ever re-invokes it (e.g. a test fixture, a reload
   path, the web UI restart), every log line will be duplicated per call. A
   simple `if root.handlers: return` guard or removal-before-add would fix it.

2. **Misleading inline comment in `logger.py`.** Line 24 says
   `# Console handler (clean, INFO+ only)` but the handler is set to the
   parameterised `level`, so passing `debug=True` sends DEBUG to the console
   despite the comment.

3. **Unused import in `logger.py`.** `from pathlib import Path` is imported on
   line 8 but never referenced — `LOGS_DIR` is already a `Path` provided by
   `config.settings`.

4. **`LOG_LEVEL` is `getattr`'d without validation.** A typo like
   `LOG_LEVEL = "INF0"` in settings raises `AttributeError` at startup instead
   of falling back to a sane default.

5. **`hurst_rs` swallows all exceptions to DEBUG.** The broad
   `except Exception as e` (line 69) hides genuine bugs (e.g. negative prices
   feeding `np.log`) behind a DEBUG-only message — only the `1e-12` epsilon
   shields against zero/negative input. Callers see `None` and continue
   silently.

6. **Hurst regime thresholds are split between docstring and settings.** The
   docstring quotes `0.55 / 0.48` but the live thresholds live in
   `config.settings.HURST_TRENDING_MIN` / `HURST_REVERTING_MAX`. If those
   settings drift, the docstring becomes lies.

7. **`RollingHurst._prices` uses `list.pop(0)`** (line 110) which is O(n) per
   call. With the default `lookback=200` this is irrelevant, but a
   `collections.deque(maxlen=lookback)` would be both simpler and O(1).

8. **`hurst_rs` recomputes from scratch on every `update()`** at every call
   once the buffer is full — there is no down-sampling, so for hot loops this
   is the dominant cost. Acceptable per-candle but not per-tick.

9. **No test coverage** at all for either module; Hurst math in particular is
   the kind of code that benefits from a synthetic-series sanity test
   (Brownian → ~0.5, persistent → > 0.5).

---

# Module Report: `predictive/`

## Purpose (intended, per CLAUDE.md and README)

Per `CLAUDE.md` (lines 33–34) and `README.md` (lines 86, 147–149), the `predictive/` package is intended to host:

1. A **trainer** invocable as `python -m predictive.trainer` — described in CLAUDE.md as: *"Train the predictive model (XGBoost) on accumulated sim data"*.
2. (Implied) **prediction-time inference** that writes rows into the `predictions` table defined in `database/models.py:375` (`class Prediction`), populating `model_version`, `win_probability`, `expected_pnl_pct`, `confidence`, `features_json`, and (post-trade) `correct`.

The intended consumer of these predictions is the trading loop: `Signal` has a 1:1 relationship to `Prediction` (`database/models.py:118`), and `database/queries.py:1414` carries a section comment `# ── Analytics helpers (used by predictive engine) ──────────` — i.e. the queries layer was pre-shaped to feed feature data into a future trainer (e.g. `get_signal_win_rate`).

### ACTUAL state

**The package is an empty stub.** Only `__init__.py` exists, and it is a 0-byte file. There is no `trainer.py`, no inference module, no feature builder, no model artifact. `python -m predictive.trainer` (as documented in CLAUDE.md) will fail with `No module named predictive.trainer`. This module is **referenced-but-unbuilt** — listed as a phase-2 feature in code comments but never implemented.

## Files

| File | LOC | SLOC | Role |
|---|---|---|---|
| `predictive/__init__.py` | 0 | 0 | Empty package marker. No exports. |

Source: `audit/cryptobot_1.0/file_inventory.csv:86` (`predictive/__init__.py,0,0,e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855,python,source`). The SHA-256 `e3b0c442…7852b855` is the canonical hash of the empty string, confirming zero bytes.

No other files. The package directory contains only `__init__.py` (verified by `ls -la predictive/`).

## Public surface

`predictive/__init__.py` is empty — **zero exports, zero classes, zero functions, zero constants**. The package can be imported (`import predictive` succeeds and yields an empty module object) but exposes nothing usable.

## Cross-references — where `predictive` is mentioned elsewhere

A repo-wide grep for the literal string `predictive` (case-sensitive) returns the following hits. The list distinguishes **actual code references** (which would fail at runtime) from **documentation/comment mentions** (descriptive only).

### Documentation references (descriptive, not load-bearing)

| File | Line | Context |
|---|---|---|
| `CLAUDE.md` | 33–34 | Comment + command: `# Train the predictive model (XGBoost) on accumulated sim data` / `python -m predictive.trainer`. **This command does not work** — module is empty. |
| `CLAUDE.md` | 74 | Repo-state caveat: *"…`predictive/`, some `signals/*.py`) are empty `__init__.py` stubs or referenced-but-missing imports."* — explicitly flags the stub. |
| `README.md` | 86 | Listed in directory tree under planned layout. |
| `README.md` | 147–149 | `python -m predictive.trainer` documented as the training entry point: *"This trains an XGBoost model on your historical data."* — not implemented. |
| `database/queries.py` | 1414 | Section header comment: `# ── Analytics helpers (used by predictive engine) ──────────` — i.e. helpers (`get_signal_win_rate`, etc.) were authored for a future predictive engine that does not exist yet. |

### Code references (would fail at runtime if invoked)

**None.** A grep for actual Python import statements (`from predictive`, `import predictive`, `predictive.`) returns **zero hits** anywhere in the source tree (only the README/CLAUDE.md doc mentions of `python -m predictive.trainer`). No module anywhere does `from predictive import …` or `import predictive`. The package is documented but not wired into any runtime code path.

### Tooling / inventory references (incidental)

| File | Line | Context |
|---|---|---|
| `audit/cryptobot_1.0/build_inventory.py` | 71 | `predictive` listed in the audit's package-iteration tuple. |
| `audit/cryptobot_1.0/list_pkgs.sh` | 4 | Audit shell script iterates `predictive` for packaging stats. |
| `audit/cryptobot_1.0/pkg_sizes.sh` | 3 | Same — package-size accounting. |
| `audit/cryptobot_1.0/tree.txt` | 136–137 | Tree snapshot records `./predictive` and `./predictive/__init__.py`. |
| `audit/cryptobot_1.0/file_inventory.csv` | 86 | Inventory row for the empty `__init__.py`. |

### Semantic uses of the word "predictive" (unrelated)

The word "predictive" also appears as English prose in unrelated comments/docs (`scalping_v2/SCALPING_V2.md:155`, `scalping_v2/SCALPING_V2_RECALIBRATION.md:153,170,263`, `scalping_v2/demo_simulate_v1_vs_v2.py:8`, `sentiment/sources/reddit.py:38`, `prompts/build_scalping_agent.md:1072`). These are not references to the `predictive/` package — they discuss whether scalping OFI / sentiment / BTC alignment is "predictive of price moves" in a generic statistical sense. **Not relevant to this module.**

### `Prediction` ORM model — defined but unused

Grep for the class name `Prediction` returns:

| File | Line | Context |
|---|---|---|
| `database/models.py` | 118 | `Signal.prediction = relationship("Prediction", back_populates="signal", uselist=False)` — 1:1 backref. |
| `database/models.py` | 375 | `class Prediction(Base):` — schema definition (`predictions` table, FK to `signals.id`, columns `model_version`, `win_probability`, `expected_pnl_pct`, `confidence`, `features_json`, `correct`). |
| `audit/cryptobot_1.0/module_reports/database.md` | 86, 244, 627, 635, 812 | Audit's database report — line 812 already flagged: *"`Predictions` table has no query helpers… The model is dead code at the moment — the `predictive/trainer` workflow described in CLAUDE.md would need to be wired up via this layer, but currently any predictive code must open a session directly (violating the documented invariant)."* |
| `audit/cryptobot_1.0/module_reports/signals.md` | 180 | Notes `win_probability` is owned by the Prediction model via FK and is therefore not in `Signal.to_db_dict()`. |

**No application code reads, writes, or otherwise touches the `Prediction` model.** The table is created by `init_db()` (via `Base.metadata.create_all`) on every startup but stays empty for the lifetime of the database. The schema is dormant infrastructure waiting for the unbuilt trainer.

### Settings / dependency references

- `requirements.txt:35` — `xgboost==2.0.3` is pinned. The library is installed but **no module in the repo imports it** (a separate grep for `xgboost`/`XGBoost` returns only `requirements.txt`, `audit/cryptobot_1.0/pip_freeze.txt:98` (which records the installed `xgboost==3.2.0` — a version mismatch vs the pin, see "Known issues"), `README.md:149`, and `CLAUDE.md:33`).
- `config/settings.py` — grep returns **zero hits** for `predictive` or `xgboost`/`XGBoost`. There are no predictive-model knobs in the central settings file, contrary to the codebase invariant that *"`config/settings.py` is the single tuning instrument."* Any future trainer will need to add its hyperparameters (n_estimators, learning_rate, max_depth, train/test split, feature window, model artifact path, etc.) there with `# test:` comment sweeps.

## Tests that import `predictive`

**None.** A grep restricted to `tests/**` for the string `predictive` returns zero matches. Per CLAUDE.md, `tests/` currently only contains `__init__.py`, so this is expected — there are no tests for this module (nor for any other).

## TODOs / FIXMEs / stubs

- The package itself is the stub. `predictive/__init__.py` contains zero bytes — no docstring, no `__all__`, no placeholder TODO comments, no `NotImplementedError` shim.
- Adjacent stub markers elsewhere in the codebase:
  - `database/models.py:376` — docstring on `Prediction`: *"Predictive model outputs per signal (phase 2 feature)."* — explicit "phase 2" deferral.
  - `database/queries.py:1414` — section comment promises analytics helpers *"used by predictive engine"*, but the consuming engine does not exist.
  - `CLAUDE.md:74` — repo-state caveat explicitly lists `predictive/` among the empty stubs.

No `TODO` / `FIXME` / `XXX` / `HACK` markers found inside the `predictive/` directory (because the directory contains no code).

## Known issues observed

1. **Package is entirely unimplemented.** The CLAUDE.md and README.md both document `python -m predictive.trainer` as if it works — it does not. Running it raises `ModuleNotFoundError: No module named 'predictive.trainer'`. Any user following the README's training instructions hits a broken command immediately.
2. **Dead schema.** `database/models.py:375 class Prediction` and the `predictions` table are created on every `init_db()` but are never written to or read from. No `queries.py` helper exists for upsert/read of predictions (cross-confirmed by `audit/cryptobot_1.0/module_reports/database.md:812`). The 1:1 `Signal.prediction` relationship will always resolve to `None` in the current build.
3. **Unused dependency (with a version drift).** `xgboost==2.0.3` is pinned in `requirements.txt:35`, but no module imports it. Additionally, `audit/cryptobot_1.0/pip_freeze.txt:98` records the **installed** version as `xgboost==3.2.0` — a major-version drift from the pin. This is dead weight in the environment today (no import → no behavior to break), but if the trainer is built against the pinned 2.0.3 API while the installed runtime is 3.2.0, expect breakage at first use.
4. **Documented invariant gap.** Per CLAUDE.md: *"`config/settings.py` is the single tuning instrument."* Any predictive-model hyperparameters belong there with `# test:` sweep comments — none currently exist, so the scaffold for tuning is missing too.
5. **Documented invariant gap (DB access).** Per CLAUDE.md: *"All DB access goes through `database/queries.py` — do not open sessions directly in feature code."* When the trainer is built, it will need queries-layer helpers to write to the `predictions` table (currently absent) so it does not bypass the queries module. The analytics helpers section at `queries.py:1414` is the intended insertion point.
6. **Naming collision risk.** The word "predictive" is used heavily in `scalping_v2/` documentation in a generic English sense ("was the OFI signal actually predictive?"). Future searches for the package by name will surface false positives; consider grepping for `from predictive` / `import predictive` instead of the bare word.

## Summary

`predictive/` is a placeholder for a phase-2 XGBoost-based win-probability/expected-PnL inference layer that has not been written. Only an empty `__init__.py` exists. The database schema (`Prediction` model, `predictions` table), a `requirements.txt` pin for `xgboost`, and section comments in `database/queries.py` show the system was pre-shaped for this module, but **no application code imports it and no tests cover it**. The README's `python -m predictive.trainer` command is documentation aspiration, not a working entry point. Flag this as a known-unbuilt module when planning future work.

---

# Module Report: scalping_v2

## Purpose

`scalping_v2/` is a **delivery bundle** — a self-contained drop that pairs the scalping-v2 selectivity layer (confluence + ATR-aware SL + integration glue) with its documentation (design overview, rollback runbook, recalibration cookbook, v3 roadmap), unit tests, and a runnable v1-vs-v2 simulation demo. It was assembled as the artefact handed to the operator to wire v2 into the live agent. The three Python modules in this bundle were subsequently copied verbatim into `agents/` for integration; the bundle survives in-tree as the canonical reference / handoff package and as the home of the standalone demo + recalibration / roadmap docs.

## Files

| File | LOC | One-sentence summary | Equivalent location in main codebase (if any) |
|------|-----|----------------------|------------------------------------------------|
| `RUNBOOK_v2_rollback_section.md` | 126 | Operator playbook for rolling v2 back (soft / hard / surgical) and pre-flight checklist before re-enabling. | None — intended to be appended to `RUNBOOK.md`. |
| `SCALPING_V2.md` | 230 | Integration guide describing what v2 adds (6/7 gates + ATR SL), file inventory, step-by-step wiring, and illustrative simulated metrics. | None — pure design doc. |
| `SCALPING_V2_RECALIBRATION.md` | 476 | Nine SQL "cookbook" queries (strength validation, gate effectiveness, blocked-trade win-rate, skip-reason breakdown, per-symbol / per-hour / ATR / activation / drawdown) plus a decision tree and tuning schedule for v2 in observation mode. | None — operator handbook. |
| `SCALPING_V3_ROADMAP.md` | 211 | Catalogue of deferred items (V3-1 through V3-10), each with what / why-deferred / trigger / scope, plus a build-order recommendation and an explicit "not on the roadmap" list. | None — strategic doc. |
| `settings_scalp_v2.py` | 112 | New `SCALP_*` constants for the v2 layer (z-threshold, session window, confluence, cross-exchange, BTC directional, adverse selection, depth, ATR SL, v2 activation criteria) plus a `V2Settings` snapshot helper. | Intended to be appended to `config/settings.py`; lives standalone here for the demo. |
| `scalping_agent_v2_integration.py` | 247 | Replacement `evaluate_signal_v2()` flow, `_initial_observation` / `_run_legacy_gates` / `_compute_position_size_usd` helpers, a `DB_COLUMNS_V2` SQL string for new nullable columns, and `is_ready_for_live_v2()` activation checker. | **Identical** byte-for-byte: `agents/scalping_agent_v2_integration.py`. |
| `scalping_atr_sl.py` | 110 | `ATRStopCalculator` + `TpSlV2` dataclass: TP = round-trip fee + target; SL = max(base SL from RR, ATR×multiplier clamped to floor/ceiling). | **Identical** byte-for-byte: `agents/scalping_atr_sl.py`. |
| `scalping_confluence.py` | 411 | `ConfluenceChecker` with seven gate methods (VWAP, HTF EMA trend, volume, cross-exchange OFI, BTC directional, adverse selection, depth) plus combined runner returning `CombinedConfluenceResult` with strength label (WEAK/MODERATE/STRONG/VERY_STRONG). | **Identical** byte-for-byte: `agents/scalping_confluence.py`. |
| `demo_simulate_v1_vs_v2.py` | 477 | Synthetic 20 000-signal generator + v1 / v2 evaluators + mock market_data / OFI adapters + ASCII comparison report (win rate, expectancy, skip-reason buckets, per-strength stats, activation-readiness verdict). | None — demo only. |
| `test_scalping_v2.py` | 440 | 41 pytest unit tests across the v2 confluence gates, ATR SL calculator, combined runner, and activation-readiness checker, using mock `MockMarketData` / `MockOFIEngine`. | None in `agents/`; lives only inside the bundle, not under top-level `tests/`. |

### .md file paragraph summaries

- **`RUNBOOK_v2_rollback_section.md`** — defines the operational triggers that justify rollback (sub-baseline WR, negative `avg_net_bps`, < 1 trade/day, counterproductive gate), explicitly rules out rolling back on a single bad day, then walks through three rollback modes: soft (flip `SCALP_USE_*_GATE = False`, restore v1 z=1.5 / persist=3 / session 7-17), hard (`git revert` the integration commit, retain v2 source for later), and surgical (disable one gate). Closes with a re-enable pre-flight checklist (100+ v1-only trades, query analysis done, one gate at a time, observation mode first) and a kill-switch reminder.

- **`SCALPING_V2.md`** — companion to `scalping_agent_doc.docx`. Enumerates the nine v2 additions (tighter z, longer persistence, 2/3 confluence, cross-exchange agree/block, BTC directional gate, adverse-selection guard, depth gate, ATR-aware SL, tighter activation criteria), shows the bundle layout, gives step-by-step integration instructions (settings copy, file copy, agent wiring snippet, DB column add, `MarketData` method audit), and reports a simulation snapshot (WR 58.13% → 66.84%, expectancy 0.959 → 1.383 bps, 5-loss-streak 1.29% → 0.40%). Closes with tuning knobs in relax-priority order and a list of deferred-to-v3 items.

- **`SCALPING_V2_RECALIBRATION.md`** — explains why simulation conditional accuracies must be revalidated against live data. Provides sample-size guidance (50 / 150 / 300 / 500 / 1000 thresholds), then nine SQL recipes against `scalp_observations` covering: strength-label monotonicity, individual confluence-gate lift, hard-gate "would-have-won" analysis via micro tracker, skip-reason bucketing, per-symbol / per-hour stratification, ATR SL effectiveness, activation-readiness verdict, daily P&L / drawdown. Ends with a decision tree mapping query findings to parameter changes and an "important: change one parameter at a time" procedure.

- **`SCALPING_V3_ROADMAP.md`** — ten deferred items (V3-1 to V3-10) each with what / why-deferred / trigger / scope / dependencies, plus a build-order recommendation prioritising V3-4 (continuous edge monitor with auto-halt) as the only item that should ship *before* going live with v2 capital. Includes an explicit "not on the roadmap" list (stop-loss-free strategies, pyramiding into losers, aggressive leverage on scalps, last-30-days overfit retraining, copy-trading, discretionary overrides) and a maintenance reminder.

### .py file inventories

- **`settings_scalp_v2.py`** — constants only (`SCALP_OFI_Z_ENTRY=2.0`, `SCALP_OFI_PERSIST_TICKS=5`, `SCALP_SESSION_START_UTC=12`, `SCALP_SESSION_END_UTC=16`, `SCALP_USE_CONFLUENCE`, `SCALP_CONFLUENCE_REQUIRED=2`, `SCALP_USE_VWAP_GATE`, `SCALP_USE_HTF_TREND_GATE`, `SCALP_HTF_TIMEFRAME="5m"`, `SCALP_HTF_EMA_FAST=8`, `SCALP_HTF_EMA_SLOW=21`, `SCALP_USE_VOLUME_GATE`, `SCALP_VOLUME_LOOKBACK_MIN=20`, `SCALP_VOLUME_THRESHOLD_RATIO=1.0`, `SCALP_USE_CROSS_EXCHANGE_OFI`, `SCALP_CROSS_EXCHANGE_DISAGREE_BLOCK`, `SCALP_CROSS_EXCHANGE_AGREE_Z_MIN=0.5`, `SCALP_USE_BTC_DIRECTIONAL`, `SCALP_BTC_OFI_NEUTRAL_BAND=0.5`, `SCALP_USE_ADVERSE_SELECTION_GUARD`, `SCALP_ADVERSE_MID_MOVE_BPS=1.0`, `SCALP_ADVERSE_MOVE_WINDOW_MS=100`, `SCALP_USE_DEPTH_GATE`, `SCALP_MIN_TOP5_DEPTH_MULTIPLIER=5.0`, `SCALP_MAX_TOP1_CONSUME_PCT=20.0`, `SCALP_USE_ATR_AWARE_SL`, `SCALP_ATR_PERIOD=20`, `SCALP_ATR_TIMEFRAME="1m"`, `SCALP_ATR_SL_MULTIPLIER=0.3`, `SCALP_ATR_SL_FLOOR_BPS=1.5`, `SCALP_ATR_SL_CEILING_BPS=8.0`, `SCALP_MIN_OBSERVATIONS_FOR_LIVE_V2=300`, `SCALP_MIN_WIN_RATE_FOR_LIVE_V2=0.55`, `SCALP_MIN_AVG_NET_BPS_FOR_LIVE_V2=0.5`, `SCALP_MAX_HOLD_EXIT_PCT_V2=0.25`, `SCALP_MIN_DIRECTIONAL_ACC_1M_V2=0.57`). Class `V2Settings` (reflects all `SCALP_*` module attrs into a snapshot object); function `default_v2_settings()` returns a `V2Settings()`.

- **`scalping_agent_v2_integration.py`** — module constant `DB_COLUMNS_V2` (SQL for 13 new nullable columns); functions `evaluate_signal_v2(agent, signal, confluence_checker, atr_calc)`, `_initial_observation(signal)`, `_run_legacy_gates(agent, signal, obs)` (stub — calls `agent.run_legacy_gates` if it exists, else pass-through), `_compute_position_size_usd(agent, signal)`, and `is_ready_for_live_v2(stats, settings)` returning `{ready, reasons_failing, stats}`. Logger `scalping_v2.integration`.

- **`scalping_atr_sl.py`** — dataclass `TpSlV2(tp_bps, sl_bps, rr_actual, base_sl_bps, atr_bps, atr_adjusted, sl_clamped)`. Class `ATRStopCalculator(market_data, settings)` with methods `compute_tp_sl_v2(symbol, exchange, round_trip_bps) -> TpSlV2` and `_safe_atr_bps(symbol, exchange)`. Setting fallbacks used: `SCALP_NET_PROFIT_TARGET_BPS=3.0`, `SCALP_RR_RATIO=1.6`, `SCALP_ATR_SL_FLOOR_BPS=1.5`, `SCALP_ATR_SL_CEILING_BPS=8.0`, `SCALP_ATR_SL_MULTIPLIER=0.3`, `SCALP_USE_ATR_AWARE_SL=True`, `SCALP_ATR_PERIOD=20`, `SCALP_ATR_TIMEFRAME="1m"`. Logger `scalping_v2.atr_sl`.

- **`scalping_confluence.py`** — dataclasses `ConfluenceResult(passed, reason, score=0.0, gate_name="", metadata={})` and `CombinedConfluenceResult(passed, blocking_reason, confluence_score, strength_label, cross_exchange_agrees, btc_compatible, adverse_selection_ok, depth_ok, individual_results, metadata)`. Class `ConfluenceChecker(market_data, ofi_engine, settings)` with methods `check_vwap_alignment`, `check_htf_trend`, `check_volume`, `check_cross_exchange_ofi`, `check_btc_directional`, `check_adverse_selection`, `check_depth`, `run_all_gates(symbol, exchange, direction, primary_z, position_size_usd)`, `_build_combined(...)`, and static `_label_strength(soft_passed, cross)` returning `"VERY_STRONG"` / `"STRONG"` / `"MODERATE"` / `"WEAK"`. Logger `scalping_v2.confluence`.

- **`demo_simulate_v1_vs_v2.py`** — dataclass `WorldState` (symbol, exchange, direction, ofi_z, mid, vwap, ema_5m_fast/slow, vol, btc_ofi_z, cross_ofi_z, mid_then_100ms, atr, top1/top5_size_usd, true_outcome). Functions `generate_signal(rng)`, `_compute_truth(...)`, `evaluate_v1(world)`, `evaluate_v2(world, checker)`, `run_simulation(n_signals=10000, seed=42)`, `print_report(results, n_signals)`. Classes `MockMarketDataAdapter(world)` (implements `get_mid_price`, `get_mid_price_at_offset`, `get_session_vwap`, `get_ema`, `get_current_minute_volume`, `get_rolling_median_volume`, `get_atr`, `get_order_book`) and `MockOFIAdapter(world)` (implements `get_z_score`, `get_exchanges_for_symbol`). `__main__` runs 20 000 signals at seed 42. Pulls `FakeBook` / `FakeLevel` from `test_scalping_v2` for the order-book mock.

- **`test_scalping_v2.py`** — pytest module. Dataclasses `FakeLevel(price, size)` and `FakeBook(bids, asks)`. Classes `MockMarketData` (configurable defaults: vwap=50000, mid=50100, mid_then=50090, ema_fast=50050, ema_slow=49950, current_vol=1500, median_vol=1000, atr=50, 5-level FakeBook) and `MockOFIEngine` (z_scores + exchanges_by_symbol dicts). Fixtures `md`, `ofi`, `settings`, `checker`, `atr_calc`. Test classes: `TestVWAP`, `TestHTF`, `TestVolume`, `TestCrossExchange`, `TestBTCDirectional`, `TestAdverseSelection`, `TestDepth`, `TestCombined`, `TestATRStopLoss`, `TestActivationReadiness`. (Note: the docs claim 41 tests; the file actually contains 37 `def test_*` methods.)

## Duplication audit

**Method:** Computed SHA-256 of each `.py` in `scalping_v2/` against the same-named file in `agents/` using PowerShell `Get-FileHash -Algorithm SHA256` (read-only).

| File | scalping_v2 SHA-256 | agents/ SHA-256 | Verdict |
|------|---------------------|-----------------|---------|
| `scalping_agent_v2_integration.py` | `921D06546F26536EC17D0DCA586A11171FBD55E08789511C9DE43CF11648AD4D` | same | **identical** |
| `scalping_atr_sl.py` | `65C8CE040E58DFECDA8D713FD62614AF13E1FD68C0DA9FB0B266FDBF244A9F3E` | same | **identical** |
| `scalping_confluence.py` | `3D82CB13F89BA0FA3A653E4D2AF5AEEA4B1DCB5B7CB4959D82F07042DF553FA6` | same | **identical** |
| `settings_scalp_v2.py` | n/a | absent from `agents/` | **unique to bundle** (constants intended to be appended to `config/settings.py`) |
| `demo_simulate_v1_vs_v2.py` | n/a | absent from `agents/` | **unique to bundle** |
| `test_scalping_v2.py` | n/a | absent from `agents/` and from top-level `tests/` | **unique to bundle** |

The three integration files are byte-identical between bundle and `agents/`. No drift today; the risk is that future changes to `agents/scalping_*.py` are not mirrored back into `scalping_v2/` (or vice versa).

## v3 Roadmap items (from SCALPING_V3_ROADMAP.md)

- **V3-1 Per-symbol learning gate** — rolling 50-trade WR per (symbol, exchange) with auto-suspend < 45% WR for 24h then 30-trade probation. *Trigger:* Recalibration Query 5 shows > 3 pairs with negative `avg_net_bps` over 50+ trades and you find yourself manually editing `SCALP_PAIRS` more than once a month. *Scope:* ~150 LOC state machine + ~50 LOC table + ~30 LOC integration as gate 14.
- **V3-2 Time-of-day learning gate** — same idea per (hour, day-of-week); auto-narrows session window. *Trigger:* Recalibration Query 6 consistently shows a 10pp+ WR spread across hours AND highest/lowest hours don't match the fixed window. *Scope:* ~100 LOC hourly state + ~30 LOC integration; pairs with V3-1.
- **V3-3 Drawdown-aware position sizing (anti-martingale)** — halve size for next 5 trades after 2 consecutive losses; reset on a winner. *Trigger:* Recalibration Query 9 shows max drawdown periods that trigger the daily-loss circuit breaker more than once per month. *Scope:* ~80 LOC sizer + ~40 LOC integration + ~20 LOC dashboard. Note: must be *anti*-martingale, never the inverse.
- **V3-4 Continuous edge monitor with auto-halt** — background task computing 50- and 200-trade rolling WR / net bps / expectancy / dir-acc; auto-returns agent to observation mode (`SCALP_CAPITAL = 0`) if any metric drops below threshold for 50+ consecutive trades; red banner on dashboard. *Trigger:* "as soon as v2 goes live" — flagged as a priority-promoted item that should be built *before* flipping live capital. *Scope:* ~120 LOC monitor + ~40 LOC thresholding/halt + ~30 LOC dashboard.
- **V3-5 Maker-passive entry mode** — new `SCALP_ENTRY_MODE` with `taker` (current) / `maker_passive` (limit at best bid/ask with 2-second TIF, cancel-and-re-evaluate). *Trigger:* v2 is live and stable AND signal edge verified (WR > 55% live). *Scope:* ~250 LOC router + ~100 LOC fill tracking + ~50 LOC observation logging + ~30 LOC tests — largest single item.
- **V3-6 Hawkes process price modelling** — replace Brownian/linear-OFI assumption with multivariate self- and cross-exciting point processes. *Trigger:* only after V3-5 is live and 200+ live maker-mode trades document inventory shocks AS didn't anticipate. *Scope:* ~600+ LOC, multi-week, research grade — "don't build this unless …".
- **V3-7 ML / DRL extensions** — three sub-items: **V3-7a** feature-augmented threshold tuning (triggers at 1000+ entered observations with v2 columns populated; ~300 LOC + offline notebook); **V3-7b** DeepLOB CNN-LSTM on raw order book snapshots (triggers after V3-7a ships; ~500 LOC + training code); **V3-7c** end-to-end DRL replacement (trigger: "don't" — research only).
- **V3-8 Funding-rate harvesting strategy** — sibling agent: short-perp / long-spot on MEXC when funding rate exceeds threshold, collect 8h funding. *Trigger:* OFI scalping reaches stable profitability AND spare capacity in MEXC scalp fund AND funding rates show consistent positive expectancy over 30-day window. *Scope:* ~400 LOC + shared fund accounting; really a "v1 of a sibling strategy", not v3.
- **V3-9 Cross-impact alt scalping** — use BTC OFI as a *positive* leading signal (fire alt entries on strong BTC OFI even when alt's own OFI is weak), not just a blocker. *Trigger:* after V3-1 (per-symbol learning) so per-alt response windows are known. *Scope:* ~80 LOC signal generation + ~30 LOC integration as new entry path.
- **V3-10 Maker rebate venue support** — handle exchanges with *negative* maker fees with appropriate breakeven recalc. *Trigger:* after V3-5 ships and is live. *Scope:* ~50 LOC `FeeManager` extension (real work is V3-5).

Explicitly-rejected (not on the roadmap): stop-loss-free strategies, pyramiding into losers, aggressive scalp leverage, last-30-day overfit retraining, copy-trading / signal-following, discretionary overrides.

## Tests

`scalping_v2/test_scalping_v2.py` — 37 `test_*` methods (the SCALPING_V2.md companion claims 41).

- `TestVWAP.test_long_passes_when_mid_above_vwap` — VWAP gate passes for LONG when `mid > vwap`, returns score 1.0.
- `TestVWAP.test_long_fails_when_mid_below_vwap` — VWAP gate fails LONG when `mid < vwap`.
- `TestVWAP.test_short_passes_when_mid_below_vwap` — VWAP gate passes for SHORT when `mid < vwap`.
- `TestVWAP.test_short_fails_when_mid_above_vwap` — VWAP gate fails SHORT when `mid > vwap`.
- `TestVWAP.test_passes_when_data_unavailable` — fail-open: `vwap=None` returns passed=True.
- `TestHTF.test_long_passes_when_htf_uptrend` — HTF gate passes LONG when fast EMA > slow EMA on 5m.
- `TestHTF.test_long_fails_when_htf_downtrend` — HTF gate fails LONG when fast EMA < slow EMA.
- `TestHTF.test_short_passes_when_htf_downtrend` — HTF gate passes SHORT when fast EMA < slow EMA.
- `TestVolume.test_passes_when_volume_above_median` — Volume gate passes when current > rolling median.
- `TestVolume.test_fails_when_volume_below_threshold` — Volume gate fails when current < median × threshold.
- `TestVolume.test_handles_zero_median` — fail-open on `median_vol == 0`.
- `TestCrossExchange.test_blocks_when_other_venue_strongly_opposes` — blocks LONG when BITGET z ≤ -0.5.
- `TestCrossExchange.test_confirms_when_other_venue_agrees` — score 1.0 + "confirming" reason when other venue agrees beyond threshold.
- `TestCrossExchange.test_neutral_when_other_venue_in_band` — neutral (score 0.5) when other venue z within ±0.5.
- `TestCrossExchange.test_handles_no_other_venues` — passes with `others_count=0` when only primary venue listed.
- `TestBTCDirectional.test_blocks_alt_long_when_btc_bearish` — blocks alt LONG when BTC z < -band.
- `TestBTCDirectional.test_blocks_alt_short_when_btc_bullish` — blocks alt SHORT when BTC z > band.
- `TestBTCDirectional.test_allows_alt_long_when_btc_in_band` — neutral BTC z permits alt LONG.
- `TestBTCDirectional.test_allows_alt_long_when_btc_aligned` — aligned BTC z permits alt LONG.
- `TestBTCDirectional.test_btc_symbol_skips_gate` — BTC/USDT itself short-circuits the gate.
- `TestAdverseSelection.test_blocks_long_when_mid_dropped` — 1 bp drop at threshold is still allowed (boundary).
- `TestAdverseSelection.test_blocks_long_when_drop_exceeds_threshold` — 3 bps drop blocks LONG.
- `TestAdverseSelection.test_blocks_short_when_mid_rose` — 3 bps rise blocks SHORT.
- `TestAdverseSelection.test_allows_when_mid_stable` — 0.1 bp move within tolerance, allowed.
- `TestDepth.test_passes_when_book_deep` — default ~$250K top-5 passes for $100 position.
- `TestDepth.test_blocks_when_top5_inadequate` — top-5 < 5× required position USD blocks.
- `TestDepth.test_blocks_when_consume_pct_too_high` — > 20% of top-1 consumed blocks.
- `TestCombined.test_all_pass_with_clean_setup` — default mocks + BITGET agree → passed, confluence=3, strength=VERY_STRONG.
- `TestCombined.test_fails_fast_on_adverse_selection` — adverse fail short-circuits before depth check.
- `TestCombined.test_two_of_three_soft_gates_passes` — VWAP+HTF pass, volume fail → confluence=2, MODERATE.
- `TestCombined.test_one_of_three_soft_gates_fails` — only VWAP passes → blocked with "confluence" reason.
- `TestATRStopLoss.test_atr_widens_sl_on_volatile_pair` — ATR ~10 bps × 0.3 = 3 bps widens SL above 1.875 base.
- `TestATRStopLoss.test_atr_floor_applied_on_calm_pair` — tiny ATR → base SL 1.875 wins via `max(base, atr_sl)`.
- `TestATRStopLoss.test_atr_ceiling_caps_extreme_volatility` — huge ATR → `sl_clamped == "CEILING"`, sl=8 bps.
- `TestATRStopLoss.test_fee_aware_tp_increases_with_fees` — BITGET (2 bps fee) TP = 5; MEXC (0) TP = 3.
- `TestATRStopLoss.test_rr_actual_recomputed` — `rr_actual == tp_bps / sl_bps`.
- `TestATRStopLoss.test_disabled_atr_falls_back_to_base` — `SCALP_USE_ATR_AWARE_SL=False` → sl_bps=1.875, not adjusted.
- `TestActivationReadiness.test_all_criteria_met` — n=350, WR=0.58, net=0.7, max-hold=0.20, dir-acc=0.60 → ready, no failures.
- `TestActivationReadiness.test_insufficient_observations` — n=100 fails the n_closed check.
- `TestActivationReadiness.test_win_rate_below_threshold` — WR=0.51 below 0.55 fails.
- `TestActivationReadiness.test_max_hold_too_high` — max-hold=0.40 > 0.25 fails.

## TODOs / FIXMEs / stubs

- `scalping_v2/scalping_agent_v2_integration.py:185 — "implemented in ScalpingAgent. This stub keeps the demo runnable without"` (in the docstring of `_run_legacy_gates`, which falls back to a no-op pass-through when the agent doesn't expose `run_legacy_gates`).

No explicit `TODO` / `FIXME` / `XXX` / `HACK` markers found in any file.

## Known issues observed

- **Active duplication risk: three files maintained in two locations.** `scalping_agent_v2_integration.py`, `scalping_atr_sl.py`, and `scalping_confluence.py` are byte-identical between `scalping_v2/` and `agents/`. There is no symlink, no shared import, no CI check enforcing parity. Any future edit in `agents/` will silently drift the bundle (and the docs that point at it), and any edit in `scalping_v2/` will not reach the live agent. The bundle's own integration doc (`SCALPING_V2.md` § "Add the new modules to agents/") encodes the copy as a manual `cp`, so drift is the expected long-run state.
- **Settings are not actually in `config/settings.py`.** `settings_scalp_v2.py` exists as a parallel constants module that the agent does *not* import; the integration guide instructs the operator to append the values to `config/settings.py`. Whether that append has happened, and whether it stayed in sync with this file, can only be verified by inspecting `config/settings.py` (out of scope for this report).
- **Tests are not under the project's `tests/` tree.** `test_scalping_v2.py` lives inside the bundle and is invoked as `cd scalping_v2/ && python -m pytest test_scalping_v2.py -v`. Top-level `pytest` will not discover it. The bundle's test count claim (41) does not match the actual `def test_*` count (37); one of the three test classes likely lost members during edits.
- **Demo imports from the test module.** `demo_simulate_v1_vs_v2.py:241` does `from test_scalping_v2 import FakeBook, FakeLevel`. The demo therefore breaks if `test_scalping_v2.py` is ever excluded from a packaging step or moved without the demo.
- **`_run_legacy_gates` is a stub.** `scalping_agent_v2_integration.py:188` checks `hasattr(agent, "run_legacy_gates")` and silently no-ops if absent. If the live `ScalpingAgent` does not expose that exact attribute, the legacy 13-gate flow is bypassed entirely in this code path — the file documents this as a demo convenience but does not warn the caller.
- **Settings-shape mismatch.** `evaluate_signal_v2` in `scalping_agent_v2_integration.py` reads `agent.s.SCALP_POSITION_PCT` (via `getattr(agent, "s", ...)`) but the live scalping agent's settings handle convention isn't validated inside the bundle. The bundle and the agent must agree on the attribute name `s`; otherwise position size silently defaults to 0.5 × scalp_capital.
- **Documentation drift risk on metrics.** `SCALPING_V2.md` quotes specific simulation outputs (WR 58.13% → 66.84%, expectancy +44%, 5-loss streak 1.29% → 0.40%) that come from `demo_simulate_v1_vs_v2.py` at seed 42, n=20 000. Any tweak to the synthetic generator's assumptions will silently invalidate the headline numbers in the doc.

---

# Module Report: config + root entry points

## Purpose

This report covers the repo-root entry points and the `config/` package — the parts that don't belong to any feature package but glue the rest together:

- `config/settings.py` is the single tuning instrument. Per `CLAUDE.md`, nothing is hardcoded elsewhere — every threshold, weight, lookback, and feature flag lives here. The full per-constant audit is in `settings_audit.md` (926 lines, 446 constants).
- `main.py` is the async entry point: parses CLI flags, loads `keys.env` early, initialises DB, builds the Coordinator (which owns every agent), and runs until SIGINT/SIGTERM.
- `run.sh` is a four-line convenience launcher for the balanced/default profile.
- `pytest.ini` scopes pytest to `tests/` only so the `scalping_v2/` reference bundle doesn't collide.
- `requirements.txt` pins the source-of-truth dep set (53 lines; 32 direct deps).
- `scripts/` contains four operator tools.

## Files

| File | LOC | Role | One-sentence summary |
|------|----:|-----|----------------------|
| `config/__init__.py` | 1 | source | Empty package marker. |
| `config/settings.py` | 1269 | source | Every tunable in the system, grouped by domain; 443 module-level constants; 234 carry `# test:` sweep ranges. |
| `main.py` | 189 | source | Click CLI + asyncio entry; loads `keys.env` before importing agents; coordinator-owned shutdown with SIGINT/SIGTERM handlers and bounded teardown timeout (`SHUTDOWN_TIMEOUT_SEC`). |
| `run.sh` | 4 | script | `cd ~/cryptobot && source venv/bin/activate && python3.11 main.py --profile balanced --strategy default`. |
| `pytest.ini` | 6 | config | `testpaths = tests` — excludes `scalping_v2/` to prevent duplicate-basename collisions with `tests/test_scalping_v2.py`. |
| `requirements.txt` | 53 | config | Pinned direct deps; major sections: exchange (ccxt + protobuf), indicators, DB, sentiment, anthropic, web3, terminal UI, predictive (xgboost), utilities, testing. |
| `scripts/mexc_probe.py` | 136 | script | Read-only probe of the 4 MEXC keys to map each key → API-tradeable USDT spot symbol set; emits JSON; never prints secrets. |
| `scripts/migrate_scalp_v2.py` | 57 | script | Idempotent ALTER-TABLE migration adding the 13 v2 columns to `scalp_observations`. |
| `scripts/run_recalibration.py` | 59 | script | Extracts every `\`\`\`sql` block from `scalping_v2/SCALPING_V2_RECALIBRATION.md` and runs each statement against the live DB; reports failures. |
| `scripts/watch_scalp.sh` | 24 | script | One-shot/loop-friendly bash that prints scalp totals, recent obs, and skip reasons via `sqlite3 -box`. |

## settings.py — domains present

Full per-constant tables (current value, test range, type, used-by) are in `settings_audit.md`. The domain dividers in the file map to:

```
Paths
CORE MODE
APPROVAL MODE SETTINGS
CAPITAL
EXCHANGES
PAIR UNIVERSE
TIMEFRAMES
SIGNAL QUALITY GATE
REGIME DETECTION
ORDER FLOW IMBALANCE (OFI)
TECHNICAL INDICATORS
ARBITRAGE (signal track + dedicated arb engine)
FUNDING-RATE ARB AGENT (Phase 1 — observation mode, delta-neutral, binance)
MOMENTUM SIGNAL (TRACK B)
MEAN REVERSION SIGNAL (TRACK C)
LIQUIDITY SWEEP (TRACK D)
DYNAMIC GRID
SENTIMENT
NEWS GUARD
BTC CORRELATION GUARD
POSITION CORRELATION GUARD
SESSION TIMING
RISK MANAGEMENT
CIRCUIT BREAKERS
EXECUTION
CLAUDE AGENT
PREDICTIVE ENGINE (phase 2)
MULTI-AGENT COORDINATOR
BALANCE AGENT (agents/balance_agent.py)
STRATEGY → EXCHANGE ROUTING
SCALPING AGENT (agents/scalping_agent.py)
CROSS-CHAIN ARB AGENT
PLUGGABLE SENTIMENT AGGREGATOR
PLUGGABLE DATA SOURCES
MACRO REGIME MONITOR
BOT LOOP TIMING
LOGGING & UI
```

The `# test:` comment convention covers 234 / 443 ≈ 52.8 % of constants. Dead-config and "hardcoded-but-should-be-here" findings are catalogued in `settings_audit.md`.

## main.py

### CLI flags

| Flag | Default | Help |
|------|---------|------|
| `--profile` | `None` (falls back to `settings.ACTIVE_PROFILE`) | Risk profile: `conservative | balanced | aggressive | custom` |
| `--strategy` | `None` (falls back to `settings.ACTIVE_STRATEGY`) | Strategy: `default | arb_only | scalper | custom` |
| `--sim` | flag | Force simulation mode (`settings.SIM_MODE = True`) |
| `--live` | flag | Force live trading mode (`settings.SIM_MODE = False`) + warning log |
| `--debug` | flag | Enable DEBUG-level logging via `utils.logger.setup_logging` |
| `--dashboard` | flag | Run the Rich terminal dashboard alongside the bot |
| `--web-ui` | flag | Start web control panel on localhost:8765 (sets `settings.WEB_UI_ENABLED = True`) |

### Startup sequence

1. Insert project root into `sys.path`.
2. **Load `config/keys.env` before any other project import** — this matters because `agents/__init__.py:REGISTERED_AGENTS` calls each agent's `is_available()` at import time, and several (notably `CrossChainArbAgent`) gate availability on `ARBITRUM_RPC_URL` / `BASE_RPC_URL` / `OPTIMISM_RPC_URL`. Lazy `load_dotenv` calls in those modules fire too late for that initial sweep.
3. `setup_logging(debug=…)`.
4. Apply `--sim`/`--live` to `settings.SIM_MODE`.
5. Resolve profile name → `profile_manager.load(name)`.
6. Resolve strategy name → `get_strategy(name)`.
7. `init_db()` (idempotent — `Base.metadata.create_all()` only adds missing tables).
8. Log mode / profile / strategy / dashboard / web UI.
9. `asyncio.run(_run(profile, strategy, dashboard, web_ui))`.

### Async `_run`

- Set `settings.ACTIVE_PROFILE` / `settings.ACTIVE_STRATEGY` so `SignalAgentWrapper` picks them up when it constructs its `CryptoBot` (the wrapper builds the bot inside its `start()`; bypassing the wrapper to inject profile/strategy directly would break agent encapsulation).
- Construct `Coordinator()`.
- Wrap `coordinator.start()` as `component_tasks[0]`.
- If `--dashboard`: build `Dashboard(coordinator=…)`, `coordinator.set_dashboard(dash)`, add `dash.run()` to `component_tasks`.
- If `--web-ui`: set `settings.WEB_UI_ENABLED`, instantiate `WebServer(coordinator=…, bot=None)`, `await web_server.start()`. Failures here are caught and logged but do not abort the bot.

### Shutdown sequence

- Install `SIGINT` / `SIGTERM` signal handlers that set `stop_event` (`Event.set()`).
- On non-POSIX (Windows / non-main thread) where `add_signal_handler` raises `NotImplementedError`/`ValueError`/`RuntimeError`, fall back to top-level `KeyboardInterrupt` handling in `main()`.
- `await asyncio.wait([*component_tasks, stop_waiter], return_when=FIRST_COMPLETED)` — wakes when shutdown is requested OR a component exits on its own (crash detection).
- Within `SHUTDOWN_TIMEOUT_SEC`:
  - `coordinator.stop()` — stops every agent.
  - `web_server.stop()` if running.
  - Cancel any component task still running and `gather(..., return_exceptions=True)` with a bounded timeout.
- Log "Shutdown complete".

The comment block at `main.py:135-141` records the history: the previous SIGINT handler stopped only the signal bot's loop and left the coordinator, other agents, and data/macro refresh loops alive — the process hung until SIGKILL (exit 9). The current shape is the fix.

### Top-level exception handling

`main()` catches `KeyboardInterrupt` and logs "Shutting down (Ctrl+C)"; any other exception is logged as `CRITICAL` with traceback and the process exits with code 1.

## run.sh — verbatim

```bash
#!/bin/bash
cd ~/cryptobot
source venv/bin/activate
python3.11 main.py --profile balanced --strategy default
```

No CLI flags supported, no error handling. Assumes the venv is at `~/cryptobot/venv` and Python 3.11 is on PATH. Does not set `PYTHONPATH=.`, which is fine because `main.py:20` does `sys.path.insert(0, str(Path(__file__).parent))`.

## pytest.ini — verbatim

```ini
[pytest]
# Collect the project suite from tests/ only. The scalping_v2/ reference
# package ships its own standalone tests (run via `cd scalping_v2 && pytest`);
# they're excluded here so the integrated copy in tests/ is the single source
# of truth and there's no duplicate-basename collision.
testpaths = tests
```

## requirements.txt — verbatim

```text
# Exchange connectivity
ccxt==4.5.54          # includes ccxt.pro (WebSocket watch_* streaming)
protobuf==5.29.5      # required by ccxt.pro to decode MEXC's WS order-book feed

# Technical indicators
pandas-ta==0.3.14b
pandas==2.2.0
numpy==1.26.4

# Database
sqlalchemy==2.0.28
alembic==1.13.1

# Sentiment
praw==7.7.1           # Reddit
telethon==1.34.0      # Telegram
feedparser==6.0.11    # RSS news feeds
pytrends==4.9.2       # Google Trends
vaderSentiment==3.3.2 # Local NLP scoring
transformers==4.38.2  # FinBERT (optional, heavier)
torch==2.2.1          # Required for transformers

# Anthropic
anthropic==0.21.3

# Cross-chain arb — Web3 reads on Arbitrum / Base / Optimism. Lazy-imported
# by execution/chains/*; agent stays OFFLINE when web3 isn't installed.
web3==7.16.0

# Terminal UI
rich==13.7.1

# Predictive engine
scikit-learn==1.4.1
xgboost==2.0.3
joblib==1.3.3

# Utilities
python-dotenv==1.0.1
aiohttp==3.9.3
asyncio-throttle==1.0.2
httpx==0.27.0
tenacity==8.2.3       # Retry logic
python-dateutil==2.9.0
pytz==2024.1
colorama==0.4.6
click==8.1.7          # CLI argument parsing
pydantic==2.6.3       # Data validation

# Testing
pytest==8.1.0
pytest-asyncio==0.23.5
pytest-mock==3.12.0
```

32 distinct direct dependencies. Dependency drift vs `pip freeze` is documented in the master audit doc under "Dependencies".

## scripts/

| Script | Touches | Purpose | Archive? |
|--------|---------|---------|----------|
| `mexc_probe.py` | read MEXC private endpoint | Discover each MEXC key's API-tradeable USDT spot symbol set; build a key → symbol map for `mexc_key_router`. Read-only on exchange; prints JSON with `proposed_map`, `candidates_covered`, `candidates_not_tradeable`. | Keep — operator-only tool for re-routing MEXC keys. |
| `migrate_scalp_v2.py` | ALTER TABLE `scalp_observations` | Idempotent — adds 13 v2 columns if missing. Run after pulling v2 against an existing DB. | Keep until Alembic exists. |
| `run_recalibration.py` | read `SCALPING_V2_RECALIBRATION.md`, run SQL against live DB | Sanity-check the cookbook queries — verifies they stay valid as the schema migrates. Returns non-zero on failures. | Keep — weekly cookbook check per the v2 docs. |
| `watch_scalp.sh` | read DB only | Quick scalp-activity dashboard via `sqlite3 -box` — totals, recent observations, skip reasons. | Keep — operator tool. |

None of the scripts perform live trading, withdrawals, or order placement. Two of them call the DB; one calls MEXC read-only; the last is a shell wrapper around `sqlite3`.

## config/keys.env env vars (referenced from source)

Per audit rules, `config/keys.env` is not read. The following env var names are referenced anywhere in the codebase (grep `os.getenv` / `os.environ[`):

### Hardcoded names (14 distinct)

| Env var | Read from | One example |
|---------|-----------|-------------|
| `ANTHROPIC_API_KEY` | core/agent.py | `core/agent.py:21` |
| `BINANCE_API_KEY`, `BINANCE_SECRET` | core/market_data.py | `core/market_data.py:42` |
| `KRAKEN_API_KEY`, `KRAKEN_SECRET` | core/market_data.py | `core/market_data.py:43` |
| `BYBIT_API_KEY`, `BYBIT_SECRET` | core/market_data.py | `core/market_data.py:44` |
| `OKX_API_KEY`, `OKX_SECRET`, `OKX_PASSPHRASE` | core/market_data.py | `core/market_data.py:45` |
| `CRYPTOPANIC_API_KEY` | sentiment/sources/cryptopanic.py | `sentiment/sources/cryptopanic.py:113` |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | sentiment/sources/reddit.py | `sentiment/sources/reddit.py:98-100` |
| `REDDIT_USER_AGENT` | sentiment/sources/reddit.py | `sentiment/sources/reddit.py:100` |

### Dynamically-constructed name patterns

| Pattern | Constructed at | Notes |
|---------|----------------|-------|
| `<EXCHANGE>_API_KEY`, `<EXCHANGE>_SECRET` | `agents/__init__.py:88`, `agents/__init__.py:261`, `execution/arb_engine.py:791-792` | Used to gate agent / arb-engine availability per configured exchange. |
| `MEXC_KEY_{1..4}_API_KEY`, `MEXC_KEY_{1..4}_SECRET` | `execution/mexc_key_router.py:107-108, 134-135`, `scripts/mexc_probe.py:72-73` | Per-key MEXC routing — 4 numbered keys × 2 fields = 8 names. |
| `self.api_key_env_var` | `data_sources/base.py:116`, `data_sources/sources/{alpha_vantage,coingecko,cryptocompare,fred}.py`, `macro/sources/{base,fred_calendar}.py` | Each plugin source declares its env var name on the class; runtime reads it via `os.getenv(self.api_key_env_var, "")`. Per-source names are catalogued in `module_reports/data_sources.md` and `module_reports/macro.md`. |
| `self.rpc_env_var` | `execution/chains/{base_connector,_solidly_volatile,arbitrum}.py` | Each chain connector exposes `rpc_env_var` (e.g. `ARBITRUM_RPC_URL`, `BASE_RPC_URL`, `OPTIMISM_RPC_URL`); presence gates `is_available()`. |

Per `settings_audit.md` the full distinct-name count (including dynamic patterns expanded against the configured exchanges + per-source attributes) is 28 names.

## Imports graph

### `config.settings`

The most-imported module in the project. Importers (counted from a `grep -rln 'from config import settings\|from config.settings'`):

```
core/, agents/ (every agent file), execution/, signals/, ui/,
sentiment/aggregator.py, data_sources/__init__.py, macro/*,
strategies/*, profiles/profile_manager.py, scripts/* (3 of 4),
main.py
```

`config.settings` is the single most-coupled module by inbound-edge count. This is by design per `CLAUDE.md`.

## Tests

No `tests/test_main.py` or `tests/test_settings.py`. Tests reach `config.settings` via the normal import chain; many tests `monkeypatch.setattr(settings, …, …)` for isolation. No test directly exercises `main.py` startup or the shutdown sequence — the shutdown invariant (`SHUTDOWN_TIMEOUT_SEC` bounded teardown) is uncovered.

## TODOs / FIXMEs / stubs in this scope

- None in `main.py`, `run.sh`, `pytest.ini`, `requirements.txt`, `config/__init__.py`, or any `scripts/*`. 
- `config/settings.py` carries inline `# test:` annotations and a handful of explanatory comments but no `TODO` / `FIXME` / `XXX` markers (grep across this scope returns 0 hits).

## Known issues observed

- **`config/settings.py.backup` exists in the repo** — a 47 KB backup of an earlier settings file sits beside `settings.py`. Not tracked in `file_inventory.csv` source role tally but visible in `tree.txt`. Decision pending whether to archive vs delete.
- **No test exercises shutdown.** The whole point of the `main.py:135-141` rework was the SIGINT-leaves-coordinator-alive bug recorded in `MEMORY.md → project_shutdown_hang`. Without coverage, the regression is undetectable.
- **`run.sh` hardcodes `python3.11`** — fine on the recorded WSL2 Ubuntu image, but breaks on systems where `python3.11` isn't on PATH or the venv was created with `python3.12`.
- **`pytest.ini` doesn't pin `asyncio_mode = auto`** — pytest-asyncio default is `strict`, which works here because each async test is decorated explicitly. If anyone adds a bare `async def test_*`, it'll silently no-op until they notice.
- **`requirements.txt` and the installed `pip freeze` are deeply out of sync.** Major version drift on `ccxt` (4.5.54 = same), `pandas` (2.2.0 requested, 3.0.3 installed), `numpy` (1.26.4 → 2.4.6), `pytest` (8.1.0 → 9.0.3), `scikit-learn` (1.4.1 → 1.8.0), `xgboost` (2.0.3 → 3.2.0), `rich` (13.7.1 → 15.0.0), `pydantic` (2.6.3 → 2.13.4), `anthropic` (0.21.3 → 0.103.0). And `pandas-ta`, `transformers`, `torch`, `telethon`, `asyncio-throttle`, `colorama`, `pytz` are in `requirements.txt` but **not** in `pip freeze` — they're missing from the venv. See `Dependencies` section of the master audit.
- **`scripts/migrate_scalp_v2.py` is a stand-in for missing Alembic.** Five additional TODO comments in `database/models.py` flag other schema gaps without dedicated migration scripts.
---

## Configuration — `config/settings.py`

The full per-constant audit follows, reproduced from `settings_audit.md` (Phase 3 deliverable).

---

# Settings audit

## Conventions

`config/settings.py` uses a single-line comment convention to mark each tunable with its sweep range: `CONSTANT = <value>   # test: <lo>-<hi>` (or `# test range: <lo>-<hi>` for a handful of older entries). Both forms are recognised; the convention is documented in `CLAUDE.md` as a hard rule for every new setting.

**Coverage:** 232 of 446 module-level constants (52.0%) carry a `# test:` annotation. The unannotated remainder are mostly enums, paths, capital aliases, watch-list literals, base URLs, weight dicts (with sum-to-1.0 invariants), and the few legacy `*_SKIP_REASON` string constants.

## By domain

### Paths

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `BASE_DIR` | `Path(__file__).parent.parent` | — | Path | **UNUSED** |
| `DATA_DIR` | `BASE_DIR / "data"` | — | Path | **UNUSED** |
| `LOGS_DIR` | `BASE_DIR / "logs"` | — | Path | logger.py |
| `DB_PATH` | `DATA_DIR / "cryptobot.db"` | — | Path | database/, scripts/, tests/ (9 files) |

### Core mode

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SIM_MODE` | `True` | — | bool | <root>, agents/, core/, execution/, tests/, ui/ (19 files) |
| `ACTIVE_PROFILE` | `"balanced"` | — | str | <root>, agents/, core/, execution/ (6 files) |
| `ACTIVE_STRATEGY` | `"default"` | — | str | bot.py, custom.py, main.py, router.py |
| `APPROVAL_MODE` | `"autonomous"` | — | str | core/, tests/, ui/ (5 files) |

### Approval modes (per_trade / window / autonomous / session floor)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `PER_TRADE_SHOW_FULL_REASONING` | `True` | — | bool | **UNUSED** |
| `PER_TRADE_AUTO_EXPIRE_SECONDS` | `600` | — | int | **UNUSED** |
| `WINDOW_BRIEF_INCLUDES` | `[ 8 items ]` | — | list | **UNUSED** |
| `WINDOW_DEFAULT_DURATION_MINUTES` | `120` | — | int | prompts.py, test_prompts.py |
| `WINDOW_MIN_DURATION_MINUTES` | `15` | — | int | bot.py |
| `WINDOW_MAX_DURATION_MINUTES` | `480` | — | int | bot.py |
| `WINDOW_AUTO_RENEW` | `False` | — | bool | **UNUSED** |
| `WINDOW_PAUSE_ON_CIRCUIT_BREAKER` | `True` | — | bool | **UNUSED** |
| `WINDOW_MAX_TRADES_PER_HOUR` | `6` | — | int | bot.py |
| `WINDOW_REAPPROVE_ON_REGIME_SHIFT` | `True` | — | bool | **UNUSED** |
| `AUTO_MAX_TRADES_PER_HOUR` | `4` | — | int | bot.py, test_bot.py |
| `AUTO_MAX_TRADES_PER_DAY` | `20` | — | int | bot.py |
| `AUTO_NOTIFY_ON_ENTRY` | `True` | — | bool | **UNUSED** |
| `AUTO_NOTIFY_ON_EXIT` | `True` | — | bool | **UNUSED** |
| `AUTO_PAUSE_ON_LOSS_STREAK` | `3` | — | int | **UNUSED** |
| `AUTO_SUMMARY_INTERVAL_MIN` | `60` | — | int | **UNUSED** |
| `SESSION_MIN_SENTIMENT_SCORE` | `40` | — | int | **UNUSED** |
| `SESSION_BLOCK_CHOPPY_REGIME` | `True` | — | bool | **UNUSED** |
| `SESSION_BLOCK_EXTREME_FEAR` | `True` | — | bool | **UNUSED** |
| `SESSION_BLOCK_EXTREME_GREED` | `False` | — | bool | **UNUSED** |
| `SESSION_REQUIRE_CLEAR_NEWS` | `True` | — | bool | **UNUSED** |
| `SESSION_MIN_ACTIVE_PAIRS` | `2` | — | int | **UNUSED** |

### Capital — per-venue ledger

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `EXCHANGE_BALANCES` | `{ 9 keys }` | — | dict | agents/, core/, execution/, tests/, ui/ (9 files) |

### Exchanges

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `ENABLED_EXCHANGES` | `["binance", "kraken", "bybit", "kucoin", "mexc"]` | — | list | agents/, core/, signals/, tests/, ui/ (8 files) |
| `MIN_LIQUIDITY_USD` | `50_000` | — | int | **UNUSED** |
| `ORDER_BOOK_DEPTH` | `10` | — | int | market_data.py |
| `ORDER_BOOK_STREAM_PAIRS` | `20` | 5-50   (top-N active pairs to stream books for) | int | market_data.py |
| `ORDER_BOOK_WATCH_TIMEOUT_S` | `30.0` | 10-60  (max wait for one symbol's book update) | float | market_data.py |
| `ORDER_BOOK_ERROR_BACKOFF_S` | `1.0` | 0.5-5  (backoff after a book-stream error — prevents busy-spin) | float | market_data.py |
| `ORDER_BOOK_MAX_STREAMS_PER_CONN` | `24` | 12-30  (per-ws-connection subscription cap; MEXC silently drops subs above ~30, so book streams shard across this many symbols per connection) | int | market_data.py, test_market_data_stream.py |

### Pair universe

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `PAIR_UNIVERSE` | `"auto"` | — | str | market_data.py |
| `PAIR_UNIVERSE_TOP_N` | `50` | — | int | market_data.py |
| `PAIR_MIN_MARKET_CAP` | `"large"` | — | str | **UNUSED** |
| `FALLBACK_PAIRS` | `[ 16 items ]` | — | list | market_data.py |

### Timeframes

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `TIMEFRAMES` | `["5m", "15m", "1h"]` | — | list | market_data.py |
| `FAST_TIMEFRAME` | `"5m"` | — | str | market_data.py, momentum.py, reversion.py |
| `MID_TIMEFRAME` | `"15m"` | — | str | momentum.py, reversion.py |
| `SLOW_TIMEFRAME` | `"1h"` | — | str | core/, signals/, ui/ (5 files) |
| `CANDLE_LOOKBACK` | `{"5m": 200, "15m": 200, "1h": 200}` | — | dict | market_data.py |

### Signal quality gate

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SIGNAL_SCORE_THRESHOLD` | `65` | 50–80 | int | quality_gate.py |
| `MAX_ACTIVE_SIGNALS` | `3` | 1–5 | int | engine.py, quality_gate.py |
| `SIGNAL_EXPIRY_MINUTES` | `10` | 5–20 | int | momentum.py, quality_gate.py, reversion.py |
| `REQUIRE_MULTI_TF_CONFIRM` | `True` | — | bool | quality_gate.py |
| `MIN_TF_CONFIRMATIONS` | `2` | — | int | momentum.py, quality_gate.py, reversion.py |

### Regime detection (ADX / Hurst / ATR / BB)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `ADX_TRENDING_MIN` | `25` | 20–30 | int | momentum.py, regime_detector.py |
| `ADX_STRONG_TREND` | `40` | 35–45 | int | momentum.py, regime_detector.py |
| `ADX_RANGING_MAX` | `20` | 18–25 | int | regime_detector.py |
| `ADX_CHOPPY_MAX` | `15` | 12–18 | int | regime_detector.py |
| `ADX_GRID_MIN` | `15` | — | int | regime_detector.py |
| `ADX_GRID_MAX` | `25` | — | int | regime_detector.py |
| `ADX_GRID_RESET` | `30` | — | int | **UNUSED** |
| `HURST_TRENDING_MIN` | `0.55` | 0.52–0.62 | float | hurst.py, regime_detector.py |
| `HURST_REVERTING_MAX` | `0.48` | 0.42–0.50 | float | hurst.py, regime_detector.py |
| `HURST_LOOKBACK_BARS` | `200` | 100–300 | int | regime_detector.py |
| `HURST_RANDOM_ZONE` | `0.04` | — | float | **UNUSED** |
| `ATR_HIGH_VOL_PERCENTILE` | `90` | 80–95 | int | regime_detector.py |
| `ATR_LOW_VOL_PERCENTILE` | `20` | — | int | **UNUSED** |
| `ATR_PERCENTILE_LOOKBACK` | `100` | — | int | regime_detector.py |
| `BB_WIDTH_EXPANDING_FACTOR` | `1.3` | 1.2–1.5 | float | **UNUSED** |

### Order-flow imbalance (OFI)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `OFI_LEVELS` | `5` | — | int | ofi.py |
| `OFI_EMA_PERIOD` | `20` | — | int | ofi.py |
| `OFI_BULLISH_THRESHOLD` | `0.65` | 0.60–0.72 | float | ofi.py |
| `OFI_BEARISH_THRESHOLD` | `0.35` | 0.28–0.40 | float | ofi.py |
| `OFI_BOOST_AMOUNT` | `10` | — | int | ofi.py |
| `OFI_PENALTY_AMOUNT` | `15` | — | int | ofi.py |
| `VPIN_HIGH_THRESHOLD` | `0.75` | 0.65–0.85 | float | ofi.py |

### Technical indicators

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `RSI_PERIOD` | `14` | 9–21 | int | market_data.py |
| `RSI_MOMENTUM_MIN` | `40` | 35–50 | int | momentum.py |
| `RSI_MOMENTUM_MAX` | `70` | 65–75 | int | momentum.py |
| `RSI_OVERBOUGHT` | `70` | — | int | reversion.py |
| `RSI_OVERSOLD` | `30` | — | int | reversion.py |
| `MACD_FAST` | `12` | — | int | market_data.py |
| `MACD_SLOW` | `26` | — | int | market_data.py |
| `MACD_SIGNAL` | `9` | — | int | market_data.py |
| `BB_PERIOD` | `20` | — | int | market_data.py |
| `BB_STDDEV` | `2.0` | 1.8–2.5 | float | market_data.py |
| `BB_REVERSION_ENTRY` | `2.0` | — | float | **UNUSED** |
| `EMA_FAST` | `9` | — | int | market_data.py |
| `EMA_SLOW` | `21` | — | int | market_data.py |
| `EMA_TREND` | `50` | — | int | market_data.py |
| `VOLUME_SURGE_MULTIPLIER` | `2.0` | 1.5–3.0 | float | **UNUSED** |
| `VWAP_STRETCH_PCT` | `0.8` | 0.5–1.2 | float | reversion.py |

### Arbitrage (signal-track + dedicated arb engine + funding-rate arb)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `ARB_MIN_GAP_PCT` | `0.03` | 0.02–0.10   (bitget special-case) | float | arb_engine.py, arbitrage.py, test_arb_engine.py, web_server.py |
| `ARB_MIN_GAP_PCT_FALLBACK` | `0.35` | 0.25–0.50   (non-bitget — also signal track) | float | arb_engine.py, arbitrage.py, test_arb_engine.py, test_signal_arbitrage.py |
| `ARB_FEE_ESTIMATE_PCT` | `0.20` | — | float | arbitrage.py |
| `ARB_MAX_TRANSFER_SECONDS` | `60` | — | int | **UNUSED** |
| `ARB_MIN_LIQUIDITY_MULT` | `2.0` | — | float | **UNUSED** |
| `ARB_SCAN_INTERVAL_MS` | `500` | 250–2000 | int | arb_engine.py, test_signal_arbitrage.py |
| `ARB_BASE_POSITION_USD` | `60.0` | 10.0–80.0  (base for gap-proportional sizing; ~3% of FUND_ARB_CAPITAL=2000) | float | agents/, execution/, tests/ (5 files) |
| `ARB_SIZE_MULTIPLIER_CAP` | `4.0` | 2.0–6.0    (max gap/threshold scale-up) | float | arb_engine.py, test_arb_engine.py |
| `ARB_MIN_LIQUIDITY_USD` | `500.0` | 250–2000   (sum of top 3 book levels) | float | arb_engine.py |
| `ARB_MAX_CONCURRENT` | `3` | 1–5 | int | arb_engine.py, web_server.py |
| `ARB_DAILY_LOSS_HALT_PCT` | `2.0` | 1-5    (% of arb fund allocation) | float | arb_engine.py, test_arb_engine.py |
| `ARB_CONSECUTIVE_LOSS_HALT` | `5` | 3–10 | int | arb_engine.py, test_arb_engine.py |
| `ARB_BALANCE_BUFFER_PCT` | `5.0` | 2.0–10.0 | float | arb_engine.py |
| `ARB_SLIPPAGE_MIN_PCT` | `0.01` | 0.005–0.02 | float | arb_engine.py, test_arb_engine.py |
| `ARB_SLIPPAGE_MAX_PCT` | `0.25` | 0.1–0.5 | float | arb_engine.py, test_arb_engine.py |
| `ARB_FUNDING_RATE_MIN_PCT` | `0.05` | 0.03–0.10  (per 8h funding) | float | arb_engine.py |
| `ARB_FUNDING_RATE_EXIT_PCT` | `0.02` | 0.01–0.05 | float | arb_engine.py |
| `ARB_FUNDING_DAILY_LOSS_HALT_PCT` | `3.0` | 1-5    (% of arb fund allocation) | float | arb_engine.py, test_arb_engine.py |
| `ARB_FUNDING_CONSECUTIVE_LOSS_HALT` | `4` | — | int | arb_engine.py, test_arb_engine.py |

### Funding-rate arb agent (Phase 1 observation mode)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `FUNDING_OBSERVATION_MODE` | `True` | True/False  (NEVER False this phase) | bool | funding_arb_agent.py, funding_engine.py, test_funding_arb.py |
| `FUNDING_CAPITAL_USD` | `0.0` | 0-5000 | float | funding_arb_agent.py, web_server.py |
| `FUNDING_SYMBOLS` | `[ 12 items ]` | — | list | funding_engine.py, test_funding_arb.py |
| `FUNDING_SCAN_INTERVAL_SEC` | `60` | 30-300 | int | funding_arb_agent.py, test_funding_arb.py |
| `FUNDING_MIN_APR` | `0.06` | 0.03-0.30 (now \|apr\| >= floor) | float | funding_engine.py, test_funding_arb.py |
| `FUNDING_MIN_OI_MULT` | `10.0` | 5-50 | float | funding_engine.py, test_funding_arb.py |
| `FUNDING_MAX_NOTIONAL_USD` | `250.0` | 100-5000 | float | funding_arb_agent.py, funding_engine.py, models.py, test_funding_arb.py |
| `FUNDING_MAX_CONCURRENT` | `2` | 1-5 | int | funding_arb_agent.py, funding_engine.py, test_funding_arb.py |
| `FUNDING_MAX_OI_FRACTION` | `0.001` | 0.0005-0.01 | float | funding_arb_agent.py, funding_engine.py |
| `FUNDING_TARGET_LEVERAGE` | `2.0` | 1.5-3.0 | float | funding_arb_agent.py, funding_engine.py |
| `FUNDING_FLIP_EXIT_APR` | `0.0` | -0.05-0.03 | float | funding_engine.py, test_funding_arb.py |
| `FUNDING_BASIS_SIGMA_EXIT` | `1.5` | 1.0-3.0 | float | funding_engine.py |
| `FUNDING_MARGIN_ALERT_RATIO` | `1.5` | 1.2-2.0 | float | funding_engine.py |
| `FUNDING_MAX_HOLD_SEC` | `1209600` | 86400-2592000 | int | funding_arb_agent.py, funding_engine.py, test_funding_arb.py |
| `FUNDING_SIM_SLIPPAGE_PCT` | `0.0002` | 0.0001-0.001 | float | funding_arb_agent.py, funding_engine.py, test_funding_arb.py |
| `FUNDING_DAILY_LOSS_HALT_PCT` | `2.0` | 1-5    (% of FUNDING_CAPITAL_USD; 0-alloc → no-op) | float | funding_arb_agent.py, test_funding_arb.py |
| `FUNDING_CONSECUTIVE_LOSS_HALT` | `4` | 3-6 | int | funding_arb_agent.py, test_funding_arb.py |

### Arb watch pairs + fee map

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `ARB_WATCH_PAIRS` | `[ 16 items ]` | — | list | arb_engine.py, test_arb_engine.py |
| `ARB_FEE_MAP` | `{ 7 keys }` | — | dict | __init__.py, arb_engine.py, test_arb_engine.py, test_funds.py |

### Momentum signal (Track B)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `MOMENTUM_MIN_VOLUME_RATIO` | `2.0` | 1.5–3.0 | float | momentum.py |
| `MOMENTUM_REQUIRE_LARGE_CAP` | `True` | — | bool | **UNUSED** |
| `MOMENTUM_REQUIRE_HURST` | `True` | — | bool | regime_detector.py |
| `MOMENTUM_REQUIRE_OFI` | `False` | — | bool | momentum.py |
| `MOMENTUM_MIN_ADX` | `22` | 18–30 | int | regime_detector.py |
| `MOMENTUM_BREAKOUT_LOOKBACK` | `20` | 10–30 | int | momentum.py |

### Mean-reversion signal (Track C)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `REVERSION_BB_THRESHOLD` | `2.0` | 1.5–2.5 | float | **UNUSED** |
| `REVERSION_REQUIRE_DIVERGENCE` | `True` | — | bool | reversion.py |
| `REVERSION_REQUIRE_HURST` | `True` | — | bool | regime_detector.py |
| `REVERSION_MAX_ADX` | `22` | 18–28 | int | regime_detector.py |
| `REVERSION_VWAP_CONFIRM` | `True` | — | bool | reversion.py |

### Liquidity sweep (Track D)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SWEEP_MIN_WICK_PCT` | `0.5` | 0.3–0.8 | float | **UNUSED** |
| `SWEEP_OFI_FLIP_REQUIRED` | `True` | — | bool | **UNUSED** |
| `SWEEP_VOLUME_SPIKE_MULT` | `2.5` | 2.0–3.5 | float | **UNUSED** |
| `SWEEP_REVERSAL_CANDLES` | `3` | 2–5 | int | **UNUSED** |
| `SWEEP_KEY_LEVEL_LOOKBACK` | `50` | 30–100 | int | **UNUSED** |

### Dynamic grid

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `GRID_ENABLED` | `True` | — | bool | regime_detector.py |
| `GRID_SPACING_PCT` | `0.5` | 0.3–1.0 | float | **UNUSED** |
| `GRID_LEVELS_EACH_SIDE` | `5` | 3–8 | int | **UNUSED** |
| `GRID_ORDER_SIZE_PCT` | `0.5` | — | float | **UNUSED** |
| `GRID_AUTO_RESET` | `True` | — | bool | **UNUSED** |
| `GRID_RESET_COOLDOWN_MIN` | `30` | — | int | **UNUSED** |

### Sentiment (legacy weights + feeds)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SENTIMENT_ENABLED` | `True` | — | bool | **UNUSED** |
| `SENTIMENT_UPDATE_INTERVAL_SECONDS` | `300` | — | int | **UNUSED** |
| `SENTIMENT_LOOKBACK_HOURS` | `4` | — | int | **UNUSED** |
| `SENTIMENT_WEIGHTS` | `{ 5 keys }` | — | dict | **UNUSED** |
| `SENTIMENT_BOOST_THRESHOLD` | `70` | 60–80 | int | arbitrage.py, momentum.py, reversion.py |
| `SENTIMENT_BLOCK_THRESHOLD` | `30` | 20–40 | int | arbitrage.py, momentum.py, reversion.py |
| `SENTIMENT_BOOST_AMOUNT` | `15` | 5–20 | int | arbitrage.py, momentum.py, reversion.py |
| `SENTIMENT_SUPPRESS_AMOUNT` | `20` | 10–25 | int | arbitrage.py, momentum.py |
| `SENTIMENT_VELOCITY_WINDOW` | `2` | — | int | **UNUSED** |
| `SENTIMENT_VELOCITY_BOOST` | `True` | — | bool | **UNUSED** |
| `REDDIT_SUBREDDITS` | `[ 6 items ]` | — | list | reddit.py |
| `REDDIT_POST_LIMIT` | `100` | — | int | **UNUSED** |
| `TELEGRAM_CHANNELS` | `[ 3 items ]` | — | list | telegram.py |
| `NEWS_FEEDS` | `[ 3 items ]` | — | list | **UNUSED** |

### News guard

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `NEWS_GUARD_ENABLED` | `True` | — | bool | guards.py |
| `NEWS_GUARD_LOOKBACK_MINUTES` | `60` | — | int | guards.py |
| `NEWS_GUARD_BLOCK_KEYWORDS` | `[ 14 items ]` | — | list | guards.py |
| `NEWS_GUARD_WARN_KEYWORDS` | `[ 8 items ]` | — | list | guards.py |
| `NEWS_GUARD_PENALTY_BLOCK` | `999` | — | int | guards.py |
| `NEWS_GUARD_PENALTY_WARN` | `20` | — | int | guards.py |

### BTC correlation guard + skip-reason constants

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `BTC_GUARD_ENABLED` | `True` | — | bool | guards.py |
| `BTC_CRASH_PCT` | `2.0` | 1.5–3.0 | float | guards.py |
| `BTC_CRASH_WINDOW_MINUTES` | `30` | 15–60 | int | guards.py |
| `BTC_GUARD_SCORE_PENALTY` | `25` | 15–35 | int | guards.py |
| `BTC_GUARD_LOOKBACK_MINUTES` | `30` | 15-60 | int | bot.py, test_bot.py |
| `BTC_GUARD_LOOKBACK_DRIFT_MINUTES` | `2` | 0-5 (acceptable snapshot age slack) | int | bot.py, test_bot.py |
| `SENTIMENT_HARD_BLOCK_SKIP_REASON` | `"SENTIMENT_HARD_BLOCK"` | — | str | quality_gate.py, test_quality_gate.py |
| `MACRO_HARD_BLOCK_SKIP_REASON` | `"MACRO_HARD_BLOCK"` | — | str | **UNUSED** |
| `PRE_EVENT_PAUSE_SKIP_REASON` | `"PRE_EVENT_PAUSE"` | — | str | **UNUSED** |

### Position correlation guard

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `CORR_GUARD_ENABLED` | `True` | — | bool | guards.py |
| `CORR_HIGH_THRESHOLD` | `0.80` | 0.70–0.90 | float | guards.py |
| `CORR_PENALTY_AMOUNT` | `15` | 10–25 | int | guards.py |
| `CORR_BLOCK_THRESHOLD` | `0.95` | — | float | guards.py |
| `CORR_LOOKBACK_HOURS` | `24` | — | int | **UNUSED** |
| `CORR_ARB_EXEMPT` | `True` | — | bool | guards.py |

### Session timing

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SESSION_SCORING_ENABLED` | `True` | — | bool | quality_gate.py |
| `SESSION_WINDOWS` | `{ 5 keys }` | — | dict | bot.py, quality_gate.py |

### Risk management

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `MAX_POSITION_SIZE_PCT` | `0.03` | 0.01–0.05 | float | agent.py |
| `MAX_OPEN_POSITIONS` | `3` | 1–5 | int | **UNUSED** |
| `DEFAULT_STOP_LOSS_PCT` | `0.01` | 0.005–0.02 | float | agent.py |
| `DEFAULT_TAKE_PROFIT_PCT` | `0.02` | 0.01–0.04 | float | agent.py |
| `MIN_RISK_REWARD_RATIO` | `1.5` | 1.0–2.5 | float | quality_gate.py |
| `TRAILING_STOP_ENABLED` | `False` | — | bool | **UNUSED** |
| `TRAILING_STOP_PCT` | `0.008` | 0.005–0.015 | float | **UNUSED** |
| `TRAILING_STOP_ACTIVATE` | `0.01` | — | float | **UNUSED** |

### Circuit breakers

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `CIRCUIT_BREAKERS` | `{ 5 keys }` | — | dict | bot.py, dashboard.py, position_manager.py, web_server.py |
| `CIRCUIT_BREAKER_PAUSE_MINUTES` | `30` | — | int | **UNUSED** |
| `CIRCUIT_BREAKER_HALT_REQUIRES_MANUAL` | `True` | — | bool | **UNUSED** |
| `SHUTDOWN_LOG_EVENT` | `True` | — | bool | bot.py, test_bot.py |

### Shutdown

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SHUTDOWN_TIMEOUT_SEC` | `15` | 5-30 | int | main.py |

### Execution (order type / retries)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `ORDER_TYPE` | `"limit"` | — | str | **UNUSED** |
| `LIMIT_SLIPPAGE_PCT` | `0.05` | — | float | **UNUSED** |
| `ORDER_TIMEOUT_SECONDS` | `30` | — | int | **UNUSED** |
| `ORDER_RETRY_ON_FAIL` | `True` | — | bool | **UNUSED** |
| `ORDER_RETRY_COUNT` | `2` | — | int | **UNUSED** |

### Claude agent

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `CLAUDE_MODEL` | `"claude-sonnet-4-5"` | — | str | agent.py |
| `CLAUDE_MAX_TOKENS` | `1024` | — | int | agent.py |
| `CLAUDE_TEMPERATURE` | `0.2` | 0.1–0.4 | float | agent.py |
| `CLAUDE_CONTEXT` | `{ 12 keys }` | — | dict | agent.py |
| `CLAUDE_WINDOW_BRIEF` | `{ 7 keys }` | — | dict | **UNUSED** |
| `SELF_REVIEW_ENABLED` | `True` | — | bool | agent.py, bot.py |
| `SELF_REVIEW_MAX_TOKENS` | `512` | — | int | agent.py |
| `CLAUDE_MAX_DAILY_COST_USD` | `2.00` | — | float | **UNUSED** |

### Predictive engine (phase 2)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `PREDICTIVE_ENABLED` | `False` | — | bool | **UNUSED** |
| `PREDICTIVE_MIN_TRAINING_TRADES` | `50` | — | int | **UNUSED** |
| `PREDICTIVE_WIN_PROB_THRESHOLD` | `0.55` | — | float | **UNUSED** |
| `PREDICTIVE_RETRAIN_EVERY_N_DAYS` | `7` | — | int | **UNUSED** |
| `PREDICTIVE_MODEL_PATH` | `DATA_DIR / "model.joblib"` | — | Path | **UNUSED** |
| `PREDICTIVE_BOOST_STRONG` | `10` | — | int | **UNUSED** |
| `PREDICTIVE_PENALTY_WEAK` | `15` | — | int | **UNUSED** |

### Multi-agent coordinator — ring-fenced funds

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `FUND_SIGNAL_CAPITAL` | `1600.0` | 0–3000  signal agent — binance/bybit/kraken | float | agents/, execution/, tests/, ui/ (6 files) |
| `FUND_ARB_CAPITAL` | `2000.0` | 0–4000  cross-exchange arb — kraken/bybit/bitget/bitstamp/gateio/bitfinex | float | agents/, execution/, tests/, ui/ (8 files) |
| `FUND_MEXC_SCALP_CAPITAL` | `500.0` | 0–1000  scalping, MEXC only (observation until SCALP_CAPITAL>0) | float | agents/, tests/, ui/ (7 files) |
| `FUND_MEXC_ARB_CAPITAL` | `500.0` | 0–1000  MEXC-only arb (counterparty-capped; un-wired until soak data justifies a dedicated agent) | float | test_funds.py |
| `FUND_XCHAIN_CAPITAL` | `0.0` | 0       observation-only this soak — see XCHAIN_CAPITAL (line ~932) | float | test_funds.py |
| `FUND_FUNDING_CAPITAL` | `0.0` | 0       observation-only this soak — see FUNDING_CAPITAL_USD (line ~264) | float | test_funds.py |
| `FUND_DAILY_LOSS_HALT_PCT` | `10.0` | 5–20 | float | coordinator.py, test_coordinator.py, test_funds.py |
| `STARTING_CAPITAL` | `5000.0` | 200–10000   (sum(FUND_*) = 4600; +400 reserve) | float | agents/, core/, execution/, tests/ (6 files) |
| `SIGNAL_AGENT_CAPITAL` | `FUND_SIGNAL_CAPITAL` | 100–800 | float | __init__.py, test_bot.py, test_equity_reconstruction.py, test_funds.py |
| `ARB_AGENT_CAPITAL` | `FUND_ARB_CAPITAL` | 100–1000 | float | __init__.py, test_bot.py, test_funds.py |
| `ARB_CAPITAL_PER_EXCHANGE` | `100.0` | 50–200 | float | arb_engine.py, test_arb_engine.py |

### Balance agent (compounding / sizing / sim / web-UI v2)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `REBALANCE_LIVE_ENABLED` | `False` | False         (hard gate; live ccxt.withdraw) | bool | agents/, tests/, ui/ (7 files) |
| `BALANCE_STRICT_OPEN_POSITION_BLOCK` | `True` | True/False    (rail 3; floor rail 2 is always-on) | bool | balance_agent.py |
| `KELLY_FRACTION` | `0.25` | 0.10-0.50  (fraction of full Kelly; never use full) | float | growth_optimal.py, test_balance_agent.py |
| `COMPOUND_RESERVE_PCT` | `0.05` | 0.02-0.15  (uncommitted buffer held out of the pool) | float | growth_optimal.py, test_balance_agent.py, test_funds.py, web_server.py |
| `ALLOCATION_CONFIDENCE` | `0.0` | 0.0-1.0    (0 = risk-parity, 1 = growth-optimal) | float | growth_optimal.py, test_balance_agent.py |
| `FUND_CAPACITY_CEILINGS_USD` | `{}` | tune from depth data; MEXC nodes carry a hard cap | dict | growth_optimal.py, inventory_state.py, test_balance_agent.py |
| `INTERNALIZE_WINDOW_S` | `300` | 60-1800    (wait for self-correction before transferring) | int | planner.py |
| `REBALANCE_DAILY_LIMIT` | `3` | 1-10       (max physical rebalances per UTC day) | int | balance_agent.py, test_balance_agent.py, web_server.py |
| `WITHDRAWAL_ROUTES` | `{}` | seeded from ccxt; live cex rail no-ops with empty map | dict | balance_agent.py, base.py, cex_rail.py, test_balance_agent.py |
| `SIM_WITHDRAWAL_FEE_USD` | `1.0` | 0.04-1.6   (simulated per-transfer fee, route-dependent live) | float | agents/, database/, tests/ (5 files) |
| `BALANCE_STRUCTURAL_DRIFT_HINT` | `0.20` | 0.10-0.40 | float | planner.py, test_balance_agent.py |
| `SIM_TRANSFER_DELAY_S` | `600` | 60-7200    (simulated in-transit time, sim_rail) | int | balance_agent.py, sim_rail.py, test_balance_agent.py |
| `SIM_REBALANCE_FAILURE_RATE` | `0.0` | 0.0-0.10   (inject failures to exercise auto-pause) | float | sim_rail.py, test_balance_agent.py |
| `REBALANCE_CONFIRM_WINDOW_S` | `3` | 2-30       (/action/rebalance arm→confirm window) | int | balance_agent.py, test_balance_agent.py |
| `REBALANCE_ARM_TIMEOUT_S` | `10` | 5-30 | int | web_server.py |
| `BALANCE_AUTO_DISPATCH` | `False` | True/False | bool | balance_agent.py |
| `STABLECOIN_BENCHMARK_APR_PCT` | `5.0` | 3-8 | float | **UNUSED** |
| `FUNDING_DELTA_TOLERANCE_USD` | `5.0` | 1-20 | float | **UNUSED** |
| `BALANCE_AGENT_CAPITAL` | `0.0` | 0.0       (BalanceAgent is operational, not alpha) | float | balance_agent.py |
| `BALANCE_SCAN_INTERVAL_SEC` | `60` | 15-300 | int | balance_agent.py |

### Strategy → exchange routing

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `STRATEGY_EXCHANGE_MAP` | `{ 5 keys }` | — | dict | agents/, core/, execution/, tests/, ui/ (9 files) |

### Scalping agent v1 — capital, pairs, MEXC key map, OFI, execution

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SCALP_CAPITAL` | `500.0` | 0-1000  ($0 = observation; >0 = sim execution) | float | agents/, scalping_v2/, tests/ (8 files) |
| `SCALP_PAIRS` | `[ 96 items ]` | — | list | agents/, core/, tests/, ui/ (6 files) |
| `MEXC_PAIR_KEY_MAP` | `{ 96 keys }` | — | dict | mexc_key_router.py, test_mexc_key_router.py, test_scalp_pair_coverage.py |
| `SCALP_NET_PROFIT_TARGET_BPS` | `3.0` | 2.0-8.0   (net profit after fees) | float | scalping_agent.py, scalping_atr_sl.py, test_scalping_agent.py |
| `SCALP_RR_RATIO` | `1.6` | 1.3-2.5   (tp_bps / sl_bps) | float | scalping_agent.py, scalping_atr_sl.py, test_scalping_agent.py |
| `SCALP_MAX_BREAKEVEN_WIN_RATE` | `0.65` | 0.55-0.75 (block if math needs >65% wr) | float | scalping_agent.py |
| `SCALP_FEE_DEFAULT_BPS` | `10.0` | — | float | scalping_agent.py |
| `SCALP_FEE_OVERRIDES` | `{ 2 keys }` | — | dict | scalping_agent.py, test_scalping_agent.py |
| `SCALP_OFI_Z_ENTRY` | `2.0` | 1.0-2.5   (z-score entry threshold; v2 raised 1.5→2.0) | float | scalping_agent.py, settings_scalp_v2.py, test_scalp_v2_integration.py, test_scalping_agent.py |
| `SCALP_OFI_Z_EXIT` | `0.3` | 0.1-0.7   (OFI exhaustion exit) | float | queries.py, scalping_agent.py, test_scalping_agent.py |
| `SCALP_OFI_Z_CONTRADICT` | `-0.8` | -0.4 to -1.5 (OFI flip exit) | float | scalping_agent.py |
| `SCALP_OFI_PERSIST_TICKS` | `5` | 2-6       (consecutive ticks above threshold; v2 raised 3→5) | int | scalping_agent.py, settings_scalp_v2.py, test_scalp_v2_integration.py, test_scalping_agent.py |
| `SCALP_OFI_LEVELS` | `10` | 1-10      (book depth levels) | int | scalping_agent.py, test_scalping_agent.py |
| `SCALP_OFI_WINDOW_SEC` | `20` | 10-40     (bucket accumulation window seconds) | int | scalping_agent.py |
| `SCALP_ZSCORE_WINDOW` | `80` | 40-150    (rolling z-score normalisation periods) | int | scalping_agent.py |
| `SCALP_MAX_SPREAD_BPS` | `3.0` | 1.5-6.0   (max bid-ask spread bps) | float | scalping_agent.py |
| `SCALP_STALE_MID_THRESHOLD_SEC` | `60` | 30-180    (skip entry if a symbol's mid hasn't moved for >= N sec — frozen feed = stale OFI) | int | scalping_agent.py, test_scalping_agent.py |
| `SCALP_MAX_HOLD_SEC` | `180` | 60-300    (force exit after N seconds) | int | scalping_agent.py |
| `SCALP_SCAN_INTERVAL_MS` | `100` | 50-500    (main loop interval ms) | int | scalping_agent.py |
| `SCALP_MAX_CONCURRENT` | `2` | 1-3       (max open scalp positions) | int | scalping_agent.py |
| `SCALP_POSITION_SIZE_USD` | `20.0` | 10-50     (per-trade size USD; ~3% of FUND_MEXC_SCALP_CAPITAL=500 baseline) | float | queries.py, scalping_agent.py, test_web_server.py |

### Scalping agent v1 — circuit breakers, session, BTC guard, depth weights

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SCALP_DAILY_LOSS_HALT_PCT` | `3.0` | 1-5       (% of scalp fund allocation; was abs USD) | float | scalping_agent.py, test_scalping_agent.py |
| `SCALP_CONSEC_LOSS_PAUSE` | `4` | 3-6       (consecutive loss pause count) | int | scalping_agent.py |
| `SCALP_SESSION_START_UTC` | `0` | 6-13      (v2: London/NY overlap start, raised 7→12) | int | scalping_agent.py, settings_scalp_v2.py, test_scalp_v2_integration.py, test_scalping_agent.py |
| `SCALP_SESSION_END_UTC` | `24` | 14-20     (v2: overlap end, lowered 17→16) | int | scalping_agent.py, settings_scalp_v2.py, test_scalp_v2_integration.py, test_scalping_agent.py |
| `SCALP_RESPECT_NEWS_GUARD` | `True` | True/False | bool | scalping_agent.py |
| `SCALP_BTC_GUARD_PCT` | `0.3` | 0.2-0.6   (block if \|BTC 1m change\| > N%) | float | scalping_agent.py |
| `SCALP_TRACKER_INTERVAL_SEC` | `15` | 10-30 | int | scalping_agent.py |
| `SCALP_DEPTH_WEIGHTS` | `{ 10 keys }` | — | dict | scalping_agent.py, test_scalping_agent.py |

### Scalping agent v2 — selectivity gates + activation criteria

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SCALP_USE_CONFLUENCE` | `True` | — | bool | scalping_agent.py, settings_scalp_v2.py, test_scalp_v2_integration.py, test_scalping_agent.py |
| `SCALP_CONFLUENCE_REQUIRED` | `2` | 1-3 | int | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_VWAP_GATE` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_HTF_TREND_GATE` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_HTF_TIMEFRAME` | `"5m"` | — | str | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_HTF_EMA_FAST` | `8` | — | int | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_HTF_EMA_SLOW` | `21` | — | int | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_VOLUME_GATE` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_VOLUME_LOOKBACK_MIN` | `20` | 10-40 | int | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_VOLUME_THRESHOLD_RATIO` | `1.0` | 0.8-1.5  (current 1m vol ≥ ratio × median) | float | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_CROSS_EXCHANGE_OFI` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_CROSS_EXCHANGE_DISAGREE_BLOCK` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_CROSS_EXCHANGE_AGREE_Z_MIN` | `0.5` | 0.3-1.0 | float | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_BTC_DIRECTIONAL` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_BTC_OFI_NEUTRAL_BAND` | `0.5` | 0.3-0.8  (\|BTC z\| below this = neutral) | float | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_ADVERSE_SELECTION_GUARD` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_ADVERSE_MID_MOVE_BPS` | `1.0` | 0.5-2.0 | float | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_ADVERSE_MOVE_WINDOW_MS` | `100` | 50-300 | int | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_DEPTH_GATE` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_MIN_TOP5_DEPTH_MULTIPLIER` | `5.0` | 3-10 | float | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_MAX_TOP1_CONSUME_PCT` | `20.0` | 10-40 | float | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_ATR_AWARE_SL` | `True` | — | bool | agents/, scalping_v2/, tests/ (5 files) |
| `SCALP_ATR_PERIOD` | `20` | 10-30 | int | scalping_atr_sl.py, settings_scalp_v2.py |
| `SCALP_ATR_TIMEFRAME` | `"1m"` | — | str | scalping_atr_sl.py, settings_scalp_v2.py |
| `SCALP_ATR_SL_MULTIPLIER` | `0.3` | 0.2-0.5 | float | scalping_atr_sl.py, settings_scalp_v2.py |
| `SCALP_ATR_SL_FLOOR_BPS` | `1.5` | 1.0-3.0   (never tighter than this) | float | scalping_atr_sl.py, settings_scalp_v2.py |
| `SCALP_ATR_SL_CEILING_BPS` | `8.0` | 6-12      (never wider than this) | float | scalping_atr_sl.py, settings_scalp_v2.py |
| `SCALP_MIN_OBSERVATIONS_FOR_LIVE` | `200` | — | int | queries.py |
| `SCALP_MIN_WIN_RATE_FOR_LIVE` | `0.52` | — | float | queries.py |
| `SCALP_MIN_AVG_NET_BPS_FOR_LIVE` | `0.0` | — | float | queries.py |
| `SCALP_MAX_HOLD_EXIT_PCT` | `0.30` | — | float | queries.py |
| `SCALP_MIN_DIRECTIONAL_ACC_1M` | `0.55` | — | float | queries.py |
| `SCALP_MIN_OBSERVATIONS_FOR_LIVE_V2` | `300` | — | int | queries.py, scalping_agent_v2_integration.py, settings_scalp_v2.py |
| `SCALP_MIN_WIN_RATE_FOR_LIVE_V2` | `0.55` | — | float | queries.py, scalping_agent_v2_integration.py, settings_scalp_v2.py |
| `SCALP_MIN_AVG_NET_BPS_FOR_LIVE_V2` | `0.5` | — | float | queries.py, scalping_agent_v2_integration.py, settings_scalp_v2.py |
| `SCALP_MAX_HOLD_EXIT_PCT_V2` | `0.25` | — | float | queries.py, scalping_agent_v2_integration.py, settings_scalp_v2.py |
| `SCALP_MIN_DIRECTIONAL_ACC_1M_V2` | `0.57` | — | float | queries.py, scalping_agent_v2_integration.py, settings_scalp_v2.py |

### Cross-chain arb agent (XCHAIN — observation-only)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `XCHAIN_LIVE_ENABLED` | `False` | False         (hard gate; keep False) | bool | base_connector.py, crosschain_agent.py, crosschain_engine.py, models.py |
| `XCHAIN_CAPITAL` | `0.0` | 0, 100, 250   (0 = observation mode) | float | agents/, database/, execution/, tests/, ui/ (7 files) |
| `XCHAIN_MIN_NET_EDGE_BPS` | `15.0` | 8, 12, 15, 20, 30 | float | crosschain_engine.py, test_crosschain_engine.py |
| `XCHAIN_GAS_BUDGET_BPS` | `5.0` | 3, 5, 8       (gas-as-%-of-notional cap) | float | crosschain_engine.py, test_crosschain_engine.py |
| `XCHAIN_SLIPPAGE_TOLERANCE_BPS` | `10.0` | 5, 10, 20     (per-leg price impact tolerance) | float | crosschain_engine.py, test_crosschain_engine.py |
| `XCHAIN_MAX_POSITION_USD` | `50.0` | 25, 50, 100, 250 | float | base_connector.py, crosschain_engine.py, test_crosschain_engine.py |
| `XCHAIN_INVENTORY_DRIFT_PCT` | `0.20` | 0.10, 0.20, 0.30  (theta; rebalance trigger) | float | inventory.py, test_crosschain_agent.py, test_inventory_targets.py |
| `XCHAIN_SCAN_INTERVAL_MS` | `2000` | 1000, 2000, 5000 | int | crosschain_engine.py |
| `XCHAIN_DAILY_LOSS_HALT_PCT` | `2.0` | 1-5    (% of XCHAIN_CAPITAL; 0-alloc → no-op) | float | crosschain_engine.py, test_crosschain_engine.py |
| `XCHAIN_CONSECUTIVE_LOSS_HALT` | `5` | 3, 5 | int | crosschain_engine.py, test_crosschain_engine.py |
| `XCHAIN_MAX_CONCURRENT` | `1` | 1, 2          (per-symbol scan concurrency cap) | int | crosschain_engine.py |
| `XCHAIN_MAX_BLOCK_STALENESS` | `5` | 2, 5, 10 | int | crosschain_engine.py |
| `XCHAIN_SYMBOLS` | `["WETH-USDC"]` | keep single pair first | list | _solidly_volatile.py, arbitrum.py, crosschain_engine.py, inventory.py |
| `XCHAIN_CHAINS` | `["arbitrum", "base", "optimism"]` | — | list | __init__.py, inventory.py, test_crosschain_agent.py |
| `XCHAIN_VENUES` | `{ 5 keys }` | — | dict | execution/, tests/ (5 files) |
| `XCHAIN_RPC_ENV_VARS` | `{ 3 keys }` | — | dict | **UNUSED** |

### Portfolio CBs + kill switch

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `PORTFOLIO_DAILY_LOSS_HALT_PCT` | `3.0` | 2.0–5.0 | float | coordinator.py, test_coordinator.py |
| `PORTFOLIO_MAX_EXPOSURE_PCT` | `80.0` | 60–95 | float | coordinator.py |
| `PORTFOLIO_MONITOR_INTERVAL_SEC` | `30` | 15–120 | int | coordinator.py, models.py |
| `KILL_SWITCH_CONFIRM_REQUIRED` | `False` | — | bool | **UNUSED** |

### Pluggable sentiment aggregator

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SENTIMENT_COMPOSITE_FLOOR` | `-60` | -50 to -70   (session-block threshold) | int | aggregator.py, test_sentiment.py |
| `SENTIMENT_FEAR_GREED_FLOOR` | `15` | 10–25        (extreme-fear cutoff) | int | aggregator.py |
| `SENTIMENT_BTC_GUARD_PCT` | `-2.0` | -1.5 to -3.0 (30m BTC drop trigger) | float | aggregator.py, test_sentiment.py |
| `SENTIMENT_BTC_GUARD_PENALTY` | `-15` | -10 to -25   (penalty on alt signals) | int | aggregator.py, test_sentiment.py |
| `SENTIMENT_NEWS_GUARD_ENABLED` | `True` | — | bool | **UNUSED** |
| `SENTIMENT_REFRESH_INTERVAL_SEC` | `300` | 60–900       (min between get_current() refreshes) | int | aggregator.py |
| `SENTIMENT_HTTP_TIMEOUT_SEC` | `10` | 5–30 | int | cryptopanic.py, fear_greed.py |
| `SENTIMENT_WEIGHT_FEAR_GREED` | `0.4` | — | float | fear_greed.py |
| `SENTIMENT_WEIGHT_CRYPTOPANIC` | `0.25` | — | float | cryptopanic.py |
| `SENTIMENT_WEIGHT_REDDIT` | `0.2` | — | float | reddit.py |
| `SENTIMENT_WEIGHT_GOOGLE_TRENDS` | `0.1` | — | float | google_trends.py |
| `SENTIMENT_WEIGHT_TELEGRAM` | `0.05` | — | float | telegram.py |

### Pluggable data sources (refresh intervals + watch lists + base URLs)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `COINGLASS_REFRESH_SEC` | `300` | 60-600 | int | coinglass.py |
| `COINGLASS_REFERENCE_EXCHANGE` | `"Binance"` | Binance \| Bybit \| OKX | str | **UNUSED** |
| `COINGLASS_RATE_LIMIT_PER_MIN` | `6` | 3-12 | int | coinglass.py, test_data_sources.py |
| `COINGECKO_REFRESH_SEC` | `300` | 60-900 | int | coingecko.py |
| `FRED_REFRESH_SEC` | `3600` | 1800-7200 | int | fred.py |
| `ALPHA_VANTAGE_REFRESH_SEC` | `900` | 300-1800 | int | alpha_vantage.py |
| `FRANKFURTER_REFRESH_SEC` | `3600` | 1800-7200 | int | frankfurter.py |
| `COINGECKO_USE_PRO` | `False` | True/False | bool | coingecko.py, test_data_sources.py |
| `DATA_SOURCES_REFRESH_LOOP_SEC` | `60` | 30-300  (top-level refresh_all tick) | int | bot.py |
| `DATA_SOURCES_HTTP_TIMEOUT_SEC` | `10` | 5-30 | int | data_sources/, macro/ (14 files) |
| `COINGLASS_WATCH_PAIRS` | `[ 16 items ]` | — | list | coinglass.py |
| `ALPHA_VANTAGE_SYMBOLS` | `[ 4 items ]` | — | list | alpha_vantage.py, test_data_sources.py |
| `FRED_SERIES` | `[ 9 items ]` | — | list | fred.py |
| `FRANKFURTER_PAIRS` | `[ 8 items ]` | — | list | frankfurter.py |

### Data-source risk regime thresholds (VIX / DXY)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `DATA_VIX_RISK_ON_MAX` | `15` | 12-18 | int | fred.py |
| `DATA_VIX_RISK_OFF_MIN` | `25` | 22-30 | int | fred.py |
| `DATA_VIX_CRISIS_MIN` | `35` | 30-40 | int | bot.py, fred.py |
| `DATA_DXY_STRONG_THRESHOLD` | `105` | 102-108 | int | frankfurter.py |
| `DATA_DXY_WEAK_THRESHOLD` | `95` | 92-98 | int | frankfurter.py |

### Alpha Vantage rate limiting

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `ALPHA_VANTAGE_DAILY_CALL_BUDGET` | `20` | 10-25 | int | alpha_vantage.py |
| `ALPHA_VANTAGE_PACE_SEC` | `1.3` | 1.1-3.0  (sleep between calls) | float | alpha_vantage.py, test_data_sources.py |

### Data-source base URLs

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `FRANKFURTER_BASE_URL` | `"https://api.frankfurter.dev/v1"` | — | str | frankfurter.py |
| `WORLD_BANK_BASE_URL` | `"https://api.worldbank.org/v2"` | — | str | world_bank.py |
| `IMF_DATAMAPPER_URL` | `"https://www.imf.org/external/datamapper/api/v1"` | — | str | imf.py |
| `ECB_BASE_URL` | `"https://data-api.ecb.europa.eu/service/data"` | — | str | ecb.py |
| `US_TREASURY_BASE_URL` | `"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/d…` | — | str | us_treasury.py |
| `CFTC_BASE_URL` | `"https://publicreporting.cftc.gov/resource/jun7-fc8e.json"` | — | str | cftc_cot.py |
| `BINANCE_FUTURES_BASE_URL` | `"https://fapi.binance.com"` | — | str | binance_futures.py |
| `BYBIT_BASE_URL` | `"https://api.bybit.com"` | — | str | bybit_derivs.py |
| `CRYPTOCOMPARE_BASE_URL` | `"https://min-api.cryptocompare.com/data"` | — | str | cryptocompare.py |

### Data-source new-source watch lists + refresh intervals

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `CRYPTOCOMPARE_REFRESH_SEC` | `300` | 60-900 | int | cryptocompare.py |
| `CRYPTOCOMPARE_FSYMS` | `["BTC", "ETH", "SOL", "BNB", "XRP"]` | — | list | cryptocompare.py |
| `CRYPTOCOMPARE_TSYM` | `"USD"` | — | str | cryptocompare.py |
| `BINANCE_FUTURES_REFRESH_SEC` | `60` | 15-300 | int | binance_futures.py |
| `BINANCE_FUTURES_SYMBOLS` | `["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]` | — | list | binance_futures.py |
| `BYBIT_REFRESH_SEC` | `60` | 15-300 | int | bybit_derivs.py |
| `BYBIT_SYMBOLS` | `["BTCUSDT", "ETHUSDT", "SOLUSDT"]` | — | list | bybit_derivs.py |
| `CFTC_REFRESH_SEC` | `3600 * 6` | — | ? | cftc_cot.py |
| `CFTC_CONTRACT` | `"BITCOIN - CHICAGO MERCANTILE EXCHANGE"` | — | str | cftc_cot.py |
| `WORLD_BANK_REFRESH_SEC` | `86400` | — | int | world_bank.py |
| `WORLD_BANK_COUNTRIES` | `["USA", "EUU", "CHN", "JPN", "GBR"]` | — | list | world_bank.py |
| `WORLD_BANK_INDICATORS` | `{ 3 keys }` | — | dict | world_bank.py |
| `IMF_REFRESH_SEC` | `86400` | — | int | imf.py |
| `IMF_COUNTRIES` | `["USA", "EUR", "CHN", "JPN", "GBR"]` | — | list | imf.py |
| `IMF_INDICATORS` | `{ 3 keys }` | — | dict | imf.py |
| `ECB_REFRESH_SEC` | `3600` | — | int | ecb.py |
| `ECB_SERIES` | `[ 3 items ]` | — | list | ecb.py |
| `US_TREASURY_REFRESH_SEC` | `3600` | — | int | us_treasury.py |
| `BTC_FUTURES_FSYM` | `"BTC"` | — | str | **UNUSED** |

### Macro modifiers (legacy direct from data_sources)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `MACRO_RISK_OFF_PENALTY` | `-5` | -10 to -2 | int | **UNUSED** |
| `MACRO_CRISIS_PENALTY` | `-20` | -25 to -15 | int | **UNUSED** |
| `MACRO_DXY_STRONG_LONG_PENALTY` | `-5` | -10 to -2 | int | **UNUSED** |
| `MACRO_YIELD_INVERTED_PENALTY` | `-3` | -6 to -1 | int | **UNUSED** |

### Macro regime monitor (macro/)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `MACRO_REFRESH_INTERVAL_SEC` | `300` | 60-600 | int | monitor.py |
| `MACRO_PRE_EVENT_PAUSE_MINUTES` | `30` | 15-60 | int | bot.py |
| `MACRO_CONFIDENCE_STALE_HOURS` | `4` | 1-12 | int | monitor.py |
| `MACRO_RATE_LOOKBACK_DAYS` | `90` | 30-180 (rate-env change window) | int | monitor.py |
| `MACRO_DXY_STRONG` | `104.0` | 102-106 | float | monitor.py |
| `MACRO_DXY_WEAK` | `99.0` | 97-101 | float | monitor.py |
| `MACRO_VIX_CALM` | `15.0` | 12-18 | float | dashboard.py, monitor.py |
| `MACRO_VIX_ELEVATED` | `25.0` | 20-30 | float | dashboard.py, monitor.py |
| `MACRO_VIX_CRISIS` | `35.0` | 30-40 | float | dashboard.py, monitor.py, test_macro.py |
| `MACRO_YIELD_CURVE_INVERSION` | `0.0` | -0.5 to 0.5  (10y - 2y) | float | monitor.py |
| `MACRO_RATE_CHANGE_TIGHTENING` | `0.25` | 0.1-0.5      (fed funds Δ%) | float | monitor.py |
| `MACRO_RATE_CHANGE_EASING` | `-0.25` | -0.5 to -0.1 | float | monitor.py |
| `MACRO_INFLATION_LOW` | `2.5` | 1.5-3.5 | float | monitor.py |
| `MACRO_INFLATION_HIGH` | `4.0` | 3.0-6.0 | float | monitor.py |
| `MACRO_WEIGHT_VIX` | `0.30` | 0.2-0.4 | float | monitor.py |
| `MACRO_WEIGHT_DOLLAR` | `0.25` | 0.15-0.35 | float | monitor.py |
| `MACRO_WEIGHT_YIELD_CURVE` | `0.20` | 0.1-0.3 | float | monitor.py |
| `MACRO_WEIGHT_RATE_ENV` | `0.15` | 0.1-0.2 | float | monitor.py |
| `MACRO_WEIGHT_INFLATION` | `0.10` | 0.05-0.15 | float | monitor.py |

### Bot loop timing

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `BOT_LOOP_INTERVAL_SEC` | `30` | 10–120 | int | bot.py |
| `HEARTBEAT_INTERVAL_SEC` | `300` | 60–600 | int | bot.py |
| `POSITION_WATCHER_INTERVAL_SEC` | `5` | 2–15 | int | bot.py |
| `FUTURE_PRICE_TRACKER_INTERVAL_SEC` | `3600` | 1800–7200 | int | bot.py |
| `SELF_REVIEW_EVERY_N_TRADES` | `5` | 3–10 | int | bot.py |

### Logging

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `LOG_LEVEL` | `"INFO"` | — | str | logger.py |
| `LOG_TO_FILE` | `True` | — | bool | logger.py |
| `LOG_ROTATION` | `"midnight"` | — | str | **UNUSED** |

### UI / web control panel

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `UI_REFRESH_RATE` | `1.0` | — | float | **UNUSED** |
| `UI_MAX_TRADE_LOG_ROWS` | `20` | — | int | **UNUSED** |
| `UI_SHOW_CLAUDE_REASONING` | `True` | — | bool | **UNUSED** |
| `UI_SHOW_REGIME_DETAILS` | `True` | — | bool | **UNUSED** |
| `UI_SHOW_OFI_BARS` | `True` | — | bool | **UNUSED** |
| `UI_SHOW_HURST_BARS` | `True` | — | bool | **UNUSED** |
| `UI_DEFAULT_TAB` | `"overview"` | — | str | **UNUSED** |
| `WEB_UI_HOST` | `"localhost"` | "0.0.0.0" for LAN access | str | web_server.py |
| `WEB_UI_PORT` | `8765` | any open port | int | test_web_server.py, web_server.py |
| `WEB_UI_ENABLED` | `False` | — | bool | main.py |
| `WEB_UI_PUSH_INTERVAL_S` | `0.5` | 0.25-2.0  (WebSocket push rate, seconds) | float | queries.py, test_web_server.py, web_server.py |
| `WEB_UI_SCALP_FEED_HISTORY` | `30` | 10, 20, 30, 50 | int | test_web_server.py, web_server.py |

### Dashboard colour ladders

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `DASHBOARD_EXEC_RATE_GREEN_PCT` | `50.0` | 30–70 | float | dashboard.py |
| `DASHBOARD_EXEC_RATE_AMBER_PCT` | `20.0` | 10–40 | float | dashboard.py |
| `DASHBOARD_BALANCE_MISS_AMBER` | `1` | 1–3 | int | dashboard.py, test_dashboard.py |
| `DASHBOARD_BALANCE_MISS_RED` | `6` | 3–10 | int | dashboard.py, test_dashboard.py |

## Dead config

90 module-level constants in `config/settings.py` are not referenced anywhere outside the file (greped across all `.py`, excluding `venv/.git/audit/logs/backups/data/htmlcov/__pycache__`):

- `BASE_DIR` (line 23)
- `DATA_DIR` (line 24)
- `PER_TRADE_SHOW_FULL_REASONING` (line 45)
- `PER_TRADE_AUTO_EXPIRE_SECONDS` (line 46)
- `WINDOW_BRIEF_INCLUDES` (line 50)
- `WINDOW_AUTO_RENEW` (line 58)
- `WINDOW_PAUSE_ON_CIRCUIT_BREAKER` (line 59)
- `WINDOW_REAPPROVE_ON_REGIME_SHIFT` (line 61)
- `AUTO_NOTIFY_ON_ENTRY` (line 67)
- `AUTO_NOTIFY_ON_EXIT` (line 68)
- `AUTO_PAUSE_ON_LOSS_STREAK` (line 69)
- `AUTO_SUMMARY_INTERVAL_MIN` (line 70)
- `SESSION_MIN_SENTIMENT_SCORE` (line 75)
- `SESSION_BLOCK_CHOPPY_REGIME` (line 76)
- `SESSION_BLOCK_EXTREME_FEAR` (line 77)
- `SESSION_BLOCK_EXTREME_GREED` (line 78)
- `SESSION_REQUIRE_CLEAR_NEWS` (line 79)
- `SESSION_MIN_ACTIVE_PAIRS` (line 80)
- `MIN_LIQUIDITY_USD` (line 119)
- `PAIR_MIN_MARKET_CAP` (line 132)
- `ADX_GRID_RESET` (line 171)
- `HURST_RANDOM_ZONE` (line 176)
- `ATR_LOW_VOL_PERCENTILE` (line 179)
- `BB_WIDTH_EXPANDING_FACTOR` (line 182)
- `BB_REVERSION_ENTRY` (line 212)
- `VOLUME_SURGE_MULTIPLIER` (line 218)
- `ARB_MAX_TRANSFER_SECONDS` (line 231)
- `ARB_MIN_LIQUIDITY_MULT` (line 232)
- `MOMENTUM_REQUIRE_LARGE_CAP` (line 330)
- `REVERSION_BB_THRESHOLD` (line 340)
- `SWEEP_MIN_WICK_PCT` (line 350)
- `SWEEP_OFI_FLIP_REQUIRED` (line 351)
- `SWEEP_VOLUME_SPIKE_MULT` (line 352)
- `SWEEP_REVERSAL_CANDLES` (line 353)
- `SWEEP_KEY_LEVEL_LOOKBACK` (line 354)
- `GRID_SPACING_PCT` (line 361)
- `GRID_LEVELS_EACH_SIDE` (line 362)
- `GRID_ORDER_SIZE_PCT` (line 363)
- `GRID_AUTO_RESET` (line 364)
- `GRID_RESET_COOLDOWN_MIN` (line 365)
- `SENTIMENT_ENABLED` (line 371)
- `SENTIMENT_UPDATE_INTERVAL_SECONDS` (line 372)
- `SENTIMENT_LOOKBACK_HOURS` (line 373)
- `SENTIMENT_WEIGHTS` (line 375)
- `SENTIMENT_VELOCITY_WINDOW` (line 387)
- `SENTIMENT_VELOCITY_BOOST` (line 388)
- `REDDIT_POST_LIMIT` (line 394)
- `NEWS_FEEDS` (line 400)
- `MACRO_HARD_BLOCK_SKIP_REASON` (line 441)
- `PRE_EVENT_PAUSE_SKIP_REASON` (line 442)
- `CORR_LOOKBACK_HOURS` (line 452)
- `MAX_OPEN_POSITIONS` (line 475)
- `TRAILING_STOP_ENABLED` (line 480)
- `TRAILING_STOP_PCT` (line 481)
- `TRAILING_STOP_ACTIVATE` (line 482)
- `CIRCUIT_BREAKER_PAUSE_MINUTES` (line 518)
- `CIRCUIT_BREAKER_HALT_REQUIRES_MANUAL` (line 519)
- `ORDER_TYPE` (line 534)
- `LIMIT_SLIPPAGE_PCT` (line 535)
- `ORDER_TIMEOUT_SECONDS` (line 536)
- `ORDER_RETRY_ON_FAIL` (line 537)
- `ORDER_RETRY_COUNT` (line 538)
- `CLAUDE_WINDOW_BRIEF` (line 563)
- `CLAUDE_MAX_DAILY_COST_USD` (line 576)
- `PREDICTIVE_ENABLED` (line 582)
- `PREDICTIVE_MIN_TRAINING_TRADES` (line 583)
- `PREDICTIVE_WIN_PROB_THRESHOLD` (line 584)
- `PREDICTIVE_RETRAIN_EVERY_N_DAYS` (line 585)
- `PREDICTIVE_MODEL_PATH` (line 586)
- `PREDICTIVE_BOOST_STRONG` (line 587)
- `PREDICTIVE_PENALTY_WEAK` (line 588)
- `STABLECOIN_BENCHMARK_APR_PCT` (line 703)
- `FUNDING_DELTA_TOLERANCE_USD` (line 704)
- `XCHAIN_RPC_ENV_VARS` (line 1006)
- `KILL_SWITCH_CONFIRM_REQUIRED` (line 1021)
- `SENTIMENT_NEWS_GUARD_ENABLED` (line 1033)
- `COINGLASS_REFERENCE_EXCHANGE` (line 1056)
- `BTC_FUTURES_FSYM` (line 1172)
- `MACRO_RISK_OFF_PENALTY` (line 1177)
- `MACRO_CRISIS_PENALTY` (line 1178)
- `MACRO_DXY_STRONG_LONG_PENALTY` (line 1179)
- `MACRO_YIELD_INVERTED_PENALTY` (line 1180)
- `LOG_ROTATION` (line 1234)
- `UI_REFRESH_RATE` (line 1236)
- `UI_MAX_TRADE_LOG_ROWS` (line 1237)
- `UI_SHOW_CLAUDE_REASONING` (line 1238)
- `UI_SHOW_REGIME_DETAILS` (line 1239)
- `UI_SHOW_OFI_BARS` (line 1240)
- `UI_SHOW_HURST_BARS` (line 1241)
- `UI_DEFAULT_TAB` (line 1242)

## Suspected hardcoded numbers in source

Numbers that look like thresholds, multipliers, or fallbacks but bypass `settings.py`. Per CLAUDE.md, every threshold should live in `settings.py` with a `# test:` annotation:

- `execution/arb_engine.py:113` — `fee_buy = fee_map.get(buy_ex, 0.002)`  (fallback fee (20 bps) not in settings; shadows ARB_FEE_ESTIMATE_PCT)
- `execution/arb_engine.py:114` — `fee_sell = fee_map.get(sell_ex, 0.002)`  (fallback fee (20 bps) not in settings)
- `execution/arb_engine.py:441` — `min(ask_liq, bid_liq) * 0.10`  (10% book-consume cap — magic; should mirror SCALP_MAX_TOP1_CONSUME_PCT pattern)
- `execution/arb_engine.py:517` — `fee_buy  = settings.ARB_FEE_MAP.get(opp.buy_exchange,  0.002)`  (fallback fee (20 bps) duplicated three times in the file)
- `execution/arb_engine.py:518` — `fee_sell = settings.ARB_FEE_MAP.get(opp.sell_exchange, 0.002)`  (fallback fee (20 bps))
- `execution/arb_engine.py:566` — `factor = min(factor, 10.0)`  (10× position-scaling cap — runaway-guard magic; no setting)
- `core/bot.py:741` — `if row.price_1h is None and age_min >= 60:`  (60/240/1440-min landmark thresholds for future-price tracker; should be a settings list)
- `core/bot.py:743` — `if row.price_4h is None and age_min >= 240:`  (sibling of line 741)
- `core/bot.py:745` — `if row.price_24h is None and age_min >= 1440:`  (sibling of line 741)
- `core/bot.py:853` — `composite_0_100 = 50.0 + (data.composite_score / 2.0)`  (−100..+100 → 0..100 rescale magic; should be a helper constant pair)
- `agents/scalping_agent.py:316` — `return float(fees["taker_bps"]) * 2.0`  (×2 to convert per-leg taker bps → round-trip; no setting)
- `agents/scalping_agent.py:422` — `if len(buckets) >= 10:`  (10-bucket warmup minimum (z-score requires N samples) — should be a setting)
- `agents/scalping_agent.py:506` — `elif z >= 0.8:`  ("moderate" direction band ±0.8 — bypasses SCALP_OFI_Z_ENTRY family)
- `agents/scalping_agent.py:510` — `elif z <= -0.8:`  (mirror of 506)
- `agents/scalping_agent.py:527` — `stale = age > 30.0`  (30-second bucket staleness gate; not a setting)
- `agents/scalping_agent.py:1128` — `if age >= 30 and obs.price_30s == 0.0:`  (30/60/180/300-second landmark thresholds in micro tracker; no setting)
- `agents/balance_agent.py:414` — `old: list, new: list, tol: float = 0.10,`  (10% drift tolerance default; should reference BALANCE_STRUCTURAL_DRIFT_HINT or its own setting)
- `agents/balance_agent.py:507` — `if equity > 0 and plan_sum > equity * 1.05:`  (5% rail-1 over-allocation guard; no setting)
- `signals/quality_gate.py:89` — `if session_mod < -10:`  (−10 "dead zone" trip-wire (compare to SESSION_WINDOWS["dead_zone"].score_boost = −15))
- `signals/quality_gate.py:95` — `if guard_penalty <= -999:`  (guard hard-block sentinel; NEWS_GUARD_PENALTY_BLOCK = 999 exists in settings but the comparison is hand-coded)

## keys.env env vars

Discovered via `Grep "os\.getenv\(|os\.environ\["` across the repo (`venv/.git/audit/logs/backups` excluded). Per-source `api_key_env_var` and `rpc_env_var` class attributes were also resolved.

### Anthropic

| Env var | Example read |
|---------|--------------|
| `ANTHROPIC_API_KEY` | `core/agent.py:21` |

### Exchange — Binance

| Env var | Example read |
|---------|--------------|
| `BINANCE_API_KEY` | `core/market_data.py:42` |
| `BINANCE_SECRET` | `core/market_data.py:42` |

### Exchange — Kraken

| Env var | Example read |
|---------|--------------|
| `KRAKEN_API_KEY` | `core/market_data.py:43` |
| `KRAKEN_SECRET` | `core/market_data.py:43` |

### Exchange — Bybit

| Env var | Example read |
|---------|--------------|
| `BYBIT_API_KEY` | `core/market_data.py:44` |
| `BYBIT_SECRET` | `core/market_data.py:44` |

### Exchange — OKX

| Env var | Example read |
|---------|--------------|
| `OKX_API_KEY` | `core/market_data.py:45` |
| `OKX_SECRET` | `core/market_data.py:45` |
| `OKX_PASSPHRASE` | `core/market_data.py:45` |

### Exchange — MEXC (×4 keys)

| Env var | Example read |
|---------|--------------|
| `MEXC_KEY_{1..4}_API_KEY` | `execution/mexc_key_router.py:107,134` |
| `MEXC_KEY_{1..4}_SECRET` | `execution/mexc_key_router.py:108,135` |

### Exchange — generic

| Env var | Example read |
|---------|--------------|
| `<EXCHANGE>_API_KEY (uppercased from settings.EXCHANGES)` | `execution/arb_engine.py:791, agents/__init__.py:88,261` |
| `<EXCHANGE>_SECRET` | `execution/arb_engine.py:792, agents/__init__.py:261` |

### Sentiment — Reddit

| Env var | Example read |
|---------|--------------|
| `REDDIT_CLIENT_ID` | `sentiment/sources/reddit.py:64,98` |
| `REDDIT_CLIENT_SECRET` | `sentiment/sources/reddit.py:99` |
| `REDDIT_USER_AGENT` | `sentiment/sources/reddit.py:100` |

### Sentiment — CryptoPanic

| Env var | Example read |
|---------|--------------|
| `CRYPTOPANIC_API_KEY` | `sentiment/sources/cryptopanic.py:113` |

### Macro — FRED

| Env var | Example read |
|---------|--------------|
| `FRED_API_KEY` | `macro/sources/fred_calendar.py:73,79; data_sources/sources/fred.py:57,68` |

### Macro — Alpha Vantage

| Env var | Example read |
|---------|--------------|
| `ALPHA_VANTAGE_API_KEY` | `data_sources/sources/alpha_vantage.py:41,60` |

### Macro — CoinGecko

| Env var | Example read |
|---------|--------------|
| `COINGECKO_API_KEY` | `data_sources/sources/coingecko.py:42,117` |

### Macro — CryptoCompare

| Env var | Example read |
|---------|--------------|
| `CRYPTOCOMPARE_API_KEY` | `data_sources/sources/cryptocompare.py:37,48` |

### Macro — Trading Economics calendar (stub)

| Env var | Example read |
|---------|--------------|
| `TRADING_ECONOMICS_API_KEY` | `macro/sources/stub_calendar.py:19` |

### Cross-chain RPC

| Env var | Example read |
|---------|--------------|
| `ARBITRUM_RPC_URL` | `execution/chains/arbitrum.py:84,123 (registered in settings.XCHAIN_RPC_ENV_VARS)` |
| `BASE_RPC_URL` | `execution/chains/base_chain.py:26` |
| `OPTIMISM_RPC_URL` | `execution/chains/optimism.py:21; execution/chains/_solidly_volatile.py:108` |


---

## Database

The following two artefacts are inlined: the per-table ORM + query audit (`module_reports/database.md`) and the live SQLite snapshot summary (`db_schema.md`). The full live `.schema` and row counts are in `sqlite_state.txt`.

# Database audit — CryptoBot 1.0

This is the consolidated database audit. The ORM-level documentation (models, columns, indexes, queries, table → write/read maps, TODOs) lives in `module_reports/database.md` (820 lines) and is **the source of truth for the schema-from-code view**. This file complements it with the **live snapshot** from `data/cryptobot.db` and a few schema-comparison observations.

## Live SQLite state

Snapshot taken from `data/cryptobot.db` via read-only `.schema` / `COUNT(*)` queries (no row contents were read). Full output: `sqlite_state.txt` (546 lines).

### Tables present in the live DB

```
agent_events
arb_opportunities
arb_trades
calendar_events
candles
capital_movements
circuit_breaker_log
daily_stats
data_log
fund_capital_efficiency
funding_arb_observations
funding_arb_trades
macro_log
portfolio_snapshots
predictions
scalp_observations
sentiment
sentiment_log
signals
trades
xchain_observations
```

21 tables — matches the 21 ORM models documented in `module_reports/database.md`.

### Row counts at snapshot time

| Table | Rows |
|---|---:|
| agent_events | 125 |
| arb_opportunities | 0 |
| arb_trades | 290 |
| calendar_events | 11 |
| candles | 0 |
| capital_movements | 31 |
| circuit_breaker_log | 317 |
| daily_stats | 0 |
| data_log | 46,923 |
| fund_capital_efficiency | 945 |
| funding_arb_observations | 110 |
| funding_arb_trades | 0 |
| macro_log | 66 |
| portfolio_snapshots | 563 |
| predictions | 0 |
| scalp_observations | 214,570 |
| sentiment | 0 |
| sentiment_log | 228 |
| signals | 120 |
| trades | 110 |
| xchain_observations | 7,374 |

Observations:

- **`scalp_observations` dominates the DB at 214,570 rows** — the scalp agent's "observe and skip" log is by far the largest write source.
- **`data_log` is the second-largest at 46,923 rows** — every data-source refresh persists a row.
- **`predictions` is empty (0 rows).** The ORM model and FK relationship from `signals.id` exist, but no training run has populated it. This matches the empty `predictive/` package.
- **`candles` is empty (0 rows).** The model exists but nothing in the live system is persisting OHLCV candles to the DB at present.
- **`sentiment` (the older aggregated table) is empty (0 rows).** All sentiment writes flow into `sentiment_log` (228 rows) — see `database.md` "Known issues" for the deprecation gap.
- **`daily_stats` is empty (0 rows).** The aggregation table is defined but no aggregator is running against it.
- **`arb_opportunities` is empty (0 rows)** while `arb_trades` has 290. The "opportunity log" path is also unwired in practice.
- **`funding_arb_trades` is empty (0 rows)** while `funding_arb_observations` has 110 — sim funding-arb has logged candidate observations but no fills.

### Indexes present in the live DB

See `sqlite_state.txt` for the full list. Notable ones:

```
ix_arb_opportunities_lookup    arb_opportunities
ix_arb_trades_pair             arb_trades
ix_arb_trades_timestamp        arb_trades
ix_calendar_events_lookup      calendar_events
ix_candles_lookup              candles
ix_capital_movements_lookup    capital_movements
ix_circuit_breaker_log_lookup  circuit_breaker_log
ix_daily_stats_date            daily_stats
ix_data_log_source_ts          data_log
ix_fund_capital_efficiency_*   fund_capital_efficiency
ix_funding_arb_observations_*  funding_arb_observations
ix_funding_arb_trades_*        funding_arb_trades
ix_macro_log_timestamp         macro_log
ix_portfolio_snapshots_ts      portfolio_snapshots
ix_predictions_signal_id       predictions
ix_scalp_observations_lookup   scalp_observations
ix_sentiment_log_lookup        sentiment_log
ix_sentiment_lookup            sentiment
ix_signals_pair                signals
ix_signals_timestamp           signals
ix_signals_type                signals
ix_trades_pair                 trades
ix_trades_timestamp            trades
ix_xchain_obs_lookup           xchain_observations
ix_xchain_observations_symbol  xchain_observations
ix_xchain_observations_timestamp xchain_observations
ix_xchain_observations_would_entry xchain_observations
```

`scalp_observations` only has one composite index (`ix_scalp_observations_lookup`) despite holding 214k rows. With more growth this becomes a candidate for additional indexing — see `module_reports/database.md` for the canonical list of fields.

## Migrations

There is **no `alembic/` directory in the repo**. `requirements.txt` pins `alembic==1.13.1` and `pip freeze` shows `alembic==1.18.4` installed, but no migration tree has been bootstrapped.

The migration story is instead:

1. `database.db.init_db()` calls `Base.metadata.create_all()` — idempotent, but **never alters existing tables**.
2. `scripts/migrate_scalp_v2.py` — a hand-rolled idempotent SQLite migration that `ALTER TABLE`s `scalp_observations` to add the v2 columns (`confluence_score`, `strength_label`, `cross_exchange_agrees`, `btc_compatible`, `adverse_selection_ok`, `depth_ok`, `vwap_aligned`, `htf_aligned`, `volume_adequate`, `atr_bps`, `atr_adjusted`, `sl_clamped`, `rr_actual`). Five other TODOs in `database/models.py` flag similar gaps that have not yet been scripted.

`module_reports/database.md` covers this in detail under "Migrations" and "Known issues".

## Cross-reference

For the full per-table column inventory, every relationship, every `queries.py` function signature with its read/write classification, and the table → write/read function map, read `module_reports/database.md`.
---

## Tests at snapshot time

- **Result:** 552 passed, 1 warning, 0 failures, ≈12.12s wallclock.
- **Test files:** 30 (under `tests/`).
- **Configuration:** `pytest.ini` scopes to `tests/` only (excludes `scalping_v2/test_scalping_v2.py` to avoid duplicate-basename collision with `tests/test_scalping_v2.py`).
- **Warning:** `tests/test_chain_connectors.py::test_is_available_false_without_rpc_env_var` emits one `DeprecationWarning` from `websockets.legacy` (transitive via `web3 → websockets`).

### Per-test-file summary

| Test file | Notes |
|-----------|-------|
| `tests/test_arb_engine.py` | ArbEngine fills, gas breakeven, slippage, capped notional, circuit breaker logging. |
| `tests/test_balance_agent.py` | BalanceAgent loop, planner integration, rebalance arming/confirm/expire/cancel. |
| `tests/test_bot.py` | CryptoBot construction with injected mocks (router, kill_switch, db_queries); approval-mode dispatch; CB logging path. |
| `tests/test_chain_connectors.py` | Each connector's `is_available()` env gating; one warning here. |
| `tests/test_coordinator.py` | Coordinator agent registration, start/stop, monitor loop. |
| `tests/test_crosschain_agent.py` | CrossChainArbAgent loop + capital allocation. |
| `tests/test_crosschain_engine.py` | Engine candidate scoring, inventory, CB logging. |
| `tests/test_dashboard.py` | Dashboard snapshot construction; mocked coordinator state. |
| `tests/test_data_sources.py` | Plugin discovery via `REGISTERED_SOURCES`, subscription dispatch, refresh-loop semantics. |
| `tests/test_equity_reconstruction.py` | Bankroll = starting + realised PnL; UTC-midnight handling. |
| `tests/test_funding_arb.py` | FundingArbAgent loop; Phase 1 observation-mode invariants. |
| `tests/test_funds.py` | Capital movement accounting. |
| `tests/test_inventory_targets.py` | `execution.inventory.compute_inventory_targets` math. |
| `tests/test_macro.py` | MacroMonitor regime classification + calendar event awareness. |
| `tests/test_market_data_stream.py` | MarketData order-book stream lifecycle + error backoff. |
| `tests/test_mexc_key_router.py` | Per-key MEXC routing decisions. |
| `tests/test_prompts.py` | ApprovalInputHandler keypress mapping (mocked Bot). |
| `tests/test_quality_gate.py` | QualityGate composite scoring; modifier addition order. |
| `tests/test_queries.py` | Read/write query helpers; FK & WAL invariants; kill_switch + position_manager writers. |
| `tests/test_scalp_activation.py` | Scalp-agent gating by strategy/exchange. |
| `tests/test_scalp_pair_coverage.py` | Pair-coverage logic for `STRATEGY_EXCHANGE_MAP`. |
| `tests/test_scalp_v2_accessors.py` | v2 accessor helpers (`evaluate_signal_v2` / `is_ready_for_live_v2`). |
| `tests/test_scalp_v2_integration.py` | Confluence + ATR-SL gates wired into the agent flow. |
| `tests/test_scalping_agent.py` | Scalping agent loop & queue semantics. |
| `tests/test_scalping_v2.py` | v2 integration tests (the in-tree copy; the `scalping_v2/test_scalping_v2.py` standalone copy is excluded by `pytest.ini`). |
| `tests/test_sentiment.py` | Aggregator composite math + source weight ladder. |
| `tests/test_signal_arbitrage.py` | Arb scanner score + ranking. |
| `tests/test_web_server.py` | Web routes, action dispatch, snapshot construction, rebalance arm/confirm flow, scalp closed-trades persistence. |

No tests fail. The full raw output is in `Appendix B`.

### Coverage gaps observed during the audit (from module reports)

These were noted while writing the per-package reports; they don't fail the suite but they're worth recording:

- `strategies/*` — zero direct unit tests; only consumed via mocked `SimpleNamespace` in `test_bot.py` / `test_dashboard.py`.
- `main.py` — no test exercises startup or the SIGINT shutdown path (the very bug recorded in `MEMORY.md → project_shutdown_hang` that the current `main.py:135-141` rework fixes).
- `predictive/` — empty package; no tests because no code.
- `core/agent.py` Claude failure path — `module_reports/core.md` flags an "agent fail-open" behaviour (no signal.indicators["claude_rec"] on exception → autonomous/window still executes) that isn't covered by a test.
- `signals/quality_gate.py:104-105` double-counting OFI when scanners already folded it into `sentiment_mod` — flagged in `module_reports/signals.md` but not under test.

---

## Documentation map

Reproduced from `docs_inventory.md` (Phase 6).

---

# Docs inventory — CryptoBot 1.0

All `.md` files in the repo (excluding `venv/`, `.git/`, `audit/`, `.pytest_cache/`). No `.docx` files are tracked.

| File | Lines | Last modified (UTC) | Purpose | Referenced from |
|------|------:|--------------------|---------|-----------------|
| `CLAUDE.md` | 74 | 2026-05-20 09:21 | Guidance for Claude Code: commands, architecture invariants, DB conventions, agent loop notes, repo-state caveat. | Loaded into every Claude Code session by the harness (project-instructions). Not linked from other repo docs. |
| `GLOSSARY.md` | 105 | 2026-05-25 21:21 | Project-specific vocabulary — "lookup before grepping". Covers bot architecture, signals, scalping, sentiment, macro. | `README.md` |
| `PLUGIN_PATTERN.md` | 55 | 2026-05-22 12:48 | Documents the one repeating pattern: abstract base + registry list + discovery layer. | `README.md`, `WEB_UI.md` |
| `README.md` | 150 | 2026-05-22 12:49 | Top-level overview: philosophy, modes, quick start, repo tour. | Cross-links to `GLOSSARY.md`, `PLUGIN_PATTERN.md`, `RUNBOOK.md`. |
| `RUNBOOK.md` | 138 | 2026-05-25 21:20 | Operational playbook — one section per agent/feature with activation checklist + commands. Contains a `### Rollback` subsection covering scalp v2 inline. | `README.md`. References `scalping_v2/SCALPING_V2.md` and `scalping_v2/SCALPING_V2_RECALIBRATION.md`. |
| `WEB_UI.md` | 431 | 2026-05-28 21:56 | Browser control panel spec — aiohttp WS + REST, push-only state, localhost-only. Most recently modified doc; aligns with the active `feat/web-ui-v2-agent-panels` branch. | Cross-links to `PLUGIN_PATTERN.md`. |
| `scalping_v2/RUNBOOK_v2_rollback_section.md` | 125 | 2026-05-25 19:56 | Standalone "rollback section" intended to be appended to `RUNBOOK.md`. | **No incoming references.** The substance was already inlined into `RUNBOOK.md → Rollback`, so this file appears orphaned. |
| `scalping_v2/SCALPING_V2.md` | 229 | 2026-05-25 18:20 | v2 integration guide — what v2 adds on top of v1, six new components, activation steps. | `RUNBOOK.md` |
| `scalping_v2/SCALPING_V2_RECALIBRATION.md` | 475 | 2026-05-25 19:56 | Cookbook — SQL recipes for tuning v2 selectivity gates against accumulated `scalp_observations`. Executed via `scripts/run_recalibration.py`. | `RUNBOOK.md`, `scripts/run_recalibration.py` (reads the SQL blocks). |
| `scalping_v2/SCALPING_V3_ROADMAP.md` | 210 | 2026-05-25 19:56 | What was deliberately NOT built into v2, why deferred, trigger conditions for each item. | **No incoming references** from other docs. (Listed in `module_reports/scalping_v2.md`.) |

## Orphan docs (not referenced from any other doc)

- `scalping_v2/RUNBOOK_v2_rollback_section.md` — content already merged into `RUNBOOK.md`, but the file remains.
- `scalping_v2/SCALPING_V3_ROADMAP.md` — never linked. Critical for understanding deferred work; the master audit doc (Phase 11 → "What is deliberately NOT built") will treat this as its source.
- `CLAUDE.md` — not referenced from README/RUNBOOK/etc., but is consumed by the Claude Code harness directly.

## Broken references

- `scalping_v2/SCALPING_V2.md` references `scalping_agent_doc.docx` (line 2: *"Companion to scalping_agent_doc.docx"*) but no `.docx` exists anywhere in the repo. The referenced design doc was either external (Google Doc / shared drive) or has been removed.

## Doc → code traceability

| Doc | Code module(s) it documents | Stays in sync? |
|-----|------------------------------|----------------|
| `CLAUDE.md` | All packages (high-level invariants) | Notes a "Repo state caveat" — README describes a fuller tree than currently exists; some `predictive/`, `sentiment/`, `ui/`, `signals/` modules listed are empty stubs. |
| `README.md` | All packages | Same caveat. |
| `RUNBOOK.md` | `agents/scalping_agent.py`, `agents/funding_arb_agent.py`, `agents/crosschain_agent.py`, `agents/balance_agent.py`, `execution/arb_engine.py`, `core/bot.py`, `ui/`. | Largely in sync; the rollback subsection (line 84-ish) matches `scalping_v2/RUNBOOK_v2_rollback_section.md` substance. |
| `WEB_UI.md` | `ui/web_server.py`, `ui/web_dashboard.html` | Most recently touched doc — actively aligned with the current `feat/web-ui-v2-agent-panels` work-in-progress branch. Some routes documented in WEB_UI.md may run ahead of code; cross-check against `module_reports/ui.md`. |
| `PLUGIN_PATTERN.md` | `sentiment/`, `data_sources/`, `macro/sources/`, `agents/`, `execution/chains/`, `strategies/` | Reasonably general. The pattern is implemented in 6 places (Phase 9 lists them). |
| `GLOSSARY.md` | All | Free-form lookup; not a code spec. |
| `scalping_v2/SCALPING_V2.md` | `agents/scalping_*.py`, `scalping_v2/*.py` | Documents v2; the production-path files in `agents/` are byte-identical to the bundle (per `module_reports/scalping_v2.md` duplication audit). |
| `scalping_v2/SCALPING_V2_RECALIBRATION.md` | `scripts/run_recalibration.py` consumes its SQL blocks; targets `scalp_observations` schema | `scripts/run_recalibration.py` exists explicitly to verify these queries stay valid as the schema migrates. |
| `scalping_v2/SCALPING_V3_ROADMAP.md` | None — explicit deferred-work catalog. | n/a |
| `scalping_v2/RUNBOOK_v2_rollback_section.md` | n/a — substance already in `RUNBOOK.md`. | Drift risk: two copies of "how to roll back v2" can diverge silently. |

---

## Build prompts inventory

# Build prompts inventory — CryptoBot 1.0

25 files in `prompts/`. Each is a markdown brief used to drive a Claude Code build session. Mapping below shows which packages each prompt targets and whether those modules exist in the current codebase.

## Executed and shipped

| Prompt | Lines | mtime | Target module(s) | Status |
|--------|------:|-------|------------------|--------|
| `build_bot_loop.md` | 81 | 2026-05-20 10:03 | `core/`, `signals/`, `execution/`, `database/`, `ui/` | **shipped** — every target module exists; `core/bot.py` is the result. |
| `build_arb_engine.md` | 312 | 2026-05-20 11:54 | `execution/arb_engine.py`, `agents/__init__.py`, `agents/base.py` | **shipped** — arb engine present (533 LOC) with full ArbEngine class. |
| `build_coordinator.md` | 302 | 2026-05-20 11:37 | `agents/coordinator.py` | **shipped** — Coordinator owns every agent; main.py uses it. |
| `build_dashboard.md` | 223 | 2026-05-20 10:22 | `ui/dashboard.py` | **shipped** — 2007 LOC Rich dashboard. |
| `build_sentiment.md` | 342 | 2026-05-20 10:36 | `sentiment/` | **shipped** — 5 sources registered (fear_greed, cryptopanic, reddit, google_trends, telegram). |
| `build_data_sources.md` | 649 | 2026-05-21 10:58 | `data_sources/` | **shipped** — 13 sources registered. |
| `build_macro.md` | 420 | 2026-05-21 12:35 | `macro/` | **shipped** — MacroMonitor + 2 calendar sources present. |
| `build_wiring.md` | 305 | 2026-05-21 13:23 | Multi-package — wires data_sources/macro into the bot. | **shipped** — but note `module_reports/sentiment.md` flags `prompts/build_wiring.md:72` references a non-existent `sentiment_aggregator` symbol (actual export is `sentiment`). Doc-drift. |
| `build_fixes.md` | 273 | 2026-05-22 00:50 | Bug-fix sweep across packages | **shipped** — referenced fixes appear in core/signals/agents. |
| `build_scalping_agent.md` | 1391 | 2026-05-22 12:21 | `agents/scalping_agent.py` (+ supporting files) | **shipped** — ScalpingAgent + ATR-SL + Confluence + v2 integration. |
| `build_arb_improvements.md` | 169 | 2026-05-23 10:46 | `execution/arb_engine.py` improvements | **shipped** — enhancements visible in arb_engine.py (gas breakeven, slippage, capped notional). |
| `build_dashboard_arb_panels.md` | 117 | 2026-05-23 11:40 | `ui/dashboard.py` arb panels | **shipped** — arb panels present in dashboard. |
| `build_data_sources_coinglass.md` | 208 | 2026-05-23 11:40 | `data_sources/sources/coinglass.py` | **shipped** — file present, registered. |
| `build_web_ui.md` | 499 | 2026-05-25 13:51 | `ui/web_server.py` + `ui/web_dashboard.html` | **shipped** — both files present; 11 routes audited in `module_reports/ui.md`. |
| `build_web_ui_refresh_v1.md` | 581 | 2026-05-27 12:50 | UI refresh v1 | **shipped** — visible in current `ui/web_dashboard.html`. |
| `build_funding_arb.md` | 252 | 2026-05-28 14:51 | `agents/funding_arb_agent.py`, `execution/funding_engine.py` | **shipped** — both files present; 110 observations logged. |
| `fix_pre_soak.md` | 146 | 2026-05-28 15:32 | "4 items" pre-soak corrections (likely small surface across multiple packages) | **shipped** — fixes appear merged; soak preparation milestone. |
| `fix_deisland_engines.md` | 107 | 2026-05-28 16:49 | "De-island FundingRateArbEngine & CrossChainArbEngine" — re-integrate isolated engines | **shipped** — both engines present and integrated via their agents. |
| `build_crosschain_agent_v2.md` | 259 | 2026-05-28 11:50 | `agents/crosschain_agent.py` v2 | **shipped** — CrossChainArbAgent present, gated on RPC env vars. |
| `balance_agent.md` | 279 | 2026-05-28 12:26 | `agents/balance_agent.py` + `agents/balance/` subpackages | **shipped** — BalanceAgent present with full balance/ subtree (policy/, rails/, planner, inventory_state). |
| `build_web_ui_v2_agent_panels.md` | 554 | 2026-05-28 20:05 | `ui/web_server.py` + `ui/web_dashboard.html` v2 panels | **shipped (on branch)** — current branch `feat/web-ui-v2-agent-panels` is the active delivery. |

## Executed and partially shipped

| Prompt | Lines | mtime | Target module(s) | Status |
|--------|------:|-------|------------------|--------|
| `build_web_ui_v3_agent_pages.md` | 270 | 2026-05-28 21:47 | `ui/web_dashboard.html`, `ui/web_server.py` — per-agent pages + dashboard compaction | **partial** — `WEB_UI.md` documents v3-style routes (`/api/agent/{id}`, `/api/session/{name}`) and several `test_api_agent_*` / `test_api_session_*` tests pass, so v3 surface is at least partly in place on the current branch. |

## Not yet executed

| Prompt | Lines | mtime | Target module(s) | Notes |
|--------|------:|-------|------------------|-------|
| `build_balance_agent.md` | **0** | 2026-05-28 11:46 | n/a | **Empty file.** Superseded the same day by the 279-line `balance_agent.md`. Should be deleted. |
| `5kfund.md` | 154 | 2026-05-29 10:45 | Funds/exchanges/treasury planning prompt — references `OPERATIONS.md` and `SOAK_CRITERIA.md` (neither exists in the repo) | **not executed** — operational planning brief; no target module to ship. Most recent prompt mtime aside from this audit prompt. |
| `cryptobot_audit.md` | 244 | 2026-05-29 11:53 | This audit itself (Phase 0–13) | **in progress** — currently being executed as `audit/cryptobot_1.0/`. |

## Cross-reference findings

- **`prompts/build_balance_agent.md` is empty** (0 bytes / 0 lines). Likely a stub created and abandoned when the larger `balance_agent.md` superseded it.
- **`prompts/5kfund.md` references `OPERATIONS.md` and `SOAK_CRITERIA.md`** in line 1. Neither file exists in the repo. These were either external operator notes or are deferred deliverables.
- **`prompts/build_wiring.md:72`** references a `sentiment_aggregator` import that doesn't exist (actual singleton name is `sentiment` per `sentiment/aggregator.py`). Documented in `module_reports/sentiment.md`.
- **Six prompts have been executed on the current branch (`feat/web-ui-v2-agent-panels`)** — the cluster of `build_web_ui_*` + `balance_agent.md` + `build_funding_arb.md` + `fix_*.md` between 2026-05-25 and 2026-05-28 maps to the recent commit history.
- **No prompt covers `predictive/`.** The package is an empty `__init__.py` and no `build_predictive.md` / `build_trainer.md` exists. CLAUDE.md mentions `python -m predictive.trainer` as if it existed.
- **No prompt covers `strategies/` directly.** The pattern was carried over from `build_bot_loop.md` and never had a dedicated brief.

## Summary

- **25** prompts total (24 with content + 1 empty)
- **22** shipped — production modules exist and match prompt intent
- **1** partial — v3 web UI work split with v2 across the active branch
- **3** not executed — empty (`build_balance_agent.md`), operational (`5kfund.md`), self-referential (`cryptobot_audit.md`)

---

## Dependencies

# Dependency audit — CryptoBot 1.0

## Environment

- **Python:** 3.11.15
- **venv path:** `~/cryptobot/venv` (Linux/WSL)
- **requirements.txt direct deps:** 33
- **pip freeze installed packages:** 99
- **Drifted versions (req vs installed):** 22
- **Missing from venv (req entry, no install):** 6
- **Incidental in venv (installed, not in req):** 72 (transitive + a few first-class additions)

## Direct dependencies — version comparison

| Package | requirements.txt | pip freeze | Drift |
|---------|------------------|------------|-------|
| `ccxt` | 4.5.54 | 4.5.54 | — |
| `protobuf` | 5.29.5 | 5.29.5 | — |
| `pandas-ta` | 0.3.14b | **MISSING** | — |
| `pandas` | 2.2.0 | 3.0.3 | major↑ |
| `numpy` | 1.26.4 | 2.4.6 | major↑ |
| `sqlalchemy` | 2.0.28 | 2.0.49 | minor |
| `alembic` | 1.13.1 | 1.18.4 | minor |
| `praw` | 7.7.1 | 7.8.1 | patch |
| `telethon` | 1.34.0 | 1.43.2 | minor |
| `feedparser` | 6.0.11 | 6.0.12 | patch |
| `pytrends` | 4.9.2 | 4.9.2 | — |
| `vaderSentiment` | 3.3.2 | 3.3.2 | — |
| `transformers` | 4.38.2 | **MISSING** | — |
| `torch` | 2.2.1 | **MISSING** | — |
| `anthropic` | 0.21.3 | 0.103.0 | **major↑ (≈80 minor releases)** |
| `web3` | 7.16.0 | 7.16.0 | — |
| `rich` | 13.7.1 | 15.0.0 | major↑ |
| `scikit-learn` | 1.4.1 | 1.8.0 | minor↑ |
| `xgboost` | 2.0.3 | 3.2.0 | major↑ |
| `joblib` | 1.3.3 | 1.5.3 | minor↑ |
| `python-dotenv` | 1.0.1 | 1.2.2 | minor↑ |
| `aiohttp` | 3.9.3 | 3.13.5 | minor↑ |
| `asyncio-throttle` | 1.0.2 | **MISSING** | — |
| `httpx` | 0.27.0 | 0.28.1 | minor↑ |
| `tenacity` | 8.2.3 | 9.1.4 | major↑ |
| `python-dateutil` | 2.9.0 | 2.9.0.post0 | post-release |
| `pytz` | 2024.1 | **MISSING** | — |
| `colorama` | 0.4.6 | **MISSING** | — |
| `click` | 8.1.7 | 8.4.0 | minor↑ |
| `pydantic` | 2.6.3 | 2.13.4 | minor↑ |
| `pytest` | 8.1.0 | 9.0.3 | major↑ |
| `pytest-asyncio` | 0.23.5 | 1.3.0 | major↑ |
| `pytest-mock` | 3.12.0 | 3.15.1 | minor↑ |

22 of 33 direct deps are drifted; 11 are at the version declared (or close enough — protobuf, ccxt, pytrends, vaderSentiment, web3 match exactly).

## Missing from venv but listed in requirements.txt — usage check

For each "missing" dep, grep the source for any import. If the dep is unused, the requirements.txt entry is stale.

| Dep | Imported anywhere in source? | Verdict |
|-----|------------------------------|---------|
| `pandas-ta` | **No** (`grep pandas_ta` returns 0 hits) | Stale requirements.txt entry. Actual TA library in use is `ta==0.11.0` (incidental install), imported in `core/market_data.py`. |
| `transformers` | **No** (`grep transformers/FinBERT` returns 0 hits) | Stale — FinBERT path was planned (see comment in requirements.txt: *"FinBERT (optional, heavier)"*) but never wired into `sentiment/`. |
| `torch` | **No** (`grep "^import torch\|^from torch"` returns 0 hits) | Stale — only needed by `transformers`, which isn't wired either. |
| `asyncio-throttle` | **No** (`grep asyncio_throttle` returns 0 hits) | Stale — `tenacity` is the retry/backoff lib actually used. |
| `pytz` | **No** (`grep "^import pytz\|^from pytz"` returns 0 hits) | Stale — code uses `datetime.timezone.utc` / `python-dateutil` (incidentally installed as `python-dateutil==2.9.0.post0`). |
| `colorama` | **No** (`grep colorama` returns 0 hits) | Stale — `rich` provides colour on every platform. |

**All 6 "missing" deps are unused in source.** They are vestigial entries in `requirements.txt` that should be pruned. The bot does not actually depend on them.

## Incidental installs (in venv but not in requirements.txt)

72 packages. The majority are transitive — installed automatically by `web3`, `anthropic`, `pandas`, `xgboost`, `praw`, `telethon`, etc. A handful are notable:

| Package | Likely reason |
|---------|---------------|
| `ta==0.11.0` | **First-class import** — `core/market_data.py` uses `from ta...`. Should be **added** to `requirements.txt`. |
| `websockets==15.0.1` | Transitive via `ccxt.pro` (ccxt 4.x bundles `ccxt.pro` which uses `websockets`). |
| `websocket-client==1.9.0` | Transitive via `praw` / Reddit auth flow. |
| `requests==2.34.2` | Transitive — used by `praw`, `pytrends`, `cryptocompare` and others. |
| `cryptography==48.0.0`, `eth-*`, `coincurve`, `ckzg`, `pycryptodome`, `bitarray`, `parsimonious`, `rlp`, `pyunormalize` | Transitive via `web3==7.16.0`. |
| `nvidia-nccl-cu12==2.30.4` | Unexpected — pulled by some `xgboost` or `scipy` variant. Worth confirming and potentially excluding via `--no-deps` if the box has no GPU. |
| `py-spy==0.4.2` | Developer profiling tool — not part of the bot. Manual install. |
| `types-requests==2.33.0.20260518` | Type stubs — developer-time, harmless. |
| `scipy==1.17.1` | Transitive via `scikit-learn` and possibly `numpy`-aware code. |
| `lxml==6.1.1` | Transitive via `feedparser` HTML parsing. |
| `Telethon==1.43.2` | This IS in `requirements.txt` (`telethon==1.34.0`); case difference (`telethon` vs `Telethon`) made it appear absent in the simple diff. Treat as drift, not incidental. |

## Notable risks

1. **`anthropic` drifted from 0.21.3 → 0.103.0 (~80 minor releases).** Major breaking changes likely (tool use, streaming, message format, model IDs). `core/agent.py` is written against the older SDK — needs review for compatibility. Tests passing (552 / 552) suggest the surface in use is small enough to survive the drift, but message construction at `core/agent.py:21-46` should be checked against current SDK.
2. **`numpy 1.x → 2.x` and `pandas 2.x → 3.x` are major-major upgrades.** Both have broken APIs (numpy 2 dropped `np.bool` aliases, pandas 3 dropped some implicit type coercions). Tests pass, so the surface is small, but recreating the venv from `requirements.txt` would land on numpy 1.x / pandas 2.x — a different runtime than what's been tested.
3. **`pytest 8.1.0 → 9.0.3` and `pytest-asyncio 0.23.5 → 1.3.0` are major upgrades.** The whole test suite was written and is being run against pytest 9. Re-installing per `requirements.txt` would downgrade and may break async-fixture wiring.
4. **`xgboost 2.0.3 → 3.2.0` and `scikit-learn 1.4.1 → 1.8.0` matter for the predictive engine** — except `predictive/` is empty, so the drift is currently moot. Worth realigning when that package gets built.
5. **`ta==0.11.0` is in use but undeclared.** A `pip install -r requirements.txt` on a clean machine that doesn't auto-pull `ta` as a transitive will fail at import time in `core/market_data.py`.
6. **Six unused deps inflate the surface.** Removing them speeds installs and avoids security-scanner noise.

## Recommendation (not actioned — audit is read-only)

The current `requirements.txt` does **not** reproduce the running venv. To match reality, the file would need to:

- Bump every drifted version to the installed one.
- Remove `pandas-ta`, `transformers`, `torch`, `asyncio-throttle`, `pytz`, `colorama`.
- Add `ta==0.11.0`.

But "match reality" and "what we want to ship" are different goals — the audit just records the gap. The decision belongs to the operator.

---

## Cross-cutting concerns

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
---

## Known TODOs, stubs, and placeholders — consolidated

Consolidated from every `module_reports/*.md` and the `settings_audit.md` findings. Ranked High / Medium / Low by **operational impact** (would this hurt you in production?), not by code-size of the fix.

### High

1. **`OrderRouter._live_execute` is a stub returning `None`.** Flipping `SIM_MODE=False` makes the signal-track Bot silently no-op every live trade.
   File: `execution/router.py`. Source: `module_reports/execution.md`.

2. **`KillSwitch._close_position` live path AttributeErrors silently.** `Bot` constructs `KillSwitch(exchange_manager=None)`; the live path calls `self._exchange_manager.market_close(...)` and swallows the exception per-trade. In live mode the kill switch closes nothing.
   File: `execution/kill_switch.py`. Source: `module_reports/execution.md`.

3. **`KillSwitch` sim closure uses `trade.entry_price` as exit price.** Every sim kill records `pnl_pct=0.0`. Kill-switch P&L attribution is broken in sim.
   File: `execution/kill_switch.py:69` (TODO present). Source: `module_reports/execution.md`.

4. **`core/regime_detector.py:73-79` — `RegimeSnapshot.summary()` has invalid f-string format specs** (e.g. `f"...:.1f if self.adx else '?'"`) that raise `ValueError` whenever called. `logger.debug(snap.summary())` runs on every regime update in DEBUG mode.
   File: `core/regime_detector.py:73-79`. Source: `module_reports/core.md`.

5. **`core/agent.py:43-46` — Claude failure handler fails open.** On API exception, sets `claude_reasoning` but never sets `signal.indicators["claude_rec"]`; `_route_for_approval` treats the missing key as "not SKIP" and auto-executes in autonomous/window mode.
   File: `core/agent.py:43-46`. Source: `module_reports/core.md`.

6. **`signals/quality_gate.py:104-105` double-counts OFI.** Momentum/reversion scanners already fold `ofi.signal_modifier(direction)` into `sentiment_mod` (`momentum.py:89`, `reversion.py:110`); `QualityGate.evaluate` adds it again. Every gated mom/rev signal gets OFI applied twice.
   Source: `module_reports/signals.md`.

7. **`Signal.to_db_dict()` persists the clamped `score` but the gate decides on `raw_score`.** The DB row's score is not the value the gate compared against `SIGNAL_SCORE_THRESHOLD`. Also drops `raw_score`, `expires_at`, `win_probability`. `raw_score` is mutated in-place by `evaluate` — a second call would compound modifiers.
   Source: `module_reports/signals.md`.

8. **No Alembic; five model TODOs flag schema gaps.** `init_db()` only creates missing tables — never alters. An older DB silently loses new columns: `ArbTrade.status`, `slippage_*_pct`, plus four newer observation/funding/xchain tables. Only `scripts/migrate_scalp_v2.py` is scripted.
   File: `database/models.py` (5 TODO comments). Source: `module_reports/database.md`.

9. **`Prediction` table has zero query helpers and is empty (0 rows).** Model + relationship exist but nothing in `queries.py` reads or writes it. `predictive/` is an empty package; CLAUDE.md mentions `python -m predictive.trainer` as if shipped.
   Source: `module_reports/database.md`, `module_reports/predictive.md`.

10. **`requirements.txt` does not reproduce the running venv.** 22 drifted (incl. anthropic 0.21 → 0.103 — ~80 minor releases), 6 unused entries (`pandas-ta`, `transformers`, `torch`, `asyncio-throttle`, `pytz`, `colorama`), and `ta==0.11.0` is in use but undeclared. A `pip install -r requirements.txt` on a fresh box would land on a different runtime than what the 552-test suite has been passing against.
   Source: `deps_audit.md`.

### Medium

11. **Coordinator portfolio-exposure breaker is log-only.** `block_new_entries` enforcement is an outstanding TODO. The breaker fires but doesn't actually prevent the next entry.
   File: `agents/coordinator.py`. Source: `module_reports/agents.md`.

12. **`agents/scalping_*.py` and `scalping_v2/scalping_*.py` are byte-identical** (SHA-256 verified). Production imports `agents.*`; `scalping_v2/` is a docs+demo bundle. No CI parity check — silent drift is the expected long-run state.
   Source: `module_reports/scalping_v2.md`.

13. **`scalping_agent_v2_integration.evaluate_signal_v2` is reference-only.** It accesses `agent.scalp_capital` / `agent.s` attributes that don't exist on the real `ScalpingAgent`. Only `is_ready_for_live_v2` is actually used (by tests).
   Source: `module_reports/agents.md`.

14. **`CexTransferRail._call_ccxt_withdraw` is a `NotImplementedError` stub** gated by `REBALANCE_LIVE_ENABLED`. `_WITHDRAWAL_ADDRESSES` allowlist is empty — fails-closed even when the live flag is set.
   File: `agents/balance/rails/cex_rail.py`. Source: `module_reports/agents.md`.

15. **Sentiment composite is mis-calibrated for a vanilla install.** 3 of 5 sentiment sources are inert (`telegram` permanent stub; `reddit` + `google_trends` need optional deps + env vars). The modifier ladder breakpoints (in `settings.py:1029-1042`) were tuned assuming all five active. With only `fear_greed` (0.4) + `cryptopanic` (0.25) = 0.65 of intended weight, the composite floor and ladder mistrigger.
   Source: `module_reports/sentiment.md`.

16. **Vestigial sentiment settings block.** `config/settings.py:371-388` defines an older sentiment ladder (`SENTIMENT_WEIGHTS`, `SENTIMENT_BOOST/BLOCK_THRESHOLD`, `SESSION_MIN_SENTIMENT_SCORE`) that no code in `sentiment/` reads. The active knobs live at lines 1029-1042. Dead config.
   Source: `module_reports/sentiment.md`, `settings_audit.md`.

17. **`prompts/build_wiring.md:72` references a non-existent `sentiment_aggregator` import.** Actual export from `sentiment/aggregator.py` is the singleton `sentiment`. Doc-drift; any future Claude session reading this prompt would fail.
   Source: `module_reports/sentiment.md`, `prompts_inventory.md`.

18. **Magic numbers outside `settings.py` violate the CLAUDE.md "single tuning instrument" rule.** Highlights:
    - `execution/arb_engine.py:441` 10 % depth cap; `:566` 10× clamp; three places hardcode 0.002 fee fallback.
    - `execution/mexc_key_router.py:88` `range(1, 31)`.
    - `execution/chains/*.py` `SWAP_GAS_UNITS` constants.
    - `agents/scalping_agent.py` ±0.8 "moderate" z-bands; 30-second bucket-staleness cutoff (shadows `SCALP_OFI_Z_*`).
    - `core/bot.py:704` 60-second `_self_review_loop` sleep.
    - `macro/monitor.py:70` 0.5 yield-curve boundary; `fred_calendar.py` UTC hour/minute defaults.
    - `strategies/*` — every concrete strategy hardcodes score weights and SL/TP multipliers (zero unit tests on this either).
    Source: `settings_audit.md`, `module_reports/{execution,agents,strategies,macro}.md`.

19. **Dead config in `settings.py`** — 90 module-level constants have no reference outside the file. Concentrated in: Predictive engine (7), legacy per_trade/window/autonomous notification + session-floor block, legacy UI options (`UI_REFRESH_RATE`, `UI_SHOW_*`, `LOG_ROTATION`, `ORDER_TYPE`, `ORDER_RETRY_*`, `TRAILING_STOP_*`).
   Source: `settings_audit.md`.

20. **`Dashboard._panel_footer` advertises stale keystrokes** (`G/M/I/1-5`) that `ApprovalInputHandler` does not handle. Cmd-bar and approval panel are in sync; the footer is out of date.
   Source: `module_reports/ui.md`.

21. **Heavy private-attribute coupling between UI and agents.** Both `Dashboard` and `WebServer` reach into `coordinator._agents` and ~15 `_private` attributes on each agent to build snapshots. Significant duplication between terminal and web snapshot code paths; fragile to agent-internal refactors.
   Source: `module_reports/ui.md`.

22. **`MacroSignal` pipeline is dead-on-arrival.** `_derive_signals` runs on every macro refresh and `get_macro_signals()` is exposed, but no module imports it — the planned `MacroAgent` consumer doesn't exist anywhere in the tree.
   Source: `module_reports/macro.md`.

23. **`coinglass.py` hardcodes its base URL** instead of pulling from settings, breaking the "every source pulls from settings" convention of the other 12 sources.
   Source: `module_reports/data_sources.md`.

24. **Data-source callers cannot distinguish 0.0 reading from "no data".** Numeric sources (`binance_futures`, `bybit_derivs`, `coinglass`) use `or 0.0`/`or 1.0` fallbacks; combined with `cached_value`'s error-suppresses-default contract, the genuine-zero vs missing-data ambiguity propagates downstream silently.
   Source: `module_reports/data_sources.md`.

25. **Doc-bundle duplication of v2 rollback section.** `RUNBOOK.md → Rollback` and `scalping_v2/RUNBOOK_v2_rollback_section.md` are two copies of "how to roll back v2" with no enforced sync.
   Source: `docs_inventory.md`.

26. **`scalping_v2/SCALPING_V3_ROADMAP.md` is orphaned** — not referenced from any other doc. Important for understanding deferred work; the master audit treats it as its source (see "What is deliberately NOT built").

### Low

27. **`scalping_v2/SCALPING_V2.md` references a `scalping_agent_doc.docx`** that does not exist anywhere in the repo. The referenced design doc was external or has been removed.
   Source: `docs_inventory.md`.

28. **`prompts/build_balance_agent.md` is empty** (0 bytes / 0 lines). Superseded the same day by the 279-line `balance_agent.md`. Should be deleted.
   Source: `prompts_inventory.md`.

29. **`prompts/5kfund.md` references `OPERATIONS.md` and `SOAK_CRITERIA.md`** which do not exist in the repo. Either external operator notes or deferred deliverables.
   Source: `prompts_inventory.md`.

30. **`run.sh` hardcodes `python3.11`.** Works on the recorded WSL2 Ubuntu image, breaks on systems with a differently-named Python binary.

31. **`pytest.ini` doesn't pin `asyncio_mode = auto`.** Adding a bare `async def test_*` would silently no-op under pytest-asyncio `strict`.

32. **`config/settings.py.backup` exists in the working tree.** Decision pending: archive or delete.

33. **`tests` directory in CLAUDE.md is described as "currently only has __init__.py"** — out of date; the directory has 30 test files and a passing 552-test suite at snapshot.

34. **Telegram sentiment source is a permanent `is_available()=False` stub.** Documented in its file; 4 of the 11 total TODO sites in the codebase live there.

35. **`websockets.legacy` deprecation warning** in `test_chain_connectors.py` — transitive via `web3 → websockets`. Will need a fix when `websockets` drops the legacy module.

---

## What is deliberately NOT built

From `scalping_v2/SCALPING_V3_ROADMAP.md` (10 deferred items, V3-1 through V3-10; V3-7 has 3 sub-items). Read that file for the full reasoning; this is the catalogue.

| ID | Item | Trigger to build | Scope |
|----|------|------------------|-------|
| V3-1 | Maker/taker fee-side selection in execution decision | When taker fees become the dominant cost component on the active venue, or post-only orders start filling reliably. | Small — adjust order type selection logic + a few settings. |
| V3-2 | Multi-symbol OFI normalisation across the universe | When BTC/ETH-only OFI proves insufficient and altcoin-specific microstructure diverges enough to matter. | Medium — new normaliser; touches `signals/ofi.py` + scalper. |
| V3-3 | Live exchange-side WebSocket book streaming for non-MEXC venues | When `ccxt.async_support.watch_*` becomes reliable across exchanges (currently dead per `MEMORY.md → project_scalper_no_observations`). | Medium — venue-specific stream wiring; possibly per-venue protobuf decoders. |
| V3-4 | Adverse-selection re-tuning from accumulated `scalp_observations` | Weekly cadence per the v2 recalibration cookbook. Triggered once 1k+ entered observations accumulate after each model bump. | Small — SQL recipe + threshold edit. |
| V3-5 | Cross-exchange OFI agreement boost | Once cross-venue feeds are healthy (V3-3 prerequisite). | Small — extend `confluence_score`. |
| V3-6 | HTF (higher-timeframe) alignment threshold tightening | When false-positive rate exceeds the cookbook target. | Small — threshold adjustment. |
| V3-7a | ATR adaptive SL — vol-of-vol awareness | Vol-of-vol regime that consistently chops out the static-ATR SL. | Small — new factor in `ATRStopCalculator`. |
| V3-7b | ATR adaptive SL — session-of-day calibration | Different SL profile by Asia/EU/US session. | Small. |
| V3-7c | ATR adaptive SL — regime-aware SL multiple | Plug `regime_detector` outputs into SL sizing. | Small. |
| V3-8 | Predictive feature pipeline (XGBoost) | Once enough observation rows accumulate AND the predictive `Prediction` schema is wired. | **Large** — needs the entire `predictive/` package to exist first. CLAUDE.md mentions `python -m predictive.trainer` but the package is empty. |
| V3-9 | Volume-adequacy adaptive threshold | When low-vol periods cause systematic skips. | Small. |
| V3-10 | Capital scaling within session | After a streak of confluent wins, allow modest size-up within session. | Medium — interacts with `BalanceAgent` and approval modes. |

Other deliberately-deferred work observed elsewhere in the codebase but not catalogued in V3 roadmap:

- **Live execution path completion** (signal-track router, kill-switch live close, balance live withdrawal). Conscious gate — the bot operates in sim until specific live readiness criteria are met.
- **Alembic migration tree.** Schema gaps tracked via TODOs in `database/models.py` and the one hand-rolled `scripts/migrate_scalp_v2.py`. Deferred until the cost of hand migrations exceeds Alembic setup.
- **MacroAgent consumer of `MacroSignal`.** The producer side runs; no consumer exists. The whole signal pipeline is dead-on-arrival until that decision lands.

---

## Restore procedure

Reproduced from `restore/RESTORE_PROCEDURE.md` (Phase 10 deliverable).

---

# Restore procedure — CryptoBot 1.0

How to rebuild a working copy of CryptoBot at this exact state from scratch. Targeted at someone with **no prior project context**.

The snapshot is anchored to:

- **Git SHA:** `8e3efd1ae4c1f4b268e4ecfa7a4bdc9b9aca8b7f`
- **Branch:** `feat/web-ui-v2-agent-panels`
- **Snapshot date:** 2026-05-29
- **177 tracked files** (per `git ls-files`); checksums in `checksums.txt`.

## 1. Environment prerequisites

| Component | Version | Notes |
|-----------|---------|-------|
| OS | Ubuntu 22.04 on WSL2 (Windows host) or native Linux | Path conventions in repo assume Unix-style |
| Python | **3.11** (3.11.15 confirmed) | `pandas-ta` (declared but unused) and several other deps in `requirements.txt` need 3.11. Do **not** use 3.12+ for fidelity. |
| Git | any modern version | |
| SQLite | 3.x (bundled with Python) | DB at `data/cryptobot.db` |
| Node.js | optional — only for installing/using Claude Code CLI | Not required to run the bot |

You do **not** need a GPU. Despite `torch` and `transformers` being in `requirements.txt`, neither is imported in source (see `deps_audit.md`).

## 2. Get the repo

```bash
# Option A — clone if a remote is available
git clone <remote-url> cryptobot
cd cryptobot
git checkout feat/web-ui-v2-agent-panels
git checkout 8e3efd1ae4c1f4b268e4ecfa7a4bdc9b9aca8b7f -b cryptobot-1.0-restore
```

```bash
# Option B — restore from a tarball/zip backup
mkdir cryptobot && cd cryptobot
tar xzf <backup.tar.gz>
# Then validate checksums (step 8)
```

## 3. Build the venv

```bash
cd ~/cryptobot
python3.11 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
```

### Two install options

**A. Reproduce the venv state captured in this snapshot (recommended).** The on-disk venv at snapshot time has 99 packages — `requirements.txt` does not match it. Use `pip_freeze.txt` for fidelity:

```bash
pip install -r audit/cryptobot_1.0/pip_freeze.txt
```

This gives you the exact versions the 552-test suite passed against.

**B. Reproduce from declared requirements (lossier).**

```bash
pip install -r requirements.txt
# Then add the deps that are imported but undeclared:
pip install ta==0.11.0
```

Note that option B downgrades several packages (`numpy 2 → 1`, `pandas 3 → 2`, `pytest 9 → 8`, etc.) — see `deps_audit.md`. Some test assertions or runtime call sites may break under the older versions.

## 4. Set up `config/keys.env`

The file `config/keys.env` is **not** in git (`.gitignore` covers it). Copy the template:

```bash
cp config/keys.example.env config/keys.env
```

Fill in (only what you intend to use — none are required for sim mode unless noted):

| Env var | Used by | When required |
|---------|---------|---------------|
| `ANTHROPIC_API_KEY` | `core/agent.py` (Claude reasoning) | **Required** for any signal evaluation — `core/agent.py:21` instantiates the SDK at import time. |
| `BINANCE_API_KEY`, `BINANCE_SECRET` | `core/market_data.py:42` | Live trading on Binance. Sim works without. |
| `KRAKEN_API_KEY`, `KRAKEN_SECRET` | `core/market_data.py:43` | Live trading on Kraken. |
| `BYBIT_API_KEY`, `BYBIT_SECRET` | `core/market_data.py:44` | Live trading on Bybit. |
| `OKX_API_KEY`, `OKX_SECRET`, `OKX_PASSPHRASE` | `core/market_data.py:45` | Live trading on OKX. |
| `MEXC_KEY_{1..4}_API_KEY`, `MEXC_KEY_{1..4}_SECRET` | `execution/mexc_key_router.py:107-135` | Multi-key MEXC routing — needed if MEXC is in your exchange set. |
| `CRYPTOPANIC_API_KEY` | `sentiment/sources/cryptopanic.py:113` | Sentiment via CryptoPanic (RSS fallback works without). |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | `sentiment/sources/reddit.py:98-100` | Reddit sentiment (otherwise the source is unavailable). |
| `FRED_API_KEY` | `data_sources/sources/fred.py`, `macro/sources/fred_calendar.py` | FRED macro data + calendar (otherwise unavailable). |
| `ALPHA_VANTAGE_API_KEY` | `data_sources/sources/alpha_vantage.py` | Alpha Vantage data source. |
| `COINGECKO_API_KEY` | `data_sources/sources/coingecko.py` | CoinGecko Pro tier (public tier works without). |
| `CRYPTOCOMPARE_API_KEY` | `data_sources/sources/cryptocompare.py` | CryptoCompare data. |
| `ARBITRUM_RPC_URL`, `BASE_RPC_URL`, `OPTIMISM_RPC_URL` | `execution/chains/*.py` | Cross-chain arb. Agent stays OFFLINE without these (per `agents/__init__.py:REGISTERED_AGENTS`). |

See `module_reports/config_and_root.md` and `data_sources.md` for the full key→source mapping.

## 5. Initialise the database

The database is created idempotently on first bot startup:

```bash
PYTHONPATH=. venv/bin/python -c "from database.db import init_db; init_db()"
```

This creates `data/cryptobot.db` with all 21 tables. WAL mode and FK enforcement are set via SQLAlchemy connect listeners (`database/db.py`).

If you are restoring against an older DB that predates the scalp v2 schema:

```bash
PYTHONPATH=. venv/bin/python scripts/migrate_scalp_v2.py
```

Note: there is **no Alembic migration tree**. Five other model TODOs flag schema gaps that would need hand migration on an older DB — see `module_reports/database.md` "Migrations" and "Known issues".

## 6. Run the test suite

```bash
PYTHONPATH=. pytest -v --tb=short
```

Expected at this snapshot: **552 passed, 1 warning, 0 failures, ≈12s wallclock**. (The warning is a `websockets.legacy` deprecation from `tests/test_chain_connectors.py::test_is_available_false_without_rpc_env_var`.)

Single-file or keyword runs:

```bash
pytest tests/test_quality_gate.py
pytest -k "scalp"
```

## 7. Run the bot (sim mode)

The default in `config/settings.py` is `SIM_MODE = True` — the live path is partially unbuilt and not safe (see `cross_cutting.md` §6 and `module_reports/execution.md`).

```bash
# Convenience launcher (balanced profile + default strategy)
./run.sh

# Explicit, with all CLI flags
PYTHONPATH=. venv/bin/python main.py \
    --profile balanced \
    --strategy default \
    --sim \
    --dashboard \
    --web-ui \
    --debug
```

All CLI flags:

| Flag | Purpose |
|------|---------|
| `--profile {conservative|balanced|aggressive|custom}` | Risk profile (overrides `settings.ACTIVE_PROFILE`). |
| `--strategy {default|arb_only|scalper|custom}` | Strategy (overrides `settings.ACTIVE_STRATEGY`). |
| `--sim` | Force sim mode. |
| `--live` | Force live mode. **Not safe in 1.0** — most live paths are stubs. |
| `--debug` | DEBUG-level logging. |
| `--dashboard` | Run Rich terminal dashboard alongside the bot. |
| `--web-ui` | Start web control panel on `http://localhost:8765`. |

Shutdown: `Ctrl+C` (SIGINT). The bot performs a bounded teardown via `coordinator.stop()` and `web_server.stop()` within `SHUTDOWN_TIMEOUT_SEC`.

## 8. Validate against the checksums

After install, before relying on the restored copy:

```bash
cd ~/cryptobot
sha256sum -c audit/cryptobot_1.0/restore/checksums.txt
```

Expected: 177 files, all OK. Any mismatch means either (a) a file was hand-edited after the snapshot, or (b) the restore copy isn't actually at `8e3efd1ae4c1f4b268e4ecfa7a4bdc9b9aca8b7f`. Reconcile by checking out the SHA again.

Files **not** in git (and thus not in checksums):

- `config/keys.env` (per `.gitignore`)
- `venv/`
- `data/cryptobot.db` + `-wal` / `-shm` files
- `logs/*`
- `backups/*`
- `audit/` (this directory)
- `.pytest_cache/`
- a couple of `.backup` / `.pre-restore.*` sidecar files near `config/` and `data/`

## 9. Operator quick-reference

| Want to … | Command |
|-----------|---------|
| Watch the scalper | `bash scripts/watch_scalp.sh` (one-shot) or `watch -n 5 bash scripts/watch_scalp.sh` |
| Run the v2 recalibration sanity check | `PYTHONPATH=. venv/bin/python scripts/run_recalibration.py` |
| Discover MEXC per-key allowlist | `PYTHONPATH=. venv/bin/python scripts/mexc_probe.py` |
| Tail today's log | `tail -f logs/cryptobot.log` |
| List open positions | `sqlite3 data/cryptobot.db "SELECT * FROM trades WHERE exit_price IS NULL;"` |
| Re-read this audit | `less audit/cryptobot_1.0/CRYPTOBOT_1.0_AUDIT.md` |
---

## Appendix A — Full file inventory

193 files. See `file_inventory.csv` (in this directory) for the machine-readable form with columns: `path, size_bytes, line_count, sha256, language, role`.

Role distribution:

```
113 source
 30 test
 25 prompt
 10 docs
  8 config
  5 script
  2 other
```

Top 20 files by line count (Python only):

| File | LOC |
|------|----:|
| `ui/dashboard.py` | 2007 |
| `prompts/build_scalping_agent.md` | 1391 (markdown, included for size context) |
| `ui/web_server.py` | 1363 |
| `config/settings.py` | 1269 |
| `ui/web_dashboard.html` | 1291 (HTML, included for size context) |
| `core/bot.py` | (large; see file_inventory.csv) |
| `database/queries.py` | (large) |
| `database/models.py` | (large) |
| `signals/quality_gate.py` | (medium) |
| `execution/arb_engine.py` | (medium) |

For the canonical list, sort `file_inventory.csv` by `line_count`.

## Appendix B — Pytest output (head + tail)

Full output: `pytest_output.txt`.

Head:

```
============================= test session starts =============================
platform linux -- Python 3.11.15, pytest-9.0.3, pluggy-1.6.0
rootdir: /home/gabriel/cryptobot
configfile: pytest.ini
testpaths: tests
plugins: asyncio-1.3.0, mock-3.15.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None
collected 552 items
```

Tail:

```
tests/test_web_server.py::test_rebalance_blocked_when_balance_agent_missing PASSED [ 99%]
tests/test_web_server.py::test_rebalance_logs_event_on_confirm PASSED    [ 99%]
tests/test_web_server.py::test_rebalance_confirm_replan_mismatch PASSED  [100%]

=============================== warnings summary ===============================
tests/test_chain_connectors.py::test_is_available_false_without_rpc_env_var
  /home/gabriel/cryptobot/venv/lib/python3.11/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html for upgrade instructions
    warnings.warn(  # deprecated in 14.0 - 2024-11-09

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
======================= 552 passed, 1 warning in 12.12s ========================
```

## Appendix C — Git state and recent commits

Verbatim from `git_state.txt`:


```
2026-05-29T10:54:47Z
--- audit started ---

=== git branch ===
feat/web-ui-v2-agent-panels

=== git SHA ===
8e3efd1ae4c1f4b268e4ecfa7a4bdc9b9aca8b7f

=== git status ===
On branch feat/web-ui-v2-agent-panels
Untracked files:
  (use "git add <file>..." to include in what will be committed)
	audit/
	config/keys.env.pre-restore.20260528-211120
	config/settings.py.backup
	prompts/5kfund.md
	prompts/balance_agent.md
	prompts/build_balance_agent.md
	prompts/build_crosschain_agent_v2.md
	prompts/build_funding_arb.md
	prompts/build_web_ui_refresh_v1.md
	prompts/build_web_ui_v2_agent_panels.md
	prompts/build_web_ui_v3_agent_pages.md
	prompts/cryptobot_audit.md
	prompts/fix_deisland_engines.md
	prompts/fix_pre_soak.md
	scripts/watch_scalp.sh

nothing added to commit but untracked files present (use "git add" to track)

=== git log -50 ===
8e3efd1 fix(funding_engine): instantiate binance with defaultType=future — fixes silent fetch_funding_rate failures
8fe5b9a feat(funding_arb): widen universe, symmetric APR floor, reverse_carry variant — fills observation table
c7c5c63 fix(inventory_state): undersubscribed venues return physical − other_claims (match docstring) — unblocks arb engine
57cc9db fix(scalping_agent): write Trade.pnl_pct as fraction (not percent) — stops phantom CB HALTs
b73a7c9 fix(signals/arbitrage): coerce ccxt prices to float — stop ~48/30min ERROR floods on every scan
44c3854 chore: re-allocate EXCHANGE_BALANCES to sum to 5000 (ring-fenced per fund)
5d4f770 test(funds): track 5k-soak fund layout — add FUND_MEXC_ARB_CAPITAL, expand reserve assertion, xfail EXCHANGE_BALANCES ledger invariant
0cdbb40 chore: reset to $5000 starting capital — soak allocation (arb 2000 / signal 1600 / mexc-scalp 500 / mexc-arb 500 / reserve 400; xchain + funding observation-only)
904cb01 feat(xchain): wire 3 EVM connectors — pinned WETH-USDC pools + dotenv load order + web3 dep
0dc67e7 feat(web-ui): v3 — per-agent click-through pages, dashboard compaction
9bb7df3 fix(web-ui): sanitize non-finite floats before json.dumps — fixes silent blank dashboard
d901e97 feat(web-ui): v2 dedicated agent panels + /action/rebalance three-action contract
a99b7de fix: de-island FundingRateArbEngine (full allocation wiring) and CrossChainArbEngine (breaker-correctness; xchain rebalancing left to cross-chain rail)
6885a63 fix: %-based loss breakers, true-P&L netting, settings-ize drift hint, correct Miller-Orr band half-width
2e6c3af feat: funding-rate arb agent (phase 1 — observation mode, delta-neutral, binance)
2fa2a97 feat: BalanceAgent — fund-aware inventory state, Kelly compounding, Miller-Orr transfer-minimising rebalance (sim-first, live stubbed)
e281b8a feat: cross-chain arb agent — observation mode, venue-aware connectors, inventory targets
36b9618 tweak(web-ui): drop of-bankroll suffix from Daily P&L percentage
4495e78 feat: web UI refresh v1 — bankroll, agent pages, session pages, scalp feed persistence
4d52a1a feat(scalp): activate sim at $500 + stale-feed entry guard
bb03439 fix: clean SIGINT/SIGTERM shutdown + wire scalp OFIEngine to the book stream
bade395 feat(market-data): stream the full scalp universe via sharded book connections
d04faa1 fix(market-data): stream order books via ccxt.pro so OFI gets fed
cc3a488 fix(market-data): same per-symbol fix for the candle streamer
fa1696e fix(market-data): stop order-book streamer pinning the event loop at 100% CPU
9095533 fix(session-pnl): fold arb trades into Session P&L
3a7d6d5 feat(equity): reconstruct fund P&L from the ledger on startup
420727c feat(web-ui): scalp/arb dashboard feeds + fees column + cls() fix
cc9d9f9 docs(scalp-v2): RUNBOOK rollback section + GLOSSARY v2 terms
44cc041 feat(scalp-v2): activation-readiness queries (v1+v2) + recalibration runner
931bf36 feat(scalp-v2): wire confluence + ATR-aware TP/SL into ScalpingAgent
77aabb2 feat(scalp-v2): extend scalp_observations with 13 nullable v2 columns + migration
d78522c feat(scalp-v2): MarketData + OFIEngine accessors for the confluence/ATR gates
ecffde7 feat(scalp-v2): settings + confluence/ATR modules + 41 tests
0704d3b fix: connect Binance market data — skip signed currency fetch (-1021 under WSL clock skew)
b592ef0 feat: web control panel — aiohttp server, WebSocket push, full browser dashboard
e30cd41 feat: OFI engine — 10-level depth + volume-weighted TFI (Xu/Gould/Howison 2018)
d589b4d fix: segregate signal-fund stats from scalp trades
cefc3f8 feat: scalping agent sim execution enabled — SCALP_CAPITAL=100, _place_order sim mode
25d0534 refactor: strip unused ArbEngine fund knobs
bea361e refactor: fold MEXC-ARB into main ARB fund, fix equity total, 3-fund dashboard
f5b46b1 fix: back funds with a $1,100 sim balance ledger; keep signal sizing ring-fenced
357e735 feat: ring-fenced fund architecture — 4 independent funds, MEXC $200 ring-fenced, per-fund circuit breakers
68194f0 fix: add POL/USDT to scalp pair coverage
fe92bef feat: wire MEXC 4-key routing + verify pair coverage + update SCALP_PAIRS to 95 pairs
eade019 feat: scalp feed dashboard panel — OFI strip, positions, closed observations, stats bar, 13 rows
9514ccb feat: MEXC per-pair key router — one account, many pair-restricted keys, symbol-scoped routing in scalper
199d850 feat: dashboard — arb opportunity panel + capital gate status surfaced
5f393cb feat: data_sources module + Coinglass source — funding rate arb engine now live-wired
3903888 feat: arb engine — capital verification, dynamic sizing, depth-aware slippage, opportunity logging, funding rate stub

=== git diff --stat ===

=== git diff (uncommitted) ===
```

---

**Document line count:** 7157
