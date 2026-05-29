# Module Report: agents

## Purpose
The `agents/` package is the project's trading-agent layer: it defines the `BaseAgent` ABC, an agent-agnostic `Coordinator` that orchestrates a roster of registered agents, six concrete agents (Signal/Arb wrappers around the existing CryptoBot/ArbEngine plus Scalping, Cross-Chain Arb, Funding-Rate Arb, and Balance), and the BalanceAgent's internal `balance/` sub-tree (ledger state, planner, policy plugins, and transfer rails). Agents share state through module-level singletons, are picked up automatically through `REGISTERED_AGENTS`, and must satisfy a small async lifecycle contract (`start`, `stop`, `get_stats`, `close_all_positions`).

## Subpackages
- `agents/balance/` — fund-aware ledger view (`InventoryState`), the rebalance `Planner` (Greedy/Miller-Orr), and the policy + rails plugin sub-trees consumed by `BalanceAgent`.
- `agents/balance/policy/` — `BasePolicy` contract, `InventoryTarget` dataclass, and the default `GrowthOptimalPolicy` (fractional Kelly blended with risk-parity).
- `agents/balance/rails/` — `BaseTransferRail` + `TransferResult` contract and two rails: `SimTransferRail` (atomic with simulated fee/delay/failure) and `CexTransferRail` (live-stubbed full state machine).

## Files
| File | LOC | One-sentence summary |
| --- | --- | --- |
| agents/__init__.py | 416 | Concrete `SignalAgentWrapper` + `ArbAgentWrapper` plus the `REGISTERED_AGENTS` roster the Coordinator iterates. |
| agents/base.py | 231 | `AgentStats` dataclass, lifecycle constants (`RUNNING/PAUSED/HALTED/OFFLINE/STOPPED`), `BaseAgent` ABC with BalanceAgent compounding hooks, and `PlaceholderAgent`. |
| agents/coordinator.py | 379 | `Coordinator` — starts agents concurrently, runs per-fund + portfolio circuit breakers, propagates dashboard, exposes kill-all/stats. |
| agents/balance_agent.py | 765 | The `BalanceAgent` operational fund-controller: realised-P&L compounding, policy/planner/rails dispatch, six safety rails, web-UI arm/confirm two-step. |
| agents/crosschain_agent.py | 232 | Thin wrapper over `execution.crosschain_engine.CrossChainArbEngine`; observation-mode by default and exposes `get_inventory_targets()`. |
| agents/funding_arb_agent.py | 400 | Funding-rate arb Phase-1 observation agent wrapping `execution.funding_engine`. |
| agents/scalping_agent.py | 1779 | OFI-driven scalping agent — `FeeManager`, multi-level `OFIEngine`, 13 entry gates + v2 selectivity layer, sim ordering, micro-price tracker. |
| agents/scalping_agent_v2_integration.py | 246 | Reference/demo module documenting the v2 integration and offering `evaluate_signal_v2()` + `is_ready_for_live_v2()` helpers — not wired into production. |
| agents/scalping_atr_sl.py | 109 | `ATRStopCalculator` + `TpSlV2` — volatility-aware TP/SL with floor/ceiling clamps. |
| agents/scalping_confluence.py | 410 | `ConfluenceChecker` (VWAP/HTF/Volume/CrossEx/BTC/Adverse/Depth gates) and result dataclasses for scalp v2. |
| agents/balance/__init__.py | 19 | Re-exports `InventoryState` and the `inventory_state` singleton. |
| agents/balance/inventory_state.py | 358 | Fund-aware ledger view; `InventoryState` with `effective_balance`, `can_arb`, in-flight transfer + paused-route tracking. |
| agents/balance/planner.py | 358 | `BaseRebalancePlanner` ABC + default `GreedyNetPlanner` (internalize → net → Miller-Orr band → greedy match); `Transfer` + `PlannerConstraints` dataclasses. |
| agents/balance/policy/__init__.py | 36 | Re-exports `BasePolicy`, `InventoryTarget`, and `REGISTERED_POLICIES` (`[GrowthOptimalPolicy()]`). |
| agents/balance/policy/base.py | 75 | `BasePolicy` ABC and `InventoryTarget` dataclass. |
| agents/balance/policy/growth_optimal.py | 269 | `GrowthOptimalPolicy` — fractional Kelly × risk-parity blend; emits one `InventoryTarget` per `(fund, exchange, USDT)`. |
| agents/balance/rails/__init__.py | 48 | Re-exports `BaseTransferRail`, `TransferResult`, and `REGISTERED_RAILS` (`[SimTransferRail(), CexTransferRail()]`). |
| agents/balance/rails/base.py | 63 | `BaseTransferRail` ABC and `TransferResult` dataclass. |
| agents/balance/rails/cex_rail.py | 287 | `CexTransferRail` — full pending→in_transit→completed/failed state machine; `ccxt.withdraw` stubbed behind `REBALANCE_LIVE_ENABLED`. |
| agents/balance/rails/sim_rail.py | 146 | `SimTransferRail` — atomic sim transfers with simulated fee, delay, and injectable failure. |

## Public surface

### agents/__init__.py
**Docstring:** Registry of trading agents the Coordinator manages by default; documents the plugin pattern for adding new agents and the rationale for keeping concrete wrappers out of the Coordinator.

**Classes:**
- `SignalAgentWrapper(BaseAgent)` — wraps the existing `core.bot.CryptoBot`; primary (non-optional) signal agent.
  - class attrs: `agent_id: str = "signal"`, `display_name: str = "Signal Agent"`, `optional: bool = False`
  - `__init__(self)` — pulls `settings.SIGNAL_AGENT_CAPITAL`; bot and dashboard set lazily.
  - `bot` (property) — exposes the underlying `CryptoBot`.
  - `set_dashboard(self, dashboard) -> None`
  - `is_available(self) -> bool` — True in sim mode; in live mode requires at least one `<EX>_API_KEY` env var across `ENABLED_EXCHANGES`.
  - `async start(self) -> None` — instantiates `CryptoBot(kill_switch=KillSwitch(sim_mode=SIM_MODE))` and runs `_bot.start()`.
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None` — triggers the bot's kill switch.
  - `get_open_position_notional(self) -> float`
  - `set_capital_allocation(self, amount: float) -> bool` — propagates to `OrderRouter.update_portfolio_value` if available.
  - `async get_stats(self) -> AgentStats` — DB-derived from `signal` strategy trades; falls back to in-memory CB state when DB unreachable.
- `ArbAgentWrapper(BaseAgent)` — wraps `execution.arb_engine.ArbEngine`.
  - class attrs: `agent_id: str = "arb"`, `display_name: str = "Arb Agent"`, `optional: bool = True`
  - `__init__(self)` — pulls `settings.ARB_AGENT_CAPITAL`.
  - `is_available(self) -> bool` — needs `ArbEngine` importable and ≥2 exchanges (sim) or ≥2 keyed exchanges (live).
  - `set_dashboard(self, dashboard) -> None`
  - `async start(self) -> None` — boots `ArbEngine(dashboard=..., fund_id="arb", exchanges=STRATEGY_EXCHANGE_MAP["arb"])` and reconstructs persisted P&L.
  - `_reconstruct_engine_pnl(engine) -> None` (staticmethod)
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None`
  - `get_open_position_notional(self) -> float` — approximates as `active_arbs × ARB_BASE_POSITION_USD`.
  - `set_capital_allocation(self, amount: float) -> bool`
  - `async get_stats(self) -> AgentStats`

**Functions:** none.

**Module constants:**
- `REGISTERED_AGENTS: list[BaseAgent] = [SignalAgentWrapper(), ArbAgentWrapper(), ScalpingAgent(), CrossChainArbAgent(), FundingArbAgent(), BalanceAgent()]`
- `__all__` lists the wrappers + concrete agents + dataclasses.

