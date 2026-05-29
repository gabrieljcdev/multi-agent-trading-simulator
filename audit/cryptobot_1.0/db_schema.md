# Database audit — CryptoBot 1.0

This is the consolidated database audit. The ORM-level documentation (models, columns, indexes, queries, table → write/read maps, TODOs) lives in `module_reports/database.md` (820 lines) and is **the source of truth for the schema-from-code view**. This file complements it with the **live snapshot** from `data/cryptobot.db` and a few schema-comparison observations.

## Live SQLite state

Snapshot taken from `data/cryptobot.db` via read-only `.schema` / `COUNT(*)` queries (no row contents were read). Full output: `sqlite_state.txt` (546 lines).

### Tables present in the live DB

```
agent_events
arb_opportunities
arb_trades
calendar_events
candles
capital_movements
circuit_breaker_log
daily_stats
data_log
fund_capital_efficiency
funding_arb_observations
funding_arb_trades
macro_log
portfolio_snapshots
predictions
scalp_observations
sentiment
sentiment_log
signals
trades
xchain_observations
```

21 tables — matches the 21 ORM models documented in `module_reports/database.md`.

### Row counts at snapshot time

| Table | Rows |
|---|---:|
| agent_events | 125 |
| arb_opportunities | 0 |
| arb_trades | 290 |
| calendar_events | 11 |
| candles | 0 |
| capital_movements | 31 |
| circuit_breaker_log | 317 |
| daily_stats | 0 |
| data_log | 46,923 |
| fund_capital_efficiency | 945 |
| funding_arb_observations | 110 |
| funding_arb_trades | 0 |
| macro_log | 66 |
| portfolio_snapshots | 563 |
| predictions | 0 |
| scalp_observations | 214,570 |
| sentiment | 0 |
| sentiment_log | 228 |
| signals | 120 |
| trades | 110 |
| xchain_observations | 7,374 |

Observations:

- **`scalp_observations` dominates the DB at 214,570 rows** — the scalp agent's "observe and skip" log is by far the largest write source.
- **`data_log` is the second-largest at 46,923 rows** — every data-source refresh persists a row.
- **`predictions` is empty (0 rows).** The ORM model and FK relationship from `signals.id` exist, but no training run has populated it. This matches the empty `predictive/` package.
- **`candles` is empty (0 rows).** The model exists but nothing in the live system is persisting OHLCV candles to the DB at present.
- **`sentiment` (the older aggregated table) is empty (0 rows).** All sentiment writes flow into `sentiment_log` (228 rows) — see `database.md` "Known issues" for the deprecation gap.
- **`daily_stats` is empty (0 rows).** The aggregation table is defined but no aggregator is running against it.
- **`arb_opportunities` is empty (0 rows)** while `arb_trades` has 290. The "opportunity log" path is also unwired in practice.
- **`funding_arb_trades` is empty (0 rows)** while `funding_arb_observations` has 110 — sim funding-arb has logged candidate observations but no fills.

### Indexes present in the live DB

See `sqlite_state.txt` for the full list. Notable ones:

```
ix_arb_opportunities_lookup    arb_opportunities
ix_arb_trades_pair             arb_trades
ix_arb_trades_timestamp        arb_trades
ix_calendar_events_lookup      calendar_events
ix_candles_lookup              candles
ix_capital_movements_lookup    capital_movements
ix_circuit_breaker_log_lookup  circuit_breaker_log
ix_daily_stats_date            daily_stats
ix_data_log_source_ts          data_log
ix_fund_capital_efficiency_*   fund_capital_efficiency
ix_funding_arb_observations_*  funding_arb_observations
ix_funding_arb_trades_*        funding_arb_trades
ix_macro_log_timestamp         macro_log
ix_portfolio_snapshots_ts      portfolio_snapshots
ix_predictions_signal_id       predictions
ix_scalp_observations_lookup   scalp_observations
ix_sentiment_log_lookup        sentiment_log
ix_sentiment_lookup            sentiment
ix_signals_pair                signals
ix_signals_timestamp           signals
ix_signals_type                signals
ix_trades_pair                 trades
ix_trades_timestamp            trades
ix_xchain_obs_lookup           xchain_observations
ix_xchain_observations_symbol  xchain_observations
ix_xchain_observations_timestamp xchain_observations
ix_xchain_observations_would_entry xchain_observations
```

`scalp_observations` only has one composite index (`ix_scalp_observations_lookup`) despite holding 214k rows. With more growth this becomes a candidate for additional indexing — see `module_reports/database.md` for the canonical list of fields.

## Migrations

There is **no `alembic/` directory in the repo**. `requirements.txt` pins `alembic==1.13.1` and `pip freeze` shows `alembic==1.18.4` installed, but no migration tree has been bootstrapped.

The migration story is instead:

1. `database.db.init_db()` calls `Base.metadata.create_all()` — idempotent, but **never alters existing tables**.
2. `scripts/migrate_scalp_v2.py` — a hand-rolled idempotent SQLite migration that `ALTER TABLE`s `scalp_observations` to add the v2 columns (`confluence_score`, `strength_label`, `cross_exchange_agrees`, `btc_compatible`, `adverse_selection_ok`, `depth_ok`, `vwap_aligned`, `htf_aligned`, `volume_adequate`, `atr_bps`, `atr_adjusted`, `sl_clamped`, `rr_actual`). Five other TODOs in `database/models.py` flag similar gaps that have not yet been scripted.

`module_reports/database.md` covers this in detail under "Migrations" and "Known issues".

## Cross-reference

For the full per-table column inventory, every relationship, every `queries.py` function signature with its read/write classification, and the table → write/read function map, read `module_reports/database.md`.
