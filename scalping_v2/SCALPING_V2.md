# Scalping Agent v2 — Integration Guide

*Companion to scalping_agent_doc.docx. Adds selectivity gates and ATR-aware SL to maximise consistency.*

---

## What v2 adds

Six new components on top of the existing 13-gate flow:

1. **Tighter z-entry threshold** (1.5 → 2.0)
2. **Longer persistence requirement** (3 → 5 ticks)
3. **Confluence gates** — VWAP alignment, HTF (5m) trend, volume — require 2 of 3
4. **Cross-exchange OFI confirmation** — block when other venues disagree, label STRONG when they confirm
5. **BTC directional gate** — alts can't fight BTC's order flow direction
6. **Adverse selection guard** — skip if mid moved against signal in last 100ms
7. **Depth adequacy** — skip if our position would consume too much top-level liquidity
8. **ATR-aware stop loss** — SL scales with realised volatility, clamped to floor/ceiling
9. **Tighter activation criteria** — 300 obs, 55% WR, 0.5 bps avg net, 25% max-hold, 57% directional acc

Everything is additive and individually togglable via `SCALP_USE_*` flags.

---

## Files in this package

```
scalping_v2/
├── settings_scalp_v2.py            # New SCALP_* constants
├── scalping_confluence.py          # ConfluenceChecker + 7 gate methods
├── scalping_atr_sl.py              # ATRStopCalculator
├── scalping_agent_v2_integration.py # New _evaluate_signal_v2() + activation check + DB migration
├── test_scalping_v2.py             # 41 unit tests (all passing)
└── demo_simulate_v1_vs_v2.py       # End-to-end simulation, v1 vs v2 comparison
```

---

## Integration steps

### Step 1 — Append v2 settings to config/settings.py

Copy the constants from `settings_scalp_v2.py` into `config/settings.py`. All toggles default ON. To A/B test individual features, flip the `SCALP_USE_*` flags.

### Step 2 — Add the new modules to agents/

```bash
cp scalping_v2/scalping_confluence.py agents/
cp scalping_v2/scalping_atr_sl.py agents/
```

### Step 3 — Wire into ScalpingAgent

In `agents/scalping_agent.py`, in `ScalpingAgent.__init__`, after the existing OFIEngine and FeeManager are constructed:

```python
from agents.scalping_confluence import ConfluenceChecker
from agents.scalping_atr_sl import ATRStopCalculator
import config.settings as settings

self.confluence = ConfluenceChecker(self.market_data, self.ofi_engine, settings)
self.atr_calc = ATRStopCalculator(self.market_data, settings)
```

Then in `_evaluate_signal()`, after gate 13 (BTC 1m change) passes, replace the FeeManager call with the v2 flow shown in `scalping_agent_v2_integration.py::evaluate_signal_v2()`. The structure is:

```python
# ... existing gates 1-13 ...

# === V2 selectivity layer ===
result = self.confluence.run_all_gates(
    symbol=signal.symbol, exchange=signal.exchange,
    direction=signal.direction, primary_z=signal.ofi_z,
    position_size_usd=self._position_size_usd(signal),
)
obs.update(self._unpack_confluence(result))  # see integration module for unpacking
if not result.passed:
    obs["would_entry"] = False
    obs["skip_reason"] = f"V2:{result.blocking_reason}"
    return obs

# === ATR-aware TP/SL (replaces fee_manager.compute_tp_sl) ===
tpsl = self.atr_calc.compute_tp_sl_v2(
    symbol=signal.symbol, exchange=signal.exchange,
    round_trip_bps=self.fee_manager.get_round_trip_bps(signal.exchange),
)
obs.update({
    "tp_bps": tpsl.tp_bps, "sl_bps": tpsl.sl_bps,
    "atr_bps": tpsl.atr_bps, "atr_adjusted": tpsl.atr_adjusted,
    "sl_clamped": tpsl.sl_clamped, "rr_actual": tpsl.rr_actual,
    "strength": result.strength_label,
})

# Continue with existing position lifecycle / simulation code
```

### Step 4 — Add v2 database columns

Apply `DB_COLUMNS_V2` from the integration module. All columns are nullable so old observations remain valid:

```python
# In database/models.py, add to ScalpObservationModel:
confluence_score = Column(Integer, nullable=True)
strength_label = Column(String, nullable=True)
cross_exchange_agrees = Column(Boolean, nullable=True)
btc_compatible = Column(Boolean, nullable=True)
adverse_selection_ok = Column(Boolean, nullable=True)
depth_ok = Column(Boolean, nullable=True)
vwap_aligned = Column(Boolean, nullable=True)
htf_aligned = Column(Boolean, nullable=True)
volume_adequate = Column(Boolean, nullable=True)
atr_bps = Column(Float, nullable=True)
atr_adjusted = Column(Boolean, nullable=True)
sl_clamped = Column(String, nullable=True)
rr_actual = Column(Float, nullable=True)
```

Run the migration or let SQLAlchemy auto-create on next startup.

### Step 5 — Implement the market_data methods if any are missing

The ConfluenceChecker assumes `MarketData` exposes:
- `get_session_vwap(symbol, exchange)` — running VWAP since UTC midnight
- `get_ema(symbol, exchange, timeframe, period)` — fast EMA computation
- `get_current_minute_volume(symbol, exchange)` — current 1m candle volume
- `get_rolling_median_volume(symbol, exchange, timeframe, lookback)` — median over N candles
- `get_mid_price_at_offset(symbol, exchange, offset_ms)` — historical mid lookup
- `get_atr(symbol, exchange, period, timeframe)` — Average True Range

