# Web UI

Browser control panel served by `ui/web_server.py`. Aiohttp WebSocket
+ REST, push-only state, localhost / LAN only — never authenticated,
never SSL, never internet-exposed. Mirrors the terminal dashboard
(`ui/dashboard.py`); the two render independently from the same data
sources.

The push snapshot is live state, not history; the one exception is the
on-demand REST reads, which DO serve historical / as-of analysis — and,
since the unified-observer-UI pass, on-demand **charts** — on request
rather than over the 2Hz push. The three $0 observer panels (wallet-flow
`/api/walletflow/*`, copy-trade `/api/copytrade/*`, meme rug-rate
`/api/meme/*`) are the widest such surface — per-actor skill scores,
evidence, point-in-time as-of reconstruction, and the viz aggregation
endpoints below. Because that data is identity-bearing (wallet / trader /
funder), every one of those endpoints is bound to the privacy guard: it
returns **403** unless `WEB_UI_HOST` is a loopback host (`localhost` /
`127.0.0.1` / `::1`). LAN binds get no observer data.

> **Scope note (second deliberate departure from real-time-only).** The
> observer tabs now render on-demand historical **SVG charts** —
> net-flow-over-time, a skill-vs-sample-size scatter, the denominator
> view, the funder-cluster graph, and the replay TP/FP/coverage plot.
> This amends the original "no chart/graph rendering" and "no historical
> analysis" notes. The charts are still **vanilla inline SVG with no
> chart library and no CDN import** — the no-dependency rule is intact;
> only the "real-time-only" scope moved. Heavy visuals are fetched over
> REST when the operator expands a tab's chart drawer and are **never**
> computed in or pushed through the 2Hz snapshot.

The single HTML file `ui/web_dashboard.html` is served at `/` and
opens a WebSocket to `/ws`. Snapshots arrive every
`WEB_UI_PUSH_INTERVAL_S` (default 0.5s); the UI applies them via
`updateUI(state)` and never makes its own state-keeping decisions.
REST endpoints handle every operator action — buttons POST, server
state changes, the snapshot on the next push reflects the change.

---

## Snapshot schema (`/ws` payload)

Every key is always present — `_build_snapshot()` wraps each reader in
`_safe(...)` and falls back to a typed empty default so a single
source raising never blocks the broadcast.

Top-level keys, in order:

| Key | Type | Source |
|---|---|---|
| `ts` | `"HH:MM:SS"` | server clock |
| `session` | `"LONDON"\|"NEW_YORK"\|"ASIA"\|"OFF_HOURS"` | derived from UTC hour |
| `uptime_s` | int | server uptime |
| `mode` | `"SIM"\|"LIVE"` | `settings.SIM_MODE` |
| `paused` | bool | bot._paused |
| `approval_mode` | `"per_trade"\|"window"\|"autonomous"` | `settings.APPROVAL_MODE` |
| `portfolio` | dict | `_snap_portfolio()` — bankroll, daily P&L, exposure, win rate |
| `agents` | list[dict] | coordinator.get_agent_stats(), one row per registered agent (each row carries `manually_halted: bool` — Web UI v3.1; `capital` reads the LIVE `agent.get_capital_allocation()` per push, cached stats as fallback — v2 fixes) |
| `circuit_breakers` | dict | bot._cb_state + `settings.CIRCUIT_BREAKERS` |
| `regime` | dict | `core.regime_detector` |
| `sentiment` | dict | bot._sentiment latest |
| `exchanges` | list[dict] | bot._market_data |
| `signals` | list[dict] | `queries.get_signal_history(days=1)` |
| `pending_signal` | dict\|null | bot.peek_pending() |
| `positions` | list[dict] | `queries.get_open_trades()` + live prices — each row carries the deploying `agent` (Trade.strategy mapped to a registered agent id; strategy-PROFILE names like "default" map to `signal`), the originating `track` (signal_type), and `exit_target` ("TP x / SL y", "—" when no targets) — v2 fixes |
| `scalp` | dict | scalp agent + `queries.get_scalp_trade_history` |
| `arb_feed` | list[dict] | recent arb trades |
| `session_pnl` | dict | `queries.get_session_pnl_today()` |
| `log` | list[dict] | in-UI log handler buffer |
| `top_pairs` | list | `queries.get_top_pairs(5)` |
| `top_strategies` | list | `queries.get_strategy_performance()` |
| `insights` | list | `queries.get_recent_postmortems(3)` |
| `arb_history` | list[dict] | full arb history for the history tab |
| **`arb`** *(v2)* | dict | dedicated arb-fund panel — exchanges, gap distribution, threshold, semaphore |
| **`xchain`** *(v2)* | dict | cross-chain panel — status, capital, chains, best_pair, inventory_targets, today_summary |
| **`funding`** *(v2)* | dict | funding-rate panel — status, capital, venue, symbols, positions, today_summary |
| **`balance`** *(v2)* | dict | balance-agent panel — status, pool, funds, nodes, in_transit, halted_pairs, today, pending_plan |
| **`opportunities`** | dict | Opportunity Scanner panel (read-only; $0 observation) — enabled, observation_count, detection_latency_ms_p50, standard, exploratory, notes |
| **`walletflow`** | dict | Wallet + exchange-flow watcher (read-only; $0 OBSERVER) — enabled, running, flow_events, netflow_spikes, pending_candidates, label_staleness, sources. Live/low-volume only; wallet-identifying + historical reads are on-demand REST under `/api/walletflow/*` |
| **`capital`** *(v2 fixes)* | dict | capital deployment + movements — totals, per-agent live allocation/deployed/idle + return-on-deployed%, in-transit moves, recent capital_movements rows |

