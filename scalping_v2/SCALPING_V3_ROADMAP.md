# Scalping v3 — Roadmap

*What we deliberately did NOT build into v2, why we deferred it, and the conditions under which each item becomes worth building.*

---

## Why a roadmap doc

Two reasons:

1. **Discipline.** v2 was scoped to a coherent unit that doesn't require new infrastructure beyond database columns. Discipline about scope is how systems stay shippable. The temptation to "while we're here, let's also add X" is real and dangerous.

2. **Memory.** In 6 months when v2 observations have accumulated and v2 tuning is well-understood, future-you will want to know what was deliberately deferred (so it can now be built) vs what was forgotten (so you don't re-derive it from scratch). This doc is that memory.

Each item below has: **what it is, why it was deferred, what data triggers it being worth building, and a rough scope estimate.**

---

## v3 candidates, in roughly priority order

### V3-1. Per-symbol learning gate

**What:** A rolling 50-trade win rate computed per (symbol, exchange) pair, with automatic suspension of pairs whose running win rate drops below 45%. After 24 hours of suspension, re-enable with reduced position size and a 30-trade probation window.

**Why deferred:** Needs running-statistics infrastructure and a per-pair state machine. Neither is trivial to bolt on. v2's per-symbol queries (Recalibration Query 5) let you do this manually for now.

**Trigger condition:** When recalibration Query 5 shows >3 pairs with negative `avg_net_bps` over 50+ trades and you find yourself manually editing `SCALP_PAIRS` more than once a month. At that point automation pays for itself.

**Scope:** ~150 lines for the state machine, ~50 lines for the suspended-pairs table, ~30 lines for the integration into `_evaluate_signal` as gate 14 (before v2 confluence runs).

**Dependency:** None new. Uses existing observation data.

---

### V3-2. Time-of-day learning gate

**What:** Same idea as V3-1 but per (hour, day-of-week). Automatically narrows the session window based on observed win rates rather than fixed `SCALP_SESSION_START_UTC` / `_END_UTC`.

**Why deferred:** Same statistical infrastructure as V3-1. Both items want to be built together — they share the running-statistics framework. Build the framework once, mount two analyses on it.

**Trigger condition:** Recalibration Query 6 consistently shows a 10pp+ win rate spread across hours, AND the highest/lowest hours don't match your session window. If your fixed window is already capturing the best hours, automation adds little.

**Scope:** ~100 lines for hourly state, ~30 lines for integration. Pairs naturally with V3-1.

---

### V3-3. Drawdown-aware position sizing (anti-martingale)

**What:** After 2 consecutive losses, halve position size for the next 5 trades. Reset to full size on a winner. Smooths the equity curve and reduces variance during regime changes without changing total expectancy materially.

**Why deferred:** Position sizing isn't currently a first-class concept in the scalping agent (positions are fixed at `SCALP_CAPITAL * SCALP_POSITION_PCT`). Adding dynamic sizing requires touching the position calculator and the order placement code.

**Trigger condition:** Recalibration Query 9 shows max drawdown periods that trigger the daily-loss circuit breaker more than once per month. If circuit breakers rarely fire, you don't need anti-martingale.

**Scope:** ~80 lines for the sizing state machine, ~40 lines for integration into order placement, ~20 lines for dashboard surfacing.

**Important:** This is *anti*-martingale (size down on losses, up on wins). Do not implement the inverse (size up on losses, "averaging down") under any circumstances — it's the most reliable account-blower in trading history.

---

### V3-4. Continuous edge monitor with auto-halt

**What:** A background task that computes — over rolling 50-trade and 200-trade windows — win rate, average net bps, expectancy, and 1-minute directional accuracy. If any metric drops below threshold for 50+ consecutive trades, automatically return the agent to observation mode (set `SCALP_CAPITAL = 0`) and surface a red banner on the dashboard.

**Why deferred:** Needs a separate background task running in the agent's event loop, plus alert routing to the dashboard. Both are doable but non-trivial. Currently the activation check is one-time; this turns it into ongoing.

**Trigger condition:** As soon as v2 goes live (`SCALP_CAPITAL > 0`). This is the structural defence against regime change destroying edge silently. Don't wait until you've experienced a silent drawdown — build this before going live.

**Scope:** ~120 lines for the monitor task, ~40 lines for thresholding and auto-halt logic, ~30 lines for dashboard integration. Should reuse the recalibration queries' SQL.

**Priority promotion:** This is technically v3, but it should probably be built *before* you flip live capital. The cost of building it in advance is small; the cost of going live without it is potentially the whole MEXC scalp fund.

---

### V3-5. Maker-passive entry mode

**What:** Currently the agent crosses the spread on entry (taker). Add a `SCALP_ENTRY_MODE` setting with options `taker` (current) and `maker_passive` (new). In maker_passive mode, place the entry as a limit order at the current best bid (for LONG) / best ask (for SHORT) with a 2-second time-in-force. If unfilled, cancel and re-evaluate on next OFI tick.

**Why deferred:** This isn't just a setting change — it requires changes to the execution router (handling cancel-and-replace logic, partial fills, fill probability tracking). The Hummingbot reference implementation shows this is a substantial engineering effort, not a config flip.

**Trigger condition:** Once v2 is live and stable AND you've verified that the *signal* has edge (win rate > 55% in live conditions). Maker-passive mode trades fill probability for price improvement; you only benefit if the underlying signal is right. Don't switch to maker mode hoping it'll rescue a marginal signal — it won't.

**Scope:** ~250 lines for the router changes, ~100 lines for fill tracking, ~50 lines for observation logging, ~30 lines for tests. This is the largest single item on the roadmap.

**Background:** This is the Avellaneda-Stoikov pattern (`scalping_bots_deep_research.md` Section 2.2). Hummingbot ships a full AS implementation as reference — read their `avellaneda_market_making.py` before designing.

---

### V3-6. Hawkes process price modelling

**What:** Replace the implicit Brownian-motion assumption (which underlies Avellaneda-Stoikov and the linear OFI/price relationship) with a multivariate Hawkes process that models order arrivals as self- and cross-exciting point processes. This is more realistic — it captures the empirical clustering of order flow that Brownian motion ignores.

**Why deferred:** This is research-grade. Implementing it correctly requires fitting Hawkes intensities from live data, validating against held-out periods, and rewriting both the OFI engine and any maker-side quote generation. The expected uplift over the current linear OFI model is modest at best.

**Trigger condition:** Only after V3-5 (maker-passive) is live and you've verified the AS quotes are leaving money on the table by being symmetric in conditions that aren't actually symmetric. In practice this means: 200+ live maker-mode trades, with documented inventory shocks that the AS model didn't anticipate.

**Scope:** Large. ~600+ lines. Realistically a multi-week project.

**Don't build this** unless you have a specific, measured pain point that simpler approaches can't solve. The papers (Cartea et al., arxiv 1903.07222) show theoretical improvements; the literature is sparse on actual live-money improvements over good AS implementations.

---

### V3-7. ML / DRL extensions

**What:** Replace the rule-based gate stack with a learned policy. Three sub-flavours:

- **V3-7a — Feature-augmented threshold tuning.** Use the v2 observation database as training data; learn optimal `SCALP_OFI_Z_ENTRY` and `SCALP_CONFLUENCE_REQUIRED` as functions of regime, volatility, time-of-day. Output is still rule-based but with state-dependent thresholds.

- **V3-7b — Deep order book prediction (DeepLOB).** Train a CNN-LSTM on raw order book snapshots to predict 2-second price direction. Use as an alternative or confirming signal alongside OFI z-score. Globe Research achieved 71% walk-forward accuracy on Coinbase BTC.

- **V3-7c — End-to-end DRL agent.** Replace the entire gate stack with a learned policy that maps order book state to entry/exit decisions. Trained with PPO or similar on observation data, then fine-tuned in a paper-trading sim.

**Why deferred:** All three have substantial dependencies (training infrastructure, walk-forward validation pipeline, inference latency budget). V3-7a is the most pragmatic — it improves v2 rather than replacing it.

**Trigger condition:**
- V3-7a: 1000+ entered observations with the v2 columns populated
- V3-7b: After V3-7a is shipped — needs an existing ML pipeline
- V3-7c: Don't. The published evidence (Guo et al. 2023, Falces Marin et al. 2022) shows DRL beats AS *in simulation*. Live results are sparse. Build only as research, not as primary production strategy.

**Scope:**
- V3-7a: ~300 lines + offline training notebook
- V3-7b: ~500 lines + DeepLOB-equivalent training code (huggingface has reference implementations)
- V3-7c: 1500+ lines, multi-month project, high risk

**Reference:** `scalping_bots_deep_research.md` Section 8 covers the ML literature.

---

### V3-8. Funding-rate harvesting strategy

**What:** A second strategy track within the scalping agent's fund — separate from OFI scalping — that opens short-perpetual / long-spot positions when MEXC funding rate exceeds a threshold, collecting funding payments every 8 hours. The Neutral-Debug/Mexc-Trading-Bot reference (mentioned in the research doc) implements this pattern.

**Why deferred:** Different risk profile, different infrastructure (needs perpetual futures access alongside spot). Conceptually orthogonal to OFI scalping — would be a sibling agent, not a v2 of the existing one.

**Trigger condition:** When the existing OFI scalping reaches stable profitability AND you have spare capacity in the MEXC scalp fund AND funding rates have shown consistent positive expectancy over a 30-day measurement window.

**Scope:** A new agent (~400 lines) plus shared infrastructure for fund accounting. Not actually a "v3 of scalping" — more like "v1 of a sibling strategy."

---

### V3-9. Cross-impact alt scalping

**What:** Use BTC's OFI as a *positive* leading signal for alt scalps, not just a *blocker*. The current v2 BTC directional gate uses BTC OFI to filter out alt signals fighting BTC; V3-9 would also fire alt signals when BTC OFI is strongly directional even if the alt's own OFI is weak. Based on the Quantitative Finance 2023 cross-impact research.

**Why deferred:** Theoretically promising, practically requires careful calibration. Cross-impact is real but the magnitude and timing varies by alt liquidity. Adding it before per-symbol tuning is mature would just dilute the signal.

**Trigger condition:** After V3-1 (per-symbol learning) is shipped and you can confidently say which alts respond to BTC OFI within what time window. Without that data, cross-impact is just adding noise.

**Scope:** ~80 lines for the cross-impact signal generation, ~30 lines for integration as a new entry path.

---

### V3-10. Maker rebate venue support

**What:** Some exchanges (notably the perpetual futures venues) offer *negative* maker fees — they pay you to provide liquidity. This is a structural advantage even larger than MEXC's 0%. Add support for maker-rebate venues with appropriate breakeven recalculation.

**Why deferred:** Requires V3-5 (maker-passive entry mode) to actually capture the rebate. Without maker-passive mode, you're paying taker fees on venues that would have paid you to be a maker.

**Trigger condition:** After V3-5 is shipped and live.

**Scope:** Mostly a `FeeManager` extension (~50 lines) to handle negative fees correctly. The real work is V3-5; this is the venue expansion that makes V3-5 maximally valuable.

---

## Build-order recommendation

If we were building everything in order of impact-to-effort ratio:

1. **V3-4** (continuous edge monitor) — build *before* going live. It's small and critical.
2. **V3-1 + V3-2** (per-symbol and per-hour learning) — share infrastructure, build together. ~200 lines total.
3. **V3-3** (drawdown-aware sizing) — small scope, meaningful variance reduction.
4. **V3-7a** (threshold tuning) — uses existing observation data, no new infrastructure.
5. **V3-5** (maker-passive entry) — largest single item, highest theoretical uplift on win rate or per-trade profit.
6. **V3-10** (maker rebate venues) — pairs with V3-5.
7. **V3-8** (funding rate harvesting) — sibling strategy, not core.
8. **V3-9** (cross-impact alt scalping) — needs V3-1's data first.
9. **V3-7b** (DeepLOB) — research-grade, optional.
10. **V3-6** (Hawkes process) — research-grade, lowest priority.
11. **V3-7c** (full DRL replacement) — do not build as production system.

---

## What is *not* on the roadmap

Things that get asked about but shouldn't be added:

**Stop-loss-free strategies.** Any approach that omits a hard SL ("we just wait for it to come back") is a martingale in disguise. Will work until the day it doesn't, then blow the account.

**Pyramiding into losers.** Adding to losing positions to lower the average entry. Same problem as above.

**Aggressive leverage on scalps.** Scalping with 5-10x leverage compounds tiny micro-edges into account-ending variance. The math doesn't work even with positive expectancy.

**Strategies optimised on the last 30 days.** Anything that wants to retrain weekly on the most recent month is overfitting to noise. If a signal works only on the last month, it doesn't work.

**Copy-trading or signal-following.** Outsourcing the strategy to a third party is outsourcing your edge and adding counterparty risk. The whole point of building this bot is to own the strategy.

**Discretionary overrides.** The bot trades; the operator monitors. The moment you start manually overriding entries or exits, you're adding emotional variance to a system designed to eliminate it.

---

## Maintenance reminder

Update this doc when:

- A v3 item gets built (move from "deferred" to "shipped," add a link to its docs)
- Real observation data changes the trigger conditions
- A new candidate appears (research finding, observed pain point, competing tool's feature)
- A deferred item is determined to be a bad idea after more thought (move to "not on the roadmap" section)

Most roadmap docs go stale fast because they're never revisited. The fix is to make updating this part of the v3 development cycle — every shipped item updates its own entry, and every quarterly retrospective revisits the trigger conditions.
