# Scalping v2 — Recalibration Cookbook

*How to tune the v2 selectivity layer against your actual observations once data starts flowing.*

---

## Why this exists

The v2 design is rooted in published microstructure research (Cont 2014, Brogaard 2014). The `demo_simulate_v1_vs_v2.py` shows a +8.7pp win rate uplift under reasonable assumptions about how each gate correlates with directional accuracy. But that simulation uses synthetic data with assumed conditional accuracies — not your specific OFI implementation against your specific pairs on MEXC.

Once you have 200+ real observations, the conditional accuracies in your live data may differ from the simulation. Some gates may be doing more work than expected; others may be filtering out trades that would have won.

This cookbook is the toolkit for finding out which is which.

**Workflow:** run the queries weekly, look for the patterns described, adjust one parameter at a time, observe for another week.

---

## Sample size guidance

| Sample size | What you can learn |
|-------------|--------------------|
| < 50 entered trades | Nothing meaningful — variance dominates |
| 50–150 entered | Direction-only sanity check (is v2 better or worse than v1?) |
| 150–300 entered | Per-strength-label stratification |
| 300+ entered | Per-gate effectiveness, per-symbol, per-hour |
| 500+ entered | Parameter sensitivity, confident tuning |
| 1000+ entered | Per-regime, per-time-of-day fine tuning |

Don't tune parameters with fewer than 150 trades — you'll be chasing noise.

---

## Query 1 — Strength label validation (run first)

**Question:** Is the strength labelling actually stratifying outcomes? VERY_STRONG should win more often than STRONG, which should win more often than MODERATE.

```sql
SELECT
    strength_label,
    COUNT(*) AS n_trades,
    ROUND(AVG(CASE WHEN pnl_bps > 0 THEN 1.0 ELSE 0.0 END), 3) AS win_rate,
    ROUND(AVG(pnl_bps), 2) AS avg_gross_bps,
    ROUND(AVG(pnl_bps - round_trip_cost_bps), 2) AS avg_net_bps,
    ROUND(AVG(hold_sec), 1) AS avg_hold_sec
FROM scalp_observations
WHERE would_entry = 1
  AND exit_price > 0
  AND strength_label IS NOT NULL
GROUP BY strength_label
ORDER BY 
    CASE strength_label
        WHEN 'VERY_STRONG' THEN 1
        WHEN 'STRONG' THEN 2
        WHEN 'MODERATE' THEN 3
        WHEN 'WEAK' THEN 4
    END;
```

**Interpretation:**

| Pattern | Diagnosis | Action |
|---------|-----------|--------|
| VERY_STRONG > STRONG > MODERATE win rates (monotonic) | Labelling works | No action — trust the strength label |
| Win rates roughly equal across labels | Labelling adds no information | Investigate which sub-gates matter — query 2 |
| MODERATE wins more than STRONG | Labelling is inverted or wrong | Check that confluence_score is incrementing correctly |
| Sample < 30 per label | Insufficient data | Wait for more observations |

If the labelling works, you can later drive position sizing off it: bigger size on VERY_STRONG, smaller on MODERATE. That's a v3 item.

---

## Query 2 — Individual confluence gate effectiveness

**Question:** Of the soft gates (VWAP / HTF / Volume), which actually correlate with win rate?

```sql
-- VWAP gate effectiveness
SELECT
    vwap_aligned,
    COUNT(*) AS n,
    ROUND(AVG(CASE WHEN pnl_bps > 0 THEN 1.0 ELSE 0.0 END), 3) AS win_rate,
    ROUND(AVG(pnl_bps - round_trip_cost_bps), 2) AS avg_net_bps
FROM scalp_observations
WHERE would_entry = 1 AND exit_price > 0 AND vwap_aligned IS NOT NULL
GROUP BY vwap_aligned;

-- HTF trend gate effectiveness
SELECT
    htf_aligned,
    COUNT(*) AS n,
    ROUND(AVG(CASE WHEN pnl_bps > 0 THEN 1.0 ELSE 0.0 END), 3) AS win_rate,
    ROUND(AVG(pnl_bps - round_trip_cost_bps), 2) AS avg_net_bps
FROM scalp_observations
WHERE would_entry = 1 AND exit_price > 0 AND htf_aligned IS NOT NULL
GROUP BY htf_aligned;

-- Volume gate effectiveness
SELECT
    volume_adequate,
    COUNT(*) AS n,
    ROUND(AVG(CASE WHEN pnl_bps > 0 THEN 1.0 ELSE 0.0 END), 3) AS win_rate,
    ROUND(AVG(pnl_bps - round_trip_cost_bps), 2) AS avg_net_bps
FROM scalp_observations
WHERE would_entry = 1 AND exit_price > 0 AND volume_adequate IS NOT NULL
GROUP BY volume_adequate;
```

**Interpretation:**

For each gate, compute `win_rate(aligned=true) - win_rate(aligned=false)`. This is the gate's "lift" — the win-rate improvement when the gate passes vs when it doesn't (yet the trade entered anyway because 2-of-3 was satisfied).

| Lift | Diagnosis | Action |
|------|-----------|--------|
| +3pp or more | Gate is doing real work | Keep enabled |
| +1pp to +3pp | Gate is marginal | Keep enabled — costs little |
| -1pp to +1pp | Gate is noise | Consider disabling (`SCALP_USE_X_GATE = False`) |
| Negative lift | Gate is *counterproductive* | Disable, investigate why |

A counterproductive gate is rare but possible — for example, if your HTF EMA periods are tuned for wrong market conditions, the gate might be filtering out the right trades. Don't just disable; investigate.

---

## Query 3 — Hard gate effectiveness via micro tracker

**Question:** For trades that were *skipped* by hard gates (adverse, BTC, cross-exchange, depth), what would have happened if they'd been taken? The micro price tracker gives the answer.

```sql
-- Adverse selection: trades blocked, were they actually adverse?
SELECT
    'ADVERSE_BLOCK' AS gate,
    direction,
    COUNT(*) AS n_blocked,
    -- Did price move in signal direction over 1 minute?
    ROUND(AVG(CASE 
        WHEN direction = 'LONG' AND price_1m > entry_price THEN 1.0
        WHEN direction = 'SHORT' AND price_1m < entry_price THEN 1.0
        WHEN price_1m IS NULL THEN NULL
        ELSE 0.0
    END), 3) AS would_have_won_1m,
    ROUND(AVG(CASE 
        WHEN direction = 'LONG' AND price_3m > entry_price THEN 1.0
        WHEN direction = 'SHORT' AND price_3m < entry_price THEN 1.0
        WHEN price_3m IS NULL THEN NULL
        ELSE 0.0
    END), 3) AS would_have_won_3m
FROM scalp_observations
WHERE would_entry = 0
  AND skip_reason LIKE 'V2:%dropped%' OR skip_reason LIKE 'V2:%rose%'
  AND price_1m IS NOT NULL
GROUP BY direction;

-- BTC directional gate: was the BTC misalignment actually predictive?
SELECT
    'BTC_BLOCK' AS gate,
    direction,
    COUNT(*) AS n_blocked,
    ROUND(AVG(CASE 
        WHEN direction = 'LONG' AND price_1m > entry_price THEN 1.0
        WHEN direction = 'SHORT' AND price_1m < entry_price THEN 1.0
        WHEN price_1m IS NULL THEN NULL
        ELSE 0.0
    END), 3) AS would_have_won_1m
FROM scalp_observations
WHERE would_entry = 0
  AND skip_reason LIKE '%BTC%'
  AND price_1m IS NOT NULL
GROUP BY direction;

-- Cross-exchange disagreement: was the other venue actually predictive?
SELECT
    'CROSS_EX_BLOCK' AS gate,
    direction,
    COUNT(*) AS n_blocked,
    ROUND(AVG(CASE 
        WHEN direction = 'LONG' AND price_1m > entry_price THEN 1.0
        WHEN direction = 'SHORT' AND price_1m < entry_price THEN 1.0
        WHEN price_1m IS NULL THEN NULL
        ELSE 0.0
    END), 3) AS would_have_won_1m
FROM scalp_observations
WHERE would_entry = 0
  AND skip_reason LIKE '%opposing%' OR skip_reason LIKE '%venue%'
  AND price_1m IS NOT NULL
GROUP BY direction;
```

**Note on `entry_price` for skipped trades.** Currently the schema only populates `entry_price` for trades that entered. For this analysis to work, the agent should log the mid price at the time of evaluation into `entry_price` for *all* observations (entered or skipped). If that's not happening, add it to the integration code — it's a one-line change and unlocks all skip-reason analysis.

**Interpretation:**

The right pattern is: hard gates should block trades whose `would_have_won_1m` is *below* 50%. If a gate blocks trades that would have won >55% of the time, it's over-aggressive.

