"""
follow/rpc.py

One rate-limited, backoff-aware client for the FREE public Solana RPC. Public
endpoints ban abusers, so EVERY RPC call in the follow/ module goes through this
single client: it never bursts (a token-spacing RateLimiter caps RPS) and it
backs off exponentially on 429 / timeout. The funding crawl and discovery's
historical mining share this one limiter — a slow background crawl is acceptable
for an observer; parallel-blasting a public endpoint is not.

The HTTP transport and the sleep/clock are injectable so the limiter and the
backoff loop are unit-testable with NO real network (see tests 23/25).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from config import settings

logger = logging.getLogger(__name__)


class RpcThrottled(Exception):
    """Raised by the transport on a 429 / timeout so the client backs off."""


class RateLimiter:
    """Token-spacing limiter: guarantees >= 1/max_rps seconds between acquires.
    time_fn/sleep_fn are injectable for deterministic tests."""

    def __init__(self, max_rps: float, *,
                 time_fn: Optional[Callable[[], float]] = None,
                 sleep_fn: Optional[Callable[[float], Awaitable[None]]] = None):
        self._min_interval = 1.0 / max(0.1, float(max_rps))
        self._next = 0.0
        self._time = time_fn or time.monotonic
        self._sleep = sleep_fn or asyncio.sleep

    async def acquire(self) -> None:
        now = self._time()
        if now < self._next:
            await self._sleep(self._next - now)
        # Re-read the clock (it advances during the sleep) before scheduling
        # the next slot, so spacing holds without drift.
        self._next = max(self._time(), self._next) + self._min_interval


class SolanaRpc:
    """Throttled async JSON-RPC client. All calls pass through the limiter and
    a bounded exponential-backoff retry loop on RpcThrottled."""

    def __init__(self, url: Optional[str] = None, ws_url: Optional[str] = None, *,
                 limiter: Optional[RateLimiter] = None,
                 max_retries: Optional[int] = None,
                 backoff_base: Optional[float] = None,
                 transport: Optional[Callable[[dict], Awaitable[dict]]] = None,
                 sleep_fn: Optional[Callable[[float], Awaitable[None]]] = None):
        self.url = url or settings.WALLETFLOW_RPC_URL
        self.ws_url = ws_url or settings.WALLETFLOW_RPC_WS_URL
        self._limiter = limiter or RateLimiter(
            float(getattr(settings, "WALLETFLOW_RPC_MAX_RPS", 4)))
        self._max_retries = (int(getattr(settings, "WALLETFLOW_RPC_MAX_RETRIES", 5))
                             if max_retries is None else int(max_retries))
        self._backoff_base = (float(getattr(settings, "WALLETFLOW_RPC_BACKOFF_BASE_S", 1.0))
                              if backoff_base is None else float(backoff_base))
        self._transport = transport or self._http_transport
        self._sleep = sleep_fn or asyncio.sleep
        self._session = None
        # Observability counters (read by the watcher's get_stats).
        self.calls = 0
        self.throttle_events = 0

    async def call(self, method: str, params: list) -> Optional[dict]:
        """One throttled JSON-RPC call with backoff. Returns the `result`
        field, or None on exhausted retries / RPC error."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire()
            try:
                self.calls += 1
                resp = await self._transport(payload)
                return (resp or {}).get("result")
            except RpcThrottled:
                self.throttle_events += 1
                if attempt >= self._max_retries:
                    logger.debug("rpc %s: throttled, retries exhausted", method)
                    return None
                await self._sleep(self._backoff_base * (2 ** attempt))
            except Exception as e:
                logger.debug("rpc %s failed: %s", method, e)
                return None
        return None

    async def get_transaction(self, signature: str) -> Optional[dict]:
        return await self.call("getTransaction", [
            signature,
            {"encoding": "jsonParsed", "commitment": "confirmed",
             "maxSupportedTransactionVersion": 0},
        ])

    async def get_signatures_for_address(self, address: str, *,
                                         before: Optional[str] = None,
                                         limit: int = 1000) -> list[dict]:
        opts: dict = {"limit": int(limit)}
        if before:
            opts["before"] = before
        res = await self.call("getSignaturesForAddress", [address, opts])
        return res or []

    # ── Default HTTP transport (lazy aiohttp; never used in unit tests) ──────

    async def _http_transport(self, payload: dict) -> dict:
        import aiohttp
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.post(self.url, json=payload, timeout=20) as r:
                if r.status == 429:
                    raise RpcThrottled("429")
                return await r.json()
        except asyncio.TimeoutError as e:
            raise RpcThrottled("timeout") from e

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None


__all__ = ["RateLimiter", "SolanaRpc", "RpcThrottled"]