### agents/base.py
**Docstring:** Defines the two contracts every trading agent satisfies (`AgentStats`, `BaseAgent`) plus `PlaceholderAgent`. Mirrors the BaseSentimentSource plugin pattern.

**Classes:**
- `AgentStats` — `@dataclass` capturing one agent's snapshot.
  - fields: `agent_id: str`, `status: str`, `capital_allocated: float`, `capital_deployed: float`, `daily_pnl: float`, `daily_pnl_pct: float`, `total_pnl: float`, `trades_today: int`, `win_rate_today: float`, `win_rate_alltime: float`, `consecutive_losses: int`, `last_trade_time: Optional[str]`, `error: Optional[str]`.
- `BaseAgent(ABC)` — every concrete trading agent.
  - class attrs: `agent_id: str = "base"`, `display_name: str = "Base Agent"`, `capital_allocation: float = 0.0`, `optional: bool = True`
  - `__init__(self)` — initialises `_status=OFFLINE`, `_start_time=None`, `_error=None`.
  - `@abstractmethod async start(self) -> None`
  - `@abstractmethod async stop(self) -> None`
  - `@abstractmethod async get_stats(self) -> AgentStats`
  - `@abstractmethod async close_all_positions(self) -> None`
  - `is_available(self) -> bool` — default True.
  - `get_capital_allocation(self) -> float`
  - `set_capital_allocation(self, amount: float) -> bool` — refuses below `get_open_position_notional`.
  - `get_open_position_notional(self) -> float` — default 0.0.
  - `async pause(self) -> None`
  - `async resume(self) -> None`
  - `status` (property) -> `str`
  - `uptime_seconds` (property) -> `float`
- `PlaceholderAgent(BaseAgent)` — base for dashboard-only agents.
  - class attrs: `optional: bool = True`
  - `is_available(self) -> bool` — always False.
  - `async start(self) -> None`
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None`
  - `async get_stats(self) -> AgentStats` — zeroed OFFLINE stats.

**Functions:** none.

**Module constants:** lifecycle strings — `RUNNING = "RUNNING"`, `PAUSED = "PAUSED"`, `HALTED = "HALTED"`, `OFFLINE = "OFFLINE"`, `STOPPED = "STOPPED"`.

### agents/coordinator.py
**Docstring:** Multi-agent coordinator. Agent-agnostic by design — only the `BaseAgent` contract. Starts agents concurrently, aggregates portfolio stats, runs portfolio-wide CBs, and persists snapshots/events.

**Classes:**
- `Coordinator` — orchestrates a roster of `BaseAgent` instances.
  - `__init__(self, agents: Optional[list[BaseAgent]] = None, dashboard=None)` — defaults to `REGISTERED_AGENTS` (lazy import); calls `_check_capital_sum`.
  - `async start(self) -> None` — kicks each available agent + the monitor loop concurrently.
  - `async stop(self) -> None`
  - `async kill_all(self, reason: str = "manual") -> dict`
  - `async get_agent_stats(self) -> list[AgentStats]` — sorted by capital descending.
  - `async get_portfolio_stats(self) -> dict` — aggregates equity, daily PnL%, exposure %, weighted win rate, portfolio status.
  - `get_agent(self, agent_id: str) -> Optional[BaseAgent]`
  - `get_primary_bot(self)` — returns `signal` agent's `bot`.
  - `set_dashboard(self, dashboard) -> None`
  - `async _monitor_loop(self) -> None`
  - `async _check_fund_circuit_breakers(self, agent_stats: list[AgentStats]) -> None` — per-fund halt at `-FUND_DAILY_LOSS_HALT_PCT`.
  - `async _check_portfolio_circuit_breakers(self, stats: dict) -> None` — global halt at `-PORTFOLIO_DAILY_LOSS_HALT_PCT`.
  - `async _safe_call(self, agent: BaseAgent, method_name: str) -> bool`
  - `async _safe_close(self, agent: BaseAgent) -> bool`
  - `async _safe_pause(self, agent: BaseAgent) -> bool`
  - `async _safe_get_stats(self, agent: BaseAgent) -> AgentStats`
  - `_check_capital_sum(self) -> None`
  - `_log_event(self, agent_id: str, event_type: str, detail: str) -> None`
  - `_log_snapshot(self, stats: dict) -> None`
  - `async _propagate_dashboard(self) -> None`

**Functions:** none.

**Module constants:** none.

### agents/balance_agent.py
**Docstring:** The `BalanceAgent` — operational (no alpha). Owns fund × exchange capital position; each scan refreshes per-fund equity, compounds via `set_capital_allocation`, asks the active policy for targets, hands to the planner, dispatches via rails, and logs efficiency rows. Documents the seven safety rails and the kill-switch contract (block new transfers; in-flight settles).

**Classes:**
- `BalanceAgent(BaseAgent)` — operational fund controller.
  - class attrs: `agent_id: str = "balance"`, `display_name: str = "Balance Agent"`, `optional: bool = True`
  - `__init__(self, *, policies: Optional[list[BasePolicy]] = None, rails: Optional[list[BaseTransferRail]] = None, planner: Optional[BaseRebalancePlanner] = None)` — pulls `BALANCE_AGENT_CAPITAL`, defaults to `REGISTERED_POLICIES`, `REGISTERED_RAILS`, `GreedyNetPlanner()`.
  - `is_available(self) -> bool` — always True.
  - `async start(self) -> None` — kicks `_loop` task and runs cex_rail `load_in_transit` reconciliation.
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None` — sets `_paused=True`.
  - `async get_stats(self) -> AgentStats` — `daily_pnl`/`total_pnl` always 0; reuses `trades_today`/`consecutive_losses` to count dispatched / failed transfers.
  - `arm(self) -> tuple[str, dict]` — issues a `secrets.token_hex(8)` token + ring-fence notice; valid for `REBALANCE_CONFIRM_WINDOW_S`.
  - `consume_arm(self, token: str) -> bool` — fail-closed token check.
  - `_ring_fence_notice(self) -> dict`
  - `get_pending_proposal(self) -> dict` — web-UI snapshot of the latest buffered plan.
  - `async execute_proposal(self, confirm_token: str) -> dict` — re-plans on confirm; documented error codes include `kill_blocked`, `token_invalid`, `token_expired`, `live_rebalance_disabled`, `no_pending_plan`, `plan_changed`, `replan_failed:<err>`, `all_blocked_by_safety`.
  - `_plans_substantially_equal(old: list, new: list, tol: float = 0.10) -> bool` (staticmethod)
  - `async _loop(self) -> None`
  - `async _scan_once(self) -> None`
  - `_compound_realised_into_funds(self) -> float`
  - `_safety_clear(self, t: Transfer) -> bool` — rail 2/3 (open-position floor / strict block).
  - `async _dispatch(self, t: Transfer) -> Optional[int]`
  - `_pick_policy(self) -> Optional[BasePolicy]`
  - `_pick_rail(self, t: Transfer) -> Optional[BaseTransferRail]`
  - `_count_in_flight(self) -> int`
  - `_cost_matrix() -> dict` (staticmethod)
  - `_find_sibling(agent_id: str) -> Optional[BaseAgent]` (staticmethod)
  - `_find_sibling_for_fund(self, fund_id: str) -> Optional[BaseAgent]`
  - `_check_daily_reset(self) -> None`

**Functions:** none.

**Module constants:** none.

### agents/crosschain_agent.py
**Docstring:** Thin `BaseAgent` wrapper over `execution.crosschain_engine.CrossChainArbEngine`. Documents the observation-mode invariant (`XCHAIN_CAPITAL == 0`) and how `get_inventory_targets()` exposes the future BalanceAgent interface.

