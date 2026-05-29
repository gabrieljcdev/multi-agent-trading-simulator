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
