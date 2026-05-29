---

## Appendix A — Full file inventory

193 files. See `file_inventory.csv` (in this directory) for the machine-readable form with columns: `path, size_bytes, line_count, sha256, language, role`.

Role distribution:

```
113 source
 30 test
 25 prompt
 10 docs
  8 config
  5 script
  2 other
```

Top 20 files by line count (Python only):

| File | LOC |
|------|----:|
| `ui/dashboard.py` | 2007 |
| `prompts/build_scalping_agent.md` | 1391 (markdown, included for size context) |
| `ui/web_server.py` | 1363 |
| `config/settings.py` | 1269 |
| `ui/web_dashboard.html` | 1291 (HTML, included for size context) |
| `core/bot.py` | (large; see file_inventory.csv) |
| `database/queries.py` | (large) |
| `database/models.py` | (large) |
| `signals/quality_gate.py` | (medium) |
| `execution/arb_engine.py` | (medium) |

For the canonical list, sort `file_inventory.csv` by `line_count`.

## Appendix B — Pytest output (head + tail)

Full output: `pytest_output.txt`.

Head:

```
============================= test session starts =============================
platform linux -- Python 3.11.15, pytest-9.0.3, pluggy-1.6.0
rootdir: /home/gabriel/cryptobot
configfile: pytest.ini
testpaths: tests
plugins: asyncio-1.3.0, mock-3.15.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None
collected 552 items
```

Tail:

```
tests/test_web_server.py::test_rebalance_blocked_when_balance_agent_missing PASSED [ 99%]
tests/test_web_server.py::test_rebalance_logs_event_on_confirm PASSED    [ 99%]
tests/test_web_server.py::test_rebalance_confirm_replan_mismatch PASSED  [100%]

=============================== warnings summary ===============================
tests/test_chain_connectors.py::test_is_available_false_without_rpc_env_var
  /home/gabriel/cryptobot/venv/lib/python3.11/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html for upgrade instructions
    warnings.warn(  # deprecated in 14.0 - 2024-11-09

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
======================= 552 passed, 1 warning in 12.12s ========================
```

## Appendix C — Git state and recent commits

Verbatim from `git_state.txt`:

