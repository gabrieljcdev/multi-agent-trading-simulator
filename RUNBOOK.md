# Runbook

Operational playbook for running CryptoBot. One section per agent /
feature with its activation checklist + day-to-day commands.

---

## Scalping Agent

### Current state
Observation mode. `SCALP_CAPITAL = 0.0`. No orders placed.
All evaluated setups logged to `scalp_observations` in the DB.

### Activation checklist
Before setting `SCALP_CAPITAL > 0`, all of the following must be true:

- [ ] At least 200 closed observations in `scalp_observations`
- [ ] `win_rate` from `get_scalp_summary()` > 52%
- [ ] `avg_net_bps` from `get_scalp_summary()` > 0 (after fees)
- [ ] MEXC API key added to `config/keys.env`
      (`MEXC_API_KEY` + `MEXC_SECRET`)
- [ ] MEXC wired to `_get_ccxt_exchange()` in execution router
      (Phase 2)
- [ ] Market data stubs replaced with live feeds (Phase 2)
- [ ] Reviewed exit reason breakdown — `MAX_HOLD` exits < 30% of closed
- [ ] Directional accuracy at 1m > 55% (from micro tracker query)

### Activation

1. In `config/settings.py`: `SCALP_CAPITAL = 50.0`
2. Restart bot
3. Confirm in dashboard agent panel: `ScalpingAgent` shows $50 capital
4. Watch first 10 real entries in the scalp feed panel
5. If daily loss approaches `SCALP_DAILY_LOSS_HALT`: set back to 0.0

### Querying observations

```bash
sqlite3 data/cryptobot.db
.read database/queries.py   # see ANALYSIS SQL QUERIES section
```

Key queries (full list in inline comments at the bottom of
`database/queries.py`):

- Win rate + net P&L by exchange/direction
- OFI z-score buckets vs net outcomes
- Skip-reason breakdown (why entries are being blocked)
- Exit-reason breakdown vs avg net
- Directional accuracy at 1m/3m (from micro tracker backfills)
- "Is `OFI_EXHAUSTED` cutting winners short?" comparison

### Kill-switch behaviour

`agent.close_all_positions()` iterates `self._positions` and calls
`_exit_position(..., reason="FORCE_EXIT")` on each. Works in both
observation and real-capital modes — observation positions just close
without P&L.

---

## Scalping Agent v2 (selectivity layer)

A confluence + ATR-aware-stop layer on top of the existing 13-gate flow; runs
after gate 13 in `_evaluate_entry`. Design: `scalping_v2/SCALPING_V2.md`.
Tuning cookbook: `scalping_v2/SCALPING_V2_RECALIBRATION.md`.

### What it adds
- Tighter entry: `SCALP_OFI_Z_ENTRY` 2.0, `SCALP_OFI_PERSIST_TICKS` 5,
  session window 12:00–16:00 UTC.
- Confluence gates (2 of 3): VWAP alignment, 5m HTF trend, volume.
- Hard gates: cross-exchange OFI, BTC-directional (alts), adverse selection,
  depth adequacy.
- ATR-aware SL (`ATRStopCalculator`) — scales with realised volatility, clamped
  to [floor, ceiling], never tighter than the fee-derived base SL.
- Per-observation `strength_label` + diagnostics in the 13 new
  `scalp_observations` columns.

Every gate is individually togglable via its `SCALP_USE_*` flag (all default ON).

### Migration
Fresh DBs get the columns from `init_db()`. For an existing DB:
```bash
PYTHONPATH=. venv/bin/python scripts/migrate_scalp_v2.py   # idempotent
```

### Activation (v2 — tighter than v1)
The v2 readiness gate should pass for 7 consecutive days before `SCALP_CAPITAL > 0`:
```python
from database import queries as q
print(q.get_scalp_activation_readiness_v2())   # {ready, reasons_failing, stats}
```
Thresholds (`SCALP_*_FOR_LIVE_V2`): ≥300 closed obs, win_rate ≥55%,
avg_net_bps ≥0.5, MAX_HOLD exits ≤25%, 1m directional accuracy ≥57%.

### Recalibration
Run the cookbook's queries against the live DB (weekly):
```bash
PYTHONPATH=. venv/bin/python scripts/run_recalibration.py
```

### Rollback

**Surgical** (right ~80% of the time) — disable the single counterproductive
gate (identified via Recalibration Query 3) and restart:
```python
SCALP_USE_BTC_DIRECTIONAL = False   # example
```

**Soft** — disable all v2 gates and restore v1 thresholds in
`config/settings.py`; the code stays in place:
```python
SCALP_USE_VWAP_GATE = SCALP_USE_HTF_TREND_GATE = SCALP_USE_VOLUME_GATE = False
SCALP_USE_CROSS_EXCHANGE_OFI = SCALP_USE_BTC_DIRECTIONAL = False
SCALP_USE_ADVERSE_SELECTION_GUARD = SCALP_USE_DEPTH_GATE = SCALP_USE_ATR_AWARE_SL = False
SCALP_OFI_Z_ENTRY = 1.5            # was 2.0
SCALP_OFI_PERSIST_TICKS = 3        # was 5
SCALP_SESSION_START_UTC = 7        # was 12
SCALP_SESSION_END_UTC = 17         # was 16
```
New observations then carry NULL v2 columns; old rows keep their v2 data.

**Hard** — revert the integration commits (the v2 source stays in `agents/` +
`scalping_v2/` for re-integration):
```bash
git revert <scalp-v2 integration commits> --no-commit
git commit -m "Rollback scalp v2 integration"
pytest
```

**When to roll back** (after ≥200 entered obs): v2 win rate below the v1
baseline; v2 avg_net_bps negative while v1 was positive; <1 trade/day; or a
gate whose blocked-trade win rate >60% (Query 3) that disabling individually
doesn't fix. Do **not** roll back on a single bad week, or because v2 trades
less — fewer trades is the design.

**Kill switch:** if anything is wrong, press `K` — it closes all positions and
halts trading, bypassing every gate. Diagnose second.
