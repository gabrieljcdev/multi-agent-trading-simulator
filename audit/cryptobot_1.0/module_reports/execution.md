# Module Report: execution

## Purpose

The `execution/` package is the order-routing and trade-lifecycle layer. It owns: (a) the synchronous-from-async `OrderRouter` that the main signal-track `Bot` calls in all three approval modes (`per_trade`, `window`, `autonomous`) to write a sim Trade row via `_sim_execute` or (stub) live execution via `_live_execute`; (b) the `KillSwitch` that bypasses every queue/approval and closes all open positions in parallel via `asyncio.gather`; (c) the `PositionManager` that ticks SL/TP exits and trips portfolio-level circuit breakers; (d) three standalone, rule-based arb engines that intentionally live OUTSIDE the Bot/agent-loop dispatch (`ArbEngine` for CEX cross-exchange arb, `FundingRateArbEngine` for funding-rate carry inside `arb_engine.py`, `FundingEngine` for the Phase-1 Binance delta-neutral funding-arb, `CrossChainArbEngine` for L2 cross-chain arb in observation mode); plus (e) supporting infrastructure: per-pair MEXC key router, cross-chain DEX connector plugin layer, and an inventory-target producer for the BalanceAgent. The arb engines deliberately do NOT import `core/bot.py` — they own their circuit breakers, capital allocation, and approval bypass because arb gaps close in seconds.

## Subpackages

- `execution/chains/` — chain-connector plugin layer for the CrossChainArbEngine. One file per L2 (Arbitrum/Base/Optimism) registers a `BaseChainConnector` subclass in `REGISTERED_CONNECTORS`; reads WETH-USDC pool state + per-swap gas cost; submit_swap raises `NotImplementedError` (observation mode).

## Files

| File | LOC | One-sentence summary |
|---|---:|---|
| `execution/__init__.py` | 0 | Empty package marker. |
| `execution/arb_engine.py` | 1026 | Standalone CEX cross-exchange `ArbEngine` + sibling `FundingRateArbEngine`, with their own circuit breakers, per-symbol locks, depth-aware slippage model, and balance/inventory gates. |
| `execution/crosschain_engine.py` | 679 | Cross-CHAIN observation-mode arb engine: scans REGISTERED_CONNECTORS, computes net edge bps, logs every (buy_chain, sell_chain) evaluation to `xchain_observations`. |
| `execution/funding_engine.py` | 483 | Phase-1 Binance delta-neutral funding-rate carry engine — sim-only, observation-mode hard-gated. |
| `execution/inventory.py` | 187 | Pure `compute_inventory_targets()` producer for the BalanceAgent — chain-weight allocation from xchain observations, drift gate baked into the dataclass. |
| `execution/kill_switch.py` | 98 | `KillSwitch.engage()` closes every open trade in parallel via `asyncio.gather`; logs to circuit_breakers. |
| `execution/mexc_key_router.py` | 164 | Per-pair MEXC API key router — lazy ccxt.mexc client pool keyed by `MEXC_PAIR_KEY_MAP` index. |
| `execution/position_manager.py` | 104 | Polls open trades, hits SL/TP exits, runs portfolio circuit-breaker checks (daily loss, consecutive losses). |
| `execution/router.py` | 85 | `OrderRouter` — main signal-track sim/live dispatch; sizes off `FUND_SIGNAL_CAPITAL`. |
| `execution/chains/__init__.py` | 58 | Registers `REGISTERED_CONNECTORS = [ArbitrumConnector(), BaseChainConnectorInstance(), OptimismConnector()]`. |
| `execution/chains/_solidly_volatile.py` | 238 | Shared partial impl for Solidly-fork volatile (x*y=k) pools — reused by Base + Optimism connectors. |
| `execution/chains/arbitrum.py` | 274 | `ArbitrumConnector` — Uniswap v3 WETH-USDC pool reader with virtual-reserve derivation from sqrtPriceX96. |
| `execution/chains/base_chain.py` | 30 | `BaseChainConnectorInstance` — Aerodrome on Base (subclass of `SolidlyVolatilePoolConnector`). |
| `execution/chains/base_connector.py` | 154 | ABC + dataclasses: `BaseChainConnector`, `PoolState`, `VenueConfig`. |
| `execution/chains/optimism.py` | 25 | `OptimismConnector` — Velodrome on Optimism (subclass of `SolidlyVolatilePoolConnector`). |

## Public surface

### execution/__init__.py
**Docstring:** none

Empty (0 bytes).

### execution/router.py
**Docstring:** `"""execution/router.py — Order router — handles both sim and live order execution."""`

**Classes:**
- `OrderRouter` — main signal-track router; sim writes Trade row, live is a stub.
  - `__init__(self, exchange_manager=None)`
  - `async execute(self, signal: Signal, profile) -> Optional[dict]` — compute size/SL/TP, dispatch to sim or live execute, return trade dict.
  - `_sim_execute(self, signal, entry, sl, tp, size_usd) -> int` — build trade_data dict and call `save_trade()`; tags `sim_mode=True`, `strategy=settings.ACTIVE_STRATEGY`.
  - `async _live_execute(self, signal, entry, sl, tp, size_usd) -> Optional[int]` — STUB: logs warning, returns None.
  - `_get_price(self, signal: Signal) -> Optional[float]` — stub returning None (MarketData provides via callback).
  - `update_portfolio_value(self, value: float)` — setter for `_portfolio_value` (currently sized off `FUND_SIGNAL_CAPITAL`).

**Module constants:** none.

### execution/kill_switch.py
**Docstring:** `"""execution/kill_switch.py — Emergency kill switch. One call closes everything immediately. Bypasses all logic, queues, and approvals. Called by [K] keypress or circuit breaker."""`

**Classes:**
- `KillSwitch` — closes all open positions in parallel.
  - `__init__(self, exchange_manager=None, sim_mode: bool = True)`
  - `async engage(self, reason: str = "manual") -> dict` — flips `_active`, fetches `get_open_trades()`, runs `_close_position` for each via `asyncio.gather(..., return_exceptions=True)`, logs to circuit_breakers, returns `{closed, timestamp, reason, trades}`.
  - `async _close_position(self, trade) -> dict` — sim path uses entry_price as exit (TODO: live price); live path calls `exchange_manager.market_close()` and computes pnl_pct.

**Module constants:** none.

### execution/position_manager.py
**Docstring:** `"""execution/position_manager.py — Monitors open positions and triggers SL/TP exits."""`

**Classes:**
- `PositionManager` — polls open trades for SL/TP hits + portfolio circuit-breaker logic.
  - `__init__(self, market_data=None)`
  - `async check_positions(self) -> list` — iterates `get_open_trades()`, evaluates SL/TP per side, closes via `close_trade(...)`, returns list of closed trade ids; then awaits `_check_circuit_breakers()`.
  - `_get_price(self, trade)` — proxies `market_data.get_price(exchange, pair)`.
  - `_calc_pnl(self, trade, exit_price) -> float` — long/short pnl_pct.
  - `async _check_circuit_breakers(self)` — reads `settings.CIRCUIT_BREAKERS`, checks daily_loss vs `get_today_pnl_pct()` and consecutive_loss vs `get_consecutive_losses()`; calls `_trigger(action, reason)`.
  - `_trigger(self, action: str, reason: str)` — logs CB event, sets `_halted` / `_paused` flag.
  - `resume(self)` — clears halt/pause flags.
  - `is_halted` (property `-> bool`), `is_paused` (property `-> bool`), `halt_reason` (property `-> str`)

**Module constants:** none.

### execution/arb_engine.py
**Docstring:** Standalone CEX cross-exchange arb engine + sibling funding-rate engine. Standalone — only imports ccxt, config.settings, database.queries.

**Module constants:**
- `STATUS_OFFLINE = "OFFLINE"`, `STATUS_RUNNING = "RUNNING"`, `STATUS_HALTED = "HALTED"`, `STATUS_STOPPED = "STOPPED"`

**Dataclasses:**
- `ArbOpportunity` — `symbol`, `buy_exchange`, `sell_exchange`, `buy_price`, `sell_price`, `gross_gap_pct`, `net_gap_pct`, `max_size_usd`, `detected_at` (time.monotonic), plus optional `spread_buy_pct=0.0`, `spread_sell_pct=0.0`, `depth_buy_usd=0.0`, `depth_sell_usd=0.0`, `opportunity_log_id: Optional[int] = None`, `funding_rate_pct: Optional[float] = None`.
- `ArbResult` — `opportunity`, `success`, `buy_fill`, `sell_fill`, `gross_pnl_usd`, `net_pnl_usd`, `execution_ms`, `error: Optional[str] = None`, `status: str = "executed"`, `slippage_buy_pct: Optional[float] = None`, `slippage_sell_pct: Optional[float] = None`.

