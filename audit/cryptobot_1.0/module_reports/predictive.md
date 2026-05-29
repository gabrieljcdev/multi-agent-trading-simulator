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
