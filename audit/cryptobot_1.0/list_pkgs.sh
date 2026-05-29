#!/usr/bin/env bash
set -u
cd "$HOME/cryptobot"
for d in core agents execution sentiment ui database data_sources macro signals strategies utils profiles predictive config scalping_v2; do
  echo "=== $d ==="
  if [ -d "$d" ]; then
    find "$d" -maxdepth 3 -type f \( -name '*.py' -o -name '*.json' -o -name '*.md' \) 2>/dev/null | sort
  else
    echo "(missing)"
  fi
done
