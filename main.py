"""
main.py
Entry point. Boots the bot, starts the dashboard, runs the main loop.

Usage:
    python main.py                        # Use settings.py defaults
    python main.py --profile conservative
    python main.py --strategy arb_only
    python main.py --sim                  # Force sim mode
    python main.py --live                 # Force live mode (careful)
"""

import asyncio
import logging
import sys
import click
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from database.db import init_db
from database.queries import log_circuit_breaker
from profiles.profile_manager import profile_manager
from strategies import get_strategy
from execution.kill_switch import KillSwitch
from utils.logger import setup_logging


@click.command()
@click.option("--profile",  default=None, help="Risk profile: conservative | balanced | aggressive | custom")
@click.option("--strategy", default=None, help="Strategy: default | arb_only | scalper | custom")
@click.option("--sim",      is_flag=True, help="Force simulation mode")
@click.option("--live",     is_flag=True, help="Force live trading mode")
@click.option("--debug",    is_flag=True, help="Enable debug logging")
def main(profile, strategy, sim, live, debug):
    """CryptoBot — Claude-powered situational trading assistant."""

    # Logging
    setup_logging(debug=debug)
    logger = logging.getLogger(__name__)

    # Mode
    if sim:
        settings.SIM_MODE = True
    elif live:
        settings.SIM_MODE = False
        logger.warning("⚠️  LIVE MODE ENABLED — real money at risk")

    # Profile
    active_profile_name = profile or settings.ACTIVE_PROFILE
    active_profile = profile_manager.load(active_profile_name)

    # Strategy
    active_strategy_name = strategy or settings.ACTIVE_STRATEGY
    active_strategy = get_strategy(active_strategy_name)

    # DB
    init_db()

    logger.info(f"Starting CryptoBot")
    logger.info(f"  Mode:     {'SIM' if settings.SIM_MODE else 'LIVE'}")
    logger.info(f"  Profile:  {active_profile_name}")
    logger.info(f"  Strategy: {active_strategy_name}")

    # Boot the async event loop
    try:
        asyncio.run(_run(active_profile, active_strategy))
    except KeyboardInterrupt:
        logger.info("Shutting down (Ctrl+C)")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


async def _run(profile, strategy):
    """Async main — imports are deferred here to keep startup fast."""
    from core.bot import CryptoBot

    kill_switch = KillSwitch(sim_mode=settings.SIM_MODE)
    bot = CryptoBot(profile=profile, strategy=strategy, kill_switch=kill_switch)

    await bot.start()


if __name__ == "__main__":
    main()
