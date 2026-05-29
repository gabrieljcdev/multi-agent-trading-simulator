# Settings audit

## Conventions

`config/settings.py` uses a single-line comment convention to mark each tunable with its sweep range: `CONSTANT = <value>   # test: <lo>-<hi>` (or `# test range: <lo>-<hi>` for a handful of older entries). Both forms are recognised; the convention is documented in `CLAUDE.md` as a hard rule for every new setting.

**Coverage:** 232 of 446 module-level constants (52.0%) carry a `# test:` annotation. The unannotated remainder are mostly enums, paths, capital aliases, watch-list literals, base URLs, weight dicts (with sum-to-1.0 invariants), and the few legacy `*_SKIP_REASON` string constants.

## By domain

### Paths

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `BASE_DIR` | `Path(__file__).parent.parent` | — | Path | **UNUSED** |
| `DATA_DIR` | `BASE_DIR / "data"` | — | Path | **UNUSED** |
| `LOGS_DIR` | `BASE_DIR / "logs"` | — | Path | logger.py |
| `DB_PATH` | `DATA_DIR / "cryptobot.db"` | — | Path | database/, scripts/, tests/ (9 files) |

### Core mode

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SIM_MODE` | `True` | — | bool | <root>, agents/, core/, execution/, tests/, ui/ (19 files) |
| `ACTIVE_PROFILE` | `"balanced"` | — | str | <root>, agents/, core/, execution/ (6 files) |
| `ACTIVE_STRATEGY` | `"default"` | — | str | bot.py, custom.py, main.py, router.py |
| `APPROVAL_MODE` | `"autonomous"` | — | str | core/, tests/, ui/ (5 files) |

### Approval modes (per_trade / window / autonomous / session floor)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `PER_TRADE_SHOW_FULL_REASONING` | `True` | — | bool | **UNUSED** |
| `PER_TRADE_AUTO_EXPIRE_SECONDS` | `600` | — | int | **UNUSED** |
| `WINDOW_BRIEF_INCLUDES` | `[ 8 items ]` | — | list | **UNUSED** |
| `WINDOW_DEFAULT_DURATION_MINUTES` | `120` | — | int | prompts.py, test_prompts.py |
| `WINDOW_MIN_DURATION_MINUTES` | `15` | — | int | bot.py |
| `WINDOW_MAX_DURATION_MINUTES` | `480` | — | int | bot.py |
| `WINDOW_AUTO_RENEW` | `False` | — | bool | **UNUSED** |
| `WINDOW_PAUSE_ON_CIRCUIT_BREAKER` | `True` | — | bool | **UNUSED** |
| `WINDOW_MAX_TRADES_PER_HOUR` | `6` | — | int | bot.py |
| `WINDOW_REAPPROVE_ON_REGIME_SHIFT` | `True` | — | bool | **UNUSED** |
| `AUTO_MAX_TRADES_PER_HOUR` | `4` | — | int | bot.py, test_bot.py |
| `AUTO_MAX_TRADES_PER_DAY` | `20` | — | int | bot.py |
| `AUTO_NOTIFY_ON_ENTRY` | `True` | — | bool | **UNUSED** |
| `AUTO_NOTIFY_ON_EXIT` | `True` | — | bool | **UNUSED** |
| `AUTO_PAUSE_ON_LOSS_STREAK` | `3` | — | int | **UNUSED** |
| `AUTO_SUMMARY_INTERVAL_MIN` | `60` | — | int | **UNUSED** |
| `SESSION_MIN_SENTIMENT_SCORE` | `40` | — | int | **UNUSED** |
| `SESSION_BLOCK_CHOPPY_REGIME` | `True` | — | bool | **UNUSED** |
| `SESSION_BLOCK_EXTREME_FEAR` | `True` | — | bool | **UNUSED** |
| `SESSION_BLOCK_EXTREME_GREED` | `False` | — | bool | **UNUSED** |
| `SESSION_REQUIRE_CLEAR_NEWS` | `True` | — | bool | **UNUSED** |
| `SESSION_MIN_ACTIVE_PAIRS` | `2` | — | int | **UNUSED** |

### Capital — per-venue ledger

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `EXCHANGE_BALANCES` | `{ 9 keys }` | — | dict | agents/, core/, execution/, tests/, ui/ (9 files) |

### Exchanges

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `ENABLED_EXCHANGES` | `["binance", "kraken", "bybit", "kucoin", "mexc"]` | — | list | agents/, core/, signals/, tests/, ui/ (8 files) |
| `MIN_LIQUIDITY_USD` | `50_000` | — | int | **UNUSED** |
| `ORDER_BOOK_DEPTH` | `10` | — | int | market_data.py |
| `ORDER_BOOK_STREAM_PAIRS` | `20` | 5-50   (top-N active pairs to stream books for) | int | market_data.py |
| `ORDER_BOOK_WATCH_TIMEOUT_S` | `30.0` | 10-60  (max wait for one symbol's book update) | float | market_data.py |
| `ORDER_BOOK_ERROR_BACKOFF_S` | `1.0` | 0.5-5  (backoff after a book-stream error — prevents busy-spin) | float | market_data.py |
| `ORDER_BOOK_MAX_STREAMS_PER_CONN` | `24` | 12-30  (per-ws-connection subscription cap; MEXC silently drops subs above ~30, so book streams shard across this many symbols per connection) | int | market_data.py, test_market_data_stream.py |

### Pair universe

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `PAIR_UNIVERSE` | `"auto"` | — | str | market_data.py |
| `PAIR_UNIVERSE_TOP_N` | `50` | — | int | market_data.py |
| `PAIR_MIN_MARKET_CAP` | `"large"` | — | str | **UNUSED** |
| `FALLBACK_PAIRS` | `[ 16 items ]` | — | list | market_data.py |

### Timeframes

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `TIMEFRAMES` | `["5m", "15m", "1h"]` | — | list | market_data.py |
| `FAST_TIMEFRAME` | `"5m"` | — | str | market_data.py, momentum.py, reversion.py |
| `MID_TIMEFRAME` | `"15m"` | — | str | momentum.py, reversion.py |
| `SLOW_TIMEFRAME` | `"1h"` | — | str | core/, signals/, ui/ (5 files) |
| `CANDLE_LOOKBACK` | `{"5m": 200, "15m": 200, "1h": 200}` | — | dict | market_data.py |

### Signal quality gate

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SIGNAL_SCORE_THRESHOLD` | `65` | 50–80 | int | quality_gate.py |
| `MAX_ACTIVE_SIGNALS` | `3` | 1–5 | int | engine.py, quality_gate.py |
| `SIGNAL_EXPIRY_MINUTES` | `10` | 5–20 | int | momentum.py, quality_gate.py, reversion.py |
| `REQUIRE_MULTI_TF_CONFIRM` | `True` | — | bool | quality_gate.py |
| `MIN_TF_CONFIRMATIONS` | `2` | — | int | momentum.py, quality_gate.py, reversion.py |

### Regime detection (ADX / Hurst / ATR / BB)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `ADX_TRENDING_MIN` | `25` | 20–30 | int | momentum.py, regime_detector.py |
| `ADX_STRONG_TREND` | `40` | 35–45 | int | momentum.py, regime_detector.py |
| `ADX_RANGING_MAX` | `20` | 18–25 | int | regime_detector.py |
| `ADX_CHOPPY_MAX` | `15` | 12–18 | int | regime_detector.py |
| `ADX_GRID_MIN` | `15` | — | int | regime_detector.py |
| `ADX_GRID_MAX` | `25` | — | int | regime_detector.py |
| `ADX_GRID_RESET` | `30` | — | int | **UNUSED** |
| `HURST_TRENDING_MIN` | `0.55` | 0.52–0.62 | float | hurst.py, regime_detector.py |
| `HURST_REVERTING_MAX` | `0.48` | 0.42–0.50 | float | hurst.py, regime_detector.py |
| `HURST_LOOKBACK_BARS` | `200` | 100–300 | int | regime_detector.py |
| `HURST_RANDOM_ZONE` | `0.04` | — | float | **UNUSED** |
| `ATR_HIGH_VOL_PERCENTILE` | `90` | 80–95 | int | regime_detector.py |
| `ATR_LOW_VOL_PERCENTILE` | `20` | — | int | **UNUSED** |
| `ATR_PERCENTILE_LOOKBACK` | `100` | — | int | regime_detector.py |
| `BB_WIDTH_EXPANDING_FACTOR` | `1.3` | 1.2–1.5 | float | **UNUSED** |

### Order-flow imbalance (OFI)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `OFI_LEVELS` | `5` | — | int | ofi.py |
| `OFI_EMA_PERIOD` | `20` | — | int | ofi.py |
| `OFI_BULLISH_THRESHOLD` | `0.65` | 0.60–0.72 | float | ofi.py |
| `OFI_BEARISH_THRESHOLD` | `0.35` | 0.28–0.40 | float | ofi.py |
| `OFI_BOOST_AMOUNT` | `10` | — | int | ofi.py |
| `OFI_PENALTY_AMOUNT` | `15` | — | int | ofi.py |
| `VPIN_HIGH_THRESHOLD` | `0.75` | 0.65–0.85 | float | ofi.py |