**Classes:**
- `CrossChainArbAgent(BaseAgent)`
  - class attrs: `agent_id: str = "xchain"`, `display_name: str = "Cross-Chain Arb"`, `optional: bool = True`
  - `__init__(self)` — `capital_allocation = float(settings.XCHAIN_CAPITAL)`.
  - `is_available(self) -> bool` — requires ≥2 available connectors from `execution.chains.REGISTERED_CONNECTORS`.
  - `observation_mode` (property) -> bool — True when `XCHAIN_CAPITAL <= 0`.
  - `set_dashboard(self, dashboard) -> None`
  - `async start(self) -> None`
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None`
  - `async get_stats(self) -> AgentStats`
  - `_zero_stats(self, *, status: str, error: Optional[str] = None) -> AgentStats`
  - `get_inventory_targets(self, current_balances: Optional[dict[str, dict[str, float]]] = None, *, lookback_observations: int = 500) -> list[InventoryTarget]` — reads `db_queries.get_xchain_observations` and delegates to `execution.inventory.compute_inventory_targets`.

**Functions:** none.

**Module constants:** `__all__ = ["CrossChainArbAgent"]`.

### agents/funding_arb_agent.py
**Docstring:** Funding-rate arb agent (Phase 1 observation-mode only). Routes nothing while `FUNDING_OBSERVATION_MODE`; uses Binance public CCXT.

**Classes:**
- `FundingArbAgent(BaseAgent)`
  - class attrs: `agent_id: str = "funding_arb"`, `display_name: str = "Funding-Rate Arb"`, `optional: bool = True`
  - `__init__(self, engine: Optional[FundingEngine] = None)` — pulls `FUNDING_CAPITAL_USD`; default engine is the module-level `funding_engine` singleton.
  - `is_available(self) -> bool` — always True.
  - `observation_mode` (property) -> bool — reads `settings.FUNDING_OBSERVATION_MODE`.
  - `async start(self) -> None`
  - `async stop(self) -> None` — also closes the engine's binance ccxt client.
  - `async close_all_positions(self) -> None`
  - `async get_stats(self) -> AgentStats`
  - `get_observation_summary(self) -> dict`
  - `async _loop(self) -> None`
  - `async _manage_open_positions(self) -> None`
  - `async _close(self, symbol: str, pos: FundingPosition, reason: str) -> None`
  - `async _log_observation(self, opp: FundingOpportunity) -> None`
  - `_check_daily_reset(self) -> None`
  - `_check_circuit_breakers(self) -> None`

**Functions:** none.

**Module constants:** module logger `log = logging.getLogger(__name__)`.

### agents/scalping_agent.py
**Docstring:** OFI-primary scalping agent; documents exchange routing, fee-aware TP/SL formula, observation mode, live-wiring expectations for market_data accessors, and Phase 2 dashboard TODO.

**Classes:**
- `_LazyMarketData` (`__slots__ = ("_resolve",)`) — proxy that re-resolves the agent's `MarketData` on every attribute access so the v2 checkers (built in `__init__`) reach the live feed.
  - `__init__(self, resolver)`
  - `__getattr__(self, name)`
- `BookSnap` — `@dataclass` with `ts: float`, `bids: list`, `asks: list`.
- `ScalpPosition` — `@dataclass` carrying entry/exit metadata and `trade_id: Optional[int] = None`.
- `ScalpObservation` — `@dataclass` recording every evaluation (entry or skip) plus v2 selectivity diagnostics (confluence_score, strength_label, cross_exchange_agrees, btc_compatible, adverse_selection_ok, depth_ok, vwap_aligned, htf_aligned, volume_adequate, atr_bps, atr_adjusted, sl_clamped, rr_actual — all `Optional` so older rows stay valid).
- `FeeManager` — caches per-(exchange,symbol) maker/taker fees, applies overrides, and derives dynamic TP/SL + breakeven win rate.
  - `__init__(self, overrides: dict)`
  - `async load_exchange(self, exchange_id: str, ccxt_exchange: Any) -> None`
  - `get_fees(self, exchange_id: str, symbol: str) -> dict`
  - `round_trip_bps(self, exchange_id: str, symbol: str) -> float`
  - `compute_tp_sl(self, exchange_id: str, symbol: str) -> tuple[float, float]`
  - `breakeven_win_rate(self, exchange_id: str, symbol: str, tp_bps: float, sl_bps: float) -> float`
  - `is_viable(self, exchange_id: str, symbol: str) -> tuple[bool, str]`
- `OFIEngine` — Cont-Kukanov-Stoikov multi-level OFI estimator keyed by `(symbol, exchange)`.
  - `__init__(self, levels: int, window_sec: float, zscore_window: int)`
  - `_key(symbol: str, exchange: str) -> str` (staticmethod)
  - `_maybe_close_bucket(self, key: str, now: float) -> None`
  - `_compute_e_n(self, prev: BookSnap, curr: BookSnap) -> float`
  - `on_book(self, symbol: str, exchange: str, bids: list, asks: list) -> None`
  - `on_trade(self, symbol: str, exchange: str, side: str, qty: float) -> None`
  - `get(self, symbol: str, exchange: str) -> dict` — `{z, direction, strength, tfi_confirms, raw_tfi, age_sec, stale}`.
  - `update_direction_ticks(self, symbol: str, exchange: str, entry_z: float) -> int`
  - `get_z_score(self, symbol: str, exchange: str)` — Optional[float].
  - `get_exchanges_for_symbol(self, symbol: str) -> list`
- `ScalpingAgent(BaseAgent)` — main scalper.
  - class attrs: `agent_id: str = "scalp"`, `display_name: str = "Scalping Agent (OFI)"`, `optional: bool = True`
  - `__init__(self, sentiment_source: Optional[Any] = None, market_data: Optional[Any] = None, regime_detector: Optional[Any] = None)`
  - `is_available(self) -> bool`
  - `get_capital_allocation(self) -> float` — `max(capital_allocation, _capital)`.
  - `set_capital_allocation(self, amount: float) -> bool`
  - `get_open_position_notional(self) -> float`
  - `set_market_data(self, market_data) -> None`
  - `set_regime_detector(self, regime_detector) -> None`
  - `set_sentiment_source(self, sentiment_source) -> None`
  - `_resolve_market_data(self)`
  - `_ensure_book_subscription(self, md) -> None`
  - `_resolve_regime_detector(self)`
  - `_reconstruct_pnl(self) -> None`
  - `async start(self) -> None`
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None`
  - `_win_rate_today(self) -> float`
  - `_win_rate_alltime(self) -> float`
  - `async get_stats(self) -> AgentStats`
  - `on_book(self, symbol: str, exchange: str, bids: list, asks: list) -> None`
  - `on_trade(self, symbol: str, exchange: str, side: str, qty: float) -> None`
  - `async _get_mid_price(self, symbol: str, exchange: str) -> float`
  - `async _get_spread_bps(self, symbol: str, exchange: str) -> float`
  - `async _get_regime(self, symbol: str) -> str`
  - `async _get_ccxt_exchange(self, exchange_id: str, symbol: Optional[str] = None)` — MEXC routes through `mexc_key_router`.
  - `async _get_btc_1m_change(self) -> float`
  - `async _place_order(self, pos: "ScalpPosition") -> Optional[int]` — sim path persists a Trade row.
  - `async _loop(self) -> None`
  - `_pos_key(symbol: str, exchange: str) -> str` (staticmethod)
  - `async _micro_price_tracker_loop(self) -> None`
  - `async _micro_price_tracker_pass(self) -> None`
  - `async _evaluate_entry(self, symbol: str, exchange: str) -> None` — runs the 13 gates plus v2 selectivity (when `SCALP_USE_CONFLUENCE`) and ATR-aware TP/SL.
  - `_unpack_confluence(conf) -> dict` (staticmethod)
  - `_annotate_v2(obs, fields: dict, tpsl=None) -> None` (staticmethod)
  - `_log_skip(self, symbol: str, exchange: str, ts: float, ofi: Optional[dict], reason: str, rt_bps: float, min_wr: float, spread_bps: float, regime: str) -> None`
  - `_make_observation(self, symbol: str, exchange: str, ts: float, ofi: Optional[dict], would_entry: bool, skip_reason: str, entry_price: float, tp_bps: float, sl_bps: float, rt_bps: float, min_wr: float, spread_bps: float, regime: str) -> ScalpObservation`
  - `async _manage_position(self, pos_key: str) -> None`
  - `async _exit_position(self, pos_key: str, pos: ScalpPosition, reason: str, exit_price: float = 0.0) -> None`
  - `_halt(self, reason: str) -> None`
  - `_check_daily_reset(self) -> None`
  - `async _flush_observations(self) -> None`
  - `get_observation_summary(self) -> dict`

