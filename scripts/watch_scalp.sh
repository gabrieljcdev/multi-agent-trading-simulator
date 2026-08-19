#!/usr/bin/env bash
# Watch scalp-agent activity in the DB.
# Live:  cd ~/cryptobot && watch -n 5 bash scripts/watch_scalp.sh
# Once:  bash scripts/watch_scalp.sh
cd "$(dirname "$0")/.." || exit 1
DB=data/cryptobot.db

echo "scalp_observations @ $(date -u '+%H:%M:%S UTC')"
sqlite3 -box "$DB" "SELECT COUNT(*) total,
       COALESCE(SUM(would_entry),0) would_enter,
       COALESCE(SUM(exit_price>0),0) closed,
       ROUND(COALESCE(SUM(pnl_usd),0),3) pnl_usd
  FROM scalp_observations;"

echo "recent:"
sqlite3 -box "$DB" "SELECT datetime(timestamp,'unixepoch') ts, symbol, exchange,
       ROUND(ofi_z,2) z, direction dir, would_entry we,
       substr(COALESCE(skip_reason,''),1,26) skip
  FROM scalp_observations ORDER BY timestamp DESC LIMIT 8;"

echo "skip reasons:"
sqlite3 -box "$DB" "SELECT CASE WHEN COALESCE(skip_reason,'')='' THEN '(entered)' ELSE skip_reason END reason,
       COUNT(*) n
  FROM scalp_observations GROUP BY reason ORDER BY n DESC LIMIT 10;"