### Technical indicators

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `RSI_PERIOD` | `14` | 9–21 | int | market_data.py |
| `RSI_MOMENTUM_MIN` | `40` | 35–50 | int | momentum.py |
| `RSI_MOMENTUM_MAX` | `70` | 65–75 | int | momentum.py |
| `RSI_OVERBOUGHT` | `70` | — | int | reversion.py |
| `RSI_OVERSOLD` | `30` | — | int | reversion.py |
| `MACD_FAST` | `12` | — | int | market_data.py |
| `MACD_SLOW` | `26` | — | int | market_data.py |
| `MACD_SIGNAL` | `9` | — | int | market_data.py |
| `BB_PERIOD` | `20` | — | int | market_data.py |
| `BB_STDDEV` | `2.0` | 1.8–2.5 | float | market_data.py |
| `BB_REVERSION_ENTRY` | `2.0` | — | float | **UNUSED** |
| `EMA_FAST` | `9` | — | int | market_data.py |
| `EMA_SLOW` | `21` | — | int | market_data.py |
| `EMA_TREND` | `50` | — | int | market_data.py |
| `VOLUME_SURGE_MULTIPLIER` | `2.0` | 1.5–3.0 | float | **UNUSED** |
| `VWAP_STRETCH_PCT` | `0.8` | 0.5–1.2 | float | reversion.py |

### Arbitrage (signal-track + dedicated arb engine + funding-rate arb)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `ARB_MIN_GAP_PCT` | `0.03` | 0.02–0.10   (bitget special-case) | float | arb_engine.py, arbitrage.py, test_arb_engine.py, web_server.py |
| `ARB_MIN_GAP_PCT_FALLBACK` | `0.35` | 0.25–0.50   (non-bitget — also signal track) | float | arb_engine.py, arbitrage.py, test_arb_engine.py, test_signal_arbitrage.py |
| `ARB_FEE_ESTIMATE_PCT` | `0.20` | — | float | arbitrage.py |
| `ARB_MAX_TRANSFER_SECONDS` | `60` | — | int | **UNUSED** |
| `ARB_MIN_LIQUIDITY_MULT` | `2.0` | — | float | **UNUSED** |
| `ARB_SCAN_INTERVAL_MS` | `500` | 250–2000 | int | arb_engine.py, test_signal_arbitrage.py |
| `ARB_BASE_POSITION_USD` | `60.0` | 10.0–80.0  (base for gap-proportional sizing; ~3% of FUND_ARB_CAPITAL=2000) | float | agents/, execution/, tests/ (5 files) |
| `ARB_SIZE_MULTIPLIER_CAP` | `4.0` | 2.0–6.0    (max gap/threshold scale-up) | float | arb_engine.py, test_arb_engine.py |
| `ARB_MIN_LIQUIDITY_USD` | `500.0` | 250–2000   (sum of top 3 book levels) | float | arb_engine.py |
| `ARB_MAX_CONCURRENT` | `3` | 1–5 | int | arb_engine.py, web_server.py |
| `ARB_DAILY_LOSS_HALT_PCT` | `2.0` | 1-5    (% of arb fund allocation) | float | arb_engine.py, test_arb_engine.py |
| `ARB_CONSECUTIVE_LOSS_HALT` | `5` | 3–10 | int | arb_engine.py, test_arb_engine.py |
| `ARB_BALANCE_BUFFER_PCT` | `5.0` | 2.0–10.0 | float | arb_engine.py |
| `ARB_SLIPPAGE_MIN_PCT` | `0.01` | 0.005–0.02 | float | arb_engine.py, test_arb_engine.py |
| `ARB_SLIPPAGE_MAX_PCT` | `0.25` | 0.1–0.5 | float | arb_engine.py, test_arb_engine.py |
| `ARB_FUNDING_RATE_MIN_PCT` | `0.05` | 0.03–0.10  (per 8h funding) | float | arb_engine.py |
| `ARB_FUNDING_RATE_EXIT_PCT` | `0.02` | 0.01–0.05 | float | arb_engine.py |
| `ARB_FUNDING_DAILY_LOSS_HALT_PCT` | `3.0` | 1-5    (% of arb fund allocation) | float | arb_engine.py, test_arb_engine.py |
| `ARB_FUNDING_CONSECUTIVE_LOSS_HALT` | `4` | — | int | arb_engine.py, test_arb_engine.py |

### Funding-rate arb agent (Phase 1 observation mode)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `FUNDING_OBSERVATION_MODE` | `True` | True/False  (NEVER False this phase) | bool | funding_arb_agent.py, funding_engine.py, test_funding_arb.py |
| `FUNDING_CAPITAL_USD` | `0.0` | 0-5000 | float | funding_arb_agent.py, web_server.py |
| `FUNDING_SYMBOLS` | `[ 12 items ]` | — | list | funding_engine.py, test_funding_arb.py |
| `FUNDING_SCAN_INTERVAL_SEC` | `60` | 30-300 | int | funding_arb_agent.py, test_funding_arb.py |
| `FUNDING_MIN_APR` | `0.06` | 0.03-0.30 (now \|apr\| >= floor) | float | funding_engine.py, test_funding_arb.py |
| `FUNDING_MIN_OI_MULT` | `10.0` | 5-50 | float | funding_engine.py, test_funding_arb.py |
| `FUNDING_MAX_NOTIONAL_USD` | `250.0` | 100-5000 | float | funding_arb_agent.py, funding_engine.py, models.py, test_funding_arb.py |
| `FUNDING_MAX_CONCURRENT` | `2` | 1-5 | int | funding_arb_agent.py, funding_engine.py, test_funding_arb.py |
| `FUNDING_MAX_OI_FRACTION` | `0.001` | 0.0005-0.01 | float | funding_arb_agent.py, funding_engine.py |
| `FUNDING_TARGET_LEVERAGE` | `2.0` | 1.5-3.0 | float | funding_arb_agent.py, funding_engine.py |
| `FUNDING_FLIP_EXIT_APR` | `0.0` | -0.05-0.03 | float | funding_engine.py, test_funding_arb.py |
| `FUNDING_BASIS_SIGMA_EXIT` | `1.5` | 1.0-3.0 | float | funding_engine.py |
| `FUNDING_MARGIN_ALERT_RATIO` | `1.5` | 1.2-2.0 | float | funding_engine.py |
| `FUNDING_MAX_HOLD_SEC` | `1209600` | 86400-2592000 | int | funding_arb_agent.py, funding_engine.py, test_funding_arb.py |
| `FUNDING_SIM_SLIPPAGE_PCT` | `0.0002` | 0.0001-0.001 | float | funding_arb_agent.py, funding_engine.py, test_funding_arb.py |
| `FUNDING_DAILY_LOSS_HALT_PCT` | `2.0` | 1-5    (% of FUNDING_CAPITAL_USD; 0-alloc → no-op) | float | funding_arb_agent.py, test_funding_arb.py |
| `FUNDING_CONSECUTIVE_LOSS_HALT` | `4` | 3-6 | int | funding_arb_agent.py, test_funding_arb.py |

### Arb watch pairs + fee map

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `ARB_WATCH_PAIRS` | `[ 16 items ]` | — | list | arb_engine.py, test_arb_engine.py |
| `ARB_FEE_MAP` | `{ 7 keys }` | — | dict | __init__.py, arb_engine.py, test_arb_engine.py, test_funds.py |

### Momentum signal (Track B)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `MOMENTUM_MIN_VOLUME_RATIO` | `2.0` | 1.5–3.0 | float | momentum.py |
| `MOMENTUM_REQUIRE_LARGE_CAP` | `True` | — | bool | **UNUSED** |
| `MOMENTUM_REQUIRE_HURST` | `True` | — | bool | regime_detector.py |
| `MOMENTUM_REQUIRE_OFI` | `False` | — | bool | momentum.py |
| `MOMENTUM_MIN_ADX` | `22` | 18–30 | int | regime_detector.py |
| `MOMENTUM_BREAKOUT_LOOKBACK` | `20` | 10–30 | int | momentum.py |

### Mean-reversion signal (Track C)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `REVERSION_BB_THRESHOLD` | `2.0` | 1.5–2.5 | float | **UNUSED** |
| `REVERSION_REQUIRE_DIVERGENCE` | `True` | — | bool | reversion.py |
| `REVERSION_REQUIRE_HURST` | `True` | — | bool | regime_detector.py |
| `REVERSION_MAX_ADX` | `22` | 18–28 | int | regime_detector.py |
| `REVERSION_VWAP_CONFIRM` | `True` | — | bool | reversion.py |

