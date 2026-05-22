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
