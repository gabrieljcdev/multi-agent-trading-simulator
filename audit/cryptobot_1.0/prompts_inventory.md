# Build prompts inventory — CryptoBot 1.0

25 files in `prompts/`. Each is a markdown brief used to drive a Claude Code build session. Mapping below shows which packages each prompt targets and whether those modules exist in the current codebase.

## Executed and shipped

| Prompt | Lines | mtime | Target module(s) | Status |
|--------|------:|-------|------------------|--------|
| `build_bot_loop.md` | 81 | 2026-05-20 10:03 | `core/`, `signals/`, `execution/`, `database/`, `ui/` | **shipped** — every target module exists; `core/bot.py` is the result. |
| `build_arb_engine.md` | 312 | 2026-05-20 11:54 | `execution/arb_engine.py`, `agents/__init__.py`, `agents/base.py` | **shipped** — arb engine present (533 LOC) with full ArbEngine class. |
| `build_coordinator.md` | 302 | 2026-05-20 11:37 | `agents/coordinator.py` | **shipped** — Coordinator owns every agent; main.py uses it. |
| `build_dashboard.md` | 223 | 2026-05-20 10:22 | `ui/dashboard.py` | **shipped** — 2007 LOC Rich dashboard. |
| `build_sentiment.md` | 342 | 2026-05-20 10:36 | `sentiment/` | **shipped** — 5 sources registered (fear_greed, cryptopanic, reddit, google_trends, telegram). |
| `build_data_sources.md` | 649 | 2026-05-21 10:58 | `data_sources/` | **shipped** — 13 sources registered. |
| `build_macro.md` | 420 | 2026-05-21 12:35 | `macro/` | **shipped** — MacroMonitor + 2 calendar sources present. |
| `build_wiring.md` | 305 | 2026-05-21 13:23 | Multi-package — wires data_sources/macro into the bot. | **shipped** — but note `module_reports/sentiment.md` flags `prompts/build_wiring.md:72` references a non-existent `sentiment_aggregator` symbol (actual export is `sentiment`). Doc-drift. |
| `build_fixes.md` | 273 | 2026-05-22 00:50 | Bug-fix sweep across packages | **shipped** — referenced fixes appear in core/signals/agents. |
| `build_scalping_agent.md` | 1391 | 2026-05-22 12:21 | `agents/scalping_agent.py` (+ supporting files) | **shipped** — ScalpingAgent + ATR-SL + Confluence + v2 integration. |
| `build_arb_improvements.md` | 169 | 2026-05-23 10:46 | `execution/arb_engine.py` improvements | **shipped** — enhancements visible in arb_engine.py (gas breakeven, slippage, capped notional). |
| `build_dashboard_arb_panels.md` | 117 | 2026-05-23 11:40 | `ui/dashboard.py` arb panels | **shipped** — arb panels present in dashboard. |
| `build_data_sources_coinglass.md` | 208 | 2026-05-23 11:40 | `data_sources/sources/coinglass.py` | **shipped** — file present, registered. |
| `build_web_ui.md` | 499 | 2026-05-25 13:51 | `ui/web_server.py` + `ui/web_dashboard.html` | **shipped** — both files present; 11 routes audited in `module_reports/ui.md`. |
| `build_web_ui_refresh_v1.md` | 581 | 2026-05-27 12:50 | UI refresh v1 | **shipped** — visible in current `ui/web_dashboard.html`. |
| `build_funding_arb.md` | 252 | 2026-05-28 14:51 | `agents/funding_arb_agent.py`, `execution/funding_engine.py` | **shipped** — both files present; 110 observations logged. |
| `fix_pre_soak.md` | 146 | 2026-05-28 15:32 | "4 items" pre-soak corrections (likely small surface across multiple packages) | **shipped** — fixes appear merged; soak preparation milestone. |
| `fix_deisland_engines.md` | 107 | 2026-05-28 16:49 | "De-island FundingRateArbEngine & CrossChainArbEngine" — re-integrate isolated engines | **shipped** — both engines present and integrated via their agents. |
| `build_crosschain_agent_v2.md` | 259 | 2026-05-28 11:50 | `agents/crosschain_agent.py` v2 | **shipped** — CrossChainArbAgent present, gated on RPC env vars. |
| `balance_agent.md` | 279 | 2026-05-28 12:26 | `agents/balance_agent.py` + `agents/balance/` subpackages | **shipped** — BalanceAgent present with full balance/ subtree (policy/, rails/, planner, inventory_state). |
| `build_web_ui_v2_agent_panels.md` | 554 | 2026-05-28 20:05 | `ui/web_server.py` + `ui/web_dashboard.html` v2 panels | **shipped (on branch)** — current branch `feat/web-ui-v2-agent-panels` is the active delivery. |

## Executed and partially shipped

| Prompt | Lines | mtime | Target module(s) | Status |
|--------|------:|-------|------------------|--------|
| `build_web_ui_v3_agent_pages.md` | 270 | 2026-05-28 21:47 | `ui/web_dashboard.html`, `ui/web_server.py` — per-agent pages + dashboard compaction | **partial** — `WEB_UI.md` documents v3-style routes (`/api/agent/{id}`, `/api/session/{name}`) and several `test_api_agent_*` / `test_api_session_*` tests pass, so v3 surface is at least partly in place on the current branch. |

## Not yet executed

| Prompt | Lines | mtime | Target module(s) | Notes |
|--------|------:|-------|------------------|-------|
| `build_balance_agent.md` | **0** | 2026-05-28 11:46 | n/a | **Empty file.** Superseded the same day by the 279-line `balance_agent.md`. Should be deleted. |
| `5kfund.md` | 154 | 2026-05-29 10:45 | Funds/exchanges/treasury planning prompt — references `OPERATIONS.md` and `SOAK_CRITERIA.md` (neither exists in the repo) | **not executed** — operational planning brief; no target module to ship. Most recent prompt mtime aside from this audit prompt. |
| `cryptobot_audit.md` | 244 | 2026-05-29 11:53 | This audit itself (Phase 0–13) | **in progress** — currently being executed as `audit/cryptobot_1.0/`. |

## Cross-reference findings

- **`prompts/build_balance_agent.md` is empty** (0 bytes / 0 lines). Likely a stub created and abandoned when the larger `balance_agent.md` superseded it.
- **`prompts/5kfund.md` references `OPERATIONS.md` and `SOAK_CRITERIA.md`** in line 1. Neither file exists in the repo. These were either external operator notes or are deferred deliverables.
- **`prompts/build_wiring.md:72`** references a `sentiment_aggregator` import that doesn't exist (actual singleton name is `sentiment` per `sentiment/aggregator.py`). Documented in `module_reports/sentiment.md`.
- **Six prompts have been executed on the current branch (`feat/web-ui-v2-agent-panels`)** — the cluster of `build_web_ui_*` + `balance_agent.md` + `build_funding_arb.md` + `fix_*.md` between 2026-05-25 and 2026-05-28 maps to the recent commit history.
- **No prompt covers `predictive/`.** The package is an empty `__init__.py` and no `build_predictive.md` / `build_trainer.md` exists. CLAUDE.md mentions `python -m predictive.trainer` as if it existed.
- **No prompt covers `strategies/` directly.** The pattern was carried over from `build_bot_loop.md` and never had a dedicated brief.

## Summary

- **25** prompts total (24 with content + 1 empty)
- **22** shipped — production modules exist and match prompt intent
- **1** partial — v3 web UI work split with v2 across the active branch
- **3** not executed — empty (`build_balance_agent.md`), operational (`5kfund.md`), self-referential (`cryptobot_audit.md`)