**Functions:**
- `gross_gap_pct(buy_price: float, sell_price: float) -> float` — `(sell-buy)/buy * 100`.
- `net_gap_pct(buy_price: float, sell_price: float, buy_ex: str, sell_ex: str, fee_map: dict) -> tuple[float, float]` — returns (gross_pct, net_pct after both legs' fees).
- `min_gap_threshold(buy_ex: str, sell_ex: str) -> float` — Bitget unlocks `ARB_MIN_GAP_PCT`; otherwise `ARB_MIN_GAP_PCT_FALLBACK`.
- `_liquidity_usd(levels: list, depth: int = 3) -> float` — sums top N price×size.
- `slippage_pct(base_spread_pct: float, size_usd: float, depth_usd: float) -> float` — `base_spread * sqrt(size/depth)`, clamped to `[ARB_SLIPPAGE_MIN_PCT, ARB_SLIPPAGE_MAX_PCT]`.

**Classes:**
- `ArbEngine` — fully async cross-exchange arb engine.
  - `__init__(self, exchange_clients: Optional[dict] = None, dashboard=None, sim_mode: Optional[bool] = None, *, fund_id: str = "arb", exchanges: Optional[list] = None)`
  - `async start(self) -> None`
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None`
  - `get_stats(self) -> dict`
  - `async _scan_loop(self) -> None`
  - `async _find_best_opportunity(self) -> Optional[ArbOpportunity]`
  - `@staticmethod _log_opportunity(**kwargs) -> Optional[int]`
  - `async _safe_fetch_book(self, ex, sym: str)`
  - `async _execute_arb(self, opp: ArbOpportunity) -> None`
  - `_dynamic_base_position(self) -> float`
  - `async _check_balances(self, opp: ArbOpportunity, size_base: float) -> tuple[bool, Optional[str]]`
  - `@staticmethod async _fetch_free_balance(ex, ccy: str) -> Optional[float]`
  - `@staticmethod _mark_opportunity_executed(opp_id: Optional[int], trade_id: Optional[int]) -> None`
  - `_sim_fills(self, opp: ArbOpportunity) -> tuple[float, float, float, float]` — depth-aware slippage on both legs.
  - `async _live_fills(self, opp: ArbOpportunity, size_base: float) -> tuple[float, float]` — both legs via single `asyncio.gather`.
  - `_update_stats(self, result: ArbResult) -> None`
  - `_notify_dashboard(self, result: ArbResult) -> None`
  - `_log_to_db(self, result: ArbResult) -> Optional[int]`
  - `_cb_triggered(self) -> bool` — %-based daily loss + consecutive-loss halt.
  - `async _daily_reset_loop(self) -> None`
  - `_build_clients(self) -> dict`
  - class attrs / instance state: `dashboard`, `sim_mode`, `fund_id`, `_exchange_filter`, `_exchanges`, `_running`, `_status`, `_scan_task`, `_reset_task`, `_daily_pnl_usd`, `_total_pnl_usd`, `_total_trades`, `_consecutive_losses`, `_last_opportunity`, `_last_trade_time`, `missed_balance_checks`, `_capital_allocation`, `_symbol_locks`, `_semaphore`, `_active_arbs`.

- `FundingRateArbEngine` — funding-rate carry engine (data_sources aggregator).
  - `__init__(self, sim_mode: Optional[bool] = None, dashboard=None)`
  - `async start(self) -> None`
  - `@staticmethod _log_coinglass_status() -> None`
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None`
  - `get_stats(self) -> dict`
  - `set_capital_allocation(self, amount: float) -> None`
  - `async fetch_funding_rates(self) -> dict[str, float]`
  - `async _scan_loop(self) -> None`
  - `_cb_triggered(self) -> bool`
  - `async _daily_reset_loop(self) -> None`

### execution/crosschain_engine.py
**Docstring:** Cross-CHAIN arb engine — non-atomic, inventory-pre-positioned, observation-mode only. NOT the cross-exchange engine.

**Module constants:** `STATUS_OFFLINE`, `STATUS_RUNNING`, `STATUS_HALTED`, `STATUS_STOPPED`.

**Dataclasses:**
- `XChainEvaluation` — fields: `symbol, buy_chain, sell_chain, buy_venue, sell_venue, notional_usd, spread_bps, rt_fee_bps, gas_bps, slip_bps, bridge_bps, net_edge_bps, gas_breakeven_usd, would_entry, skip_reason="", buy_block=0, sell_block=0, detected_at=field(default_factory=time.monotonic)`.
- `CircuitBreakerState` — `daily_pnl_usd=0.0, consecutive_losses=0, halted=False, halt_reason=""`.

**Functions:**
- `gas_breakeven_usd(b_gas_usd: float, s_gas_usd: float, gas_budget_bps: float) -> float` — smallest notional where round-trip gas <= budget.
- `optimal_notional_constant_product(buy_state: PoolState, sell_state: PoolState, fee_bps: float) -> float` — Angeris et al. (2019) `dx* = (sqrt(γxyP_ext) - x)/γ`.
- `capped_optimal_notional(buy_state, sell_state, *, fee_bps: float, slippage_tolerance_bps: float, max_position_usd: float) -> float` — caps raw optimal by depth + tolerance + position cap.
- `estimated_two_leg_slippage_bps(notional_usd: float, buy_state: PoolState, sell_state: PoolState) -> float` — two-leg additive slippage.

**Classes:**
- `CrossChainArbEngine` — scan loop, edge math, circuit breakers, observation logger.
  - `__init__(self, connectors: Optional[list[BaseChainConnector]] = None, *, sim_mode: Optional[bool] = None)`
  - `async start(self) -> None` — refuses to come online with <2 connectors.
  - `async stop(self) -> None`
  - `async close_all_positions(self) -> None` — no-op in observation mode + log to circuit_breakers.
  - `get_stats(self) -> dict`
  - `set_capital_allocation(self, amount: float) -> None`
  - `async _scan_loop(self) -> None`
  - `async _scan_symbol(self, symbol: str) -> None`
  - `async _evaluate_symbol(self, symbol: str) -> None`
  - `async _fetch_states(self, symbol: str) -> list[PoolState]`
  - `async _fetch_gas_costs(self) -> dict[str, float]`
  - `_evaluate_pair(self, *, symbol: str, buy: PoolState, sell: PoolState, gas_costs: dict[str, float]) -> Optional[XChainEvaluation]`
  - `@staticmethod _block_is_fresh(buy_block: int, sell_block: int) -> bool`
  - `_persist(self, e: XChainEvaluation) -> None` — writes `xchain_observations` row; `observation_only=(self._capital_allocation == 0)`.
  - `_cb_triggered(self) -> bool`
  - `async _daily_reset_loop(self) -> None`

### execution/funding_engine.py
**Docstring:** Funding-rate arbitrage engine for FundingArbAgent (Phase 1). Single venue Binance, delta-neutral. Observation mode hard-gated.

**Dataclasses:**
- `FundingOpportunity` — `symbol, variant, venue_long, venue_short, funding_apr, spread_apr, oi_usd, depth_ok`.
- `FundingPosition` — `opp, notional_usd, margin_used, basis_at_entry, funding_collected=0.0, fees_paid=0.0, opened_at=field(default_factory=time.time)`.

**Classes:**
- `FundingEngine` — scan + sim execution for funding-rate carry.
  - `__init__(self, ccxt_factory=None)`
  - `async scan(self) -> list[FundingOpportunity]`
  - `async open(self, opp: FundingOpportunity) -> None` — hard `assert settings.SIM_MODE` under `FUNDING_OBSERVATION_MODE`; rejects non-delta_neutral; both legs via `asyncio.gather`.
  - `exit_reason(self, pos: FundingPosition) -> Optional[str]` — priority: funding_decay > basis_blowout > margin_breach > venue_health > max_hold.
  - `async close(self) -> None` — close ccxt client.
  - `_get_exchange(self)` — lazy ccxt.binance with `options={"defaultType": "future"}` (REQUIRED).
  - `async _fetch_native_funding(self, venue: str, symbol: str) -> tuple[Optional[float], float]`
  - `_build_opportunities(self, symbol: str, venue: str, funding_apr: float, oi_usd: float) -> list[FundingOpportunity]`
  - `@staticmethod _filter(opps: list[FundingOpportunity]) -> list[FundingOpportunity]`
  - `async _place(self, pos: FundingPosition, leg: str) -> Optional[int]` — sim route; live is a stub log.
  - `@staticmethod _basis(opp: FundingOpportunity) -> float` (returns 0.0 Phase 1)
  - `@staticmethod _basis_blowout(pos: FundingPosition) -> bool` (returns False Phase 1)
  - `@staticmethod _margin_breach(pos: FundingPosition) -> bool` (returns False Phase 1)
  - `_venue_unhealthy(self, pos: FundingPosition) -> bool`

**Module-level singleton:** `funding_engine = FundingEngine()`

### execution/inventory.py
**Docstring:** Inventory targets for the cross-chain arb agent — "publish target, let the BalanceAgent move USDC". DO NOT implement transfers here.

**Dataclasses:**
- `InventoryTarget` — `connector_id, symbol, target_base_usd, target_quote_usd, current_base_usd, current_quote_usd, drift_pct, needs_rebalance`.

**Functions:**
- `_chain_weights_from_observations(observations: list[dict], chains: list[str]) -> dict[str, float]` — proportional to would_entry frequency with `1/(2N)` floor.
- `compute_inventory_targets(observations: list[dict], current_balances: dict[str, dict[str, float]] | None = None, *, capital_usd: float | None = None, chains: list[str] | None = None, symbol: str | None = None, drift_threshold: float | None = None) -> list[InventoryTarget]` — one InventoryTarget per enabled chain; 50/50 base/quote split.

**Module constants:** `__all__ = ["InventoryTarget", "compute_inventory_targets"]`.

### execution/mexc_key_router.py
**Docstring:** MEXC per-pair key router. One account can hold many API keys each restricted to a different pair subset. Module-level singleton.

**Classes:**
- `MexcKeyRouter` — lazily-built pool of ccxt.mexc clients keyed by per-pair key index.
  - `__init__(self)`
  - `get_client_for(self, symbol: str)` — returns ccxt client or None.
  - `any_client(self)` — any constructed/constructable MEXC client (FeeManager pre-warm); falls back to scanning indices 1..30.
  - `has_route_for(self, symbol: str) -> bool` — pure sync, no client construction.
  - `async close_all(self) -> None` — idempotent close of every constructed client.
  - `_client_for_index(self, key_index: int)` — lazy construct from `MEXC_KEY_{N}_API_KEY/SECRET`.

**Module-level singleton:** `mexc_key_router = MexcKeyRouter()`

### execution/chains/__init__.py
**Docstring:** Registry of chain connectors. To-add-a-new-chain instructions inline.

**Module constants:**
- `REGISTERED_CONNECTORS: list[BaseChainConnector] = [ArbitrumConnector(), BaseChainConnectorInstance(), OptimismConnector()]`
- `__all__ = ["REGISTERED_CONNECTORS", "BaseChainConnector", "PoolState", "VenueConfig", "ArbitrumConnector", "BaseChainConnectorInstance", "OptimismConnector"]`

### execution/chains/base_connector.py
**Docstring:** The three contracts every chain connector must satisfy: VenueConfig / PoolState / BaseChainConnector. submit_swap raises NotImplementedError at this layer.

**Dataclasses:**
- `VenueConfig` — `venue: str`, `pool_address: str`, `fee_bps: float`.
- `PoolState` — `connector_id, symbol, venue, reserve_base, reserve_quote, fee_bps, spot_price, depth_usd_1pct, block_number, timestamp, error: Optional[str] = None`.

**Classes:**
- `BaseChainConnector(ABC)` — chain-connector ABC.
  - class attrs: `connector_id = "base"`, `display_name = "Base Chain"`, `rpc_env_var = ""`, `optional = True`, `venues: dict`.
  - `@abstractmethod async get_pool_state(self, symbol: str) -> PoolState`
  - `@abstractmethod async gas_cost_usd(self) -> float`
  - `is_available(self) -> bool` — env var present AND no `<FILL>` pool addresses.
  - `async submit_swap(self, *args, **kwargs)` — raises `NotImplementedError("live execution is a separate build (XCHAIN_LIVE_ENABLED + web3 signing)")`.

**Module constants:** `__all__ = ["VenueConfig", "PoolState", "BaseChainConnector"]`.

### execution/chains/_solidly_volatile.py
**Docstring:** Shared partial implementation for Solidly-fork volatile pools (x*y=k). Used by Aerodrome/Velodrome.

**Module constants:** `_SOLIDLY_POOL_ABI`, `_ERC20_ABI` (minimal ABI fragments).

**Functions:**
- `_try_import_web3()` — lazy import; returns Web3 class or None.

**Classes:**
- `SolidlyVolatilePoolConnector(BaseChainConnector)` — partial impl for Solidly-volatile chains.
  - class attr: `SWAP_GAS_UNITS = 120_000`.
  - `__init__(self)` — builds `self.venues` from `settings.XCHAIN_VENUES[connector_id]`, `_w3=None`, `_decimals_cache={}`.
  - `is_available(self) -> bool` — extends base with web3 importability.
  - `_resolve_w3(self)` — lazy `Web3(Web3.HTTPProvider(rpc))`.
  - `async _decimals(self, addr: str, default: int) -> int`
  - `async get_pool_state(self, symbol: str) -> PoolState` — reads `getReserves`, `token0`, `token1`; orients base=WETH, quote=USDC by decimal count.
  - `_error_state(self, symbol: str, reason: str, *, venue: Optional[VenueConfig] = None, block: int = 0) -> PoolState`
  - `async gas_cost_usd(self) -> float` — `gasPrice * SWAP_GAS_UNITS * (ETH/USD) / 1e18`; inf on failure.

**Module constants:** `__all__ = ["SolidlyVolatilePoolConnector"]`.

### execution/chains/arbitrum.py
**Docstring:** ArbitrumConnector — reads WETH-USDC from Uniswap v3 pool on Arbitrum.

**Module constants:** `_UNISWAP_V3_POOL_ABI`, `_ERC20_ABI`.

**Functions:**
- `_try_import_web3()` — lazy import.

**Classes:**
- `ArbitrumConnector(BaseChainConnector)` — Uniswap v3 reader.
  - class attrs: `connector_id = "arbitrum"`, `display_name = "Arbitrum"`, `rpc_env_var = "ARBITRUM_RPC_URL"`, `optional = True`, `SWAP_GAS_UNITS = 150_000`.
  - `__init__(self)`
  - `is_available(self) -> bool` — extends base with web3 importability.
  - `_resolve_w3(self)`
  - `async _decimals(self, addr: str, default: int) -> int`
  - `async get_pool_state(self, symbol: str) -> PoolState` — slot0 + liquidity → virtual reserves via sqrtPriceX96.
  - `_error_state(self, symbol: str, reason: str, *, venue: Optional[VenueConfig] = None, block: int = 0) -> PoolState`
  - `async gas_cost_usd(self) -> float`

### execution/chains/base_chain.py
**Docstring:** BaseChainConnectorInstance — reads WETH-USDC from Aerodrome on Base. File name is base_chain.py (NOT base.py) to avoid collision with base_connector.py.

**Classes:**
- `BaseChainConnectorInstance(SolidlyVolatilePoolConnector)`
  - class attrs: `connector_id = "base"`, `display_name = "Base (Aerodrome)"`, `rpc_env_var = "BASE_RPC_URL"`, `optional = True`.

**Module constants:** `__all__ = ["BaseChainConnectorInstance"]`.

### execution/chains/optimism.py
**Docstring:** OptimismConnector — reads WETH-USDC from Velodrome on Optimism.

**Classes:**
- `OptimismConnector(SolidlyVolatilePoolConnector)`
  - class attrs: `connector_id = "optimism"`, `display_name = "Optimism (Velodrome)"`, `rpc_env_var = "OPTIMISM_RPC_URL"`, `optional = True`.

**Module constants:** `__all__ = ["OptimismConnector"]`.

## Imports graph

**This package imports from project:**
- `signals.base.Signal` (router.py)
- `database.queries` — `save_trade`, `close_trade`, `get_open_trades`, `log_circuit_breaker`, `get_today_pnl_pct`, `get_consecutive_losses`, `log_arb_opportunity`, `log_arb_trade`, `log_arb_balance_fail`, `mark_arb_opportunity_executed`, `insert_xchain_observation`
- `config.settings` (every non-__init__ file)
- `execution.chains` → `REGISTERED_CONNECTORS`, `BaseChainConnector`, `PoolState`, `VenueConfig`, concrete connectors
- `agents.balance.inventory_state.inventory_state` (lazy import inside `ArbEngine._check_balances`)
- `data_sources.data_sources` (lazy import inside `FundingRateArbEngine` + `fetch_funding_rates`)
- External: `ccxt.async_support` (arb_engine.py, funding_engine.py, mexc_key_router.py — all guarded by try/except), `web3` (chain connectors — lazy via `_try_import_web3`)

**This package is imported by:**
- `core.bot` — imports `KillSwitch`, `PositionManager`, `OrderRouter`.
- `main.py` — imports `KillSwitch`.
- `agents/__init__.py` — imports `KillSwitch` and `ArbEngine`.
- `agents/scalping_agent.py` — imports `mexc_key_router`.
- `agents/funding_arb_agent.py` — imports from `execution.funding_engine`.
- `agents/crosschain_agent.py` — imports `CrossChainArbEngine`, `InventoryTarget`, `compute_inventory_targets`, `REGISTERED_CONNECTORS`.
- Tests: `test_arb_engine.py`, `test_funding_arb.py`, `test_crosschain_engine.py`, `test_chain_connectors.py`, `test_inventory_targets.py`, `test_mexc_key_router.py`, `test_balance_agent.py`, `test_funds.py`, `test_crosschain_agent.py`.

## Plugin registrations

`execution/chains/__init__.py:REGISTERED_CONNECTORS`:
- `ArbitrumConnector()` — Arbitrum (Uniswap v3 WETH-USDC pool).
- `BaseChainConnectorInstance()` — Base (Aerodrome volatile WETH-USDC pool).
- `OptimismConnector()` — Optimism (Velodrome volatile WETH-USDC pool).

The CrossChainArbEngine filters this list to those whose `is_available()` returns True (env var set + no `<FILL>` pool addresses + web3 importable).

## Sim vs live behaviour

`SIM_MODE` is consulted in:
- `execution/router.py:17` — `self._sim_mode = settings.SIM_MODE`; `execute()` dispatches sim vs live at `router.py:44`. `_sim_execute` writes a Trade row with `sim_mode=True`. **`_live_execute` is a stub** that logs a warning and returns `None` — live order execution is unimplemented in the main signal-track router.
- `execution/arb_engine.py:178` — `ArbEngine.sim_mode = settings.SIM_MODE` unless overridden. `_execute_arb` branches at line 512: sim uses `_sim_fills` (depth-aware slippage model), live uses `_live_fills` which actually fires `create_market_buy_order` + `create_market_sell_order` via `asyncio.gather`. **The live path is implemented** here (unlike the signal-track router); `_check_balances` enforces a hard capital gate first.
- `execution/arb_engine.py:841` — `FundingRateArbEngine.sim_mode = settings.SIM_MODE`. The execution path is not yet wired (`_scan_loop` reads funding rates but does not open positions — TODO).
- `execution/crosschain_engine.py:254` — `CrossChainArbEngine.sim_mode = settings.SIM_MODE`. Engine is OBSERVATION-ONLY: `submit_swap` on every connector raises `NotImplementedError` and the engine never calls it; only logs to `xchain_observations`. Live build gated by `settings.XCHAIN_LIVE_ENABLED` (a future PR).
- `execution/funding_engine.py:155` — `FundingEngine.open()` runs `assert settings.SIM_MODE` when `settings.FUNDING_OBSERVATION_MODE` is True (defence in depth: even if the agent misroutes a call, no live order can be placed). `_place` at line 400 logs a stub for live mode and returns None.
- `execution/kill_switch.py` — sim path uses `trade.entry_price` as exit price (TODO line 69); live path calls `exchange_manager.market_close(...)`.

Status of `_live_execute` stubs:
- `OrderRouter._live_execute` (router.py:77-79) — STUB, just `logger.warning("LIVE execution — real money"); return None`.
- `FundingEngine._place` live branch (funding_engine.py:400-405) — STUB, logs and returns None.
- `BaseChainConnector.submit_swap` (base_connector.py:140-151) — raises `NotImplementedError` (deliberate).
- `ArbEngine._live_fills` — **implemented** (real ccxt market order calls).
- `KillSwitch._close_position` live branch — **implemented** (real `exchange_manager.market_close` call) but depends on an exchange_manager that supplies `market_close`.

## Kill switch wiring

Full path from `KillSwitch.engage()` to all-positions-closed:

1. `core/bot.py:39` imports `KillSwitch`. The Bot keypress handler / circuit-breaker chain calls `await kill_switch.engage(reason)`.
2. `execution/kill_switch.py:23` `KillSwitch.engage(reason)`:
   - Guards on `self._active` (re-entry block).
   - Logs CRITICAL `"KILL SWITCH ENGAGED — reason: {reason}"`.
   - Calls `database.queries.get_open_trades()` to enumerate everything currently open.
   - Builds `tasks = [self._close_position(trade) for trade in open_trades]`.
   - `await asyncio.gather(*tasks, return_exceptions=True)` — closes all in parallel.
   - Calls `database.queries.log_circuit_breaker(reason="kill_switch", detail=...)`.
   - Returns `{closed, timestamp, reason, trades}`.
3. `KillSwitch._close_position(trade)` (line 64) per trade:
   - sim: `exit_price = trade.entry_price` (TODO — no live price yet), `pnl_pct = 0.0`.
   - live: `await self._exchange_manager.market_close(exchange, pair, side, size)` → `exit_price = result.get("price", trade.entry_price)`; pnl_pct computed for long/short.
   - Calls `database.queries.close_trade(trade_id, exit_price, exit_reason="kill_switch", pnl_usd, pnl_pct)`.
4. The agent-side arb engines (`ArbEngine.close_all_positions`, `FundingRateArbEngine.close_all_positions`, `CrossChainArbEngine.close_all_positions`) are SEPARATE — they're not called by `KillSwitch`. The agent wrappers invoke their own `close_all_positions()` paths. The `KillSwitch.engage()` path closes only the main-bot Trade rows.

Files involved, in order: `core/bot.py` (caller) → `execution/kill_switch.py:KillSwitch.engage` → `database/queries.get_open_trades` → `execution/kill_switch.py:KillSwitch._close_position` (per trade in parallel) → (live only) `exchange_manager.market_close` → `database/queries.close_trade` → `database/queries.log_circuit_breaker`.

## Tests

Test files in `tests/` that import from `execution/`:

### tests/test_arb_engine.py
- `test_gross_gap_calculation` — pure `gross_gap_pct` math.
- `test_net_gap_after_fees_subtracts_both_legs` — `net_gap_pct` subtracts both legs' fees from gross.
- `test_min_gap_threshold_bitget_special_case` — Bitget unlocks `ARB_MIN_GAP_PCT`; others use fallback.
- `test_opportunity_below_threshold_not_returned` — sub-threshold gap returns None from `_find_best_opportunity`.
- `test_opportunity_above_threshold_returned` — above-threshold gap is returned.
- `test_live_fills_runs_both_legs_concurrently` — `_live_fills` fires both legs via gather.
- `test_per_symbol_lock_blocks_second_attempt` — `_symbol_locks` blocks re-entry on same pair.
- `test_circuit_breaker_halts_on_daily_loss` — `_cb_triggered` flips on daily-loss threshold.
- `test_circuit_breaker_halts_on_consecutive_losses` — `_cb_triggered` flips on streak.
- `test_circuit_breaker_clear_when_under_thresholds` — baseline no-halt.
- `test_sim_fills_apply_slippage_model` — `_sim_fills` applies slippage.
- `test_execute_arb_uses_sim_fills_in_sim_mode` — `_execute_arb` routes to sim path in sim mode.
- `test_new_exchange_in_fee_map_evaluated` — added venue auto-discovered.
- `test_dashboard_add_arb_called_on_completed_trade` — dashboard notified on success.
- `test_dashboard_not_called_on_failed_trade` — dashboard skipped on failure.
- `test_balance_check_blocks_execution_when_insufficient_funds` — `_check_balances` returns False.
- `test_balance_check_logs_miss_to_db` — `log_arb_balance_fail` written.
- `test_slippage_model_increases_with_position_size` — `slippage_pct` monotonic in size.
- `test_slippage_model_clamps_to_min_max` — `[ARB_SLIPPAGE_MIN_PCT, ARB_SLIPPAGE_MAX_PCT]` clamp.
- `test_dynamic_sizing_scales_with_gap_width` — `_dynamic_base_position * gap_ratio`.
- `test_dynamic_sizing_caps_at_multiplier_cap` — capped at `ARB_SIZE_MULTIPLIER_CAP`.
- `test_opportunity_log_records_unexecuted_gaps` — sub-threshold gaps still logged.
- `test_opportunity_log_records_executed_gaps_with_trade_id` — executed flag + trade_id linked.
- `test_funding_arb_engine_pulls_from_data_sources` — FundingRateArbEngine.fetch_funding_rates returns aggregator dict.
- `test_funding_arb_engine_returns_empty_when_no_coinglass_data` — empty {} when aggregator empty.
- `test_funding_arb_circuit_breaker_halts_on_daily_loss` — FundingRateArbEngine breaker.
- `test_funding_arb_circuit_breaker_zero_alloc_noop` — zero alloc skips % rule.
- `test_funding_arb_initial_allocation_from_fund_constant` — `_capital_allocation` initialised from `FUND_ARB_CAPITAL`.
- `test_funding_arb_set_capital_allocation_changes_field` — setter writes.
- `test_funding_arb_breaker_scales_with_live_allocation` — halt scales with live alloc.
- `test_funding_arb_breaker_zero_allocation_no_divide_by_zero` — guard.
- `test_funding_arb_fund_claim_visible_to_balance_agent` — fund claim visible in InventoryState.

### tests/test_funding_arb.py
- `test_funding_apr_annualisation_is_1095` — `funding_apr = rate_8h * 1095`.
- `test_scan_builds_delta_neutral_per_symbol` — scan emits one delta_neutral opp per symbol.
- `test_filter_drops_below_min_apr` — `_filter` floor.
- `test_negative_funding_builds_reverse_carry_variant` — negative APR → variant="reverse_carry".
- `test_filter_uses_abs_funding_apr` — symmetric filter.
- `test_filter_drops_subthreshold_negative` — sub-threshold negative skipped.
- `test_engine_binance_client_uses_future_market_type` — `defaultType=future` required.
- `test_open_refuses_reverse_carry_variant` — observation-only guard.
- `test_depth_gate_rejects_thin_oi` — `depth_ok` gate.
- `test_open_fires_legs_concurrently` — both legs via gather.
- `test_per_symbol_lock_prevents_double_open` — re-entry guard.
- `test_semaphore_caps_concurrent_opens` — semaphore enforced.
- `test_sim_fill_applies_slippage` — `_place` sim slippage.
- `test_exit_reason_funding_decay_first` — priority ordering.
- `test_exit_reason_basis_blowout` / `test_exit_reason_margin_breach` / `test_exit_reason_venue_health` / `test_exit_reason_max_hold` — each rung.
- `test_exit_reason_priority_order` — full ordering.
- `test_observation_mode_zero_routing` — observation mode bypasses open().
- `test_daily_loss_circuit_breaker_halts` — daily loss breaker.
- `test_daily_loss_circuit_breaker_zero_alloc_noop` — zero alloc no-op.
- `test_daily_loss_halt_scales_with_allocation` — scales with allocation.
- `test_consecutive_loss_circuit_breaker_halts` — streak breaker.
- `test_get_stats_and_availability` — get_stats shape.
- `test_close_all_positions_drains_via_gather` — close path.
- `test_db_roundtrip_observations_and_summary` — DB roundtrip.
- `test_coordinator_picks_up_via_registry` — registered with agent coordinator.

### tests/test_crosschain_engine.py
- `test_evaluate_pair_known_positive_edge` — `_evaluate_pair` returns positive net edge.
- `test_evaluate_pair_known_negative_edge` — negative spread caught.
- `test_optimal_notional_positive_when_spread_exists` — `optimal_notional_constant_product` positive.
- `test_capped_optimal_notional_respects_slippage_tolerance` — tolerance cap.
- `test_capped_optimal_notional_respects_max_position_cap` — position cap.
- `test_estimated_slippage_scales_linearly` — two-leg slippage linear.
- `test_gas_breakeven_at_specified_floor` — `gas_breakeven_usd` math.
- `test_evaluate_pair_refuses_300_at_5bps_budget` — below-breakeven floor blocks entry.
- `test_evaluate_pair_allows_600_at_5bps_budget` — above-breakeven allowed.
- `test_circuit_breaker_halts_on_daily_loss` — `_cb_triggered`.
- `test_circuit_breaker_zero_alloc_noop` — zero alloc no-op.
- `test_circuit_breaker_halts_on_consecutive_losses` — streak.
- `test_circuit_breaker_not_triggered_at_baseline` — baseline clean.
- `test_xchain_initial_allocation_from_settings` — initial allocation from XCHAIN_CAPITAL.
- `test_xchain_set_capital_allocation_changes_field` — setter writes.
- `test_xchain_breaker_reads_live_allocation` — breaker uses live value.
- `test_xchain_does_not_register_cex_fund_claim` — InventoryState gets no claim.
- `test_xchain_observation_behaviour_unchanged_under_zero_alloc` — zero-alloc behaves as observation.
- `test_each_evaluation_writes_exactly_one_observation_row` — 1:1 evaluation→row.
- `test_three_connector_evaluations_all_persist` — N×(N-1) pairs persisted.
- `test_skip_observation_records_skip_reason` — skip_reason written.
- `test_gas_breakeven_handles_infinite_gas` — inf handled.
- `test_optimal_notional_zero_when_no_spread` — no spread → 0.

### tests/test_chain_connectors.py
- `test_registry_lists_all_three_chains` — REGISTERED_CONNECTORS has 3 entries.
- `test_every_connector_subclasses_base` — each is a `BaseChainConnector`.
- `test_every_connector_declares_required_attrs` — connector_id, display_name, rpc_env_var set.
- `test_is_available_false_without_rpc_env_var` — env-var gate.
- `test_is_available_false_with_fill_pool_sentinel` — `<FILL>` blocks.
- `test_base_connector_submit_swap_raises_in_observation_mode` — NotImplementedError.
- `test_pool_state_roundtrip_fields` — PoolState fields preserved.
- `test_venue_config_carries_documented_tier` — VenueConfig.fee_bps preserved.

### tests/test_inventory_targets.py
- `test_needs_rebalance_true_when_drift_exceeds_theta` — drift gate.
- `test_needs_rebalance_false_just_under_theta` — under-theta clean.
- `test_weighting_favours_chains_with_more_entries` — weight ordering.
- `test_weighting_floor_prevents_zero_allocation` — `1/(2N)` floor.
- `test_cold_start_falls_back_to_equal_split` — no observations → equal weights.
- `test_observation_mode_capital_zero_emits_zero_targets` — capital=0 → 0 targets.
- `test_targets_emitted_per_enabled_chain` — one target per chain.
- `test_inventory_target_dataclass_carries_symbol` — InventoryTarget.symbol round-trips.

### tests/test_mexc_key_router.py
- `test_returns_none_when_symbol_not_in_map` — unmapped symbol.
- `test_returns_none_when_env_var_missing` — missing creds.
- `test_returns_client_when_key_provisioned` — happy path.
- `test_one_client_cached_per_index` — caching.
- `test_missing_env_caches_negative_lookup` — `_missing_indices`.
- `test_any_client_picks_lowest_configured_index` — `any_client` ordering.
- `test_any_client_falls_back_to_unmapped_key_scan` — fallback scan 1..30.
- `test_any_client_returns_none_with_no_keys` — none usable.
- `test_has_route_for_does_not_construct_client` — pure sync check.
- `test_has_route_for_missing_symbol` — unmapped returns False.
- `test_close_all_clears_pool` — `close_all` idempotent.

### tests/test_funds.py (only one execution-related test)
- `test_order_router_sizes_off_signal_fund_not_total` — `OrderRouter._portfolio_value == FUND_SIGNAL_CAPITAL` ring-fence.

### tests/test_balance_agent.py (touches `from execution.arb_engine import ArbEngine`)
- Indirect — used to construct an ArbEngine for BalanceAgent interaction tests; specific test functions are scoped to BalanceAgent behaviour and out of scope here.

## TODOs / FIXMEs / stubs

- `execution/kill_switch.py:69` — `exit_price = trade.entry_price  # TODO: use live price from market data`
- `execution/router.py:77-79` — `_live_execute` is a stub: `logger.warning("LIVE execution — real money"); return None  # Implement when ready for live`
- `execution/router.py:82` — `_get_price` stub: `return None  # MarketData provides this via callback`
- `execution/funding_engine.py:400-405` — `_place` live path is a stub: logs and returns None (per design — Phase 1).
- `execution/funding_engine.py:447-449` — `_basis` returns 0.0 unconditionally (Phase 1 placeholder for Phase 2 cross-venue).
- `execution/funding_engine.py:457-459` — `_basis_blowout` returns False (Phase 1 placeholder).
- `execution/funding_engine.py:468-470` — `_margin_breach` returns False (Phase 1 placeholder; observation never posts margin).
- `execution/arb_engine.py:986-989` — `FundingRateArbEngine._scan_loop` reads funding rates but the execution route is unwired (`# Execution path lands in a follow-up — for now the rate fetch keeps the cache warm`).
- `execution/chains/base_connector.py:149-151` — `submit_swap` raises `NotImplementedError("live execution is a separate build (XCHAIN_LIVE_ENABLED + web3 signing)")` (deliberate observation-mode invariant).
- `execution/chains/base_connector.py:126` — comment about `<FILL>` placeholder sentinel (gate, not a stub).

Total: ~10 TODO/stub/placeholder sites; most are deliberate Phase-1 / observation-mode invariants, only two are flagged as wiring-incomplete (`router._live_execute`, `kill_switch._close_position` sim exit price).

## Known issues observed

- **`OrderRouter._live_execute` is a stub** — the signal-track router has no live order placement implementation. Anything that flips `SIM_MODE = False` and routes through the main signal-track will write nothing to the exchange, but `Bot._on_new_signal` calls `execute()` which logs success only if a `trade_id` is returned (None here) → silent no-op for live signals. The ArbEngine's `_live_fills` path is the only fully live order-placement code in this package.
- **`KillSwitch._close_position` sim path uses `trade.entry_price` as the exit price**, yielding `pnl_pct = 0.0` on every kill-switch closure in sim mode (TODO line 69). This makes kill-switch P&L attribution useless for sim runs.
- **`KillSwitch._close_position` live path** depends on an `exchange_manager` that supplies `market_close(exchange, pair, side, size)`. No exchange_manager class in the codebase exposes that interface (Bot constructs `KillSwitch(exchange_manager=None)` based on `core/bot.py:39`), so the live kill path would raise `AttributeError` on `None.market_close(...)`. The `_close_position` exception handler catches it and the kill swallows silently with `"Failed to close trade {id}: {error}"`.
- **`FundingRateArbEngine._scan_loop` reads but never executes** (line 988). The funding-rate carry inside `arb_engine.py` is a permanently-OFFLINE-for-routing engine until a follow-up phase wires the spot-long/perp-short opener. Status / dashboard / breakers tick correctly but no trades will ever flow from it.
- **`FundingRateArbEngine` has no agent wrapper in `REGISTERED_AGENTS`** (commented at arb_engine.py:858-867); `set_capital_allocation` is the only seam for compounding.
- **`CrossChainArbEngine` is observation-only by construction** — `submit_swap` raises everywhere, gated behind `XCHAIN_LIVE_ENABLED` (a flag that's not yet implemented or honoured anywhere in the engine). The observation rows go to `xchain_observations` only.
- **Magic numbers:**
  - `mexc_key_router.py:88` — hardcoded `range(1, 31)` fallback scan limit (the docstring at line 87 explains why = 30 = realistic MEXC key cap, but it's still a literal in code; CLAUDE.md says no hardcoded constants outside `settings.py`).
  - `_solidly_volatile.py:78` — `SWAP_GAS_UNITS = 120_000` (operationally measured per docstring, but a class-attr literal).
  - `arbitrum.py:90` — `SWAP_GAS_UNITS = 150_000` (same comment).
  - `arb_engine.py:441` — `min(ask_liq, bid_liq) * 0.10` (10% of depth cap) is hardcoded; not in `settings.py`.
  - `arb_engine.py:566` — `factor = min(factor, 10.0)` (10× position-size clamp) is hardcoded.
- **Lazy import inside hot path** — `arb_engine.py:596` imports `agents.balance.inventory_state.inventory_state` inside `_check_balances` (per-execution import). Wrapped in try/except so it fails-open if the import errors — meaning the InventoryState scoped-pause gate silently doesn't fire if the import path breaks.
- **`execution/__init__.py` is 0 bytes** — no exports; everything is imported via fully-qualified paths.
- **`router.py:_get_price`** returns None unconditionally and the comment says "MarketData provides this via callback" — but no callback mechanism exists in `OrderRouter`. `execute()` falls back to `signal.suggested_entry` and errors out with "No price for {pair}" when that's missing.
- **`PositionManager._check_circuit_breakers` reads `settings.CIRCUIT_BREAKERS`** (a dict-of-dicts) while the main signal-Bot path independently maintains its own `CBState` — duplicated breaker logic with no shared source of truth.
- **`CrossChainArbEngine._evaluate_pair` returns `gas_bps=-1.0` and `net_edge_bps=-1.0` when math values are inf** (lines 575-579) — sentinel-by-magic-number; consumers (DB, dashboard) must know to interpret -1.0 as "infinite/unavailable".
- **Submit-swap NotImplementedError vs is_available()** — `is_available()` can return True for a connector (env var set, pool pinned) yet `submit_swap` still raises. This is intentional (observation invariant) but a future operator who pulls a connector for direct use could trip the assertion.