| Gate's blocked-trade win rate | Diagnosis | Action |
|------|-----------|--------|
| < 45% | Gate working well — skips were correct | Keep current threshold |
| 45–55% | Gate neutral — skips were random | Consider relaxing threshold by 20% |
| > 55% | Gate counterproductive — skips would have won | Tighten threshold OR disable |

---

## Query 4 — Skip-reason breakdown

**Question:** Which gates are doing most of the work? If one gate accounts for 60% of all skips, that's where to focus tuning.

```sql
SELECT
    -- Bucket skip reasons into v2 categories
    CASE
        WHEN skip_reason LIKE 'V2:z<%' THEN 'V2:z_too_weak'
        WHEN skip_reason LIKE '%confluence%' THEN 'V2:confluence'
        WHEN skip_reason LIKE '%dropped%' OR skip_reason LIKE '%rose%' THEN 'V2:adverse'
        WHEN skip_reason LIKE '%BTC%' THEN 'V2:btc_directional'
        WHEN skip_reason LIKE '%opposing%' OR skip_reason LIKE '%venue%' THEN 'V2:cross_exchange'
        WHEN skip_reason LIKE '%top5%' OR skip_reason LIKE '%consume%' THEN 'V2:depth'
        WHEN skip_reason LIKE 'V2:%' THEN 'V2:other'
        WHEN skip_reason != '' THEN 'V1_legacy'
        ELSE 'no_skip'
    END AS reason_bucket,
    COUNT(*) AS n,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM scalp_observations
WHERE timestamp > strftime('%s', 'now', '-7 days')
GROUP BY reason_bucket
ORDER BY n DESC;
```

**Healthy distribution (rough guide):**
- V1_legacy: 30–50% (regime, session, spread, news guard — these were already working)
- V2:z_too_weak: 20–35% (most signals are below 2.0)
- V2:confluence: 10–20%
- V2:adverse + V2:depth + V2:btc + V2:cross_exchange: 5–15% each
- no_skip (entered): 2–10%

If V2:z_too_weak is over 50%, your raised threshold is too aggressive — drop to 1.8.
If V2:confluence is over 30%, the 2-of-3 requirement is too strict — drop to 1.
If V2:adverse is over 20%, either the market is unusually choppy or the 100ms window is too sensitive — bump to 200ms.

---

## Query 5 — Per-symbol stratification

**Question:** Is v2's edge concentrated in certain pairs? If yes, restrict trading to those.

```sql
SELECT
    symbol,
    COUNT(*) AS n_trades,
    ROUND(AVG(CASE WHEN pnl_bps > 0 THEN 1.0 ELSE 0.0 END), 3) AS win_rate,
    ROUND(AVG(pnl_bps - round_trip_cost_bps), 2) AS avg_net_bps,
    ROUND(SUM(pnl_bps - round_trip_cost_bps), 1) AS total_net_bps,
    -- Most common exit reason
    (SELECT exit_reason FROM scalp_observations s2
     WHERE s2.symbol = s1.symbol AND s2.would_entry = 1 AND s2.exit_reason != ''
     GROUP BY exit_reason ORDER BY COUNT(*) DESC LIMIT 1) AS top_exit_reason
FROM scalp_observations s1
WHERE would_entry = 1 AND exit_price > 0
GROUP BY symbol
HAVING n_trades >= 20
ORDER BY avg_net_bps DESC;
```

**Action:** Symbols with `avg_net_bps < 0` after 50+ trades should be removed from `SCALP_PAIRS` or added to a per-symbol blocklist. Symbols with `top_exit_reason = MAX_HOLD` are time-decaying — the signal isn't reaching TP, suggesting OFI on that pair isn't predictive of short moves.

---

## Query 6 — Per-hour stratification

**Question:** Some hours of the session window may be much more profitable than others.

```sql
SELECT
    CAST(strftime('%H', datetime(timestamp, 'unixepoch')) AS INTEGER) AS hour_utc,
    COUNT(*) AS n_trades,
    ROUND(AVG(CASE WHEN pnl_bps > 0 THEN 1.0 ELSE 0.0 END), 3) AS win_rate,
    ROUND(AVG(pnl_bps - round_trip_cost_bps), 2) AS avg_net_bps
FROM scalp_observations
WHERE would_entry = 1 AND exit_price > 0
GROUP BY hour_utc
HAVING n_trades >= 15
ORDER BY hour_utc;
```

