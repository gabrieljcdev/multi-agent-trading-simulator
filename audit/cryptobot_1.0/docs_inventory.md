# Docs inventory — CryptoBot 1.0

All `.md` files in the repo (excluding `venv/`, `.git/`, `audit/`, `.pytest_cache/`). No `.docx` files are tracked.

| File | Lines | Last modified (UTC) | Purpose | Referenced from |
|------|------:|--------------------|---------|-----------------|
| `CLAUDE.md` | 74 | 2026-05-20 09:21 | Guidance for Claude Code: commands, architecture invariants, DB conventions, agent loop notes, repo-state caveat. | Loaded into every Claude Code session by the harness (project-instructions). Not linked from other repo docs. |
| `GLOSSARY.md` | 105 | 2026-05-25 21:21 | Project-specific vocabulary — "lookup before grepping". Covers bot architecture, signals, scalping, sentiment, macro. | `README.md` |
| `PLUGIN_PATTERN.md` | 55 | 2026-05-22 12:48 | Documents the one repeating pattern: abstract base + registry list + discovery layer. | `README.md`, `WEB_UI.md` |
| `README.md` | 150 | 2026-05-22 12:49 | Top-level overview: philosophy, modes, quick start, repo tour. | Cross-links to `GLOSSARY.md`, `PLUGIN_PATTERN.md`, `RUNBOOK.md`. |
| `RUNBOOK.md` | 138 | 2026-05-25 21:20 | Operational playbook — one section per agent/feature with activation checklist + commands. Contains a `### Rollback` subsection covering scalp v2 inline. | `README.md`. References `scalping_v2/SCALPING_V2.md` and `scalping_v2/SCALPING_V2_RECALIBRATION.md`. |
| `WEB_UI.md` | 431 | 2026-05-28 21:56 | Browser control panel spec — aiohttp WS + REST, push-only state, localhost-only. Most recently modified doc; aligns with the active `feat/web-ui-v2-agent-panels` branch. | Cross-links to `PLUGIN_PATTERN.md`. |
| `scalping_v2/RUNBOOK_v2_rollback_section.md` | 125 | 2026-05-25 19:56 | Standalone "rollback section" intended to be appended to `RUNBOOK.md`. | **No incoming references.** The substance was already inlined into `RUNBOOK.md → Rollback`, so this file appears orphaned. |
| `scalping_v2/SCALPING_V2.md` | 229 | 2026-05-25 18:20 | v2 integration guide — what v2 adds on top of v1, six new components, activation steps. | `RUNBOOK.md` |
| `scalping_v2/SCALPING_V2_RECALIBRATION.md` | 475 | 2026-05-25 19:56 | Cookbook — SQL recipes for tuning v2 selectivity gates against accumulated `scalp_observations`. Executed via `scripts/run_recalibration.py`. | `RUNBOOK.md`, `scripts/run_recalibration.py` (reads the SQL blocks). |
| `scalping_v2/SCALPING_V3_ROADMAP.md` | 210 | 2026-05-25 19:56 | What was deliberately NOT built into v2, why deferred, trigger conditions for each item. | **No incoming references** from other docs. (Listed in `module_reports/scalping_v2.md`.) |

## Orphan docs (not referenced from any other doc)

- `scalping_v2/RUNBOOK_v2_rollback_section.md` — content already merged into `RUNBOOK.md`, but the file remains.
- `scalping_v2/SCALPING_V3_ROADMAP.md` — never linked. Critical for understanding deferred work; the master audit doc (Phase 11 → "What is deliberately NOT built") will treat this as its source.
- `CLAUDE.md` — not referenced from README/RUNBOOK/etc., but is consumed by the Claude Code harness directly.

## Broken references

- `scalping_v2/SCALPING_V2.md` references `scalping_agent_doc.docx` (line 2: *"Companion to scalping_agent_doc.docx"*) but no `.docx` exists anywhere in the repo. The referenced design doc was either external (Google Doc / shared drive) or has been removed.

## Doc → code traceability

| Doc | Code module(s) it documents | Stays in sync? |
|-----|------------------------------|----------------|
| `CLAUDE.md` | All packages (high-level invariants) | Notes a "Repo state caveat" — README describes a fuller tree than currently exists; some `predictive/`, `sentiment/`, `ui/`, `signals/` modules listed are empty stubs. |
| `README.md` | All packages | Same caveat. |
| `RUNBOOK.md` | `agents/scalping_agent.py`, `agents/funding_arb_agent.py`, `agents/crosschain_agent.py`, `agents/balance_agent.py`, `execution/arb_engine.py`, `core/bot.py`, `ui/`. | Largely in sync; the rollback subsection (line 84-ish) matches `scalping_v2/RUNBOOK_v2_rollback_section.md` substance. |
| `WEB_UI.md` | `ui/web_server.py`, `ui/web_dashboard.html` | Most recently touched doc — actively aligned with the current `feat/web-ui-v2-agent-panels` work-in-progress branch. Some routes documented in WEB_UI.md may run ahead of code; cross-check against `module_reports/ui.md`. |
| `PLUGIN_PATTERN.md` | `sentiment/`, `data_sources/`, `macro/sources/`, `agents/`, `execution/chains/`, `strategies/` | Reasonably general. The pattern is implemented in 6 places (Phase 9 lists them). |
| `GLOSSARY.md` | All | Free-form lookup; not a code spec. |
| `scalping_v2/SCALPING_V2.md` | `agents/scalping_*.py`, `scalping_v2/*.py` | Documents v2; the production-path files in `agents/` are byte-identical to the bundle (per `module_reports/scalping_v2.md` duplication audit). |
| `scalping_v2/SCALPING_V2_RECALIBRATION.md` | `scripts/run_recalibration.py` consumes its SQL blocks; targets `scalp_observations` schema | `scripts/run_recalibration.py` exists explicitly to verify these queries stay valid as the schema migrates. |
| `scalping_v2/SCALPING_V3_ROADMAP.md` | None — explicit deferred-work catalog. | n/a |
| `scalping_v2/RUNBOOK_v2_rollback_section.md` | n/a — substance already in `RUNBOOK.md`. | Drift risk: two copies of "how to roll back v2" can diverge silently. |
