"""Run every SQL block in scalping_v2/SCALPING_V2_RECALIBRATION.md against the
live scalp database and report which execute cleanly.

The cookbook is the single source of truth for the queries; this verifies they
stay valid against the (migrated) scalp_observations schema. Use it weekly per
the cookbook, or as a post-migration check.

Run:  PYTHONPATH=. venv/bin/python scripts/run_recalibration.py
"""
import re
import sqlite3
import sys
from pathlib import Path

from config import settings

DOC = Path(__file__).resolve().parent.parent / "scalping_v2" / "SCALPING_V2_RECALIBRATION.md"


def extract_statements(md_text: str) -> list:
    """Return the individual SQL statements from every ```sql fenced block,
    stripping -- line comments and splitting on ';'."""
    out = []
    for block in re.findall(r"```sql\n(.*?)```", md_text, re.DOTALL):
        for raw in block.split(";"):
            lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
            stmt = "\n".join(lines).strip()
            if stmt:
                out.append(stmt)
    return out


def run(db_path: str, statements: list) -> tuple:
    con = sqlite3.connect(db_path)
    ok, fails = 0, []
    try:
        for i, stmt in enumerate(statements, 1):
            try:
                con.execute(stmt).fetchall()
                ok += 1
            except Exception as e:
                fails.append((i, str(e), stmt[:90].replace("\n", " ")))
    finally:
        con.close()
    return ok, fails


def main() -> int:
    statements = extract_statements(DOC.read_text(encoding="utf-8"))
    ok, fails = run(str(settings.DB_PATH), statements)
    print(f"DB: {settings.DB_PATH}")
    print(f"recalibration SQL: {ok}/{len(statements)} statements executed cleanly")
    for i, err, head in fails:
        print(f"  FAIL #{i}: {err}\n    {head}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
