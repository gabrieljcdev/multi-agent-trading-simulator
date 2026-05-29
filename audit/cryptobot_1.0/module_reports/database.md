# Module Report: database

## Purpose
SQLAlchemy 2.x ORM layer over a single SQLite file (`data/cryptobot.db`, path from `config.settings.DB_PATH`). `db.py` owns the engine, applies WAL mode + `foreign_keys=ON` + `synchronous=NORMAL` via a `connect` event listener, exposes a `get_session()` context manager, and runs `init_db()` (idempotent `create_all`) on every startup. `models.py` defines every ORM table; `queries.py` is the single chokepoint for all feature code — modules call helpers there rather than opening sessions directly. `expire_on_commit=False` is explicitly set so query helpers can return ORM rows that callers continue to access after the `with get_session()` block exits.

## Files
| File | LOC | One-sentence summary |
| --- | --- | --- |
| database/__init__.py | 0 | Empty package marker. |
| database/db.py | 66 | Engine, SQLite PRAGMAs, `init_db()`, `get_session()` context manager. |
| database/models.py | 751 | 22 ORM table definitions (Base + 21 mapped classes). |
| database/queries.py | 2485 | ~90 query helpers — every read/write goes through here. |

## Public surface

### database/__init__.py
Empty (zero bytes). No re-exports.

### database/db.py
**Docstring**: "Database connection, session management, and initialisation."

