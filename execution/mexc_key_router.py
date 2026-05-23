"""
execution/mexc_key_router.py

MEXC per-pair key router.

MEXC supports per-key pair allowlists — one account can hold many API
keys, each restricted to a different subset of pairs. This router maps
(symbol) → (ccxt.mexc client built with the right key) so the scalper
doesn't have to track which key covers which pair.

Inputs:
  settings.MEXC_PAIR_KEY_MAP      {symbol: key_index}     (1-based)
  env MEXC_KEY_{N}_API_KEY        per-key credentials
  env MEXC_KEY_{N}_SECRET

Behaviour:
  - Clients are constructed lazily on first request per key index.
  - Missing env vars are tolerated — get_client_for() returns None and
    the caller treats the pair as "can't trade on MEXC right now".
  - One account → one set of fees → FeeManager only needs any_client().

Singleton at module scope so tests can monkeypatch the env / map and
construct a fresh router via MexcKeyRouter() when isolation matters.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

try:
    import ccxt.async_support as ccxt
except Exception:                        # pragma: no cover — ccxt always present in venv
    ccxt = None                          # type: ignore

from config import settings

logger = logging.getLogger(__name__)


class MexcKeyRouter:
    """Lazily-built pool of ccxt.mexc clients keyed by per-pair key index."""

    def __init__(self):
        # key_index -> ccxt.mexc client (built on first request)
        self._clients: dict[int, object] = {}
        # key_indices we've already confirmed are missing in env — avoids
        # re-reading os.getenv on every call once a pair is known to be
        # unroutable.
        self._missing_indices: set[int] = set()

    # ── Public API ──────────────────────────────────────────────────────

    def get_client_for(self, symbol: str):
        """Return the ccxt.mexc client whose key holds `symbol`, or None.

        None when:
          - symbol isn't in MEXC_PAIR_KEY_MAP
          - the resolved key index has no env credentials
          - ccxt isn't importable
          - client construction raised
        """
        if ccxt is None:
            return None
        key_index = settings.MEXC_PAIR_KEY_MAP.get(symbol)
        if key_index is None:
            return None
        return self._client_for_index(key_index)

    def any_client(self):
        """Return any constructed/constructable MEXC client.

        FeeManager pre-warm doesn't care which key it talks to — MEXC
        fees are per-account, not per-key — so this picks the lowest
        configured index that has env credentials. None if no key on
        the account is usable.
        """
        seen_indices = sorted(set(settings.MEXC_PAIR_KEY_MAP.values()))
        for idx in seen_indices:
            client = self._client_for_index(idx)
            if client is not None:
                return client
        # Map may be empty but env vars set anyway — fall back to scanning
        # MEXC_KEY_1..MEXC_KEY_{MAX_SCAN} so a freshly-keyed install still
        # boots. 30 matches the realistic MEXC key cap (one per pair set).
        for idx in range(1, 31):
            if idx in seen_indices:
                continue
            client = self._client_for_index(idx)
            if client is not None:
                return client
        return None

    def has_route_for(self, symbol: str) -> bool:
        """True if a usable key covers this symbol. Pure sync, no client
        construction — for filter loops that just want to skip unroutable
        pairs without holding open a CCXT session."""
        key_index = settings.MEXC_PAIR_KEY_MAP.get(symbol)
        if key_index is None:
            return False
        if key_index in self._clients:
            return True
        if key_index in self._missing_indices:
            return False
        return bool(
            os.getenv(f"MEXC_KEY_{key_index}_API_KEY")
            and os.getenv(f"MEXC_KEY_{key_index}_SECRET")
        )

    async def close_all(self) -> None:
        """Close every constructed CCXT client. Idempotent."""
        for client in list(self._clients.values()):
            try:
                close = getattr(client, "close", None)
                if close is None:
                    continue
                res = close()
                if hasattr(res, "__await__"):
                    await res
            except Exception as e:
                logger.debug(f"MexcKeyRouter close: {e}")
        self._clients.clear()

    # ── Internal ────────────────────────────────────────────────────────

    def _client_for_index(self, key_index: int):
        client = self._clients.get(key_index)
        if client is not None:
            return client
        if key_index in self._missing_indices:
            return None

        api_key = os.getenv(f"MEXC_KEY_{key_index}_API_KEY")
        secret  = os.getenv(f"MEXC_KEY_{key_index}_SECRET")
        if not api_key or not secret:
            self._missing_indices.add(key_index)
            logger.debug(
                f"MexcKeyRouter: MEXC_KEY_{key_index}_API_KEY/SECRET not set "
                f"in env — pairs assigned to this key can't be traded"
            )
            return None

        if ccxt is None:
            return None
        try:
            client = ccxt.mexc({
                "apiKey":          api_key,
                "secret":          secret,
                "enableRateLimit": True,
                "options":         {"defaultType": "spot"},
            })
        except Exception as e:
            logger.warning(
                f"MexcKeyRouter: failed to construct MEXC_KEY_{key_index} "
                f"client — {e}"
            )
            return None
        self._clients[key_index] = client
        return client


# Module-level singleton — CLAUDE.md singleton pattern.
mexc_key_router = MexcKeyRouter()
