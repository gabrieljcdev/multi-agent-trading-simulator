"""
execution/funding_venues/binance.py

BinanceFundingVenue — the first funding venue plugin, wrapping the lazy
CCXT binance USD-M futures client. Behaviour is IDENTICAL to the engine's
former FundingEngine._fetch_native_funding (Phase 1): same defaultType=future
client, same fetchFundingRate → rate_8h, same annualisation (rate * 1095),
same open-interest gate fallback. The refactor moved the read here without
changing a single number, so the binance-only observation output is
unchanged — verified against tests/test_funding_arb.py.

Read-only public endpoints only. No keys required, no order placement.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

try:
    import ccxt.async_support as ccxt           # public endpoints only
except Exception:                               # pragma: no cover — venv carries ccxt
    ccxt = None                                 # type: ignore

from execution.funding_venues.base_funding import (
    BaseFundingVenue, FundingQuote, annualise_funding,
)

logger = logging.getLogger(__name__)


# Binance USD-M perps fund every 8h. The annualisation constant (1095) is
# derived from this interval via annualise_funding — pinned by the Phase-1
# tests. Keep the interval here, not a literal 1095, so a venue with a
# different funding cadence is a one-line change.
_BINANCE_FUNDING_INTERVAL_SEC = 8 * 3600        # 28_800


class BinanceFundingVenue(BaseFundingVenue):
    venue_id     = "binance"
    display_name = "Binance USD-M"
    key_env_var  = None          # public funding endpoints — no key needed
    optional     = True

    # Documented Binance USD-M perp fee tier (bps). Venue facts, not tunable
    # thresholds — same precedent as ArbitrumConnector.SWAP_GAS_UNITS. The
    # cross-venue break-even (Phase 2) reads these.
    TAKER_FEE_BPS = 4.0
    MAKER_FEE_BPS = 2.0

    def __init__(self, ccxt_factory=None):
        # Optional injection: tests supply a callable returning a stub
        # binance client (so fetchFundingRate is mockable without network).
        # None → build a real ccxt.async_support.binance() lazily.
        self._ccxt_factory = ccxt_factory
        self._exchange = None

    # ── Availability ────────────────────────────────────────────────────

    def is_available(self) -> bool:
        # Public funding reads need only ccxt importable (or an injected
        # stub factory). No API key gate.
        if self._ccxt_factory is not None:
            return True
        return ccxt is not None

    # ── Lazy client (moved verbatim from FundingEngine._get_exchange) ────

    def _get_exchange(self):
        """Return a cached binance ccxt client built lazily.

        defaultType=future is REQUIRED — ccxt's binance.fetch_funding_rate
        raises NotSupported on spot ("supports linear and inverse contracts
        only"). Without it every scan tick silently dropped every symbol.
        Verified via direct A/B test on 2026-05-29.
        """
        if self._exchange is not None:
            return self._exchange
        if self._ccxt_factory is not None:
            self._exchange = self._ccxt_factory()
            return self._exchange
        if ccxt is None:
            return None
        try:
            self._exchange = ccxt.binance({
                "enableRateLimit": True,
                "options": {"defaultType": "future"},
            })
        except Exception as e:
            logger.debug(f"BinanceFundingVenue: binance() failed: {e}")
            self._exchange = None
        return self._exchange

    # ── fetch_funding (the read) ────────────────────────────────────────

    async def fetch_funding(self, symbol: str) -> FundingQuote:
        """Funding APR + OI for one symbol. Never raises — error→FundingQuote
        with error set and funding_apr=None."""
        interval = float(_BINANCE_FUNDING_INTERVAL_SEC)
        ex = self._get_exchange()
        if ex is None:
            return FundingQuote(
                venue=self.venue_id, symbol=symbol, funding_apr=None,
                funding_interval_sec=interval, ts=time.time(),
                error="exchange_unavailable",
            )

        try:
            payload = await ex.fetch_funding_rate(symbol)
        except Exception as e:
            logger.debug(f"BinanceFundingVenue fetch_funding_rate {symbol}: {e}")
            return FundingQuote(
                venue=self.venue_id, symbol=symbol, funding_apr=None,
                funding_interval_sec=interval, ts=time.time(),
                error=f"funding_fetch_failed:{e}",
            )

        # CCXT shapes 'fundingRate' as a fractional 8h rate (e.g. 0.0001).
        # Fall through to the nested 'info' block when the top-level is None.
        rate_8h = payload.get("fundingRate")
        if rate_8h is None:
            info = payload.get("info") or {}
            rate_8h = info.get("lastFundingRate") or info.get("fundingRate")
        try:
            rate_8h = float(rate_8h) if rate_8h is not None else None
        except (TypeError, ValueError):
            rate_8h = None
        if rate_8h is None:
            return FundingQuote(
                venue=self.venue_id, symbol=symbol, funding_apr=None,
                funding_interval_sec=interval, ts=time.time(),
                error="no_funding_rate",
            )

        funding_apr = annualise_funding(rate_8h, interval)      # rate_8h * 1095

        mark_price  = _safe_float(payload.get("markPrice"))
        index_price = _safe_float(payload.get("indexPrice"))

        # Open interest is optional: honour the OI gate when CCXT exposes it,
        # otherwise record 0.0 (engine treats 0 as "no info, pass"). Guard
        # exactly as the former engine did — getattr + callable — so a stub
        # without fetch_open_interest is tolerated.
        oi_usd = 0.0
        try:
            fetch_oi = getattr(ex, "fetch_open_interest", None)
            if callable(fetch_oi):
                oi_payload = await fetch_oi(symbol)
                if oi_payload is not None:
                    raw = (oi_payload.get("openInterestAmount")
                           or oi_payload.get("openInterestValue")
                           or oi_payload.get("openInterest"))
                    try:
                        oi_usd = float(raw or 0.0)
                    except (TypeError, ValueError):
                        oi_usd = 0.0
        except Exception as e:
            logger.debug(f"BinanceFundingVenue fetch_open_interest {symbol}: {e}")

        return FundingQuote(
            venue=self.venue_id,
            symbol=symbol,
            funding_apr=funding_apr,
            funding_interval_sec=interval,
            mark_price=mark_price,
            index_price=index_price,
            oi_usd=oi_usd,
            depth_ok=True,
            taker_fee_bps=self.TAKER_FEE_BPS,
            maker_fee_bps=self.MAKER_FEE_BPS,
            ts=time.time(),
            error=None,
        )

    # ── list_perps ──────────────────────────────────────────────────────

    async def list_perps(self) -> list[str]:
        """The binance USD-M swap universe (unified symbols). Empty on any
        failure. Cheap-cached via ccxt's own market cache."""
        ex = self._get_exchange()
        if ex is None:
            return []
        try:
            markets = await ex.load_markets()
        except Exception as e:
            logger.debug(f"BinanceFundingVenue list_perps: {e}")
            return []
        out = []
        for sym, m in (markets or {}).items():
            try:
                if m.get("swap") and m.get("quote") in ("USDT", "USD"):
                    out.append(sym)
            except Exception:
                continue
        return out

    async def close(self) -> None:
        ex = self._exchange
        if ex is None:
            return
        try:
            close = getattr(ex, "close", None)
            if close is None:
                return
            res = close()
            import asyncio
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            logger.debug(f"BinanceFundingVenue close: {e}")


def _safe_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