**Functions**
- `set_sqlite_pragma(dbapi_conn, _)` — `@event.listens_for(engine, "connect")`; executes `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `PRAGMA synchronous=NORMAL` on every new SQLite connection.
- `init_db() -> None` — `Base.metadata.create_all(bind=engine)`; safe to call on every startup. Logs the DB path.
- `get_session() -> Session` — `@contextmanager`; yields a `SessionLocal()`, commits on clean exit, rolls back on exception, always closes.

**Module constants**
- `engine` — `create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False}, echo=False)`.
- `SessionLocal` — `sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)`.
- `logger = logging.getLogger(__name__)`.

Side effect at import: `DB_PATH.parent.mkdir(parents=True, exist_ok=True)`.

### database/models.py
**Docstring**: "All database table definitions using SQLAlchemy ORM."

**Class**: `Base(DeclarativeBase)` — declarative base.

#### `Candle` — `__tablename__ = "candles"`
OHLCV per (exchange, pair, timeframe).

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| exchange | String(20) | NOT NULL | — | |
| pair | String(20) | NOT NULL | — | |
| timeframe | String(5) | NOT NULL | — | |
| timestamp | DateTime | NOT NULL | — | |
| open, high, low, close, volume | Float | NOT NULL | — | |
| num_trades | Integer | nullable | — | |
| rsi, macd, macd_signal, macd_hist, bb_upper, bb_mid, bb_lower, ema_fast, ema_slow, volume_sma | Float | nullable | — | pre-computed indicators |
| created_at | DateTime | nullable | `datetime.utcnow` | |

Indexes: `ix_candles_lookup` on (exchange, pair, timeframe, timestamp).
Relationships: none.

#### `Signal` — `__tablename__ = "signals"`
Every signal emitted by the scan, pass or fail.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| pair | String(20) | NOT NULL | — | |
| exchange | String(20) | nullable | — | |
| exchange_b | String(20) | nullable | — | second venue for arb |
| signal_type | String(20) | NOT NULL | — | arb / momentum / reversion |
| direction | String(5) | nullable | — | long / short / arb |
| score | Float | NOT NULL | — | |
| passed_gate | Boolean | nullable | False | |
| rsi, macd_hist, bb_position, volume_ratio, arb_gap_pct | Float | nullable | — | indicator snapshot |
| sentiment_score, sentiment_mod, sentiment_velocity | Float | nullable | — | |
| tf_5m_confirm, tf_15m_confirm, tf_1h_confirm | Boolean | nullable | — | |
| indicators_json | JSON | nullable | — | |
| claude_reasoning | Text | nullable | — | |
| claude_suggested_entry, claude_suggested_sl, claude_suggested_tp, claude_suggested_size, claude_risk_reward, claude_api_cost_usd | Float | nullable | — | |
| user_action | String(10) | nullable | — | go / skip / modify / expired |
| user_action_at | DateTime | nullable | — | |
| skip_reason | Text | nullable | — | free text |
| price_at_signal, price_1h, price_4h, price_24h | Float | nullable | — | future-price tracker |
| outcome | String(10) | nullable | — | win / loss / breakeven / open |
| outcome_pnl_pct | Float | nullable | — | |
| profile, strategy | String(30) | nullable | — | active at signal time |

Indexes: `ix_signals_timestamp`, `ix_signals_pair`, `ix_signals_type`.
Relationships: `trade -> Trade` (back-populates, uselist=False), `prediction -> Prediction` (back-populates, uselist=False).

#### `Trade` — `__tablename__ = "trades"`
Executed trades (sim + live).

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| signal_id | Integer | nullable | — | FK → `signals.id` |
| timestamp_open | DateTime | NOT NULL | `datetime.utcnow` | |
| timestamp_close | DateTime | nullable | — | |
| pair | String(20) | NOT NULL | — | |
| exchange | String(20) | NOT NULL | — | |
| exchange_b | String(20) | nullable | — | arb closing venue |
| side | String(5) | NOT NULL | — | long / short / arb |
| signal_type | String(20) | nullable | — | |
| entry_price | Float | NOT NULL | — | |
| exit_price | Float | nullable | — | |
| size_usd | Float | NOT NULL | — | |
| size_base | Float | nullable | — | |
| stop_loss, take_profit | Float | nullable | — | |
| fees_usd | Float | nullable | 0.0 | |
| pnl_usd, pnl_pct, hold_minutes | Float | nullable | — | |
| exit_reason | String(20) | nullable | — | tp_hit / sl_hit / manual / kill_switch |
| claude_postmortem | Text | nullable | — | |
| sim_mode | Boolean | NOT NULL | True | |
| profile, strategy | String(30) | nullable | — | |
| order_id_open, order_id_close | String(80) | nullable | — | live exchange ids |

Indexes: `ix_trades_timestamp` (timestamp_open), `ix_trades_pair`.
Relationships: `signal -> Signal` (back-populates).

#### `SentimentSnapshot` — `__tablename__ = "sentiment"`
Aggregated per-coin 5-minute snapshot.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| coin | String(10) | NOT NULL | — | BTC / ETH / SOL / MARKET |
| reddit_score, telegram_score, news_score, fear_greed, google_trends | Float | nullable | — | |
| composite | Float | nullable | — | weighted blend |
| velocity | Float | nullable | — | vs 2h ago |
| post_count | Integer | nullable | — | |
| mention_count | Integer | nullable | — | |

Indexes: `ix_sentiment_lookup` on (coin, timestamp).

#### `SentimentLog` — `__tablename__ = "sentiment_log"`
Per-source raw fetch result.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| source_id | String(30) | NOT NULL | — | |
| score | Float | nullable | — | -100..+100 |
| composite_score | Float | nullable | — | aggregator output at log time |
| hard_block | Boolean | nullable | False | |
| block_reason | Text | nullable | — | |
| confidence | Float | nullable | — | 0–1 |
| raw_data | JSON | nullable | — | |

Indexes: `ix_sentiment_log_lookup` on (source_id, timestamp).

#### `MacroLog` — `__tablename__ = "macro_log"`
One row per `MacroMonitor` regime computation.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| scenario | String(25) | nullable | — | GOLDILOCKS / RISK_OFF / … |
| macro_score | Float | nullable | — | -100..+100 |
| dollar_strength | String(10) | nullable | — | STRONG/NEUTRAL/WEAK |
| risk_appetite | String(10) | nullable | — | RISK_ON/NEUTRAL/RISK_OFF |
| rate_environment | String(12) | nullable | — | TIGHTENING/NEUTRAL/EASING |
| vol_regime | String(10) | nullable | — | CALM/ELEVATED/CRISIS |
| dxy, vix, yield_10y, yield_2y, yield_curve, fed_funds_rate, cpi_yoy | Float | nullable | — | |
| confidence | Float | nullable | — | 0..1 |
| raw_data | JSON | nullable | — | |

Indexes: `ix_macro_log_ts` on (timestamp).

#### `CalendarEvent` — `__tablename__ = "calendar_events"`
Scheduled economic-calendar item.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| event_id | String(80) | NOT NULL, UNIQUE | — | natural key for upserts |
| title | String(120) | NOT NULL | — | |
| country | String(10) | nullable | — | |
| scheduled_utc | DateTime | NOT NULL | — | |
| impact | String(8) | nullable | — | HIGH/MEDIUM/LOW |
| actual, forecast, previous | Float | nullable | — | |
| source_id | String(30) | nullable | — | |
| fetched_at | DateTime | nullable | `datetime.utcnow` | |

Indexes: `ix_calendar_events_lookup` on (scheduled_utc, impact). Plus unique on `event_id`.

#### `ScalpObservationModel` — `__tablename__ = "scalp_observations"`
Per-evaluated scalp candidate (entered or skipped).

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK, autoincrement |
| symbol | String(20) | NOT NULL | — | column-level `index=True` |
| exchange | String(20) | NOT NULL | — | `index=True` |
| timestamp | Float | NOT NULL | — | unix epoch; `index=True` |
| ofi_z | Float | nullable | — | |
| direction | String(10) | nullable | — | |
| strength | String(10) | nullable | — | |
| tfi_confirms | Boolean | nullable | — | |
| raw_tfi | Float | nullable | — | |
| spread_bps | Float | nullable | — | |
| regime | String(20) | nullable | — | |
| round_trip_cost_bps | Float | nullable | — | |
| min_win_rate_required | Float | nullable | — | |
| tp_bps, sl_bps | Float | nullable | — | |
| would_entry | Boolean | nullable | — | `index=True` |
| skip_reason | String(160) | nullable | — | |
| entry_price | Float | nullable | — | |
| exit_price | Float | nullable | 0.0 | |
| exit_time | Float | nullable | 0.0 | unix epoch |
| exit_reason | String(30) | nullable | — | |
| hold_sec | Float | nullable | 0.0 | |
| pnl_bps | Float | nullable | 0.0 | GROSS pre-fee |
| pnl_usd | Float | nullable | 0.0 | GROSS |
| observation_only | Boolean | nullable | True | |
| price_30s, price_1m, price_3m, price_5m | Float | nullable | 0.0 | micro-tracker backfill |
| confluence_score | Integer | nullable | — | v2 diagnostics |
| strength_label | String(16) | nullable | — | v2 |
| cross_exchange_agrees, btc_compatible, adverse_selection_ok, depth_ok, vwap_aligned, htf_aligned, volume_adequate | Boolean | nullable | — | v2 |
| atr_bps | Float | nullable | — | v2 |
| atr_adjusted | Boolean | nullable | — | v2 |
| sl_clamped | String(8) | nullable | — | v2 |
| rr_actual | Float | nullable | — | v2 |
| created_at | DateTime | nullable | `datetime.utcnow` | |

Indexes: column-level on `symbol`, `exchange`, `timestamp`, `would_entry`; composite `ix_scalp_obs_lookup` on (symbol, exchange, timestamp).

#### `DataLog` — `__tablename__ = "data_log"`
Generic DataPoint sink from `data_sources/`.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| source_id | String(30) | NOT NULL | — | |
| metric | String(40) | NOT NULL | — | |
| symbol | String(20) | nullable | — | null = global metric |
| value | Float | nullable | — | |
| raw_data | JSON | nullable | — | |
| error | Text | nullable | — | |

Indexes: `ix_data_log_lookup` on (source_id, metric, symbol, timestamp).

#### `Prediction` — `__tablename__ = "predictions"`
ML model output per signal (phase-2 feature).

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| signal_id | Integer | nullable | — | FK → `signals.id` |
| timestamp | DateTime | nullable | `datetime.utcnow` | |
| model_version | String(20) | nullable | — | |
| win_probability | Float | nullable | — | 0.0–1.0 |
| expected_pnl_pct | Float | nullable | — | |
| confidence | Float | nullable | — | |
| features_json | JSON | nullable | — | |
| correct | Boolean | nullable | — | filled at trade close |

Relationships: `signal -> Signal` (back-populates).
Indexes: none declared.

#### `DailyStats` — `__tablename__ = "daily_stats"`
End-of-day aggregate.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| date | String(10) | NOT NULL, UNIQUE | — | YYYY-MM-DD |
| total_signals, signals_passed_gate, signals_acted_on | Integer | nullable | 0 | |
| total_trades, wins, losses | Integer | nullable | 0 | |
| win_rate | Float | nullable | — | |
| pnl_usd | Float | nullable | 0.0 | |
| pnl_pct | Float | nullable | 0.0 | |
| fees_usd | Float | nullable | 0.0 | |
| best_trade_pnl_pct, worst_trade_pnl_pct, avg_hold_minutes | Float | nullable | — | |
| best_signal_type | String(20) | nullable | — | |
| avg_score_winners, avg_score_losers | Float | nullable | — | |
| portfolio_value_usd, portfolio_peak_usd | Float | nullable | — | |
| claude_api_cost_usd | Float | nullable | 0.0 | |
| sim_mode | Boolean | nullable | True | |
| profile, strategy | String(30) | nullable | — | |

Indexes: implicit from UNIQUE on `date`.

#### `ArbTrade` — `__tablename__ = "arb_trades"`
One row per arb attempt by `execution/arb_engine.py`.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | nullable | `datetime.utcnow` | |
| symbol | String(20) | NOT NULL | — | |
| buy_exchange, sell_exchange | String(20) | nullable | — | |
| buy_price, sell_price, buy_fill, sell_fill | Float | nullable | — | |
| gross_gap_pct, net_gap_pct | Float | nullable | — | |
| size_usd, gross_pnl_usd, net_pnl_usd | Float | nullable | — | |
| execution_ms | Float | nullable | — | |
| status | String(20) | nullable | "executed" | "executed" / "balance_fail" |
| slippage_buy_pct, slippage_sell_pct | Float | nullable | — | |
| sim_mode | Boolean | nullable | True | |
| success | Boolean | nullable | False | |
| error | Text | nullable | — | |

Indexes: `ix_arb_trades_lookup` on (symbol, timestamp).

#### `ArbOpportunity` — `__tablename__ = "arb_opportunities"`
Every above-liquidity gap detected by the arb scan.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| detected_at | DateTime | nullable | `datetime.utcnow` | |
| symbol | String(20) | NOT NULL | — | |
| buy_exchange, sell_exchange | String(20) | nullable | — | |
| gap_pct, threshold_pct | Float | nullable | — | |
| above_threshold | Boolean | nullable | False | |
| depth_buy_usd, depth_sell_usd | Float | nullable | — | |
| executed | Boolean | nullable | False | |
| arb_trade_id | Integer | nullable | — | FK → `arb_trades.id` |

Indexes: `ix_arb_opportunities_lookup` on (symbol, detected_at).

#### `FundingArbTrade` — `__tablename__ = "funding_arb_trades"`
Funding-rate arb attempts by `FundingRateArbEngine`.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | nullable | `datetime.utcnow` | |
| symbol | String(20) | NOT NULL | — | |
| buy_exchange, sell_exchange | String(20) | nullable | — | |
| buy_price, sell_price, buy_fill, sell_fill | Float | nullable | — | |
| gross_gap_pct, net_gap_pct, size_usd, gross_pnl_usd, net_pnl_usd | Float | nullable | — | |
| execution_ms | Float | nullable | — | |
| status | String(20) | nullable | "executed" | |
| slippage_buy_pct, slippage_sell_pct | Float | nullable | — | |
| funding_rate_pct | Float | nullable | — | |
| sim_mode | Boolean | nullable | True | |
| success | Boolean | nullable | False | |
| error | Text | nullable | — | |

Indexes: `ix_funding_arb_trades_lookup` on (symbol, timestamp).

#### `FundingArbObservationModel` — `__tablename__ = "funding_arb_observations"`
Per-evaluated funding arb opportunity (observation-mode).

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | Float | NOT NULL | — | epoch; `index=True` |
| symbol | String(20) | NOT NULL | — | `index=True` |
| variant | String(20) | NOT NULL | — | delta_neutral |
| venue_long, venue_short | String(20) | nullable | — | |
| funding_apr, spread_apr | Float | nullable | — | |
| oi_usd | Float | nullable | — | |
| depth_ok | Boolean | nullable | True | |
| notional_usd, margin_used | Float | nullable | — | |
| basis_at_entry | Float | nullable | 0.0 | |
| projected_funding_per_interval, projected_fees, projected_net_apr | Float | nullable | 0.0 | |
| would_enter | Boolean | nullable | False | `index=True` |
| skip_reason | String(160) | nullable | — | |
| exit_time | Float | nullable | 0.0 | |
| exit_reason | String(30) | nullable | — | |
| hold_sec | Float | nullable | 0.0 | |
| funding_collected, fees_paid, pnl_usd | Float | nullable | 0.0 | realised net |
| observation_only | Boolean | nullable | True | |
| created_at | DateTime | nullable | `datetime.utcnow` | |

Indexes: column-level on `timestamp`, `symbol`, `would_enter`; composite `ix_funding_arb_obs_lookup` on (symbol, timestamp).

#### `XChainObservation` — `__tablename__ = "xchain_observations"`
Per-evaluated cross-chain arb (observation-only).

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | nullable | `datetime.utcnow` | `index=True` |
| symbol | String(20) | NOT NULL | — | `index=True` |
| buy_chain, sell_chain | String(20) | NOT NULL | — | |
| buy_venue, sell_venue | String(30) | nullable | — | |
| notional_usd | Float | nullable | — | |
| spread_bps, rt_fee_bps, gas_bps, slip_bps | Float | nullable | — | |
| bridge_bps | Float | nullable | 0.0 | |
| net_edge_bps, gas_breakeven_usd | Float | nullable | — | |
| would_entry | Boolean | nullable | False | `index=True` |
| skip_reason | String(160) | nullable | — | |
| observation_only | Boolean | nullable | True | |

Indexes: column-level on `timestamp`, `symbol`, `would_entry`; composite `ix_xchain_obs_lookup` on (symbol, timestamp). No P&L columns by design (observation mode).

#### `PortfolioSnapshot` — `__tablename__ = "portfolio_snapshots"`
Cross-agent state every `PORTFOLIO_MONITOR_INTERVAL_SEC`.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| total_equity, total_daily_pnl, total_exposure_pct | Float | nullable | — | |
| agents_running | Integer | nullable | — | |
| portfolio_status | String(20) | nullable | — | HEALTHY/WARNING/HALTED |
| snapshot_json | JSON | nullable | — | full stats dict |

Indexes: `ix_portfolio_snapshots_ts` on (timestamp).

#### `AgentEvent` — `__tablename__ = "agent_events"`
Lifecycle event log per agent.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | nullable | `datetime.utcnow` | |
| agent_id | String(30) | NOT NULL | — | |
| event_type | String(20) | nullable | — | STARTED/STOPPED/HALTED/KILLED/ERROR/SKIPPED |
| detail | Text | nullable | — | |

Indexes: `ix_agent_events_lookup` on (agent_id, timestamp).

#### `CapitalMovement` — `__tablename__ = "capital_movements"`
BalanceAgent rebalance attempts + lifecycle state machine.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| from_fund, to_fund | String(30) | NOT NULL | — | |
| from_exchange, to_exchange | String(30) | nullable | — | schema extension |
| asset | String(10) | nullable | "USDT" | |
| amount_usd | Float | NOT NULL | — | |
| mode | String(8) | nullable | "sim" | sim / live |
| state | String(12) | nullable | "pending" | pending/in_transit/completed/failed |
| initiated_by | String(30) | nullable | — | policy/web_ui/auto/… |
| note | Text | nullable | — | |
| rail_id | String(30) | nullable | — | sim/cex/… |
| network | String(20) | nullable | — | ccxt network |
| transfer_tx_hash | String(120) | nullable | — | live only |
| error | Text | nullable | — | |
| completed_at | DateTime | nullable | — | |

Indexes: `ix_capital_movements_state` on (state, timestamp); `ix_capital_movements_lookup` on (from_fund, to_fund, timestamp).

#### `FundCapitalEfficiency` — `__tablename__ = "fund_capital_efficiency"`
Per-fund return-on-deployed-capital snapshot.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | NOT NULL | `datetime.utcnow` | |
| fund | String(30) | NOT NULL | — | |
| deployed_usd | Float | nullable | 0.0 | |
| realised_return_usd | Float | nullable | 0.0 | |
| return_on_deployed_pct | Float | nullable | 0.0 | |
| starvation_event | Boolean | nullable | False | |
| starvation_detail | String(200) | nullable | — | |

Indexes: `ix_fund_capital_efficiency_lookup` on (fund, timestamp).

#### `CircuitBreakerLog` — `__tablename__ = "circuit_breaker_log"`
One row per circuit-breaker firing.

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| id | Integer | — | — | PK |
| timestamp | DateTime | nullable | `datetime.utcnow` | |
| reason | String(50) | nullable | — | daily_loss/consecutive_loss/drawdown/kill_switch |
| detail | Text | nullable | — | |
| auto_resume_at | DateTime | nullable | — | |
| manually_resolved | Boolean | nullable | False | |

Indexes: none declared.

### database/queries.py
**Docstring**: "Reusable query functions. Everything that touches the DB goes through here."

**Module constants**
- `_SESSIONS = ("LONDON", "NEW_YORK", "ASIA", "OFF_HOURS")` — trading-session bucket names.

**Functions** (signature + one-line summary)

Candles:
- `get_recent_candles(exchange, pair, timeframe, limit=200)` — read: newest-first Candle rows.
- `save_candle(candle_data: dict)` — write: `session.merge()` on Candle.

Signals:
- `save_signal(signal_data: dict) -> int` — insert Signal, return new id.
- `update_signal_decision(signal_id, action)` — patch user_action + user_action_at.
- `update_signal_outcome(signal_id, outcome, pnl_pct)` — patch outcome + outcome_pnl_pct.
- `update_signal_claude(signal_id, fields: dict)` — patch any of Claude evaluation fields (whitelisted).
- `update_signal_skip(signal_id, reason, price_at_signal=None)` — mark skip with reason + optional price snapshot.
- `update_signal_future_prices(signal_id, fields: dict)` — patch price_1h/4h/24h.
- `get_signals_needing_price_update(hours=24) -> list` — signals with any null future-price slot.
- `get_today_skipped_signals() -> int` — count of today's skips (counts `user_action='skip'` or `skip_reason IS NOT NULL`).
- `get_trade_by_id(trade_id) -> Trade | None`.
- `get_signal_history(days=30, signal_type=None)` — historical GO-action signals.

Trades:
- `save_trade(trade_data: dict) -> int`.
- `close_trade(trade_id, exit_price, exit_reason, pnl_usd, pnl_pct)` — set close fields + hold_minutes.
- `get_open_trades()` — Trade rows with `timestamp_close IS NULL`.
- `get_today_trades()` — today's trades by `timestamp_open`.
- `get_today_pnl_pct() -> float` — sum of today's closed pnl_pct.
- `get_recent_closed_trades(limit=5) -> list`.
- `save_postmortem(trade_id, text)`.
- `get_consecutive_losses() -> int` — scans the 10 most recent closed trades.

Sentiment:
- `save_sentiment(data: dict)` — insert SentimentSnapshot.
- `get_latest_sentiment(coin='MARKET')`.
- `log_sentiment_result(result, composite_score=0.0)` — write SentimentLog from duck-typed SourceResult.
- `get_sentiment_history(source_id, hours=24)`.
- `get_composite_history(hours=24)` — (timestamp, composite_score) tuples.

Daily stats:
- `upsert_daily_stats(date_str, data: dict)`.

Arb engine:
- `log_arb_trade(result, sim_mode=True) -> int`.
- `log_arb_balance_fail(symbol, buy_exchange, sell_exchange, detail, sim_mode=True) -> int` — status='balance_fail'.
- `get_arb_trades(hours=24)`.
- `get_arb_pnl_today() -> float`.
- `get_arb_stats() -> dict` — counts/win_rate/avg_pnl/best_pair/best_combo (all-time successful).

Arb opportunities:
- `log_arb_opportunity(...) -> int`.
- `mark_arb_opportunity_executed(opp_id, arb_trade_id)`.
- `get_arb_opportunities_today() -> list`.
- `get_arb_opportunity_stats() -> dict`.

Funding arb:
- `log_funding_arb_trade(result, sim_mode=True) -> int`.
- `get_true_pnl(days=7) -> dict` — arb gross P&L net of CapitalMovement sim-fees in window.
- `get_funding_arb_pnl_today() -> float`.

Portfolio + agents:
- `log_portfolio_snapshot(stats: dict)`.
- `log_agent_event(agent_id, event_type, detail='')`.
- `get_last_equity() -> Optional[float]` — most recent total_equity or None on empty.
- `get_trade_realized_pnl(*, only_strategy=None, exclude_strategy=None, today=False) -> float`.
- `get_arb_realized_pnl(*, today=False) -> float`.
- `get_scalp_realized_pnl(*, today=False) -> float` — sums pnl_usd of closed scalp_observations.
- `get_portfolio_history(hours=24)`.
- `get_agent_events(agent_id=None, limit=50)`.

Circuit breakers:
- `log_circuit_breaker(reason, detail, auto_resume_at=None)`.

Data sources:
- `log_data_point(point)` — DataPoint → DataLog.
- `get_data_history(source_id, metric, symbol=None, hours=24)`.
- `get_latest_data_point(source_id, metric, symbol=None)`.
- `get_data_at_time(source_id, metric, timestamp, symbol=None)`.

Macro:
- `save_macro_regime(regime)`.
- `get_macro_history(hours=24)`.
- `save_calendar_events(events: list)` — upsert keyed on event_id.
- `get_pending_events(hours_ahead=48)`.

Scalp observations:
- `save_scalp_observations(obs_list: list)` — upsert keyed on (symbol, exchange, timestamp).
- `get_scalp_summary(days=7) -> dict`.
- `get_scalp_observations(symbol=None, exchange=None, would_entry_only=True, closed_only=True, limit=500) -> list[dict]`.

Funding observations:
- `save_funding_observations(rows: list)` — upsert keyed on (symbol, timestamp, variant).
- `get_funding_summary(days=7) -> dict`.
- `get_funding_observations(symbol=None, would_enter_only=False, limit=500) -> list[dict]`.

Analytics:
- `get_signal_win_rate(signal_type=None, days=30, exclude_strategy=None) -> dict`.

Web UI v1:
- `_session_for_hour(hour) -> str` (private).
- `get_session_pnl_today() -> dict` — combines Trade + ArbTrade by UTC hour.
- `get_top_pairs(n=5) -> list[dict]`.
- `get_strategy_performance() -> list[dict]`.
- `get_recent_postmortems(n=3) -> list[dict]`.
- `_arb_trade_to_dict(r)` (private).
- `get_arb_trades_all() -> list[dict]`.

Web UI v2:
- `get_alltime_realised_pnl() -> float`.
- `get_daily_fees() -> float` — Trade.fees_usd (non-scalp) + arb gross-net + scalp round-trip cost USD.
- `_scalp_obs_to_dict(r)` (private).
- `get_scalp_closed_today() -> list[dict]`.
- `get_scalp_trade_history(limit=100) -> list[dict]`.
- `_trade_to_history_dict(t)` (private).
- `get_signal_trade_history(limit=100) -> list[dict]`.
- `get_postmortems_by_agent(agent_id, n=3) -> list[dict]`.
- `get_closed_trades_by_session(session, date='today') -> list[dict]` — unions Trade + ArbTrade + scalp_observations.

Scalp activation:
- `get_scalp_activation_stats() -> dict`.
- `_scalp_readiness(stats, *, min_obs, min_wr, min_net, max_hold, min_dir)` (private).
- `get_scalp_activation_readiness() -> dict` (v1 thresholds).
- `get_scalp_activation_readiness_v2() -> dict`.

Cross-chain:
- `insert_xchain_observation(...) -> int`.
- `get_xchain_summary(days=7) -> dict`.
- `get_xchain_observations(symbol=None, would_entry_only=False, limit=500) -> list[dict]`.

Capital movements:
- `log_capital_movement(data: dict) -> int`.
- `update_capital_movement(movement_id, fields: dict)` (whitelisted).
- `get_in_transit_movements()`.
- `get_capital_movements_today()`.
- `get_capital_movement_history(hours=168)`.

Fund capital efficiency:
- `log_fund_capital_efficiency(data: dict) -> int`.
- `get_fund_capital_efficiency(fund, hours=720)`.
- `get_latest_fund_efficiency(fund)`.

Web UI v2 agent-panel snapshots (defensive — return defaults on Exception):
- `_today_utc_start()` (private).
- `get_xchain_today_summary() -> dict`.
- `get_funding_today_summary() -> dict` — includes `skip_reasons` taxonomy.
- `_capital_movement_to_dict(r)` (private).
- `get_capital_movements_recent(limit=50) -> list[dict]`.
- `get_capital_movements_in_transit() -> list[dict]`.
- `get_fund_efficiency_summary(window_hours=24) -> list[dict]`.

## ORM models (summary table)
| Model | Table name | Primary key | Notable indexes | FKs | Relationships |
| --- | --- | --- | --- | --- | --- |
| Candle | candles | id | ix_candles_lookup(exchange,pair,timeframe,timestamp) | — | — |
| Signal | signals | id | ix_signals_timestamp / _pair / _type | — | Trade (1:1), Prediction (1:1) |
| Trade | trades | id | ix_trades_timestamp(open) / _pair | signal_id→signals.id | Signal |
| SentimentSnapshot | sentiment | id | ix_sentiment_lookup(coin,timestamp) | — | — |
| SentimentLog | sentiment_log | id | ix_sentiment_log_lookup(source_id,timestamp) | — | — |
| MacroLog | macro_log | id | ix_macro_log_ts | — | — |
| CalendarEvent | calendar_events | id | ix_calendar_events_lookup(scheduled_utc,impact); UNIQUE(event_id) | — | — |
| ScalpObservationModel | scalp_observations | id | col-idx on symbol/exchange/timestamp/would_entry; ix_scalp_obs_lookup | — | — |
| DataLog | data_log | id | ix_data_log_lookup(source_id,metric,symbol,timestamp) | — | — |
| Prediction | predictions | id | — | signal_id→signals.id | Signal |
| DailyStats | daily_stats | id | UNIQUE(date) | — | — |
| ArbTrade | arb_trades | id | ix_arb_trades_lookup(symbol,timestamp) | — | — |
| ArbOpportunity | arb_opportunities | id | ix_arb_opportunities_lookup(symbol,detected_at) | arb_trade_id→arb_trades.id | — |
| FundingArbTrade | funding_arb_trades | id | ix_funding_arb_trades_lookup(symbol,timestamp) | — | — |
| FundingArbObservationModel | funding_arb_observations | id | col-idx on timestamp/symbol/would_enter; ix_funding_arb_obs_lookup | — | — |
| XChainObservation | xchain_observations | id | col-idx on timestamp/symbol/would_entry; ix_xchain_obs_lookup | — | — |
| PortfolioSnapshot | portfolio_snapshots | id | ix_portfolio_snapshots_ts | — | — |
| AgentEvent | agent_events | id | ix_agent_events_lookup(agent_id,timestamp) | — | — |
| CapitalMovement | capital_movements | id | ix_capital_movements_state(state,timestamp); ix_capital_movements_lookup | — | — |
| FundCapitalEfficiency | fund_capital_efficiency | id | ix_fund_capital_efficiency_lookup(fund,timestamp) | — | — |
| CircuitBreakerLog | circuit_breaker_log | id | — | — | — |

Total: 21 mapped models.

## Queries summary
| Function | Tables touched | R/W | Brief purpose |
| --- | --- | --- | --- |
| get_recent_candles | candles | R | newest-first OHLCV |
| save_candle | candles | W | merge upsert |
| save_signal | signals | W | insert, return id |
| update_signal_decision | signals | W | user_action + timestamp |
| update_signal_outcome | signals | W | outcome + pnl_pct |
| update_signal_claude | signals | W | whitelisted Claude fields |
| update_signal_skip | signals | W | skip + reason + price |
| update_signal_future_prices | signals | W | price_1h/4h/24h |
| get_signals_needing_price_update | signals | R | rows with null future-price |
| get_today_skipped_signals | signals | R | count |
| get_trade_by_id | trades | R | PK lookup |
| get_signal_history | signals | R | go-action history |
| save_trade | trades | W | insert, return id |
| close_trade | trades | W | exit + pnl + hold_minutes |
| get_open_trades | trades | R | timestamp_close NULL |
| get_today_trades | trades | R | today by open ts |
| get_today_pnl_pct | trades | R | sum today pnl_pct |
| get_recent_closed_trades | trades | R | last N closed |
| save_postmortem | trades | W | claude_postmortem |
| get_consecutive_losses | trades | R | scans last 10 |
| save_sentiment | sentiment | W | insert |
| get_latest_sentiment | sentiment | R | newest by coin |
| log_sentiment_result | sentiment_log | W | insert |
| get_sentiment_history | sentiment_log | R | window by source |
| get_composite_history | sentiment_log | R | (ts,composite) |
| upsert_daily_stats | daily_stats | W | insert-or-update by date |
| log_arb_trade | arb_trades | W | insert, return id |
| log_arb_balance_fail | arb_trades | W | status='balance_fail' row |
| get_arb_trades | arb_trades | R | window |
| get_arb_pnl_today | arb_trades | R | sum net_pnl_usd today |
| get_arb_stats | arb_trades | R | aggregate (all-time success) |
| log_arb_opportunity | arb_opportunities | W | insert |
| mark_arb_opportunity_executed | arb_opportunities | W | flip executed + link |
| get_arb_opportunities_today | arb_opportunities | R | today |
| get_arb_opportunity_stats | arb_opportunities | R | aggregate |
| log_funding_arb_trade | funding_arb_trades | W | insert, return id |
| get_true_pnl | arb_trades, capital_movements | R | gross arb − rebalance cost |
| get_funding_arb_pnl_today | funding_arb_trades | R | sum today |
| log_portfolio_snapshot | portfolio_snapshots | W | insert |
| log_agent_event | agent_events | W | insert |
| get_last_equity | portfolio_snapshots | R | latest total_equity |
| get_trade_realized_pnl | trades | R | sum pnl_usd (filtered) |
| get_arb_realized_pnl | arb_trades | R | sum net_pnl_usd |
| get_scalp_realized_pnl | scalp_observations | R | sum closed-entry pnl_usd |
| get_portfolio_history | portfolio_snapshots | R | window |
| get_agent_events | agent_events | R | newest by agent |
| log_circuit_breaker | circuit_breaker_log | W | insert |
| log_data_point | data_log | W | insert |
| get_data_history | data_log | R | (source,metric,symbol) window |
| get_latest_data_point | data_log | R | newest |
| get_data_at_time | data_log | R | latest ≤ ts |
| save_macro_regime | macro_log | W | insert |
| get_macro_history | macro_log | R | window |
| save_calendar_events | calendar_events | W | upsert by event_id |
| get_pending_events | calendar_events | R | upcoming window |
| save_scalp_observations | scalp_observations | W | upsert (sym,ex,ts) |
| get_scalp_summary | scalp_observations | R | aggregate |
| get_scalp_observations | scalp_observations | R | filtered → dicts |
| save_funding_observations | funding_arb_observations | W | upsert (sym,ts,variant) |
| get_funding_summary | funding_arb_observations | R | aggregate |
| get_funding_observations | funding_arb_observations | R | newest → dicts |
| get_signal_win_rate | trades | R | model-training stats |
| get_session_pnl_today | trades, arb_trades | R | by-session P&L |
| get_top_pairs | trades | R | top-N by realised pnl |
| get_strategy_performance | trades | R | per-track aggregate |
| get_recent_postmortems | trades | R | last N postmortems |
| get_arb_trades_all | arb_trades | R | all (dicts) |
| get_alltime_realised_pnl | trades, arb_trades | R | bankroll sum |
| get_daily_fees | trades, arb_trades, scalp_observations | R | today's fees USD |
| get_scalp_closed_today | scalp_observations | R | today's closed (dicts) |
| get_scalp_trade_history | scalp_observations | R | newest closed (dicts) |
| get_signal_trade_history | trades | R | newest non-scalp (dicts) |
| get_postmortems_by_agent | trades | R | last N per agent |
| get_closed_trades_by_session | trades, arb_trades, scalp_observations | R | union by session |
| get_scalp_activation_stats | scalp_observations | R | activation aggregate |
| get_scalp_activation_readiness | scalp_observations | R | v1 gate vs thresholds |
| get_scalp_activation_readiness_v2 | scalp_observations | R | v2 gate |
| insert_xchain_observation | xchain_observations | W | insert |
| get_xchain_summary | xchain_observations | R | aggregate |
| get_xchain_observations | xchain_observations | R | newest → dicts |
| log_capital_movement | capital_movements | W | insert |
| update_capital_movement | capital_movements | W | whitelisted patch |
| get_in_transit_movements | capital_movements | R | pending+in_transit |
| get_capital_movements_today | capital_movements | R | today |
| get_capital_movement_history | capital_movements | R | window |
| log_fund_capital_efficiency | fund_capital_efficiency | W | insert |
| get_fund_capital_efficiency | fund_capital_efficiency | R | window per fund |
| get_latest_fund_efficiency | fund_capital_efficiency | R | newest per fund |
| get_xchain_today_summary | xchain_observations | R | today aggregate (defensive) |
| get_funding_today_summary | funding_arb_observations | R | today + skip taxonomy |
| get_capital_movements_recent | capital_movements | R | newest → dicts |
| get_capital_movements_in_transit | capital_movements | R | state filter → dicts |
| get_fund_efficiency_summary | fund_capital_efficiency | R | per-fund window summary |

## Table → write/read functions
| Table | Write functions | Read functions |
| --- | --- | --- |
| candles | save_candle | get_recent_candles |
| signals | save_signal, update_signal_decision, update_signal_outcome, update_signal_claude, update_signal_skip, update_signal_future_prices | get_signals_needing_price_update, get_today_skipped_signals, get_signal_history |
| trades | save_trade, close_trade, save_postmortem | get_trade_by_id, get_open_trades, get_today_trades, get_today_pnl_pct, get_recent_closed_trades, get_consecutive_losses, get_trade_realized_pnl, get_signal_win_rate, get_session_pnl_today, get_top_pairs, get_strategy_performance, get_recent_postmortems, get_alltime_realised_pnl, get_daily_fees, get_signal_trade_history, get_postmortems_by_agent, get_closed_trades_by_session |
| sentiment | save_sentiment | get_latest_sentiment |
| sentiment_log | log_sentiment_result | get_sentiment_history, get_composite_history |
| macro_log | save_macro_regime | get_macro_history |
| calendar_events | save_calendar_events | get_pending_events |
| scalp_observations | save_scalp_observations | get_scalp_summary, get_scalp_observations, get_scalp_realized_pnl, get_scalp_closed_today, get_scalp_trade_history, get_daily_fees, get_closed_trades_by_session, get_scalp_activation_stats, get_scalp_activation_readiness, get_scalp_activation_readiness_v2 |
| data_log | log_data_point | get_data_history, get_latest_data_point, get_data_at_time |
| predictions | — (no helper; no inserts via queries.py) | — |
| daily_stats | upsert_daily_stats | — |
| arb_trades | log_arb_trade, log_arb_balance_fail | get_arb_trades, get_arb_pnl_today, get_arb_stats, get_true_pnl, get_arb_realized_pnl, get_session_pnl_today, get_arb_trades_all, get_alltime_realised_pnl, get_daily_fees, get_closed_trades_by_session |
| arb_opportunities | log_arb_opportunity, mark_arb_opportunity_executed | get_arb_opportunities_today, get_arb_opportunity_stats |
| funding_arb_trades | log_funding_arb_trade | get_funding_arb_pnl_today |
| funding_arb_observations | save_funding_observations | get_funding_summary, get_funding_observations, get_funding_today_summary |
| xchain_observations | insert_xchain_observation | get_xchain_summary, get_xchain_observations, get_xchain_today_summary |
| portfolio_snapshots | log_portfolio_snapshot | get_last_equity, get_portfolio_history |
| agent_events | log_agent_event | get_agent_events |
| capital_movements | log_capital_movement, update_capital_movement | get_in_transit_movements, get_capital_movements_today, get_capital_movement_history, get_true_pnl, get_capital_movements_recent, get_capital_movements_in_transit |
| fund_capital_efficiency | log_fund_capital_efficiency | get_fund_capital_efficiency, get_latest_fund_efficiency, get_fund_efficiency_summary |
| circuit_breaker_log | log_circuit_breaker | — |

## Migrations
No `alembic/` directory exists at the repo root. There is no Alembic migration history checked in.

Schema evolution relies entirely on `init_db()` calling `Base.metadata.create_all` (which is additive — it only creates missing tables, never alters existing ones). Five `# TODO` comments in `models.py` explicitly note that existing DBs need a manual Alembic migration to pick up new tables or new columns (see "TODOs" section below). The de-facto migration story is "delete `data/cryptobot.db` and let init_db rebuild" or hand-rolled ALTER statements.

