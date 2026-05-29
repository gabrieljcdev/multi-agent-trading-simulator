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