**Functions:** none.

**Module constants:** logger `log = logging.getLogger(__name__)`.

### agents/scalping_agent_v2_integration.py
**Docstring:** Documents the additive v2 integration with the 13-gate flow, the DB columns needed, and the deployment recipe. Not imported in production aside from the `is_ready_for_live_v2()` helper used by tests.

**Classes:** none.

**Functions:**
- `evaluate_signal_v2(agent, signal, confluence_checker, atr_calc) -> Optional[Dict[str, Any]]` — reference replacement for `_evaluate_signal`.
- `_initial_observation(signal) -> Dict[str, Any]`
- `_run_legacy_gates(agent, signal, obs) -> Optional[Dict[str, Any]]`
- `_compute_position_size_usd(agent, signal) -> float`
- `is_ready_for_live_v2(stats: Dict[str, Any], settings) -> Dict[str, Any]` — activation gate using `SCALP_*_FOR_LIVE_V2` thresholds (min observations 300, min win_rate 0.55, min avg_net_bps 0.5, max_hold_pct 0.25, min directional_acc_1m 0.57).

**Module constants:**
- `DB_COLUMNS_V2: str` — multi-line `ALTER TABLE` statements documenting the v2 columns.

### agents/scalping_atr_sl.py
**Docstring:** ATR-aware stop loss calculator replacing the fixed-bps SL with `max(base_sl, ATR×multiplier)` clamped to `[floor, ceiling]`.

**Classes:**
- `TpSlV2` — `@dataclass` returning `tp_bps`, `sl_bps`, `rr_actual`, `base_sl_bps`, `atr_bps`, `atr_adjusted`, `sl_clamped`.
- `ATRStopCalculator`
  - `__init__(self, market_data, settings)`
  - `compute_tp_sl_v2(self, symbol: str, exchange: str, round_trip_bps: float) -> TpSlV2`
  - `_safe_atr_bps(self, symbol: str, exchange: str)` — returns `Optional[float]` (bps of mid).

**Functions:** none.

**Module constants:** logger `logger = logging.getLogger("scalping_v2.atr_sl")` (note: logger name uses the `scalping_v2.` prefix even when imported from `agents.`).

### agents/scalping_confluence.py
**Docstring:** Stateless confluence/selectivity gates (VWAP, HTF EMA, Volume, Cross-Exchange OFI, BTC directional, Adverse selection, Depth) wrapped in `ConfluenceChecker` for DI.

**Classes:**
- `ConfluenceResult` — `@dataclass` (`passed`, `reason`, `score`, `gate_name`, `metadata`).
  - `__str__(self)` — `[✓ NAME] reason (score=X.XX)`.
- `CombinedConfluenceResult` — `@dataclass` carrying overall verdict, `confluence_score`, `strength_label`, per-gate booleans, `individual_results`, `metadata`.
- `ConfluenceChecker`
  - `__init__(self, market_data, ofi_engine, settings)`
  - `check_vwap_alignment(self, symbol: str, exchange: str, direction: str) -> ConfluenceResult`
  - `check_htf_trend(self, symbol: str, exchange: str, direction: str) -> ConfluenceResult`
  - `check_volume(self, symbol: str, exchange: str) -> ConfluenceResult`
  - `check_cross_exchange_ofi(self, symbol: str, primary_exchange: str, direction: str, primary_z: float) -> ConfluenceResult`
  - `check_btc_directional(self, symbol: str, exchange: str, direction: str) -> ConfluenceResult`
  - `check_adverse_selection(self, symbol: str, exchange: str, direction: str) -> ConfluenceResult`
  - `check_depth(self, symbol: str, exchange: str, position_size_usd: float) -> ConfluenceResult`
  - `run_all_gates(self, symbol: str, exchange: str, direction: str, primary_z: float, position_size_usd: float) -> CombinedConfluenceResult`
  - `_build_combined(self, passed, reason, results, adverse, depth, btc, cross, soft_passed=0) -> CombinedConfluenceResult`
  - `_label_strength(soft_passed: int, cross: Optional[ConfluenceResult]) -> str` (staticmethod) — WEAK / MODERATE / STRONG / VERY_STRONG.

**Functions:** none.

**Module constants:** logger `logger = logging.getLogger("scalping_v2.confluence")`.

### agents/balance/__init__.py
**Docstring:** Documents the four strictly-separated layers — policy → planner → rails → state — and the plugin pattern.

**Classes:** none defined here (re-exports only).

**Functions:** none.

**Module constants:** `__all__ = ["InventoryState", "inventory_state"]`.

### agents/balance/inventory_state.py
**Docstring:** Fund-aware ledger view. Documents why per-fund claims are needed on shared venues, the two-band `can_arb` gate, and the singleton pattern.

**Classes:**
- `_PendingTransfer` — `@dataclass` with `movement_id, from_fund, to_fund, from_exchange: Optional[str], to_exchange: Optional[str], amount_usd, asset, state, opened_at: float = field(default_factory=time.time)`.
- `InventoryState` — singleton.
  - `__init__(self)` — initialises `_lock=RLock()`, `_claims`, `_allocations`, `_transfers`, `_paused_routes` maps.
  - `effective_balance(self, fund: str, exchange: str, asset: str = "USDT") -> float` — physical − other funds' claims − pending out + pending in (oversubscribed branch scales pro-rata).
  - `get_allocation(self, fund: str, exchange: str) -> float`
  - `is_arb_halted(self, pair: str, side: str = "buy") -> bool`
  - `can_arb(self, pair: str, side: str, size: float, *, buy_exchange: Optional[str] = None, sell_exchange: Optional[str] = None, fund: str = "arb") -> bool`
  - `get_snapshot(self) -> dict`
  - `apply_allocation(self, fund: str, exchange: str, asset: str, target_usd: float) -> None`
  - `open_transfer(self, movement_id: int, from_fund: str, to_fund: str, amount_usd: float, *, from_exchange: Optional[str] = None, to_exchange: Optional[str] = None, asset: str = "USDT", state: str = "in_transit") -> None`
  - `settle_transfer(self, movement_id: int) -> None`
  - `fail_transfer(self, movement_id: int, reason: str = "") -> None`
  - `pause_route(self, buy_exchange: str, sell_exchange: str, reason: str) -> None`
  - `clear_route_pause(self, buy_exchange: str, sell_exchange: str) -> None`
  - `reset(self) -> None` — test-only.

**Functions:** none.

**Module constants:** `inventory_state = InventoryState()` (singleton).

### agents/balance/planner.py
**Docstring:** Documents the 4-step planning pipeline (internalize → net → derived Miller-Orr band → greedy match) and the seam for a future min-cost-flow solver.

**Classes:**
- `Transfer` — `@dataclass` with `from_fund, to_fund, from_exchange, to_exchange: str`, `amount_usd: float`, `asset: str = "USDT"`, `cost_usd: float = 0.0`, `note: str = ""`.
- `PlannerConstraints` — `@dataclass` with `daily_limit: int = 3`, `daily_used: int = 0`, `in_flight: int = 0`, `floor_overrides: dict = field(default_factory=dict)`.
- `BaseRebalancePlanner(ABC)`
  - class attrs: `planner_id: str = "base"`, `display_name: str = "Base Planner"`
  - `@abstractmethod plan(self, inv: "InventoryState", targets: list["InventoryTarget"], cost_matrix: dict, constraints: PlannerConstraints) -> list[Transfer]`
