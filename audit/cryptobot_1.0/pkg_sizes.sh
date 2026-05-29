#!/usr/bin/env bash
cd "$HOME/cryptobot"
for d in core agents execution sentiment ui database data_sources macro signals strategies utils profiles predictive config scalping_v2; do
  if [ -d "$d" ]; then
    lc=$(find "$d" -type f -name '*.py' -not -path '*/__pycache__/*' -exec cat {} + 2>/dev/null | wc -l)
    nf=$(find "$d" -type f -name '*.py' -not -path '*/__pycache__/*' | wc -l)
    echo "$d: $nf .py files, $lc LOC"
  fi
done
