"""
follow/wallet_flow.py

WalletFlowWatcher — a free-public-Solana-RPC streaming OBSERVER source. Tracks a
watchlist of Solana wallets, detects their actions and (critically) their token
transfers TO/FROM labelled exchange addresses, and logs them as SUGGESTIVE flow
events.

OBSERVER, hard requirement: there is NO submit / execute / trade / order method
anywhere on this class. It watches and logs; the operator acts (or not). The
absence of an execution path is the point.

Data layer (rewired off paid Helius onto the free public RPC):
  - SUBSCRIPTION IS NARROW — one logsSubscribe per watchlist + labelled-exchange
    address. NEVER the launchpad / all-mints firehose (that floods public RPC).
  - GAP-TOLERANT BY DESIGN — public RPC drops data; a missed/dropped notification
    logs a structured "stream_gap" and the watcher keeps running. get_stats()
    exposes best_effort=True + a gap count so no surface implies completeness.
  - SELF-THROTTLING — every RPC call goes through the shared rate-limited client
    (follow/rpc.py); never bursts, backs off on 429/timeout.

Signal-emission discipline: a flow/action event is persisted for EVERY watched
wallet (candidates included, so they can be scored and displayed), but an
emitted signal is produced ONLY for "manual" and "confirmed" wallets
(follow/provenance.is_signal_eligible). A candidate's activity never influences
a signal — it is observed, not followed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime
from typing import Optional

from config import settings
from database import queries as q
from follow import labels, provenance
from follow.base import BaseStreamingDataSource
from follow.rpc import SolanaRpc
from follow.sol_parse import parse_transaction

logger = logging.getLogger(__name__)

_GAP_SAMPLE_MAX = 500


class WalletFlowWatcher(BaseStreamingDataSource):
    """Public-RPC observer for the wallet + exchange-flow watchlist."""

    source_id    = "wallet_flow"
    display_name = "Wallet + Exchange-Flow Watcher"
    optional     = True

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._rpc: Optional[SolanaRpc] = None
        # Observed watch set (manual + confirmed + candidate). Candidates are
        # observed but never emit a signal — see _process_event.
        self._watchlist: set[str] = set()
        # In-memory stats (the DB is the durable record).
        self._events_seen = 0
        self._signals_emitted = 0
        self._last_event_ts: Optional[float] = None
        # Gap tracking — public RPC is lossy; we surface this honestly.
        self._gaps: deque = deque(maxlen=_GAP_SAMPLE_MAX)
        self._gap_count = 0

    # ── Availability ────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Runnable when enabled AND the exchange-label set is loaded (without
        labels there is no flow to classify). The free public RPC needs no key,
        so — unlike the old Helius path — there is no key gate."""
        if not bool(getattr(settings, "WALLETFLOW_ENABLED", False)):
            return False
        return labels.is_available()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Open the public-RPC stream for the watchlist + exchange addresses.
        Degrades gracefully: if unavailable or the connection cannot be
        established, the watcher stays idle rather than raising to the host."""
        try:
            labels.load_seed_labels()
        except Exception as e:
            logger.debug("walletflow: seed labels failed: %s", e)
        if not self.is_available():
            logger.info("walletflow: not available (enabled=%s, labels=%s) "
                        "— observer idle",
                        getattr(settings, "WALLETFLOW_ENABLED", False),
                        labels.is_available())
            self._running = False
            return
        self.refresh_watchlist()
        self._rpc = SolanaRpc()
        self._running = True
        self._task = asyncio.create_task(self._stream_loop())
        logger.info("walletflow: observing %d wallets via public RPC %s",
                    len(self._watchlist), self._rpc.url)

    async def stop(self) -> None:
        """Clean teardown. Idempotent; never raises."""
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
        self._task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._rpc is not None:
            try:
                await self._rpc.close()
            except Exception:
                pass
        logger.info("walletflow: stopped")

    def refresh_watchlist(self) -> set[str]:
        """Rebuild the observed watch set from the provenance store: manual +
        confirmed + candidate. Rejected wallets are not observed."""
        watch: set[str] = set()
        try:
            for state in ("manual", "confirmed", "candidate"):
                for e in q.get_watchlist_by_provenance(state):
                    watch.add(e["address"])
        except Exception as e:
            logger.debug("walletflow: refresh_watchlist failed: %s", e)
        self._watchlist = watch
        return watch

    def subscription_addresses(self) -> list[str]:
        """The NARROW subscription set: watchlist + labelled exchange addresses
        ONLY. Never a full-launchpad / all-mints firehose."""
        addrs = set(self._watchlist)
        try:
            addrs.update(labels.all_addresses())
        except Exception as e:
            logger.debug("walletflow: label addresses failed: %s", e)
        return sorted(addrs)

    # ── Stream + event handling ─────────────────────────────────────────────

    async def _stream_loop(self) -> None:
        """Public-RPC websocket consume loop. Gap-tolerant: a dropped socket
        logs a stream_gap and reconnects; it never crashes the host agent."""
        try:
            import websockets  # lazy — only when actually streaming
        except Exception:
            logger.info("walletflow: `websockets` not installed — stream idle")
            return
        ws_url = self._rpc.ws_url if self._rpc else settings.WALLETFLOW_RPC_WS_URL
        while self._running:
            try:
                async with websockets.connect(ws_url, ping_interval=30) as ws:
                    self._ws = ws
                    await self._subscribe(ws)
                    async for raw in ws:
                        if not self._running:
                            break
                        await self._on_raw_message(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Public RPC drops sockets routinely — record a gap and retry.
                self._record_gap("stream_drop", str(e))
                logger.debug("walletflow: stream reconnect after drop: %s", e)
                await asyncio.sleep(5)

    async def _subscribe(self, ws) -> None:
        """NARROW: one logsSubscribe per watched + labelled address (mentions
        filter takes exactly one address). NEVER a firehose 'all' subscription."""
        addrs = self.subscription_addresses()
        for i, addr in enumerate(addrs):
            try:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": i + 1, "method": "logsSubscribe",
                    "params": [{"mentions": [addr]}, {"commitment": "confirmed"}],
                }))
            except Exception as e:
                logger.debug("walletflow: subscribe %s failed: %s", addr, e)

    async def _on_raw_message(self, raw) -> None:
        """Decode one logsNotification, fetch the full tx via getTransaction,
        and hand the raw JSON to the parser. Gap-tolerant throughout."""
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except Exception as e:
            self._record_gap("decode_error", str(e))
            return
        if not isinstance(msg, dict) or msg.get("method") != "logsNotification":
            return                                   # subscription acks etc.
        value = (((msg.get("params") or {}).get("result") or {}).get("value") or {})
        if value.get("err") is not None:
            return                                   # failed tx — ignore
        sig = value.get("signature")
        if not sig:
            self._record_gap("missing_signature", "")
            return
        await self._fetch_and_handle(sig)

    async def _fetch_and_handle(self, signature: str) -> None:
        """Fetch the full tx and parse it. A failed fetch is a known public-RPC
        gap — logged, not fatal."""
        if self._rpc is None:
            return
        try:
            tx = await self._rpc.get_transaction(signature)
        except Exception as e:
            self._record_gap("tx_fetch_error", f"{signature}:{e}")
            return
        if not tx:
            self._record_gap("tx_fetch_missing", signature)
            return
        self._handle_raw_tx(tx)

    def _handle_raw_tx(self, tx: dict) -> list:
        """Parse one standard RPC tx into ActorEvents, persist each as
        suggestive evidence, and emit signals ONLY for signal-eligible wallets.
        Returns the parsed ActorEvents (for tests). Never raises."""
        detected_at = time.time()
        try:
            events = parse_transaction(
                tx, self._watchlist, labels.is_terminal_address,
                detected_at=detected_at, label_name=self._label_name,
                source_id=self.source_id)
        except Exception as e:
            logger.debug("walletflow: parse failed: %s", e)
            return []
        for ev in events:
            self._persist_event(ev)
            self._process_event(ev)
        return events

    @staticmethod
    def _label_name(address: str) -> Optional[str]:
        lab = labels.lookup(address)
        return lab.get("exchange_name") if lab else None

    def _persist_event(self, ev) -> None:
        """Write the flow event. Suggestive evidence — never a confirmed sell.
        size_usd is None for RPC-sourced events (public RPC carries no USD
        valuation without a price oracle); the raw amount rides in meta."""
        try:
            q.insert_wallet_flow_event({
                "source_id":   ev.source_id,
                "actor_id":    ev.actor_id,
                "action":      ev.action,
                "asset":       ev.asset,
                "venue":       ev.venue,
                "size_usd":    ev.size_usd,
                "occurred_at": datetime.utcfromtimestamp(ev.occurred_at or 0),
                "detected_at": datetime.utcfromtimestamp(ev.detected_at or 0),
                "meta":        ev.meta,
            })
            self._events_seen += 1
            self._last_event_ts = ev.detected_at
        except Exception as e:
            logger.debug("walletflow: persist failed: %s", e)

    def _process_event(self, ev) -> None:
        """Emit a signal ONLY for manual/confirmed wallets. Candidate wallets
        are observed (already persisted) but NEVER influence a signal."""
        try:
            if not provenance.is_signal_eligible(ev.actor_id):
                return
            # Observer "signal" = a surfaced, logged observation — NOT an order.
            # There is intentionally no routing/execution here.
            self._signals_emitted += 1
            q.bump_wallet_actions(ev.actor_id, 1)
            logger.info("walletflow SIGNAL (suggestive): %s %s %s via %s",
                        ev.actor_id, ev.action, ev.asset, ev.venue)
        except Exception as e:
            logger.debug("walletflow: process_event failed: %s", e)

    # ── Gap tracking (public RPC is lossy — surface it honestly) ─────────────

    def _record_gap(self, reason: str, detail: str = "") -> None:
        self._gap_count += 1
        self._gaps.append((time.time(), reason))
        logger.warning("walletflow stream_gap: reason=%s detail=%s", reason, detail)

    def _gaps_in_last(self, seconds: float) -> int:
        cutoff = time.time() - seconds
        return sum(1 for ts, _ in self._gaps if ts >= cutoff)

    # ── Stats (for the snapshot) ────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Never raises — zeroed/empty defaults on any failure. best_effort is
        always True: public RPC drops data, so no surface may imply the history
        is complete."""
        try:
            stale = labels.staleness()
        except Exception:
            stale = {"is_stale": True, "count": 0}
        return {
            "source_id":        self.source_id,
            "running":          bool(self._running),
            "available":        self._safe_available(),
            "watched":          len(self._watchlist),
            "events_seen":      self._events_seen,
            "signals_emitted":  self._signals_emitted,
            "last_event_ts":    self._last_event_ts,
            "label_staleness":  stale,
            # Honesty about completeness — data is best-effort over public RPC.
            "best_effort":      True,
            "data_source":      "public_rpc",
            "stream_gaps":      self._gap_count,
            "gaps_24h":         self._gaps_in_last(86400),
            "rpc_calls":        (self._rpc.calls if self._rpc else 0),
            "rpc_throttle_events": (self._rpc.throttle_events if self._rpc else 0),
        }

    def _safe_available(self) -> bool:
        try:
            return self.is_available()
        except Exception:
            return False


__all__ = ["WalletFlowWatcher"]