**Action:** If win rate at hour X is consistently below 50% over 50+ trades, narrow `SCALP_SESSION_START_UTC` / `SCALP_SESSION_END_UTC` to exclude it. Conversely, if win rate is highest 13:00–15:00 UTC, consider tightening the session window further.

---

## Query 7 — ATR-aware SL effectiveness

**Question:** Is the ATR adjustment actually preventing noise stop-outs? Compare exit reasons by `atr_adjusted` and `sl_clamped`.

```sql
SELECT
    atr_adjusted,
    sl_clamped,
    exit_reason,
    COUNT(*) AS n,
    ROUND(AVG(pnl_bps), 2) AS avg_pnl_bps,
    ROUND(AVG(hold_sec), 1) AS avg_hold_sec
FROM scalp_observations
WHERE would_entry = 1 AND exit_price > 0
GROUP BY atr_adjusted, sl_clamped, exit_reason
ORDER BY n DESC;
```

**Interpretation:**

- If `atr_adjusted = TRUE` trades have a meaningfully *lower* SL hit rate than `atr_adjusted = FALSE`, the ATR widening is working
- If `sl_clamped = CEILING` trades dominate, your ATR multiplier (0.3) or ceiling (8 bps) is too low — the market is more volatile than expected
- If `sl_clamped = FLOOR` dominates, you're rarely getting ATR-aware SL benefit because all pairs are calmer than the 1.5 bps floor — consider lowering the floor to 1.0

---

## Query 8 — Activation readiness check

**Question:** Are we ready to flip live?

```sql
WITH stats AS (
    SELECT
        COUNT(*) AS n_closed,
        AVG(CASE WHEN pnl_bps > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
        AVG(pnl_bps - round_trip_cost_bps) AS avg_net_bps,
        AVG(CASE WHEN exit_reason = 'MAX_HOLD' THEN 1.0 ELSE 0.0 END) AS max_hold_pct,
        AVG(CASE 
            WHEN direction = 'LONG' AND price_1m > entry_price THEN 1.0
            WHEN direction = 'SHORT' AND price_1m < entry_price THEN 1.0
            WHEN price_1m IS NULL THEN NULL
            ELSE 0.0
        END) AS dir_acc_1m
    FROM scalp_observations
    WHERE would_entry = 1 AND exit_price > 0
)
SELECT
    n_closed,
    ROUND(win_rate, 3) AS win_rate,
    ROUND(avg_net_bps, 2) AS avg_net_bps,
    ROUND(max_hold_pct, 3) AS max_hold_pct,
    ROUND(dir_acc_1m, 3) AS dir_acc_1m,
    -- Each criterion
    CASE WHEN n_closed >= 300 THEN '✓' ELSE '✗' END AS check_n,
    CASE WHEN win_rate >= 0.55 THEN '✓' ELSE '✗' END AS check_wr,
    CASE WHEN avg_net_bps >= 0.5 THEN '✓' ELSE '✗' END AS check_net,
    CASE WHEN max_hold_pct <= 0.25 THEN '✓' ELSE '✗' END AS check_hold,
    CASE WHEN dir_acc_1m >= 0.57 THEN '✓' ELSE '✗' END AS check_dir,
    -- Overall
    CASE WHEN n_closed >= 300 AND win_rate >= 0.55 AND avg_net_bps >= 0.5 
              AND max_hold_pct <= 0.25 AND dir_acc_1m >= 0.57 
         THEN 'READY' ELSE 'NOT_READY' END AS verdict
FROM stats;
```

If verdict is READY for 7 consecutive days, the data confirms edge and you can consider activating live. **Do not activate on a single READY reading** — the variance on small samples is too high. The 7-day requirement filters out lucky weeks.

---

## Query 9 — Drawdown and streak analysis

**Question:** What does the equity curve actually look like? Daily P&L, max consecutive losses, worst-day loss.

```sql
WITH daily AS (
    SELECT
        DATE(datetime(timestamp, 'unixepoch')) AS day,
        COUNT(*) AS trades,
        SUM(CASE WHEN pnl_bps > 0 THEN 1 ELSE 0 END) AS wins,
        SUM(pnl_bps - round_trip_cost_bps) AS net_bps,
        SUM(pnl_usd) AS net_usd
    FROM scalp_observations
    WHERE would_entry = 1 AND exit_price > 0
    GROUP BY day
)
SELECT
    day,
    trades,
    wins,
    ROUND(100.0 * wins / NULLIF(trades, 0), 1) AS win_pct,
    ROUND(net_bps, 1) AS net_bps,
    ROUND(net_usd, 2) AS net_usd
FROM daily
ORDER BY day DESC
LIMIT 30;
```