- `GreedyNetPlanner(BaseRebalancePlanner)`
  - class attrs: `planner_id: str = "greedy_net"`, `display_name: str = "Greedy Net Planner (Miller-Orr)"`
  - `plan(self, inv, targets, cost_matrix, constraints) -> list[Transfer]` — try/except wrapper returning `[]` on failure.
  - `_plan_inner(self, inv, targets, cost_matrix, constraints) -> list[Transfer]`
  - `_filter_to_structural(targets) -> list["InventoryTarget"]` (staticmethod)
  - `_net_residual(inv, targets) -> dict[tuple[str, str], float]` (staticmethod)
  - `_miller_orr_band(tgt) -> float` (staticmethod) — returns full spread; caller takes `half = band/2.0`.
  - `_edge_cost(cost_matrix, edge, amount_usd, *, src_cur, floor) -> float` (staticmethod) — doubles cost when headroom < 10%.

**Functions:** none.

**Module constants:** none.

### agents/balance/policy/__init__.py
**Docstring:** Policy plugin entry point; documents how to add a new policy.

**Module constants:** `REGISTERED_POLICIES: list[BasePolicy] = [GrowthOptimalPolicy()]`; `__all__` re-exports `BasePolicy`, `InventoryTarget`, `GrowthOptimalPolicy`, `REGISTERED_POLICIES`.

### agents/balance/policy/base.py
**Docstring:** Defines `BasePolicy` + `InventoryTarget` as the universal contract.

**Classes:**
- `InventoryTarget` — `@dataclass` with `fund: str`, `exchange: str`, `asset: str`, `target_usd: float`, `floor_usd: float`, `cap_usd: float`, `drift_pct: float`, `needs_rebalance: bool`.
- `BasePolicy(ABC)`
  - class attrs: `policy_id: str = "base"`, `display_name: str = "Base Policy"`
  - `is_available(self) -> bool` — default True.
  - `@abstractmethod compute_targets(self, inv: "InventoryState", equity: float) -> list[InventoryTarget]`

**Functions:** none.

**Module constants:** none.

### agents/balance/policy/growth_optimal.py
**Docstring:** Growth-optimal allocation blended with risk-parity by `ALLOCATION_CONFIDENCE`; uses fractional Kelly (`KELLY_FRACTION`).

**Classes:**
- `GrowthOptimalPolicy(BasePolicy)`
  - class attrs: `policy_id: str = "growth_optimal"`, `display_name: str = "Growth-Optimal (Kelly · Risk-Parity blend)"`
  - `compute_targets(self, inv: "InventoryState", equity: float) -> list[InventoryTarget]`
  - `_compute_targets_inner(self, inv, equity) -> list[InventoryTarget]`

**Functions:**
- `_fund_capital_constant(fund: str) -> float`
- `_fund_edge_estimates() -> dict[str, float]`
- `_blend(growth_w: dict[str, float], parity_w: dict[str, float], alpha: float) -> dict[str, float]`
- `_venue_weights_for_fund(fund: str) -> dict[str, float]`
- `_open_position_floor(fund: str, exchange: str) -> float` — currently returns 0.0 hard-coded; documented sentinel.
- `_capacity_cap(fund: str, exchange: str) -> float`

**Module constants:**
- `_FUNDS_AND_VENUES: dict[str, list[str]] = {"signal": ["binance", "kraken", "bybit", "kucoin"], "arb": ["kraken", "bybit", "bitget", "bitstamp", "gateio", "bitfinex", "mexc"], "mexc_scalp": ["mexc"]}`

### agents/balance/rails/__init__.py
**Docstring:** Rail plugin entry point; documents how to add a new rail and notes the future `CrossChainTransferRail` plug-in slot.

**Module constants:** `REGISTERED_RAILS: list[BaseTransferRail] = [SimTransferRail(), CexTransferRail()]`; `__all__` re-exports `BaseTransferRail`, `TransferResult`, `SimTransferRail`, `CexTransferRail`, `REGISTERED_RAILS`.

### agents/balance/rails/base.py
**Docstring:** Rail contract; rails never raise — always return `TransferResult(success=False, error=...)`.

**Classes:**
- `TransferResult` — `@dataclass` with `success: bool`, `state: str`, `movement_id: Optional[int] = None`, `fee_usd: float = 0.0`, `network: Optional[str] = None`, `tx_hash: Optional[str] = None`, `error: Optional[str] = None`.
- `BaseTransferRail(ABC)`
  - class attrs: `rail_id: str = "base"`, `display_name: str = "Base Rail"`
  - `@abstractmethod is_available(self) -> bool`
  - `@abstractmethod async execute(self, transfer: "Transfer") -> TransferResult`

**Functions:** none.

**Module constants:** none.

### agents/balance/rails/cex_rail.py
**Docstring:** CEX withdrawal rail — full state machine with network selection, hardcoded address allowlist, and `ccxt.withdraw` stub behind `REBALANCE_LIVE_ENABLED`.

**Classes:**
- `CexTransferRail(BaseTransferRail)`
  - class attrs: `rail_id: str = "cex"`, `display_name: str = "CEX Transfer Rail"`
  - `is_available(self) -> bool` — requires `REBALANCE_LIVE_ENABLED`, importable `ccxt.async_support`, populated `WITHDRAWAL_ROUTES`.
  - `_pick_network(from_exchange: str, to_exchange: str, asset: str) -> Optional[str]` (staticmethod)
  - `async execute(self, transfer: "Transfer") -> TransferResult`
  - `async _execute_inner(self, transfer: "Transfer") -> TransferResult`
  - `async _call_ccxt_withdraw(self, transfer: "Transfer", *, network: str, address: str) -> str` — raises `NotImplementedError`.
  - `load_in_transit(self) -> int` — restart reconciliation.
  - `_log_pending(t: "Transfer", *, network: str) -> int` (staticmethod)
  - `_update(movement_id: int, fields: dict) -> None` (staticmethod)

**Functions:** none.

**Module constants:**
- `_WITHDRAWAL_ADDRESSES: dict[tuple[str, str], str] = {}` — hardcoded operator-provisioned address allowlist; empty by default.

### agents/balance/rails/sim_rail.py
**Docstring:** Atomic sim transfer rail with simulated fee, delay, and injectable failure for testing the auto-pause path.

**Classes:**
- `SimTransferRail(BaseTransferRail)`
  - class attrs: `rail_id: str = "sim"`, `display_name: str = "Sim Transfer Rail"`
  - `is_available(self) -> bool` — True iff `SIM_MODE`.
  - `async execute(self, transfer: "Transfer") -> TransferResult`
  - `async _execute_inner(self, transfer: "Transfer") -> TransferResult`
  - `_log(state: str, t: "Transfer", *, fee: float, error: str = None) -> int` (staticmethod)
  - `_update(movement_id: int, fields: dict) -> None` (staticmethod)

**Functions:** none.

**Module constants:** none.

