"""Idempotent migration — add the OpportunityScannerAgent tables.

The six tables (opportunity_core, liquidation_detail, funding_detail,
lp_detail, launch_detail, opportunity_observation) are entirely new, so
SQLAlchemy's create_all() (init_db) creates them on fresh AND existing
databases alike — this script exists for two reasons:

  1. the project convention that schema changes ship with an explicit,
     re-runnable migration (mirrors scripts/migrate_funding_frontier.py;
     there is no Alembic infrastructure in this repo), and
  2. forward-compat: if a future build adds COLUMNS to these tables,
     the ALTER logic slots in here next to the existing-column check.

Safe to run repeatedly: create_all skips tables that already exist, and
the column check below ALTERs only what is missing.

Run:  PYTHONPATH=. venv/bin/python scripts/migrate_opportunity_scanner.py
"""
import sqlite3

from config import settings

OPPORTUNITY_TABLES = [
    "opportunity_core",
    "liquidation_detail",
    "funding_detail",
    "lp_detail",
    "launch_detail",
    "opportunity_observation",
]

# (table, column, sqlite type) — empty this pass (the tables are new);
# future column additions to existing installs go here.
OPPORTUNITY_COLUMNS: list[tuple[str, str, str]] = []


def migrate(db_path: str) -> dict:
    """Create missing opportunity tables + columns. Returns
    {"tables_created": [...], "columns_added": [...]}."""
    # Tables: lean on the ORM metadata so this can never drift from
    # database/models.py.
    from sqlalchemy import create_engine, inspect
    from database.models import Base

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        before = set(inspect(engine).get_table_names())
        Base.metadata.create_all(bind=engine)
        after = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    created = sorted((after - before) & set(OPPORTUNITY_TABLES))

    # Columns (future-proofing — no-op this pass).
    added: list[str] = []
    if OPPORTUNITY_COLUMNS:
        con = sqlite3.connect(db_path)
        try:
            cur = con.cursor()
            for table, name, typ in OPPORTUNITY_COLUMNS:
                existing = {row[1] for row in
                            cur.execute(f"PRAGMA table_info({table})")}
                if name not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                    added.append(f"{table}.{name}")
            con.commit()
        finally:
            con.close()
    return {"tables_created": created, "columns_added": added}


if __name__ == "__main__":
    db = str(settings.DB_PATH)
    result = migrate(db)
    print(f"DB: {db}")
    print(f"created {len(result['tables_created'])} tables: "
          f"{result['tables_created'] or '(all present — no-op)'}")
    if result["columns_added"]:
        print(f"added columns: {result['columns_added']}")