## Imports graph

### Imports from project
- `database/db.py` → `config.settings.DB_PATH`, `database.models.Base`.
- `database/models.py` → only SQLAlchemy stdlib.
- `database/queries.py` → `database.db.get_session`, `database.models.*`. Lazy imports inside functions: `config.settings`, `database.models.CapitalMovement` / `FundCapitalEfficiency` (to avoid circular cost), `datetime` re-import in `log_data_point`, `time` module locally.

### Imported by (grep `from database` / `import database`)
- Source modules (37 matches): `main.py`, `core/bot.py`, `core/market_data.py`, `core/agent.py`, `signals/engine.py`, `execution/{arb_engine,funding_engine,crosschain_engine,router,position_manager,kill_switch}.py`, `agents/{coordinator,scalping_agent,balance_agent,funding_arb_agent,crosschain_agent,__init__}.py`, `agents/balance/{planner.py,rails/cex_rail.py,rails/sim_rail.py,policy/growth_optimal.py}`, `data_sources/__init__.py`, `sentiment/aggregator.py`, `macro/monitor.py`, `ui/{web_server,dashboard}.py`.
- Doc/prompts that reference (informational): `RUNBOOK.md`, `prompts/{5kfund,fix_pre_soak,build_funding_arb,build_scalping_agent,build_fixes,build_wiring}.md`.