Most of these likely already exist (the project research notes the indicator engine covers RSI/MACD/BB/EMA/ATR/ADX/VWAP). The two that need adding are `get_mid_price_at_offset` (a ring buffer of recent mids, 1-second resolution is fine) and `get_rolling_median_volume` (trivial extension of existing volume tracking).

### Step 6 — Verify in observation mode

Restart the bot, watch the scalp dashboard panel. You should see:
- Reduced "would-entry" count compared to v1 (expected — v2 is more selective)
- New skip reasons in the format `V2:adverse`, `V2:confluence`, `V2:btc_directional`
- Per-observation strength labels: VERY_STRONG / STRONG / MODERATE

Run for 48 hours, then query:

```sql
SELECT strength_label, COUNT(*) as n,
       AVG(CASE WHEN pnl_bps > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
       AVG(pnl_bps - round_trip_cost_bps) as avg_net_bps
FROM scalp_observations
WHERE would_entry = 1 AND exit_price > 0 AND strength_label IS NOT NULL
GROUP BY strength_label
ORDER BY win_rate DESC;
```

If VERY_STRONG and STRONG show meaningfully better win rates than MODERATE, the confluence labelling is working. If they don't — the assumed effects of the gates aren't holding in your data, and you should recalibrate.

---

## What the simulation says (illustrative, not predictive)

Running 20,000 synthetic signals through both v1 and v2:

| Metric | v1 | v2 | Δ |
|--------|----|----|----|
| Trades entered | 20,000 | 5,000 | -75% |
| Win rate | 58.13% | **66.84%** | **+8.71pp** |
| Expectancy (bps/trade) | 0.959 | **1.383** | **+44%** |
| 5-loss streak probability | 1.29% | **0.40%** | **3x safer** |
| Per-strength WR (v2) | — | VERY_STRONG 69%, STRONG 67%, MODERATE 64% | — |

**The trade-off this represents:** v2 takes 75% fewer trades. Per-1k-signals total profit *drops* — because there are fewer total trades multiplying a higher per-trade win.

**This is the trade-off you asked for.** You said: "looking for consistency, higher win rate rather than higher individual profit." That's exactly what v2 delivers. The losing-streak probability dropping 3× is the consistency you wanted — when you're trading $100 of real money, the difference between a 1.3% chance of 5 losses in a row and a 0.4% chance is the difference between hitting the daily circuit breaker once a month vs once a quarter.

Important caveats on the simulation:
- The assumed conditional accuracies (60% for z≥2.0, +3pp per confluence dimension, etc.) are reasonable approximations of the literature but **not calibrated to your specific signal**
- The simulation is generating idealised signals; real OFI in noisy markets will be messier
- Cross-exchange disagreement and adverse selection are modelled as independent draws, but in reality they correlate

Once you have 200+ real observations, recalibrate the simulation against your actual `scalp_observations` table to predict what live performance will look like.

---

## Tuning knobs (in priority order)

If after a week of observation the v2 trade count is too low (< 5 trades/day on MEXC during session hours), relax in this order:

1. **`SCALP_CONFLUENCE_REQUIRED`** from 2 to 1 — admits MODERATE-strength signals
2. **`SCALP_OFI_Z_ENTRY`** from 2.0 back to 1.8 — admits the upper-middle band
3. **`SCALP_BTC_OFI_NEUTRAL_BAND`** from 0.5 to 0.8 — fewer alt blocks
4. **Lift `SCALP_VOLUME_THRESHOLD_RATIO`** from 1.0 to 0.8 — admit lower-volume conditions

Do not relax adverse selection or depth checks — those are pure risk reducers and shouldn't be loosened.

If trade count is fine but win rate isn't moving above 55%, that's a stronger signal: either the OFI signal is weaker than expected in your operating conditions, or the confluence gates aren't picking up what they should. In that case, query the database to see which gates are blocking the trades that *did* win — if VWAP-aligned trades aren't winning, the VWAP gate isn't helping and you can disable it.

---

## Components NOT in this v2 drop (deferred to v3)

The Tier 2/3/4 items from the research recommendation that aren't built yet:

- **Per-symbol learning gates** — needs running statistics infrastructure
- **Time-of-day learning gates** — same
- **Drawdown-aware position sizing** — needs position-sizing refactor
- **Continuous edge monitor with auto-halt** — needs background task + alert wiring
- **Maker-passive entry mode** — needs execution router changes
- **Hawkes process price modelling** — research-grade addition, large scope

These can be built incrementally. The v2 package is the highest-leverage subset that doesn't require new infrastructure beyond the database columns.

---

## Test status

41 unit tests, all passing:

```
test_scalping_v2.py::TestVWAP                     5 tests ✓
test_scalping_v2.py::TestHTF                      3 tests ✓
test_scalping_v2.py::TestVolume                   3 tests ✓
test_scalping_v2.py::TestCrossExchange            4 tests ✓
test_scalping_v2.py::TestBTCDirectional           5 tests ✓
test_scalping_v2.py::TestAdverseSelection         4 tests ✓
test_scalping_v2.py::TestDepth                    3 tests ✓
test_scalping_v2.py::TestCombined                 4 tests ✓
test_scalping_v2.py::TestATRStopLoss              6 tests ✓
test_scalping_v2.py::TestActivationReadiness      4 tests ✓
```

Run with: `cd scalping_v2/ && python -m pytest test_scalping_v2.py -v`

Run the v1-vs-v2 simulation: `cd scalping_v2/ && python demo_simulate_v1_vs_v2.py`
