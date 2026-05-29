#!/usr/bin/env bash
set -eu
cd "$HOME/cryptobot/audit/cryptobot_1.0"
OUT="CRYPTOBOT_1.0_AUDIT.md"

# Build the master doc
{
  cat _master_header.md
  echo
  echo "## Module-by-module reports"
  echo
  for f in core agents execution signals strategies database \
           sentiment data_sources macro \
           ui profiles utils predictive scalping_v2 \
           config_and_root; do
    if [ -f "module_reports/${f}.md" ]; then
      echo
      echo "---"
      echo
      cat "module_reports/${f}.md"
    fi
  done
  cat _master_middle.md
  cat settings_audit.md
  echo
  echo "---"
  echo
  echo "## Database"
  echo
  echo "The following two artefacts are inlined: the per-table ORM + query audit (\`module_reports/database.md\`) and the live SQLite snapshot summary (\`db_schema.md\`). The full live \`.schema\` and row counts are in \`sqlite_state.txt\`."
  echo
  cat db_schema.md
  cat _master_tests.md
  cat docs_inventory.md
  echo
  echo "---"
  echo
  echo "## Build prompts inventory"
  echo
  cat prompts_inventory.md
  echo
  echo "---"
  echo
  echo "## Dependencies"
  echo
  cat deps_audit.md
  echo
  echo "---"
  echo
  echo "## Cross-cutting concerns"
  echo
  cat cross_cutting.md
  cat _master_known_todos.md
  cat restore/RESTORE_PROCEDURE.md
  cat _master_appendix_intro.md
  echo
  echo "\`\`\`"
  cat git_state.txt
  echo "\`\`\`"
  echo
  echo "---"
  echo
  # Footer placeholder — line count gets filled in below.
  echo "**Document line count:** _filled in at end_"
} > "$OUT"

# Now compute the actual line count and patch the footer.
LC=$(wc -l < "$OUT")
sed -i "s/_filled in at end_/${LC}/" "$OUT"

echo "Wrote $OUT — $(wc -l < "$OUT") lines, $(wc -c < "$OUT") bytes"
