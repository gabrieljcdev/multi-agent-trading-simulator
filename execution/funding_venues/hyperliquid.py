"""
execution/funding_venues/hyperliquid.py

HyperliquidFundingVenue — the second funding venue plugin. Reads funding +
mark/index + open interest from the CCXT `hyperliquid` client over its
READ-ONLY public REST API. No keys, no order placement.

Rate-limit discipline (the public REST budget is ~100 req/min): a scan of
N symbols must NOT be N+ network calls. ccxt exposes fetchFundingRates and
fetchOpenInterests in BULK, so this venue fetches both once and serves
every symbol from a short-TTL cache (FUNDING_HL_BULK_TTL_SEC). A bulk fetch
that trips the venue's rate limit sets error="rate_limited" on the served
quotes and backs off rather than hammering — never raises into the scan.

Symbol space: the observer's canonical symbols are "BTC/USDT"-style;
Hyperliquid's unified perp symbols are "BTC/USDC:USDC"-style. We resolve by
BASE coin (BTC, ETH, …) so the same asset lines up across venues for the
cross-venue carry math.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

try:
    import ccxt.async_support as ccxt           # public endpoints only
except Exception:                               # pragma: no cover — venv carries ccxt
    ccxt = None                                 # type: ignore

from config import settings
from execution.funding_venues.base_funding import (
    BaseFundingVenue, FundingQuote, annualise_funding,
)

logger = logging.getLogger(__name__)


# Hyperliquid perps fund hourly. Annualisation (rate * 8760) is derived from
# this interval via annualise_funding — kept here, not as a literal, so the
# cross-venue diff normalises Binance-8h against Hyperliquid-1h correctly.
_HL_FUNDING_INTERVAL_SEC = 3600


def _base_of(symbol: str) -> str:
    """Extract the base coin from a unified symbol.

    "BTC/USDT" -> "BTC"; "BTC/USDC:USDC" -> "BTC"; "kPEPE/USDC:USDC" -> "kPEPE".
    Used to line up the same asset across venues with different quote/settle.
    """
    if not symbol:
        return ""
    head = symbol.split(":", 1)[0]          # drop settle suffix
    return head.split("/", 1)[0].strip().upper()


class HyperliquidFundingVenue(BaseFundingVenue):
    venue_id     = "hyperliquid"
    display_name = "Hyperliquid"
    # Optional public-endpoint override key only — NEVER a trading key. When
    # unset, the default public REST is used and the venue is still available.
    key_env_var  = None
    optional     = True

    # Documented Hyperliquid perp fee tier (bps). Venue facts (cf.
    # BinanceFundingVenue.TAKER_FEE_BPS); the cross-venue break-even reads them.
    TAKER_FEE_BPS = 4.5
    MAKER_FEE_BPS = 1.5

    def __init__(self, ccxt_factory=None):
        # Tests inject a stub client exposing fetch_funding_rates /
        # fetch_open_interests / load_markets. None → real ccxt.hyperliquid().
        self._ccxt_factory = ccxt_factory
        self._exchange = None

        # Bulk caches (symbol-keyed) + their stamp. Refreshed at most once per
        # FUNDING_HL_BULK_TTL_SEC so an N-symbol scan is ~2 network calls.
        self._funding_cache: dict = {}
        self._oi_cache: dict = {}
        self._markets: dict = {}
        self._base_index: dict = {}          # base coin -> hl unified symbol
        self._cache_ts: float = 0.0
        self._rate_limited: bool = False

    # ── Availability ────────────────────────────────────────────────────

    def is_available(self) -> bool:
        if self._ccxt_factory is not None:
            return True
        if ccxt is None:
            return False
        # Public reads — available whenever the optional override key rule
        # (none required) and ccxt are satisfied.
        return super().is_available()

    # ── Lazy client ─────────────────────────────────────────────────────

    def _get_exchange(self):
        if self._exchange is not None:
            return self._exchange
        if self._ccxt_factory is not None:
            self._exchange = self._ccxt_factory()
            return self._exchange
        if ccxt is None:
            return None
        try:
            opts: dict = {"enableRateLimit": True}
            api_url = settings_getenv("HYPERLIQUID_API_URL")
            ex = ccxt.hyperliquid(opts)
            if api_url:
                # Optional public-endpoint override (rate-limit relief only).
                try:
                    ex.urls["api"] = api_url
                except Exception:
                    pass
            self._exchange = ex
        except Exception as e:
            logger.debug(f"HyperliquidFundingVenue: hyperliquid() failed: {e}")
            self._exchange = None
        return self._exchange

    # ── Bulk refresh (rate-limit-respecting) ────────────────────────────

    def _cache_fresh(self) -> bool:
        ttl = float(getattr(settings, "FUNDING_HL_BULK_TTL_SEC", 30) or 30)
        return (time.time() - self._cache_ts) < ttl and bool(self._funding_cache)

    async def _refresh_bulk(self) -> None:
        """Refresh the funding + OI + market caches in at most a couple of
        public calls. Sets self._rate_limited on a venue rate-limit and
        returns without raising — served quotes carry error='rate_limited'."""
        if self._cache_fresh():
            return
        ex = self._get_exchange()
        if ex is None:
            self._rate_limited = False
            return

        # Markets (for base-coin resolution) — cached cheaply by ccxt too.
        try:
            markets = await ex.load_markets()
            self._markets = markets or {}
            self._base_index = {}
            for sym, m in self._markets.items():
                try:
                    if m.get("swap"):
                        self._base_index.setdefault(_base_of(sym), sym)
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"HyperliquidFundingVenue load_markets: {e}")

        # Bulk funding — one call for the whole universe.
        try:
            self._funding_cache = await ex.fetch_funding_rates() or {}
            self._rate_limited = False
        except Exception as e:
            if _is_rate_limit(e):
                logger.warning("HyperliquidFundingVenue: funding rate-limited — backing off")
                self._rate_limited = True
                return
            logger.debug(f"HyperliquidFundingVenue fetch_funding_rates: {e}")
            self._funding_cache = {}

        # Bulk open interest — best-effort; absence just means oi_usd=0.
        try:
            fetch_ois = getattr(ex, "fetch_open_interests", None)
            if callable(fetch_ois):
                self._oi_cache = await fetch_ois() or {}
        except Exception as e:
            logger.debug(f"HyperliquidFundingVenue fetch_open_interests: {e}")
            self._oi_cache = {}

        self._cache_ts = time.time()

    # ── fetch_funding ───────────────────────────────────────────────────

    async def fetch_funding(self, symbol: str) -> FundingQuote:
        interval = float(_HL_FUNDING_INTERVAL_SEC)
        ex = self._get_exchange()
        if ex is None:
            return FundingQuote(
                venue=self.venue_id, symbol=symbol, funding_apr=None,
                funding_interval_sec=interval, ts=time.time(),
                error="exchange_unavailable",
            )

        await self._refresh_bulk()
        if self._rate_limited:
            return FundingQuote(
                venue=self.venue_id, symbol=symbol, funding_apr=None,
                funding_interval_sec=interval, ts=time.time(),
                error="rate_limited",
            )

        hl_symbol = self._resolve(symbol)
        if hl_symbol is None:
            return FundingQuote(
                venue=self.venue_id, symbol=symbol, funding_apr=None,
                funding_interval_sec=interval, ts=time.time(),
                error="symbol_not_listed",
            )

        fstruct = self._funding_cache.get(hl_symbol)
        if not fstruct:
            return FundingQuote(
                venue=self.venue_id, symbol=symbol, funding_apr=None,
                funding_interval_sec=interval, ts=time.time(),
                error="no_funding_rate",
            )

        rate_1h = fstruct.get("fundingRate")
        try:
            rate_1h = float(rate_1h) if rate_1h is not None else None
        except (TypeError, ValueError):
            rate_1h = None
        if rate_1h is None:
            return FundingQuote(
                venue=self.venue_id, symbol=symbol, funding_apr=None,
                funding_interval_sec=interval, ts=time.time(),
                error="no_funding_rate",
            )

        funding_apr = annualise_funding(rate_1h, interval)      # rate_1h * 8760
        mark_price  = _safe_float(fstruct.get("markPrice"))
        index_price = _safe_float(fstruct.get("indexPrice"))
        oi_usd      = self._oi_usd_for(hl_symbol, mark_price)

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

    def _resolve(self, symbol: str) -> Optional[str]:
        """Map a canonical symbol to this venue's unified symbol by base coin."""
        if symbol in self._funding_cache:
            return symbol
        return self._base_index.get(_base_of(symbol))

    def _oi_usd_for(self, hl_symbol: str, mark_price: float) -> float:
        oi = self._oi_cache.get(hl_symbol) if isinstance(self._oi_cache, dict) else None
        if not oi:
            return 0.0
        val = oi.get("openInterestValue")
        if val is not None:
            return _safe_float(val)
        amount = _safe_float(oi.get("openInterestAmount") or oi.get("openInterest"))
        return amount * mark_price if mark_price > 0 else 0.0

    # ── list_perps ──────────────────────────────────────────────────────

    async def list_perps(self) -> list[str]:
        """The Hyperliquid perp universe (unified symbols). Empty on failure."""
        await self._refresh_bulk()
        if self._markets:
            return [s for s, m in self._markets.items()
                    if _safe_get(m, "swap")]
        # Fall back to whatever funding keys we have if markets didn't load.
        return list(self._funding_cache.keys())

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
            logger.debug(f"HyperliquidFundingVenue close: {e}")


# ── small helpers ───────────────────────────────────────────────────────

def _safe_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _safe_get(m, key):
    try:
        return m.get(key)
    except Exception:
        return None


def _is_rate_limit(exc: Exception) -> bool:
    """True when an exception looks like a venue rate-limit / DDoS guard."""
    name = type(exc).__name__.lower()
    if "ratelimit" in name or "ddos" in name:
        return True
    return "rate limit" in str(exc).lower() or "429" in str(exc)


def settings_getenv(name: str) -> str:
    """os.getenv indirection kept tiny + importable; never reads a trading key."""
    import os
    return os.getenv(name, "") or ""