### `arb` (v2)

```
{
  exchanges:         [{id, fee_bps, available, latency_ms, last_book_ts, errors_5m}],
  gap_distribution:  {bucket_edges_bps, bucket_counts, median_bps, p90_bps},
  threshold:         {min_net_gap_bps, rationale},
  semaphore:         {active, capacity}
}
```
Source: arb engine attrs (best-effort) + `settings.ARB_*` constants. Under
the current scope, `exchanges`, `gap_distribution.bucket_counts`, and
`threshold.rationale` are empty/zero defaults until the engine surfaces
them; the panel renders a "pending engine work" placeholder for those.

### `xchain` (v2)

```
{
  status:            "RUNNING" | "OBSERVATION" | "OFFLINE" | "ERROR",
  capital_usd:       float,
  chains:            [],                  # not surfaced — pending engine work
  best_pair:         {would_entry: false, skip_reason: "not_surfaced", ...},
  inventory_targets: [{fund, chain, asset, current_usd, target_usd, drift_pct, needs_rebalance}],
  today_summary:     {n_observations, n_would_entry, mean_net_edge_bps,
                      median_net_edge_bps, pct_blocked_by_gas, pct_blocked_by_min_edge}
}
```
Sources: `CrossChainArbAgent.observation_mode` + `get_inventory_targets()`,
`queries.get_xchain_today_summary()`.

### `funding` (v2)

```
{
  status:        "RUNNING" | "OBSERVATION" | "OFFLINE" | "ERROR",
  capital_usd:   float,
  venue:         "binance",
  symbols:       [],                       # not surfaced — pending engine work
  positions:     [{symbol, side, spot_qty, perp_qty, delta_usd, entry_ts,
                   funding_collected_usd, realised_apr_pct, next_exit_check}],
  today_summary: {n_observations, n_would_enter, utilisation_pct,
                  mean_projected_apr_pct, median_projected_apr_pct,
                  blended_apr_pct,
                  skip_reasons: {below_gate, basis_unfavourable, depth_thin, other}}
}
```
Sources: `FundingArbAgent.observation_mode` + `_positions`,
`queries.get_funding_today_summary()`.

### `balance` (v2)

```
{
  status:        "RUNNING" | "PAUSED" | "HALTED" | "OFFLINE" | "ERROR",
  kill_blocked:  bool,                          # agent._paused
  pool:          {equity_usd, reserve_usd, deployed_usd, pool_usd},
  funds:         [{id, allocation_usd, target_usd, deployed_usd, drift_pct,
                   return_24h_pct, return_7d_pct, starvation_events_24h}],
  nodes:         [{fund, exchange, asset, physical_usd, effective_usd, floor_usd,
                   cap_usd, band_lower_usd, band_upper_usd, in_band}],
  in_transit:    [{id, ts, from_fund, to_fund, from_exchange, to_exchange,
                   asset, amount_usd, state, initiated_by, error}],
  halted_pairs:  [],                            # not surfaced — needs agent tracker
  today:         {transfers_count, transfers_fees_usd, transfers_remaining},
  pending_plan:  {proposed_at, confirm_token, transfers: [...]}
}
```
Sources: `BalanceAgent.get_pending_proposal()`, `_last_computed_targets`,
`_last_computed_bands`, `inventory_state.effective_balance(...)`,
`queries.get_fund_efficiency_summary(24|168)`,
`queries.get_capital_movements_in_transit()`.

`pending_plan.confirm_token` is non-null only while the arm window is
open (in-memory only; expires at `REBALANCE_ARM_TIMEOUT_S` after arm).

### `opportunities`