### Liquidity sweep (Track D)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SWEEP_MIN_WICK_PCT` | `0.5` | 0.3–0.8 | float | **UNUSED** |
| `SWEEP_OFI_FLIP_REQUIRED` | `True` | — | bool | **UNUSED** |
| `SWEEP_VOLUME_SPIKE_MULT` | `2.5` | 2.0–3.5 | float | **UNUSED** |
| `SWEEP_REVERSAL_CANDLES` | `3` | 2–5 | int | **UNUSED** |
| `SWEEP_KEY_LEVEL_LOOKBACK` | `50` | 30–100 | int | **UNUSED** |

### Dynamic grid

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `GRID_ENABLED` | `True` | — | bool | regime_detector.py |
| `GRID_SPACING_PCT` | `0.5` | 0.3–1.0 | float | **UNUSED** |
| `GRID_LEVELS_EACH_SIDE` | `5` | 3–8 | int | **UNUSED** |
| `GRID_ORDER_SIZE_PCT` | `0.5` | — | float | **UNUSED** |
| `GRID_AUTO_RESET` | `True` | — | bool | **UNUSED** |
| `GRID_RESET_COOLDOWN_MIN` | `30` | — | int | **UNUSED** |

### Sentiment (legacy weights + feeds)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SENTIMENT_ENABLED` | `True` | — | bool | **UNUSED** |
| `SENTIMENT_UPDATE_INTERVAL_SECONDS` | `300` | — | int | **UNUSED** |
| `SENTIMENT_LOOKBACK_HOURS` | `4` | — | int | **UNUSED** |
| `SENTIMENT_WEIGHTS` | `{ 5 keys }` | — | dict | **UNUSED** |
| `SENTIMENT_BOOST_THRESHOLD` | `70` | 60–80 | int | arbitrage.py, momentum.py, reversion.py |
| `SENTIMENT_BLOCK_THRESHOLD` | `30` | 20–40 | int | arbitrage.py, momentum.py, reversion.py |
| `SENTIMENT_BOOST_AMOUNT` | `15` | 5–20 | int | arbitrage.py, momentum.py, reversion.py |
| `SENTIMENT_SUPPRESS_AMOUNT` | `20` | 10–25 | int | arbitrage.py, momentum.py |
| `SENTIMENT_VELOCITY_WINDOW` | `2` | — | int | **UNUSED** |
| `SENTIMENT_VELOCITY_BOOST` | `True` | — | bool | **UNUSED** |
| `REDDIT_SUBREDDITS` | `[ 6 items ]` | — | list | reddit.py |
| `REDDIT_POST_LIMIT` | `100` | — | int | **UNUSED** |
| `TELEGRAM_CHANNELS` | `[ 3 items ]` | — | list | telegram.py |
| `NEWS_FEEDS` | `[ 3 items ]` | — | list | **UNUSED** |

### News guard

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `NEWS_GUARD_ENABLED` | `True` | — | bool | guards.py |
| `NEWS_GUARD_LOOKBACK_MINUTES` | `60` | — | int | guards.py |
| `NEWS_GUARD_BLOCK_KEYWORDS` | `[ 14 items ]` | — | list | guards.py |
| `NEWS_GUARD_WARN_KEYWORDS` | `[ 8 items ]` | — | list | guards.py |
| `NEWS_GUARD_PENALTY_BLOCK` | `999` | — | int | guards.py |
| `NEWS_GUARD_PENALTY_WARN` | `20` | — | int | guards.py |

### BTC correlation guard + skip-reason constants

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `BTC_GUARD_ENABLED` | `True` | — | bool | guards.py |
| `BTC_CRASH_PCT` | `2.0` | 1.5–3.0 | float | guards.py |
| `BTC_CRASH_WINDOW_MINUTES` | `30` | 15–60 | int | guards.py |
| `BTC_GUARD_SCORE_PENALTY` | `25` | 15–35 | int | guards.py |
| `BTC_GUARD_LOOKBACK_MINUTES` | `30` | 15-60 | int | bot.py, test_bot.py |
| `BTC_GUARD_LOOKBACK_DRIFT_MINUTES` | `2` | 0-5 (acceptable snapshot age slack) | int | bot.py, test_bot.py |
| `SENTIMENT_HARD_BLOCK_SKIP_REASON` | `"SENTIMENT_HARD_BLOCK"` | — | str | quality_gate.py, test_quality_gate.py |
| `MACRO_HARD_BLOCK_SKIP_REASON` | `"MACRO_HARD_BLOCK"` | — | str | **UNUSED** |
| `PRE_EVENT_PAUSE_SKIP_REASON` | `"PRE_EVENT_PAUSE"` | — | str | **UNUSED** |

### Position correlation guard

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `CORR_GUARD_ENABLED` | `True` | — | bool | guards.py |
| `CORR_HIGH_THRESHOLD` | `0.80` | 0.70–0.90 | float | guards.py |
| `CORR_PENALTY_AMOUNT` | `15` | 10–25 | int | guards.py |
| `CORR_BLOCK_THRESHOLD` | `0.95` | — | float | guards.py |
| `CORR_LOOKBACK_HOURS` | `24` | — | int | **UNUSED** |
| `CORR_ARB_EXEMPT` | `True` | — | bool | guards.py |

### Session timing

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SESSION_SCORING_ENABLED` | `True` | — | bool | quality_gate.py |
| `SESSION_WINDOWS` | `{ 5 keys }` | — | dict | bot.py, quality_gate.py |

### Risk management

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `MAX_POSITION_SIZE_PCT` | `0.03` | 0.01–0.05 | float | agent.py |
| `MAX_OPEN_POSITIONS` | `3` | 1–5 | int | **UNUSED** |
| `DEFAULT_STOP_LOSS_PCT` | `0.01` | 0.005–0.02 | float | agent.py |
| `DEFAULT_TAKE_PROFIT_PCT` | `0.02` | 0.01–0.04 | float | agent.py |
| `MIN_RISK_REWARD_RATIO` | `1.5` | 1.0–2.5 | float | quality_gate.py |
| `TRAILING_STOP_ENABLED` | `False` | — | bool | **UNUSED** |
| `TRAILING_STOP_PCT` | `0.008` | 0.005–0.015 | float | **UNUSED** |
| `TRAILING_STOP_ACTIVATE` | `0.01` | — | float | **UNUSED** |

### Circuit breakers

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `CIRCUIT_BREAKERS` | `{ 5 keys }` | — | dict | bot.py, dashboard.py, position_manager.py, web_server.py |
| `CIRCUIT_BREAKER_PAUSE_MINUTES` | `30` | — | int | **UNUSED** |
| `CIRCUIT_BREAKER_HALT_REQUIRES_MANUAL` | `True` | — | bool | **UNUSED** |
| `SHUTDOWN_LOG_EVENT` | `True` | — | bool | bot.py, test_bot.py |

### Shutdown

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SHUTDOWN_TIMEOUT_SEC` | `15` | 5-30 | int | main.py |

### Execution (order type / retries)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `ORDER_TYPE` | `"limit"` | — | str | **UNUSED** |
| `LIMIT_SLIPPAGE_PCT` | `0.05` | — | float | **UNUSED** |
| `ORDER_TIMEOUT_SECONDS` | `30` | — | int | **UNUSED** |
| `ORDER_RETRY_ON_FAIL` | `True` | — | bool | **UNUSED** |
| `ORDER_RETRY_COUNT` | `2` | — | int | **UNUSED** |

### Claude agent

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `CLAUDE_MODEL` | `"claude-sonnet-4-5"` | — | str | agent.py |
| `CLAUDE_MAX_TOKENS` | `1024` | — | int | agent.py |
| `CLAUDE_TEMPERATURE` | `0.2` | 0.1–0.4 | float | agent.py |
| `CLAUDE_CONTEXT` | `{ 12 keys }` | — | dict | agent.py |
| `CLAUDE_WINDOW_BRIEF` | `{ 7 keys }` | — | dict | **UNUSED** |
| `SELF_REVIEW_ENABLED` | `True` | — | bool | agent.py, bot.py |
| `SELF_REVIEW_MAX_TOKENS` | `512` | — | int | agent.py |
| `CLAUDE_MAX_DAILY_COST_USD` | `2.00` | — | float | **UNUSED** |

### Predictive engine (phase 2)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `PREDICTIVE_ENABLED` | `False` | — | bool | **UNUSED** |
| `PREDICTIVE_MIN_TRAINING_TRADES` | `50` | — | int | **UNUSED** |
| `PREDICTIVE_WIN_PROB_THRESHOLD` | `0.55` | — | float | **UNUSED** |
| `PREDICTIVE_RETRAIN_EVERY_N_DAYS` | `7` | — | int | **UNUSED** |
| `PREDICTIVE_MODEL_PATH` | `DATA_DIR / "model.joblib"` | — | Path | **UNUSED** |
| `PREDICTIVE_BOOST_STRONG` | `10` | — | int | **UNUSED** |
| `PREDICTIVE_PENALTY_WEAK` | `15` | — | int | **UNUSED** |

