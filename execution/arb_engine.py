"""
execution/arb_engine.py

Standalone cross-exchange arbitrage engine.

Design principles (from prompts/build_arb_engine.md):
  * No Claude evaluation — pure rule-based execution
  * Time critical — gaps close in seconds, no approval gate
  * Own capital pool — separate from signal agent
  * Own circuit breakers — independent of the main bot
  * Both legs placed simultaneously via asyncio.gather
  * Per-symbol locks — prevent double-execution on same pair
  * Fee-aware — net gap after fees must exceed threshold

Standalone: only imports from ccxt, config.settings, and database.queries.
Never imports core/bot.py or agents/* (the ArbAgentWrapper imports us,
not the other way around).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

try:
    import ccxt.async_support as ccxt
except Exception:                        # pragma: no cover — ccxt always present in venv
    ccxt = None                          # type: ignore

from config import settings
from database import queries as db_queries

logger = logging.getLogger(__name__)


# Status sentinel strings — mirror agents/base.py without importing it
STATUS_OFFLINE = "OFFLINE"
STATUS_RUNNING = "RUNNING"
STATUS_HALTED  = "HALTED"
STATUS_STOPPED = "STOPPED"


# ─────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class ArbOpportunity:
    symbol:        str
    buy_exchange:  str
    sell_exchange: str
    buy_price:     float
    sell_price:    float
    gross_gap_pct: float
    net_gap_pct:   float                  # after both legs' fees
    max_size_usd:  float                  # liquidity-limited
    detected_at:   float                  # time.monotonic() at scan time


@dataclass
class ArbResult:
    opportunity:    ArbOpportunity
    success:        bool
    buy_fill:       float
    sell_fill:      float
    gross_pnl_usd:  float
    net_pnl_usd:    float
    execution_ms:   float
    error:          Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers — exported so tests can pin gap math directly
# ─────────────────────────────────────────────────────────────────────────

def gross_gap_pct(buy_price: float, sell_price: float) -> float:
    if buy_price <= 0:
        return 0.0
    return (sell_price - buy_price) / buy_price * 100.0


def net_gap_pct(
    buy_price:  float,
    sell_price: float,
    buy_ex:     str,
    sell_ex:    str,
    fee_map:    dict,
) -> tuple[float, float]:
    """Return (gross_pct, net_pct) — net subtracts both legs' fees."""
    gross   = gross_gap_pct(buy_price, sell_price)
    fee_buy = fee_map.get(buy_ex, 0.002)
    fee_sell = fee_map.get(sell_ex, 0.002)
    net = gross - (fee_buy + fee_sell) * 100
    return gross, net


def min_gap_threshold(buy_ex: str, sell_ex: str) -> float:
    """Bitget's ultra-low fee unlocks tighter thresholds; everywhere else
    falls back to the conservative cross-exchange threshold."""
    if "bitget" in (buy_ex, sell_ex):
        return settings.ARB_MIN_GAP_PCT
    return settings.ARB_MIN_GAP_PCT_FALLBACK


def _liquidity_usd(levels: list, depth: int = 3) -> float:
    """Sum top `depth` book levels as price × size."""
    total = 0.0
    for entry in levels[:depth]:
        try:
            price, size = float(entry[0]), float(entry[1])
            total += price * size
        except (TypeError, IndexError, ValueError):
            continue
    return total


# ─────────────────────────────────────────────────────────────────────────
# ArbEngine
# ─────────────────────────────────────────────────────────────────────────

