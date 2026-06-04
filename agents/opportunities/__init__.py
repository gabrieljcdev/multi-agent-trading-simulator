"""
agents/opportunities/

The OpportunityScannerAgent's three-stage pipeline, kept strictly
separated (PROTOCOL_OPPORTUNITIES):

  detectors/   — DETECTION: pluggable BaseDetector watchers (plugin
                 pattern; REGISTERED_DETECTORS is the single source of
                 truth). Detectors never know about the gate or the view.
  fatal_risk_gate.py — GATE: screens FATAL flaws only; records all,
                 enforces on the view. The gate never ranks.
  competition.py — per-type competitor_count + trajectory recipes.
  edge_normalizer.py — annualized-% edge across opp_types (pinned method).
  ranker.py    — VIEW: trajectory → edge → reachability. Never re-screens.
  exploration.py — FEATURE BLOCK 6a: unconventional flagging/scoring.
                 Observation-only; never gates, sizes, or acts.
"""