### Multi-agent coordinator — ring-fenced funds

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `FUND_SIGNAL_CAPITAL` | `1600.0` | 0–3000  signal agent — binance/bybit/kraken | float | agents/, execution/, tests/, ui/ (6 files) |
| `FUND_ARB_CAPITAL` | `2000.0` | 0–4000  cross-exchange arb — kraken/bybit/bitget/bitstamp/gateio/bitfinex | float | agents/, execution/, tests/, ui/ (8 files) |
| `FUND_MEXC_SCALP_CAPITAL` | `500.0` | 0–1000  scalping, MEXC only (observation until SCALP_CAPITAL>0) | float | agents/, tests/, ui/ (7 files) |
| `FUND_MEXC_ARB_CAPITAL` | `500.0` | 0–1000  MEXC-only arb (counterparty-capped; un-wired until soak data justifies a dedicated agent) | float | test_funds.py |
| `FUND_XCHAIN_CAPITAL` | `0.0` | 0       observation-only this soak — see XCHAIN_CAPITAL (line ~932) | float | test_funds.py |
| `FUND_FUNDING_CAPITAL` | `0.0` | 0       observation-only this soak — see FUNDING_CAPITAL_USD (line ~264) | float | test_funds.py |
| `FUND_DAILY_LOSS_HALT_PCT` | `10.0` | 5–20 | float | coordinator.py, test_coordinator.py, test_funds.py |
| `STARTING_CAPITAL` | `5000.0` | 200–10000   (sum(FUND_*) = 4600; +400 reserve) | float | agents/, core/, execution/, tests/ (6 files) |
| `SIGNAL_AGENT_CAPITAL` | `FUND_SIGNAL_CAPITAL` | 100–800 | float | __init__.py, test_bot.py, test_equity_reconstruction.py, test_funds.py |
| `ARB_AGENT_CAPITAL` | `FUND_ARB_CAPITAL` | 100–1000 | float | __init__.py, test_bot.py, test_funds.py |
| `ARB_CAPITAL_PER_EXCHANGE` | `100.0` | 50–200 | float | arb_engine.py, test_arb_engine.py |

### Balance agent (compounding / sizing / sim / web-UI v2)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `REBALANCE_LIVE_ENABLED` | `False` | False         (hard gate; live ccxt.withdraw) | bool | agents/, tests/, ui/ (7 files) |
| `BALANCE_STRICT_OPEN_POSITION_BLOCK` | `True` | True/False    (rail 3; floor rail 2 is always-on) | bool | balance_agent.py |
| `KELLY_FRACTION` | `0.25` | 0.10-0.50  (fraction of full Kelly; never use full) | float | growth_optimal.py, test_balance_agent.py |
| `COMPOUND_RESERVE_PCT` | `0.05` | 0.02-0.15  (uncommitted buffer held out of the pool) | float | growth_optimal.py, test_balance_agent.py, test_funds.py, web_server.py |
| `ALLOCATION_CONFIDENCE` | `0.0` | 0.0-1.0    (0 = risk-parity, 1 = growth-optimal) | float | growth_optimal.py, test_balance_agent.py |
| `FUND_CAPACITY_CEILINGS_USD` | `{}` | tune from depth data; MEXC nodes carry a hard cap | dict | growth_optimal.py, inventory_state.py, test_balance_agent.py |
| `INTERNALIZE_WINDOW_S` | `300` | 60-1800    (wait for self-correction before transferring) | int | planner.py |
| `REBALANCE_DAILY_LIMIT` | `3` | 1-10       (max physical rebalances per UTC day) | int | balance_agent.py, test_balance_agent.py, web_server.py |
| `WITHDRAWAL_ROUTES` | `{}` | seeded from ccxt; live cex rail no-ops with empty map | dict | balance_agent.py, base.py, cex_rail.py, test_balance_agent.py |
| `SIM_WITHDRAWAL_FEE_USD` | `1.0` | 0.04-1.6   (simulated per-transfer fee, route-dependent live) | float | agents/, database/, tests/ (5 files) |
| `BALANCE_STRUCTURAL_DRIFT_HINT` | `0.20` | 0.10-0.40 | float | planner.py, test_balance_agent.py |
| `SIM_TRANSFER_DELAY_S` | `600` | 60-7200    (simulated in-transit time, sim_rail) | int | balance_agent.py, sim_rail.py, test_balance_agent.py |
| `SIM_REBALANCE_FAILURE_RATE` | `0.0` | 0.0-0.10   (inject failures to exercise auto-pause) | float | sim_rail.py, test_balance_agent.py |
| `REBALANCE_CONFIRM_WINDOW_S` | `3` | 2-30       (/action/rebalance arm→confirm window) | int | balance_agent.py, test_balance_agent.py |
| `REBALANCE_ARM_TIMEOUT_S` | `10` | 5-30 | int | web_server.py |
| `BALANCE_AUTO_DISPATCH` | `False` | True/False | bool | balance_agent.py |
| `STABLECOIN_BENCHMARK_APR_PCT` | `5.0` | 3-8 | float | **UNUSED** |
| `FUNDING_DELTA_TOLERANCE_USD` | `5.0` | 1-20 | float | **UNUSED** |
| `BALANCE_AGENT_CAPITAL` | `0.0` | 0.0       (BalanceAgent is operational, not alpha) | float | balance_agent.py |
| `BALANCE_SCAN_INTERVAL_SEC` | `60` | 15-300 | int | balance_agent.py |

### Strategy → exchange routing

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `STRATEGY_EXCHANGE_MAP` | `{ 5 keys }` | — | dict | agents/, core/, execution/, tests/, ui/ (9 files) |

### Scalping agent v1 — capital, pairs, MEXC key map, OFI, execution

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SCALP_CAPITAL` | `500.0` | 0-1000  ($0 = observation; >0 = sim execution) | float | agents/, scalping_v2/, tests/ (8 files) |
| `SCALP_PAIRS` | `[ 96 items ]` | — | list | agents/, core/, tests/, ui/ (6 files) |
| `MEXC_PAIR_KEY_MAP` | `{ 96 keys }` | — | dict | mexc_key_router.py, test_mexc_key_router.py, test_scalp_pair_coverage.py |
| `SCALP_NET_PROFIT_TARGET_BPS` | `3.0` | 2.0-8.0   (net profit after fees) | float | scalping_agent.py, scalping_atr_sl.py, test_scalping_agent.py |
| `SCALP_RR_RATIO` | `1.6` | 1.3-2.5   (tp_bps / sl_bps) | float | scalping_agent.py, scalping_atr_sl.py, test_scalping_agent.py |
| `SCALP_MAX_BREAKEVEN_WIN_RATE` | `0.65` | 0.55-0.75 (block if math needs >65% wr) | float | scalping_agent.py |
| `SCALP_FEE_DEFAULT_BPS` | `10.0` | — | float | scalping_agent.py |
| `SCALP_FEE_OVERRIDES` | `{ 2 keys }` | — | dict | scalping_agent.py, test_scalping_agent.py |
| `SCALP_OFI_Z_ENTRY` | `2.0` | 1.0-2.5   (z-score entry threshold; v2 raised 1.5→2.0) | float | scalping_agent.py, settings_scalp_v2.py, test_scalp_v2_integration.py, test_scalping_agent.py |
| `SCALP_OFI_Z_EXIT` | `0.3` | 0.1-0.7   (OFI exhaustion exit) | float | queries.py, scalping_agent.py, test_scalping_agent.py |
| `SCALP_OFI_Z_CONTRADICT` | `-0.8` | -0.4 to -1.5 (OFI flip exit) | float | scalping_agent.py |
| `SCALP_OFI_PERSIST_TICKS` | `5` | 2-6       (consecutive ticks above threshold; v2 raised 3→5) | int | scalping_agent.py, settings_scalp_v2.py, test_scalp_v2_integration.py, test_scalping_agent.py |
| `SCALP_OFI_LEVELS` | `10` | 1-10      (book depth levels) | int | scalping_agent.py, test_scalping_agent.py |
| `SCALP_OFI_WINDOW_SEC` | `20` | 10-40     (bucket accumulation window seconds) | int | scalping_agent.py |
| `SCALP_ZSCORE_WINDOW` | `80` | 40-150    (rolling z-score normalisation periods) | int | scalping_agent.py |
| `SCALP_MAX_SPREAD_BPS` | `3.0` | 1.5-6.0   (max bid-ask spread bps) | float | scalping_agent.py |
| `SCALP_STALE_MID_THRESHOLD_SEC` | `60` | 30-180    (skip entry if a symbol's mid hasn't moved for >= N sec — frozen feed = stale OFI) | int | scalping_agent.py, test_scalping_agent.py |
| `SCALP_MAX_HOLD_SEC` | `180` | 60-300    (force exit after N seconds) | int | scalping_agent.py |
| `SCALP_SCAN_INTERVAL_MS` | `100` | 50-500    (main loop interval ms) | int | scalping_agent.py |
| `SCALP_MAX_CONCURRENT` | `2` | 1-3       (max open scalp positions) | int | scalping_agent.py |
| `SCALP_POSITION_SIZE_USD` | `20.0` | 10-50     (per-trade size USD; ~3% of FUND_MEXC_SCALP_CAPITAL=500 baseline) | float | queries.py, scalping_agent.py, test_web_server.py |

### Scalping agent v1 — circuit breakers, session, BTC guard, depth weights

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SCALP_DAILY_LOSS_HALT_PCT` | `3.0` | 1-5       (% of scalp fund allocation; was abs USD) | float | scalping_agent.py, test_scalping_agent.py |
| `SCALP_CONSEC_LOSS_PAUSE` | `4` | 3-6       (consecutive loss pause count) | int | scalping_agent.py |
| `SCALP_SESSION_START_UTC` | `0` | 6-13      (v2: London/NY overlap start, raised 7→12) | int | scalping_agent.py, settings_scalp_v2.py, test_scalp_v2_integration.py, test_scalping_agent.py |
| `SCALP_SESSION_END_UTC` | `24` | 14-20     (v2: overlap end, lowered 17→16) | int | scalping_agent.py, settings_scalp_v2.py, test_scalp_v2_integration.py, test_scalping_agent.py |
| `SCALP_RESPECT_NEWS_GUARD` | `True` | True/False | bool | scalping_agent.py |
| `SCALP_BTC_GUARD_PCT` | `0.3` | 0.2-0.6   (block if \|BTC 1m change\| > N%) | float | scalping_agent.py |
| `SCALP_TRACKER_INTERVAL_SEC` | `15` | 10-30 | int | scalping_agent.py |
| `SCALP_DEPTH_WEIGHTS` | `{ 10 keys }` | — | dict | scalping_agent.py, test_scalping_agent.py |

