"""
execution/inventory.py

Inventory targets for the cross-chain arb agent — the "publish target,
let the BalanceAgent move USDC" interface.

The cross-chain agent is non-atomic and inventory-pre-positioned (see
crosschain_engine.py module docstring for the research rationale). Each
chain holds a portion of the agent's USDC + WETH inventory; opportunities
get exploited locally without bridging mid-trade. As the agent observes
where edge actually appears (logged to xchain_observations by the engine)
this module computes the target inventory split for each chain, and
publishes it. The downstream BalanceAgent will read these targets and
execute the actual rebalancing over Circle CCTP / canonical bridges.

DO NOT implement transfers here. This module is the *interface* the
BalanceAgent depends on, not the bridge layer itself. Same shape as
core/agent.py producing trade decisions that Bot._on_new_signal acts on —
producer here, consumer there.

Rule from Öz et al. 2025 (arXiv:2501.17335): rebalance when |I - I*| >
theta * I*. theta is settings.XCHAIN_INVENTORY_DRIFT_PCT (default 0.20)
— keeps the bridge fee amortised across multiple opportunities rather
than paid back on every small drift.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from config import settings


@dataclass
class InventoryTarget:
    """One chain's target inventory split for one symbol.

    USD-denominated on both sides so the dashboard / BalanceAgent can
    read targets without needing to know spot prices. drift_pct is
    computed against the TARGET (not the current) so a chain at 0 with
    target $100 reads drift=1.0 (100%) not undefined.

    needs_rebalance bakes the theta gate (XCHAIN_INVENTORY_DRIFT_PCT)
    into the dataclass so the BalanceAgent doesn't re-implement the
    check — the producer/consumer contract carries it.
    """
    connector_id:      str
    symbol:            str
    target_base_usd:   float    # desired WETH inventory in USD on this chain
    target_quote_usd:  float    # desired USDC inventory in USD on this chain
    current_base_usd:  float    # observed (in observation mode: from a ledger)
    current_quote_usd: float
    drift_pct:         float    # max(|cur-tgt|/tgt) across base + quote
    needs_rebalance:   bool     # drift_pct > XCHAIN_INVENTORY_DRIFT_PCT


def _chain_weights_from_observations(
    observations: list[dict],
    chains:       list[str],
) -> dict[str, float]:
    """Allocate weight per chain proportional to its observed would_entry
    frequency. Chains with no observed entries get a small equal-share
    floor so the BalanceAgent never fully drains an enabled chain on the
    basis of a cold-start observation window.

    `observations` is the duck-typed list returned by
    db_queries.get_xchain_observations (or any equivalent shape: each
    item is a dict with "buy_chain" / "sell_chain" / "would_entry").
    """
    n_chains = len(chains)
    if n_chains == 0:
        return {}

    # Per-chain entry counts: a chain participates when it's either side
    # of a would_entry observation (the inventory it holds matters on
    # both legs — base on the sell side, quote on the buy side).
    counts: dict[str, int] = defaultdict(int)
    for o in observations:
        if not o.get("would_entry"):
            continue
        for side in ("buy_chain", "sell_chain"):
            ch = o.get(side)
            if ch in chains:
                counts[ch] += 1

    total = sum(counts.values())
    if total == 0:
        # No observed edges anywhere — fall back to equal split. Keeps
        # the BalanceAgent able to seed inventory before any data lands.
        return {c: 1.0 / n_chains for c in chains}

    # Soft floor: every enabled chain keeps at least 1/(2N) of capital,
    # so a chain with one observed entry doesn't get crowded out by a
    # chain with fifty. Floor + scale-to-1 keeps the relative ordering
    # intact while preventing degenerate 100/0/0 splits in cold weeks.
    floor = 1.0 / (2.0 * n_chains)
    weights: dict[str, float] = {}
    for c in chains:
        share = counts.get(c, 0) / total
        weights[c] = max(floor, share)
    # Renormalise so weights sum to 1.0 after applying the floor.
    s = sum(weights.values())
    return {c: w / s for c, w in weights.items()} if s > 0 else weights


def compute_inventory_targets(
    observations:    list[dict],
    current_balances: dict[str, dict[str, float]] | None = None,
    *,
    capital_usd:     float | None = None,
    chains:          list[str] | None = None,
    symbol:          str | None  = None,
    drift_threshold: float | None = None,
) -> list[InventoryTarget]:
    """Compute one InventoryTarget per enabled chain.

    Args:
      observations: rows from get_xchain_observations. Each row needs at
        least "buy_chain", "sell_chain", "would_entry" keys.
      current_balances: ``{connector_id: {"base_usd": x, "quote_usd": y}}``.
        Defaults to all-zero (cold start) so the very first call still
        emits actionable rebalance targets.
      capital_usd: total cross-chain capital to allocate. Defaults to
        settings.XCHAIN_CAPITAL — zero in observation mode, which still
        produces InventoryTargets with target=0 + drift=0 so downstream
        wiring can be exercised end-to-end pre-go-live.
      chains: enabled chain IDs. Defaults to settings.XCHAIN_CHAINS.
      symbol: the trading pair these targets are for. Defaults to the
        first symbol in settings.XCHAIN_SYMBOLS.
      drift_threshold: theta. Defaults to XCHAIN_INVENTORY_DRIFT_PCT.

    Within each chain, target is split 50/50 between base (WETH) and quote
    (USDC) — both sides are needed (you sell WETH on the high-priced chain
    using its USDC depth and buy on the low-priced chain consuming WETH).
    A future tuning could weight by direction-specific entry frequency.
    """
    capital   = capital_usd     if capital_usd     is not None else settings.XCHAIN_CAPITAL
    chains    = chains          if chains          is not None else list(settings.XCHAIN_CHAINS)
    symbol    = symbol          if symbol          is not None else (
        settings.XCHAIN_SYMBOLS[0] if settings.XCHAIN_SYMBOLS else "WETH-USDC"
    )
    theta     = drift_threshold if drift_threshold is not None else settings.XCHAIN_INVENTORY_DRIFT_PCT
    balances  = current_balances or {}

    weights = _chain_weights_from_observations(observations, chains)
    out: list[InventoryTarget] = []

    for chain in chains:
        weight = weights.get(chain, 0.0)
        chain_capital = capital * weight
        target_base  = chain_capital * 0.5
        target_quote = chain_capital * 0.5

        bal = balances.get(chain, {})
        cur_base  = float(bal.get("base_usd",  0.0))
        cur_quote = float(bal.get("quote_usd", 0.0))

        # drift_pct: max relative deviation across base and quote. With
        # target == 0 (no capital allocated yet) we report 0 drift —
        # there's nothing to rebalance toward.
        drifts = []
        for cur, tgt in ((cur_base, target_base), (cur_quote, target_quote)):
            if tgt > 0:
                drifts.append(abs(cur - tgt) / tgt)
            elif cur > 0:
                # Holding inventory on a chain with target=0 IS a drift
                # (overfunded). Use cur as the denominator to keep the
                # ratio bounded — full overfund reads 1.0.
                drifts.append(1.0)
        drift_pct = max(drifts) if drifts else 0.0
        needs = drift_pct > theta

        out.append(InventoryTarget(
            connector_id=chain,
            symbol=symbol,
            target_base_usd=target_base,
            target_quote_usd=target_quote,
            current_base_usd=cur_base,
            current_quote_usd=cur_quote,
            drift_pct=drift_pct,
            needs_rebalance=needs,
        ))
    return out


__all__ = ["InventoryTarget", "compute_inventory_targets"]
