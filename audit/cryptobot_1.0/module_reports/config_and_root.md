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
