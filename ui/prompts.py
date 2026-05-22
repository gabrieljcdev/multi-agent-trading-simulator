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
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


_HELP = (
    "Commands:  a=approve  s=skip  w=window  k=kill  p=pause  "
    "q=quit  h=help  (Ctrl+C also kills)"
)


class ApprovalInputHandler:
    """Reads stdin in a daemon thread; dispatches actions onto the bot's
    asyncio loop. Construct after the loop is running and call start()."""

    def __init__(self, bot, loop: Optional[asyncio.AbstractEventLoop] = None):
        self._bot = bot
        # Loop lookup is deferred to _call_coro — calling
        # asyncio.get_event_loop() at construction time raises in 3.10+
        # when no loop is running (tests, REPL).
        self._loop: Optional[asyncio.AbstractEventLoop] = loop
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
            # Echo the submitted line into the bot's command-bar state
            # so the dashboard panel can render it on the next tick.
            self._set_state(current=cmd)
            try:
                result = self._dispatch(cmd)
            except Exception as e:
                logger.warning(f"approval-input dispatch failed: {e}")
                result = f"error: {e}"
            self._set_state(current="", last_result=result or cmd)
            if cmd in ("q", "quit"):
                return

    def _dispatch(self, cmd: str) -> Optional[str]:
        """Run the user's command. Returns a short human-readable label
        for the command-bar 'last result' line, or None when the
        command's own coroutine handles the messaging."""
        if cmd in ("a", "approve"):
            self._call_coro(self._bot.approve_next_pending())
            return "approve"
        if cmd in ("s", "skip"):
            self._call_coro(self._bot.skip_next_pending("user_skipped"))
            return "skip"
        if cmd in ("w", "window"):
            # Default window duration from settings — keeps everything
            # configurable, no magic numbers.
            from config import settings as _s
            dur = int(_s.WINDOW_DEFAULT_DURATION_MINUTES)
            try:
                self._bot.approve_window(dur)
            except Exception as e:
                logger.warning(f"approve_window failed: {e}")
                return f"window failed: {e}"
            return f"window approved ({dur}m)"
        if cmd in ("k", "kill"):
            self._call_coro(self._bot.trigger_kill_switch("user_command"))
            return "kill switch"
        if cmd in ("p", "pause"):
            try:
                paused = self._bot.toggle_pause()
            except Exception as e:
                logger.warning(f"toggle_pause failed: {e}")
                return f"pause failed: {e}"
            return "paused" if paused else "resumed"
        if cmd in ("q", "quit"):
            self._call_coro(self._bot.shutdown("user_quit"))
            return "quit"
        if cmd in ("h", "help", "?"):
            # Log path — shows up in the dashboard's LOG panel + stdout.
            logger.info(_HELP)
            return "help"
        # Unknown command — print to the log panel too so the user sees
        # the vocabulary without having to look at the cmd-bar hint strip.
        logger.info(f"Unknown command '{cmd}'. {_HELP}")
        return f"unknown: {cmd}"

    def _set_state(
        self,
        current: Optional[str] = None,
        last_result: Optional[str] = None,
    ) -> None:
        """Mirror command-bar state onto the bot so the dashboard panel
        can read it. Best-effort — if the bot lacks the attributes
        (older code / tests) we just no-op."""
        try:
            if current is not None:
                self._bot._cmd_current = current
            if last_result is not None:
                self._bot._cmd_last_result = last_result
                self._bot._cmd_last_ts     = datetime.utcnow().strftime("%H:%M:%S")
        except Exception:
            pass

    def _call_coro(self, coro) -> None:
        """Submit a coroutine to the bot's loop. Blocks briefly for the
        result so errors surface in the terminal rather than vanishing
        into the loop's exception handler."""
        loop = self._loop
        if loop is None:
            # Resolve on first dispatch — `_run` is invoked from a
            # daemon thread, so the running loop lives on the main task.
            try:
                loop = asyncio.get_event_loop_policy().get_event_loop()
            except Exception:
                loop = None
            self._loop = loop
        if loop is None or not loop.is_running():
            print("(loop not running — command ignored)")
            return
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            future.result(timeout=15)
        except Exception as e:
            print(f"command failed: {e}")