### Scalping agent v2 — selectivity gates + activation criteria

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SCALP_USE_CONFLUENCE` | `True` | — | bool | scalping_agent.py, settings_scalp_v2.py, test_scalp_v2_integration.py, test_scalping_agent.py |
| `SCALP_CONFLUENCE_REQUIRED` | `2` | 1-3 | int | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_VWAP_GATE` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_HTF_TREND_GATE` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_HTF_TIMEFRAME` | `"5m"` | — | str | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_HTF_EMA_FAST` | `8` | — | int | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_HTF_EMA_SLOW` | `21` | — | int | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_VOLUME_GATE` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_VOLUME_LOOKBACK_MIN` | `20` | 10-40 | int | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_VOLUME_THRESHOLD_RATIO` | `1.0` | 0.8-1.5  (current 1m vol ≥ ratio × median) | float | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_CROSS_EXCHANGE_OFI` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_CROSS_EXCHANGE_DISAGREE_BLOCK` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_CROSS_EXCHANGE_AGREE_Z_MIN` | `0.5` | 0.3-1.0 | float | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_BTC_DIRECTIONAL` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_BTC_OFI_NEUTRAL_BAND` | `0.5` | 0.3-0.8  (\|BTC z\| below this = neutral) | float | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_ADVERSE_SELECTION_GUARD` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_ADVERSE_MID_MOVE_BPS` | `1.0` | 0.5-2.0 | float | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_ADVERSE_MOVE_WINDOW_MS` | `100` | 50-300 | int | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_DEPTH_GATE` | `True` | — | bool | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_MIN_TOP5_DEPTH_MULTIPLIER` | `5.0` | 3-10 | float | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_MAX_TOP1_CONSUME_PCT` | `20.0` | 10-40 | float | scalping_confluence.py, settings_scalp_v2.py |
| `SCALP_USE_ATR_AWARE_SL` | `True` | — | bool | agents/, scalping_v2/, tests/ (5 files) |
| `SCALP_ATR_PERIOD` | `20` | 10-30 | int | scalping_atr_sl.py, settings_scalp_v2.py |
| `SCALP_ATR_TIMEFRAME` | `"1m"` | — | str | scalping_atr_sl.py, settings_scalp_v2.py |
| `SCALP_ATR_SL_MULTIPLIER` | `0.3` | 0.2-0.5 | float | scalping_atr_sl.py, settings_scalp_v2.py |
| `SCALP_ATR_SL_FLOOR_BPS` | `1.5` | 1.0-3.0   (never tighter than this) | float | scalping_atr_sl.py, settings_scalp_v2.py |
| `SCALP_ATR_SL_CEILING_BPS` | `8.0` | 6-12      (never wider than this) | float | scalping_atr_sl.py, settings_scalp_v2.py |
| `SCALP_MIN_OBSERVATIONS_FOR_LIVE` | `200` | — | int | queries.py |
| `SCALP_MIN_WIN_RATE_FOR_LIVE` | `0.52` | — | float | queries.py |
| `SCALP_MIN_AVG_NET_BPS_FOR_LIVE` | `0.0` | — | float | queries.py |
| `SCALP_MAX_HOLD_EXIT_PCT` | `0.30` | — | float | queries.py |
| `SCALP_MIN_DIRECTIONAL_ACC_1M` | `0.55` | — | float | queries.py |
| `SCALP_MIN_OBSERVATIONS_FOR_LIVE_V2` | `300` | — | int | queries.py, scalping_agent_v2_integration.py, settings_scalp_v2.py |
| `SCALP_MIN_WIN_RATE_FOR_LIVE_V2` | `0.55` | — | float | queries.py, scalping_agent_v2_integration.py, settings_scalp_v2.py |
| `SCALP_MIN_AVG_NET_BPS_FOR_LIVE_V2` | `0.5` | — | float | queries.py, scalping_agent_v2_integration.py, settings_scalp_v2.py |
| `SCALP_MAX_HOLD_EXIT_PCT_V2` | `0.25` | — | float | queries.py, scalping_agent_v2_integration.py, settings_scalp_v2.py |
| `SCALP_MIN_DIRECTIONAL_ACC_1M_V2` | `0.57` | — | float | queries.py, scalping_agent_v2_integration.py, settings_scalp_v2.py |

### Cross-chain arb agent (XCHAIN — observation-only)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `XCHAIN_LIVE_ENABLED` | `False` | False         (hard gate; keep False) | bool | base_connector.py, crosschain_agent.py, crosschain_engine.py, models.py |
| `XCHAIN_CAPITAL` | `0.0` | 0, 100, 250   (0 = observation mode) | float | agents/, database/, execution/, tests/, ui/ (7 files) |
| `XCHAIN_MIN_NET_EDGE_BPS` | `15.0` | 8, 12, 15, 20, 30 | float | crosschain_engine.py, test_crosschain_engine.py |
| `XCHAIN_GAS_BUDGET_BPS` | `5.0` | 3, 5, 8       (gas-as-%-of-notional cap) | float | crosschain_engine.py, test_crosschain_engine.py |
| `XCHAIN_SLIPPAGE_TOLERANCE_BPS` | `10.0` | 5, 10, 20     (per-leg price impact tolerance) | float | crosschain_engine.py, test_crosschain_engine.py |
| `XCHAIN_MAX_POSITION_USD` | `50.0` | 25, 50, 100, 250 | float | base_connector.py, crosschain_engine.py, test_crosschain_engine.py |
| `XCHAIN_INVENTORY_DRIFT_PCT` | `0.20` | 0.10, 0.20, 0.30  (theta; rebalance trigger) | float | inventory.py, test_crosschain_agent.py, test_inventory_targets.py |
| `XCHAIN_SCAN_INTERVAL_MS` | `2000` | 1000, 2000, 5000 | int | crosschain_engine.py |
| `XCHAIN_DAILY_LOSS_HALT_PCT` | `2.0` | 1-5    (% of XCHAIN_CAPITAL; 0-alloc → no-op) | float | crosschain_engine.py, test_crosschain_engine.py |
| `XCHAIN_CONSECUTIVE_LOSS_HALT` | `5` | 3, 5 | int | crosschain_engine.py, test_crosschain_engine.py |
| `XCHAIN_MAX_CONCURRENT` | `1` | 1, 2          (per-symbol scan concurrency cap) | int | crosschain_engine.py |
| `XCHAIN_MAX_BLOCK_STALENESS` | `5` | 2, 5, 10 | int | crosschain_engine.py |
| `XCHAIN_SYMBOLS` | `["WETH-USDC"]` | keep single pair first | list | _solidly_volatile.py, arbitrum.py, crosschain_engine.py, inventory.py |
| `XCHAIN_CHAINS` | `["arbitrum", "base", "optimism"]` | — | list | __init__.py, inventory.py, test_crosschain_agent.py |
| `XCHAIN_VENUES` | `{ 5 keys }` | — | dict | execution/, tests/ (5 files) |
| `XCHAIN_RPC_ENV_VARS` | `{ 3 keys }` | — | dict | **UNUSED** |

### Portfolio CBs + kill switch

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `PORTFOLIO_DAILY_LOSS_HALT_PCT` | `3.0` | 2.0–5.0 | float | coordinator.py, test_coordinator.py |
| `PORTFOLIO_MAX_EXPOSURE_PCT` | `80.0` | 60–95 | float | coordinator.py |
| `PORTFOLIO_MONITOR_INTERVAL_SEC` | `30` | 15–120 | int | coordinator.py, models.py |
| `KILL_SWITCH_CONFIRM_REQUIRED` | `False` | — | bool | **UNUSED** |

