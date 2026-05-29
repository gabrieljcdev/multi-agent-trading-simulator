#!/usr/bin/env bash
set -u
cd "$HOME/cryptobot"
OUT="audit/cryptobot_1.0/sqlite_state.txt"
DB="data/cryptobot.db"
{
  echo "=== .schema ==="
  sqlite3 "$DB" ".schema"
  echo
  echo "=== tables ==="
  sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
  echo
  echo "=== indexes ==="
  sqlite3 "$DB" "SELECT name, tbl_name FROM sqlite_master WHERE type='index' ORDER BY tbl_name, name;"
  echo
  echo "=== row counts ==="
  for t in $(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"); do
    n=$(sqlite3 "$DB" "SELECT COUNT(*) FROM $t;")
    printf "%-40s %s\n" "$t" "$n"
  done
} > "$OUT"
wc -l "$OUT"
