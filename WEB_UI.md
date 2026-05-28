# Web UI

Browser control panel served by `ui/web_server.py`. Aiohttp WebSocket
+ REST, push-only state, localhost / LAN only — never authenticated,
never SSL, never internet-exposed. Mirrors the terminal dashboard
(`ui/dashboard.py`); the two render independently from the same data
sources.

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
| `agents` | list[dict] | coordinator.get_agent_stats(), one row per registered agent |
| `circuit_breakers` | dict | bot._cb_state + `settings.CIRCUIT_BREAKERS` |
| `regime` | dict | `core.regime_detector` |
| `sentiment` | dict | bot._sentiment latest |
| `exchanges` | list[dict] | bot._market_data |
| `signals` | list[dict] | `queries.get_signal_history(days=1)` |
| `pending_signal` | dict\|null | bot.peek_pending() |
| `positions` | list[dict] | `queries.get_open_trades()` + live prices |
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
  agent grid: metrics row, regime + sentiment, signal feed + approval,
  circuit breakers + exchanges, **Open Positions**, **Session P&L**.
  Nothing else — the v2 inline panels and the standalone Live Arb Feed
  are gone from this view.
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

Agent grid: all six registered agents (`signal`, `arb`, `scalp`,
`xchain`, `funding_arb`, `balance`) render uniform compact cards in this
fixed order, regardless of whether they own a detail page. The active
card is marked with the same outline style as an active session card
(`outline:2px solid var(--tx0)`). Observation-mode agents
(`xchain`, `funding_arb`) render at 70% opacity per the v2 OBS
convention.

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
| `signal`      | open positions filtered to `agent=="signal"`; recent signal feed; self-review insights |
| `arb`         | full `arbFundPanelHtml(arb, arb_feed, arb_history)` — exchanges table, gap distribution, recent fills, today's performance |
| `scalp`       | OFI strip (placeholder — pending engine surface); open scalp positions (filtered `agent=="scalp"`); live + closed scalp feed (`scalpFeedHtml`); today's perf (placeholder) |
| `xchain`      | full `xchainPanelHtml(xchain)` |
| `funding_arb` | full `fundingPanelHtml(funding)` |
| `balance`     | full `balancePanelHtml(balance)` — includes the arm/confirm/cancel control block, which now lives on this detail page rather than on the dashboard |

The four `*PanelHtml` functions (`arbFundPanelHtml`, `xchainPanelHtml`,
`fundingPanelHtml`, `balancePanelHtml`) are unchanged from v2 — they
produce the same HTML they always have. They moved from inline panels
on the dashboard into the corresponding agent's detail page.

**Per-agent open positions** filter from `snapshot.positions[]` via the
existing `agent` field (`_snap_positions` already tags every row with
the owning agent_id; `signal` is the default when `strategy` is unset).

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

REBALANCE_CONFIRM_WINDOW_S        3                     arm→confirm click window
REBALANCE_ARM_TIMEOUT_S           10                    in-memory arm-token TTL
REBALANCE_DAILY_LIMIT             3                     per-day rebalance count cap
REBALANCE_LIVE_ENABLED            False                 live cex.withdraw gate
BALANCE_AUTO_DISPATCH             False                 v2 operator-gated dispatch (default OFF)

STABLECOIN_BENCHMARK_APR_PCT      5.0                   funding panel APR colour threshold
FUNDING_DELTA_TOLERANCE_USD       5.0                   funding position delta colour threshold
```

`BALANCE_AUTO_DISPATCH=False` is the v2 default — the operator confirms
every rebalance via `/action/rebalance`. Flip to `True` only when no
operator is in the loop and the policy + planner are trusted to move
capital unattended (the legacy behaviour).
