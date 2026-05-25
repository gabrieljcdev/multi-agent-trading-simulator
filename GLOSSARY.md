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

---

## Scalping v2 (selectivity layer)

### ConfluenceChecker
`agents/scalping_confluence.py`. Runs the v2 selectivity gates after the
existing 13-gate flow and returns a `CombinedConfluenceResult` (pass/block,
blocking reason, strength label, per-gate diagnostics). Dependency-injected
over `MarketData` + `OFIEngine`; fails open on missing data. Reference copy in
`scalping_v2/`.

### Confluence Gates
The three soft gates — VWAP alignment, 5m HTF trend, volume — of which
`SCALP_CONFLUENCE_REQUIRED` (default 2 of 3) must pass to enter. Distinct from
the v2 hard gates (adverse selection, depth, BTC-directional, cross-exchange),
which block outright.

### Strength Label
Per-observation conviction tag — WEAK / MODERATE / STRONG / VERY_STRONG —
from how many soft gates passed plus cross-exchange agreement. Stored in
`scalp_observations.strength_label`; intended to drive position sizing in v3.

### Cross-Exchange OFI Confirmation
v2 gate: blocks a signal when another venue's OFI strongly opposes it, and
labels it stronger when another venue confirms. Reads `OFIEngine.get_z_score`
across venues.

### BTC Directional Gate
v2 gate for alts: blocks an alt scalp that fights BTC's order-flow direction
(BTC OFI z beyond `SCALP_BTC_OFI_NEUTRAL_BAND`). BTC itself is exempt.

### Adverse Selection Guard
v2 gate: skips when the mid moved against the signal within
`SCALP_ADVERSE_MOVE_WINDOW_MS` (default 100 ms) by more than
`SCALP_ADVERSE_MID_MOVE_BPS` — avoids supplying liquidity into an adverse move.

### Depth Adequacy Gate
v2 gate: skips when top-5 book depth is below
`SCALP_MIN_TOP5_DEPTH_MULTIPLIER` × position size, or the position would consume
more than `SCALP_MAX_TOP1_CONSUME_PCT` of the top level.

### ATRStopCalculator
`agents/scalping_atr_sl.py`. v2 replacement for FeeManager's fixed-bps SL:
SL = ATR(period) × `SCALP_ATR_SL_MULTIPLIER`, clamped to
[`SCALP_ATR_SL_FLOOR_BPS`, `SCALP_ATR_SL_CEILING_BPS`], never tighter than the
fee-derived base. Returns `atr_bps` / `atr_adjusted` / `sl_clamped` / `rr_actual`
for logging. When ATR data is unavailable it returns the base SL (so it equals
FeeManager).

### Activation Readiness v2
The tighter observation→live gate (`queries.get_scalp_activation_readiness_v2`):
≥300 closed obs, ≥55% win rate, ≥0.5 avg net bps, ≤25% MAX_HOLD exits, ≥57% 1m
directional accuracy (`SCALP_*_FOR_LIVE_V2`).
