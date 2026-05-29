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
