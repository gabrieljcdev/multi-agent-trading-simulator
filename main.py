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
@click.option("--profile",   default=None, help="Risk profile: conservative | balanced | aggressive | custom")
@click.option("--strategy",  default=None, help="Strategy: default | arb_only | scalper | custom")
@click.option("--sim",       is_flag=True, help="Force simulation mode")
@click.option("--live",      is_flag=True, help="Force live trading mode")
@click.option("--debug",     is_flag=True, help="Enable debug logging")
@click.option("--dashboard", is_flag=True, help="Run the Rich terminal dashboard alongside the bot")
@click.option("--web-ui", "web_ui", is_flag=True, help="Start web control panel on localhost:8765")
def main(profile, strategy, sim, live, debug, dashboard, web_ui):
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
    logger.info(f"  Dashboard: {'on' if dashboard else 'off'}")
    logger.info(f"  Web UI:    {'on' if web_ui else 'off'}")

    # Boot the async event loop
    try:
        asyncio.run(_run(active_profile, active_strategy, dashboard, web_ui))
    except KeyboardInterrupt:
        logger.info("Shutting down (Ctrl+C)")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


async def _run(profile, strategy, dashboard: bool, web_ui: bool = False):
    """Async main — imports are deferred here to keep startup fast.

    The Coordinator owns every agent (incl. SignalAgent which wraps
    CryptoBot). Dashboard / web UI, if enabled, late-bind to the signal
    agent's bot once the agent constructs it.

    `profile` and `strategy` apply to the signal agent only; we set them
    on settings so SignalAgentWrapper picks them up when it constructs
    its CryptoBot. (Bypassing the wrapper to inject profile/strategy
    directly would break agent encapsulation.)
    """
    logger = logging.getLogger(__name__)
    from agents.coordinator import Coordinator

    if profile is not None:
        settings.ACTIVE_PROFILE = profile.name if hasattr(profile, "name") else profile
    if strategy is not None:
        settings.ACTIVE_STRATEGY = strategy.name if hasattr(strategy, "name") else strategy

    coordinator = Coordinator()

    tasks = [coordinator.start()]

    if dashboard:
        from ui.dashboard import Dashboard
        dash = Dashboard(coordinator=coordinator)
        coordinator.set_dashboard(dash)
        tasks.append(dash.run())

    # Web control panel — never let its startup crash the bot.
    web_server = None
    if web_ui:
        try:
            settings.WEB_UI_ENABLED = True
            from ui.web_server import WebServer
            web_server = WebServer(coordinator=coordinator, bot=None)
            tasks.append(web_server.start())
        except Exception as e:
            logger.error(f"Web UI failed to start: {e}", exc_info=True)
            web_server = None

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if web_server is not None:
            try:
                await web_server.stop()
            except Exception as e:
                logger.debug(f"web server stop: {e}")


if __name__ == "__main__":
    main()
