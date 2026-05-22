"""
tests/test_prompts.py — ApprovalInputHandler dispatch + command-bar
state mirroring.

The handler is a daemon thread in production. These tests exercise the
dispatch path directly (no real stdin) and assert it (a) calls the
expected bot coroutine and (b) writes the command-bar state the
dashboard panel reads.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ui.prompts import ApprovalInputHandler


def _fake_bot():
    """Bot-shaped stub with the surface the handler touches."""
    return SimpleNamespace(
        approve_next_pending = MagicMock(return_value=None),
        skip_next_pending    = MagicMock(return_value=None),
        trigger_kill_switch  = MagicMock(return_value=None),
        shutdown             = MagicMock(return_value=None),
        approve_window       = MagicMock(return_value=None),
        toggle_pause         = MagicMock(return_value=True),
        _cmd_current     = "",
        _cmd_last_result = "",
        _cmd_last_ts     = None,
    )


def _handler_for(bot) -> ApprovalInputHandler:
    """Construct the handler with the bridge-coroutine method neutered —
    we want to verify dispatch logic without spinning up a real loop."""
    h = ApprovalInputHandler(bot)
    h._call_coro = MagicMock()
    return h


def test_help_command_logs_help_and_records_state(caplog):
    bot = _fake_bot()
    h = _handler_for(bot)
    with caplog.at_level("INFO"):
        result = h._dispatch("h")
    assert result == "help"
    assert any("Commands:" in r.message for r in caplog.records)


def test_pause_command_toggles_and_returns_label():
    bot = _fake_bot()
    bot.toggle_pause = MagicMock(return_value=True)
    h = _handler_for(bot)
    assert h._dispatch("p") == "paused"
    bot.toggle_pause.assert_called_once()
    bot.toggle_pause = MagicMock(return_value=False)
    assert h._dispatch("pause") == "resumed"


def test_window_command_calls_approve_window_with_settings_default():
    from config import settings
    bot = _fake_bot()
    h = _handler_for(bot)
    result = h._dispatch("w")
    bot.approve_window.assert_called_once_with(
        int(settings.WINDOW_DEFAULT_DURATION_MINUTES),
    )
    assert result.startswith("window approved")


def test_unknown_command_returns_unknown_label(caplog):
    bot = _fake_bot()
    h = _handler_for(bot)
    with caplog.at_level("INFO"):
        result = h._dispatch("xyzzy")
    assert result.startswith("unknown: xyzzy")
    assert any("Unknown command" in r.message for r in caplog.records)


def test_run_loop_writes_cmd_state_per_line(monkeypatch):
    """Drive _run() with a stubbed stdin — every accepted line must
    leave bot._cmd_last_result + _cmd_last_ts populated."""
    bot = _fake_bot()
    h = _handler_for(bot)
    # Feed two lines then EOF.
    monkeypatch.setattr("sys.stdin", iter(["h\n", "p\n"]))
    h._run()
    assert bot._cmd_last_result in ("paused", "resumed")
    assert bot._cmd_last_ts is not None
    # Current input is cleared after each dispatch.
    assert bot._cmd_current == ""


def test_run_loop_exits_on_quit(monkeypatch):
    bot = _fake_bot()
    h = _handler_for(bot)
    monkeypatch.setattr("sys.stdin", iter(["q\n", "a\n"]))   # 'a' after 'q'
    h._run()
    # Quit was the last command — approve never got dispatched.
    h._call_coro.assert_called_once()       # only the shutdown coroutine
    assert bot._cmd_last_result == "quit"
