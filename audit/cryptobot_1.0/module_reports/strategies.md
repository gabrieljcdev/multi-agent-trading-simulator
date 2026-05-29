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
