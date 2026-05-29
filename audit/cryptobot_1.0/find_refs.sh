#!/usr/bin/env bash
set -u
cd "$HOME/cryptobot"
for f in CLAUDE.md README.md RUNBOOK.md WEB_UI.md GLOSSARY.md PLUGIN_PATTERN.md \
         scalping_v2/RUNBOOK_v2_rollback_section.md scalping_v2/SCALPING_V2.md \
         scalping_v2/SCALPING_V2_RECALIBRATION.md scalping_v2/SCALPING_V3_ROADMAP.md; do
  echo "=== Refs in $f ==="
  if [ -f "$f" ]; then
    grep -oE 'SCALPING_V[23][A-Z_]*\.md|RUNBOOK_v2[A-Za-z_]*\.md|scalping_v2/[A-Za-z_0-9]+\.md|SCALPING_V2_RECALIBRATION\.md|scalping_agent_doc\.docx' "$f" 2>/dev/null | sort -u
  fi
  echo
done