### Pluggable sentiment aggregator

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `SENTIMENT_COMPOSITE_FLOOR` | `-60` | -50 to -70   (session-block threshold) | int | aggregator.py, test_sentiment.py |
| `SENTIMENT_FEAR_GREED_FLOOR` | `15` | 10–25        (extreme-fear cutoff) | int | aggregator.py |
| `SENTIMENT_BTC_GUARD_PCT` | `-2.0` | -1.5 to -3.0 (30m BTC drop trigger) | float | aggregator.py, test_sentiment.py |
| `SENTIMENT_BTC_GUARD_PENALTY` | `-15` | -10 to -25   (penalty on alt signals) | int | aggregator.py, test_sentiment.py |
| `SENTIMENT_NEWS_GUARD_ENABLED` | `True` | — | bool | **UNUSED** |
| `SENTIMENT_REFRESH_INTERVAL_SEC` | `300` | 60–900       (min between get_current() refreshes) | int | aggregator.py |
| `SENTIMENT_HTTP_TIMEOUT_SEC` | `10` | 5–30 | int | cryptopanic.py, fear_greed.py |
| `SENTIMENT_WEIGHT_FEAR_GREED` | `0.4` | — | float | fear_greed.py |
| `SENTIMENT_WEIGHT_CRYPTOPANIC` | `0.25` | — | float | cryptopanic.py |
| `SENTIMENT_WEIGHT_REDDIT` | `0.2` | — | float | reddit.py |
| `SENTIMENT_WEIGHT_GOOGLE_TRENDS` | `0.1` | — | float | google_trends.py |
| `SENTIMENT_WEIGHT_TELEGRAM` | `0.05` | — | float | telegram.py |

### Pluggable data sources (refresh intervals + watch lists + base URLs)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `COINGLASS_REFRESH_SEC` | `300` | 60-600 | int | coinglass.py |
| `COINGLASS_REFERENCE_EXCHANGE` | `"Binance"` | Binance \| Bybit \| OKX | str | **UNUSED** |
| `COINGLASS_RATE_LIMIT_PER_MIN` | `6` | 3-12 | int | coinglass.py, test_data_sources.py |
| `COINGECKO_REFRESH_SEC` | `300` | 60-900 | int | coingecko.py |
| `FRED_REFRESH_SEC` | `3600` | 1800-7200 | int | fred.py |
| `ALPHA_VANTAGE_REFRESH_SEC` | `900` | 300-1800 | int | alpha_vantage.py |
| `FRANKFURTER_REFRESH_SEC` | `3600` | 1800-7200 | int | frankfurter.py |
| `COINGECKO_USE_PRO` | `False` | True/False | bool | coingecko.py, test_data_sources.py |
| `DATA_SOURCES_REFRESH_LOOP_SEC` | `60` | 30-300  (top-level refresh_all tick) | int | bot.py |
| `DATA_SOURCES_HTTP_TIMEOUT_SEC` | `10` | 5-30 | int | data_sources/, macro/ (14 files) |
| `COINGLASS_WATCH_PAIRS` | `[ 16 items ]` | — | list | coinglass.py |
| `ALPHA_VANTAGE_SYMBOLS` | `[ 4 items ]` | — | list | alpha_vantage.py, test_data_sources.py |
| `FRED_SERIES` | `[ 9 items ]` | — | list | fred.py |
| `FRANKFURTER_PAIRS` | `[ 8 items ]` | — | list | frankfurter.py |

### Data-source risk regime thresholds (VIX / DXY)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `DATA_VIX_RISK_ON_MAX` | `15` | 12-18 | int | fred.py |
| `DATA_VIX_RISK_OFF_MIN` | `25` | 22-30 | int | fred.py |
| `DATA_VIX_CRISIS_MIN` | `35` | 30-40 | int | bot.py, fred.py |
| `DATA_DXY_STRONG_THRESHOLD` | `105` | 102-108 | int | frankfurter.py |
| `DATA_DXY_WEAK_THRESHOLD` | `95` | 92-98 | int | frankfurter.py |

### Alpha Vantage rate limiting

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `ALPHA_VANTAGE_DAILY_CALL_BUDGET` | `20` | 10-25 | int | alpha_vantage.py |
| `ALPHA_VANTAGE_PACE_SEC` | `1.3` | 1.1-3.0  (sleep between calls) | float | alpha_vantage.py, test_data_sources.py |

### Data-source base URLs

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `FRANKFURTER_BASE_URL` | `"https://api.frankfurter.dev/v1"` | — | str | frankfurter.py |
| `WORLD_BANK_BASE_URL` | `"https://api.worldbank.org/v2"` | — | str | world_bank.py |
| `IMF_DATAMAPPER_URL` | `"https://www.imf.org/external/datamapper/api/v1"` | — | str | imf.py |
| `ECB_BASE_URL` | `"https://data-api.ecb.europa.eu/service/data"` | — | str | ecb.py |
| `US_TREASURY_BASE_URL` | `"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/d…` | — | str | us_treasury.py |
| `CFTC_BASE_URL` | `"https://publicreporting.cftc.gov/resource/jun7-fc8e.json"` | — | str | cftc_cot.py |
| `BINANCE_FUTURES_BASE_URL` | `"https://fapi.binance.com"` | — | str | binance_futures.py |
| `BYBIT_BASE_URL` | `"https://api.bybit.com"` | — | str | bybit_derivs.py |
| `CRYPTOCOMPARE_BASE_URL` | `"https://min-api.cryptocompare.com/data"` | — | str | cryptocompare.py |

### Data-source new-source watch lists + refresh intervals

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `CRYPTOCOMPARE_REFRESH_SEC` | `300` | 60-900 | int | cryptocompare.py |
| `CRYPTOCOMPARE_FSYMS` | `["BTC", "ETH", "SOL", "BNB", "XRP"]` | — | list | cryptocompare.py |
| `CRYPTOCOMPARE_TSYM` | `"USD"` | — | str | cryptocompare.py |
| `BINANCE_FUTURES_REFRESH_SEC` | `60` | 15-300 | int | binance_futures.py |
| `BINANCE_FUTURES_SYMBOLS` | `["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]` | — | list | binance_futures.py |
| `BYBIT_REFRESH_SEC` | `60` | 15-300 | int | bybit_derivs.py |
| `BYBIT_SYMBOLS` | `["BTCUSDT", "ETHUSDT", "SOLUSDT"]` | — | list | bybit_derivs.py |
| `CFTC_REFRESH_SEC` | `3600 * 6` | — | ? | cftc_cot.py |
| `CFTC_CONTRACT` | `"BITCOIN - CHICAGO MERCANTILE EXCHANGE"` | — | str | cftc_cot.py |
| `WORLD_BANK_REFRESH_SEC` | `86400` | — | int | world_bank.py |
| `WORLD_BANK_COUNTRIES` | `["USA", "EUU", "CHN", "JPN", "GBR"]` | — | list | world_bank.py |
| `WORLD_BANK_INDICATORS` | `{ 3 keys }` | — | dict | world_bank.py |
| `IMF_REFRESH_SEC` | `86400` | — | int | imf.py |
| `IMF_COUNTRIES` | `["USA", "EUR", "CHN", "JPN", "GBR"]` | — | list | imf.py |
| `IMF_INDICATORS` | `{ 3 keys }` | — | dict | imf.py |
| `ECB_REFRESH_SEC` | `3600` | — | int | ecb.py |
| `ECB_SERIES` | `[ 3 items ]` | — | list | ecb.py |
| `US_TREASURY_REFRESH_SEC` | `3600` | — | int | us_treasury.py |
| `BTC_FUTURES_FSYM` | `"BTC"` | — | str | **UNUSED** |

### Macro modifiers (legacy direct from data_sources)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `MACRO_RISK_OFF_PENALTY` | `-5` | -10 to -2 | int | **UNUSED** |
| `MACRO_CRISIS_PENALTY` | `-20` | -25 to -15 | int | **UNUSED** |
| `MACRO_DXY_STRONG_LONG_PENALTY` | `-5` | -10 to -2 | int | **UNUSED** |
| `MACRO_YIELD_INVERTED_PENALTY` | `-3` | -6 to -1 | int | **UNUSED** |

