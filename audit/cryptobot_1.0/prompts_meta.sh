#!/usr/bin/env bash
set -u
cd "$HOME/cryptobot"
for f in prompts/*.md; do
  lc=$(wc -l < "$f")
  mt=$(stat -c '%y' "$f" | cut -c1-19)
  # First substantive line — skip blanks and # heading markdown markers later
  hdr=$(awk 'NF{print; exit}' "$f")
  # Try to find a "build_<target>" target hint
  echo "=== $f | $lc lines | mtime=$mt ==="
  echo "HEADER: $hdr"
done
