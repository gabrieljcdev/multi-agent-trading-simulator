"""
execution/crosschain_engine.py

Cross-CHAIN arb engine — non-atomic, inventory-pre-positioned. Reads the
SAME asset (WETH-USDC) priced differently across L2s (Arbitrum, Base,
Optimism) and computes the signed net-edge for every ordered chain pair
every scan.

NOT the cross-EXCHANGE arb engine (execution/arb_engine.py — that one is
atomic, fee-aware, two-leg-simultaneous on centralised exchanges). This
engine is its sibling on the DEX side and MUST NOT touch arb_engine.py.

Design rationale (research-backed; same shape as the cross-exchange
engine's rule-based execution but a different physics):

* Öz et al. 2025 (arXiv:2501.17335): inventory-based arbs settle in ~9s
  vs ~242s for bridged. Inventory wins 66.96% of the time. So we
  pre-position and NEVER bridge mid-trade.
* Wu et al. 2025: the top-of-block CEX-DEX latency race is an oligopoly
  (3 searchers = 73% of value). We deliberately do NOT compete there;
  we play the slower, inventory-pre-positioned L2 game.
* Gogol et al. 2024 (arXiv:2406.02172): L2 opportunities persist 10–20
  blocks. The persistence gate exploits that — opportunities younger
  than one of our scan cycles or smaller than gas-breakeven are skipped.

OBSERVATION MODE invariant: this engine NEVER calls connector.submit_swap.
Every scan logs one xchain_observations row per evaluated (buy_chain,
sell_chain) pair — passing OR skipping — with the full cost breakdown,
exactly as scalp_observations proved the scalp edge before going live.
XCHAIN_LIVE_ENABLED is a hard gate kept False; the live build is
separate.

Standalone: only imports from config.settings, database.queries, and
the chain-connector layer. Never imports core/bot.py or agents/*.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from config import settings
from database import queries as db_queries
from execution.chains import REGISTERED_CONNECTORS
from execution.chains.base_connector import BaseChainConnector, PoolState

logger = logging.getLogger(__name__)


# Status sentinels — mirror execution/arb_engine.py (avoid importing
# agents/base.py from the engine layer).
STATUS_OFFLINE = "OFFLINE"
STATUS_RUNNING = "RUNNING"
STATUS_HALTED  = "HALTED"
STATUS_STOPPED = "STOPPED"


# ─────────────────────────────────────────────────────────────────────────
# Dataclasses — engine outputs that the agent + DB consume
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class XChainEvaluation:
    """One scan's evaluation of one (buy_chain, sell_chain, symbol) tuple.

    Mirrors the ArbResult shape but for the cross-CHAIN model: cost terms
    are in bps (not fractional %) because gas dominates and bps-resolution
    matters; notional is the size at which the edge was computed (capped
    optimal, not the requested max). would_entry distinguishes the
    candidates that cleared every gate from those that didn't.
    """
    symbol:            str
    buy_chain:         str
    sell_chain:        str
    buy_venue:         str
    sell_venue:        str
    notional_usd:      float
    spread_bps:        float
    rt_fee_bps:        float
    gas_bps:           float
    slip_bps:          float
    bridge_bps:        float
    net_edge_bps:      float
    gas_breakeven_usd: float
    would_entry:       bool
    skip_reason:       str = ""
    # snapshots — kept so retrospective analysis can recompute with
    # different fee/gas assumptions without rerunning the scan
    buy_block:    int   = 0
    sell_block:   int   = 0
    detected_at:  float = field(default_factory=time.monotonic)


@dataclass
class CircuitBreakerState:
    """Independent of the cross-exchange arb engine's breakers and
    independent of the portfolio-level breakers.

    daily_pnl is realised P&L — in observation mode it stays at 0 because
    no positions are opened. Kept here so the live build (separate later
    PR) wires straight into the same halt logic without restructuring.
    """
    daily_pnl_usd:      float = 0.0
    consecutive_losses: int   = 0
    halted:             bool  = False
    halt_reason:        str   = ""


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers — exported so tests can pin the math directly
# ─────────────────────────────────────────────────────────────────────────

def gas_breakeven_usd(b_gas_usd: float, s_gas_usd: float, gas_budget_bps: float) -> float:
    """Smallest notional at which round-trip gas <= gas_budget_bps.

    Below this floor the engine refuses to set would_entry True regardless
    of how wide the spread is — gas dominates small-notional arbs on L2s
    and a $0.20 round-trip gas at 5 bps budget puts the floor near $400.
    Returns inf when either gas is inf (connector failure) or the budget
    is non-positive — both cases mean "no notional satisfies the budget".
    """
    if not math.isfinite(b_gas_usd) or not math.isfinite(s_gas_usd):
        return float("inf")
    if gas_budget_bps <= 0:
        return float("inf")
    return (b_gas_usd + s_gas_usd) / (gas_budget_bps / 1e4)


def optimal_notional_constant_product(
    buy_state:  PoolState,
    sell_state: PoolState,
    fee_bps:    float,
) -> float:
    """Angeris et al. (2019) optimal trade size on a constant-product leg.

    Direction we model: put USDC into the BUY-side pool to receive WETH,
    then sell that WETH against the SELL-side pool at its external spot.
    With x=quote (USDC input), y=base (WETH output), gamma = 1 - fee, and
    p_ext = USDC per WETH on the sell side (the external price for the
    asset we're acquiring), the profit-maximising input dx* (in USDC) is:

        dx* = (sqrt(gamma * x * y * p_ext) - x) / gamma

    Returned directly as USDC notional. Floored at 0 when sell_spot is
    not high enough to justify trading at any size (post-fee no arb).
    """
    if buy_state.reserve_base <= 0 or buy_state.reserve_quote <= 0:
        return 0.0
    if sell_state.spot_price <= 0:
        return 0.0
    gamma = 1.0 - fee_bps / 1e4
    if gamma <= 0:
        return 0.0
    x = buy_state.reserve_quote          # USDC reserves (input asset)
    y = buy_state.reserve_base           # WETH reserves (output asset)
    p_ext = sell_state.spot_price        # USDC per WETH on sell chain
    inner = gamma * x * y * p_ext
    if inner <= 0:
        return 0.0
    dx_star = (math.sqrt(inner) - x) / gamma     # USDC notional
    if dx_star <= 0:
        return 0.0
    return dx_star


def capped_optimal_notional(
    buy_state:  PoolState,
    sell_state: PoolState,
    *,
    fee_bps:                  float,
    slippage_tolerance_bps:   float,
    max_position_usd:         float,
) -> float:
    """Optimal notional, then capped by:

    - slippage_tolerance_bps on BOTH legs (the smaller of the two limits
      wins), via the constant-product 1%-impact depth as a linear scaler:
      depth_usd_at_tol = depth_usd_1pct * (tolerance / 100 bps)
    - the depth_usd_1pct of either pool, whichever is shallower
    - settings.XCHAIN_MAX_POSITION_USD

    All four caps protect a different failure mode; we take the min.
    """
    raw = optimal_notional_constant_product(buy_state, sell_state, fee_bps)
    if raw <= 0:
        return 0.0

    # Scale the 1%-impact depth linearly to the configured tolerance.
    # 1% = 100 bps, so tolerance/100 is the right scaling factor.
    tol_scale = slippage_tolerance_bps / 100.0
    buy_depth_at_tol  = buy_state.depth_usd_1pct  * tol_scale
    sell_depth_at_tol = sell_state.depth_usd_1pct * tol_scale

    return max(0.0, min(
        raw,
        buy_state.depth_usd_1pct,
        sell_state.depth_usd_1pct,
        buy_depth_at_tol,
        sell_depth_at_tol,
        max_position_usd,
    ))


def estimated_two_leg_slippage_bps(
    notional_usd: float,
    buy_state:    PoolState,
    sell_state:   PoolState,
) -> float:
    """Depth-aware slippage estimate across both legs in bps.

    Approximation: on x*y=k a trade of `n` USDC against quote depth Q at
    1% impact moves price by ~ (n/Q) * 100 bps per leg. Two legs add.
    Clamped to 0 below to avoid negative slip from rounding; no upper
    clamp — if you're trading more than the pool can support the gate
    above (capped_optimal_notional) has already failed.
    """
    if notional_usd <= 0:
        return 0.0
    slip = 0.0
    for state in (buy_state, sell_state):
        depth = state.depth_usd_1pct
        if depth <= 0:
            # No depth info = treat the whole notional as 1% slip (very
            # conservative; would_entry will reject if it matters).
            slip += 100.0
            continue
        slip += (notional_usd / depth) * 100.0
    return max(0.0, slip)


# ─────────────────────────────────────────────────────────────────────────
# CrossChainArbEngine
# ─────────────────────────────────────────────────────────────────────────

class CrossChainArbEngine:
    """The scan loop, edge math, circuit breakers, and observation logger.

    Construct with no args to use REGISTERED_CONNECTORS. Pass
    ``connectors=[...]`` in tests with fake connectors that return
    pre-baked PoolState objects.
    """

    def __init__(
        self,
        connectors: Optional[list[BaseChainConnector]] = None,
        *,
        sim_mode: Optional[bool] = None,
    ):
        self.sim_mode = settings.SIM_MODE if sim_mode is None else sim_mode

        # Only keep available connectors. Plugin Pattern Rule: unavailable
        # plugins are skipped silently, not by raising. is_available()
        # gates on env vars + web3 importability + pinned pool addresses.
        all_conns = connectors if connectors is not None else REGISTERED_CONNECTORS
        self.connectors: list[BaseChainConnector] = [
            c for c in all_conns if c.is_available()
        ]

        # Lifecycle
        self._running:    bool = False
        self._status:     str  = STATUS_OFFLINE
        self._scan_task               = None
        self._reset_task              = None

        # Circuit breakers — independent of arb_engine + portfolio breakers
        self.cb = CircuitBreakerState()

        # Concurrency primitives: per-symbol lock (no overlapping scans on
        # the same pair) + semaphore cap on concurrent symbol scans.
        self._symbol_locks: dict[str, asyncio.Lock] = {
            sym: asyncio.Lock() for sym in settings.XCHAIN_SYMBOLS
        }
        self._semaphore = asyncio.Semaphore(settings.XCHAIN_MAX_CONCURRENT)

        # Observation counter — surfaced via get_stats() for the dashboard.
        self._evaluations_today: int = 0
        self._would_entries_today: int = 0
        self._last_scan_time:     Optional[datetime] = None

        # Live deployable capital — initialised from XCHAIN_CAPITAL so a
        # fresh launch sizes off the configured pool, and made settable via
        # set_capital_allocation so the % daily-loss breaker reads a LIVE
        # value rather than a frozen constant. NOTE: unlike ArbEngine, this
        # is NOT a CEX fund — it represents on-chain inventory across L2s.
        # No InventoryState CEX fund claim is registered for it; the
        # CEX rebalance loop never sees this capital. Compounding /
        # rebalancing of cross-chain inventory belongs to the future
        # CrossChainTransferRail, which will consume get_inventory_targets()
        # (see agents/crosschain_agent.py:get_inventory_targets) — that is
        # the seam where xchain rebalancing will land.
        self._capital_allocation: float = float(
            getattr(settings, "XCHAIN_CAPITAL", 0.0) or 0.0
        )

        # Web UI v3.1 — operator-initiated halt mirror. CrossChainArbAgent
        # writes this when the operator toggles per-agent halt; the scan
        # loop skips evaluation passes while set. Observation mode means
        # there are no positions to manage — halt affects evaluation only.
        self._manually_halted: bool = False

    # ── Public API ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Boot scan + daily-reset loops. Returns when stop() flips _running.

        Refuses to come online when fewer than 2 connectors are available —
        cross-chain arb needs two sides of a quote, by definition.
        """
        if len(self.connectors) < 2:
            logger.warning(
                "CrossChainArbEngine: need >=2 connectors, have %d — staying OFFLINE",
                len(self.connectors),
            )
            self._status = STATUS_OFFLINE
            return
        self._running = True
        self._status  = STATUS_RUNNING
        logger.info(
            "CrossChainArbEngine: %d connectors ready (%s) — %s mode",
            len(self.connectors),
            ", ".join(c.connector_id for c in self.connectors),
            "OBSERVATION" if self._capital_allocation == 0 else "EXECUTION-PENDING",
        )
        self._scan_task  = asyncio.create_task(self._scan_loop())
        self._reset_task = asyncio.create_task(self._daily_reset_loop())
        await asyncio.gather(self._scan_task, self._reset_task,
                             return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        for task in (self._scan_task, self._reset_task):
            if task is not None and not task.done():
                task.cancel()
        self._status = STATUS_STOPPED

    async def close_all_positions(self) -> None:
        """No-op in observation mode (no positions exist).

        Kept on the engine surface so the live build (separate PR) wires
        straight into the agent's close_all_positions kill-switch path.
        """
        try:
            db_queries.log_circuit_breaker(
                reason="xchain_kill",
                detail=(f"observation_mode={self._capital_allocation == 0}, "
                        f"daily_pnl=${self.cb.daily_pnl_usd:.2f}"),
            )
        except Exception:
            pass
        self._status = STATUS_STOPPED

    def get_stats(self) -> dict:
        """Snapshot the agent translates to AgentStats. Mirrors
        ArbEngine.get_stats() shape so the dashboard wiring doesn't
        need an xchain-specific branch."""
        return {
            "status":              self._status,
            "connectors":          [c.connector_id for c in self.connectors],
            "daily_pnl":           self.cb.daily_pnl_usd,
            "consecutive_losses":  self.cb.consecutive_losses,
            "halted":              self.cb.halted,
            "halt_reason":         self.cb.halt_reason,
            "evaluations_today":   self._evaluations_today,
            "would_entries_today": self._would_entries_today,
            "last_scan_time": (
                self._last_scan_time.isoformat() if self._last_scan_time else None
            ),
            "capital_allocation":  self._capital_allocation,
        }

    def set_capital_allocation(self, amount: float) -> None:
        """Update the engine's live deployable allocation.

        Cross-chain capital is on-chain inventory — this setter intentionally
        does NOT register a CEX fund claim in InventoryState (the BalanceAgent's
        CEX rebalance loop would double-count it). It only updates the breaker's
        denominator and the observation-mode label. xchain rebalancing /
        compounding belongs to the future CrossChainTransferRail (which will
        consume agents.crosschain_agent.CrossChainArbAgent.get_inventory_targets).
        """
        try:
            self._capital_allocation = max(0.0, float(amount))
        except (TypeError, ValueError):
            logger.debug(
                "CrossChainArbEngine: set_capital_allocation ignored "
                "non-numeric %r", amount,
            )

    # ── Scan loop ───────────────────────────────────────────────────────

    async def _scan_loop(self) -> None:
        interval = max(0.05, settings.XCHAIN_SCAN_INTERVAL_MS / 1000.0)
        while self._running:
            try:
                if self._cb_triggered():
                    if self._status != STATUS_HALTED:
                        logger.warning(
                            "CrossChainArbEngine: circuit breaker triggered (%s) — HALTED",
                            self.cb.halt_reason,
                        )
                    self._status = STATUS_HALTED
                    await asyncio.sleep(interval)
                    continue

                # Web UI v3.1 — operator halt skips per-symbol evaluation.
                # No positions to manage in observation mode, so the whole
                # scan body is the entry / evaluation path.
                if self._manually_halted:
                    await asyncio.sleep(interval)
                    continue

                # Per-symbol scans run under the semaphore — symbols don't
                # share a lock, so two symbols can be in flight at once if
                # XCHAIN_MAX_CONCURRENT > 1.
                tasks = [
                    asyncio.create_task(self._scan_symbol(sym))
                    for sym in settings.XCHAIN_SYMBOLS
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
                self._last_scan_time = datetime.utcnow()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("xchain scan loop: %s", e, exc_info=True)
            await asyncio.sleep(interval)

    async def _scan_symbol(self, symbol: str) -> None:
        lock = self._symbol_locks.get(symbol)
        if lock is None or lock.locked():
            # Another scan is in flight for this pair — skip this tick.
            return
        async with lock, self._semaphore:
            await self._evaluate_symbol(symbol)

    async def _evaluate_symbol(self, symbol: str) -> None:
        """Fetch every connector's pool state concurrently, then evaluate
        every ordered (buy, sell) pair. Each evaluation writes one row to
        xchain_observations (entry OR skip) — mirrors the scalp discipline."""
        states = await self._fetch_states(symbol)
        if len(states) < 2:
            return

        # gas_cost_usd is per-connector and is constant within a single
        # scan, so we fetch it once per chain. Returns inf on failure —
        # the breakeven helper propagates that to "no notional satisfies".
        gas_costs = await self._fetch_gas_costs()

        for buy in states:
            if buy.error is not None:
                continue
            for sell in states:
                if sell is buy or sell.error is not None:
                    continue
                evaluation = self._evaluate_pair(
                    symbol=symbol, buy=buy, sell=sell, gas_costs=gas_costs,
                )
                if evaluation is None:
                    continue
                self._persist(evaluation)
                self._evaluations_today += 1
                if evaluation.would_entry:
                    self._would_entries_today += 1

    async def _fetch_states(self, symbol: str) -> list[PoolState]:
        coros = [c.get_pool_state(symbol) for c in self.connectors]
        states = await asyncio.gather(*coros, return_exceptions=True)
        out: list[PoolState] = []
        for s in states:
            if isinstance(s, Exception):
                logger.debug("get_pool_state raised: %s", s)
                continue
            out.append(s)
        return out

    async def _fetch_gas_costs(self) -> dict[str, float]:
        coros = [c.gas_cost_usd() for c in self.connectors]
        results = await asyncio.gather(*coros, return_exceptions=True)
        out: dict[str, float] = {}
        for c, r in zip(self.connectors, results):
            if isinstance(r, Exception):
                logger.debug("[%s] gas_cost_usd raised: %s", c.connector_id, r)
                out[c.connector_id] = float("inf")
            else:
                out[c.connector_id] = float(r)
        return out

    # ── Edge math (the heart) ──────────────────────────────────────────

    def _evaluate_pair(
        self,
        *,
        symbol:    str,
        buy:       PoolState,
        sell:      PoolState,
        gas_costs: dict[str, float],
    ) -> Optional[XChainEvaluation]:
        """Compute net_edge_bps for one ordered (buy, sell) pair and decide
        would_entry. ALWAYS returns an XChainEvaluation when both sides have
        a positive spot — even a skip is observable. Returns None only when
        the math can't be computed at all (zero spot on either side).
        """
        if buy.spot_price <= 0 or sell.spot_price <= 0:
            return None

        # Only positively-signed spreads are candidates — the engine
        # evaluates both orderings of every chain pair (b,s) and (s,b)
        # in the caller, so we never miss a direction.
        if sell.spot_price <= buy.spot_price:
            return None

        spread_bps = (sell.spot_price - buy.spot_price) / buy.spot_price * 1e4

        b_gas = gas_costs.get(buy.connector_id,  float("inf"))
        s_gas = gas_costs.get(sell.connector_id, float("inf"))

        # Fee is the sum of both pools' actual on-chain fee tiers. The
        # math NEVER trusts the documented tier — fee_bps comes from
        # PoolState which is sampled live.
        rt_fee_bps = buy.fee_bps + sell.fee_bps

        # Optimal notional under capped slippage tolerance + depth limits.
        notional_usd = capped_optimal_notional(
            buy, sell,
            fee_bps=rt_fee_bps,
            slippage_tolerance_bps=settings.XCHAIN_SLIPPAGE_TOLERANCE_BPS,
            max_position_usd=settings.XCHAIN_MAX_POSITION_USD,
        )

        # gas_bps = round-trip gas USD / notional USD * 1e4. With zero
        # notional this is inf — the would_entry check below short-circuits.
        gas_breakeven = gas_breakeven_usd(
            b_gas, s_gas, settings.XCHAIN_GAS_BUDGET_BPS,
        )
        if notional_usd <= 0:
            gas_bps = float("inf")
        elif not math.isfinite(b_gas + s_gas):
            gas_bps = float("inf")
        else:
            gas_bps = (b_gas + s_gas) / notional_usd * 1e4

        slip_bps   = estimated_two_leg_slippage_bps(notional_usd, buy, sell)
        bridge_bps = 0.0      # inventory pre-positioned; never bridge mid-trade

        # Net edge with safe arithmetic when gas_bps is inf
        if math.isfinite(gas_bps):
            net_edge_bps = spread_bps - rt_fee_bps - gas_bps - bridge_bps - slip_bps
        else:
            net_edge_bps = -float("inf")

        # Skip reason resolution — first failing gate wins so the operator
        # sees the dominant cost. Order: notional floor, persistence, min edge.
        skip_reason = ""
        would_entry = False

        if notional_usd <= 0:
            skip_reason = "notional_zero_post_caps"
        elif notional_usd < gas_breakeven:
            skip_reason = (
                f"below_gas_breakeven (notional=${notional_usd:.0f} "
                f"< floor=${gas_breakeven:.0f})"
            )
        elif not self._block_is_fresh(buy.block_number, sell.block_number):
            skip_reason = (
                f"stale_block (buy={buy.block_number}, sell={sell.block_number})"
            )
        elif net_edge_bps <= settings.XCHAIN_MIN_NET_EDGE_BPS:
            skip_reason = (
                f"below_min_edge (net={net_edge_bps:.2f}bps "
                f"<= {settings.XCHAIN_MIN_NET_EDGE_BPS})"
            )
        else:
            would_entry = True

        return XChainEvaluation(
            symbol=symbol,
            buy_chain=buy.connector_id,
            sell_chain=sell.connector_id,
            buy_venue=buy.venue,
            sell_venue=sell.venue,
            notional_usd=notional_usd,
            spread_bps=spread_bps,
            rt_fee_bps=rt_fee_bps,
            gas_bps=gas_bps if math.isfinite(gas_bps) else -1.0,
            slip_bps=slip_bps,
            bridge_bps=bridge_bps,
            net_edge_bps=net_edge_bps if math.isfinite(net_edge_bps) else -1.0,
            gas_breakeven_usd=gas_breakeven if math.isfinite(gas_breakeven) else -1.0,
            would_entry=would_entry,
            skip_reason=skip_reason,
            buy_block=buy.block_number,
            sell_block=sell.block_number,
        )

    @staticmethod
    def _block_is_fresh(buy_block: int, sell_block: int) -> bool:
        """Both sides must have a non-zero block and the gap between them
        must be within XCHAIN_MAX_BLOCK_STALENESS blocks (an L2 opportunity
        that hasn't refreshed within our scan window is not actionable —
        Gogol et al. 2024)."""
        if buy_block <= 0 or sell_block <= 0:
            return False
        return abs(buy_block - sell_block) <= settings.XCHAIN_MAX_BLOCK_STALENESS

    # ── Persistence ─────────────────────────────────────────────────────

    def _persist(self, e: XChainEvaluation) -> None:
        """Write one xchain_observations row. Never let a DB hiccup take
        down the scan loop — mirrors arb_engine._log_to_db discipline.

        observation_only is True whenever the engine's live capital allocation
        is 0 (the live build will pass False when an actual position is opened).
        Reads _capital_allocation rather than the frozen settings constant so a
        runtime allocation change flips the persisted flag accurately.
        """
        try:
            db_queries.insert_xchain_observation(
                symbol=e.symbol,
                buy_chain=e.buy_chain,
                sell_chain=e.sell_chain,
                buy_venue=e.buy_venue,
                sell_venue=e.sell_venue,
                notional_usd=e.notional_usd,
                spread_bps=e.spread_bps,
                rt_fee_bps=e.rt_fee_bps,
                gas_bps=e.gas_bps,
                slip_bps=e.slip_bps,
                bridge_bps=e.bridge_bps,
                net_edge_bps=e.net_edge_bps,
                gas_breakeven_usd=e.gas_breakeven_usd,
                would_entry=e.would_entry,
                skip_reason=e.skip_reason,
                observation_only=(self._capital_allocation == 0),
            )
        except Exception as exc:
            logger.debug("insert_xchain_observation: %s", exc)

    # ── Circuit breakers ────────────────────────────────────────────────

    def _cb_triggered(self) -> bool:
        if self.cb.halted:
            return True
        # %-based: scale the daily-loss halt to the LIVE allocation so a
        # runtime change (set_capital_allocation) takes effect immediately,
        # and a compounding fund doesn't tighten its leash silently.
        # Observation mode (alloc=0) → halt is a no-op on this rule.
        alloc = float(self._capital_allocation or 0.0)
        if alloc > 0:
            halt_usd = (float(settings.XCHAIN_DAILY_LOSS_HALT_PCT) / 100.0) * alloc
            if self.cb.daily_pnl_usd <= -halt_usd:
                self.cb.halted = True
                self.cb.halt_reason = (
                    f"daily_loss_halt (${self.cb.daily_pnl_usd:.2f} <= "
                    f"-${halt_usd:.2f}; "
                    f"{settings.XCHAIN_DAILY_LOSS_HALT_PCT:.1f}% of ${alloc:.0f})"
                )
                return True
        if self.cb.consecutive_losses >= settings.XCHAIN_CONSECUTIVE_LOSS_HALT:
            self.cb.halted = True
            self.cb.halt_reason = (
                f"consecutive_loss_halt ({self.cb.consecutive_losses} >= "
                f"{settings.XCHAIN_CONSECUTIVE_LOSS_HALT})"
            )
            return True
        return False

    async def _daily_reset_loop(self) -> None:
        """Sleep until UTC midnight; reset daily P&L + halt flag; repeat.

        Same shape as arb_engine._daily_reset_loop. consecutive_losses is
        NOT reset at midnight — that one only clears on a winning trade,
        so an all-loss day rolls its streak into the next day intentionally.
        """
        while self._running:
            now = datetime.utcnow()
            tomorrow_midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            try:
                await asyncio.sleep((tomorrow_midnight - now).total_seconds())
            except asyncio.CancelledError:
                raise
            self.cb.daily_pnl_usd = 0.0
            self.cb.halted        = False
            self.cb.halt_reason   = ""
            self._evaluations_today   = 0
            self._would_entries_today = 0
            logger.info("CrossChainArbEngine: daily P&L reset at UTC midnight")
