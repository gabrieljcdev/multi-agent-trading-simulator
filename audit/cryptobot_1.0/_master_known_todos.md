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

