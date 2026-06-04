"""
agents/opportunity_scanner_agent.py

OpportunityScannerAgent — the edge-layer watcher ($0 OBSERVATION MODE).

One BaseAgent, many detectors: the unified shape from
PROTOCOL_OPPORTUNITIES §4 (per-type agents split off only when an
opp_type graduates to live execution). It places no capital and opens
no positions — capital_allocation = 0.0 is absolute; no order routing,
no ccxt order calls, no transfers exist anywhere in this module, and
close_all_positions() is a no-op (the safety tell).

Three strictly-separated stages per scan:

  DETECTION  — REGISTERED_DETECTORS run concurrently, each on its own
               refresh_interval; one slow/failing detector never blocks
               the others (scan() returns [] on failure).
  GATE       — FatalRiskGate screens fatal flaws only. Record-vs-
               enforce: every candidate is written, disqualified rows
               just never enter the default ranked view.
  VIEW       — trajectory-ranked survivors, re-measured on a loop
               (a stale trajectory is a lie, §5).

Plus the two defence loops:
  - hypothesis log (FEATURE BLOCK 5): immutable decision-time feature
    vector at first_seen; forward labels backfilled by a SEPARATE timed
    pass into SEPARATE columns (mirrors _future_price_tracker_loop) so
    deflated-Sharpe / CPCV rigour stays mechanically possible.
  - exploration lane (FEATURE BLOCK 6): unconventional flagging on
    survivors + gate self-audit counterfactuals on disqualified rows.
    Surfacing and study ONLY — never loosens the gate, never reorders
    the standard view, never makes a disqualified row actionable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from typing import Optional

from config import settings
from database import queries as db_queries

from agents.base import (
    AgentStats, BaseAgent,
    RUNNING, OFFLINE, STOPPED,
)
from agents.opportunities.detectors import REGISTERED_DETECTORS, BaseDetector
from agents.opportunities.detectors.base import OpportunityCandidate
from agents.opportunities.fatal_risk_gate import fatal_risk_gate, SURVIVABLE
from agents.opportunities.competition import (
    competition_measurer, derive_window_status,
)
from agents.opportunities.edge_normalizer import edge_normalizer
from agents.opportunities.ranker import trajectory_ranker
from agents.opportunities.exploration import score_unconventional

log = logging.getLogger(__name__)

# Rolling detection-latency sample for the stats panel (in-memory only).
_LATENCY_SAMPLE_MAX = 200


class OpportunityScannerAgent(BaseAgent):
    agent_id           = "opportunity_scanner"
    display_name       = "Opportunity Scanner"
    capital_allocation = 0.0          # $0 observation mode — absolute
    optional           = True

    def __init__(self, detectors: Optional[list[BaseDetector]] = None):
        super().__init__()
        # Plugin pattern: only BaseDetector + the registration list —
        # never a concrete detector by name. Tests inject their own.
        self._detectors = detectors if detectors is not None \
            else list(REGISTERED_DETECTORS)
        self._gate    = fatal_risk_gate
        self._measure = competition_measurer
        self._edge    = edge_normalizer
        self._ranker  = trajectory_ranker

        self._running = False
        self._tasks: list[asyncio.Task] = []

        # Stats counters (in-memory; the DB is the durable record).
        self._candidates_seen   = 0
        self._survivable_count  = 0
        self._disqualified_count = 0
        self._latencies_ms: deque = deque(maxlen=_LATENCY_SAMPLE_MAX)
        self._last_scan_ts: Optional[float] = None

    # ── BaseAgent contract ──────────────────────────────────────────────

    def is_available(self) -> bool:
        return bool(settings.OPPORTUNITY_SCANNER_ENABLED)

    @property
    def observation_mode(self) -> bool:
        """Always True — observation mode is this agent's entire design,
        not a phase flag. The web UI status taxonomy reads this."""
        return True

    async def start(self) -> None:
        self._running = True
        self._status = RUNNING
        self._start_time = time.time()
        available = [d for d in self._detectors if d.is_available()]
        skipped = [d.detector_id for d in self._detectors
                   if not d.is_available()]
        if skipped:
            log.info("[OpportunityScanner] skipping unavailable detectors: %s",
                     skipped)
        log.info("[OpportunityScanner] starting in OBSERVATION mode "
                 "($0, %d detectors)", len(available))
        # One loop per detector (own cadence) + the competition
        # re-measurement loop + the label-backfill loop.
        self._tasks = [
            asyncio.create_task(self._detector_loop(d)) for d in available
        ]
        self._tasks.append(asyncio.create_task(self._competition_loop()))
        self._tasks.append(asyncio.create_task(self._label_backfill_loop()))

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            if not t.done():
                t.cancel()
        for t in self._tasks:
            try:
                await t
            except Exception:
                pass
        self._tasks = []
        self._status = STOPPED
        log.info("[OpportunityScanner] stopped")

    async def close_all_positions(self) -> None:
        # No-op — there is nothing to close. This agent never opens a
        # position, routes an order, or moves a cent; that this method
        # has nothing to do is the tell that the agent is safe.
        log.debug("[OpportunityScanner] close_all_positions: no positions "
                  "exist in observation mode (no-op)")

    async def get_stats(self) -> AgentStats:
        """Zero capital, zero P&L — observation agents never fabricate
        either. Candidate/observation counts live in the status string
        and get_observation_summary() (mirrors funding_arb's shape)."""
        try:
            return AgentStats(
                agent_id=self.agent_id,
                status=RUNNING if self._running else OFFLINE,
                capital_allocated=0.0,
                capital_deployed=0.0,
                daily_pnl=0.0,
                daily_pnl_pct=0.0,
                total_pnl=0.0,
                trades_today=0,
                win_rate_today=0.0,
                win_rate_alltime=0.0,
                consecutive_losses=0,
                last_trade_time=None,
                error=(f"observation: {self._candidates_seen} candidates, "
                       f"{self._survivable_count} survivable, "
                       f"{self._disqualified_count} disqualified"
                       if self._candidates_seen else None),
            )
        except Exception as e:
            log.debug("[OpportunityScanner] get_stats failed: %s", e)
            return AgentStats(
                agent_id=self.agent_id, status=OFFLINE,
                capital_allocated=0.0, capital_deployed=0.0,
                daily_pnl=0.0, daily_pnl_pct=0.0, total_pnl=0.0,
                trades_today=0, win_rate_today=0.0, win_rate_alltime=0.0,
                consecutive_losses=0, last_trade_time=None, error=str(e),
            )

    # ── Dashboard helpers (read-only) ───────────────────────────────────

    def get_observation_summary(self) -> dict:
        """DB rollup + in-memory detection-latency stats. Zeroed
        defaults on any hiccup — the panel treats zeros as 'no data
        yet', never as an error."""
        try:
            out = db_queries.get_opportunity_summary()
        except Exception as e:
            log.debug("[OpportunityScanner] get_observation_summary: %s", e)
            out = {
                "n_core": 0, "n_survivable": 0, "n_disqualified": 0,
                "n_observations": 0, "n_labeled": 0, "by_trend": {},
                "mean_detection_latency_ms": 0.0,
            }
        lats = list(self._latencies_ms)
        out["session_detection_latency_ms"] = (
            sum(lats) / len(lats) if lats else 0.0)
        out["detection_latency_sla_ms"] = float(
            settings.OPPORTUNITY_DETECTION_LATENCY_SLA_MS)
        out["candidates_seen_session"] = self._candidates_seen
        return out

    def get_ranked_view(self, mode: str = "standard") -> list[dict]:
        """Ranked rows for the dashboard. mode='standard' re-ranks
        through TrajectoryRanker (which also enforces the edge-never-
        without-trajectory render invariant); mode='exploratory' is the
        fenced-off 6b lane. The two never merge."""
        try:
            rows = db_queries.get_ranked_opportunities(
                mode=mode,
                show_disqualified=bool(settings.OPPORTUNITY_SHOW_DISQUALIFIED),
            )
            if mode == "standard":
                rows = self._ranker.rank(rows)
            return rows
        except Exception as e:
            log.debug("[OpportunityScanner] get_ranked_view(%s): %s", mode, e)
            return []

    # ── Stage 1: detection loops ────────────────────────────────────────

    async def _detector_loop(self, detector: BaseDetector) -> None:
        interval = max(1.0, float(detector.refresh_interval))
        while self._running:
            try:
                await asyncio.sleep(interval)
                if self._manually_halted:
                    # Operator halt skips scans; nothing else to manage —
                    # observation mode holds no positions.
                    continue
                candidates = await detector.scan() or []
                self._last_scan_ts = time.time()
                for cand in candidates:
                    await self._process_candidate(cand)
            except asyncio.CancelledError:
                break
            except Exception as e:
                # A detector that violates Rule 4 and raises anyway must
                # not kill its own loop, let alone a sibling's.
                log.exception("[OpportunityScanner] %s loop error: %s",
                              detector.detector_id, e)

    async def _process_candidate(self, cand: OpportunityCandidate) -> None:
        """Gate → persist (record-vs-enforce) → hypothesis log →
        exploration scoring. Re-detections UPSERT mutable fields only;
        first_seen and the observation snapshot are written once."""
        try:
            self._candidates_seen += 1
            risk_status, risk_flags = self._gate.screen(cand)
            if risk_status == SURVIVABLE:
                self._survivable_count += 1
            else:
                self._disqualified_count += 1

            edge_pct, edge_conf = self._edge.annualize(cand)

            core_row = {
                "opp_type":   cand.opp_type,
                "protocol":   cand.protocol,
                "chain":      cand.chain,
                "market_key": cand.market_key,
                "asset_class": cand.asset_class,
                "detector_id": cand.detector_id,
                "first_seen":  cand.first_seen,   # immutable downstream
                "edge_annualized_pct": edge_pct,
                "edge_confidence":     edge_conf,
                "risk_status":         risk_status,
                "risk_flags":          risk_flags,
                "reachability_verdict": (cand.raw_detail or {}).get(
                    "reachability_verdict", "unknown"),
                "as_of": datetime.utcnow(),
                # competitor_count / trend / window_status deliberately
                # absent — seeded on insert, owned by the re-measurement
                # loop afterwards.
            }

            # Exploration lane (6a) — SURVIVORS only, observation-only.
            unconventional = None
            if (settings.OPPORTUNITY_EXPLORATION_ENABLED
                    and risk_status == SURVIVABLE):
                score, factors, rationale = score_unconventional(
                    {**core_row, "competitor_trend": None},
                    cand.raw_detail, cand.feature_vector)
                unconventional = (score, factors, rationale)
                core_row.update(
                    unconventional_score=score,
                    unconventional_factors=factors,
                    unconventional_rationale=rationale,
                )

            core_id = await asyncio.to_thread(
                db_queries.upsert_opportunity_core, core_row)
            await asyncio.to_thread(
                db_queries.save_opportunity_detail,
                cand.opp_type, core_id, cand.raw_detail)

            # Hypothesis log — EVERY candidate, disqualified included
            # (non-events are data: omitting misses is the survivorship
            # bias that makes spurious edges look real). Written once.
            obs_row = {
                "core_id":              core_id,
                "detector_id":          cand.detector_id,
                "opp_type":             cand.opp_type,
                "first_seen":           cand.first_seen,
                "feature_vector_json":  cand.feature_vector,
                "detection_latency_ms": cand.detection_latency_ms,
            }
            if unconventional is not None:
                obs_row.update(
                    unconventional_score=unconventional[0],
                    unconventional_factors_json=unconventional[1],
                    unconventional_rationale=unconventional[2],
                )
            await asyncio.to_thread(
                db_queries.save_opportunity_observation, obs_row)

            if cand.detection_latency_ms is not None:
                self._latencies_ms.append(float(cand.detection_latency_ms))
                if (cand.detection_latency_ms
                        > float(settings.OPPORTUNITY_DETECTION_LATENCY_SLA_MS)):
                    log.debug(
                        "[OpportunityScanner] %s detection latency %.0fms "
                        "over SLA", cand.market_key, cand.detection_latency_ms)
        except Exception as e:
            log.exception("[OpportunityScanner] process candidate %s: %s",
                          getattr(cand, "market_key", "?"), e)

    # ── Stage 3 heartbeat: competition re-measurement loop ─────────────

    async def _competition_loop(self) -> None:
        interval = max(1.0, float(settings.OPPORTUNITY_COMPETITION_RECHECK_SEC))
        while self._running:
            try:
                await asyncio.sleep(interval)
                cores = await asyncio.to_thread(
                    db_queries.get_open_opportunity_cores)
                for core in cores:
                    try:
                        count, trend, append = await self._measure.measure(core)
                        window = derive_window_status(trend, count)
                        await asyncio.to_thread(
                            db_queries.record_competition_check,
                            core["id"], count, trend, append, window)
                    except Exception as e:
                        log.debug("[OpportunityScanner] re-measure core %s: %s",
                                  core.get("id"), e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("[OpportunityScanner] competition loop: %s", e)

    # ── FEATURE BLOCK 5/6c: forward-label backfill loop ─────────────────

    async def _label_backfill_loop(self) -> None:
        """The SEPARATE scheduled pass that fills forward labels —
        features at first_seen, labels later, never co-mingled (the
        _future_price_tracker_loop discipline). For disqualified rows it
        also writes the gate self-audit counterfactual (6c). LOGS ONLY:
        nothing here changes a gate verdict or either ranked view."""
        interval = max(1.0, float(settings.OPPORTUNITY_LABEL_BACKFILL_SEC))
        while self._running:
            try:
                await asyncio.sleep(interval)
                for horizon_h in settings.OPPORTUNITY_LABEL_HORIZONS_H:
                    due = await asyncio.to_thread(
                        db_queries.get_opportunity_observations_needing_labels,
                        int(horizon_h))
                    for obs in due:
                        await self._backfill_one(obs, int(horizon_h))
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("[OpportunityScanner] label backfill: %s", e)

    async def _backfill_one(self, obs: dict, horizon_h: int) -> None:
        try:
            core = await asyncio.to_thread(
                db_queries.get_opportunity_core_by_id, obs["core_id"])
            if core is None:
                return
            labels = {
                "realized_competitor_count": core.get("competitor_count"),
                "realized_edge_decay":       core.get("edge_annualized_pct"),
                "window_status_at_horizon":  core.get("window_status"),
            }
            if (core.get("risk_status") == "disqualified"
                    and settings.OPPORTUNITY_GATE_SELF_AUDIT_ENABLED):
                labels["counterfactual_outcome"] = \
                    self._counterfactual(core, horizon_h)
            await asyncio.to_thread(
                db_queries.update_opportunity_labels,
                obs["core_id"], horizon_h, labels)
        except Exception as e:
            log.debug("[OpportunityScanner] backfill core %s @%dh: %s",
                      obs.get("core_id"), horizon_h, e)

    @staticmethod
    def _counterfactual(core: dict, horizon_h: int) -> dict:
        """6c — did the flaw the gate fired on actually manifest within
        the horizon? Keyed by the red flag that disqualified. This pass
        can only attest what the pipeline observes (bad-debt evidence is
        a later feed); 'unknown' is recorded honestly rather than
        guessed. Re-calibrating the gate from this evidence is a
        SEPARATE human-in-the-loop decision — the agent gathers evidence
        about its own risk rules; it does not act on it."""
        fatal_flags = [f for f in (core.get("risk_flags") or [])
                       if f.get("severity") == "fatal"]
        detail = {}
        try:
            detail = db_queries.get_opportunity_detail(
                core.get("opp_type"), core.get("id")) or {}
        except Exception:
            pass
        bad_debt = detail.get("bad_debt_history")
        out = {}
        for f in fatal_flags:
            out[f.get("flag", "unknown")] = {
                "manifested": (bool(bad_debt) if bad_debt is not None else None),
                "evidence":   ("bad_debt_history" if bad_debt
                               else "none_observed_yet"),
                "window_status": core.get("window_status"),
            }
        return out
