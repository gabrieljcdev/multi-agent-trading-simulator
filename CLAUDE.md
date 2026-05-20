# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The project targets Python 3.11 and depends on the `venv/` virtualenv at the repo root.

```bash
# Activate env (created via `python3 -m venv venv`)
source venv/bin/activate     # Linux/WSL
venv\Scripts\activate        # Windows

# Install deps
pip install -r requirements.txt

# Run the bot (sim mode by default — set SIM_MODE=False in settings.py for live)
python main.py
python main.py --profile conservative --strategy arb_only
python main.py --sim                  # force sim
python main.py --live                 # force live (real money)
python main.py --debug                # debug logging

# Quick start helper
./run.sh                              # balanced profile + default strategy

# Tests (pytest + pytest-asyncio; tests/ currently only has __init__.py)
pytest                                # all tests
pytest tests/test_foo.py              # single file
pytest tests/test_foo.py::test_bar    # single test
pytest -k "quality_gate"              # by keyword

# Train the predictive model (XGBoost) on accumulated sim data
python -m predictive.trainer
```

API keys live in `config/keys.env` (gitignored). Template is `config/keys.example.env`. The `ANTHROPIC_API_KEY` is required for the Claude agent loop; exchange keys are only needed once `SIM_MODE = False`.

## Architecture

This is an async, event-driven trading assistant. The flow on every scan is:

```
MarketData (ccxt) ──► SignalEngine ──► QualityGate ──► ClaudeAgent ──► Bot ──► OrderRouter ──► DB
                          ▲                ▲                              │
                          │                │                              ▼
                       Strategy        Regime + Guards               Approval mode
                                       + OFI + Session                (per_trade | window | autonomous)
```

Key invariants worth knowing before touching anything:

- **`config/settings.py` is the single tuning instrument.** Nothing is hardcoded elsewhere — every threshold, weight, lookback, and feature flag comes from here. Don't introduce magic numbers in other modules; add a constant in `settings.py` and reference it. Each setting has a `# test:` comment giving its sweep range; preserve that convention.
- **Profiles (`profiles/*.json`) override settings at runtime** via `profile_manager`. Adding a tunable means it likely needs both a default in `settings.py` and a slot in the `Profile` dataclass (`profiles/profile_manager.py`) plus each `*.json` profile.
- **Strategy registry pattern**: every strategy subclasses `BaseStrategy` (`strategies/base_strategy.py`) and registers in `strategies/__init__.py:STRATEGIES`. A strategy controls which scanners run (`should_run_arb/momentum/reversion`), how scores are adjusted (`score_signal`), and how passing signals are ranked (`rank_signals`). It does not change indicators.
- **Module-level singletons** are how cross-cutting state is shared: `agent` (core/agent.py), `profile_manager` (profiles/profile_manager.py), `quality_gate` (signals/quality_gate.py), `regime_detector` (core/regime_detector.py), `guard_runner` (core/guards.py), `ofi_scorer` (signals/ofi.py). Use them by import, not by re-instantiation.
- **`Signal` (signals/base.py) is the universal currency.** Every scanner emits one; everything downstream — quality gate, agent, router, DB — consumes one. New per-signal data goes into `Signal` fields or the freeform `indicators` dict, and `to_db_dict()` must stay in sync with `database/models.py:Signal`.
- **Score composition is additive**: `Signal.score = clamp(raw_score + sentiment_mod, 0, 100)`. `QualityGate.evaluate` mutates `raw_score` by adding regime, session, guard, OFI, and sentiment modifiers in that order before applying the threshold. If you add a new modifier, fold it into the gate, not the scanners.
- **Three approval modes drive everything in `Bot._on_new_signal`**: `per_trade` queues for keyboard approval, `window` auto-executes inside an approved session, `autonomous` auto-executes within hourly/daily caps. Don't bypass `Bot` to call `OrderRouter` directly — the mode dispatch and circuit-breaker checks live there.
- **Sim vs live is a boolean (`settings.SIM_MODE`), not separate code paths.** `OrderRouter._sim_execute` writes a trade row with `sim_mode=True`; `_live_execute` is currently a stub. The kill switch (`execution/kill_switch.py:KillSwitch.engage`) is the only thing that bypasses approval — it closes every open trade in parallel via `asyncio.gather`.

### Database

SQLite at `data/cryptobot.db` via SQLAlchemy 2.x (`database/db.py`). WAL mode and foreign keys are enabled in a `connect` event listener. All DB access goes through `database/queries.py` — do not open sessions directly in feature code; use the `get_session()` context manager or extend `queries.py`. `init_db()` is idempotent and called on every startup.

Schema lives in `database/models.py`. Note `Signal` has 1:1 relationships to `Trade` and `Prediction` — when adding a column, add it both to the ORM model and to `Signal.to_db_dict()` in `signals/base.py`.

### Claude agent

`core/agent.py` builds a markdown brief from the `Signal` + regime + OFI snapshots, calls the Anthropic SDK synchronously inside an `async` method, and parses the response with regex looking for `RECOMMENDATION: GO` or `RECOMMENDATION: SKIP` plus `entry/stop loss/take profit` numbers. The model, max tokens, and temperature come from `settings.CLAUDE_MODEL/MAX_TOKENS/TEMPERATURE`. `CLAUDE_MAX_DAILY_COST_USD` is alert-only — it does not halt trading.

### Repo state caveat

The README describes a fuller tree than currently exists. Several modules listed there (e.g. most of `sentiment/`, `ui/`, `predictive/`, some `signals/*.py`) are empty `__init__.py` stubs or referenced-but-missing imports. Expect to scaffold a module when wiring something new, and verify imports resolve before assuming a file is present.
