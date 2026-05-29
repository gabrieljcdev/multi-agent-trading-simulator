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
