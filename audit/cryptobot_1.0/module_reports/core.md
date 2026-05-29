# Module Report: core

## Purpose
The `core/` package is the central nervous system of CryptoBot. It owns the main async trading loop (`CryptoBot` in `bot.py`), the exchange-facing market data layer (REST + WebSocket streaming via `ccxt.pro`), the regime classifier that labels every pair/timeframe as TRENDING/RANGING/HIGH_VOL/CHOPPY, the three protective signal guards (BTC crash, position correlation, news), and the Claude agent that turns raw signals into GO/SKIP recommendations with entry/SL/TP suggestions. It is the orchestration layer between market data inputs (ccxt, sentiment, macro, data_sources), the signal pipeline (`signals/*`), and execution (`execution/router`, `execution/position_manager`, `execution/kill_switch`).

## Files
| File | LOC | One-sentence summary |
| --- | --- | --- |
| core/__init__.py | 1 | Empty package marker. |
| core/agent.py | 188 | Claude agent — builds markdown briefs, calls Anthropic SDK, parses GO/SKIP + numeric suggestions, exposes a module-level `agent` singleton. |
| core/bot.py | 864 | `CryptoBot` async orchestrator: main `_cycle`, circuit breaker state, approval-mode routing (per_trade/window/autonomous), background loops for heartbeat / position watcher / self-review / future price tracker / VIX subscriber. |
| core/guards.py | 290 | Three signal guards (BTC crash, correlation, news) with a `GuardRunner` aggregator and module-level `guard_runner` singleton. |
| core/market_data.py | 652 | Exchange connections via ccxt.pro: REST history fetch, WebSocket candle + order-book streaming (with per-symbol loops and sharded connections), candle indicator computation, scalp-v2 accessors (mid/EMA/ATR/VWAP/volume/order book). |
| core/regime_detector.py | 335 | Per-(pair,timeframe) regime classification using ADX, Hurst, ATR percentile and BB width; emits `RegimeSnapshot` with strategy-activation flags and score modifiers. |

## Public surface

### core/__init__.py
**Docstring:** none

**Classes:** none.

**Functions:** none.

**Module constants:** none.

### core/agent.py
**Docstring:** `core/agent.py — Claude agent — evaluates signals and generates trade recommendations.`

**Classes:**
- `ClaudeAgent` — wraps the Anthropic SDK; produces and parses the per-signal evaluation prompt and a post-trade self-review.
  - `__init__(self)` — instantiates `anthropic.Anthropic` client from `ANTHROPIC_API_KEY` env var; zeros daily cost and call counters.
  - `async evaluate_signal(self, signal: Signal, regime_snap=None, ofi_snap=None) -> Signal` — synchronously calls `client.messages.create` inside the async method, parses recommendation + numeric fields, sets `signal.claude_api_cost`, increments daily cost.
  - `_build_brief(self, signal: Signal, regime_snap=None, ofi_snap=None) -> str` — composes a markdown analyst brief with signal, indicators, timeframe checks, regime, OFI, sentiment, optional historical performance, task instructions and profile footer.
  - `_parse_response(self, signal: Signal, text: str) -> Signal` — sets `signal.claude_reasoning`; regex-extracts `RECOMMENDATION: GO/SKIP` into `signal.indicators["claude_rec"]`; regex-extracts entry/stop loss/take profit numbers if not already populated.
  - `_estimate_cost(self, response) -> float` — uses `response.usage.input_tokens` / `output_tokens` with hardcoded `0.000003` and `0.000015` USD/token rates (Sonnet); falls back to `0.001` on any error.
  - `async self_review(self, trade) -> str` — gated by `settings.SELF_REVIEW_ENABLED`; sends a brief WIN/LOSS post-mortem prompt to Claude with `SELF_REVIEW_MAX_TOKENS`.
  - `daily_cost: float` (property) — read-only accessor for accumulated daily cost.
  - `reset_daily_cost(self)` — zeros `_daily_cost` and `_call_count`.

**Functions:** none (module-level).

**Module constants:**
- `logger = logging.getLogger(__name__)`
- `agent = ClaudeAgent()` — module-level singleton instantiated at import (will call `anthropic.Anthropic(...)` even with no API key, returning a client that errors only on first request).

### core/bot.py
**Docstring:** Multi-line: describes the main `_cycle` loop running every `BOT_LOOP_INTERVAL_SEC`, the five background loops (`_cycle_loop`, `_heartbeat_loop`, `_position_watcher_loop`, `_self_review_loop`, `_future_price_tracker_loop`) launched via `asyncio.gather`, and lists the public API (`start`, `stop`, `trigger_kill_switch`, `approve_window`, `record_trade_result`).

**Classes:**
- `CircuitBreakerState` — tracks daily P&L, consecutive losses, drawdown, peak equity; the authoritative kill source for the bot's main cycle.
  - `__init__(self, starting_equity: Optional[float] = None)` — resolves starting equity in order: explicit arg → `settings.STARTING_CAPITAL` → `sum(settings.EXCHANGE_BALANCES.values())`. Initialises `daily_pnl_pct=0.0`, `consecutive_losses=0`, `current_equity`, `daily_peak_equity`, `last_reset_date`, `halted=False`, `halt_reason=None`, `halt_at=None`.
  - `drawdown_pct -> float` (property) — `(current_equity - daily_peak_equity) / daily_peak_equity * 100`; zero when peak is non-positive.
  - `reset_if_new_day(self)` — when UTC date rolls over, zero `daily_pnl_pct` and re-seed `daily_peak_equity` from current equity.
  - `update_after_trade(self, pnl_pct: float)` — pnl_pct as fraction; updates `daily_pnl_pct`, consecutive-loss streak, current_equity, peak.
  - `evaluate(self) -> tuple[bool, str]` — checks each of `daily_loss`, `consecutive_loss`, `drawdown` rules from `settings.CIRCUIT_BREAKERS`; returns `(should_halt, reason)`.
  - `halt(self, reason: str)` — sets halted flag, reason, and `halt_at = datetime.utcnow()`.

