# Glossary

Project-specific vocabulary. Lookup before grepping — many of these
names are reused across modules and docs.

---

## Bot Architecture

### ScalpingAgent
The scalping agent. Rule-based, no Claude evaluation. Uses Order Flow
Imbalance as the primary signal. Starts in observation mode
(`SCALP_CAPITAL=0`) and logs every evaluated setup to `scalp_observations`
for analysis. Activated by setting `SCALP_CAPITAL > 0` after data
confirms edge.

### Observation Mode
Agent state when `SCALP_CAPITAL = 0.0`. All signal evaluation, gate
checks, and position lifecycle logic runs normally, but `_place_order()`
is never called. All outcomes are logged as `observation_only=True` in
`scalp_observations`. The data collected in observation mode is the
basis for activation decisions.

### OFI Engine
The Order Flow Imbalance computation engine inside `ScalpingAgent`.
Implements the Cont-Kukanov-Stoikov multi-level formula. Keyed by
`(symbol, exchange)` — separate z-score per exchange. Produces a
normalised z-score, direction, strength, and TFI (Trade Flow Imbalance)
confirmation flag.

### FeeManager
Internal class in `ScalpingAgent`. Queries CCXT for maker/taker fees per
exchange, caches results, supports manual overrides. Computes dynamic
TP/SL targets and breakeven win rates. Blocks entry when fees make the
math impossible. The primary reason scalping requires exchange-specific
routing.

### STRATEGY_EXCHANGE_MAP
A `settings.py` dict mapping each strategy type to its approved
exchanges. Encodes which exchanges are suitable for each strategy
based on fee structure, liquidity, and market participant type. Single
source of truth for exchange routing. Adding a new exchange = one line
in `settings.py`.

### Micro Price Tracker
A background loop in `ScalpingAgent` that backfills `price_30s`,
`price_1m`, `price_3m`, `price_5m` on each scalp observation after
those intervals elapse. The scalp equivalent of the main bot's
`future_price_tracker_loop`. Enables retrospective analysis of OFI
directional accuracy independently of whether the position was held or
exited early.
