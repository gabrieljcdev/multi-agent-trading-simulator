"""Idempotent migration — add the funding-frontier columns to
funding_arb_observations.

SQLAlchemy's create_all() creates missing tables but never ALTERs an existing
one, so a database that predates the funding-frontier work (cross-venue carry,
long-tail/HIP-3 discovery, openness signal) needs these columns added
explicitly. Safe to run repeatedly: it skips columns that already exist. Fresh
databases get the columns from the model via init_db() and this becomes a no-op.

Run:  PYTHONPATH=. venv/bin/python scripts/migrate_funding_frontier.py
"""
import sqlite3

from config import settings

FRONTIER_COLUMNS = [
    ("legs",                     "TEXT"),
    ("funding_interval_sec",     "REAL"),
    ("taker_fee_bps",            "REAL"),
    ("maker_fee_bps",            "REAL"),
    ("is_long_tail",             "BOOLEAN"),
    ("is_hip3",                  "BOOLEAN"),
    ("pair_age_days",            "REAL"),
    ("spread_decay_bps_per_day", "REAL"),
    ("oi_growth_pct_24h",        "REAL"),
    ("crowding_verdict",         "TEXT"),
]


def migrate(db_path: str) -> list:
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='funding_arb_observations'"
        )
        if not cur.fetchone():
            return []   # table will be created by init_db with the columns present
        existing = {
            row[1] for row in cur.execute("PRAGMA table_info(funding_arb_observations)")
        }
        added = []
        for name, typ in FRONTIER_COLUMNS:
            if name not in existing:
                cur.execute(
                    f"ALTER TABLE funding_arb_observations ADD COLUMN {name} {typ}"
                )
                added.append(name)
        con.commit()
        return added
    finally:
        con.close()


if __name__ == "__main__":
    db = str(settings.DB_PATH)
    added = migrate(db)
    print(f"DB: {db}")
    print(f"added {len(added)} frontier columns: {added}" if added
          else "all frontier columns already present (no-op)")