- `CryptoBot` — async trading bot orchestrator. Constructor accepts injectable collaborators for testability.
  - `__init__(self, profile=None, strategy=None, kill_switch: Optional[KillSwitch] = None, market_data: Optional[MarketData] = None, signal_engine: Optional[SignalEngine] = None, position_mgr: Optional[PositionManager] = None, router: Optional[OrderRouter] = None)` — lazy-resolves defaults from `profile_manager`, `strategies.get_strategy`, `KillSwitch`, `MarketData`, `SignalEngine`, `PositionManager`, `OrderRouter`. Calls `signal_engine.setup_scanners()` if present. Imports `sentiment` singleton. Seeds `_cb_state` from `db_queries.get_last_equity()`. Sets up window-mode and autonomous-mode rate-limit lists, `_pending_signals: asyncio.Queue`, closed-trade count, BTC 30m snapshot tracker, command-bar state, pause flag. Wires `signal_engine.on_signal(self._on_signal)` and subscribes `_on_vix_crisis` to `data_sources.subscribe("fred.vix", ...)` with a `DATA_VIX_CRISIS_MIN` filter.
  - `async start(self)` — sets `_running=True`, calls `init_db()`, logs mode/profile/strategy/approval, starts `ApprovalInputHandler` if stdin is a tty, launches `data_sources.run_refresh_loop` and `macro_monitor.run_refresh_loop` lazily, then `await asyncio.gather(market_data.start(), _cycle_loop(), _heartbeat_loop(), _position_watcher_loop(), _self_review_loop(), _future_price_tracker_loop(), data_sources_loop, macro_loop, return_exceptions=True)`.
  - `_on_vix_crisis(self, new_point, prev_point) -> None` — fires CRITICAL log when VIX clears `DATA_VIX_CRISIS_MIN`.
  - `async stop(self)` — flags `_running=False`, awaits `market_data.stop()`.
  - `async trigger_kill_switch(self, reason: str = "manual") -> dict` — engages the kill switch then halts circuit breaker.
  - `approve_window(self, duration_minutes: int) -> datetime` — clamps to `[WINDOW_MIN_DURATION_MINUTES, WINDOW_MAX_DURATION_MINUTES]`, sets `_window_until`.
  - `async approve_next_pending(self) -> bool` — drains one pending signal and calls `_execute_signal`; returns False when queue empty.
  - `async skip_next_pending(self, reason: str = "user_skipped") -> bool` — drains one pending signal and records it skipped.
  - `async shutdown(self, reason: str = "user_quit") -> None` — idempotent; writes final portfolio snapshot, logs `SHUTDOWN` agent_event when `SHUTDOWN_LOG_EVENT`, stops market_data.
  - `peek_pending(self)` — returns next pending signal without removing it; reaches into `asyncio.Queue._queue` deque internal.
  - `record_trade_result(self, pnl_pct: float)` — resets day, updates state, evaluates CB; on first trip logs to `db_queries.log_circuit_breaker` and critically logs.
  - `async _cycle_loop(self)` — `while self._running: try _cycle except log; await asyncio.sleep(BOT_LOOP_INTERVAL_SEC)`.
  - `toggle_pause(self) -> bool` — flips `_paused`.
  - `async _cycle(self)` — pause guard → reset_if_new_day → dead-zone guard → CB re-evaluation → sentiment session_floor (best-effort) → macro hard block / pre-event pause → `signal_engine.run_scan()`.
  - `async _on_signal(self, signal)` — fetches regime + OFI snapshots, calls `agent.evaluate_signal`, takes price snapshot, persists Claude verdict via `db_queries.update_signal_claude`, dispatches to `_route_for_approval`.
  - `async _route_for_approval(self, signal, price_at_signal: Optional[float])` — branches on `settings.APPROVAL_MODE`: claude `SKIP` short-circuits; `autonomous` checks rate limit then executes; `window` checks window open + rate limit; default (`per_trade`) queues to `_pending_signals`.
  - `_record_skip(self, signal, reason: str, price_at_signal: Optional[float])` — calls `db_queries.update_signal_skip` and logs.
  - `async _execute_signal(self, signal)` — refuses when halted; calls `router.execute(signal, profile)`; appends timestamps to auto/window rate-limit buffers; logs.
  - `_window_open(self) -> bool` — `_window_until is not None and now < _window_until`.
  - `_window_rate_limit_ok(self) -> bool` — caps via `settings.WINDOW_MAX_TRADES_PER_HOUR`.
  - `_auto_rate_limit_ok(self) -> bool` — enforces `AUTO_MAX_TRADES_PER_HOUR` and `AUTO_MAX_TRADES_PER_DAY`.
  - `async _heartbeat_loop(self)` — sleeps `HEARTBEAT_INTERVAL_SEC`; refreshes sentiment, updates `guard_runner.btc_guard.update_price`, calls `sentiment.set_btc_change_30m` on mature snapshot.
  - `async _position_watcher_loop(self)` — sleeps `POSITION_WATCHER_INTERVAL_SEC`; calls `position_mgr.check_positions()`; per closed trade pulls the row and calls `record_trade_result(trade.pnl_pct)`.
  - `async _self_review_loop(self)` — sleeps 60s; gated by `SELF_REVIEW_ENABLED`; every `SELF_REVIEW_EVERY_N_TRADES` closes calls `agent.self_review` and persists via `db_queries.save_postmortem`.
  - `async _future_price_tracker_loop(self)` — sleeps `FUTURE_PRICE_TRACKER_INTERVAL_SEC`; fills `price_1h`/`price_4h`/`price_24h` columns on past signals via `db_queries.update_signal_future_prices`.
  - `_in_dead_zone(self) -> bool` — compares UTC HH:MM against `settings.SESSION_WINDOWS["dead_zone"]`.
  - `_price_for(self, signal) -> Optional[float]` — prefers `signal.suggested_entry`, falls back to `_price_for_pair`.
  - `_price_for_pair(self, pair: str, exchange: Optional[str] = None) -> Optional[float]` — best-effort via `market_data.get_price` then `get_all_prices`.
  - `_btc_price_with_fallback(self) -> Optional[float]` — priority: market_data → `data_sources.cryptocompare.get_price("BTC")` → `data_sources.bybit_derivs.get_mark_price("BTC/USDT")` → None.
  - `_update_btc_snapshot(self, current_price: float) -> Optional[float]` — discrete 30m tracker; returns None on first call or before `(BTC_GUARD_LOOKBACK_MINUTES - BTC_GUARD_LOOKBACK_DRIFT_MINUTES)` mins old; returns percent delta and replaces snapshot otherwise.
  - `_get_trade_by_id(self, trade_id: int)` — delegates to `db_queries.get_trade_by_id`.
  - `async _fetch_sentiment(self) -> dict` — pulls `sentiment.get_current()`; translates aggregator's -100..+100 to 0..100; returns `{"MARKET": {"composite", "velocity", "fear_greed", "hard_block"}}`.

**Functions:** none (module-level).

**Module constants:**
- `logger = logging.getLogger(__name__)`

### core/guards.py
**Docstring:** `core/guards.py — Three protective guards that run on every signal before it reaches Claude. BTC Guard / Correlation Guard / News Guard.`

**Classes:**
- `BTCGuard` — detects BTC crash and penalises alt signals.
  - `__init__(self)` — empty `_price_history: list[tuple[float, float]]`, `_triggered=False`, `_trigger_time: Optional[float]=None`.
  - `update_price(self, price: float)` — appends `(now, price)` to history, trims to `settings.BTC_CRASH_WINDOW_MINUTES`, calls `_check`.
  - `_check(self)` — flips `_triggered` when drop `>= settings.BTC_CRASH_PCT`; records trigger time on transition.
  - `apply(self, signal: Signal) -> float` — returns 0 when disabled / not triggered / pair contains BTC and is arb; otherwise `-settings.BTC_GUARD_SCORE_PENALTY`.
  - `is_triggered: bool` (property).
  - `change_pct_30m(self) -> Optional[float]` — percent change between oldest and newest price in window; consumed by sentiment aggregator.
  - `status(self) -> str` — `"⚠ TRIGGERED Nm ago"` or `"OK"`.

- `CorrelationGuard` — blocks/penalises over-correlated exposure against open positions.
  - `apply(self, signal: Signal, open_positions: list) -> float` — returns 0 when disabled / arb-exempt / no positions; reads `_KNOWN_HIGH_CORR`; returns `-999.0` above `settings.CORR_BLOCK_THRESHOLD`, `-settings.CORR_PENALTY_AMOUNT` above `CORR_HIGH_THRESHOLD`, else 0.
  - `update_correlations(self, correlation_matrix: dict)` — mutates the module-level `_KNOWN_HIGH_CORR` dict in place.

- `NewsGuard` — scans recent headlines for block/warn keywords.
  - `__init__(self)` — empty `_events: list[dict]`, `_last_scan=0.0`.
  - `ingest(self, headline: str, timestamp: Optional[float] = None)` — case-insensitive scan against `settings.NEWS_GUARD_BLOCK_KEYWORDS` then `NEWS_GUARD_WARN_KEYWORDS`; stores `{"text", "ts", "level"}`.
  - `apply(self, signal: Signal) -> float` — within `NEWS_GUARD_LOOKBACK_MINUTES`, returns `-NEWS_GUARD_PENALTY_BLOCK` if any block event, `-NEWS_GUARD_PENALTY_WARN` if any warn, else 0.
  - `active_events(self) -> list[dict]` — events within lookback.
  - `is_clear(self) -> bool` — `not active_events()`.
  - `status(self) -> str` — formatted summary of block/warn counts.
  - `purge_old(self)` — drops events older than 2× lookback.

- `GuardRunner` — applies all three guards.
  - `__init__(self)` — instantiates `btc_guard: BTCGuard`, `corr_guard: CorrelationGuard`, `news_guard: NewsGuard`.
  - `apply_all(self, signal: Signal, open_positions: list) -> tuple[float, list[str]]` — sums penalties; returns `(total, reasons)`. `total <= -999` indicates block.
  - `status_summary(self) -> dict` — `{"btc_guard": ..., "news_guard": ...}`.

**Functions:** none.

**Module constants:**
- `logger = logging.getLogger(__name__)`
- `_KNOWN_HIGH_CORR: dict[frozenset, float]` — hardcoded static correlation table for 9 pair-pairs (ETH/SOL=0.88, ETH/AVAX=0.85, ETH/BNB=0.82, BTC/ETH=0.80, SOL/AVAX=0.83, BTC/SOL=0.78, MATIC/ETH=0.81, DOT/ETH=0.79, LINK/ETH=0.77).
- `guard_runner = GuardRunner()` — module-level singleton.

