"""
ui/prompts.py

ApprovalInputHandler — a daemon stdin reader that turns user keystrokes
into actions on the running CryptoBot. Runs alongside Rich Live so the
dashboard owns the alternate screen buffer while we own line-buffered
stdin in a parallel thread.

Commands:
  a / approve — approve the next pending signal (per_trade mode)
  s / skip    — skip the next pending signal
  k / kill    — fire the kill switch (close everything)
  q / quit    — clean shutdown (final snapshot + agent_events row)

The handler bridges threading→asyncio via run_coroutine_threadsafe so
all of the bot's state mutations stay on its event loop.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)


_HELP = "Commands: a=approve  s=skip  k=kill  q=quit"


class ApprovalInputHandler:
    """Reads stdin in a daemon thread; dispatches actions onto the bot's
    asyncio loop. Construct after the loop is running and call start()."""

    def __init__(self, bot, loop: Optional[asyncio.AbstractEventLoop] = None):
        self._bot = bot
        self._loop = loop or asyncio.get_event_loop()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ── Lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the reader thread. daemon=True so a stuck stdin read
        never blocks process exit."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="approval-input", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to exit on its next iteration. The thread
        is daemon so this is best-effort — fine for shutdown paths."""
        self._stop.set()

    # ── Reader loop ─────────────────────────────────────────────────────

    def _run(self) -> None:
        """Block on stdin line-by-line until quit is requested or
        stdin closes (EOF)."""
        for raw in sys.stdin:
            if self._stop.is_set():
                return
            cmd = (raw or "").strip().lower()
            if not cmd:
                continue
            try:
                self._dispatch(cmd)
            except Exception as e:
                logger.warning(f"approval-input dispatch failed: {e}")
            if cmd in ("q", "quit"):
                return

    def _dispatch(self, cmd: str) -> None:
        if cmd in ("a", "approve"):
            self._call_coro(self._bot.approve_next_pending())
        elif cmd in ("s", "skip"):
            self._call_coro(self._bot.skip_next_pending("user_skipped"))
        elif cmd in ("k", "kill"):
            self._call_coro(self._bot.trigger_kill_switch("user_command"))
        elif cmd in ("q", "quit"):
            self._call_coro(self._bot.shutdown("user_quit"))
        else:
            print(_HELP)

    def _call_coro(self, coro) -> None:
        """Submit a coroutine to the bot's loop. Blocks briefly for the
        result so errors surface in the terminal rather than vanishing
        into the loop's exception handler."""
        if self._loop is None or not self._loop.is_running():
            print("(loop not running — command ignored)")
            return
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            future.result(timeout=15)
        except Exception as e:
            print(f"command failed: {e}")
