"""
agents/scalping_agent.py

Scalping agent driven by Order Flow Imbalance (OFI) as the *primary*
entry signal — not a confirmation layer (which is what the existing
SignalAgent uses OFI for). Entry fires when the order book shows a
statistically significant, persistent, trade-confirmed imbalance in
one direction; nothing else (RSI, MACD, candles) participates.

Exchange routing
----------------
Different strategies belong on different exchanges based on fee
structure, liquidity depth, and where the relevant participants trade.
Scalping is fee-sensitive above all — it only works where round-trip
fees are near zero. settings.STRATEGY_EXCHANGE_MAP["scalp"] is the
approved list. Adding a venue is one line of settings, no code change.

Fee-aware TP/SL
---------------
TP and SL are not fixed values. FeeManager queries CCXT for taker/maker
fees per (exchange, symbol), caches them, and applies overrides for
known CCXT inaccuracies. From those fees the agent derives:

  tp_bps = round_trip_bps + SCALP_NET_PROFIT_TARGET_BPS
  sl_bps = tp_bps / SCALP_RR_RATIO
  breakeven_win_rate = (round_trip_bps + sl_bps) / (tp_bps + sl_bps)

If the breakeven win rate exceeds SCALP_MAX_BREAKEVEN_WIN_RATE the
trade is refused. That's how a "Binance is too expensive for scalping"
decision becomes data-driven rather than an opinion.

Observation mode
----------------
SCALP_CAPITAL == 0.0 by default. Every potential entry is evaluated
and logged to the scalp_observations table with its fee context, but
no orders are placed. Once 48h of logs confirm edge (win_rate > 52%,
avg_net_bps > 0 — see database/queries.get_scalp_summary), set
SCALP_CAPITAL > 0 and supply the MEXC keys.

Live wiring (no longer stubs)
-----------------------------
  _get_mid_price(symbol, exchange)   → MarketData.get_price + fallback
  _get_spread_bps(symbol, exchange)  → MarketData.get_spread_bps
  _get_regime(symbol)                → regime_detector.get_primary
  _get_btc_1m_change()               → MarketData.get_change_pct
                                       (fed by orderbook sample buffer)
  _get_ccxt_exchange(exchange_id, symbol=...) → MEXC routes through
      execution.mexc_key_router (one account, many pair-restricted
      keys); other exchanges fall through to market_data._exchanges.

Execution
---------
  _place_order(...)  → SIM_MODE: persists a sim Trade row (mirrors the
      signal agent's OrderRouter._sim_execute) so scalp fills land in the
      trades table + P&L queries. Live execution (SIM_MODE=False) is the
      remaining follow-up → execution/router.py.

When MarketData is absent (offline / test mode), every getter falls
through to its permissive default so observation mode still hums
along.

# TODO (dashboard, Phase 2): expose self._observations[-15:] as a
# property so ui/dashboard.py can render a "scalp feed" panel alongside
# the existing signal_feed. Suggested columns: timestamp, symbol:exchange,
# direction, ofi_z, tp_bps/sl_bps, exit_reason, pnl_net_bps. Slot it in
# as Panel 19 of dashboard.py — get_observation_summary already returns
# the aggregate stats that panel header would show.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Optional

import numpy as np

from config import settings
from database import queries as db_queries

from agents.base import (
    AgentStats, BaseAgent,
    RUNNING, HALTED, OFFLINE, STOPPED,
)

log = logging.getLogger(__name__)


class _LazyMarketData:
    """Forwards v2 accessor calls to the agent's resolved MarketData,
    re-resolving on each access so the ConfluenceChecker / ATRStopCalculator
    (built in __init__, before market_data is wired) always reach the live
    feed. Raises AttributeError when md isn't available yet — the gates treat
    that as 'data unavailable' and fail open."""

    __slots__ = ("_resolve",)

    def __init__(self, resolver):
        self._resolve = resolver

    def __getattr__(self, name):
        md = self._resolve()
        if md is None:
            raise AttributeError(f"market_data unavailable for {name!r}")
        return getattr(md, name)


# ─────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class BookSnap:
    ts:   float
    bids: list   # [(price, qty), ...] length >= SCALP_OFI_LEVELS
    asks: list


@dataclass
class ScalpPosition:
    symbol:              str
    exchange:            str
    direction:           str            # 'LONG' or 'SHORT'
    entry_price:         float
    entry_time:          float
    entry_ofi_z:         float
    entry_tfi:           float
    size_usd:            float
    tp_price:            float
    sl_price:            float
    tp_bps:              float
    sl_bps:              float
    round_trip_cost_bps: float
    observation_only:    bool
    # Sim Trade row id (set by _place_order when observation_only is False)
    # so _exit_position can close the same row. None in observation mode.
    trade_id:            Optional[int] = None


@dataclass
class ScalpObservation:
    symbol:                str
    exchange:              str
    timestamp:             float
    ofi_z:                 float
    direction:             str
    strength:              str
    tfi_confirms:          bool
    raw_tfi:               float
    spread_bps:            float
    regime:                str
    round_trip_cost_bps:   float
    min_win_rate_required: float
    tp_bps:                float
    sl_bps:                float
    would_entry:           bool
    skip_reason:           str
    entry_price:           float
    exit_price:            float = 0.0
    exit_time:             float = 0.0
    exit_reason:           str   = ""
    hold_sec:              float = 0.0
    pnl_bps:               float = 0.0
    pnl_usd:               float = 0.0
    observation_only:      bool  = True
    # Micro price tracker backfills these on closed observations so the
    # analysis SQL can compare "did OFI predict" vs "what did we realise
    # at exit". 0.0 = not yet sampled.
    price_30s:             float = 0.0
    price_1m:              float = 0.0
    price_3m:              float = 0.0
    price_5m:              float = 0.0
    # v2 selectivity diagnostics — None on v1 / pre-v2 rows so old data stays
    # valid. Populated by the confluence + ATR layer in _evaluate_entry.
    confluence_score:      Optional[int]   = None
    strength_label:        Optional[str]   = None
    cross_exchange_agrees: Optional[bool]  = None
    btc_compatible:        Optional[bool]  = None
    adverse_selection_ok:  Optional[bool]  = None
    depth_ok:              Optional[bool]  = None
    vwap_aligned:          Optional[bool]  = None
    htf_aligned:           Optional[bool]  = None
    volume_adequate:       Optional[bool]  = None
    atr_bps:               Optional[float] = None
    atr_adjusted:          Optional[bool]  = None
    sl_clamped:            Optional[str]   = None
    rr_actual:             Optional[float] = None


# ─────────────────────────────────────────────────────────────────────────
# FeeManager
# ─────────────────────────────────────────────────────────────────────────

class FeeManager:
    """Caches CCXT-derived maker/taker fees per (exchange, symbol),
    applies overrides for known wrong values, and derives the dynamic
    TP/SL + breakeven win rate the entry gate consumes.

    Never raises — falls through override → default on every error path."""

    def __init__(self, overrides: dict):
        # cache[exchange][symbol] = {"maker_bps": float, "taker_bps": float,
        #                            "source": "override"|"ccxt"|"default"}
        self._cache: dict[str, dict[str, dict]] = {}
        self._overrides = overrides or {}

    async def load_exchange(self, exchange_id: str, ccxt_exchange: Any) -> None:
        """Populate the cache for one exchange. Safe to call multiple times.

        If ccxt_exchange is None (stub mode / offline) we skip the
        network call and emit only the override / default-backed entries
        the gate needs. Always logs the resulting bps per symbol so the
        operator can see what the agent thinks fees are at startup.
        """
        self._cache.setdefault(exchange_id, {})

        ov = self._overrides.get(exchange_id, {})
        ov_maker = ov.get("maker")
        ov_taker = ov.get("taker")

        symbols = list(settings.SCALP_PAIRS)

        if ccxt_exchange is None:
            for sym in symbols:
                if ov_maker is not None and ov_taker is not None:
                    self._cache[exchange_id][sym] = {
                        "maker_bps": float(ov_maker),
                        "taker_bps": float(ov_taker),
                        "source":    "override",
                    }
                    log.info(
                        "[FeeManager] %s: %s maker=%.2fbps taker=%.2fbps (override)",
                        exchange_id, sym, ov_maker, ov_taker,
                    )
                else:
                    self._cache[exchange_id][sym] = {
                        "maker_bps": float(settings.SCALP_FEE_DEFAULT_BPS),
                        "taker_bps": float(settings.SCALP_FEE_DEFAULT_BPS),
                        "source":    "default",
                    }
                    log.warning(
                        "[FeeManager] %s: %s using default %.2fbps "
                        "(no CCXT, no override)",
                        exchange_id, sym, settings.SCALP_FEE_DEFAULT_BPS,
                    )
            return

        try:
            await ccxt_exchange.load_markets()
        except Exception as e:
            log.warning("[FeeManager] %s: load_markets failed (%s) — using overrides",
                        exchange_id, e)

        for sym in symbols:
            try:
                mkt = (getattr(ccxt_exchange, "markets", {}) or {}).get(sym) or {}
                ccxt_maker = mkt.get("maker")
                ccxt_taker = mkt.get("taker")
                if ccxt_maker is None or ccxt_taker is None:
                    raise ValueError("ccxt fee fields absent")
                maker_bps = float(ccxt_maker) * 10000
                taker_bps = float(ccxt_taker) * 10000
                source    = "ccxt"
            except Exception as e:
                log.debug("[FeeManager] %s %s: ccxt fee read failed (%s)",
                          exchange_id, sym, e)
                maker_bps = float(ov_maker) if ov_maker is not None \
                    else float(settings.SCALP_FEE_DEFAULT_BPS)
                taker_bps = float(ov_taker) if ov_taker is not None \
                    else float(settings.SCALP_FEE_DEFAULT_BPS)
                source    = "override" if ov_maker is not None else "default"

            # Per-exchange override wins over CCXT even on success — the
            # whole point of overrides is "CCXT is wrong here".
            if ov_maker is not None and ov_taker is not None:
                maker_bps = float(ov_maker)
                taker_bps = float(ov_taker)
                source    = "override"

            self._cache[exchange_id][sym] = {
                "maker_bps": maker_bps,
                "taker_bps": taker_bps,
                "source":    source,
            }
            log.info(
                "[FeeManager] %s: %s maker=%.2fbps taker=%.2fbps (%s)",
                exchange_id, sym, maker_bps, taker_bps, source,
            )

    def get_fees(self, exchange_id: str, symbol: str) -> dict:
        ex_cache = self._cache.get(exchange_id) or {}
        fees = ex_cache.get(symbol)
        if fees is not None:
            return dict(fees)
        ov = self._overrides.get(exchange_id, {})
        if "maker" in ov and "taker" in ov:
            return {
                "maker_bps": float(ov["maker"]),
                "taker_bps": float(ov["taker"]),
                "source":    "override",
            }
        return {
            "maker_bps": float(settings.SCALP_FEE_DEFAULT_BPS),
            "taker_bps": float(settings.SCALP_FEE_DEFAULT_BPS),
            "source":    "default",
        }

    def round_trip_bps(self, exchange_id: str, symbol: str) -> float:
        """Conservative: assumes market orders both legs. Limit-order
        execution would swap to maker_bps * 2."""
        fees = self.get_fees(exchange_id, symbol)
        return float(fees["taker_bps"]) * 2.0

    def compute_tp_sl(self, exchange_id: str, symbol: str) -> tuple[float, float]:
        rt = self.round_trip_bps(exchange_id, symbol)
        tp = rt + float(settings.SCALP_NET_PROFIT_TARGET_BPS)
        rr = float(settings.SCALP_RR_RATIO)
        sl = tp / rr if rr > 0 else tp
        return tp, sl

    def breakeven_win_rate(
        self,
        exchange_id: str,
        symbol: str,
        tp_bps: float,
        sl_bps: float,
    ) -> float:
        rt = self.round_trip_bps(exchange_id, symbol)
        denom = tp_bps + sl_bps
        if denom <= 0:
            return 1.0
        return (rt + sl_bps) / denom

    def is_viable(self, exchange_id: str, symbol: str) -> tuple[bool, str]:
        tp, sl = self.compute_tp_sl(exchange_id, symbol)
        be = self.breakeven_win_rate(exchange_id, symbol, tp, sl)
        limit = float(settings.SCALP_MAX_BREAKEVEN_WIN_RATE)
        if be > limit:
            rt = self.round_trip_bps(exchange_id, symbol)
            return False, (
                f"Fee breakeven {be:.1%} on {exchange_id} exceeds limit "
                f"{limit:.1%} (round_trip={rt:.1f}bps, "
                f"tp={tp:.1f}bps, sl={sl:.1f}bps)"
            )
        return True, ""


# ─────────────────────────────────────────────────────────────────────────
# OFIEngine — Cont-Kukanov-Stoikov multi-level OFI, keyed by (sym, ex)
# ─────────────────────────────────────────────────────────────────────────

class OFIEngine:
    """Multi-level Order Flow Imbalance estimator.

    State is keyed by (symbol, exchange) tuples flattened to
    "symbol:exchange" strings so MEXC BTC/USDT and Bitget BTC/USDT
    have completely independent order books, z-scores, and persistence
    counters.
    """

    def __init__(self, levels: int, window_sec: float, zscore_window: int):
        self._levels        = max(1, int(levels))
        self._window_sec    = float(window_sec)
        self._zscore_window = int(zscore_window)

        # Last book snap per key — used to derive e_n on the next snap.
        self._last_book: dict[str, BookSnap] = {}
        # In-progress bucket: list of e_n events accumulated within this window.
        self._cur_bucket: dict[str, list[float]]   = defaultdict(list)
        # In-progress traded volume this window, split by side, so the bucket
        # TFI is a volume-weighted imbalance ratio (buy-sell)/(buy+sell).
        self._cur_buy_vol:  dict[str, float]       = defaultdict(float)
        self._cur_sell_vol: dict[str, float]       = defaultdict(float)
        # Bucket close times.
        self._bucket_start: dict[str, float]       = {}
        # Closed buckets — deque of bucket-level OFI sums.
        self._ofi_buckets: dict[str, deque]        = defaultdict(
            lambda: deque(maxlen=self._zscore_window * 2)
        )
        # Latest snapshot stats.
        self._last_z:            dict[str, float] = defaultdict(float)
        self._last_tfi:          dict[str, float] = defaultdict(float)
        self._last_bucket_close: dict[str, float] = {}
        # Direction-persistence counter.
        self._persist:           dict[str, int]   = defaultdict(int)

    # ── Internal helpers ────────────────────────────────────────────────

    @staticmethod
    def _key(symbol: str, exchange: str) -> str:
        return f"{symbol}:{exchange}"

    def _maybe_close_bucket(self, key: str, now: float) -> None:
        start = self._bucket_start.get(key)
        if start is None:
            self._bucket_start[key] = now
            return
        if (now - start) < self._window_sec:
            return
        bucket_ofi = float(sum(self._cur_bucket[key]))
        # Volume-weighted trade-flow imbalance in [-1, 1]: net signed volume
        # normalised by total traded volume this window. Sign is unchanged
        # from the raw signed-volume version, so tfi_confirms is unaffected.
        buy_vol   = float(self._cur_buy_vol[key])
        sell_vol  = float(self._cur_sell_vol[key])
        total_vol = buy_vol + sell_vol
        bucket_tfi = (buy_vol - sell_vol) / total_vol if total_vol > 0 else 0.0
        self._ofi_buckets[key].append(bucket_ofi)
        self._last_tfi[key] = bucket_tfi
        # Reset accumulators for the next window.
        self._cur_bucket[key].clear()
        self._cur_buy_vol[key]  = 0.0
        self._cur_sell_vol[key] = 0.0
        self._bucket_start[key] = now
        self._last_bucket_close[key] = now
        # Recompute z-score from the rolling window.
        buckets = list(self._ofi_buckets[key])
        if len(buckets) >= 10:
            window = buckets[-self._zscore_window:]
            arr = np.array(window, dtype=float)
            mean = float(arr.mean())
            std  = float(arr.std(ddof=0))
            if std == 0.0:
                z = 0.0
            else:
                z = (bucket_ofi - mean) / std
            self._last_z[key] = z
        else:
            self._last_z[key] = 0.0

    def _compute_e_n(self, prev: BookSnap, curr: BookSnap) -> float:
        """Cont-Kukanov-Stoikov event increment across SCALP_OFI_LEVELS."""
        weights = settings.SCALP_DEPTH_WEIGHTS
        max_levels = min(self._levels, len(prev.bids), len(prev.asks),
                         len(curr.bids), len(curr.asks))
        total = 0.0
        for m in range(max_levels):
            w = float(weights.get(m, 0.0))
            if w == 0.0:
                continue
            bp_prev, bq_prev = prev.bids[m]
            bp_curr, bq_curr = curr.bids[m]
            ap_prev, aq_prev = prev.asks[m]
            ap_curr, aq_curr = curr.asks[m]
            # bid delta
            if bp_curr > bp_prev:
                bid_d = bq_curr
            elif bp_curr == bp_prev:
                bid_d = bq_curr - bq_prev
            else:
                bid_d = -bq_prev
            # ask delta
            if ap_curr < ap_prev:
                ask_d = aq_curr
            elif ap_curr == ap_prev:
                ask_d = aq_curr - aq_prev
            else:
                ask_d = -aq_prev
            total += w * (bid_d - ask_d)
        return total

    # ── Public API ──────────────────────────────────────────────────────

    def on_book(
        self,
        symbol: str,
        exchange: str,
        bids: list,
        asks: list,
    ) -> None:
        key = self._key(symbol, exchange)
        now = time.time()
        curr = BookSnap(ts=now, bids=list(bids), asks=list(asks))
        prev = self._last_book.get(key)
        if prev is not None:
            e_n = self._compute_e_n(prev, curr)
            self._cur_bucket[key].append(e_n)
        self._last_book[key] = curr
        self._maybe_close_bucket(key, now)

    def on_trade(
        self,
        symbol: str,
        exchange: str,
        side: str,
        qty: float,
    ) -> None:
        key = self._key(symbol, exchange)
        q = float(qty)
        if side.lower() in ("buy", "b"):
            self._cur_buy_vol[key] += q
        else:
            self._cur_sell_vol[key] += q

    def get(self, symbol: str, exchange: str) -> dict:
        key = self._key(symbol, exchange)
        z = float(self._last_z.get(key, 0.0))
        raw_tfi = float(self._last_tfi.get(key, 0.0))

        if z >= settings.SCALP_OFI_Z_ENTRY:
            direction, strength = "LONG", "strong"
        elif z >= 0.8:
            direction, strength = "LONG", "moderate"
        elif z <= -settings.SCALP_OFI_Z_ENTRY:
            direction, strength = "SHORT", "strong"
        elif z <= -0.8:
            direction, strength = "SHORT", "moderate"
        else:
            direction, strength = "NEUTRAL", "weak"

        # TFI confirmation: trade flow doesn't contradict OFI.
        if raw_tfi == 0.0 or z == 0.0:
            tfi_confirms = True
        else:
            tfi_confirms = (raw_tfi > 0) == (z > 0)

        last_close = self._last_bucket_close.get(key)
        if last_close is None:
            age   = 9_999.0
            stale = True
        else:
            age   = time.time() - last_close
            stale = age > 30.0

        return {
            "z":            z,
            "direction":    direction,
            "strength":     strength,
            "tfi_confirms": tfi_confirms,
            "raw_tfi":      raw_tfi,
            "age_sec":      age,
            "stale":        stale,
        }

    def update_direction_ticks(
        self,
        symbol: str,
        exchange: str,
        entry_z: float,
    ) -> int:
        """Counter that increments on each tick where |z| >= |entry_z|;
        resets to 0 on any miss. The gate uses this to require N
        consecutive ticks of conviction before firing."""
        key = self._key(symbol, exchange)
        z = float(self._last_z.get(key, 0.0))
        if abs(z) < abs(entry_z) or z == 0.0:
            self._persist[key] = 0
            return 0
        self._persist[key] += 1
        return self._persist[key]

    # ── Scalp v2 read-only accessors (no effect on OFI accumulation) ────

    def get_z_score(self, symbol: str, exchange: str):
        """Latest bucket z-score for (symbol, exchange), or None if no bucket
        has closed yet. Consumed by the v2 cross-exchange / BTC gates."""
        key = self._key(symbol, exchange)
        if self._last_bucket_close.get(key) is None:
            return None
        return float(self._last_z.get(key, 0.0))

    def get_exchanges_for_symbol(self, symbol: str) -> list:
        """Exchanges this engine has received books for on `symbol` — lets the
        cross-exchange OFI gate discover other venues."""
        prefix = f"{symbol}:"
        return [k.split(":", 1)[1] for k in self._last_book if k.startswith(prefix)]


# ─────────────────────────────────────────────────────────────────────────
# ScalpingAgent
# ─────────────────────────────────────────────────────────────────────────

class ScalpingAgent(BaseAgent):
    agent_id     = "scalp"
    display_name = "Scalping Agent (OFI)"
    optional     = True

    def __init__(
        self,
        sentiment_source: Optional[Any] = None,
        market_data:      Optional[Any] = None,
        regime_detector:  Optional[Any] = None,
    ):
        super().__init__()
        # Fund allocation (the ring-fenced MEXC-scalp pool, shown on the
        # dashboard) is decoupled from the *trading* budget: the latter
        # stays SCALP_CAPITAL so observation mode (SCALP_CAPITAL=0) is
        # preserved regardless of fund size. Flip to live by raising
        # SCALP_CAPITAL once the DB confirms edge.
        self.capital_allocation = float(settings.FUND_MEXC_SCALP_CAPITAL)
        self._capital           = float(settings.SCALP_CAPITAL)

        self._ofi_engine  = OFIEngine(
            levels=settings.SCALP_OFI_LEVELS,
            window_sec=settings.SCALP_OFI_WINDOW_SEC,
            zscore_window=settings.SCALP_ZSCORE_WINDOW,
        )
        self._fee_manager = FeeManager(settings.SCALP_FEE_OVERRIDES)

        # v2 selectivity layer (scalping_v2). Built once; market_data is wired
        # after __init__, so the checkers get a lazy proxy that resolves it per
        # call. Each gate is individually SCALP_USE_*-flagged.
        from agents.scalping_confluence import ConfluenceChecker
        from agents.scalping_atr_sl import ATRStopCalculator
        _lazy_md = _LazyMarketData(self._resolve_market_data)
        self.confluence = ConfluenceChecker(_lazy_md, self._ofi_engine, settings)
        self.atr_calc   = ATRStopCalculator(_lazy_md, settings)

        # Optional injected dependencies. Any of these can be None — the
        # agent then runs in pure-stub mode (or lazy-resolves at first
        # call). Tests construct the agent without args; the coordinator
        # populates them at startup via the set_*() methods below.
        self._sentiment_source = sentiment_source
        self._market_data      = market_data
        self._regime_detector  = regime_detector

        self._positions:     dict[str, ScalpPosition] = {}
        self._observations:  list[ScalpObservation]   = []
        self._pending_flush: list[ScalpObservation]   = []
        # One-shot guard: we subscribe the OFIEngine to market_data's
        # order-book stream the first time market_data resolves (see
        # _ensure_book_subscription). Without that the engine never sees a
        # book and every entry silent-skips on stale OFI.
        self._book_subscribed = False
        # Per-(symbol:exchange) last-seen mid and the time it last *changed* —
        # backs the stale-feed entry guard. A mid that hasn't moved for
        # SCALP_STALE_MID_THRESHOLD_SEC means the book feed has frozen and OFI
        # is being computed on stale data.
        self._last_mid: dict[str, tuple[float, float]] = {}

        # Per-agent circuit breakers (independent of the portfolio breaker).
        # _daily_loss accumulates only losses (drives the daily-loss halt);
        # _daily_pnl is the NET realised daily P&L (wins + losses) used for
        # equity reporting against SCALP_CAPITAL. Both reset at UTC rollover.
        self._daily_loss:      float = 0.0
        self._daily_pnl:       float = 0.0
        self._consec_losses:   int   = 0
        self._halted:          bool  = False
        self._halt_reason:     str   = ""
        self._last_reset_date: date  = datetime.utcnow().date()

        self._stats = {
            "entries_today": 0,
            "exits_today":   0,
            "total_pnl":     0.0,
        }

        self._running                          = False
        self._task: Optional[asyncio.Task]     = None
        self._tracker_task: Optional[asyncio.Task] = None

    # ── BaseAgent contract ──────────────────────────────────────────────

    def is_available(self) -> bool:
        # Always — observation mode runs without keys / market data /
        # fees. Whether it actually trades depends on SCALP_CAPITAL +
        # the wiring TODOs, both checked at evaluation time.
        return True

    # ── BalanceAgent compounding hooks ──────────────────────────────────

    def get_capital_allocation(self) -> float:
        """Effective deployable for sizing: max(allocation, trading
        budget). Observation mode keeps the trading budget at 0 even
        when the fund is funded — the larger of the two is what the
        BalanceAgent's compounding view should track."""
        return float(max(self.capital_allocation, self._capital))

    def set_capital_allocation(self, amount: float) -> bool:
        """Update both the fund-level allocation AND the trading budget
        in lockstep. The trading budget is what observation-mode reads
        to decide between a sim entry and observation-only. Refuses
        below open-position notional."""
        ok = super().set_capital_allocation(amount)
        if not ok:
            return False
        # Mirror the trading budget. We keep observation mode intact:
        # if SCALP_CAPITAL was 0 by configuration, raising allocation
        # via the BalanceAgent should NOT auto-flip observation mode
        # — observation mode is operator-driven. We only mirror when
        # SCALP_CAPITAL is already > 0 (already-live fund).
        if float(settings.SCALP_CAPITAL) > 0:
            try:
                self._capital = float(amount)
            except Exception as e:
                log.debug("ScalpingAgent _capital propagation: %s", e)
        return True

    def get_open_position_notional(self) -> float:
        """USD notional of open scalp positions — sum size_usd."""
        try:
            return float(sum(p.size_usd for p in self._positions.values()))
        except Exception:
            return 0.0

    # ── Late-binding setters (Optional dependency injection) ────────────

    def set_market_data(self, market_data) -> None:
        """Late-bind a MarketData reference — used by the coordinator
        after SignalAgent's bot has constructed it. None-safe."""
        self._market_data = market_data

    def set_regime_detector(self, regime_detector) -> None:
        """Late-bind a regime detector. The module singleton is also
        looked up lazily on first call, so this is mostly for tests."""
        self._regime_detector = regime_detector

    def set_sentiment_source(self, sentiment_source) -> None:
        """Match the other setters so all three injectable deps share
        the same shape. Idempotent."""
        self._sentiment_source = sentiment_source

    # ── Lazy resolvers ──────────────────────────────────────────────────

    def _resolve_market_data(self):
        """Return cached / injected market_data, or fish it out of the
        signal agent's bot if REGISTERED_AGENTS is reachable. None when
        nothing is wired — caller falls back to stub behaviour."""
        if self._market_data is not None:
            self._ensure_book_subscription(self._market_data)
            return self._market_data
        try:
            from agents import REGISTERED_AGENTS
            for ag in REGISTERED_AGENTS:
                if getattr(ag, "agent_id", "") != "signal":
                    continue
                bot = getattr(ag, "bot", None) or getattr(ag, "_bot", None)
                if bot is None:
                    continue
                md = getattr(bot, "_market_data", None)
                if md is not None:
                    self._market_data = md     # cache to skip the walk next time
                    self._ensure_book_subscription(md)
                    return md
        except Exception as e:
            log.debug("[ScalpingAgent] market_data lookup failed: %s", e)
        return None

    def _ensure_book_subscription(self, md) -> None:
        """Subscribe our OFIEngine to market_data's order-book stream, once.

        market_data fans every book tick out to on_book_update callbacks as
        (exchange, pair, bids, asks); OFIEngine.on_book wants
        (symbol, exchange, bids, asks), so we adapt the argument order. This
        is the missing link that left the engine empty (root cause of zero
        scalp_observations): the stream fed ofi_scorer but nothing fed us.
        None-safe — a market_data without on_book_update (older / test stub)
        is silently skipped and the agent falls back to stale-OFI behaviour."""
        if self._book_subscribed:
            return
        register = getattr(md, "on_book_update", None)
        if not callable(register):
            return
        register(lambda exchange, pair, bids, asks:
                 self.on_book(pair, exchange, bids, asks))
        self._book_subscribed = True
        log.info("[ScalpingAgent] subscribed OFIEngine to order-book stream")

    def _resolve_regime_detector(self):
        """Return cached / injected detector, else the module singleton."""
        if self._regime_detector is not None:
            return self._regime_detector
        try:
            from core.regime_detector import regime_detector as _rd
            self._regime_detector = _rd
            return _rd
        except Exception as e:
            log.debug("[ScalpingAgent] regime_detector import failed: %s", e)
            return None

    def _reconstruct_pnl(self) -> None:
        """Seed in-memory P&L counters from closed scalp observations so a
        restart resumes from the fund's accumulated figure rather than zero.
        pnl_usd is gross, which equals net at MEXC's 0% fees."""
        try:
            self._stats["total_pnl"] = db_queries.get_scalp_realized_pnl()
            self._daily_pnl = db_queries.get_scalp_realized_pnl(today=True)
        except Exception as e:
            log.debug("[ScalpingAgent] P&L reconstruction skipped: %s", e)

    async def start(self) -> None:
        self._running = True
        self._status = RUNNING
        self._start_time = time.time()
        self._reconstruct_pnl()
        log.info(
            "[ScalpingAgent] starting in %s mode (capital=$%.0f)",
            "OBSERVATION" if self._capital == 0 else "LIVE",
            self._capital,
        )
        self._task = asyncio.create_task(self._loop())
        self._tracker_task = asyncio.create_task(self._micro_price_tracker_loop())

    async def stop(self) -> None:
        self._running = False
        for t in (self._task, self._tracker_task):
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except Exception:
                    pass
        self._status = STOPPED
        log.info("[ScalpingAgent] stopped")

    async def close_all_positions(self) -> None:
        for pos_key in list(self._positions.keys()):
            try:
                await self._exit_position(
                    pos_key, self._positions[pos_key], reason="FORCE_EXIT",
                )
            except Exception as e:
                log.error("[ScalpingAgent] force exit %s: %s", pos_key, e)

    def _win_rate_today(self) -> float:
        """Win rate (0.0–1.0) for scalp trades closed today, computed directly
        from scalp_observations (would_entry=1, exit_price>0; pnl_bps>0 = win).
        Source of the agent-card + agent-page win rate — fixes the 0.0%-despite-
        closed-trades bug. Never raises (get_stats must not)."""
        try:
            rows = db_queries.get_scalp_closed_today()
        except Exception:
            return 0.0
        if not rows:
            return 0.0
        wins = sum(1 for r in rows if (r.get("pnl_bps") or 0) > 0)
        return wins / len(rows)

    def _win_rate_alltime(self) -> float:
        """All-time scalp win rate over closed observations. Never raises."""
        try:
            return float(db_queries.get_scalp_activation_stats().get("win_rate", 0.0))
        except Exception:
            return 0.0

    async def get_stats(self) -> AgentStats:
        capital_deployed = sum(p.size_usd for p in self._positions.values())
        # Equity tracks net daily P&L against the fund (SCALP_CAPITAL): the
        # coordinator reads fund equity as capital_allocated + daily_pnl.
        daily_pnl_pct = (self._daily_pnl / self.capital_allocation * 100.0) \
            if self.capital_allocation else 0.0
        return AgentStats(
            agent_id=self.agent_id,
            status=HALTED if self._halted else (RUNNING if self._running else OFFLINE),
            capital_allocated=self.capital_allocation,
            capital_deployed=capital_deployed,
            daily_pnl=float(self._daily_pnl),
            daily_pnl_pct=daily_pnl_pct,
            total_pnl=float(self._stats["total_pnl"]),
            trades_today=int(self._stats["entries_today"]),
            win_rate_today=self._win_rate_today(),
            win_rate_alltime=self._win_rate_alltime(),
            consecutive_losses=int(self._consec_losses),
            last_trade_time=None,
            error=None,
        )

    # ── WebSocket hooks (Phase-2 wiring) ────────────────────────────────

    def on_book(
        self,
        symbol: str,
        exchange: str,
        bids: list,
        asks: list,
    ) -> None:
        # TODO Phase 2: call from market_data WebSocket handler.
        self._ofi_engine.on_book(symbol, exchange, bids, asks)

    def on_trade(
        self,
        symbol: str,
        exchange: str,
        side: str,
        qty: float,
    ) -> None:
        # TODO Phase 2: call from market_data WebSocket handler.
        self._ofi_engine.on_trade(symbol, exchange, side, qty)

    # ── Market-data stubs — wire in Phase 2 ─────────────────────────────

    async def _get_mid_price(self, symbol: str, exchange: str) -> float:
        """Last-known mid price for (symbol, exchange).

        Wired: MarketData.get_price (per-exchange) → get_all_prices
        (any exchange) fallback. Stub when MarketData is missing or
        before any tick has arrived → 0.0 (entry gate skips silently).
        """
        md = self._resolve_market_data()
        if md is None:
            return 0.0
        try:
            p = md.get_price(exchange, symbol)
            if p is not None and float(p) > 0:
                return float(p)
            prices = md.get_all_prices(symbol) or {}
            for v in prices.values():
                if v is not None and float(v) > 0:
                    return float(v)
        except Exception as e:
            log.debug("[ScalpingAgent] _get_mid_price failed: %s", e)
        return 0.0

    async def _get_spread_bps(self, symbol: str, exchange: str) -> float:
        """Top-of-book spread in bps from the cached order book.

        Wired: MarketData.get_spread_bps. Stub when MarketData is
        missing OR when no book has been received yet → 0.0 (gate 9
        treats this as "spread fine"; the price gate downstream will
        skip if the mid is also missing, so we don't fire a bogus
        entry on cold start).
        """
        md = self._resolve_market_data()
        if md is None or not hasattr(md, "get_spread_bps"):
            return 0.0
        try:
            spread = md.get_spread_bps(exchange, symbol)
        except Exception as e:
            log.debug("[ScalpingAgent] _get_spread_bps failed: %s", e)
            return 0.0
        return float(spread) if spread is not None else 0.0

    async def _get_regime(self, symbol: str) -> str:
        """Primary-timeframe regime label, uppercased to match the
        gate's CHOPPY / TRENDING / RANGING string compare.

        Wired: regime_detector.get_primary(symbol).regime. Stub returns
        "RANGING" (a permissive default — the gate only blocks on
        "CHOPPY", so RANGING falls through cleanly).
        """
        rd = self._resolve_regime_detector()
        if rd is None:
            return "RANGING"
        try:
            snap = rd.get_primary(symbol)
        except Exception as e:
            log.debug("[ScalpingAgent] _get_regime failed: %s", e)
            return "RANGING"
        if snap is None or not getattr(snap, "regime", None):
            return "RANGING"
        # regime_detector stores lowercase ("choppy" etc); our gate
        # compares against uppercase strings.
        return str(snap.regime).upper()

    async def _get_ccxt_exchange(self, exchange_id: str, symbol: Optional[str] = None):
        """Return a live ccxt client for `exchange_id`, or None.

        MEXC supports per-key pair allowlists — one account, many keys,
        each restricted to a subset of pairs. When exchange_id == "mexc"
        the symbol parameter chooses the right key via
        execution.mexc_key_router. With no symbol (e.g. fee pre-warm)
        the router returns any constructable MEXC client.

        Every other exchange falls through to MarketData's connection
        pool — that's where bitget / binance / kraken clients live.
        """
        if exchange_id == "mexc":
            try:
                from execution.mexc_key_router import mexc_key_router
            except Exception as e:
                log.debug("[ScalpingAgent] mexc_key_router import failed: %s", e)
                return None
            client = (
                mexc_key_router.get_client_for(symbol) if symbol
                else mexc_key_router.any_client()
            )
            if client is not None:
                return client
            # No symbol match (or symbol omitted with no usable key) —
            # fall through to MarketData in case an installer wired a
            # single shared MEXC client there.
        md = self._resolve_market_data()
        if md is None:
            return None
        try:
            return getattr(md, "_exchanges", {}).get(exchange_id)
        except Exception as e:
            log.debug("[ScalpingAgent] _get_ccxt_exchange failed: %s", e)
            return None

    async def _get_btc_1m_change(self) -> float:
        """BTC 1-minute % change for the correlation guard (gate 13).

        Wired: MarketData.get_change_pct(exchange, "BTC/USDT", 60). The
        orderbook stream feeds (ts, mid) samples into a per-pair ring
        buffer, so this query is O(buffer size) and gets sub-minute
        resolution the 5m candles can't.

        Tries every enabled exchange in turn — the first one with
        sufficient history wins. Returns 0.0 when no exchange has
        accumulated 60s of samples yet (cold start), which leaves the
        correlation guard permissive on boot rather than spuriously
        blocking entries.
        """
        md = self._resolve_market_data()
        if md is None or not hasattr(md, "get_change_pct"):
            return 0.0
        for ex in settings.ENABLED_EXCHANGES:
            try:
                change = md.get_change_pct(ex, "BTC/USDT", 60)
            except Exception as e:
                log.debug("[ScalpingAgent] _get_btc_1m_change %s: %s", ex, e)
                continue
            if change is not None:
                return float(change)
        return 0.0

    async def _place_order(self, pos: "ScalpPosition") -> Optional[int]:
        """Execute a scalp entry.

        SIM_MODE: persist a sim Trade row — mirrors the signal agent's
        OrderRouter._sim_execute (sim_mode=True) so scalp fills appear in the
        trades table and the shared P&L queries alongside signal/arb. Returns
        the Trade id, stored on the position so _exit_position can close the
        same row. Never raises — a DB hiccup must not stop trading.

        Live (SIM_MODE=False): still a stub → execution/router.py follow-up.
        """
        if not settings.SIM_MODE:
            log.info(
                "[ScalpingAgent] _place_order LIVE stub %s:%s %s $%.2f @ %.4f",
                pos.symbol, pos.exchange, pos.direction, pos.size_usd, pos.entry_price,
            )
            return None

        trade_data = {
            "signal_id":      None,   # scalp has no Signal row; FK is nullable
            "pair":           pos.symbol,
            "exchange":       pos.exchange,
            "side":           "long" if pos.direction == "LONG" else "short",
            "signal_type":    "scalp",
            "entry_price":    pos.entry_price,
            "size_usd":       pos.size_usd,
            "size_base":      pos.size_usd / pos.entry_price if pos.entry_price > 0 else 0.0,
            "stop_loss":      pos.sl_price,
            "take_profit":    pos.tp_price,
            "sim_mode":       True,
            "profile":        settings.ACTIVE_PROFILE,
            "strategy":       "scalp",
            "timestamp_open": datetime.utcnow(),
        }
        try:
            return await asyncio.to_thread(db_queries.save_trade, trade_data)
        except Exception as e:
            log.warning("[ScalpingAgent] sim _place_order save_trade failed: %s", e)
            return None

    # ── Main loop ───────────────────────────────────────────────────────

    async def _loop(self) -> None:
        # Pre-warm the fee cache for every approved scalp exchange.
        for exchange_id in settings.STRATEGY_EXCHANGE_MAP.get("scalp", []):
            try:
                ccxt_ex = await self._get_ccxt_exchange(exchange_id)
                await self._fee_manager.load_exchange(exchange_id, ccxt_ex)
            except Exception as e:
                log.warning("[ScalpingAgent] fee load %s: %s", exchange_id, e)

        interval = max(0.01, settings.SCALP_SCAN_INTERVAL_MS / 1000.0)
        while self._running:
            try:
                await asyncio.sleep(interval)
                # Make sure the OFIEngine is subscribed to the book stream.
                # The entry gates silent-skip on stale OFI *before* reaching
                # the market_data accessors, so we can't rely on those to
                # trigger the lazy resolve — drive it here every tick until it
                # sticks (idempotent: cached + a one-shot subscribe flag). This
                # is what breaks the chicken-and-egg (no books -> stale OFI ->
                # gate-2 skip -> never resolve -> never subscribe -> no books).
                self._resolve_market_data()
                if self._halted:
                    continue
                approved = settings.STRATEGY_EXCHANGE_MAP.get("scalp", [])
                for symbol in settings.SCALP_PAIRS:
                    for exchange in approved:
                        pos_key = self._pos_key(symbol, exchange)
                        if pos_key in self._positions:
                            await self._manage_position(pos_key)
                        else:
                            await self._evaluate_entry(symbol, exchange)
                if self._pending_flush:
                    await self._flush_observations()
                self._check_daily_reset()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("[ScalpingAgent] Loop error: %s", e)

    @staticmethod
    def _pos_key(symbol: str, exchange: str) -> str:
        return f"{symbol}:{exchange}"

    # ── Micro price tracker ────────────────────────────────────────────

    async def _micro_price_tracker_loop(self) -> None:
        """Backfill price_30s/1m/3m/5m on every real entry as its age
        crosses each threshold.

        Scalp analogue of the main bot's future_price_tracker — lets us
        ask, retrospectively, "did the OFI signal predict correctly even
        when the position was exited early by OFI_EXHAUSTED?" without
        depending on whether the exit reason happened to be TP/SL.
        Runs every SCALP_TRACKER_INTERVAL_SEC.
        """
        while self._running:
            try:
                await asyncio.sleep(settings.SCALP_TRACKER_INTERVAL_SEC)
                await self._micro_price_tracker_pass()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("[ScalpingAgent] Micro tracker error: %s", e)

    async def _micro_price_tracker_pass(self) -> None:
        """One sweep of the observation list — extracted so tests can
        drive a single iteration without sleeping."""
        now = time.time()
        updated: list[ScalpObservation] = []
        for obs in self._observations:
            if obs.entry_price == 0.0:
                continue   # not a real entry — nothing to track
            age = now - obs.timestamp
            changed = False
            if age >= 30 and obs.price_30s == 0.0:
                p = await self._get_mid_price(obs.symbol, obs.exchange)
                if p > 0.0:
                    obs.price_30s = p
                    changed = True
            if age >= 60 and obs.price_1m == 0.0:
                p = await self._get_mid_price(obs.symbol, obs.exchange)
                if p > 0.0:
                    obs.price_1m = p
                    changed = True
            if age >= 180 and obs.price_3m == 0.0:
                p = await self._get_mid_price(obs.symbol, obs.exchange)
                if p > 0.0:
                    obs.price_3m = p
                    changed = True
            if age >= 300 and obs.price_5m == 0.0:
                p = await self._get_mid_price(obs.symbol, obs.exchange)
                if p > 0.0:
                    obs.price_5m = p
                    changed = True
            if changed:
                updated.append(obs)
        if updated:
            self._pending_flush.extend(updated)

    # ── Entry gate ──────────────────────────────────────────────────────

    async def _evaluate_entry(self, symbol: str, exchange: str) -> None:
        approved = settings.STRATEGY_EXCHANGE_MAP.get("scalp", [])
        now = time.time()

        # 1. exchange approved
        if exchange not in approved:
            self._log_skip(symbol, exchange, now, ofi=None,
                           reason=f"Exchange {exchange} not in scalp approved list",
                           rt_bps=0.0, min_wr=1.0,
                           spread_bps=0.0, regime="?")
            return

        ofi = self._ofi_engine.get(symbol, exchange)

        # 1b. Stale-feed guard. A mid that hasn't moved for >= the threshold
        # means the order-book feed has frozen, so the OFI z-score is computed
        # on stale data — skip and LOG it (unlike the silent OFI-stale skip
        # below). A symbol seen for the first time records its baseline and is
        # given one cycle to populate (no skip). On a stale hit we re-arm the
        # change-time so a persistently frozen feed logs once per threshold
        # window rather than on every tick.
        mid_now = await self._get_mid_price(symbol, exchange)
        if mid_now and mid_now > 0:
            mid_key = self._pos_key(symbol, exchange)
            prev = self._last_mid.get(mid_key)
            if prev is None:
                self._last_mid[mid_key] = (mid_now, now)       # first scan — one cycle
            elif mid_now != prev[0]:
                self._last_mid[mid_key] = (mid_now, now)       # mid moved — feed is live
            elif (now - prev[1]) >= settings.SCALP_STALE_MID_THRESHOLD_SEC:
                self._last_mid[mid_key] = (mid_now, now)       # re-arm (debounce repeat logs)
                self._log_skip(symbol, exchange, now, ofi=ofi,
                               reason="stale_feed",
                               rt_bps=0.0, min_wr=1.0,
                               spread_bps=0.0, regime="?")
                return

        # 2. OFI not stale and active — silent skip (no observation logged)
        if ofi["stale"] or ofi["z"] == 0.0:
            return
        # 3. OFI direction actionable — silent skip
        if ofi["direction"] not in ("LONG", "SHORT"):
            return

        rt_bps = self._fee_manager.round_trip_bps(exchange, symbol)

        # 4. Fee viability
        viable, reason = self._fee_manager.is_viable(exchange, symbol)
        if not viable:
            self._log_skip(symbol, exchange, now, ofi=ofi,
                           reason=reason, rt_bps=rt_bps, min_wr=1.0,
                           spread_bps=0.0, regime="?")
            return

        # 5. Z-score above entry threshold
        if abs(ofi["z"]) < settings.SCALP_OFI_Z_ENTRY:
            self._log_skip(symbol, exchange, now, ofi=ofi,
                           reason=(f"OFI too weak z={ofi['z']:.2f} "
                                   f"(need {settings.SCALP_OFI_Z_ENTRY})"),
                           rt_bps=rt_bps, min_wr=1.0,
                           spread_bps=0.0, regime="?")
            return

        # 6. Directional persistence
        persist = self._ofi_engine.update_direction_ticks(
            symbol, exchange, settings.SCALP_OFI_Z_ENTRY,
        )
        if persist < settings.SCALP_OFI_PERSIST_TICKS:
            self._log_skip(symbol, exchange, now, ofi=ofi,
                           reason=(f"OFI persistence {persist}/"
                                   f"{settings.SCALP_OFI_PERSIST_TICKS} ticks"),
                           rt_bps=rt_bps, min_wr=1.0,
                           spread_bps=0.0, regime="?")
            return

        # 7. TFI confirmation
        if not ofi["tfi_confirms"]:
            self._log_skip(symbol, exchange, now, ofi=ofi,
                           reason=(f"TFI diverges (possible spoof) "
                                   f"tfi={ofi['raw_tfi']:.4f}"),
                           rt_bps=rt_bps, min_wr=1.0,
                           spread_bps=0.0, regime="?")
            return

        # 8. Concurrent position limit
        if len(self._positions) >= settings.SCALP_MAX_CONCURRENT:
            self._log_skip(symbol, exchange, now, ofi=ofi,
                           reason=(f"Max concurrent positions "
                                   f"({settings.SCALP_MAX_CONCURRENT})"),
                           rt_bps=rt_bps, min_wr=1.0,
                           spread_bps=0.0, regime="?")
            return

        # 9. Spread check
        spread_bps = await self._get_spread_bps(symbol, exchange)
        if spread_bps > settings.SCALP_MAX_SPREAD_BPS:
            self._log_skip(symbol, exchange, now, ofi=ofi,
                           reason=(f"Spread {spread_bps:.1f}bps > "
                                   f"{settings.SCALP_MAX_SPREAD_BPS} on {exchange}"),
                           rt_bps=rt_bps, min_wr=1.0,
                           spread_bps=spread_bps, regime="?")
            return

        # 10. Regime check
        regime = await self._get_regime(symbol)
        if regime == "CHOPPY":
            self._log_skip(symbol, exchange, now, ofi=ofi,
                           reason="Regime CHOPPY — no scalp edge",
                           rt_bps=rt_bps, min_wr=1.0,
                           spread_bps=spread_bps, regime=regime)
            return

        # 11. Session timing — scalp edge requires tight spreads + active
        # order flow, both of which only hold during London/NY overlap.
        hour_utc = datetime.utcnow().hour
        if not (settings.SCALP_SESSION_START_UTC
                <= hour_utc < settings.SCALP_SESSION_END_UTC):
            self._log_skip(symbol, exchange, now, ofi=ofi,
                           reason=f"Outside scalp session ({hour_utc:02d}:xx UTC)",
                           rt_bps=rt_bps, min_wr=1.0,
                           spread_bps=spread_bps, regime=regime)
            return

        # 12. News guard — read the sentiment aggregator's news_guard flag
        # if a source was injected. Offline source never blocks the gate
        # (try/except + None-check).
        if settings.SCALP_RESPECT_NEWS_GUARD and self._sentiment_source is not None:
            try:
                composite = await self._sentiment_source.get_composite()
                if composite and composite.get("news_guard_active", False):
                    self._log_skip(symbol, exchange, now, ofi=ofi,
                                   reason="News guard active — scalp edge unreliable",
                                   rt_bps=rt_bps, min_wr=1.0,
                                   spread_bps=spread_bps, regime=regime)
                    return
            except Exception as e:
                log.debug("[ScalpingAgent] news guard check failed: %s", e)

        # 13. BTC correlation guard — alt order books gap when BTC moves
        # sharply; the spread gate would catch it eventually but this is
        # a cheaper check against an upstream data source.
        if symbol != "BTC/USDT":
            try:
                btc_change = await self._get_btc_1m_change()
            except Exception:
                btc_change = 0.0
            if abs(btc_change) > settings.SCALP_BTC_GUARD_PCT:
                self._log_skip(symbol, exchange, now, ofi=ofi,
                               reason=(f"BTC moving {btc_change:.2f}% in 1m "
                                       "— alt scalp risky"),
                               rt_bps=rt_bps, min_wr=1.0,
                               spread_bps=spread_bps, regime=regime)
                return

        # ── Passed gates 1–13 ─────────────────────────────────────────
        entry_price = await self._get_mid_price(symbol, exchange)
        if entry_price == 0.0:
            return     # market_data unwired — silent skip, no observation
        direction = ofi["direction"]

        # ── V2 selectivity layer (runs after gate 13) ─────────────────
        v2_fields: dict = {}
        if settings.SCALP_USE_CONFLUENCE:
            conf = self.confluence.run_all_gates(
                symbol=symbol, exchange=exchange, direction=direction,
                primary_z=ofi["z"],
                position_size_usd=settings.SCALP_POSITION_SIZE_USD,
            )
            v2_fields = self._unpack_confluence(conf)
            if not conf.passed:
                # Log a skipped observation. entry_price carries the mid at
                # evaluation time (so the recalibration skip-analysis can ask
                # "would it have won?"), and the v2 diagnostics are recorded
                # even on a skip.
                obs = self._make_observation(
                    symbol, exchange, now, ofi=ofi,
                    would_entry=False, skip_reason=f"V2:{conf.blocking_reason}",
                    entry_price=entry_price,
                    tp_bps=0.0, sl_bps=0.0, rt_bps=rt_bps, min_wr=1.0,
                    spread_bps=spread_bps, regime=regime,
                )
                self._annotate_v2(obs, v2_fields)
                self._observations.append(obs)
                self._pending_flush.append(obs)
                return

        # ── ATR-aware TP/SL (replaces fee_manager.compute_tp_sl on pass) ─
        tpsl = self.atr_calc.compute_tp_sl_v2(
            symbol=symbol, exchange=exchange,
            round_trip_bps=self._fee_manager.round_trip_bps(exchange, symbol),
        )
        tp_bps, sl_bps = tpsl.tp_bps, tpsl.sl_bps
        min_wr = self._fee_manager.breakeven_win_rate(exchange, symbol,
                                                     tp_bps, sl_bps)
        if direction == "LONG":
            tp_price = entry_price * (1 + tp_bps / 10000.0)
            sl_price = entry_price * (1 - sl_bps / 10000.0)
        else:
            tp_price = entry_price * (1 - tp_bps / 10000.0)
            sl_price = entry_price * (1 + sl_bps / 10000.0)

        obs = self._make_observation(
            symbol, exchange, now, ofi=ofi,
            would_entry=True, skip_reason="",
            entry_price=entry_price,
            tp_bps=tp_bps, sl_bps=sl_bps,
            rt_bps=rt_bps, min_wr=min_wr,
            spread_bps=spread_bps, regime=regime,
        )
        self._annotate_v2(obs, v2_fields, tpsl=tpsl)
        self._observations.append(obs)
        self._pending_flush.append(obs)

        observation_only = self._capital <= 0
        # Position size scales with the BalanceAgent-set allocation so
        # realised profit compounds: base = SCALP_POSITION_SIZE_USD ×
        # (allocation / FUND_MEXC_SCALP_CAPITAL). 1.0× on cold start.
        starting_pool = float(getattr(settings, "FUND_MEXC_SCALP_CAPITAL", 0.0) or 0.0)
        factor = 1.0
        if starting_pool > 0:
            factor = min(10.0, max(0.0, self.capital_allocation / starting_pool))
        pos = ScalpPosition(
            symbol=symbol, exchange=exchange, direction=direction,
            entry_price=entry_price, entry_time=now,
            entry_ofi_z=ofi["z"], entry_tfi=ofi["raw_tfi"],
            size_usd=(settings.SCALP_POSITION_SIZE_USD * factor
                      if not observation_only else 0.0),
            tp_price=tp_price, sl_price=sl_price,
            tp_bps=tp_bps, sl_bps=sl_bps,
            round_trip_cost_bps=rt_bps,
            observation_only=observation_only,
        )
        self._positions[self._pos_key(symbol, exchange)] = pos
        self._stats["entries_today"] += 1

        if observation_only:
            log.debug(
                "[ScalpingAgent] %s:%s %s @ %.4f [observation only]",
                symbol, exchange, direction, entry_price,
            )
        else:
            pos.trade_id = await self._place_order(pos)
            log.info(
                "[ScalpingAgent] ENTRY (SIM) %s:%s %s @ %.4f tp=%.4f sl=%.4f size=$%.2f",
                symbol, exchange, direction, entry_price, tp_price, sl_price, pos.size_usd,
            )

    @staticmethod
    def _unpack_confluence(conf) -> dict:
        """Flatten a CombinedConfluenceResult into the obs v2 diagnostic
        fields (recorded whether the signal passed or was blocked)."""
        fields = {
            "confluence_score":      conf.confluence_score,
            "strength_label":        conf.strength_label,
            "cross_exchange_agrees": conf.cross_exchange_agrees,
            "btc_compatible":        conf.btc_compatible,
            "adverse_selection_ok":  conf.adverse_selection_ok,
            "depth_ok":              conf.depth_ok,
        }
        for r in conf.individual_results:
            if r.gate_name == "VWAP":
                fields["vwap_aligned"] = r.passed and r.score >= 0.99
            elif r.gate_name == "HTF":
                fields["htf_aligned"] = r.passed and r.score >= 0.99
            elif r.gate_name == "VOLUME":
                fields["volume_adequate"] = r.passed and r.score >= 0.99
        return fields

    @staticmethod
    def _annotate_v2(obs, fields: dict, tpsl=None) -> None:
        """Write the v2 confluence fields (and, on entry, the ATR TP/SL
        diagnostics) onto a ScalpObservation."""
        for k, v in fields.items():
            setattr(obs, k, v)
        if tpsl is not None:
            obs.atr_bps      = tpsl.atr_bps
            obs.atr_adjusted = tpsl.atr_adjusted
            obs.sl_clamped   = tpsl.sl_clamped
            obs.rr_actual    = tpsl.rr_actual

    def _log_skip(
        self,
        symbol: str,
        exchange: str,
        ts: float,
        ofi: Optional[dict],
        reason: str,
        rt_bps: float,
        min_wr: float,
        spread_bps: float,
        regime: str,
    ) -> None:
        obs = self._make_observation(
            symbol, exchange, ts, ofi=ofi,
            would_entry=False, skip_reason=reason,
            entry_price=0.0,
            tp_bps=0.0, sl_bps=0.0,
            rt_bps=rt_bps, min_wr=min_wr,
            spread_bps=spread_bps, regime=regime,
        )
        self._observations.append(obs)
        self._pending_flush.append(obs)

    def _make_observation(
        self,
        symbol: str,
        exchange: str,
        ts: float,
        ofi: Optional[dict],
        would_entry: bool,
        skip_reason: str,
        entry_price: float,
        tp_bps: float,
        sl_bps: float,
        rt_bps: float,
        min_wr: float,
        spread_bps: float,
        regime: str,
    ) -> ScalpObservation:
        if ofi is None:
            return ScalpObservation(
                symbol=symbol, exchange=exchange, timestamp=ts,
                ofi_z=0.0, direction="NEUTRAL", strength="weak",
                tfi_confirms=True, raw_tfi=0.0,
                spread_bps=spread_bps, regime=regime,
                round_trip_cost_bps=rt_bps,
                min_win_rate_required=min_wr,
                tp_bps=tp_bps, sl_bps=sl_bps,
                would_entry=would_entry, skip_reason=skip_reason,
                entry_price=entry_price,
                observation_only=self._capital <= 0,
            )
        return ScalpObservation(
            symbol=symbol, exchange=exchange, timestamp=ts,
            ofi_z=ofi["z"], direction=ofi["direction"],
            strength=ofi["strength"], tfi_confirms=ofi["tfi_confirms"],
            raw_tfi=ofi["raw_tfi"],
            spread_bps=spread_bps, regime=regime,
            round_trip_cost_bps=rt_bps,
            min_win_rate_required=min_wr,
            tp_bps=tp_bps, sl_bps=sl_bps,
            would_entry=would_entry, skip_reason=skip_reason,
            entry_price=entry_price,
            observation_only=self._capital <= 0,
        )

    # ── Exit logic ──────────────────────────────────────────────────────

    async def _manage_position(self, pos_key: str) -> None:
        pos = self._positions.get(pos_key)
        if pos is None:
            return
        symbol, exchange = pos_key.rsplit(":", 1)
        current_price = await self._get_mid_price(symbol, exchange)
        if current_price == 0.0:
            return
        ofi = self._ofi_engine.get(symbol, exchange)
        now = time.time()

        # 1. TP
        if (pos.direction == "LONG"  and current_price >= pos.tp_price) or \
           (pos.direction == "SHORT" and current_price <= pos.tp_price):
            await self._exit_position(pos_key, pos, "TP", current_price)
            return
        # 2. SL
        if (pos.direction == "LONG"  and current_price <= pos.sl_price) or \
           (pos.direction == "SHORT" and current_price >= pos.sl_price):
            await self._exit_position(pos_key, pos, "SL", current_price)
            return
        # 3. OFI exhausted
        if pos.direction == "LONG"  and ofi["z"] < settings.SCALP_OFI_Z_EXIT:
            await self._exit_position(pos_key, pos, "OFI_EXHAUSTED", current_price)
            return
        if pos.direction == "SHORT" and ofi["z"] > -settings.SCALP_OFI_Z_EXIT:
            await self._exit_position(pos_key, pos, "OFI_EXHAUSTED", current_price)
            return
        # 4. OFI flipped
        if pos.direction == "LONG"  and ofi["z"] < settings.SCALP_OFI_Z_CONTRADICT:
            await self._exit_position(pos_key, pos, "OFI_FLIP", current_price)
            return
        if pos.direction == "SHORT" and ofi["z"] > -settings.SCALP_OFI_Z_CONTRADICT:
            await self._exit_position(pos_key, pos, "OFI_FLIP", current_price)
            return
        # 5. Max hold
        if (now - pos.entry_time) > settings.SCALP_MAX_HOLD_SEC:
            await self._exit_position(pos_key, pos, "MAX_HOLD", current_price)
            return

    async def _exit_position(
        self,
        pos_key: str,
        pos: ScalpPosition,
        reason: str,
        exit_price: float = 0.0,
    ) -> None:
        symbol, exchange = pos_key.rsplit(":", 1)
        if exit_price == 0.0:
            exit_price = await self._get_mid_price(symbol, exchange)
            if exit_price == 0.0:
                exit_price = pos.entry_price
        now = time.time()
        hold_sec = now - pos.entry_time
        if pos.direction == "LONG":
            pnl_bps = (exit_price - pos.entry_price) / pos.entry_price * 10000.0
        else:
            pnl_bps = (pos.entry_price - exit_price) / pos.entry_price * 10000.0
        pnl_usd = (pnl_bps / 10000.0) * pos.size_usd if pos.size_usd > 0 else 0.0
        pnl_net_bps = pnl_bps - pos.round_trip_cost_bps

        # Fill the corresponding open observation in place so the DB row
        # carries entry + exit on the natural key (sym + ex + ts).
        for obs in reversed(self._observations):
            if (obs.symbol == symbol and obs.exchange == exchange
                    and obs.would_entry and obs.exit_price == 0
                    and obs.entry_price == pos.entry_price):
                obs.exit_price  = exit_price
                obs.exit_time   = now
                obs.exit_reason = reason
                obs.hold_sec    = hold_sec
                obs.pnl_bps     = pnl_bps      # gross — net computed in queries
                obs.pnl_usd     = pnl_usd
                self._pending_flush.append(obs)
                break

        # Circuit breakers — only count real trades.
        if pos.size_usd > 0:
            self._stats["total_pnl"] += pnl_usd
            self._daily_pnl  += pnl_usd            # net realised P&L → equity
            self._daily_loss += min(pnl_usd, 0.0)  # losses only → halt trigger
            if pnl_usd < 0:
                self._consec_losses += 1
            else:
                self._consec_losses = 0
            # %-based daily-loss halt — scales with this agent's
            # allocation so a compounding fund doesn't tighten its leash.
            # get_capital_allocation() reads the live deployable pool
            # (max of fund allocation and trading budget). 0 → no-op:
            # observation mode has no allocated capital to halt against.
            alloc = float(self.get_capital_allocation() or 0.0)
            if alloc > 0:
                halt_usd = (float(settings.SCALP_DAILY_LOSS_HALT_PCT) / 100.0) * alloc
                if abs(self._daily_loss) >= halt_usd:
                    self._halt(
                        f"Daily loss halt: ${abs(self._daily_loss):.2f} >= "
                        f"${halt_usd:.2f} "
                        f"({settings.SCALP_DAILY_LOSS_HALT_PCT:.1f}% of ${alloc:.0f})"
                    )
            if self._consec_losses >= settings.SCALP_CONSEC_LOSS_PAUSE:
                self._halt(f"Consecutive losses: {self._consec_losses}")

        # Close the sim Trade row for a real (non-observation) fill so the
        # trades table carries the exit + P&L. Fund equity (SCALP_CAPITAL +
        # net daily P&L) is surfaced via get_stats.daily_pnl. Never raises.
        # Trade.pnl_pct convention (see core.bot.CircuitBreakerState
        # docstring): FRACTION, not percent — e.g. -0.012 = -1.2%. The
        # position_manager CB check (execution/position_manager.py:68) and
        # database/queries.get_today_pnl_pct both sum these as fractions.
        # Smoke test 2026-05-29 caught a phantom HALT because this line
        # used to multiply by 100, so a -2.43 bps scalp loss read as a
        # -2.43% portfolio daily PnL.
        if not pos.observation_only and pos.trade_id is not None:
            pnl_pct = (pnl_usd / pos.size_usd) if pos.size_usd > 0 else 0.0
            try:
                await asyncio.to_thread(
                    db_queries.close_trade,
                    pos.trade_id, exit_price, reason, pnl_usd, pnl_pct,
                )
            except Exception as e:
                log.warning("[ScalpingAgent] sim close_trade failed: %s", e)

        self._stats["exits_today"] += 1
        if pos_key in self._positions:
            del self._positions[pos_key]
        log.info(
            "[ScalpingAgent] EXIT %s:%s @ %.4f %s %.2fbps "
            "(net %.2fbps after %.1fbps fees)",
            symbol, exchange, exit_price, reason, pnl_bps,
            pnl_net_bps, pos.round_trip_cost_bps,
        )

    def _halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason
        log.warning("[ScalpingAgent] HALTED — %s", reason)

    def _check_daily_reset(self) -> None:
        today = datetime.utcnow().date()
        if today > self._last_reset_date:
            self._daily_loss = 0.0
            self._daily_pnl = 0.0
            self._consec_losses = 0
            self._stats["entries_today"] = 0
            self._stats["exits_today"]   = 0
            if self._halted and "daily loss" in self._halt_reason.lower():
                self._halted = False
                self._halt_reason = ""
                log.info("[ScalpingAgent] Daily reset — circuit breakers cleared")
            self._last_reset_date = today

    async def _flush_observations(self) -> None:
        if not self._pending_flush:
            return
        batch = list(self._pending_flush)
        self._pending_flush.clear()
        try:
            await asyncio.to_thread(db_queries.save_scalp_observations, batch)
        except Exception as e:
            log.warning("[ScalpingAgent] flush_observations failed: %s", e)

    # ── Analysis helper ────────────────────────────────────────────────

    def get_observation_summary(self) -> dict:
        """Snapshot of observation-mode performance for the dashboard
        and coordinator. Never raises — every branch falls back to
        zeroed defaults if something is missing.

        See prompts/build_scalping_agent.md → GET_OBSERVATION_SUMMARY
        SPEC for the full key list. The shape is stable so dashboard /
        coordinator code can rely on it.
        """
        zeroed = {
            "total_evaluated":   0,
            "would_enter":       0,
            "closed":            0,
            "open":              0,
            "win_rate":          0.0,
            "avg_pnl_gross_bps": 0.0,
            "avg_pnl_net_bps":   0.0,
            "avg_hold_sec":      0.0,
            "halted":            bool(self._halted),
            "halt_reason":       self._halt_reason,
            "daily_loss_usd":    float(self._daily_loss),
            "consec_losses":     int(self._consec_losses),
            "by_exchange":       {},
            "fee_viability":     {},
            "top_skip_reasons":  [],
        }
        try:
            total   = len(self._observations)
            entries = [o for o in self._observations if o.would_entry]
            closed  = [o for o in entries if o.exit_price > 0]
            wins    = sum(1 for o in closed if (o.pnl_bps or 0) > 0)
            win_rate = wins / len(closed) if closed else 0.0
            avg_gross = (
                sum((o.pnl_bps or 0) for o in closed) / len(closed)
                if closed else 0.0
            )
            avg_net = (
                sum((o.pnl_bps - o.round_trip_cost_bps) for o in closed) / len(closed)
                if closed else 0.0
            )
            avg_hold = (
                sum((o.hold_sec or 0) for o in closed) / len(closed)
                if closed else 0.0
            )

            # Per-exchange breakdown over closed entries.
            by_ex: dict[str, dict] = {}
            for o in closed:
                agg = by_ex.setdefault(o.exchange, {
                    "n": 0, "wins": 0, "net_sum": 0.0, "fee_sum": 0.0,
                })
                agg["n"] += 1
                if (o.pnl_bps or 0) > 0:
                    agg["wins"] += 1
                agg["net_sum"] += (o.pnl_bps - o.round_trip_cost_bps)
                agg["fee_sum"] += o.round_trip_cost_bps
            by_exchange = {
                ex: {
                    "n":           agg["n"],
                    "win_rate":    agg["wins"] / agg["n"] if agg["n"] else 0.0,
                    "avg_net_bps": agg["net_sum"] / agg["n"] if agg["n"] else 0.0,
                    "fee_bps":     agg["fee_sum"] / agg["n"] if agg["n"] else 0.0,
                }
                for ex, agg in by_ex.items()
            }

            # Live fee viability per approved scalp exchange — lets the
            # dashboard show fee health without importing FeeManager.
            fee_viability: dict[str, dict] = {}
            for ex in settings.STRATEGY_EXCHANGE_MAP.get("scalp", []):
                try:
                    tp, sl = self._fee_manager.compute_tp_sl(ex, settings.SCALP_PAIRS[0])
                    rt     = self._fee_manager.round_trip_bps(ex, settings.SCALP_PAIRS[0])
                    be     = self._fee_manager.breakeven_win_rate(
                        ex, settings.SCALP_PAIRS[0], tp, sl,
                    )
                    viable, _ = self._fee_manager.is_viable(ex, settings.SCALP_PAIRS[0])
                    fee_viability[ex] = {
                        "viable":       bool(viable),
                        "breakeven_wr": float(be),
                        "tp_bps":       float(tp),
                        "sl_bps":       float(sl),
                        "rt_bps":       float(rt),
                    }
                except Exception:
                    fee_viability[ex] = {
                        "viable": False, "breakeven_wr": 1.0,
                        "tp_bps": 0.0, "sl_bps": 0.0, "rt_bps": 0.0,
                    }

            # Top-5 skip reasons across all skipped observations.
            from collections import Counter
            ctr = Counter(
                o.skip_reason for o in self._observations
                if not o.would_entry and o.skip_reason
            )
            top_skips = ctr.most_common(5)

            zeroed.update({
                "total_evaluated":   total,
                "would_enter":       len(entries),
                "closed":            len(closed),
                "open":              len(self._positions),
                "win_rate":          win_rate,
                "avg_pnl_gross_bps": avg_gross,
                "avg_pnl_net_bps":   avg_net,
                "avg_hold_sec":      avg_hold,
                "by_exchange":       by_exchange,
                "fee_viability":     fee_viability,
                "top_skip_reasons":  top_skips,
            })
        except Exception as e:
            log.debug("[ScalpingAgent] get_observation_summary failed: %s", e)
        return zeroed
