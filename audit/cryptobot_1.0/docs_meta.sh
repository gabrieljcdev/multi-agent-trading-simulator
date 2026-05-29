#!/usr/bin/env bash
set -u
cd "$HOME/cryptobot"
DOCS=(
  CLAUDE.md
  GLOSSARY.md
  PLUGIN_PATTERN.md
  README.md
  RUNBOOK.md
  WEB_UI.md
  scalping_v2/RUNBOOK_v2_rollback_section.md
  scalping_v2/SCALPING_V2.md
  scalping_v2/SCALPING_V2_RECALIBRATION.md
  scalping_v2/SCALPING_V3_ROADMAP.md
)
for f in "${DOCS[@]}"; do
  [ -f "$f" ] || continue
  mt=$(stat -c '%y' "$f" 2>/dev/null | cut -c1-19)
  lc=$(wc -l < "$f" 2>/dev/null)
  # Skip leading frontmatter-like blank lines for the snippet
  first=$(awk 'NF && !/^---/{print; n++; if (n>=4) exit}' "$f" | tr '\n' ' ' | sed 's/  */ /g' | cut -c1-260)
  echo "=== $f | $lc lines | mtime=$mt ==="
  echo "FIRST: $first"
  echo
done
