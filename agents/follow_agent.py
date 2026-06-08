"""
agents/follow_agent.py

FollowAgent — the capital-FREE host for the follow/ OBSERVER sources.

Mirrors OpportunityScannerAgent's $0 observation-mode shape: capital_allocation
= 0.0 is absolute, close_all_positions() is a no-op (the safety tell — there is
nothing to close because nothing is ever opened), and get_stats() fabricates no
P&L. It owns the follow streaming-source registry (REGISTERED_FOLLOW_SOURCES),
starts only the available ones, and runs a maintenance pass that drives the
reviewed auto-discovery pipeline + trust-lifecycle enforcement.

It is registered with the coordinator as an observer ONLY. No capital, no
positions, no order routing, no execution path lives anywhere under it — the
source it hosts (WalletFlowWatcher) has no submit/execute method, and neither
does this agent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from config import settings
from database import queries as db_queries

from agents.base import (
    AgentStats, BaseAgent,
    RUNNING, OFFLINE, STOPPED,
)
from follow import REGISTERED_FOLLOW_SOURCES, BaseStreamingDataSource
from follow import discovery, provenance, labels

log = logging.getLogger(__name__)


class FollowAgent(BaseAgent):
    agent_id           = "follow"
    display_name       = "Follow Watcher"
    capital_allocation = 0.0          # $0 observation mode — absolute
    optional           = True

    def __init__(self, sources: Optional[list[BaseStreamingDataSource]] = None):
        super().__init__()
        # Plugin pattern: only BaseStreamingDataSource + the registration list,
        # never a concrete source by name. Tests inject their own.
        self._sources = sources if sources is not None \
            else list(REGISTERED_FOLLOW_SOURCES)
        self._running = False
        self._tasks: list[asyncio.Task] = []

    # ── BaseAgent contract ──────────────────────────────────────────────────

    def is_available(self) -> bool:
        return bool(getattr(settings, "WALLETFLOW_ENABLED", False))

    @property
    def observation_mode(self) -> bool:
        """Always True — observation is this agent's entire design. The web UI
        status taxonomy reads this."""
        return True

    async def start(self) -> None:
        self._running = True
        self._status = RUNNING
        self._start_time = time.time()
        # Ensure the shared label set is seeded before sources classify flow.
        try:
            labels.load_seed_labels()
        except Exception as e:
            log.debug("[Follow] seed labels: %s", e)
        available = [s for s in self._sources if s.is_available()]
        skipped = [s.source_id for s in self._sources if not s.is_available()]
        if skipped:
            log.info("[Follow] skipping unavailable sources: %s", skipped)
        log.info("[Follow] starting in OBSERVATION mode ($0, %d source(s))",
                 len(available))
        for s in available:
            self._tasks.append(asyncio.create_task(self._safe_source_start(s)))
        self._tasks.append(asyncio.create_task(self._maintenance_loop()))

    async def stop(self) -> None:
        self._running = False
        for s in self._sources:
            try:
                await s.stop()
            except Exception as e:
                log.debug("[Follow] source %s stop: %s", s.source_id, e)
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
        log.info("[Follow] stopped")

    async def close_all_positions(self) -> None:
        # No-op — there is nothing to close. This agent never opens a position,
        # routes an order, or moves a cent; that this method has nothing to do
        # is the tell that the agent is a pure observer.
        log.debug("[Follow] close_all_positions: no positions exist (observer no-op)")

    async def get_stats(self) -> AgentStats:
        """Zero capital, zero P&L — observation agents never fabricate either.
        Activity counts ride in the status string + get_walletflow_snapshot()."""
        try:
            watched = sum(int(s.get_stats().get("watched", 0)) for s in self._sources)
            events = sum(int(s.get_stats().get("events_seen", 0)) for s in self._sources)
            note = (f"observation: {watched} watched, {events} events"
                    if (watched or events) else None)
        except Exception:
            note = None
        return AgentStats(
            agent_id=self.agent_id,
            status=RUNNING if self._running else OFFLINE,
            capital_allocated=0.0, capital_deployed=0.0,
            daily_pnl=0.0, daily_pnl_pct=0.0, total_pnl=0.0,
            trades_today=0, win_rate_today=0.0, win_rate_alltime=0.0,
            consecutive_losses=0, last_trade_time=None, error=note,
        )

    # ── Source helpers + dashboard read ─────────────────────────────────────

    def get_source(self, source_id: str) -> Optional[BaseStreamingDataSource]:
        for s in self._sources:
            if s.source_id == source_id:
                return s
        return None

    def get_walletflow_snapshot(self) -> dict:
        """Live, low-volume block for the 2Hz snapshot. Every key present even
        when a read raises (snapshot iron rule). Historical/heavy reads (skill
        score, candidate list, as-of reconstruction) are on-demand REST, NOT
        here."""
        out = {
            "enabled":          bool(getattr(settings, "WALLETFLOW_ENABLED", False)),
            "running":          bool(self._running),
            "flow_events":      [],
            "netflow_spikes":   [],
            "pending_candidates": 0,
            "label_staleness":  {"is_stale": True, "count": 0},
            "sources":          [],
        }
        try:
            out["sources"] = [s.get_stats() for s in self._sources]
        except Exception as e:
            log.debug("[Follow] source stats: %s", e)
        try:
            out["flow_events"] = db_queries.get_recent_wallet_flow_events(limit=15)
        except Exception as e:
            log.debug("[Follow] recent flow events: %s", e)
        try:
            windows = list(getattr(settings, "WALLETFLOW_NETFLOW_WINDOWS_H", [1, 24]))
            spikes = db_queries.get_wallet_net_flows(windows)
            # Largest absolute net-flow first — the operator wants the spikes.
            spikes.sort(key=lambda r: abs(r.get("net_usd", 0.0) or 0.0), reverse=True)
            out["netflow_spikes"] = spikes[:10]
        except Exception as e:
            log.debug("[Follow] net flows: %s", e)
        try:
            out["pending_candidates"] = len(db_queries.get_pending_candidates(limit=500))
        except Exception as e:
            log.debug("[Follow] pending candidates: %s", e)
        try:
            out["label_staleness"] = labels.staleness()
        except Exception as e:
            log.debug("[Follow] label staleness: %s", e)
        return out

    # ── Internal loops ──────────────────────────────────────────────────────

    async def _safe_source_start(self, source: BaseStreamingDataSource) -> None:
        try:
            await source.start()
        except Exception as e:
            log.exception("[Follow] source %s start failed: %s",
                          source.source_id, e)

    async def _maintenance_loop(self) -> None:
        """Periodic discovery pass + trust-lifecycle enforcement. Defensive:
        one failing pass never kills the loop or a sibling source."""
        interval = max(60.0, float(getattr(settings,
                       "WALLETFLOW_MAINTENANCE_INTERVAL_S", 3600)))
        while self._running:
            try:
                await asyncio.sleep(interval)
                if self._manually_halted:
                    continue
                await asyncio.to_thread(self._run_maintenance)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("[Follow] maintenance loop: %s", e)

    def _run_maintenance(self) -> None:
        """Discovery (writes only candidates) + trust expiry on confirmed
        wallets. Sync — run via to_thread. Never raises out."""
        try:
            discovery.run_discovery()
        except Exception as e:
            log.debug("[Follow] discovery pass: %s", e)
        try:
            for e in db_queries.get_watchlist_by_provenance("confirmed"):
                provenance.enforce_trust_expiry(e["address"])
        except Exception as e:
            log.debug("[Follow] trust expiry pass: %s", e)


__all__ = ["FollowAgent"]
