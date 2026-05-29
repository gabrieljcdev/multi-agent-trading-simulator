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