## Imports graph
**This package imports from elsewhere in the project:**
- `config.settings` — everywhere.
- `database.queries` — `__init__.py`, `balance_agent.py`, `coordinator.py`, `crosschain_agent.py`, `funding_arb_agent.py`, `scalping_agent.py`, `balance/policy/growth_optimal.py`, `balance/planner.py`, `balance/rails/cex_rail.py`, `balance/rails/sim_rail.py`.
- `core.bot.CryptoBot` — `__init__.py` (lazy, inside `SignalAgentWrapper.start`).
- `core.regime_detector.regime_detector` — `scalping_agent.py` (lazy resolver).
- `execution.kill_switch.KillSwitch` — `__init__.py`.
- `execution.arb_engine.ArbEngine` — `__init__.py` (`ArbAgentWrapper`).
- `execution.crosschain_engine.CrossChainArbEngine` — `crosschain_agent.py`.
- `execution.chains.REGISTERED_CONNECTORS` — `crosschain_agent.py` (inside `is_available`).
- `execution.inventory.InventoryTarget, compute_inventory_targets` — `crosschain_agent.py`.
- `execution.funding_engine.{funding_engine, FundingEngine, FundingOpportunity, FundingPosition}` — `funding_arb_agent.py`.
- `execution.mexc_key_router.mexc_key_router` — `scalping_agent.py` (lazy).
- Self-imports: `agents.base`, `agents.scalping_agent`, `agents.crosschain_agent`, `agents.balance_agent`, `agents.funding_arb_agent`, `agents.scalping_confluence`, `agents.scalping_atr_sl`, and the balance sub-tree's internal cross-imports.

**This package is imported by:**
- `main.py`
- `ui/web_server.py`
- `core/bot.py` (indirect — receives `Coordinator` injection)
- `execution/arb_engine.py`
- Tests: `tests/test_arb_engine.py`, `tests/test_balance_agent.py`, `tests/test_bot.py`, `tests/test_coordinator.py`, `tests/test_crosschain_agent.py`, `tests/test_crosschain_engine.py`, `tests/test_dashboard.py`, `tests/test_equity_reconstruction.py`, `tests/test_funding_arb.py`, `tests/test_funds.py`, `tests/test_scalp_activation.py`, `tests/test_scalp_v2_accessors.py`, `tests/test_scalp_v2_integration.py`, `tests/test_scalping_agent.py`, `tests/test_scalping_v2.py`, `tests/test_web_server.py`.

## Plugin registrations
**`agents/__init__.py::REGISTERED_AGENTS`** (instances; coordinator iterates and calls `is_available()` per agent):
- `SignalAgentWrapper()` — `agent_id="signal"`, `optional=False`, `capital_allocation=settings.SIGNAL_AGENT_CAPITAL`. `is_available()` True in sim mode, else True iff any `{EX}_API_KEY` env var is set for an exchange in `ENABLED_EXCHANGES`.
- `ArbAgentWrapper()` — `agent_id="arb"`, `optional=True`, `capital_allocation=settings.ARB_AGENT_CAPITAL`. `is_available()` requires `execution.arb_engine.ArbEngine` importable AND ≥2 venues in `ARB_FEE_MAP` (with API key+secret when not in sim).
- `ScalpingAgent()` — `agent_id="scalp"`, `optional=True`, `capital_allocation=settings.FUND_MEXC_SCALP_CAPITAL`, with trading-budget `_capital=settings.SCALP_CAPITAL`. `is_available()` always True.
- `CrossChainArbAgent()` — `agent_id="xchain"`, `optional=True`, `capital_allocation=settings.XCHAIN_CAPITAL`. `is_available()` requires ≥2 available connectors in `execution.chains.REGISTERED_CONNECTORS`.
- `FundingArbAgent()` — `agent_id="funding_arb"`, `optional=True`, `capital_allocation=settings.FUNDING_CAPITAL_USD`. `is_available()` always True.
- `BalanceAgent()` — `agent_id="balance"`, `optional=True`, `capital_allocation=settings.BALANCE_AGENT_CAPITAL` (default 0). `is_available()` always True.

**`agents/balance/policy/__init__.py::REGISTERED_POLICIES`:**
- `GrowthOptimalPolicy()` — `policy_id="growth_optimal"`. `is_available()` inherits the default True.

**`agents/balance/rails/__init__.py::REGISTERED_RAILS`:**
- `SimTransferRail()` — `rail_id="sim"`. `is_available()` True iff `SIM_MODE`.
- `CexTransferRail()` — `rail_id="cex"`. `is_available()` True iff `REBALANCE_LIVE_ENABLED` + `ccxt.async_support` importable + non-empty `WITHDRAWAL_ROUTES`.

No `REGISTERED_*` for `BaseRebalancePlanner` — `GreedyNetPlanner` is the single default instantiated by `BalanceAgent.__init__`.

## Tests
**tests/test_balance_agent.py** — covers `BalanceAgent`, `InventoryState`, `GreedyNetPlanner`, rails, `GrowthOptimalPolicy`.
- `test_rails_registry_minimum` — verifies sim + cex are both registered.
- `test_policies_registry_minimum` — verifies growth-optimal is registered.
- `test_aggregator_imports_only_base_and_registry` — enforces Plugin Rule 1.
- `test_fake_rail_is_picked_up` — registry injection.
- `test_crashing_rail_does_not_break_agent` — error isolation.
- `test_unavailable_rail_is_skipped` — falls through.
- `test_defaults_dont_change_unconfigured_agents` — `BaseAgent.set_capital_allocation` no-op safety.
- `test_set_capital_refuses_below_open_position` — rail-2 floor.
- `test_effective_balance_fails_open_with_no_claims` — fail-open default.
- `test_claims_partition_shared_venue` — fund partitioning.
- `test_undersubscribed_venue_returns_physical_minus_other_claims` — confirms the 2026-05-29 FIX described in `InventoryState.effective_balance` docstring.
- `test_partially_subscribed_two_funds_each_get_slack_plus_claim`
- `test_oversubscribed_venue_scales_proportionally`
- `test_pending_out_subtracts_from_source`
- `test_can_arb_refuses_paused_route`
- `test_targets_respect_reserve` — `COMPOUND_RESERVE_PCT`.
- `test_risk_parity_fallback_when_edge_absent`
- `test_capacity_cap_caps_and_cascades`
- `test_zero_plan_when_nodes_at_target`
- `test_in_flight_lockout_in_live` — rail 4.
- `test_daily_rate_limit` — rail 5.
- `test_atomic_completion_fee_and_state` — sim rail completion.
- `test_injected_failure_path` — `SIM_REBALANCE_FAILURE_RATE`.
- `test_dormant_without_live_flag` — cex rail `is_available` gating.
- `test_available_when_live_and_routes`
- `test_refuses_unknown_network`
- `test_kill_blocks_new_transfers`
- `test_arm_confirm_two_step` — arm/confirm token.
- `test_arm_expiry`
- `test_dynamic_base_position_scales_with_allocation`
- `test_dynamic_base_position_clamped`
- `test_get_capital_allocation_takes_max`
- `test_drift_hint_below_threshold_internalized`
- `test_drift_hint_above_threshold_structural`
- `test_drift_hint_can_be_swept`
- `test_no_020_literal_in_planner` — guard against magic numbers.
- `test_node_at_0_4_spread_does_not_breach` — Miller-Orr band semantics.
- `test_node_at_0_6_spread_breaches`
- `test_gross_cost_net_round_trip` — fee accounting.
- `test_raw_rows_unmutated`
- `test_excludes_old_rows_outside_window`
- `test_get_true_pnl_importable` — smoke import.

**tests/test_coordinator.py** — covers `Coordinator`.
- `test_kill_all_calls_close_on_every_agent`
- `test_kill_all_runs_close_concurrently`
- `test_unavailable_agent_skipped_on_start`
- `test_portfolio_halt_when_daily_loss_exceeds_threshold`
- `test_portfolio_healthy_below_threshold`
- `test_get_portfolio_stats_aggregates_correctly`
- `test_get_agent_stats_sorted_by_capital_desc`
- `test_crashing_agent_does_not_break_others_on_kill_all`
- `test_crashing_agent_get_stats_returns_error_field`
- `test_new_agent_class_plugs_in_via_coordinator` — plugin pattern.
- `test_get_agent_lookup`
- `test_set_dashboard_propagates_to_agents`
- `test_per_fund_circuit_breaker_halts_only_breaching_fund`
- `test_per_fund_circuit_breaker_clears_when_pnl_recovers`
- `test_total_equity_is_dynamic_sum_of_fund_equities`

