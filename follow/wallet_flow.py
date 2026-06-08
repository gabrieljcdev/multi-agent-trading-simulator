"""
follow/wallet_flow.py

WalletFlowWatcher — a raw-Helius streaming OBSERVER source. Tracks a watchlist
of Solana wallets, detects their actions and (critically) their token transfers
TO/FROM labelled exchange addresses, and logs them as SUGGESTIVE flow events.

OBSERVER, hard requirement: there is NO submit / execute / trade / order method
anywhere on this class. It watches and logs; the operator acts (or not). The
absence of an execution path is the point.

Signal-emission discipline: a flow/action event is persisted for EVERY watched
wallet (candidates included, so they can be scored and displayed), but an
emitted signal is produced ONLY for "manual" and "confirmed" wallets
(follow/provenance.is_signal_eligible). A candidate wallet's activity never
influences a signal — it is observed, not followed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from config import settings
from database import queries as q
from follow import labels, provenance
from follow.base import BaseStreamingDataSource
from follow.helius_parse import parse_transaction

logger = logging.getLogger(__name__)


class WalletFlowWatcher(BaseStreamingDataSource):
    """Live Helius observer for the wallet + exchange-flow watchlist."""

    source_id    = "wallet_flow"
    display_name = "Wallet + Exchange-Flow Watcher"
    optional     = True

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        # Observed watch set (manual + confirmed + candidate). Candidates are
        # observed but never emit a signal — see _process_event.
        self._watchlist: set[str] = set()
        # In-memory stats (the DB is the durable record).
        self._events_seen = 0
        self._signals_emitted = 0
        self._last_event_ts: Optional[float] = None

    # ── Availability ────────────────────────────────────────────────────────

    def _helius_key(self) -> str:
        return os.getenv(getattr(settings, "WALLETFLOW_HELIUS_KEY_ENV",
                                 "HELIUS_API_KEY"), "")

    def is_available(self) -> bool:
        """Runnable only when enabled, the Helius key is present, AND the
        exchange-label set is loaded (without labels there is no flow to
        classify). False otherwise — the host agent then skips it silently."""
        if not bool(getattr(settings, "WALLETFLOW_ENABLED", False)):
            return False
        if not self._helius_key():
            return False
        return labels.is_available()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Open the Helius stream for the watchlist + exchange addresses.
        Degrades gracefully: if unavailable or the connection cannot be
        established, the watcher stays idle rather than raising to the host."""
        # Seed the shared label set if empty (idempotent), then re-check.
        try:
            labels.load_seed_labels()
        except Exception as e:
            logger.debug("walletflow: seed labels failed: %s", e)
        if not self.is_available():
            logger.info("walletflow: not available (enabled=%s, key=%s, labels=%s) "
                        "— observer idle",
                        getattr(settings, "WALLETFLOW_ENABLED", False),
                        bool(self._helius_key()), labels.is_available())
            self._running = False
            return
        self.refresh_watchlist()
        self._running = True
        self._task = asyncio.create_task(self._stream_loop())
        logger.info("walletflow: observing %d wallets via Helius",
                    len(self._watchlist))

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

    # ── Stream + event handling ─────────────────────────────────────────────

    async def _stream_loop(self) -> None:
        """Best-effort Helius websocket consume loop. Connection enrichment is
        Helius-specific; this stays defensive so a dead stream never crashes
        the host agent. Real message payloads are parsed by _handle_raw_tx."""
        try:
            import websockets  # lazy — only when actually streaming
        except Exception:
            logger.info("walletflow: `websockets` not installed — stream idle")
            return
        url = (f"wss://atlas-mainnet.helius-rpc.com/?api-key={self._helius_key()}")
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=30) as ws:
                    self._ws = ws
                    await self._subscribe(ws)
                    async for raw in ws:
                        if not self._running:
                            break
                        await self._on_raw_message(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("walletflow: stream reconnect after error: %s", e)
                await asyncio.sleep(5)

    async def _subscribe(self, ws) -> None:
        """Subscribe to transactions touching the watchlist. Helius's enhanced
        transaction subscription accepts an accountInclude filter."""
        import json
        try:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "transactionSubscribe",
                "params": [
                    {"accountInclude": list(self._watchlist)},
                    {"commitment": "confirmed", "encoding": "jsonParsed",
                     "transactionDetails": "full"},
                ],
            }))
        except Exception as e:
            logger.debug("walletflow: subscribe failed: %s", e)

    async def _on_raw_message(self, raw) -> None:
        """Decode one websocket frame into an enhanced-tx dict and handle it."""
        import json
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            tx = (((msg or {}).get("params") or {}).get("result") or {})
            if tx:
                self._handle_raw_tx(tx)
        except Exception as e:
            logger.debug("walletflow: bad message: %s", e)

    def _handle_raw_tx(self, tx: dict) -> list:
        """Parse one enhanced-tx into ActorEvents, persist each as suggestive
        evidence, and emit signals ONLY for signal-eligible wallets. Returns
        the parsed ActorEvents (for tests). Never raises."""
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
        """Write the flow event. Suggestive evidence — never a confirmed sell."""
        from datetime import datetime
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

    # ── Stats (for the snapshot) ────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Never raises — zeroed/empty defaults on any failure."""
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
        }

    def _safe_available(self) -> bool:
        try:
            return self.is_available()
        except Exception:
            return False


__all__ = ["WalletFlowWatcher"]
