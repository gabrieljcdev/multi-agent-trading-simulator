"""Idempotent migration — add the scalp v2 columns to scalp_observations.

SQLAlchemy's create_all() creates missing tables but never ALTERs an existing
one, so a database that predates v2 needs these columns added explicitly. Safe
to run repeatedly: it skips columns that already exist. Fresh databases get the
columns from the model via init_db() and this becomes a no-op.

Run:  PYTHONPATH=. venv/bin/python scripts/migrate_scalp_v2.py
"""
import sqlite3

from config import settings

V2_COLUMNS = [
    ("confluence_score",      "INTEGER"),
    ("strength_label",        "TEXT"),
    ("cross_exchange_agrees", "BOOLEAN"),
    ("btc_compatible",        "BOOLEAN"),
    ("adverse_selection_ok",  "BOOLEAN"),
    ("depth_ok",              "BOOLEAN"),
    ("vwap_aligned",          "BOOLEAN"),
    ("htf_aligned",           "BOOLEAN"),
    ("volume_adequate",       "BOOLEAN"),
    ("atr_bps",               "REAL"),
    ("atr_adjusted",          "BOOLEAN"),
    ("sl_clamped",            "TEXT"),
    ("rr_actual",             "REAL"),
]


def migrate(db_path: str) -> list:
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scalp_observations'"
        )
        if not cur.fetchone():
            return []   # table will be created by init_db with the columns present
        existing = {row[1] for row in cur.execute("PRAGMA table_info(scalp_observations)")}
        added = []
        for name, typ in V2_COLUMNS:
            if name not in existing:
                cur.execute(f"ALTER TABLE scalp_observations ADD COLUMN {name} {typ}")
                added.append(name)
        con.commit()
        return added
    finally:
        con.close()


if __name__ == "__main__":
    db = str(settings.DB_PATH)
    added = migrate(db)
    print(f"DB: {db}")
    print(f"added {len(added)} v2 columns: {added}" if added
          else "all v2 columns already present (no-op)")