**What to watch:**
- Days where `net_usd < -$3` are approaching the $5 daily-loss circuit breaker
- More than 2 consecutive losing days warrants review
- Trade count per day below 3 means v2 is too selective for current conditions

---

## Decision tree — what to change based on findings

After running queries 1–9 against 200+ observations, follow this tree:

```
START
│
├─ Q8 verdict = READY for 7 consecutive days?
│   └─ YES → Consider activating live with $25-50 first, not full $100
│
├─ Q1 shows VERY_STRONG win rate > MODERATE by 5+ pp?
│   ├─ YES → Strength labelling works → start using it for position sizing in v3
│   └─ NO → Investigate Q2: which sub-gates are doing real work?
│
├─ Q4 shows V2:z_too_weak > 50%?
│   └─ YES → Lower SCALP_OFI_Z_ENTRY from 2.0 to 1.8
│
├─ Q4 shows V2:confluence > 30%?
│   └─ YES → Lower SCALP_CONFLUENCE_REQUIRED from 2 to 1
│            OR disable the weakest gate found in Q2
│
├─ Q3 shows any gate's blocked-trade win rate > 55%?
│   └─ YES → That gate is over-aggressive
│            Identify which threshold to relax — see specific table in Q3
│
├─ Q5 shows any symbol with negative avg_net_bps over 50+ trades?
│   └─ YES → Remove from SCALP_PAIRS
│
├─ Q6 shows any hour with win rate < 50% over 50+ trades?
│   └─ YES → Narrow session window to exclude it
│
├─ Q7 shows sl_clamped = CEILING dominates?
│   └─ YES → Raise SCALP_ATR_SL_CEILING_BPS from 8 to 10 or 12
│            (or lower SCALP_ATR_SL_MULTIPLIER from 0.3 to 0.25)
│
└─ Q9 shows daily trade count < 3?
    └─ YES → Either market is unusually quiet (wait) OR v2 too selective (relax)
```

---

## Schedule

| Cadence | What to do |
|---------|------------|
| Daily | Glance at Q9 (daily P&L) — alert if approaching circuit breakers |
| Weekly | Run Q1, Q4, Q8 — direction-check and skip distribution |
| Bi-weekly | Run Q2, Q3 — gate effectiveness |
| Monthly | Run Q5, Q6, Q7 — per-symbol, per-hour, ATR analysis |
| Before any parameter change | Run all queries — confirm the change is justified by data, not noise |

---

## Important — change one parameter at a time

The single biggest mistake in tuning a multi-parameter system is changing several knobs at once. If you lower `SCALP_OFI_Z_ENTRY` AND `SCALP_CONFLUENCE_REQUIRED` AND widen the session window simultaneously, and win rate drops, you won't know which change caused it.

**Procedure:**
1. Run the queries, identify the single highest-impact change
2. Make that change, restart the agent
3. Wait 50+ trades
4. Re-run the queries
5. If improved, keep the change and consider the next one
6. If worse, revert and try a different change

This is slow on purpose. Scalping edges are small; tuning that respects that is the only kind that works.

---

## When the queries lie

A few patterns where the data is misleading:

**Survivorship bias on skipped trades.** Q3 measures price movement for skipped trades, but only those with populated `entry_price`. If your agent only logs `entry_price` for entered trades (the current default), Q3 won't work until you fix that logging. Audit the agent code if Q3 returns no rows for skipped trades.

**Selection bias in strength labels.** Q1 only shows results for trades that *entered*. A WEAK signal that didn't enter has no row in Q1. If the strength labelling looks great because MODERATE trades won 60%, remember that MODERATE trades that *didn't* enter (because confluence < 2) aren't represented — and they might have won less often.

**Regime contamination.** If your observation period was entirely a bull market, alt-LONG trades will look great in Q5. Don't extrapolate to a bear market. Wait for regime variety.

**News-day distortion.** A single CPI release day can produce 10x normal volatility and skew the whole week's numbers. Either filter out high-volatility days from the queries, or accept that monthly aggregates wash this out.

---

*Update this doc as you discover new query patterns. The cookbook gets more valuable with use.*
