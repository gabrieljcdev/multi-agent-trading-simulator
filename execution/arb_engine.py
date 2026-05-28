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
    # Per-side bid-ask spread in % — fed to the depth-aware slippage model.
    spread_buy_pct:  float = 0.0
    spread_sell_pct: float = 0.0
    # USD depth of the top N book levels on each side (mirrors what the
    # liquidity check sums up). Slippage scales with size/depth.
    depth_buy_usd:   float = 0.0
    depth_sell_usd:  float = 0.0
    # arb_opportunities row id — set when the scan logs this gap; lets
    # _execute_arb update the row with executed=True post-fill.
    opportunity_log_id: Optional[int] = None
    # Funding-rate arb only — % per 8h that triggered the opportunity.
    funding_rate_pct: Optional[float] = None


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
    # "executed" on the happy path; "balance_fail" when capital gate
    # blocked. Mirrors arb_trades.status.
    status:            str   = "executed"
    slippage_buy_pct:  Optional[float] = None
    slippage_sell_pct: Optional[float] = None


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


def slippage_pct(base_spread_pct: float, size_usd: float, depth_usd: float) -> float:
    """Depth-aware slippage model:

        slip = base_spread * sqrt(size_usd / depth_usd)

    Clamped to [ARB_SLIPPAGE_MIN_PCT, ARB_SLIPPAGE_MAX_PCT].  Used both
    by sim fills and by the spot/perp legs of FundingRateArbEngine.
    """
    if depth_usd <= 0 or base_spread_pct <= 0 or size_usd <= 0:
        slip = settings.ARB_SLIPPAGE_MIN_PCT
    else:
        slip = base_spread_pct * (size_usd / depth_usd) ** 0.5
    return max(
        settings.ARB_SLIPPAGE_MIN_PCT,
        min(settings.ARB_SLIPPAGE_MAX_PCT, slip),
    )


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
        *,
        fund_id:    str            = "arb",
        exchanges:  Optional[list] = None,
    ):
        self.dashboard = dashboard
        self.sim_mode  = settings.SIM_MODE if sim_mode is None else sim_mode

        # fund_id labels logs/stats. `exchanges` scopes _build_clients to a
        # subset of ARB_FEE_MAP (the arb fund passes STRATEGY_EXCHANGE_MAP
        # ["arb"]); None builds every venue in ARB_FEE_MAP.
        self.fund_id          = fund_id
        self._exchange_filter = set(exchanges) if exchanges is not None else None

        # Exchange clients
        if exchange_clients is None:
            exchange_clients = self._build_clients()
        self._exchanges: dict = dict(exchange_clients)
        logger.info(
            f"ArbEngine[{self.fund_id}]: {len(self._exchanges)} exchanges ready "
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

        # Pre-execution capital gate counter — incremented every time a
        # would-be arb is blocked because one side doesn't have enough
        # free balance. Surfaced via get_stats() so the dashboard can
        # show how many real misses pre-positioning cost us.
        self.missed_balance_checks: int = 0

        # BalanceAgent-set deployable capital. Defaults to the configured
        # arb fund constant so a fresh launch sizes off the original
        # plan; the agent wrapper raises this as realised profit
        # accumulates so position sizes compound (Block 1 of the
        # BalanceAgent build). Reads as a multiplier into ARB_BASE_POSITION_USD
        # so any sweep on that constant still works.
        self._capital_allocation: float = float(
            getattr(settings, "FUND_ARB_CAPITAL", 0.0) or 0.0
        )

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
            "fund_id":               self.fund_id,
            "status":                self._status,
            "daily_pnl":             self._daily_pnl_usd,
            "total_pnl":             self._total_pnl_usd,
            "total_trades":          self._total_trades,
            "consecutive_losses":    self._consecutive_losses,
            "active_arbs":           self._active_arbs,
            "last_opportunity":      self._last_opportunity,
            "last_trade_time":       self._last_trade_time.isoformat() if self._last_trade_time else None,
            "missed_balance_checks": self.missed_balance_checks,
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
        above-threshold opportunity, or None.

        Side effect: every detected gap that passes the liquidity check
        is logged to arb_opportunities, regardless of whether it clears
        the execution threshold. The best above-threshold candidate
        carries its opportunity-row id so _execute_arb can mark it as
        executed after the trade fires.
        """
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
                    bids_a = book_a.get("bids") or []
                    asks_b = book_b.get("asks") or []
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

                    ask_liq = _liquidity_usd(asks_a)
                    bid_liq = _liquidity_usd(bids_b)
                    if ask_liq < settings.ARB_MIN_LIQUIDITY_USD or bid_liq < settings.ARB_MIN_LIQUIDITY_USD:
                        continue

                    # Per-side bid-ask spread feeds the depth-aware
                    # slippage model. Fall back to 0 when one side is
                    # missing (treated as "no info"; slippage clamps to
                    # the configured minimum).
                    spread_buy_pct  = 0.0
                    spread_sell_pct = 0.0
                    if bids_a:
                        try:
                            best_bid_a = float(bids_a[0][0])
                            if buy_price > 0:
                                spread_buy_pct = (buy_price - best_bid_a) / buy_price * 100.0
                        except (TypeError, IndexError, ValueError):
                            pass
                    if asks_b:
                        try:
                            best_ask_b = float(asks_b[0][0])
                            if sell_price > 0:
                                spread_sell_pct = (best_ask_b - sell_price) / sell_price * 100.0
                        except (TypeError, IndexError, ValueError):
                            pass

                    threshold = min_gap_threshold(a, b)
                    above_threshold = net >= threshold

                    # Log every above-liquidity gap, even sub-threshold
                    # ones — execution-rate stats only mean something
                    # when we know the denominator.
                    opp_log_id = self._log_opportunity(
                        symbol=sym, buy_ex=a, sell_ex=b,
                        gap_pct=net, threshold_pct=threshold,
                        above_threshold=above_threshold,
                        depth_buy_usd=ask_liq, depth_sell_usd=bid_liq,
                    )

                    if not above_threshold:
                        continue

                    # Dynamic position sizing — wider gaps get bigger
                    # positions, capped at ARB_SIZE_MULTIPLIER_CAP × base.
                    # Still respect the 10%-of-depth liquidity cap and
                    # the per-exchange capital budget. Base scales with
                    # the BalanceAgent-set capital allocation so realised
                    # profit compounds into the next trade's size.
                    gap_ratio = net / threshold if threshold > 0 else 1.0
                    size_multiplier = min(gap_ratio, settings.ARB_SIZE_MULTIPLIER_CAP)
                    dynamic_size = self._dynamic_base_position() * size_multiplier
                    max_size = min(
                        dynamic_size,
                        min(ask_liq, bid_liq) * 0.10,
                        settings.ARB_CAPITAL_PER_EXCHANGE,
                    )

                    candidate = ArbOpportunity(
                        symbol=sym, buy_exchange=a, sell_exchange=b,
                        buy_price=buy_price, sell_price=sell_price,
                        gross_gap_pct=gross, net_gap_pct=net,
                        max_size_usd=max_size, detected_at=time.monotonic(),
                        spread_buy_pct=spread_buy_pct,
                        spread_sell_pct=spread_sell_pct,
                        depth_buy_usd=ask_liq, depth_sell_usd=bid_liq,
                        opportunity_log_id=opp_log_id,
                    )
                    if best is None or candidate.net_gap_pct > best.net_gap_pct:
                        best = candidate
        return best

    @staticmethod
    def _log_opportunity(**kwargs) -> Optional[int]:
        """Safe wrapper around db_queries.log_arb_opportunity — never
        let a DB hiccup take down the scan loop."""
        try:
            return db_queries.log_arb_opportunity(**kwargs)
        except Exception as e:
            logger.debug(f"log_arb_opportunity: {e}")
            return None

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

                # Hard capital gate — must pass before any leg fires.
                ok, detail = await self._check_balances(opp, size_base)
                if not ok:
                    self.missed_balance_checks += 1
                    logger.warning(f"arb balance_fail {opp.symbol}: {detail}")
                    try:
                        db_queries.log_arb_balance_fail(
                            symbol=opp.symbol,
                            buy_exchange=opp.buy_exchange,
                            sell_exchange=opp.sell_exchange,
                            detail=detail or "",
                            sim_mode=self.sim_mode,
                        )
                    except Exception as e:
                        logger.debug(f"log_arb_balance_fail: {e}")
                    return

                slip_buy_pct: Optional[float] = None
                slip_sell_pct: Optional[float] = None
                if self.sim_mode:
                    buy_fill, sell_fill, slip_buy_pct, slip_sell_pct = self._sim_fills(opp)
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
                    status="executed",
                    slippage_buy_pct=slip_buy_pct,
                    slippage_sell_pct=slip_sell_pct,
                )
                self._update_stats(result)
                self._notify_dashboard(result)
                trade_id = self._log_to_db(result)
                self._mark_opportunity_executed(opp.opportunity_log_id, trade_id)
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

    def _dynamic_base_position(self) -> float:
        """ARB_BASE_POSITION_USD scaled by (current allocation / starting
        allocation). When the BalanceAgent compounds realised profit
        into _capital_allocation, this scales up so the next trade is
        bigger. Falls back to 1.0× when the starting fund constant is
        unset (cold start).
        """
        base = float(settings.ARB_BASE_POSITION_USD)
        starting = float(getattr(settings, "FUND_ARB_CAPITAL", 0.0) or 0.0)
        if starting <= 0:
            return base
        factor = max(0.0, self._capital_allocation / starting)
        # Clamp factor to a reasonable band so a runaway loop in the
        # BalanceAgent (which can't happen — set rejects below open
        # notional — but defence in depth) doesn't 100× the position.
        factor = min(factor, 10.0)
        return base * factor

    async def _check_balances(
        self,
        opp: ArbOpportunity,
        size_base: float,
    ) -> tuple[bool, Optional[str]]:
        """Hard pre-execution capital gate.

        Buy side must hold ``size_usd × (1 + buffer)`` in the quote
        currency; sell side must hold ``size_base × (1 + buffer)`` of
        the base currency. In sim mode the InventoryState ledger view
        is the source of truth (it respects this fund's claim on a
        shared venue and any pending-out transfers). In live mode the
        ccxt balance API is authoritative; InventoryState's scoped-pause
        gate still runs first.
        """
        if "/" in opp.symbol:
            base_ccy, quote_ccy = opp.symbol.split("/", 1)
        else:
            base_ccy, quote_ccy = opp.symbol, "USDT"

        buffer = 1.0 + settings.ARB_BALANCE_BUFFER_PCT / 100.0
        required_quote = opp.max_size_usd * buffer
        required_base  = size_base        * buffer

        # Scoped-pause gate via InventoryState — refuses routes the
        # BalanceAgent has flagged after a failed rebalance.
        try:
            from agents.balance.inventory_state import inventory_state
            if not inventory_state.can_arb(
                opp.symbol, "buy", opp.max_size_usd,
                buy_exchange=opp.buy_exchange,
                sell_exchange=opp.sell_exchange,
                fund=self.fund_id,
            ):
                return False, (
                    f"InventoryState.can_arb blocked "
                    f"{opp.buy_exchange}->{opp.sell_exchange}"
                )
        except Exception as e:
            # Fail-open on InventoryState import errors — the existing
            # balance gate still catches the actual mismatch.
            logger.debug("InventoryState.can_arb skipped: %s", e)

        # InventoryState's route-pause gate above is the safety integration;
        # the actual balance check stays on ccxt's authoritative response
        # (sim mode still falls through to "ok" when fetch_balance is
        # unavailable, preserving the existing behaviour). When the
        # BalanceAgent's allocations supersede the physical view, the
        # gate's compound-aware reading happens via can_arb (above).
        buy_free  = await self._fetch_free_balance(
            self._exchanges.get(opp.buy_exchange),  quote_ccy,
        )
        sell_free = await self._fetch_free_balance(
            self._exchanges.get(opp.sell_exchange), base_ccy,
        )

        if buy_free is None or sell_free is None:
            if not self.sim_mode:
                return False, "fetch_balance unavailable on one or both exchanges"
            return True, None

        if buy_free < required_quote:
            return False, (
                f"{opp.buy_exchange} {quote_ccy} free={buy_free:.4f} "
                f"< required={required_quote:.4f}"
            )
        if sell_free < required_base:
            return False, (
                f"{opp.sell_exchange} {base_ccy} free={sell_free:.8f} "
                f"< required={required_base:.8f}"
            )
        return True, None

    @staticmethod
    async def _fetch_free_balance(ex, ccy: str) -> Optional[float]:
        """Return the free balance of `ccy` on `ex`, or None if the
        balance API is unavailable or returned unparseable shape."""
        if ex is None:
            return None
        fetch = getattr(ex, "fetch_balance", None)
        if fetch is None:
            return None
        try:
            res = fetch()
            bal = await res if asyncio.iscoroutine(res) else res
        except Exception as e:
            logger.debug(f"fetch_balance failed: {e}")
            return None
        if not isinstance(bal, dict):
            return None
        free = bal.get("free")
        if not isinstance(free, dict):
            return None
        try:
            return float(free.get(ccy, 0) or 0)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _mark_opportunity_executed(opp_id: Optional[int], trade_id: Optional[int]) -> None:
        if opp_id is None:
            return
        try:
            db_queries.mark_arb_opportunity_executed(opp_id, trade_id)
        except Exception as e:
            logger.debug(f"mark_arb_opportunity_executed: {e}")

    def _sim_fills(
        self,
        opp: ArbOpportunity,
    ) -> tuple[float, float, float, float]:
        """Depth-aware slippage applied independently to each leg.

        Returns (buy_fill, sell_fill, slip_buy_pct, slip_sell_pct). The
        per-leg slippage is also surfaced on the ArbResult so the DB
        row captures what the model thought the impact was.
        """
        slip_buy_pct = slippage_pct(
            opp.spread_buy_pct, opp.max_size_usd, opp.depth_buy_usd,
        )
        slip_sell_pct = slippage_pct(
            opp.spread_sell_pct, opp.max_size_usd, opp.depth_sell_usd,
        )
        buy_fill  = opp.buy_price  * (1.0 + slip_buy_pct  / 100.0)
        sell_fill = opp.sell_price * (1.0 - slip_sell_pct / 100.0)
        return buy_fill, sell_fill, slip_buy_pct, slip_sell_pct

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

    def _log_to_db(self, result: ArbResult) -> Optional[int]:
        try:
            return db_queries.log_arb_trade(result, sim_mode=self.sim_mode)
        except Exception as e:
            logger.debug(f"log_arb_trade: {e}")
            return None

    # ── Circuit breakers ────────────────────────────────────────────────

    def _cb_triggered(self) -> bool:
        # %-based daily-loss halt — scales with the fund's allocation so a
        # compounding fund doesn't silently tighten its leash. 0-alloc means
        # no allocation gate to lose against → halt is a no-op (never divide
        # by zero, never halt a zero-capital engine on this rule).
        alloc = float(self._capital_allocation or 0.0)
        if alloc > 0:
            halt_usd = (float(settings.ARB_DAILY_LOSS_HALT_PCT) / 100.0) * alloc
            if self._daily_pnl_usd <= -halt_usd:
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
            if self._exchange_filter is not None and name not in self._exchange_filter:
                continue
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


# ─────────────────────────────────────────────────────────────────────────
# FundingRateArbEngine — sibling engine for funding-rate carry
# ─────────────────────────────────────────────────────────────────────────
#
# Strategy: when a perpetual's funding rate exceeds
# ARB_FUNDING_RATE_MIN_PCT (per 8h), open spot-long + perp-short. The
# position is directionally neutral and earns the funding rate until it
# decays below ARB_FUNDING_RATE_EXIT_PCT.
#
# Data source: data_sources.get_funding_rates() — backed by Coinglass
# as the primary source plus BinanceFutures / BybitDerivs as fallbacks.
# When no source has cached a reading yet the aggregator returns {}
# and the scan loop simply does nothing on that tick.
#
# Circuit breakers mirror ArbEngine but use independent thresholds —
# funding-rate carry has a different loss profile than cross-exchange
# arb and shouldn't share the same halt limits.

class FundingRateArbEngine:
    """Funding-rate carry engine, wired to the data_sources aggregator.

    Funding rates come from data_sources.get_funding_rates(), which
    blends every available source that publishes the funding_rate
    metric (Coinglass is primary; BinanceFutures + BybitDerivs are
    secondaries). Construct with no args in normal operation; tests
    pass sim_mode=True.
    """

    def __init__(
        self,
        sim_mode: Optional[bool] = None,
        dashboard=None,
    ):
        self.sim_mode  = settings.SIM_MODE if sim_mode is None else sim_mode
        self.dashboard = dashboard

        self._running:     bool          = False
        self._status:      str           = STATUS_OFFLINE
        self._scan_task                  = None
        self._reset_task                 = None

        # Independent circuit-breaker state
        self._daily_pnl_usd:      float            = 0.0
        self._total_pnl_usd:      float            = 0.0
        self._total_trades:       int              = 0
        self._consecutive_losses: int              = 0
        self._last_trade_time:    Optional[datetime] = None

    # ── Public API ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Boot the funding-rate watch loop. Returns when stop() flips
        _running. Logs the Coinglass connection state once on boot so
        the operator knows whether real rates are flowing."""
        self._log_coinglass_status()
        self._running = True
        self._status  = STATUS_RUNNING
        self._scan_task  = asyncio.create_task(self._scan_loop())
        self._reset_task = asyncio.create_task(self._daily_reset_loop())
        await asyncio.gather(self._scan_task, self._reset_task,
                             return_exceptions=True)

    @staticmethod
    def _log_coinglass_status() -> None:
        """One-shot startup log: INFO when Coinglass is reachable,
        WARN otherwise. Kept defensive — the aggregator import or the
        coinglass attribute could be missing in test harnesses, and we
        don't want that to block the engine from starting."""
        try:
            from data_sources import data_sources as ds
            coinglass = getattr(ds, "coinglass", None)
            if coinglass is not None and coinglass.is_available():
                logger.info("Funding rate arb engine connected to Coinglass")
                return
            logger.warning(
                "Funding rate arb engine: Coinglass unavailable — "
                "running without live funding-rate data"
            )
        except Exception as e:
            logger.warning(f"Funding rate arb engine: Coinglass probe failed — {e}")

    async def stop(self) -> None:
        self._running = False
        for task in (self._scan_task, self._reset_task):
            if task is not None and not task.done():
                task.cancel()
        self._status = STATUS_STOPPED

    async def close_all_positions(self) -> None:
        try:
            db_queries.log_circuit_breaker(
                reason="funding_arb_kill",
                detail=f"daily_pnl=${self._daily_pnl_usd:.2f}",
            )
        except Exception:
            pass
        self._status = STATUS_STOPPED

    def get_stats(self) -> dict:
        return {
            "status":             self._status,
            "daily_pnl":          self._daily_pnl_usd,
            "total_pnl":          self._total_pnl_usd,
            "total_trades":       self._total_trades,
            "consecutive_losses": self._consecutive_losses,
            "last_trade_time":    self._last_trade_time.isoformat() if self._last_trade_time else None,
        }

    # ── Data layer ──────────────────────────────────────────────────────

    async def fetch_funding_rates(self) -> dict[str, float]:
        """Return ``{symbol: latest funding rate}`` from the data_sources
        aggregator. Empty when no source has cached a reading yet —
        callers (the scan loop) handle that case as "nothing to do"."""
        try:
            from data_sources import data_sources as ds
        except Exception as e:
            logger.debug(f"data_sources import failed: {e}")
            return {}
        try:
            return ds.get_funding_rates()
        except Exception as e:
            logger.debug(f"data_sources.get_funding_rates failed: {e}")
            return {}

    # ── Scan loop ───────────────────────────────────────────────────────

    async def _scan_loop(self) -> None:
        """Periodically pull funding rates; surface eligible carries.

        Today the loop reads rates but doesn't yet route the spot-long
        + perp-short pair — that's the next phase. The cycle exists so
        circuit-breaker checks keep ticking and the operator can see
        rates flow through the dashboard once the route lands.
        """
        interval = settings.ARB_SCAN_INTERVAL_MS / 1000.0
        while self._running:
            try:
                if self._cb_triggered():
                    if self._status != STATUS_HALTED:
                        logger.warning(
                            "FundingRateArbEngine: circuit breaker triggered — HALTED"
                        )
                    self._status = STATUS_HALTED
                    await asyncio.sleep(interval)
                    continue
                rates = await self.fetch_funding_rates()
                if rates:
                    # Execution path lands in a follow-up — for now the
                    # rate fetch keeps the cache warm and the dashboard
                    # visible.
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"funding arb scan loop: {e}", exc_info=True)
            await asyncio.sleep(interval)

    # ── Circuit breakers ────────────────────────────────────────────────

    def _cb_triggered(self) -> bool:
        # FundingRateArbEngine lives inside the arb fund — there's no
        # per-engine allocation, so we measure the daily-loss halt against
        # the arb fund constant. 0-allocation (test fixture, etc.) → no-op.
        alloc = float(getattr(settings, "FUND_ARB_CAPITAL", 0.0) or 0.0)
        if alloc > 0:
            halt_usd = (float(settings.ARB_FUNDING_DAILY_LOSS_HALT_PCT) / 100.0) * alloc
            if self._daily_pnl_usd <= -halt_usd:
                return True
        if self._consecutive_losses >= settings.ARB_FUNDING_CONSECUTIVE_LOSS_HALT:
            return True
        return False

    async def _daily_reset_loop(self) -> None:
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
            logger.info("FundingRateArbEngine: daily P&L reset at UTC midnight")
