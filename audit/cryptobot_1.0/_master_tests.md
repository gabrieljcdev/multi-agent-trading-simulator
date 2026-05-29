---

## Tests at snapshot time

- **Result:** 552 passed, 1 warning, 0 failures, ≈12.12s wallclock.
- **Test files:** 30 (under `tests/`).
- **Configuration:** `pytest.ini` scopes to `tests/` only (excludes `scalping_v2/test_scalping_v2.py` to avoid duplicate-basename collision with `tests/test_scalping_v2.py`).
- **Warning:** `tests/test_chain_connectors.py::test_is_available_false_without_rpc_env_var` emits one `DeprecationWarning` from `websockets.legacy` (transitive via `web3 → websockets`).

### Per-test-file summary

| Test file | Notes |
|-----------|-------|
| `tests/test_arb_engine.py` | ArbEngine fills, gas breakeven, slippage, capped notional, circuit breaker logging. |
| `tests/test_balance_agent.py` | BalanceAgent loop, planner integration, rebalance arming/confirm/expire/cancel. |
| `tests/test_bot.py` | CryptoBot construction with injected mocks (router, kill_switch, db_queries); approval-mode dispatch; CB logging path. |
| `tests/test_chain_connectors.py` | Each connector's `is_available()` env gating; one warning here. |
| `tests/test_coordinator.py` | Coordinator agent registration, start/stop, monitor loop. |
| `tests/test_crosschain_agent.py` | CrossChainArbAgent loop + capital allocation. |
| `tests/test_crosschain_engine.py` | Engine candidate scoring, inventory, CB logging. |
| `tests/test_dashboard.py` | Dashboard snapshot construction; mocked coordinator state. |
| `tests/test_data_sources.py` | Plugin discovery via `REGISTERED_SOURCES`, subscription dispatch, refresh-loop semantics. |
| `tests/test_equity_reconstruction.py` | Bankroll = starting + realised PnL; UTC-midnight handling. |
| `tests/test_funding_arb.py` | FundingArbAgent loop; Phase 1 observation-mode invariants. |
| `tests/test_funds.py` | Capital movement accounting. |
| `tests/test_inventory_targets.py` | `execution.inventory.compute_inventory_targets` math. |
| `tests/test_macro.py` | MacroMonitor regime classification + calendar event awareness. |
| `tests/test_market_data_stream.py` | MarketData order-book stream lifecycle + error backoff. |
| `tests/test_mexc_key_router.py` | Per-key MEXC routing decisions. |
| `tests/test_prompts.py` | ApprovalInputHandler keypress mapping (mocked Bot). |
| `tests/test_quality_gate.py` | QualityGate composite scoring; modifier addition order. |
| `tests/test_queries.py` | Read/write query helpers; FK & WAL invariants; kill_switch + position_manager writers. |
| `tests/test_scalp_activation.py` | Scalp-agent gating by strategy/exchange. |
| `tests/test_scalp_pair_coverage.py` | Pair-coverage logic for `STRATEGY_EXCHANGE_MAP`. |
| `tests/test_scalp_v2_accessors.py` | v2 accessor helpers (`evaluate_signal_v2` / `is_ready_for_live_v2`). |
| `tests/test_scalp_v2_integration.py` | Confluence + ATR-SL gates wired into the agent flow. |
| `tests/test_scalping_agent.py` | Scalping agent loop & queue semantics. |
| `tests/test_scalping_v2.py` | v2 integration tests (the in-tree copy; the `scalping_v2/test_scalping_v2.py` standalone copy is excluded by `pytest.ini`). |
| `tests/test_sentiment.py` | Aggregator composite math + source weight ladder. |
| `tests/test_signal_arbitrage.py` | Arb scanner score + ranking. |
| `tests/test_web_server.py` | Web routes, action dispatch, snapshot construction, rebalance arm/confirm flow, scalp closed-trades persistence. |

No tests fail. The full raw output is in `Appendix B`.

### Coverage gaps observed during the audit (from module reports)

These were noted while writing the per-package reports; they don't fail the suite but they're worth recording:

- `strategies/*` — zero direct unit tests; only consumed via mocked `SimpleNamespace` in `test_bot.py` / `test_dashboard.py`.
- `main.py` — no test exercises startup or the SIGINT shutdown path (the very bug recorded in `MEMORY.md → project_shutdown_hang` that the current `main.py:135-141` rework fixes).
- `predictive/` — empty package; no tests because no code.
- `core/agent.py` Claude failure path — `module_reports/core.md` flags an "agent fail-open" behaviour (no signal.indicators["claude_rec"] on exception → autonomous/window still executes) that isn't covered by a test.
- `signals/quality_gate.py:104-105` double-counting OFI when scanners already folded it into `sentiment_mod` — flagged in `module_reports/signals.md` but not under test.

---

## Documentation map

Reproduced from `docs_inventory.md` (Phase 6).

---