### Macro regime monitor (macro/)

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `MACRO_REFRESH_INTERVAL_SEC` | `300` | 60-600 | int | monitor.py |
| `MACRO_PRE_EVENT_PAUSE_MINUTES` | `30` | 15-60 | int | bot.py |
| `MACRO_CONFIDENCE_STALE_HOURS` | `4` | 1-12 | int | monitor.py |
| `MACRO_RATE_LOOKBACK_DAYS` | `90` | 30-180 (rate-env change window) | int | monitor.py |
| `MACRO_DXY_STRONG` | `104.0` | 102-106 | float | monitor.py |
| `MACRO_DXY_WEAK` | `99.0` | 97-101 | float | monitor.py |
| `MACRO_VIX_CALM` | `15.0` | 12-18 | float | dashboard.py, monitor.py |
| `MACRO_VIX_ELEVATED` | `25.0` | 20-30 | float | dashboard.py, monitor.py |
| `MACRO_VIX_CRISIS` | `35.0` | 30-40 | float | dashboard.py, monitor.py, test_macro.py |
| `MACRO_YIELD_CURVE_INVERSION` | `0.0` | -0.5 to 0.5  (10y - 2y) | float | monitor.py |
| `MACRO_RATE_CHANGE_TIGHTENING` | `0.25` | 0.1-0.5      (fed funds Δ%) | float | monitor.py |
| `MACRO_RATE_CHANGE_EASING` | `-0.25` | -0.5 to -0.1 | float | monitor.py |
| `MACRO_INFLATION_LOW` | `2.5` | 1.5-3.5 | float | monitor.py |
| `MACRO_INFLATION_HIGH` | `4.0` | 3.0-6.0 | float | monitor.py |
| `MACRO_WEIGHT_VIX` | `0.30` | 0.2-0.4 | float | monitor.py |
| `MACRO_WEIGHT_DOLLAR` | `0.25` | 0.15-0.35 | float | monitor.py |
| `MACRO_WEIGHT_YIELD_CURVE` | `0.20` | 0.1-0.3 | float | monitor.py |
| `MACRO_WEIGHT_RATE_ENV` | `0.15` | 0.1-0.2 | float | monitor.py |
| `MACRO_WEIGHT_INFLATION` | `0.10` | 0.05-0.15 | float | monitor.py |

### Bot loop timing

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `BOT_LOOP_INTERVAL_SEC` | `30` | 10–120 | int | bot.py |
| `HEARTBEAT_INTERVAL_SEC` | `300` | 60–600 | int | bot.py |
| `POSITION_WATCHER_INTERVAL_SEC` | `5` | 2–15 | int | bot.py |
| `FUTURE_PRICE_TRACKER_INTERVAL_SEC` | `3600` | 1800–7200 | int | bot.py |
| `SELF_REVIEW_EVERY_N_TRADES` | `5` | 3–10 | int | bot.py |

### Logging

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `LOG_LEVEL` | `"INFO"` | — | str | logger.py |
| `LOG_TO_FILE` | `True` | — | bool | logger.py |
| `LOG_ROTATION` | `"midnight"` | — | str | **UNUSED** |

### UI / web control panel

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `UI_REFRESH_RATE` | `1.0` | — | float | **UNUSED** |
| `UI_MAX_TRADE_LOG_ROWS` | `20` | — | int | **UNUSED** |
| `UI_SHOW_CLAUDE_REASONING` | `True` | — | bool | **UNUSED** |
| `UI_SHOW_REGIME_DETAILS` | `True` | — | bool | **UNUSED** |
| `UI_SHOW_OFI_BARS` | `True` | — | bool | **UNUSED** |
| `UI_SHOW_HURST_BARS` | `True` | — | bool | **UNUSED** |
| `UI_DEFAULT_TAB` | `"overview"` | — | str | **UNUSED** |
| `WEB_UI_HOST` | `"localhost"` | "0.0.0.0" for LAN access | str | web_server.py |
| `WEB_UI_PORT` | `8765` | any open port | int | test_web_server.py, web_server.py |
| `WEB_UI_ENABLED` | `False` | — | bool | main.py |
| `WEB_UI_PUSH_INTERVAL_S` | `0.5` | 0.25-2.0  (WebSocket push rate, seconds) | float | queries.py, test_web_server.py, web_server.py |
| `WEB_UI_SCALP_FEED_HISTORY` | `30` | 10, 20, 30, 50 | int | test_web_server.py, web_server.py |

### Dashboard colour ladders

| Name | Current Value | Test Range | Type | Used by |
|------|---------------|------------|------|---------|
| `DASHBOARD_EXEC_RATE_GREEN_PCT` | `50.0` | 30–70 | float | dashboard.py |
| `DASHBOARD_EXEC_RATE_AMBER_PCT` | `20.0` | 10–40 | float | dashboard.py |
| `DASHBOARD_BALANCE_MISS_AMBER` | `1` | 1–3 | int | dashboard.py, test_dashboard.py |
| `DASHBOARD_BALANCE_MISS_RED` | `6` | 3–10 | int | dashboard.py, test_dashboard.py |

## Dead config

90 module-level constants in `config/settings.py` are not referenced anywhere outside the file (greped across all `.py`, excluding `venv/.git/audit/logs/backups/data/htmlcov/__pycache__`):

- `BASE_DIR` (line 23)
- `DATA_DIR` (line 24)
- `PER_TRADE_SHOW_FULL_REASONING` (line 45)
- `PER_TRADE_AUTO_EXPIRE_SECONDS` (line 46)
- `WINDOW_BRIEF_INCLUDES` (line 50)
- `WINDOW_AUTO_RENEW` (line 58)
- `WINDOW_PAUSE_ON_CIRCUIT_BREAKER` (line 59)
- `WINDOW_REAPPROVE_ON_REGIME_SHIFT` (line 61)
- `AUTO_NOTIFY_ON_ENTRY` (line 67)
- `AUTO_NOTIFY_ON_EXIT` (line 68)
- `AUTO_PAUSE_ON_LOSS_STREAK` (line 69)
- `AUTO_SUMMARY_INTERVAL_MIN` (line 70)
- `SESSION_MIN_SENTIMENT_SCORE` (line 75)
- `SESSION_BLOCK_CHOPPY_REGIME` (line 76)
- `SESSION_BLOCK_EXTREME_FEAR` (line 77)
- `SESSION_BLOCK_EXTREME_GREED` (line 78)
- `SESSION_REQUIRE_CLEAR_NEWS` (line 79)
- `SESSION_MIN_ACTIVE_PAIRS` (line 80)
- `MIN_LIQUIDITY_USD` (line 119)
- `PAIR_MIN_MARKET_CAP` (line 132)
- `ADX_GRID_RESET` (line 171)
- `HURST_RANDOM_ZONE` (line 176)
- `ATR_LOW_VOL_PERCENTILE` (line 179)
- `BB_WIDTH_EXPANDING_FACTOR` (line 182)
- `BB_REVERSION_ENTRY` (line 212)
- `VOLUME_SURGE_MULTIPLIER` (line 218)
- `ARB_MAX_TRANSFER_SECONDS` (line 231)
- `ARB_MIN_LIQUIDITY_MULT` (line 232)
- `MOMENTUM_REQUIRE_LARGE_CAP` (line 330)
- `REVERSION_BB_THRESHOLD` (line 340)
- `SWEEP_MIN_WICK_PCT` (line 350)
- `SWEEP_OFI_FLIP_REQUIRED` (line 351)
- `SWEEP_VOLUME_SPIKE_MULT` (line 352)
- `SWEEP_REVERSAL_CANDLES` (line 353)
- `SWEEP_KEY_LEVEL_LOOKBACK` (line 354)
- `GRID_SPACING_PCT` (line 361)
- `GRID_LEVELS_EACH_SIDE` (line 362)
- `GRID_ORDER_SIZE_PCT` (line 363)
- `GRID_AUTO_RESET` (line 364)
- `GRID_RESET_COOLDOWN_MIN` (line 365)
- `SENTIMENT_ENABLED` (line 371)
- `SENTIMENT_UPDATE_INTERVAL_SECONDS` (line 372)
- `SENTIMENT_LOOKBACK_HOURS` (line 373)
- `SENTIMENT_WEIGHTS` (line 375)
- `SENTIMENT_VELOCITY_WINDOW` (line 387)
- `SENTIMENT_VELOCITY_BOOST` (line 388)
- `REDDIT_POST_LIMIT` (line 394)
- `NEWS_FEEDS` (line 400)
- `MACRO_HARD_BLOCK_SKIP_REASON` (line 441)
- `PRE_EVENT_PAUSE_SKIP_REASON` (line 442)
- `CORR_LOOKBACK_HOURS` (line 452)
- `MAX_OPEN_POSITIONS` (line 475)
- `TRAILING_STOP_ENABLED` (line 480)
- `TRAILING_STOP_PCT` (line 481)
- `TRAILING_STOP_ACTIVATE` (line 482)
- `CIRCUIT_BREAKER_PAUSE_MINUTES` (line 518)
- `CIRCUIT_BREAKER_HALT_REQUIRES_MANUAL` (line 519)
- `ORDER_TYPE` (line 534)
- `LIMIT_SLIPPAGE_PCT` (line 535)
- `ORDER_TIMEOUT_SECONDS` (line 536)
- `ORDER_RETRY_ON_FAIL` (line 537)
- `ORDER_RETRY_COUNT` (line 538)
- `CLAUDE_WINDOW_BRIEF` (line 563)
- `CLAUDE_MAX_DAILY_COST_USD` (line 576)
- `PREDICTIVE_ENABLED` (line 582)
- `PREDICTIVE_MIN_TRAINING_TRADES` (line 583)
- `PREDICTIVE_WIN_PROB_THRESHOLD` (line 584)
- `PREDICTIVE_RETRAIN_EVERY_N_DAYS` (line 585)
- `PREDICTIVE_MODEL_PATH` (line 586)
- `PREDICTIVE_BOOST_STRONG` (line 587)
- `PREDICTIVE_PENALTY_WEAK` (line 588)
- `STABLECOIN_BENCHMARK_APR_PCT` (line 703)
- `FUNDING_DELTA_TOLERANCE_USD` (line 704)
- `XCHAIN_RPC_ENV_VARS` (line 1006)
- `KILL_SWITCH_CONFIRM_REQUIRED` (line 1021)
- `SENTIMENT_NEWS_GUARD_ENABLED` (line 1033)
- `COINGLASS_REFERENCE_EXCHANGE` (line 1056)
- `BTC_FUTURES_FSYM` (line 1172)
- `MACRO_RISK_OFF_PENALTY` (line 1177)
- `MACRO_CRISIS_PENALTY` (line 1178)
- `MACRO_DXY_STRONG_LONG_PENALTY` (line 1179)
- `MACRO_YIELD_INVERTED_PENALTY` (line 1180)
- `LOG_ROTATION` (line 1234)
- `UI_REFRESH_RATE` (line 1236)
- `UI_MAX_TRADE_LOG_ROWS` (line 1237)
- `UI_SHOW_CLAUDE_REASONING` (line 1238)
- `UI_SHOW_REGIME_DETAILS` (line 1239)
- `UI_SHOW_OFI_BARS` (line 1240)
- `UI_SHOW_HURST_BARS` (line 1241)
- `UI_DEFAULT_TAB` (line 1242)

