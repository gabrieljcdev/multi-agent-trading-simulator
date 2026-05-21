"""
database/db.py
Database connection, session management, and initialisation.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from pathlib import Path
import logging

from .models import Base
from config.settings import DB_PATH

logger = logging.getLogger(__name__)

# Ensure data directory exists
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False,
)

# Enable WAL mode for better concurrent read performance
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    # Existing query helpers return ORM rows out of `with get_session()`
    # blocks (queries.get_open_trades, get_data_history, …). The default
    # expire_on_commit=True would mark every attribute stale at commit,
    # making `row.value` raise DetachedInstanceError as soon as the
    # session closes. Keeping attributes populated post-commit matches
    # how consumers (dashboard panels, tests) actually use the rows.
    expire_on_commit=False,
)


def init_db():
    """Create all tables if they don't exist. Safe to call on every startup."""
    Base.metadata.create_all(bind=engine)
    logger.info(f"Database initialised at {DB_PATH}")


@contextmanager
def get_session() -> Session:
    """Context manager for database sessions with automatic cleanup."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
