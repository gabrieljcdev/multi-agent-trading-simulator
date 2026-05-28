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

# Load keys.env BEFORE importing anything that gates on env vars at module
# load time (notably the agent registry — CrossChainArbAgent.is_available()
# checks ARBITRUM_RPC_URL / BASE_RPC_URL / OPTIMISM_RPC_URL when the
# coordinator inspects REGISTERED_AGENTS at startup). Other modules also
# call load_dotenv lazily, but lazy is too late for the coordinator's
# initial availability sweep — by then the agent has already been logged
# as "unavailable" and skipped.
from dotenv import load_dotenv as _load_dotenv     # noqa: E402
_load_dotenv(Path(__file__).parent / "config" / "keys.env")

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

    # Long-running components become tracked tasks so shutdown can cancel them.
    component_tasks = [asyncio.ensure_future(coordinator.start())]

    if dashboard:
        from ui.dashboard import Dashboard
        dash = Dashboard(coordinator=coordinator)
        coordinator.set_dashboard(dash)
        component_tasks.append(asyncio.ensure_future(dash.run()))

    # Web control panel — start() returns once the aiohttp runner is up (its
    # teardown is web_server.stop()). Never let its startup crash the bot.
    web_server = None
    if web_ui:
        try:
            settings.WEB_UI_ENABLED = True
            from ui.web_server import WebServer
            web_server = WebServer(coordinator=coordinator, bot=None)
            await web_server.start()
        except Exception as e:
            logger.error(f"Web UI failed to start: {e}", exc_info=True)
            web_server = None

    # Coordinated shutdown. SIGINT/SIGTERM set an event; we then stop the
    # coordinator (which stops every agent) and the web server under a bounded
    # timeout, then force-cancel anything still running. Previously SIGINT was
    # handled inside the signal bot and only stopped its own loop, leaving the
    # coordinator, the other agents, the data/macro refresh loops, and the web
    # server alive — the process hung until SIGKILL (exit 9).
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    import signal as _signal

    def _request_stop(signame: str):
        logger.info(f"Shutdown requested ({signame})")
        stop_event.set()

    for _sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(_sig, _request_stop, _sig.name)
        except (NotImplementedError, ValueError, RuntimeError):
            # Windows / non-main thread: fall back to KeyboardInterrupt in main()
            pass

    # Wake when shutdown is requested OR a component exits on its own (crash).
    stop_waiter = asyncio.ensure_future(stop_event.wait())
    try:
        await asyncio.wait([*component_tasks, stop_waiter],
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop_waiter.cancel()
        timeout = settings.SHUTDOWN_TIMEOUT_SEC
        logger.info("Shutting down — stopping agents and web server")
        try:
            await asyncio.wait_for(coordinator.stop(), timeout)
        except asyncio.TimeoutError:
            logger.warning("coordinator.stop() timed out — forcing cancel")
        except Exception as e:
            logger.warning(f"coordinator.stop: {e}")
        if web_server is not None:
            try:
                await asyncio.wait_for(web_server.stop(), timeout)
            except Exception as e:
                logger.debug(f"web server stop: {e}")
        # Force-cancel any component still running, bounded so a wedged task
        # can't hold the process open.
        for t in component_tasks:
            t.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*component_tasks, return_exceptions=True), timeout)
        except asyncio.TimeoutError:
            logger.warning("some components did not cancel in time")
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