### core/market_data.py
**Docstring:** `core/market_data.py — Exchange connections, WebSocket streaming, and candle management.`

**Classes:**
- `MarketData` — main exchange manager; owns ccxt clients, candle DataFrames, last prices, last books, short-window price history.
  - `PRICE_HISTORY_WINDOW_SEC = 300` (class attr) — sample buffer size used by `get_change_pct` and `get_mid_price_at_offset`.
  - `__init__(self, dashboard=None)` — dicts: `_exchanges`, `_candles` (keyed by `(exchange, pair, tf)`), `_last_price` (keyed by `(exchange, pair)`), `_last_book`, `_price_history` (deques); lists: `_callbacks`, `_book_callbacks`, `_ob_conns`, `_active_pairs`; `_running=False`; optional `_dashboard`.
  - `set_dashboard(self, dashboard) -> None` — late-binds dashboard.
  - `_report_health(self, exchange: str, latency_ms: float, connected: bool) -> None` — best-effort push to dashboard.
  - `on_candle_close(self, fn)` — registers candle-close callback.
  - `on_book_update(self, fn)` — registers `fn(exchange, pair, bids, asks)` callback fired on every book tick.
  - `get_candles(self, exchange, pair, timeframe)` — returns DataFrame or None.
  - `get_latest_candle(self, exchange, pair, timeframe)` — last row or None.
  - `get_price(self, exchange, pair)` — last cached close, or None.
  - `get_all_prices(self, pair)` — `{exchange: price}` across all venues for a pair.
  - `get_book(self, exchange, pair)` — most recent ccxt-shape book, or None.
  - `get_spread_bps(self, exchange, pair)` — top-of-book spread in bps; None on missing/malformed book.
  - `get_change_pct(self, exchange, pair, window_sec)` — % change from current mid vs the sample nearest `window_sec` ago; None when insufficient history.
  - `_record_price_sample(self, exchange, pair, mid_price)` — append to bounded per-pair sample deque.
  - `get_mid_price(self, symbol, exchange)` — top-of-book mid, falls back to last price.
  - `get_mid_price_at_offset(self, symbol, exchange, offset_ms)` — mid `offset_ms` ago from sample buffer.
  - `_candle_tf(self, exchange, symbol, preferred)` — returns preferred tf if cached, else fastest from `settings.TIMEFRAMES`.
  - `_last_finite(series)` (static) — last non-NaN float or None.
  - `get_session_vwap(self, symbol, exchange)` — latest VWAP from fastest candle frame.
  - `get_ema(self, symbol, exchange, timeframe, period)` — computes `ta.trend.EMAIndicator(period)` on close; None on missing frame.
  - `get_atr(self, symbol, exchange, period, timeframe)` — computes `ta.volatility.AverageTrueRange(period)`; None on missing frame.
  - `get_current_minute_volume(self, symbol, exchange)` — latest candle volume from fastest available frame (prefers `"1m"`).
  - `get_rolling_median_volume(self, symbol, exchange, timeframe, lookback)` — median of last `lookback` volumes.
  - `get_order_book(self, symbol, exchange, levels=5)` — returns `OrderBook(bids=[OBLevel(price,size)], asks=[…])`; None on missing book.
  - `active_pairs(self)` — `_active_pairs` list.
  - `async start(self)` — boots every `settings.ENABLED_EXCHANGES` via `_make_exchange`; calls `load_markets`; resolves pairs; loads history; runs `_stream_candles` + `_stream_orderbooks` per exchange via `asyncio.gather`.
  - `async stop(self)` — flags `_running=False`; closes every primary client and every `_ob_conns` shard.
  - `async _resolve_pairs(self)` — if `PAIR_UNIVERSE == "manual"` returns `FALLBACK_PAIRS`; else uses first exchange's `fetch_tickers`, sorts by quoteVolume, takes top `PAIR_UNIVERSE_TOP_N`.
  - `async _load_history(self)` — fetches OHLCV for first 20 active pairs × all timeframes via `asyncio.gather`.
  - `async _fetch_candles(self, exchange_name, ex, pair, tf)` — pulls history, builds DataFrame, runs `_compute_indicators`, seeds `_last_price`, feeds `regime_detector` for every row.
  - `_update_regime(self, pair, tf, row)` — wraps `regime_detector.update(...)` with None-safe column reads.
  - `async _stream_candles(self, exchange_name, ex)` — gated on `ex.has["watchOHLCV"]`; launches `_stream_one_candle` for first `ORDER_BOOK_STREAM_PAIRS` × `TIMEFRAMES`.
  - `async _stream_one_candle(self, exchange_name, ex, pair, tf)` — awaits `watch_ohlcv` with `ORDER_BOOK_WATCH_TIMEOUT_S` timeout; reports health; backs off on errors via `ORDER_BOOK_ERROR_BACKOFF_S`.
  - `async _process_candle(self, exchange_name, pair, tf, raw)` — updates `_last_price`, appends/upserts row in DataFrame (trims to `CANDLE_LOOKBACK + 50`), recomputes indicators, updates regime, fires `_callbacks`.
  - `async _stream_orderbooks(self, exchange_name, ex)` — gated on `ex.has["watchOrderBook"]`; resolves pairs via `_orderbook_pairs_for`; if pairs > `ORDER_BOOK_MAX_STREAMS_PER_CONN` shards across dedicated clients.
  - `_orderbook_pairs_for(self, exchange_name, ex)` — base = `_active_pairs[:ORDER_BOOK_STREAM_PAIRS]`; for scalp venues from `settings.STRATEGY_EXCHANGE_MAP["scalp"]` also adds `settings.SCALP_PAIRS`; filters to listed markets.
  - `async _stream_orderbook_shard(self, exchange_name, pairs)` — creates dedicated ccxt.pro client, tracks in `_ob_conns`, calls `load_markets`, runs per-symbol `_stream_one_orderbook`.
  - `async _stream_one_orderbook(self, exchange_name, ex, pair)` — awaits `watch_order_book(pair, ORDER_BOOK_DEPTH)`; persists book, samples mid into history deque, calls `ofi_scorer.update_book`, fans tick to `_book_callbacks`; on `NotSupported` stops the symbol's loop; on other errors backs off.