## Suspected hardcoded numbers in source

Numbers that look like thresholds, multipliers, or fallbacks but bypass `settings.py`. Per CLAUDE.md, every threshold should live in `settings.py` with a `# test:` annotation:

- `execution/arb_engine.py:113` — `fee_buy = fee_map.get(buy_ex, 0.002)`  (fallback fee (20 bps) not in settings; shadows ARB_FEE_ESTIMATE_PCT)
- `execution/arb_engine.py:114` — `fee_sell = fee_map.get(sell_ex, 0.002)`  (fallback fee (20 bps) not in settings)
- `execution/arb_engine.py:441` — `min(ask_liq, bid_liq) * 0.10`  (10% book-consume cap — magic; should mirror SCALP_MAX_TOP1_CONSUME_PCT pattern)
- `execution/arb_engine.py:517` — `fee_buy  = settings.ARB_FEE_MAP.get(opp.buy_exchange,  0.002)`  (fallback fee (20 bps) duplicated three times in the file)
- `execution/arb_engine.py:518` — `fee_sell = settings.ARB_FEE_MAP.get(opp.sell_exchange, 0.002)`  (fallback fee (20 bps))
- `execution/arb_engine.py:566` — `factor = min(factor, 10.0)`  (10× position-scaling cap — runaway-guard magic; no setting)
- `core/bot.py:741` — `if row.price_1h is None and age_min >= 60:`  (60/240/1440-min landmark thresholds for future-price tracker; should be a settings list)
- `core/bot.py:743` — `if row.price_4h is None and age_min >= 240:`  (sibling of line 741)
- `core/bot.py:745` — `if row.price_24h is None and age_min >= 1440:`  (sibling of line 741)
- `core/bot.py:853` — `composite_0_100 = 50.0 + (data.composite_score / 2.0)`  (−100..+100 → 0..100 rescale magic; should be a helper constant pair)
- `agents/scalping_agent.py:316` — `return float(fees["taker_bps"]) * 2.0`  (×2 to convert per-leg taker bps → round-trip; no setting)
- `agents/scalping_agent.py:422` — `if len(buckets) >= 10:`  (10-bucket warmup minimum (z-score requires N samples) — should be a setting)
- `agents/scalping_agent.py:506` — `elif z >= 0.8:`  ("moderate" direction band ±0.8 — bypasses SCALP_OFI_Z_ENTRY family)
- `agents/scalping_agent.py:510` — `elif z <= -0.8:`  (mirror of 506)
- `agents/scalping_agent.py:527` — `stale = age > 30.0`  (30-second bucket staleness gate; not a setting)
- `agents/scalping_agent.py:1128` — `if age >= 30 and obs.price_30s == 0.0:`  (30/60/180/300-second landmark thresholds in micro tracker; no setting)
- `agents/balance_agent.py:414` — `old: list, new: list, tol: float = 0.10,`  (10% drift tolerance default; should reference BALANCE_STRUCTURAL_DRIFT_HINT or its own setting)
- `agents/balance_agent.py:507` — `if equity > 0 and plan_sum > equity * 1.05:`  (5% rail-1 over-allocation guard; no setting)
- `signals/quality_gate.py:89` — `if session_mod < -10:`  (−10 "dead zone" trip-wire (compare to SESSION_WINDOWS["dead_zone"].score_boost = −15))
- `signals/quality_gate.py:95` — `if guard_penalty <= -999:`  (guard hard-block sentinel; NEWS_GUARD_PENALTY_BLOCK = 999 exists in settings but the comparison is hand-coded)

## keys.env env vars

Discovered via `Grep "os\.getenv\(|os\.environ\["` across the repo (`venv/.git/audit/logs/backups` excluded). Per-source `api_key_env_var` and `rpc_env_var` class attributes were also resolved.

### Anthropic

| Env var | Example read |
|---------|--------------|
| `ANTHROPIC_API_KEY` | `core/agent.py:21` |

### Exchange — Binance

| Env var | Example read |
|---------|--------------|
| `BINANCE_API_KEY` | `core/market_data.py:42` |
| `BINANCE_SECRET` | `core/market_data.py:42` |

### Exchange — Kraken

| Env var | Example read |
|---------|--------------|
| `KRAKEN_API_KEY` | `core/market_data.py:43` |
| `KRAKEN_SECRET` | `core/market_data.py:43` |

### Exchange — Bybit

| Env var | Example read |
|---------|--------------|
| `BYBIT_API_KEY` | `core/market_data.py:44` |
| `BYBIT_SECRET` | `core/market_data.py:44` |

### Exchange — OKX

| Env var | Example read |
|---------|--------------|
| `OKX_API_KEY` | `core/market_data.py:45` |
| `OKX_SECRET` | `core/market_data.py:45` |
| `OKX_PASSPHRASE` | `core/market_data.py:45` |

### Exchange — MEXC (×4 keys)

| Env var | Example read |
|---------|--------------|
| `MEXC_KEY_{1..4}_API_KEY` | `execution/mexc_key_router.py:107,134` |
| `MEXC_KEY_{1..4}_SECRET` | `execution/mexc_key_router.py:108,135` |

### Exchange — generic

| Env var | Example read |
|---------|--------------|
| `<EXCHANGE>_API_KEY (uppercased from settings.EXCHANGES)` | `execution/arb_engine.py:791, agents/__init__.py:88,261` |
| `<EXCHANGE>_SECRET` | `execution/arb_engine.py:792, agents/__init__.py:261` |

### Sentiment — Reddit

| Env var | Example read |
|---------|--------------|
| `REDDIT_CLIENT_ID` | `sentiment/sources/reddit.py:64,98` |
| `REDDIT_CLIENT_SECRET` | `sentiment/sources/reddit.py:99` |
| `REDDIT_USER_AGENT` | `sentiment/sources/reddit.py:100` |

### Sentiment — CryptoPanic

| Env var | Example read |
|---------|--------------|
| `CRYPTOPANIC_API_KEY` | `sentiment/sources/cryptopanic.py:113` |

### Macro — FRED

| Env var | Example read |
|---------|--------------|
| `FRED_API_KEY` | `macro/sources/fred_calendar.py:73,79; data_sources/sources/fred.py:57,68` |

### Macro — Alpha Vantage

| Env var | Example read |
|---------|--------------|
| `ALPHA_VANTAGE_API_KEY` | `data_sources/sources/alpha_vantage.py:41,60` |

### Macro — CoinGecko

| Env var | Example read |
|---------|--------------|
| `COINGECKO_API_KEY` | `data_sources/sources/coingecko.py:42,117` |

### Macro — CryptoCompare

| Env var | Example read |
|---------|--------------|
| `CRYPTOCOMPARE_API_KEY` | `data_sources/sources/cryptocompare.py:37,48` |

### Macro — Trading Economics calendar (stub)

| Env var | Example read |
|---------|--------------|
| `TRADING_ECONOMICS_API_KEY` | `macro/sources/stub_calendar.py:19` |

### Cross-chain RPC

| Env var | Example read |
|---------|--------------|
| `ARBITRUM_RPC_URL` | `execution/chains/arbitrum.py:84,123 (registered in settings.XCHAIN_RPC_ENV_VARS)` |
| `BASE_RPC_URL` | `execution/chains/base_chain.py:26` |
| `OPTIMISM_RPC_URL` | `execution/chains/optimism.py:21; execution/chains/_solidly_volatile.py:108` |

