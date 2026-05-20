"""
utils/logger.py
Structured logging setup. Logs to console and rotating file.
"""

import logging
import logging.handlers
from pathlib import Path
from config.settings import LOGS_DIR, LOG_LEVEL, LOG_TO_FILE


def setup_logging(debug: bool = False):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if debug else getattr(logging, LOG_LEVEL)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)-25s  %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler (clean, INFO+ only)
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # File handler
    if LOG_TO_FILE:
        fh = logging.handlers.TimedRotatingFileHandler(
            LOGS_DIR / "cryptobot.log",
            when="midnight",
            backupCount=30,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # Silence noisy third-party libs
    for noisy in ["ccxt", "asyncio", "urllib3", "telethon", "praw"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)