**Functions:**
- `_make_exchange(name)` — builds a ccxt.pro client with per-exchange config (Binance public-only with `fetchCurrencies=False` + `adjustForTimeDifference` + `recvWindow=10000`; Kraken; Bybit `defaultType=spot`; OKX with passphrase); always sets `enableRateLimit=True`.
- `_compute_indicators(df)` — uses `ta.*` to add RSI (`RSI_PERIOD`), MACD (`MACD_FAST/SLOW/SIGNAL`), Bollinger (`BB_PERIOD/STDDEV`), EMA fast/slow/trend (`EMA_FAST/SLOW/TREND`), ATR (window 14, hardcoded), ADX (window 14, hardcoded), volume SMA (window 20, hardcoded), VWAP with fallback to 20-period close rolling mean.

**Module constants:**
- `logger = logging.getLogger(__name__)`
- `OBLevel = namedtuple("OBLevel", ["price", "size"])`
- `OrderBook = namedtuple("OrderBook", ["bids", "asks"])`

### core/regime_detector.py
**Docstring:** `core/regime_detector.py — Classifies the current market regime for every pair on every timeframe. Runs continuously — updates on each candle close. Regimes: TRENDING / RANGING / HIGH_VOL / CHOPPY.`

**Classes:**
- `RegimeSnapshot` (dataclass) — full regime picture for one pair at one timeframe.
  - Fields: `pair: str`, `timeframe: str`, `timestamp: datetime`, `regime: str = UNKNOWN`, `adx: Optional[float] = None`, `atr: Optional[float] = None`, `atr_percentile: Optional[float] = None`, `bb_width: Optional[float] = None`, `bb_width_avg: Optional[float] = None`, `hurst: Optional[float] = None`, `hurst_label: str = "unknown"`, `is_trending/is_ranging/is_high_vol/is_choppy: bool = False`, `momentum_ok/reversion_ok/grid_ok/sweep_ok: bool = False`, `arb_ok: bool = True`, `score_modifier: float = 0.0`.
  - `summary(self) -> str` — formatted single-line summary.

- `RegimeDetector` — keeps regime state for every `(pair, timeframe)`.
  - `__init__(self)` — `_regimes: dict[tuple, RegimeSnapshot]`, `_hurst: dict[tuple, RollingHurst]`, `_atr_history: dict[tuple, list[float]]`, `_bb_width_history: dict[tuple, list[float]]`.
  - `update(self, pair: str, timeframe: str, close: float, high: float, low: float, adx: Optional[float], atr: Optional[float], bb_upper: Optional[float], bb_lower: Optional[float], bb_mid: Optional[float]) -> RegimeSnapshot` — lazily allocates `RollingHurst(lookback=settings.HURST_LOOKBACK_BARS)`, appends ATR (capped at `settings.ATR_PERCENTILE_LOOKBACK`), computes BB width and rolling mean (capped at 50), classifies, computes strategy-OK flags and score modifier, caches and returns snapshot.
  - `get(self, pair: str, timeframe: str) -> Optional[RegimeSnapshot]` — keyed lookup.
  - `get_primary(self, pair: str) -> Optional[RegimeSnapshot]` — regime on `settings.SLOW_TIMEFRAME`.
  - `all_regimes(self) -> list[RegimeSnapshot]` — values.
  - `pairs_by_regime(self, regime: str) -> list[str]` — unique pairs in regime on slow TF.
  - `_classify(self, adx, atr_pct, hurst, bb_width, bb_width_avg) -> str` — order of precedence: HIGH_VOL when `atr_pct >= ATR_HIGH_VOL_PERCENTILE`, CHOPPY when `adx < ADX_CHOPPY_MAX`, TRENDING when ADX strong + Hurst persistent (or ADX strong + Hurst absent), RANGING when ADX weak, fallback to Hurst-based call, else UNKNOWN.
  - `_momentum_ok(self, regime, adx, hurst) -> bool` — only TRENDING/HIGH_VOL; respects `MOMENTUM_REQUIRE_HURST` and `MOMENTUM_MIN_ADX`.
  - `_reversion_ok(self, regime, adx, hurst) -> bool` — blocks TRENDING/CHOPPY/UNKNOWN; respects `REVERSION_REQUIRE_HURST` and `REVERSION_MAX_ADX`.
  - `_grid_ok(self, regime, adx) -> bool` — only when `GRID_ENABLED` and `ADX_GRID_MIN <= adx <= ADX_GRID_MAX`.
  - `_score_modifier(self, regime, adx, hurst, atr_pct) -> float` — returns `-30.0` for CHOPPY, `+5.0` for TRENDING (`+5.0` extra above `ADX_STRONG_TREND`), `+3.0` for HIGH_VOL; Hurst bonuses: `+5.0` if `>=0.60`, `+3.0` if `<=0.42`, `-5.0` if `0.47 <= hurst <= 0.53`.