**tests/test_crosschain_agent.py** — covers `CrossChainArbAgent`.
- `test_xchain_agent_registered`, `test_registered_xchain_is_crosschain_agent` — registry hookup.
- `test_subclasses_base_agent`, `test_required_class_attrs`
- `test_observation_mode_capital_zero_by_default`
- `test_get_stats_returns_offline_before_start`
- `test_get_stats_reports_observation_capital_when_xchain_capital_zero`
- `test_close_all_positions_is_safe_with_no_engine`, `test_stop_is_safe_with_no_engine`
- `test_start_offline_without_two_connectors`
- `test_get_inventory_targets_returns_inventory_target_list`
- `test_get_inventory_targets_swallows_db_errors`
- `test_get_inventory_targets_uses_current_balances_for_drift`

**tests/test_funding_arb.py** — covers `FundingArbAgent` (and the underlying engine). Tests relevant to the agent class:
- `test_observation_mode_zero_routing` — Phase-1 invariant.
- `test_daily_loss_circuit_breaker_halts`, `test_daily_loss_circuit_breaker_zero_alloc_noop`, `test_daily_loss_halt_scales_with_allocation`, `test_consecutive_loss_circuit_breaker_halts` — `_check_circuit_breakers`.
- `test_get_stats_and_availability`
- `test_close_all_positions_drains_via_gather`
- `test_db_roundtrip_observations_and_summary`
- `test_coordinator_picks_up_via_registry`
- Remaining tests (e.g. funding APR annualisation, depth gate, exit-reason ordering) exercise the underlying `FundingEngine`.

**tests/test_scalping_agent.py** — covers `ScalpingAgent` + `OFIEngine` + `FeeManager`.
- `test_ofi_event_increment_bid_improved`, `..._ask_improved`, `..._neutral` — Cont-Kukanov-Stoikov increment math.
- `test_ofi_multilevel_weights`, `test_ofi_zscore_after_min_buckets`, `test_ofi_depth_weights_ten_levels`, `test_ofi_uses_available_levels_when_book_shorter`.
- `test_fee_manager_override_mexc`, `test_fee_manager_dynamic_tp_sl_mexc`, `test_fee_manager_dynamic_tp_sl_bitget`, `test_fee_manager_viability_mexc_passes`, `test_fee_manager_viability_high_fee_fails`.
- `test_tfi_confirms_matching_direction`, `test_tfi_normalized_imbalance_ratio`, `test_direction_persistence_resets`.
- `test_entry_observation_logged_mexc`, `test_stale_feed_guard_skips_and_logs`, `test_entry_blocked_unapproved_exchange`.
- `test_exit_ofi_exhausted`, `test_circuit_breaker_daily_loss`, `test_scalp_daily_loss_halt_scales_with_allocation`, `test_scalp_zero_alloc_daily_loss_noop`.
- `test_place_order_records_sim_trade`, `test_exit_closes_sim_trade_and_tracks_net_equity`.
- `test_daily_reset_clears_circuit_breaker`, `test_session_gate_blocks_outside_window`, `test_news_guard_blocks_entry`.
- `test_market_data_wired_mid_price`, `test_market_data_falls_back_to_all_prices`, `test_market_data_stub_when_unwired`.
- `test_regime_detector_wired_returns_uppercase`, `test_regime_choppy_blocks_entry`.
- `test_market_data_get_spread_bps_math`, `test_market_data_get_change_pct_returns_pct_over_window`, `..._none_with_no_history`, `..._none_when_window_predates_history`, `test_market_data_record_price_sample_trims_old_entries`.
- `test_get_btc_1m_change_wired_to_market_data`, `..._returns_zero_when_no_data`, `..._zero_when_market_data_missing`.
- `test_micro_price_tracker_backfills_30s`.

**tests/test_scalp_v2_integration.py** — covers the v2 integration with the agent.
- `test_v2_block_logs_prefixed_skip_with_entry_price`
- `test_v2_pass_uses_atr_tp_sl_and_labels`
- `test_confluence_disabled_falls_back_to_v1_entry`

**tests/test_scalp_v2_accessors.py** — covers `OFIEngine` v2 accessors + market_data v2 helpers.
- `test_get_mid_price_from_book`, `test_get_mid_price_falls_back_to_last_price`, `test_get_order_book_shape`, `test_get_ema_and_atr_from_candles`, `test_get_vwap_and_volume`, `test_get_mid_price_at_offset`, `test_ofi_get_z_score_none_until_bucket_closes`, `test_ofi_get_exchanges_for_symbol`.

**tests/test_scalp_activation.py** — covers v2 activation gating.
- `test_activation_readiness_empty_not_ready`, `test_activation_stats_and_v1_v2`, `test_recalibration_statements_execute`.

**tests/test_scalping_v2.py** — exercises `ConfluenceChecker`, `ATRStopCalculator`, and `is_ready_for_live_v2` via direct imports from `agents.scalping_confluence`, `agents.scalping_atr_sl`, `agents.scalping_agent_v2_integration`. The file body has no `def test_*` discovered by the standard pattern (it may rely on pytest-collected fixtures or class methods); review the source for the actual test layout.

**tests/test_equity_reconstruction.py** — `test_signal_get_stats_reads_ledger`, `test_signal_get_stats_falls_back_when_db_unreachable`, `test_scalp_reconstruct_pnl`, `test_scalp_reconstruct_pnl_survives_db_error`, `test_arb_reconstruct_engine_pnl` — cover the wrapper restart-resume P&L paths.

**tests/test_funds.py** — `test_fund_constants_present_and_positive`, `test_mexc_arb_fund_constant_present`, `test_starting_capital_is_sum_of_funds_plus_reserve`, `test_legacy_aliases_track_fund_constants`, `test_mexc_folded_into_arb_routing_and_fee_map`, `test_registered_funds_carry_their_allocation`, `test_mexc_arb_wrapper_removed`, `test_scalp_fund_size_decoupled_from_trading_budget`, `test_sim_balance_ledger_sums_to_starting_capital_and_includes_mexc`, `test_sim_balance_ledger_ring_fence_coverage_per_fund`, `test_order_router_sizes_off_signal_fund_not_total`.

**tests/test_arb_engine.py** — primarily covers `execution.arb_engine.ArbEngine` but includes integration tests touching `agents.funding_arb_agent`: `test_funding_arb_circuit_breaker_halts_on_daily_loss`, `..._zero_alloc_noop`, `test_funding_arb_initial_allocation_from_fund_constant`, `test_funding_arb_set_capital_allocation_changes_field`, `test_funding_arb_breaker_scales_with_live_allocation`, `test_funding_arb_breaker_zero_allocation_no_divide_by_zero`, `test_funding_arb_fund_claim_visible_to_balance_agent`.

**tests/test_dashboard.py** — exercises dashboard panels that depend on the Coordinator-injected `Bot`/`ScalpingAgent`; non-agent panels skipped above. Relevant cases: `test_scalp_panel_*`, `test_scalp_snapshot_*`, `test_full_dashboard_includes_scalp_row`.

