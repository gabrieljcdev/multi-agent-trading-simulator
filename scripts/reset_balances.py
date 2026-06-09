"""One-off: restart the sim BALANCES while keeping every learning/log table.

The portfolio equity is computed live as (configured fund capital + realised P&L
from the trade ledgers) — there is no stored balance to reset — so flattening the
balances means clearing the trade ledgers + the derived/operational capital state.
Everything the agents learn from (signals, predictions, observer data, opportunity
logs, sentiment, macro, candles, events) is KEPT.

Back up data/cryptobot.db before running with --apply (this script does not).

    python scripts/reset_balances.py            # dry-run: print row counts
    python scripts/reset_balances.py --apply    # delete the balance rows
"""
import sys

from database.db import get_session

# Portfolio/balance STATE — cleared to flatten balances to the configured $5k.
CLEAR = [
    "trades",                  # signal + executed-scalp fills (pnl_usd → bankroll)
    "arb_trades",              # arb fills (net_pnl_usd → bankroll)
    "funding_arb_trades",      # funding-arb fills
    "capital_movements",       # balance-agent transfer ledger
    "fund_capital_efficiency", # per-fund return rollups (derived)
    "portfolio_snapshots",     # equity-over-time snapshots (derived)
    "daily_stats",             # daily P&L rollups (derived)
]

# A few KEEP tables, sampled only to prove they are untouched.
KEEP_SAMPLE = [
    "signals", "predictions", "agent_events", "candles",
    "wallet_flow_events", "copytrade_events", "meme_decisions",
    "opportunity_observation", "scalp_observations", "circuit_breaker_log",
]


def _count(s, table):
    from sqlalchemy import text
    return s.execute(text(f"SELECT count(*) FROM {table}")).scalar()


def main():
    apply = "--apply" in sys.argv
    with get_session() as s:
        print("=== CLEAR (balance/portfolio state) ===")
        before = {}
        for t in CLEAR:
            before[t] = _count(s, t)
            print(f"  {t:<26} {before[t]:>10,} rows")
        print("=== KEEP (sample — untouched) ===")
        for t in KEEP_SAMPLE:
            print(f"  {t:<26} {_count(s, t):>10,} rows")

        if not apply:
            print("\nDRY-RUN — nothing deleted. Re-run with --apply to clear the "
                  "CLEAR set above.")
            return

        from sqlalchemy import text
        print("\nAPPLYING — deleting balance rows…")
        for t in CLEAR:
            s.execute(text(f"DELETE FROM {t}"))
        s.flush()

    # Verify post-state in a fresh session.
    with get_session() as s:
        print("=== AFTER ===")
        for t in CLEAR:
            print(f"  {t:<26} {_count(s, t):>10,} rows")
        for t in KEEP_SAMPLE:
            print(f"  KEEP {t:<21} {_count(s, t):>10,} rows (preserved)")
    print("\nBalances reset. Next full-bot run starts flat at the configured "
          "STARTING_CAPITAL ($5,000).")


if __name__ == "__main__":
    main()