**Functions:** none (module-level).

**Module constants:**
- `logger = logging.getLogger(__name__)`
- `TRENDING = "trending"`
- `RANGING = "ranging"`
- `HIGH_VOL = "high_vol"`
- `CHOPPY = "choppy"`
- `UNKNOWN = "unknown"`
- `regime_detector = RegimeDetector()` — module-level singleton.

## Imports graph

### This package imports from elsewhere in the project
- From `config`: `settings`
- From `signals.base`: `Signal`
- From `signals.ofi`: `ofi_scorer`
- From `database.queries`: `get_signal_history`, `get_signal_win_rate`, `save_candle`, and via `from database import queries as db_queries` in `bot.py` (used: `get_last_equity`, `update_signal_claude`, `update_signal_skip`, `log_circuit_breaker`, `log_portfolio_snapshot`, `log_agent_event`, `get_recent_closed_trades`, `save_postmortem`, `get_signals_needing_price_update`, `update_signal_future_prices`, `get_trade_by_id`)
- From `database.db`: `init_db`
- From `execution.kill_switch`: `KillSwitch`
- From `execution.position_manager`: `PositionManager`
- From `execution.router`: `OrderRouter`
- From `signals.engine`: `SignalEngine`
- From `utils.hurst`: `RollingHurst`
- Lazy imports inside `core/bot.py` (deferred to avoid early boot / circular import): `from profiles.profile_manager import profile_manager`, `from strategies import get_strategy`, `from sentiment import sentiment`, `from data_sources import data_sources`, `from macro import macro_monitor`, `from ui.prompts import ApprovalInputHandler`.
- `core/market_data.py` lazy: imports `core.regime_detector.regime_detector` (in-package).

### This package is imported by
- `core/market_data.py` (intra-package: imports `core.regime_detector`)
- `core/bot.py` (intra-package: imports `core.agent`, `core.guards`, `core.market_data`, `core.regime_detector`)
- `signals/momentum.py` — imports `core.regime_detector.regime_detector`
- `signals/reversion.py` — imports `core.regime_detector.regime_detector`
- `signals/quality_gate.py` — imports `core.regime_detector.regime_detector`, `CHOPPY`, and `core.guards.guard_runner`
- `ui/web_server.py` — imports `core.regime_detector` (line 1215, runtime)
- `ui/dashboard.py` — imports `core.regime_detector.regime_detector` (lines 512, 703)
- `agents/__init__.py` — imports `core.bot.CryptoBot` (line 96)
- `agents/scalping_agent.py` — imports `core.regime_detector` as `_rd` (line 768)
- `tests/test_bot.py` — imports `core.bot.CryptoBot`, `CircuitBreakerState`
- `tests/test_market_data_stream.py` — imports `core.market_data.MarketData`
- `tests/test_scalp_v2_accessors.py` — imports `core.market_data.MarketData`, `OrderBook`, `OBLevel`
- `tests/test_scalping_agent.py` — imports `core.market_data.MarketData` (multiple test setups)

## Plugin registrations
None. The `core/` package does not define or register any `REGISTERED_*` list. It exposes three module-level singletons (`core.agent.agent`, `core.guards.guard_runner`, `core.regime_detector.regime_detector`) that other modules import directly.

## Tests
- `tests/test_bot.py` — imports `CryptoBot`, `CircuitBreakerState`.
  - `test_peek_pending_returns_none_when_empty` — `peek_pending()` on empty queue returns None.
  - `test_peek_pending_returns_front_without_consuming` — peek twice does not drain the queue.
  - `test_cb_state_trips_on_daily_loss` — `-2.5%` trade trips `daily_loss` rule.
  - `test_cb_state_trips_on_consecutive_losses` — three tiny losses trip streak rule.
  - `test_cb_state_resets_streak_on_win` — winning trade resets `consecutive_losses` to 0.
  - `test_cycle_skips_dead_zone` — `_cycle` at 03:00 UTC skips `run_scan`.
  - `test_cycle_runs_outside_dead_zone` — `_cycle` at 13:00 UTC calls `run_scan` once.
  - `test_record_trade_result_halts_on_daily_loss` — `-3%` triggers halt and logs CB row.
  - `test_cycle_skips_when_halted` — halted CB causes `_cycle` to skip `run_scan`.
  - `test_per_trade_queues_signal` — per_trade mode queues, does not execute.
  - `test_autonomous_executes_within_limits` — autonomous mode executes immediately.
  - `test_autonomous_skip_when_rate_limited` — saturated hourly cap prevents execution.
  - `test_window_open_executes` — window open + within rate limit executes.
  - `test_window_closed_queues` — window closed queues the signal.
  - `test_claude_skip_short_circuits` — `claude_rec == "SKIP"` blocks execution.
  - `test_btc_snapshot_first_call_stores_returns_none` — first call seeds tracker, returns None.
  - `test_btc_snapshot_immature_window_returns_none` — pre-maturity call returns None and keeps snapshot.
  - `test_btc_snapshot_mature_window_emits_delta` — mature window returns ~`-2%` and resets snapshot.
  - `test_btc_price_fallback_uses_data_sources_when_market_data_empty` — falls back to `data_sources.cryptocompare`.
  - `test_btc_price_fallback_returns_none_when_no_source` — all sources empty → None, no crash.
  - `test_startup_reads_last_equity_from_db` — seeds `current_equity` from `get_last_equity`.
  - `test_startup_uses_starting_capital_when_db_empty` — falls through to `STARTING_CAPITAL`.
  - `test_capital_allocation_matches_settings` — `SIGNAL_AGENT_CAPITAL`/`ARB_AGENT_CAPITAL` propagate to agent wrappers.
  - `test_clean_shutdown_writes_final_snapshot` — `shutdown` writes a portfolio snapshot with `SHUTDOWN` status.
  - `test_clean_shutdown_logs_agent_event` — `shutdown` logs `("portfolio", "SHUTDOWN", reason)`.
  - `test_shutdown_is_idempotent` — second `shutdown` is a no-op.
  - `test_approve_next_pending_drains_queue_and_executes` — `a` command path drains and executes.
  - `test_approve_next_pending_when_empty_is_noop` — empty queue returns False.
  - `test_skip_next_pending_records_skip` — `skip_next_pending` calls `update_signal_skip`.
  - `test_toggle_pause_flips_flag_and_returns_state` — `toggle_pause` flips `_paused`.
  - `test_paused_cycle_skips_scan` — paused state causes `_cycle` to skip scan.
