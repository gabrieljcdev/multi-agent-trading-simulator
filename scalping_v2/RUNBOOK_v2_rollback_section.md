# Scalping v2 — Rollback Procedure

*Append this section to RUNBOOK.md. Read it before you need it, not when you need it.*

---

## When to roll back

Roll back v2 if any of the following is true after 200+ entered observations:

- v2 win rate is **below** baseline v1 win rate (i.e., the selectivity layer is destroying edge rather than concentrating it)
- v2 `avg_net_bps` is negative when v1 simulated `avg_net_bps` is positive
- v2 trade count is below 1 per day on average (the gates are so tight no signal qualifies)
- A specific gate is showing counterproductive behaviour per Recalibration Query 3 (blocked-trade win rate > 60%) and individually disabling it doesn't help

Don't roll back on:

- A single bad day or week — variance is real, give it 200 trades minimum
- v2 entering fewer trades than v1 — that's the design, not a failure
- A drawdown that hasn't hit the circuit breaker — daily-loss halt exists for a reason

## How to roll back

There are two rollback modes. Pick the right one.

### Soft rollback — disable v2 gates, keep v2 code in place

Use when: v2 is underperforming but you want to keep the v2 code installed for later re-enabling.

Edit `config/settings.py`:

```python
# === v2 OFF (soft rollback) ===
SCALP_USE_VWAP_GATE = False
SCALP_USE_HTF_TREND_GATE = False
SCALP_USE_VOLUME_GATE = False
SCALP_USE_CROSS_EXCHANGE_OFI = False
SCALP_USE_BTC_DIRECTIONAL = False
SCALP_USE_ADVERSE_SELECTION_GUARD = False
SCALP_USE_DEPTH_GATE = False
SCALP_USE_ATR_AWARE_SL = False

# Restore v1 thresholds
SCALP_OFI_Z_ENTRY = 1.5            # was 2.0 in v2
SCALP_OFI_PERSIST_TICKS = 3        # was 5 in v2
SCALP_SESSION_START_UTC = 7        # was 12 in v2
SCALP_SESSION_END_UTC = 17         # was 16 in v2
```

Then restart:

```bash
# Stop the bot (Ctrl-C in dashboard, or systemctl stop cryptobot)
python main.py --dashboard
```

The v2 code remains in `agents/`, the database columns remain populated (where applicable). The agent runs in pure v1 mode. New observations will have NULL v2 columns; old observations retain their v2 data for later analysis.

### Hard rollback — revert the integration entirely

Use when: v2 is causing errors, the integration is broken, or you want to remove all v2 traces from the running agent.

```bash
# Revert the integration commit(s) only — keep v2 files for reference
git revert <commit-sha-of-integration> --no-commit
git commit -m "Rollback v2 integration — v2 code retained in agents/"

# Verify
pytest tests/
python main.py --dashboard
```

The v2 source files stay in `agents/` for future re-integration. The agent runs as it did before v2 was wired in.

### Surgical rollback — disable one gate only

Use when: queries identify a single counterproductive gate but the rest of v2 is working.

```python
# Example: disable the BTC directional gate only
SCALP_USE_BTC_DIRECTIONAL = False
```

Restart. The other six gates continue to operate. The disabled gate logs as "disabled" in observations rather than firing checks.

This is the right move 80% of the time when v2 underperforms. Full rollback is rarely necessary.

---

## After rollback

Whatever rollback mode you used:

1. **Document what happened.** Add an entry to `RUNBOOK.md` (or a separate incident log) with: date, what failed, what queries showed, what was changed, what the next observation period revealed.

2. **Run the recalibration queries against the v2 data you have.** Even failed v2 data is useful — Query 3 (skip-reason analysis) will tell you which gate was the problem.

3. **Don't re-enable in the same configuration.** If a gate underperformed at threshold X, don't turn it back on at threshold X expecting different results. Either change the threshold or leave it off.

4. **Run pure v1 for 100+ trades** before attempting v2 again. This gives you a clean baseline to compare against.

5. **Re-enable gates one at a time.** Not all at once. Start with the gate that has the clearest theoretical basis (adverse selection, then depth, then BTC directional, then confluence). Wait 50+ trades between each addition.

---

## Pre-flight checklist before re-enabling v2

After a rollback, before turning v2 back on:

- [ ] At least 100 v1-only trades have been logged since rollback
- [ ] Recalibration queries have been run against the failed v2 data and a specific cause identified
- [ ] The cause has been addressed (parameter changed, gate disabled, dependency fixed)
- [ ] Tests pass: `pytest tests/`
- [ ] The change is documented in `RUNBOOK.md` so future-you knows what was tried
- [ ] You're starting in observation mode (`SCALP_CAPITAL = 0`)

If any box is unchecked, don't re-enable. The cost of a delayed re-enable is small; the cost of a re-enable that fails for the same reason as last time is your own time and confidence in the system.

---

## Universal kill-switch reminder

If anything is wrong at any time — v2, v1, or anything else — press K. The master kill switch closes all positions across all agents and halts trading. It bypasses every gate, every approval mode, every circuit breaker. Press it first, diagnose second.

This applies during v2 rollout as much as any other time. The selectivity gates make individual trades better; they don't change the fact that the kill switch is your final defence.