## Tests
Tests that import from `database/`:
- `tests/test_queries.py` — `test_get_last_equity_returns_none_on_empty_table`, `..._returns_most_recent_value`, `..._skips_null_rows`, `test_portfolio_snapshot_timestamp_is_non_zero_after_write`, `test_get_trade_by_id_returns_none_when_missing`, `..._returns_row`, `test_get_signal_win_rate_excludes_strategy`, `test_get_today_pnl_pct_treats_trade_pnl_pct_as_fraction`. Covers the equity-recovery + single-row lookup + win-rate filter paths.
- `tests/test_equity_reconstruction.py` — `test_trade_realized_pnl_all_time_today_and_strategy`, `..._empty`, `test_arb_realized_pnl`, `test_session_pnl_includes_arb_and_trade`, `test_scalp_realized_pnl`, plus 3 monkeypatch tests for engine-side reconstruction. Validates per-fund realised-P&L sums match the ledger.
- `tests/test_scalp_activation.py` — `test_activation_readiness_empty_not_ready`, `test_activation_stats_and_v1_v2`, `test_recalibration_statements_execute`. Exercises `get_scalp_activation_*` against in-memory observations.
- `tests/test_macro.py` — uses `database.db` + `database.queries` to round-trip `save_macro_regime` / `get_macro_history`.
- `tests/test_funding_arb.py` — recreates `Base.metadata`, exercises `save_funding_observations` + `get_funding_summary` against a temp DB.
- `tests/test_balance_agent.py` — exercises `log_capital_movement`, `update_capital_movement`, `get_true_pnl`, plus checks `from database.queries import get_true_pnl` import path.
- `tests/test_dashboard.py` — imports `database.queries` for dashboard panel reads.
- `tests/test_web_server.py` — uses `database.db`, `database.queries`, `database.models` extensively against a temp DB for the v2 panel snapshots.