- `tests/test_market_data_stream.py` — imports `MarketData`.
  - `test_stream_one_orderbook_processes_then_backs_off` — processes book, feeds OFI, backs off on error (no spin).
  - `test_stream_one_orderbook_fires_book_callbacks` — registered `on_book_update` subscribers receive every tick.
  - `test_orderbook_pairs_for_scalp_venue_streams_full_universe` — scalp venue streams the full `SCALP_PAIRS` (filtered to listed markets); signal venue gets only top-N.
  - `test_stream_orderbooks_shards_over_cap` — over per-conn cap, sharding creates dedicated clients (10 pairs ÷ 4/conn = 3 shards).
  - `test_stream_one_orderbook_skips_when_not_running` — non-running state skips immediately.
  - `test_stream_orderbooks_noop_without_watch` — REST-only ccxt client returns without raising.
  - `test_stream_one_candle_processes_then_backs_off` — candle stream processes bar, backs off on error.
  - `test_stream_candles_noop_without_watch` — REST-only ccxt client returns without raising.
- `tests/test_scalp_v2_accessors.py` — imports `MarketData`, `OrderBook`, `OBLevel`.
  - `test_get_mid_price_from_book` — mid from top-of-book.
  - `test_get_mid_price_falls_back_to_last_price` — last_price fallback when no book.
  - `test_get_order_book_shape` — returns `OrderBook` of `OBLevel` per side.
  - `test_get_ema_and_atr_from_candles` — EMA and ATR computed from cached DataFrame.
  - `test_get_vwap_and_volume` — VWAP + minute-volume + rolling-median accessors.
  - `test_get_mid_price_at_offset` — mid from sample buffer at offset.
  - `test_ofi_get_z_score_none_until_bucket_closes` — `OFIEngine` test (scalp agent module, not core).
  - `test_ofi_get_exchanges_for_symbol` — `OFIEngine` test (scalp agent module, not core).