class ArbEngine:
    """Fully async cross-exchange arb engine.

    Construct with no args to build CCXT clients from env + ARB_FEE_MAP.
    Pass `exchange_clients={"bitget": client, ...}` in tests.
    """

    def __init__(
        self,
        exchange_clients: Optional[dict] = None,
        dashboard=None,
        sim_mode:         Optional[bool] = None,
    ):
        self.dashboard = dashboard
        self.sim_mode  = settings.SIM_MODE if sim_mode is None else sim_mode

        # Exchange clients
        if exchange_clients is None:
            exchange_clients = self._build_clients()
        self._exchanges: dict = dict(exchange_clients)
        logger.info(
            f"ArbEngine: {len(self._exchanges)} exchanges ready "
            f"({', '.join(self._exchanges) or '—'})"
        )

        # Lifecycle
        self._running     = False
        self._status      = STATUS_OFFLINE
        self._scan_task   = None
        self._reset_task  = None

        # Own circuit-breaker state
        self._daily_pnl_usd:      float            = 0.0
        self._total_pnl_usd:      float            = 0.0
        self._total_trades:       int              = 0
        self._consecutive_losses: int              = 0
        self._last_opportunity:   Optional[str]    = None
        self._last_trade_time:    Optional[datetime] = None

        # Concurrency primitives
        self._symbol_locks: dict[str, asyncio.Lock] = {
            sym: asyncio.Lock() for sym in settings.ARB_WATCH_PAIRS
        }
        self._semaphore = asyncio.Semaphore(settings.ARB_MAX_CONCURRENT)
        # Track in-flight count manually — asyncio.Semaphore has no public counter
        self._active_arbs: int = 0

    # ── Public API ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Boot scan + daily-reset loops. Returns when stop() flips _running."""
        if len(self._exchanges) < 2:
            logger.warning(
                f"ArbEngine: need ≥2 exchanges, have {len(self._exchanges)} — staying OFFLINE"
            )
            self._status = STATUS_OFFLINE
            return
        self._running = True
        self._status  = STATUS_RUNNING
        self._scan_task  = asyncio.create_task(self._scan_loop())
        self._reset_task = asyncio.create_task(self._daily_reset_loop())
        await asyncio.gather(self._scan_task, self._reset_task,
                             return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        for task in (self._scan_task, self._reset_task):
            if task is not None and not task.done():
                task.cancel()
        # Close CCXT clients
        for name, ex in self._exchanges.items():
            try:
                close = getattr(ex, "close", None)
                if close is None:
                    continue
                res = close()
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.debug(f"close {name}: {e}")
        self._status = STATUS_STOPPED

    async def close_all_positions(self) -> None:
        """Arb positions complete in milliseconds — both legs already
        filled by the time this is reachable. We still cancel any pending
        orders the exchange exposes, and log a halt event."""
        for name, ex in self._exchanges.items():
            try:
                cancel = getattr(ex, "cancel_all_orders", None)
                if cancel is None:
                    continue
                res = cancel()
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.debug(f"cancel_all_orders {name}: {e}")
        try:
            db_queries.log_circuit_breaker(
                reason="arb_kill",
                detail=f"daily_pnl=${self._daily_pnl_usd:.2f}",
            )
        except Exception:
            pass
        self._status = STATUS_STOPPED

    def get_stats(self) -> dict:
        """Return a stats dict — ArbAgentWrapper translates this to AgentStats."""
        return {
            "status":             self._status,
            "daily_pnl":          self._daily_pnl_usd,
            "total_pnl":          self._total_pnl_usd,
            "total_trades":       self._total_trades,
            "consecutive_losses": self._consecutive_losses,
            "active_arbs":        self._active_arbs,
            "last_opportunity":   self._last_opportunity,
            "last_trade_time":    self._last_trade_time.isoformat() if self._last_trade_time else None,
        }

    # ── Scan loop ───────────────────────────────────────────────────────

    async def _scan_loop(self) -> None:
        interval = settings.ARB_SCAN_INTERVAL_MS / 1000.0
        while self._running:
            try:
                if self._cb_triggered():
                    if self._status != STATUS_HALTED:
                        logger.warning("ArbEngine: circuit breaker triggered — HALTED")
                    self._status = STATUS_HALTED
                    await asyncio.sleep(interval)
                    continue
                if self._active_arbs >= settings.ARB_MAX_CONCURRENT:
                    await asyncio.sleep(interval)
                    continue

                opp = await self._find_best_opportunity()
                if opp is not None:
                    self._last_opportunity = (
                        f"{opp.symbol} {opp.buy_exchange}→{opp.sell_exchange} "
                        f"net={opp.net_gap_pct:.3f}%"
                    )
                    # Fire-and-forget — the scan continues independently.
                    asyncio.create_task(self._execute_arb(opp))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"arb scan loop: {e}", exc_info=True)

            await asyncio.sleep(interval)

    async def _find_best_opportunity(self) -> Optional[ArbOpportunity]:
        """Fetch every (exchange, pair) book once, then evaluate every
        cross-exchange combination both directions. Returns the best
        above-threshold opportunity, or None."""
        ex_names = list(self._exchanges.keys())
        if len(ex_names) < 2:
            return None

        # Concurrent fetch of all books
        fetch_keys = []
        fetch_coros = []
        for sym in settings.ARB_WATCH_PAIRS:
            for name in ex_names:
                fetch_keys.append((name, sym))
                fetch_coros.append(self._safe_fetch_book(self._exchanges[name], sym))
        books = await asyncio.gather(*fetch_coros, return_exceptions=False)
        idx = {k: b for k, b in zip(fetch_keys, books) if b is not None}

        best: Optional[ArbOpportunity] = None
        for sym in settings.ARB_WATCH_PAIRS:
            for a in ex_names:
                for b in ex_names:
                    if a == b:
                        continue
                    book_a = idx.get((a, sym))
                    book_b = idx.get((b, sym))
                    if not book_a or not book_b:
                        continue
                    asks_a = book_a.get("asks") or []
                    bids_b = book_b.get("bids") or []
                    if not asks_a or not bids_b:
                        continue
                    try:
                        buy_price  = float(asks_a[0][0])
                        sell_price = float(bids_b[0][0])
                    except (TypeError, IndexError, ValueError):
                        continue
                    if buy_price <= 0 or sell_price <= 0:
                        continue

                    gross, net = net_gap_pct(buy_price, sell_price, a, b, settings.ARB_FEE_MAP)
                    if net < min_gap_threshold(a, b):
                        continue

                    ask_liq = _liquidity_usd(asks_a)
                    bid_liq = _liquidity_usd(bids_b)
                    if ask_liq < settings.ARB_MIN_LIQUIDITY_USD or bid_liq < settings.ARB_MIN_LIQUIDITY_USD:
                        continue

                    # Three independent caps: the legacy per-trade max,
                    # 10% of order-book depth, and the per-exchange
                    # capital budget (FIX 6 — keeps any single venue's
                    # exposure bounded by the agent allocation).
                    max_size = min(
                        settings.ARB_MAX_POSITION_USD,
                        min(ask_liq, bid_liq) * 0.10,
                        settings.ARB_CAPITAL_PER_EXCHANGE,
                    )

                    candidate = ArbOpportunity(
                        symbol=sym, buy_exchange=a, sell_exchange=b,
                        buy_price=buy_price, sell_price=sell_price,
                        gross_gap_pct=gross, net_gap_pct=net,
                        max_size_usd=max_size, detected_at=time.monotonic(),
                    )
                    if best is None or candidate.net_gap_pct > best.net_gap_pct:
                        best = candidate
        return best

    async def _safe_fetch_book(self, ex, sym: str):
        try:
            res = ex.fetch_order_book(sym, limit=5)
            if asyncio.iscoroutine(res):
                return await res
            return res
        except Exception as e:
            logger.debug(f"fetch_order_book {sym}: {e}")
            return None

    # ── Execution ───────────────────────────────────────────────────────

    async def _execute_arb(self, opp: ArbOpportunity) -> None:
        lock = self._symbol_locks.get(opp.symbol)
        if lock is None or lock.locked():
            # Per-symbol re-entry guard — somebody else is already on this pair
            return

        async with lock, self._semaphore:
            self._active_arbs += 1
            start_t = time.perf_counter()
            try:
                size_base = opp.max_size_usd / opp.buy_price if opp.buy_price > 0 else 0.0
                if self.sim_mode:
                    buy_fill, sell_fill = self._sim_fills(opp)
                else:
                    buy_fill, sell_fill = await self._live_fills(opp, size_base)

                fee_buy  = settings.ARB_FEE_MAP.get(opp.buy_exchange,  0.002)
                fee_sell = settings.ARB_FEE_MAP.get(opp.sell_exchange, 0.002)

                gross_pnl = (sell_fill - buy_fill) * size_base
                fees      = (buy_fill * size_base * fee_buy) + (sell_fill * size_base * fee_sell)
                net_pnl   = gross_pnl - fees

                result = ArbResult(
                    opportunity=opp, success=True,
                    buy_fill=buy_fill, sell_fill=sell_fill,
                    gross_pnl_usd=gross_pnl, net_pnl_usd=net_pnl,
                    execution_ms=(time.perf_counter() - start_t) * 1000.0,
                )
                self._update_stats(result)
                self._notify_dashboard(result)
                self._log_to_db(result)
            except Exception as e:
                logger.error(f"arb execute {opp.symbol}: {e}")
                result = ArbResult(
                    opportunity=opp, success=False,
                    buy_fill=0.0, sell_fill=0.0,
                    gross_pnl_usd=0.0, net_pnl_usd=0.0,
                    execution_ms=(time.perf_counter() - start_t) * 1000.0,
                    error=str(e),
                )
                self._update_stats(result)
                self._log_to_db(result)
            finally:
                self._active_arbs = max(0, self._active_arbs - 1)

    def _sim_fills(self, opp: ArbOpportunity) -> tuple[float, float]:
        """+/- 0.02% slippage model — represents realistic market impact
        without making an exchange call."""
        return opp.buy_price * 1.0002, opp.sell_price * 0.9998

    async def _live_fills(
        self,
        opp:       ArbOpportunity,
        size_base: float,
    ) -> tuple[float, float]:
        """Place both legs simultaneously via asyncio.gather — never sequential."""
        buy_ex  = self._exchanges[opp.buy_exchange]
        sell_ex = self._exchanges[opp.sell_exchange]
        buy_task  = buy_ex.create_market_buy_order(opp.symbol, size_base)
        sell_task = sell_ex.create_market_sell_order(opp.symbol, size_base)
        buy_order, sell_order = await asyncio.gather(buy_task, sell_task)
        buy_fill  = float((buy_order  or {}).get("price", opp.buy_price))
        sell_fill = float((sell_order or {}).get("price", opp.sell_price))
        return buy_fill, sell_fill

    # ── State updates ───────────────────────────────────────────────────

    def _update_stats(self, result: ArbResult) -> None:
        if result.success:
            self._total_trades       += 1
            self._total_pnl_usd      += result.net_pnl_usd
            self._daily_pnl_usd      += result.net_pnl_usd
            if result.net_pnl_usd < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0
            self._last_trade_time = datetime.utcnow()

    def _notify_dashboard(self, result: ArbResult) -> None:
        if self.dashboard is None or not result.success:
            return
        try:
            self.dashboard.add_arb(
                pair=result.opportunity.symbol,
                buy_ex=result.opportunity.buy_exchange,
                sell_ex=result.opportunity.sell_exchange,
                gap_pct=result.opportunity.net_gap_pct,
                pnl=result.net_pnl_usd,
            )
        except Exception as e:
            logger.debug(f"dashboard.add_arb: {e}")

    def _log_to_db(self, result: ArbResult) -> None:
        try:
            db_queries.log_arb_trade(result, sim_mode=self.sim_mode)
        except Exception as e:
            logger.debug(f"log_arb_trade: {e}")

    # ── Circuit breakers ────────────────────────────────────────────────

    def _cb_triggered(self) -> bool:
        if self._daily_pnl_usd <= -settings.ARB_DAILY_LOSS_HALT_USD:
            return True
        if self._consecutive_losses >= settings.ARB_CONSECUTIVE_LOSS_HALT:
            return True
        return False

    async def _daily_reset_loop(self) -> None:
        """Sleep until UTC midnight; reset daily P&L; repeat."""
        while self._running:
            now = datetime.utcnow()
            tomorrow_midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            try:
                await asyncio.sleep((tomorrow_midnight - now).total_seconds())
            except asyncio.CancelledError:
                raise
            self._daily_pnl_usd = 0.0
            logger.info("ArbEngine: daily P&L reset at UTC midnight")

    # ── Client construction ─────────────────────────────────────────────

    def _build_clients(self) -> dict:
        """Build CCXT async clients for every exchange in ARB_FEE_MAP that
        ccxt knows about. In live mode each must also have API key + secret
        in env; in sim mode public endpoints are enough."""
        clients = {}
        if ccxt is None:
            return clients
        for name in settings.ARB_FEE_MAP:
            cls = getattr(ccxt, name, None)
            if cls is None:
                logger.debug(f"ccxt has no exchange '{name}'")
                continue
            key    = os.getenv(f"{name.upper()}_API_KEY")
            secret = os.getenv(f"{name.upper()}_SECRET")
            cfg: dict = {"enableRateLimit": True}
            if key and secret:
                cfg["apiKey"] = key
                cfg["secret"] = secret
            elif not self.sim_mode:
                logger.info(f"  {name}: skipped (no API key/secret in live mode)")
                continue
            try:
                clients[name] = cls(cfg)
                logger.info(f"  {name}: client ready")
            except Exception as e:
                logger.warning(f"  {name}: failed to build client — {e}")
        return clients