## TODOs / FIXMEs / stubs
- `database/models.py:441` — `TODO: existing databases need an Alembic migration to add the status / slippage_buy_pct / slippage_sell_pct columns. Fresh init_db creates them automatically.` (ArbTrade)
- `database/models.py:481` — `TODO: new table — fresh init_db creates it; existing databases need an Alembic migration.` (ArbOpportunity)
- `database/models.py:511` — `TODO: new table — fresh init_db creates it; existing databases need an Alembic migration.` (FundingArbTrade)
- `database/models.py:559` — `TODO: new table — fresh init_db creates it; existing databases need an Alembic migration.` (FundingArbObservationModel)
- `database/models.py:610` — `TODO: new table — fresh init_db creates it; existing databases need an Alembic migration.` (XChainObservation)
- `database/queries.py:607` — `TODO: wire once live transfers carry a fee_usd column.` (inside `get_true_pnl` — live rebalance fees are currently counted as zero.)

No `FIXME` / `XXX` / `HACK` markers under `database/`.

## Known issues observed

1. **No Alembic; five tables / one column-set need hand migration.** `init_db()` only creates missing tables — it never adds columns to existing ones. The five TODOs in `models.py` flag this explicitly, but there is no migration script, runbook step, or guard to enforce the ALTERs. A bot started against an older `data/cryptobot.db` will silently lose the new columns on `ArbTrade` (`status`, `slippage_buy_pct`, `slippage_sell_pct`) and writes will fail with SQLite's "no such column" error.
2. **`Predictions` table has no query helpers.** Although the `predictions` table is created and `Signal.prediction` is a 1:1 relationship, **no function in `queries.py` writes or reads it**. The model is dead code at the moment — the `predictive/trainer` workflow described in CLAUDE.md would need to be wired up via this layer, but currently any predictive code must open a session directly (violating the documented invariant).
3. **`Signal` columns missing from typical write paths.** `outcome` / `outcome_pnl_pct` are only set via `update_signal_outcome` (one call site), and the `tf_*_confirm`, `indicators_json`, `claude_api_cost_usd` columns rely on the scanner shoving them into the dict passed to `save_signal`. There is no compile-time check that `signals/base.py:Signal.to_db_dict()` keeps pace with `models.Signal` — the CLAUDE.md invariant about that synchronisation is enforced only by convention.
4. **`CircuitBreakerLog` has no index on `timestamp` or `reason`.** Queries scoped to "circuit breakers in the last hour" or "today's halt reasons" would scan the whole table. Currently no read helper exists, but the table is being written without retrieval ever planned.
5. **`expire_on_commit=False` is load-bearing.** `db.py` comments call this out. Multiple query helpers return ORM rows out of the `with get_session()` block (`get_open_trades`, `get_in_transit_movements`, `get_capital_movement_history`, …); a future move back to the SQLAlchemy default would silently raise `DetachedInstanceError` across many consumers including the dashboard and tests.
6. **Mixed-type `timestamp` columns.** `ScalpObservationModel.timestamp` and `FundingArbObservationModel.timestamp` are `Float` (epoch), while every other timestamp is `DateTime`. Aggregations that union these (e.g. `get_closed_trades_by_session` uses `o.created_at` for scalp, not `o.timestamp`) must be careful — `get_funding_today_summary` correctly converts via `_today_utc_start().timestamp()`, but the asymmetry is a footgun for new joins.
7. **`get_consecutive_losses` early-exits the day's snapshot.** It scans only the latest 10 trades and stops on the first non-loss — a 30-trade losing streak is invisible.
8. **`get_arb_stats` and `get_top_pairs` are all-time, unbounded.** They load every row of `arb_trades` / `trades` into memory; on a busy soak the dashboard latency degrades linearly.
9. **No explicit unique constraint on `(symbol, exchange, timestamp)` for scalp_observations** even though `save_scalp_observations` treats it as a natural key. A race could insert duplicates; the existing index is non-unique.
10. **`get_true_pnl` understates live-mode rebalance cost.** TODO at `queries.py:607` — live rows always charge 0 because there's no `fee_usd` column on `CapitalMovement`. Reported net P&L is optimistic once live transfers run.