- `tests/test_scalping_agent.py` — imports `MarketData` in five test setups (uses it as a stub for the scalp agent's market data needs; tests are not asserting `core` behaviour).

## TODOs / FIXMEs / stubs
- `core/bot.py:604` — `# TODO: ui/prompts.py is a stub; until it exists, per_trade signals only land in the queue and never auto-execute.`
- `core/bot.py:677` — `# TODO: recompute equity from market_data + open positions and feed self._cb_state.current_equity so drawdown stays accurate.`

No FIXME, XXX, HACK, NotImplementedError or `pass  #` markers found anywhere in the package. The comment in `bot.py:604` is partly out of date — `ui/prompts.py` is actually present and used (see `from ui.prompts import ApprovalInputHandler` at `bot.py:267`), and `approve_next_pending` / `skip_next_pending` drain the queue. The TODO text predates that wiring.

## Known issues observed

- `core/agent.py` — `agent = ClaudeAgent()` runs at import time, which calls `anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))`. If the env var is missing the SDK accepts None and only fails at first request, but it still couples module import to the SDK being installed. There is no test stub for this singleton; tests that import `core.bot` transitively run this.
- `core/agent.py:153` — Per-token cost rates (`0.000003` input, `0.000015` output, i.e. Claude 3.5 Sonnet) are hardcoded and would silently drift if `settings.CLAUDE_MODEL` is changed to a different model. The CLAUDE.md invariant says "no magic numbers outside settings.py" — these should live in `settings.CLAUDE_COST_PER_INPUT_TOKEN` etc.
- `core/agent.py:128` — `import re` happens inside `_parse_response` on every call instead of at module top. Minor; the import is cached but the lookup runs each time.
- `core/agent.py:104-105`, `core/agent.py:133-146`, `core/agent.py:150-155`, `core/agent.py:176-178` — multiple bare `except:` / `except Exception:` clauses that swallow all errors silently. The catch in `evaluate_signal` (line 43) sets `claude_reasoning` but does not set `indicators["claude_rec"]`, so a failed Claude call lands as the default — i.e. the signal goes into "UNCLEAR" status only if `_parse_response` ran. After an exception, `claude_rec` is never set at all and `_route_for_approval` reads `(signal.indicators or {}).get("claude_rec", "")`, treating the missing key as "neither GO nor SKIP" → still executes on autonomous/window. Failure-mode is therefore "trade through" rather than "skip on uncertainty".
- `core/agent.py:65` — `if signal.bb_position is not None:` uses `is not None` but other indicators above (`signal.rsi`, `signal.volume_ratio`, `signal.arb_gap_pct`, `signal.macd_hist`) use truthiness — so a zero RSI or zero arb gap is silently dropped from the brief.
- `core/bot.py:418-427` — `peek_pending` reaches into `asyncio.Queue._queue` private attr; flagged in its own comment but is a CPython-internal coupling.
- `core/bot.py:677` (TODO) — `_heartbeat_loop` never recomputes equity from live market data, so `_cb_state.current_equity` stays exactly at the post-last-trade value forever between trades. This means `drawdown_pct` only reflects realised P&L, not unrealised mark-to-market drawdown.
- `core/bot.py:599-600` — Window-closed signals are pushed to `_pending_signals` but never explicitly drained when the window opens; if the user runs `approve_window` while signals already sit in the queue, those signals will not auto-execute (they wait for an `a` keystroke).
- `core/bot.py` mode dispatch — `_route_for_approval` only knows the three approval modes by string literal (`"autonomous"`, `"window"`, default = per_trade). There is no validation of `settings.APPROVAL_MODE`, so a typo silently degrades to per_trade.
- `core/guards.py:102-112` — `_KNOWN_HIGH_CORR` is a hardcoded static table. `CorrelationGuard.update_correlations` mutates this module-level dict (not the instance), so updates persist across the module's lifetime but are not scoped to the guard instance. Also, the table is keyed by `frozenset([pair_a, pair_b])` of exact USDT-quoted strings — does not handle `BTC/USD`, perp suffixes, or any non-USDT quote.
- `core/guards.py:67` — The BTC guard exemption is `"BTC" in signal.pair and signal.signal_type == "arb"`. This matches `WBTC`, `BTCUP`, `1000BTCSHIB`, etc. — the substring check is too loose. Should be a proper symbol/base check.
- `core/guards.py:135-149` — Correlation guard only reads `_KNOWN_HIGH_CORR`; pairs missing from the table are treated as 0.0 correlation. No fallback to a computed correlation from price history.
- `core/guards.py:175-193` — `NewsGuard.ingest` does substring matching in `headline.lower()`. With keywords like `"hack"` this will match `"Hackathon"` headlines. There is no anchor/word-boundary check.
- `core/guards.py:236-239` — `NewsGuard.purge_old` uses 2× the lookback as its expiry — there is no scheduler that calls it; the dispatcher relies on `apply` filtering. Without a periodic call the `_events` list grows unbounded over a long run.
- `core/market_data.py:67-68` — Hardcoded `window=14` for ATR and ADX in `_compute_indicators`; everywhere else uses `settings.*_PERIOD` constants. Violates the "no magic numbers" CLAUDE.md invariant.
- `core/market_data.py:69-73` — `volume_sma` window 20 is hardcoded; VWAP rolling fallback uses 20 again; both should be settings constants.
- `core/market_data.py:418` — `_load_history` hardcodes `self._active_pairs[:20]`; ignores `ORDER_BOOK_STREAM_PAIRS` or any setting. So if the bot is configured to monitor 96 scalp pairs, only the first 20 ever get historical OHLCV preloaded — the rest must wait until the WS stream fills in.
- `core/market_data.py:519-521` — In `_process_candle`, the candle DataFrame is trimmed only when a new bar arrives (`new_row.index[0] not in df.index` branch); in the upsert branch the DataFrame is never re-trimmed. Slow-growing key edge but bounded.
- `core/market_data.py:639-640` — On `asyncio.TimeoutError` the orderbook stream just `continue`s without backing off; this means a perma-stalled WS that lets `wait_for` time out cleanly will spin without any delay. The error path does back off, but timeouts do not.
- `core/market_data.py:386-390` — `start` calls `asyncio.gather(*tasks, return_exceptions=True)` for all streams. There is no supervisor loop — if a per-symbol streamer exits permanently (e.g. `NotSupported` at `_stream_one_orderbook`), nothing relaunches it. The bot continues without book data for that symbol silently except the warning log.
- `core/regime_detector.py:73-79` — `RegimeSnapshot.summary` uses f-strings that put a conditional expression *inside* the format spec (e.g. `f"ADX={self.adx:.1f if self.adx else '?'}"`). This is invalid: the part after `:` is a format-spec, not a Python expression. Calling `summary()` raises `ValueError: Invalid format specifier`. It is currently invoked at `core/regime_detector.py:197` as `logger.debug(snap.summary())` — at default INFO level this is suppressed and the bug stays hidden; flipping the bot to DEBUG would surface it on every candle close.
- `core/regime_detector.py:131-132` — `_atr_history` and `_bb_width_history` use `list.pop(0)` for ring trim, which is O(n). Should be a `collections.deque(maxlen=…)`.
- `core/regime_detector.py:122-124` — `_hurst[key].update(close)` is called even when `close` is the same bar's update; there is no de-duplication. The `regime_detector.update` is called once per fetched candle row in `_load_history` (line 438 in market_data) and once per closed candle in `_process_candle`, so on history backfill the rolling Hurst sees the same series of closes both ways. Acceptable but worth noting.
- `core/regime_detector.py:298-331` — `_score_modifier` returns `-30.0` for CHOPPY, but `BTCGuard.apply` returns `-settings.BTC_GUARD_SCORE_PENALTY`, etc. — all penalty magnitudes are inconsistent across the codebase (some hardcoded constants here, some settings constants in guards). Should be unified through `settings`.
- `core/__init__.py` — Empty (just `# core`), so consumers must always import the submodule directly (e.g. `from core.bot import CryptoBot`). No re-exports of the three singletons (`agent`, `guard_runner`, `regime_detector`). Not necessarily a bug but increases coupling to internal module paths.