**tests/test_web_server.py** — exercises the web layer's interaction with the BalanceAgent + roster snapshot. Relevant: `test_snapshot_includes_balance_keys`, `test_snapshot_safe_when_balance_agent_raises`, `test_rebalance_arm_returns_token`, `test_rebalance_confirm_executes_within_window`, `test_rebalance_confirm_expired_token`, `test_rebalance_confirm_mismatched_token`, `test_rebalance_cancel_invalidates_token`, `test_rebalance_blocked_when_live_disabled`, `test_rebalance_blocked_when_balance_agent_missing`, `test_rebalance_logs_event_on_confirm`, `test_rebalance_confirm_replan_mismatch`, `test_placeholder_agents_not_in_snapshot`, `test_api_agent_signal_returns_trades_and_insights`, `test_api_agent_arb_returns_trades_and_insights`, `test_api_agent_scalp_returns_trades_and_insights`, `test_api_agent_placeholders_now_404`, `test_api_agent_unknown_returns_404`, `test_api_agent_insights_filtered_by_agent_id`, `test_scalp_win_rate_zero_when_no_trades`, `test_scalp_win_rate_computed_from_pnl_bps_positive`, `test_scalp_win_rate_resets_at_utc_midnight`, `test_scalp_closed_trades_persisted_in_snapshot`, `test_scalp_closed_trades_buffer_capped_at_setting`, `test_scalp_live_trades_separate_from_closed`, `test_exposure_includes_scalp_positions`, `test_snapshot_includes_arb_extended_keys`, `test_snapshot_includes_xchain_keys`, `test_snapshot_includes_funding_keys`, `test_snapshot_safe_when_xchain_engine_raises`, `test_snapshot_safe_when_funding_engine_raises`.

**tests/test_bot.py** — exercises `core.bot.CryptoBot` (which the `SignalAgentWrapper` instantiates) but doesn't import from `agents` directly aside from injecting the coordinator. Listed here for completeness; tests not enumerated since they target `core.bot`.

## TODOs / FIXMEs / stubs
- `agents/scalping_agent.py:62` — `# TODO (dashboard, Phase 2): expose self._observations[-15:] as a` (scalp-feed dashboard panel).
- `agents/scalping_agent.py:661` — `# the wiring TODOs, both checked at evaluation time.` (referential).
- `agents/scalping_agent.py:871` — `# TODO Phase 2: call from market_data WebSocket handler.` (`on_book`).
- `agents/scalping_agent.py:881` — `# TODO Phase 2: call from market_data WebSocket handler.` (`on_trade`).
- `agents/coordinator.py:286` — `# enforcement is a TODO once agents grow that hook.` (portfolio exposure soft block).
- `agents/__init__.py:382` — `# Web UI v2 removed the macro / sentiment_agent / onchain placeholder` (documents removed placeholders; `PlaceholderAgent` kept as base class).
- `agents/__init__.py:26` — `placeholders) live in this module rather than the Coordinator, so the` (docstring referencing PlaceholderAgent).
- `agents/base.py:161` — `Default 0.0 — placeholder / operational agents have no open` (docstring).
- `agents/base.py:206` — `logger.debug(f"placeholder agent {self.agent_id}: start (no-op)")`.
- `agents/crosschain_agent.py:20` — `connectors' submit_swap raises NotImplementedError until that path is` (docstring documenting the connector-level stub).
- `agents/balance/rails/cex_rail.py:214` — `raise NotImplementedError("live ccxt.withdraw integration pending operator authorisation")` (the production live withdraw path is intentionally stubbed).

## Known issues observed
- **Duplicate scalp-v2 source between `agents/` and `scalping_v2/`.** `agents/scalping_atr_sl.py`, `agents/scalping_confluence.py`, and `agents/scalping_agent_v2_integration.py` are byte-identical to `scalping_v2/scalping_atr_sl.py`, `scalping_v2/scalping_confluence.py`, and `scalping_v2/scalping_agent_v2_integration.py` respectively (`diff` returns empty). Production code, tests, and `agents/scalping_agent.py` import the `agents.*` copies; `scripts/run_recalibration.py` and `scalping_v2/demo_simulate_v1_vs_v2.py` are the only consumers of the `scalping_v2/` copies, both via path-based or local imports. The `scalping_v2/` tree should be treated as a docs+demo bundle; the duplicated `.py` files are dead code or risk silent drift if either copy is edited.
- **Logger naming leak.** `agents/scalping_atr_sl.py` and `agents/scalping_confluence.py` use `logging.getLogger("scalping_v2.atr_sl")` and `"scalping_v2.confluence"` — these strings reflect the original module location and won't follow if the file is renamed/moved or filtered in logging configuration that targets `agents.*` paths.
- **`agents/scalping_agent_v2_integration.py` is a reference module, not production wiring.** It documents the v2 integration recipe; the only production usage is `is_ready_for_live_v2()` (consumed by tests). `evaluate_signal_v2`, `_initial_observation`, `_run_legacy_gates`, and `_compute_position_size_usd` are reference/demo code never called from `ScalpingAgent`. Risk: it carries `DB_COLUMNS_V2` as the canonical schema-change doc but the actual ALTER statements live elsewhere.
- **Dual sources of truth for capital.** `ScalpingAgent` keeps both `self.capital_allocation` (fund pool, drives BalanceAgent compounding) and `self._capital` (trading budget — drives observation-vs-sim toggle); the documented invariant is that `set_capital_allocation` only mirrors to `_capital` when `settings.SCALP_CAPITAL > 0`. Operators flipping the fund allocation via the BalanceAgent will NOT auto-flip observation mode — that's documented intent but easy to forget.
- **Theta hint is a hard-coded magic number.** `agents/balance/policy/growth_optimal.py:258` hard-codes `theta_hint = 0.20` rather than reading a settings constant; the planner-side guard `BALANCE_STRUCTURAL_DRIFT_HINT` is the real gate, but the producer-side hint diverging from settings is a latent inconsistency the docstring acknowledges without resolving (and `test_no_020_literal_in_planner` only guards the planner side).
- **`_open_position_floor` is a placeholder.** `agents/balance/policy/growth_optimal.py:117-127` always returns `0.0` with a docstring acknowledging the coupling problem; the rail-2 floor enforcement therefore relies entirely on `BalanceAgent._safety_clear` reading the sibling agent's `get_open_position_notional()` at dispatch time, not at policy-target time. A divergence between target and floor is possible in principle.
- **`_WITHDRAWAL_ADDRESSES` is empty by default.** `agents/balance/rails/cex_rail.py:51-54` ships an empty allowlist; even with `REBALANCE_LIVE_ENABLED=True` every live transfer fails with `no whitelisted address for ...`. This is intentional fail-closed behaviour but is a hard prerequisite that must be operator-provisioned before live transfers are possible.
- **Coordinator portfolio-exposure CB is observational only.** `agents/coordinator.py:286-293` logs a warning when `total_exposure_pct >= PORTFOLIO_MAX_EXPOSURE_PCT` but does not block new entries; the comment explicitly flags that `block_new_entries` enforcement is a TODO.
- **`SignalAgentWrapper.start` calls `_bot.start()` which never returns.** The implicit lifecycle is "start blocks forever" — the Coordinator's `gather` pattern handles this, but any caller awaiting `start()` directly will hang. Behaviour is documented in the docstring but worth noting.
- **`agents/balance_agent.py` reaches into sibling agents via `REGISTERED_AGENTS` walk.** `_find_sibling` re-imports `REGISTERED_AGENTS` inside its method body to avoid circular import — fine but couples the BalanceAgent to the package-level registry implicitly. The fund→agent mapping (`signal→signal`, `arb→arb`, `mexc_scalp→scalp`) is hard-coded in `_find_sibling_for_fund` rather than coming from settings.
- **`agents/scalping_agent.py:1180-1190` debounces stale-feed logging with the same `_last_mid` dict it uses to detect motion** — re-arming after a hit means a persistently frozen feed will log once per `SCALP_STALE_MID_THRESHOLD_SEC`, but if the threshold is misconfigured to 0 the debounce collapses. Defensive constants in `settings.py` cover this.
- **`scalping_agent_v2_integration._compute_position_size_usd`** references `agent.scalp_capital`, `agent.scalp_fund_capital`, and `agent.s` — none of these attributes exist on the real `ScalpingAgent` class. Confirms this module is reference-only; calling it against a live `ScalpingAgent` would `AttributeError`.