```
{
  enabled:                   bool,          # settings.OPPORTUNITY_SCANNER_ENABLED
  observation_count:         int,           # hypothesis-log rows
  detection_latency_ms_p50:  int | null,    # median on-chain-event → first_seen gap
  standard: [{                              # survivors, trajectory → edge → reachability
    opp_type, protocol, chain, market_key,
    first_seen:        "MM-DD HH:MM",       # UTC
    competitor_trend,                       # HEADLINE — rendered first
    competitor_count,
    edge_annualized_pct: float | null,      # never rendered without trend adjacent
    edge_confidence:     "low"|"medium"|"high",
    reachability, window_status
  }],
  exploratory: [{                           # SAME survivor set, sorted by
    ...standard fields, plus:               # unconventional_score; never merged
    unconventional_score, unconventional_factors,
    unconventional_rationale                # free text — rendered inline
  }],
  notes: [{                                 # noteworthy reasoning posts (push);
    id, ts, body, factor_tags,              # the full feed is a pull (endpoint below)
    edge_pct, competitor_trend, reachability,
    noteworthy: bool,
    was_right: "correct"|"incorrect"|"inconclusive"|null,
    outcome_summary: string | null
  }]
}
```
Sources: `queries.get_ranked_opportunities(mode=...)`,
`queries.get_opportunity_summary()`, `queries.get_opportunity_notes()`.
Read-only — the panel exposes no action control; its one interactive
element (the feed's Noteworthy/All-posts dropdown) fires a GET read.
Lists cap at `OPPORTUNITY_PANEL_MAX_ROWS`; exploratory drops rows below
`OPPORTUNITY_UNCONVENTIONAL_MIN_SCORE`; disqualified rows appear in
neither list while `OPPORTUNITY_SHOW_DISQUALIFIED=False`, and the 6c
counterfactual self-audit is deliberately NOT surfaced (DB-only, for
later analysis).

### `capital` (v2 fixes)

```
{
  total_equity:     float,          # coordinator.get_portfolio_stats()
  total_deployed:   float,          # Σ per_agent[].deployed
  total_idle:       float,          # total_equity − total_deployed
  per_agent: [{                     # one row per registered agent
    id, allocation,                 # LIVE agent.get_capital_allocation()
    deployed,                       # LIVE agent.get_open_position_notional()
    idle,                           # allocation − deployed
    return_on_deployed_pct          # latest fund_capital_efficiency row's
  }],                               #   figure per fund; null until one exists
  in_transit:       [{from_fund, to_fund, from_exchange, to_exchange,
                      amount_usd, state}],
  recent_movements: [               # last WEB_UI_CAPITAL_MOVEMENTS_N rows,
    {ts, from_fund, to_fund,        #   newest first. ts is time-only for
     from_exchange, to_exchange,    #   today's rows, date+time for older.
     asset, amount_usd, mode,       #   mode: sim | live
     state,                         #   pending|in_transit|completed|failed
     initiated_by, note, error}
  ]
}
```

Sources: `coordinator.get_portfolio_stats()` (cached by the push loop),
live per-agent reads via `coordinator.get_agent()` (the cached stats row
is the fallback — this is what makes a BalanceAgent rebalance visible the
push after it lands), `queries.get_fund_efficiency_summary(24)` (scalp
agent → `mexc_scalp` fund), `queries.get_capital_movements_in_transit()`,
`queries.get_capital_movements_recent(n)`.

Renders as the **Capital** + **Capital Movements** cards at the top of the
dashboard view: equity/deployed/idle header, per-agent table (return%
coloured pos/neg), an In-transit subsection shown only when non-empty, and
a scrollable newest-first movements list with mode + state chips
("No movements yet" empty state).

---

## Navigation model (v3)

The Web UI v3 build (this section) replaces the v2 inline-panel layout
with a click-through pattern: the dashboard view stays compact and each
agent gets its own detail page reached by clicking that agent's card.

**Top-level tabs** (unchanged from v2): `Dashboard`, `Arb History`, `Log`.
These are global; their behaviour does not depend on which agent — if any
— is selected.

**Dashboard tab — two states** driven by a single JS variable
`activeAgentId` (default `null`):

- `activeAgentId == null` → the **dashboard view** renders below the
  agent grid: metrics row, **Capital + Capital Movements** (v2 fixes —
  see the `capital` snapshot block), regime + sentiment, signal feed +
  approval, circuit breakers + exchanges, **Open Positions** (with
  per-track colour badges and the `exit_target` column), and
  **Session P&L**. The Opportunity Scanner panel moved to the scanner's
  agent detail page — it previously rendered in both places and the
  dashboard copy was removed as a duplicate. The v2 inline panels and
  the standalone Live Arb Feed remain gone from this view.
- `activeAgentId == "<agent_id>"` → the **agent detail page** for that
  agent replaces the dashboard view content below the grid. The agent
  grid itself stays visible above so the operator can switch agents
  directly without going back to the dashboard first.

The variable is in JS memory only — no hash routing, no localStorage —
so a browser refresh resets to the dashboard view. The variable does
**survive WebSocket reconnects**: drop and reconnect the WS, you stay on
whichever detail page you were viewing.

**← Dashboard** button on every detail page sets `activeAgentId = null`
and re-renders. Bound once at startup, outside any `safeRender`
boundary, so it remains clickable even if the current detail page's
body render throws.

Agent grid: all seven registered agents (`signal`, `arb`, `scalp`,
`xchain`, `funding_arb`, `balance`, `opportunity_scanner`) render
uniform compact cards in this fixed order, regardless of whether they
own a detail page. The active card is marked with the same outline
style as an active session card (`outline:2px solid var(--tx0)`).
Observation-mode agents (`xchain`, `funding_arb`,
`opportunity_scanner`) render at 70% opacity per the v2 OBS
convention.

Two v2-fixes additions on the cards:

- a one-push **▲/▼ cue** beside the cap figure when the live allocation
  moved since the previous snapshot (a BalanceAgent rebalance landing);
- a **pause/play toggle** on the five trading agents (`signal`, `arb`,
  `scalp`, `xchain`, `funding_arb`) wired to `/action/agent_pause` (see
  below). RUNNING/SIM-TRADING/OBSERVATION → ⏸ Pause; PAUSED → ▶ Resume;
  HALTED (circuit breaker) → ▶ Resume in warning colour whose FIRST
  click never overrides — the card expands an inline arm→confirm
  ("Override & resume?", 3s auto-disarm, kill-button UX) with an extra
  warning line on drawdown halts. Buttons disable on click and re-render
  from server status on the next push — no optimistic flip.

## Per-agent detail pages

Each detail page starts with a uniform header strip:

```
[← Dashboard]  [display_name]  [STATUS]  cap $… · daily P&L · N tr · WR%
```

Below the header, the body is rendered via `safeRender("ad-body", …)`
so a single broken detail page never escapes the operator into a dead
UI. The render function dispatched per agent_id:

| Agent | Body |
|---|---|
| `signal`      | open positions filtered to `agent=="signal"`; recent signal feed; self-review insights. The signal agent is a shared execution layer — it executes on behalf of funding_arb / momentum / reversion etc., so the feed deliberately shows ALL tracks, each labelled with its colour badge (`.tag.strat-*`: funding_arb cyan, momentum orange, reversion purple, arb blue, scalp green, signal yellow, unknown grey — same map in the positions table and trade log) |
| `arb`         | full `arbFundPanelHtml(arb, arb_feed, arb_history)` — exchanges table, gap distribution, recent fills, today's performance |
| `scalp`       | OFI strip (placeholder — pending engine surface); open scalp positions (filtered `agent=="scalp"`); live + closed scalp feed (`scalpFeedHtml`); today's perf (placeholder) |
| `xchain`      | full `xchainPanelHtml(xchain)` |
| `funding_arb` | full `fundingPanelHtml(funding)` |
| `balance`     | full `balancePanelHtml(balance)` — includes the arm/confirm/cancel control block, which now lives on this detail page rather than on the dashboard |
| `opportunity_scanner` | full `opportunityPanelHtml(opportunities)` — the read-only panel (standard + exploratory views, reasoning feed); this detail page is now its ONLY surface — the duplicate dashboard card was removed |
| `follow` | the three stacked $0 OBSERVER panels — `walletflowPanelHtml(walletflow)`, `copytradePanelHtml(copytrade)`, `memePanelHtml(meme)` — each carrying a collapsed **▸ Visualizations** drawer (`observerVizDrawer`). Live layers (suggestive-not-confirmed flow / position changes / AVOID·NO-SIGNAL decisions, plus the candidate-review queue) come from the snapshot; the chart drawers and per-wallet/actor/launch as-of reconstructions are on-demand REST (`bindObserverViz()` + `bindWalletflowReview()`), localhost-guarded, never pushed. The drawers render inline SVG charts (one shared vanilla helper set: `vizFlowChart`, `vizBarsH`, `vizScatter`, `vizCluster`) with a consistent colour language (risk=`--neg`, neutral/NO-SIGNAL=`--warn`, good/followable=`--pos`, censored/cut/gaps=`--tx2`) and uncertainty shown as prominently as conclusions |

The four `*PanelHtml` functions (`arbFundPanelHtml`, `xchainPanelHtml`,
`fundingPanelHtml`, `balancePanelHtml`) are unchanged from v2 — they
produce the same HTML they always have. They moved from inline panels
on the dashboard into the corresponding agent's detail page.

**Per-agent open positions** filter from `snapshot.positions[]` via the
`agent` field. `_snap_positions` maps `Trade.strategy` to a registered
agent id: the non-signal funds write their own names (`scalp`,
`funding_arb`, …), while router-written signal trades carry the ACTIVE
strategy-PROFILE name ("default", "arb_only", …) — anything not in the
registered set maps to `signal` (v2 fixes; previously the profile name
leaked through and broke these filters).

**Follow-up engine work** (placeholders rendered today, no server-side
work in this build):
- `scalp` detail page — OFI strip + today's evaluated/would-enter/closed counters; needs a `snapshot.scalp.ofi` + `snapshot.scalp.today` surface from `ScalpingAgent`.
- `arb` detail page — `arb.exchanges`, `arb.gap_distribution.bucket_counts`, `arb.threshold.rationale` are stubbed at the server (`_snap_arb_v2`) until the arb engine exposes per-exchange health + gap distribution. Same caveat as v2.
- `xchain` detail page — `xchain.chains[]` and `xchain.best_pair.would_entry` similarly stubbed, pending engine work.

## Panel pattern

Every dashboard section is a `sec-label` + `card` div pair, rendered by
a function called from `updateUI(state)`. The detail-page render
functions follow the same conventions:

- Each render function is wrapped in a per-panel `try`/`catch` via
  `safeRender(elId, build)`. A single broken render shows a one-line
  "render error" placeholder; siblings keep updating.
- No new CSS frameworks, no CDN imports, no chart libraries. The
  gap-distribution histogram and the funding skip-reasons bar are
  plain `.bar > i` divs with width = pct.
- Stale rows (last_update > 30s for a chain or symbol) render at 50%
  opacity with the s_ago count in red.
- Observation-mode panels render the header strip at 70% opacity with
  an `OBS` tag (`obsHeader(text, isObs)` helper).
- All buttons disable on click and re-enable when the next snapshot
  reflects the new server state — never optimistically update local UI.

### LED price grid (page-level drawer, not a tab panel)

A dot-matrix-styled drawer at the very top of the page (`#ledWrap`),
above the top bar and the tab nav — visible on every view. The header
bar carries a collapse toggle (▲/▼), a status LED (green live / amber
fetching / red no-data) with venue + pair count + last-update, and the
exchange dropdown (defaults to `TICKER_DEFAULT_EXCHANGE` on every load —
deliberately not persisted). The header also carries ‹ › page
arrows + a page indicator — ALL of the venue's dollar-quoted pairs are
available, paged `TICKER_GRID_BOXES × TICKER_ROWS_PER_BOX` at a time
(page resets to 1 on venue change). The body is a 4-wide grid of LED
tiles (2-wide under 1100px), each tile listing `TICKER_ROWS_PER_BOX`
pairs: coin logo (server-proxied via `/api/coinlogo/{coin}`, letter
avatar on 404), white symbol, **amber/yellow price**, green-▲ / red-▼ /
grey-▬ 24h % with glows (null pct renders `▬ —`), and a dim 24h volume
(`$1.2B` / `$48M` style).

Data comes from `GET /api/ticker` polled every `TICKER_POLL_INTERVAL_S`
seconds — a plain `fetch`, intentionally separate from the WebSocket
snapshot. Polls are sequence-tagged so a slow venue's late reply can
never overwrite a newer selection (the venue-switch race). The venue
list / default / poll cadence / grid shape are injected server-side into
the page by `_load_html` (the `__TICKER_CFG_JSON__` placeholder), so
they're settings-driven without touching the WS schema.

---

## Push loop

`_broadcast_loop` runs every `WEB_UI_PUSH_INTERVAL_S` (default 0.5s):

1. `_refresh_coordinator()` awaits `get_portfolio_stats()` and
   `get_agent_stats()` into sync caches.
2. `_build_snapshot()` reads every other source defensively and
   returns the dict above.
3. `_broadcast(payload)` sends to every connected WebSocket; dead
   clients are dropped silently.

A broken read at step 2 logs at DEBUG and falls back to the typed
empty default for that key — the broadcast never fails because a
field couldn't be computed.

---

## Log handler

`WebLogHandler` is installed on the root logger in `WebServer.start()`
and feeds the `log` snapshot key from a 50-entry bounded deque. Each
record is classified by level + keyword into:

| Type | Rule |
|---|---|
| `kill` | message contains "kill" |
| `err`  | level ≥ ERROR |
| `warn` | level ≥ WARNING |
| `arb`  | message contains "arb" |
| `skip` | message contains "skip" |
| `exec` | message contains "execut" |
| `info` | everything else |

The handler swallows its own exceptions — logging must never raise.

---

## REST actions

All POST. JSON body. Response is always `{"ok": bool, ...}`; the
handler wraps the underlying agent call in `try/except` and serialises
the error string. Never raises to the aiohttp layer.

| Path | Body | Effect |
|---|---|---|
| `/action/approve` | `{}` | `bot.approve_next_pending()` |
| `/action/skip` | `{reason?}` | `bot.skip_next_pending(reason)` |
| `/action/pause` | `{paused: bool}` | toggle `bot._paused` |
| `/action/set_mode` | `{mode}` | set `settings.APPROVAL_MODE` |
| `/action/approve_window` | `{minutes?}` | `bot.approve_window(minutes)` |
| `/action/kill` | `{}` | `coordinator.kill_all(reason="web")`; logs `KILL_WEB` |
| `/action/rebalance` | see below | three-action arm/confirm/cancel |
| `/action/agent/{agent_id}/halt`   | `{}` | `coordinator.halt_agent(id)`; logs `HALT_MANUAL` (v3.1) |
| `/action/agent/{agent_id}/resume` | `{}` | `coordinator.resume_agent(id)`; logs `RESUME_MANUAL` (v3.1) |
| `/action/agent_pause` | `{agent_id, paused, override_breaker?}` | `BaseAgent.pause()/resume()`; breaker-halted resume requires the explicit override; logs `AGENT_PAUSE` / `AGENT_RESUME` / `AGENT_BREAKER_OVERRIDE` (v2 fixes) |

### GET endpoints

| Path | Returns |
|---|---|
| `/api/agent/{agent_id}` | `{trades, insights}` for the agent detail page |
| `/api/session/{session_name}` | session trades + local clock for the session page |
| `/api/ticker?exchange=<id>` | LED-ticker prices, proxied through the bot's ccxt layer (see below) |
| `/api/coinlogo/{coin}` | coin-logo proxy (cryptocurrency-icons SVG, server-cached; 404 → grid letter-avatar fallback) |
| `/api/opportunity_notes?noteworthy={true\|false}&limit={int}` | `{"ok": true, "notes": [...]}` — the reasoning feed's full-history pull (`noteworthy=true` default; `false` includes routine posts). Read-only; errors return `{"ok": false, "error": ...}`. The kill switch remains the only DB-writing endpoint. |
| `GET /api/walletflow/wallet/{address}?as_of={iso}` | `{"ok": true, "wallet", "score", "evidence", "as_of"}` — point-in-time skill score + evidence + as-of reconstruction. The as-of view calls the SAME shared point-in-time function as the scorer (`follow.skill_scorer.resolved_actions_as_of`) — one code path. **403** off-loopback (privacy guard). |
| `GET /api/walletflow/candidates` | `{"ok": true, "candidates": [...]}` — pending auto-discovery candidates WITH bait-resistance warnings (informational, never auto-applied). **403** off-loopback. |
| `POST /api/walletflow/candidate/confirm` `{address}` | candidate → confirmed (operator action — the ONLY path to confirmed). `{"ok": bool, ...}`. **403** off-loopback. |
| `POST /api/walletflow/candidate/reject` `{address}` | candidate → rejected (permanent; never re-proposed). `{"ok": bool, ...}`. **403** off-loopback. |
| `GET /api/walletflow/health` | `{"ok": true, "label_staleness", "coverage", "latency_delta_s"}` — label-set staleness, source coverage, latency-δ p50/p90. **403** off-loopback. |
| `GET /api/walletflow/viz/flow_series` | `{"ok": true, "series": {window_h, buckets:[{t, inflow_usd, outflow_usd, net_usd, events}], n_events}, "provenance": {counts:{candidate,confirmed,manual,rejected}, expiring_soon, total}, "best_effort", "gaps_24h"}` — **on-demand chart data**: time-bucketed exchange net-flow + watchlist provenance, derived read-side from stored flow events via `queries.get_wallet_flow_series` / `get_watchlist_provenance_counts`. NOT in the snapshot. **403** off-loopback. |
| Copy-trade on-demand (all **403** off-loopback): `GET /api/copytrade/actor/{id}?as_of=` (point-in-time skill + as-of via the shared `skill_scorer.resolved_actions_as_of`), `/surfaced`, `/denominator`, `/health`, and `GET /api/copytrade/viz/skill` → `{"ok": true, "points":[{actor_id, venue, skill, sample, delta_s, drawdown, followable}], "denominator":{evaluated, surfaced, rejected, rejection_reasons}}` — the luck-vs-skill scatter population + denominator chart data. |
| Meme on-demand (all **403** off-loopback): `GET /api/meme/launch/{mint}` (per-launch cluster + each funder's rug-rate via the shared `meme_scorer.rug_rate_as_of`), `GET /api/meme/asof/{funder}?as_of=` (the as-of inspector — SAME shared fn), `GET /api/meme/health` (sampling best-effort/gap flag + replay TP/FP/coverage). The meme charts (cluster graph, rug-rate evidence, replay plot, as-of inspector) reuse these existing endpoints — no new meme endpoint was added. |

The wallet-flow confirm/reject endpoints are operator WRITES — distinct from
the opportunity panel's read-only feed. They mutate watchlist provenance only
(never capital), are localhost-guarded, and are the operator's two paths off
the "candidate" state; discovery itself can never confirm.

### `GET /api/ticker`

Feeds the LED price grid. The browser never calls a venue directly, and
neither does this handler: a dedicated worker thread
(`web_server._TickerWorker`, its own event loop so ccxt's heavy
load_markets / bulk parsing never starves the dashboard loop) owns the
venue clients, round-robins every configured venue with one bulk
`fetch_tickers()` each per `TICKER_POLL_INTERVAL_S` cycle (bounded by
`TICKER_FETCH_TIMEOUT_S`, 3× on the first markets-loading call), and
writes finished payloads into a cache. The handler is a pure cache read
— instant regardless of venue health:

```json
{"ok": true, "exchange": "kraken", "label": "Kraken", "ts": "HH:MM:SS",
 "rows": [{"coin": "BTC", "symbol": "BTC/USD", "price": 64210.5, "pct": 1.23}]}
```

Rows are ALL of the venue's dollar-quoted pairs (USD/USDT/USDC), deduped
per base, ranked by 24h quote volume — uncapped; the grid paginates
client-side in pages of `TICKER_GRID_BOXES × TICKER_ROWS_PER_BOX`. Each
row carries `price`, `pct` (24h change from the bulk payload; null when
the venue omits it) and `vol` (24h quote volume, ~USD); unusable entries
are skipped. A venue's last good payload is sticky across failed
refreshes. Before the worker's first payload for a venue the endpoint
answers `{ok: false, error: "warming up"}`; unknown `exchange` falls
back to `TICKER_DEFAULT_EXCHANGE`; the handler never raises. The venue
registry (labels + accepted quotes) lives in
`web_server._TICKER_REGISTRY`. Row logos come from
`GET /api/coinlogo/{coin}` — a server-cached proxy of the
cryptocurrency-icons SVG set (the browser only ever talks to the bot);
a 404 falls back to a letter avatar in the grid.

### `/action/kill`

Two-click arm/fire in the UI (not on the server side). Posts an empty
body. Server runs `coordinator.kill_all()` and logs a `KILL_WEB`
event via `queries.log_agent_event("portfolio", "KILL_WEB", "...")`.

Response: `{"ok": True, "result": {...}}`.

### `/action/rebalance`

Web UI v2 three-action contract.

**Arm:**
```
POST /action/rebalance
{"action": "arm"}

→ {
  "ok": true,
  "confirm_token": "<opaque hex>",
  "expires_in_s": REBALANCE_ARM_TIMEOUT_S,
  "proposal": { "proposed_at": "HH:MM:SS",
                "proposal_id": int,
                "transfers": [
                  {from_fund, to_fund, from_exchange, to_exchange,
                   asset, amount_usd, est_fee_usd, est_time_s,
                   ring_fence_warning} ] },
  "ring_fence_warnings": ["<str>", ...]
}
```
Server calls `BalanceAgent.arm()` (issues + persists the token
in-memory) and `BalanceAgent.get_pending_proposal()` (returns the
buffered plan with ring-fence warnings derived per-transfer).

The snapshot's `balance.pending_plan.confirm_token` flips to the new
token on the next push so the UI can render the **Confirm Rebalance**
+ **Cancel** buttons distinct from the **Arm Rebalance** button.

**Confirm:**
```
POST /action/rebalance
{"action": "confirm", "confirm_token": "<same hex>"}

→ {"ok": true, "transfer_ids": [<capital_movements.id>, ...]}
  | {"ok": false, "error": "<reason>"}
```
Server runs `BalanceAgent.execute_proposal(token)` which:
1. validates the token (`token_invalid`, `token_expired`),
2. re-runs the policy + planner against the current ledger,
3. compares the fresh plan to the buffered one — refuses with
   `plan_changed` if any route differs or any amount has moved by
   more than 10%,
4. dispatches the buffered transfers via the active rail (sim or
   live), surfacing `transfer_ids` from the resulting
   `capital_movements` rows.

Reasons surfaceable in the error envelope: `balance_agent_unavailable`,
`live_rebalance_disabled`, `token_invalid`, `token_expired`,
`no_pending_plan`, `plan_changed`, `kill_blocked`, `replan_failed:<msg>`,
`all_blocked_by_safety`, `agent_returned_non_dict`, `invalid_action`.

On success the handler logs a `REBALANCE_WEB` event:
```
queries.log_agent_event("balance", "REBALANCE_WEB",
                        f"source=web_ui transfer_ids={ids}")
```

**Cancel:**
```
POST /action/rebalance
{"action": "cancel", "confirm_token": "<same hex>"}

→ {"ok": true}
```
Server calls `BalanceAgent.consume_arm(token)` to invalidate the
token. Subsequent confirms with that token fail with `token_invalid`.

**Live gate:** when `SIM_MODE=False` and `REBALANCE_LIVE_ENABLED=False`,
the handler returns `live_rebalance_disabled` immediately on confirm
without calling the agent. Defence-in-depth: the agent also gates on
this internally.

**Kill switch:** when `balance.kill_blocked` is true (the agent's
`_paused` flag, raised by `coordinator.kill_all()`), the UI disables
the Arm / Confirm / Cancel buttons with a tooltip; on the server side
`execute_proposal` returns `kill_blocked`.

### `/action/agent/{agent_id}/halt` and `/resume` (v3.1)

Per-agent halt toggle — distinct from the global Kill (closes all
positions on every agent) and the global Pause (pauses the whole bot).
Halting one agent skips its **entry-creation path only**; exit /
management paths keep running so open positions are never abandoned. To
force exits, use the global Kill switch.

Single click, no arm/confirm — halt is reversible (the cost of a misclick
is at most one skipped scan cycle), so the operator friction of an
arm/confirm would defeat the purpose. Reserve arm/confirm for genuinely
destructive actions (kill, rebalance).

**Halt:**
```
POST /action/agent/{agent_id}/halt
{}

→ 200 {"ok": true,  "agent_id": "...", "halted": true}
| 404 {"ok": false, "error": "agent_not_found"}
| 200 {"ok": false, "error": "<reason>"}      # coordinator exception
```

**Resume:**
```
POST /action/agent/{agent_id}/resume
{}

→ 200 {"ok": true,  "agent_id": "...", "halted": false}
| 404 {"ok": false, "error": "agent_not_found"}
```

Server calls `coordinator.halt_agent(id)` / `resume_agent(id)` — the
coordinator is the only public seam. Each successful call logs a
`HALT_MANUAL` / `RESUME_MANUAL` agent event with `source=web_ui`.

The snapshot's `agents[<i>].manually_halted` reflects the new state on
the next push. The UI is snapshot-driven — the button disables on click
and re-enables when the snapshot confirms the change; no optimistic
update.

**Interaction with global controls:**

- When global Kill is engaged, every agent is effectively halted
  regardless of its per-agent state. The per-agent button still works
  and persists state — so when Kill releases, agents that were
  individually halted **stay** halted, and agents that weren't resume.
- Global Pause pauses the whole bot. When unpaused, individually halted
  agents stay halted.

**Persistence — known limitation:** halt state is in-memory only
(`BaseAgent._manually_halted`). A bot restart resets every agent to
unhalted. Persisting halt state across restarts is a follow-up build.

### `/action/agent_pause` (v2 fixes)

```
POST /action/agent_pause
{"agent_id": "scalp", "paused": true|false, "override_breaker": false}

→ {"ok": true,  "status": "<new status>"}
| {"ok": false, "error": "unknown agent"}
| {"ok": false, "error": "halted_by_breaker", "breaker_reason": "<reason>"}
| {"ok": false, "error": "<exception string>"}
```

Wired to the existing `BaseAgent` lifecycle — `paused: true` →
`await agent.pause()` (logs `AGENT_PAUSE`), `paused: false` →
`await agent.resume()` (logs `AGENT_RESUME`), both `source=web_ui`.

**Breaker override.** When the agent is halted by a **circuit breaker**
(signal: `bot._cb_state.halted` with its reason string; arb: the
engine's HALTED status with a reason synthesised from its counters;
generic: a literal HALTED lifecycle status), a plain play returns
`halted_by_breaker` + the reason WITHOUT resuming. Only an explicit
`override_breaker: true` (the UI's second, armed confirm click) clears
the agent's CB state (`clear_circuit_breakers()`) plus the
coordinator's fund/portfolio trackers, resumes, and logs a distinct
`AGENT_BREAKER_OVERRIDE` event carrying the overridden reason. The
breakers themselves are never weakened — this is the audited operator
escape hatch the RUNBOOK warns about (drawdown halts surface an extra
warning in the confirm step: manual review required).

**Pause vs halt (v3.1) — two different levers.** Halt skips the agent's
*entry-creation path only* while exits keep running; pause flips the
`BaseAgent` lifecycle status to PAUSED (the same mechanism the per-fund
circuit breaker uses via `coordinator._safe_pause`). An operator pause
on a fund whose CB condition still holds will be re-paused by the next
monitor pass after a plain resume — that's deliberate.

---

## Adding a new agent

Per `PLUGIN_PATTERN.md`, appending an instance to `REGISTERED_AGENTS`
in `agents/__init__.py` is the only gate for the **agent grid card**.
The coordinator picks the new agent up automatically, the snapshot's
`agents[]` array carries its stats, and the HTML renders a default
card with status / capital / daily P&L / trades / win rate. The card
is automatically clickable but opens a default detail page that just
says "no detail page yet".

To give an agent a **dedicated detail page** (like arb / xchain /
funding / balance):

1. Add a `_snap_<id>()` helper in `ui/web_server.py:WebServer` that
   reads from the agent (defensively) and returns a dict, and surface
   it as a new top-level snapshot key in `_build_snapshot()`.
2. Add the new agent_id to `AGENT_ORDER` in `ui/web_dashboard.html`
   (the grid renders in this order — all registered agents must
   appear).
3. Add a `<id>DetailHtml(state)` render function in
   `ui/web_dashboard.html` that returns the body HTML for that page.
4. Dispatch to it from `renderAgentDetail(state, id)` — the switch
   statement at the top of the v3 detail-page block.

Steps 3 and 4 replace what was step 3 of the v2 instructions ("add a
panel `<div>` to the main dashboard grid + a `safeRender` call from
`updateUI`"). The dashboard grid layout is fixed in v3 — extending the
UI means adding a detail page, not a new dashboard panel.

The terminal dashboard (`ui/dashboard.py`) is independent — adding a
new agent there is a separate edit. The two interfaces share data
sources (the same agent / engine / DB layer) but render their UIs
separately so they can evolve independently.

---

## Settings touched by the Web UI

```
WEB_UI_HOST                       "localhost"           bind address
WEB_UI_PORT                       8765                  bind port
WEB_UI_PUSH_INTERVAL_S            0.5                   broadcast cadence
WEB_UI_SCALP_FEED_HISTORY         30                    closed-trade cap on the scalp panel
WEB_UI_CAPITAL_MOVEMENTS_N        15                    rows in the Capital Movements list
WEB_UI_VIZ_FLOW_BUCKETS           24                    time buckets in the wallet-flow net-flow chart
WEB_UI_VIZ_FLOW_WINDOW_H          24                    lookback window (hours) for that chart
WEB_UI_VIZ_EVENT_SCAN_LIMIT       500                   newest flow events scanned to build the series
WEB_UI_VIZ_TRUST_EXPIRY_SOON_H    24                    confirmed-wallet trust expiring within this = "soon"

REBALANCE_CONFIRM_WINDOW_S        3                     arm→confirm click window
REBALANCE_ARM_TIMEOUT_S           10                    in-memory arm-token TTL
REBALANCE_DAILY_LIMIT             3                     per-day rebalance count cap
REBALANCE_LIVE_ENABLED            False                 live cex.withdraw gate
BALANCE_AUTO_DISPATCH             False                 v2 operator-gated dispatch (default OFF)

STABLECOIN_BENCHMARK_APR_PCT      5.0                   funding panel APR colour threshold
FUNDING_DELTA_TOLERANCE_USD       5.0                   funding position delta colour threshold

OPPORTUNITY_PANEL_MAX_ROWS        12                    max rows per view in the Opportunity Scanner panel
  (panel also reads OPPORTUNITY_SCANNER_ENABLED, OPPORTUNITY_SHOW_DISQUALIFIED,
   OPPORTUNITY_UNCONVENTIONAL_MIN_SCORE from the agent build)

WALLETFLOW_ENABLED                False                 master enable for the wallet-flow OBSERVER panel
  (the /api/walletflow/* endpoints are localhost-guarded by WEB_UI_HOST; the
   panel also surfaces WALLETFLOW_LABEL_STALENESS_WARN_DAYS-driven staleness)

TICKER_EXCHANGES                  [kraken,…,hyperliquid] venues in the ticker dropdown
TICKER_DEFAULT_EXCHANGE           "kraken"              fresh selection each page load
TICKER_GRID_BOXES                 16                    LED tiles in the grid (4-wide)
TICKER_ROWS_PER_BOX               5                     pairs per tile (capacity = boxes × rows)
TICKER_POLL_INTERVAL_S            15                    worker refresh cycle + frontend poll cadence
TICKER_FETCH_TIMEOUT_S            10                    worker per-venue bulk-fetch bound (3× first call)
```

`BALANCE_AUTO_DISPATCH=False` is the v2 default — the operator confirms
every rebalance via `/action/rebalance`. Flip to `True` only when no
operator is in the loop and the policy + planner are trusted to move
capital unattended (the legacy behaviour).
