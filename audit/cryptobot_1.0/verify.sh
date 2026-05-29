#!/usr/bin/env bash
set -u
cd "$HOME/cryptobot"
OUT="audit/cryptobot_1.0/verification.txt"
{
  echo "=== Verification pass ==="
  echo

  echo "--- 1. Every .py in source packages is mentioned somewhere in audit ---"
  miss=0
  while IFS= read -r f; do
    # Search in module_reports and the master doc
    base=$(basename "$f")
    rel="$f"
    if ! grep -lq -- "$rel" audit/cryptobot_1.0/CRYPTOBOT_1.0_AUDIT.md audit/cryptobot_1.0/module_reports/*.md 2>/dev/null; then
      # Try matching just the basename if path form not found
      if ! grep -lq -- "$base" audit/cryptobot_1.0/CRYPTOBOT_1.0_AUDIT.md audit/cryptobot_1.0/module_reports/*.md 2>/dev/null; then
        echo "  MISSING: $rel"
        miss=$((miss+1))
      fi
    fi
  done < <(find core agents execution sentiment ui database data_sources macro signals strategies utils profiles predictive config scalping_v2 -type f -name '*.py' -not -path '*/__pycache__/*' 2>/dev/null | sort)
  echo "Result: $miss .py files not mentioned"
  echo

  echo "--- 2. No literal 'TODO: fill in' or unresolved tokens in master doc ---"
  if grep -nE "TODO: fill in|_filled in at end_|XXX TODO|FIXME: TODO|<<< |^TODO$" audit/cryptobot_1.0/CRYPTOBOT_1.0_AUDIT.md; then
    echo "  WARN: unresolved tokens above"
  else
    echo "  OK"
  fi
  echo

  echo "--- 3. REGISTERED_* lists each have an inventory entry ---"
  for reg in $(grep -roEh "REGISTERED_[A-Z_]+" --include='*.py' --exclude-dir=venv --exclude-dir=.git --exclude-dir=audit | sort -u); do
    if grep -q "$reg" audit/cryptobot_1.0/CRYPTOBOT_1.0_AUDIT.md; then
      echo "  OK   $reg"
    else
      echo "  MISS $reg"
    fi
  done
  echo

  echo "--- 4. Every table in database/models.py appears in DB audit ---"
  for t in $(grep -oE '__tablename__\s*=\s*"[a-z_]+"' database/models.py | sed -E 's/.*"([a-z_]+)".*/\1/' | sort -u); do
    if grep -q "$t" audit/cryptobot_1.0/db_schema.md audit/cryptobot_1.0/module_reports/database.md; then
      echo "  OK   $t"
    else
      echo "  MISS $t"
    fi
  done
  echo

  echo "--- 5. Every settings.py constant appears in settings_audit.md ---"
  # Sample: pick 20 random constants and check
  miss=0
  total=0
  for c in $(grep -oE '^[A-Z_][A-Z0-9_]+\s*=' config/settings.py | sed 's/[ =].*//' | sort -u); do
    total=$((total+1))
    if ! grep -q "\`$c\`\|^| $c \|$c |" audit/cryptobot_1.0/settings_audit.md 2>/dev/null; then
      if ! grep -q "$c" audit/cryptobot_1.0/settings_audit.md; then
        miss=$((miss+1))
        # Cap reporting at 15 misses
        if [ "$miss" -le 15 ]; then
          echo "  MISS $c"
        fi
      fi
    fi
  done
  echo "Result: $miss / $total settings constants not found in settings_audit.md"
  echo

  echo "--- 6. Master doc line count ---"
  wc -l audit/cryptobot_1.0/CRYPTOBOT_1.0_AUDIT.md
} > "$OUT" 2>&1
echo "Verification written to $OUT"
wc -l "$OUT"
