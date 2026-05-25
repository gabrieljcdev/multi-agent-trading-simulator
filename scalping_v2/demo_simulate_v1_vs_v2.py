"""
Scalping v1 vs v2 — simulated comparison.

Generates synthetic OFI signals with realistic noise structure, runs them
through v1 (z>=1.5, basic gates) and v2 (z>=2.0, confluence, adverse,
cross-exchange) logic, and reports the win-rate / trade-count tradeoff.

The simulation is illustrative, not predictive. It encodes assumptions:
  - Real OFI signals with z>=2.0 have ~60% directional accuracy at 1m
  - Weaker signals (1.5<=z<2.0) have ~50% accuracy
  - Cross-exchange agreement bumps accuracy by +5pp (independent venues confirming)
  - VWAP/HTF alignment bumps accuracy by +3pp each
  - Adverse-selection trades have accuracy ~40% (we are the dumb money)
  - BTC misalignment for alts halves the alt's accuracy edge

These assumptions are reasonable approximations of the literature findings
(Cont 2014, Brogaard 2014, Globe 2020) but are NOT calibrated to your specific
agent's observations. Once you have 200+ real obs, recalibrate this script
against the actual conditional accuracy rates by querying scalp_observations.

Run:  python demo_simulate_v1_vs_v2.py
"""

import random
import statistics
from dataclasses import dataclass, field
from typing import List, Optional
from collections import Counter

from scalping_confluence import ConfluenceChecker
from scalping_atr_sl import ATRStopCalculator
import settings_scalp_v2 as v2


# =============================================================================
# Synthetic world generator
# =============================================================================

@dataclass
class WorldState:
    """A single moment in time on a single (symbol, exchange) pair."""
    symbol: str
    exchange: str
    direction: str       # LONG / SHORT
    ofi_z: float
    mid: float
    vwap: float
    ema_5m_fast: float
    ema_5m_slow: float
    current_vol: float
    median_vol: float
    btc_ofi_z: float
    cross_ofi_z: dict    # other_exchange -> z
    mid_then_100ms: float
    atr: float
    top1_size_usd: float
    top5_size_usd: float
    # ground truth — the signal's actual directional outcome
    true_outcome: str    # "WIN" / "LOSS"


def generate_signal(rng: random.Random) -> WorldState:
    """Generate one synthetic OFI signal with realistic structure."""

    symbol = rng.choices(["BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT", "XRP/USDT"],
                         weights=[4, 3, 2, 1, 1])[0]
    exchange = "MEXC"
    direction = rng.choice(["LONG", "SHORT"])

    # OFI z-score — most signals are 1.5-2.0 (marginal), fewer at extremes
    z_raw = rng.gauss(0, 1)
    ofi_z = 1.5 + abs(z_raw)  # at least 1.5 (otherwise gate 5 blocks)
    if rng.random() < 0.15:
        ofi_z = rng.uniform(2.5, 4.0)  # 15% strong signals

    # The agent represents direction explicitly; sign of OFI matches it
    signed_z = ofi_z if direction == "LONG" else -ofi_z

    # Mid price
    mid = {"BTC/USDT": 50000, "ETH/USDT": 3000, "SOL/USDT": 150,
           "DOGE/USDT": 0.12, "XRP/USDT": 0.5}[symbol]
    mid *= rng.uniform(0.98, 1.02)

    # VWAP — 60% of the time aligned with signal direction
    vwap_aligned = rng.random() < 0.60
    if direction == "LONG":
        vwap = mid * (rng.uniform(0.995, 0.9999) if vwap_aligned else rng.uniform(1.0001, 1.005))
    else:
        vwap = mid * (rng.uniform(1.0001, 1.005) if vwap_aligned else rng.uniform(0.995, 0.9999))

    # HTF EMAs — 55% aligned with signal
    htf_aligned = rng.random() < 0.55
    if (direction == "LONG" and htf_aligned) or (direction == "SHORT" and not htf_aligned):
        ema_fast, ema_slow = mid * 1.001, mid * 0.999
    else:
        ema_fast, ema_slow = mid * 0.999, mid * 1.001

    # Volume — 60% above median
    if rng.random() < 0.60:
        current_vol, median_vol = rng.uniform(1100, 2500), 1000.0
    else:
        current_vol, median_vol = rng.uniform(400, 950), 1000.0

    # Cross-exchange OFI — partial correlation with primary
    cross_ofi = {}
    if symbol == "BTC/USDT" or symbol == "ETH/USDT":
        # liquid pairs: another venue available
        agree_prob = 0.55 if ofi_z >= 2.0 else 0.40
        if rng.random() < agree_prob:
            cross_ofi["BITGET"] = signed_z * rng.uniform(0.4, 1.0)
        elif rng.random() < 0.20:
            cross_ofi["BITGET"] = -signed_z * rng.uniform(0.3, 0.8)
        else:
            cross_ofi["BITGET"] = rng.uniform(-0.3, 0.3)

    # BTC OFI — for alts, 40% chance of misalignment
    if symbol != "BTC/USDT":
        if rng.random() < 0.40:
            btc_ofi = -signed_z * rng.uniform(0.5, 1.5) / abs(signed_z) * 0.8
        else:
            btc_ofi = rng.uniform(-0.4, 0.4)
            if rng.random() < 0.5:
                btc_ofi = signed_z * rng.uniform(0.3, 1.0) / abs(signed_z) * 0.7
    else:
        btc_ofi = signed_z * 0.5

    # Adverse selection — 20% chance the book moved against us in last 100ms
    if rng.random() < 0.20:
        adverse_bps = rng.uniform(1.5, 4.0)
        if direction == "LONG":
            mid_then = mid * (1 + adverse_bps / 10000)  # mid HIGHER 100ms ago = dropped
        else:
            mid_then = mid * (1 - adverse_bps / 10000)
    else:
        mid_then = mid * (1 + rng.uniform(-0.5, 0.5) / 10000)

    # ATR — varies by symbol volatility
    atr_pct = {"BTC/USDT": 0.0010, "ETH/USDT": 0.0015, "SOL/USDT": 0.0025,
               "DOGE/USDT": 0.0040, "XRP/USDT": 0.0030}[symbol]
    atr = mid * atr_pct * rng.uniform(0.7, 1.4)

    # Book depth — some pairs are thin
    if symbol in ("DOGE/USDT", "XRP/USDT"):
        top1 = rng.uniform(500, 3000)
        top5 = top1 * rng.uniform(3, 6)
    else:
        top1 = rng.uniform(20_000, 200_000)
        top5 = top1 * rng.uniform(4, 8)

    return WorldState(
        symbol=symbol, exchange=exchange, direction=direction,
        ofi_z=ofi_z, mid=mid, vwap=vwap,
        ema_5m_fast=ema_fast, ema_5m_slow=ema_slow,
        current_vol=current_vol, median_vol=median_vol,
        btc_ofi_z=btc_ofi, cross_ofi_z=cross_ofi,
        mid_then_100ms=mid_then, atr=atr,
        top1_size_usd=top1, top5_size_usd=top5,
        true_outcome=_compute_truth(direction, ofi_z, vwap, mid, ema_fast, ema_slow,
                                    current_vol, median_vol, cross_ofi, btc_ofi,
                                    symbol, mid_then, rng),
    )


def _compute_truth(direction, ofi_z, vwap, mid, ema_fast, ema_slow, current_vol,
                   median_vol, cross_ofi, btc_ofi, symbol, mid_then, rng) -> str:
    """Ground-truth outcome generator — captures the signal-quality assumptions."""

    # Base accuracy from OFI strength
    if ofi_z >= 2.5:
        p_win = 0.62
    elif ofi_z >= 2.0:
        p_win = 0.58
    elif ofi_z >= 1.7:
        p_win = 0.53
    else:
        p_win = 0.50

    # Confluence bumps
    if direction == "LONG":
        if mid > vwap:
            p_win += 0.025
        if ema_fast > ema_slow:
            p_win += 0.025
    else:
        if mid < vwap:
            p_win += 0.025
        if ema_fast < ema_slow:
            p_win += 0.025

    if current_vol >= median_vol:
        p_win += 0.02

    # Cross-exchange agreement
    if cross_ofi:
        other_z = list(cross_ofi.values())[0]
        if direction == "LONG" and other_z > 0.5:
            p_win += 0.04
        elif direction == "SHORT" and other_z < -0.5:
            p_win += 0.04
        elif direction == "LONG" and other_z < -0.5:
            p_win -= 0.06
        elif direction == "SHORT" and other_z > 0.5:
            p_win -= 0.06

    # BTC misalignment penalty for alts
    if symbol != "BTC/USDT":
        if direction == "LONG" and btc_ofi < -0.5:
            p_win -= 0.08
        elif direction == "SHORT" and btc_ofi > 0.5:
            p_win -= 0.08

    # Adverse selection — strong negative
    move_bps = ((mid - mid_then) / mid_then) * 10000
    if direction == "LONG" and move_bps < -1.0:
        p_win -= 0.12
    elif direction == "SHORT" and move_bps > 1.0:
        p_win -= 0.12

    p_win = max(0.30, min(0.80, p_win))
    return "WIN" if rng.random() < p_win else "LOSS"


# =============================================================================
# v1 / v2 evaluators
# =============================================================================

class MockMarketDataAdapter:
    """Adapter that exposes WorldState to v2 components via the expected API."""
    def __init__(self, world: WorldState):
        self.w = world

    def get_mid_price(self, s, e): return self.w.mid
    def get_mid_price_at_offset(self, s, e, offset_ms): return self.w.mid_then_100ms
    def get_session_vwap(self, s, e): return self.w.vwap
    def get_ema(self, s, e, tf, p): return self.w.ema_5m_fast if p == 8 else self.w.ema_5m_slow
    def get_current_minute_volume(self, s, e): return self.w.current_vol
    def get_rolling_median_volume(self, s, e, tf, lb): return self.w.median_vol
    def get_atr(self, s, e, period, timeframe): return self.w.atr

    def get_order_book(self, s, e, levels=5):
        from test_scalping_v2 import FakeBook, FakeLevel
        # Synthesise a book consistent with top1/top5 USD totals
        bid_price = self.w.mid * 0.99995
        ask_price = self.w.mid * 1.00005
        bid_size = self.w.top1_size_usd / bid_price
        ask_size = self.w.top1_size_usd / ask_price
        deeper_total_bid = (self.w.top5_size_usd - self.w.top1_size_usd) / 4
        deeper_total_ask = (self.w.top5_size_usd - self.w.top1_size_usd) / 4
        bids = [FakeLevel(bid_price, bid_size)]
        asks = [FakeLevel(ask_price, ask_size)]
        for i in range(4):
            bp = bid_price * (1 - 0.0001 * (i+1))
            ap = ask_price * (1 + 0.0001 * (i+1))
            bids.append(FakeLevel(bp, deeper_total_bid / bp))
            asks.append(FakeLevel(ap, deeper_total_ask / ap))
        return FakeBook(bids=bids, asks=asks)


class MockOFIAdapter:
    """Adapter that exposes WorldState's cross-exchange z-scores."""
    def __init__(self, world: WorldState):
        self.w = world

    def get_z_score(self, symbol, exchange):
        if symbol == "BTC/USDT":
            return self.w.btc_ofi_z
        if exchange == self.w.exchange and symbol == self.w.symbol:
            return self.w.ofi_z if self.w.direction == "LONG" else -self.w.ofi_z
        return self.w.cross_ofi_z.get(exchange)

    def get_exchanges_for_symbol(self, symbol):
        if symbol == self.w.symbol:
            return [self.w.exchange] + list(self.w.cross_ofi_z.keys())
        return ["MEXC"]


def evaluate_v1(world: WorldState) -> dict:
    """Original gates: z>=1.5, persist (assumed met), regime/session ok,
    no confluence, no adverse-selection guard, no cross-exchange check."""
    out = {"entered": False, "skip_reason": "", "outcome": None}

    # Gate 5: z >= 1.5
    if world.ofi_z < 1.5:
        out["skip_reason"] = "OFI too weak"
        return out

    # Gate 13: BTC moving > 0.3% in 1m — only block extreme cases (proxy)
    if abs(world.btc_ofi_z) > 3.0 and world.symbol != "BTC/USDT":
        out["skip_reason"] = "BTC moving fast"
        return out

    out["entered"] = True
    out["outcome"] = world.true_outcome
    return out


def evaluate_v2(world: WorldState, checker: ConfluenceChecker) -> dict:
    """v2 logic: tighter z, confluence, adverse, cross-exchange, BTC, depth."""
    out = {"entered": False, "skip_reason": "", "outcome": None,
           "confluence_score": 0, "strength": "WEAK"}

    # Gate 5 (v2): z >= 2.0
    if world.ofi_z < 2.0:
        out["skip_reason"] = "V2:z<2.0"
        return out

    position_size_usd = 50.0
    result = checker.run_all_gates(
        symbol=world.symbol, exchange=world.exchange,
        direction=world.direction, primary_z=world.ofi_z,
        position_size_usd=position_size_usd,
    )

    out["confluence_score"] = result.confluence_score
    out["strength"] = result.strength_label

    if not result.passed:
        out["skip_reason"] = f"V2:{result.blocking_reason[:60]}"
        return out

    out["entered"] = True
    out["outcome"] = world.true_outcome
    return out


# =============================================================================
# Simulation runner
# =============================================================================

def run_simulation(n_signals: int = 10000, seed: int = 42) -> dict:
    rng = random.Random(seed)
    settings = v2.default_v2_settings()

    results = {
        "v1": {"trades": 0, "wins": 0, "skip_reasons": Counter()},
        "v2": {"trades": 0, "wins": 0, "skip_reasons": Counter(),
               "by_strength": {"WEAK": [0, 0], "MODERATE": [0, 0],
                               "STRONG": [0, 0], "VERY_STRONG": [0, 0]}},
    }

    for i in range(n_signals):
        world = generate_signal(rng)

        md_adapter = MockMarketDataAdapter(world)
        ofi_adapter = MockOFIAdapter(world)
        checker = ConfluenceChecker(md_adapter, ofi_adapter, settings)

        v1_result = evaluate_v1(world)
        v2_result = evaluate_v2(world, checker)

        if v1_result["entered"]:
            results["v1"]["trades"] += 1
            if v1_result["outcome"] == "WIN":
                results["v1"]["wins"] += 1
        elif v1_result["skip_reason"]:
            results["v1"]["skip_reasons"][v1_result["skip_reason"]] += 1

        if v2_result["entered"]:
            results["v2"]["trades"] += 1
            if v2_result["outcome"] == "WIN":
                results["v2"]["wins"] += 1
            strength = v2_result["strength"]
            results["v2"]["by_strength"][strength][0] += 1
            if v2_result["outcome"] == "WIN":
                results["v2"]["by_strength"][strength][1] += 1
        elif v2_result["skip_reason"]:
            # bucket by reason category
            reason = v2_result["skip_reason"]
            if "z<2.0" in reason:
                results["v2"]["skip_reasons"]["V2:z<2.0"] += 1
            elif "adverse" in reason.lower() or "dropped" in reason or "rose" in reason:
                results["v2"]["skip_reasons"]["V2:adverse"] += 1
            elif "depth" in reason.lower() or "consume" in reason.lower():
                results["v2"]["skip_reasons"]["V2:depth"] += 1
            elif "btc" in reason.lower() or "alt" in reason.lower():
                results["v2"]["skip_reasons"]["V2:btc_directional"] += 1
            elif "opposing" in reason.lower() or "venue" in reason.lower():
                results["v2"]["skip_reasons"]["V2:cross_exchange"] += 1
            elif "confluence" in reason.lower():
                results["v2"]["skip_reasons"]["V2:confluence"] += 1
            else:
                results["v2"]["skip_reasons"]["V2:other"] += 1

    return results


def print_report(results: dict, n_signals: int):
    r_v1 = results["v1"]
    r_v2 = results["v2"]

    def wr(d):
        return d["wins"] / d["trades"] if d["trades"] > 0 else 0.0

    v1_wr = wr(r_v1)
    v2_wr = wr(r_v2)

    # Expectancy with 1.6 RR — TP=3 bps, SL=1.875 bps
    def exp_bps(win_rate, tp=3.0, sl=1.875):
        return win_rate * tp - (1 - win_rate) * sl

    print("=" * 72)
    print(f"SCALPING AGENT — v1 vs v2 SIMULATION ({n_signals:,} synthetic signals)")
    print("=" * 72)

    print(f"\n{'METRIC':<35}{'v1':>15}{'v2':>15}{'Δ':>10}")
    print("-" * 72)
    print(f"{'Trades entered':<35}{r_v1['trades']:>15,}{r_v2['trades']:>15,}"
          f"{r_v2['trades']-r_v1['trades']:>+10,}")
    print(f"{'Win rate':<35}{v1_wr*100:>14.2f}%{v2_wr*100:>14.2f}%"
          f"{(v2_wr-v1_wr)*100:>+9.2f}pp")
    print(f"{'Expectancy (bps/trade)':<35}{exp_bps(v1_wr):>15.3f}{exp_bps(v2_wr):>15.3f}"
          f"{exp_bps(v2_wr)-exp_bps(v1_wr):>+10.3f}")

    daily_v1 = exp_bps(v1_wr) * r_v1['trades'] / n_signals * 100
    daily_v2 = exp_bps(v2_wr) * r_v2['trades'] / n_signals * 100
    print(f"{'Per-1k-signals net bps':<35}{daily_v1:>15.1f}{daily_v2:>15.1f}"
          f"{daily_v2-daily_v1:>+10.1f}")

    print(f"\n{'v1 SKIP REASONS':<45}{'count':>10}{'pct':>10}")
    print("-" * 72)
    total_v1_skips = sum(r_v1["skip_reasons"].values())
    if total_v1_skips == 0:
        print("  (none — v1 takes every signal that passes basic gates)")
    else:
        for reason, count in r_v1["skip_reasons"].most_common():
            pct = count / total_v1_skips * 100
            print(f"{reason:<45}{count:>10,}{pct:>9.1f}%")

    print(f"\n{'v2 SKIP REASONS':<45}{'count':>10}{'pct':>10}")
    print("-" * 72)
    total_v2_skips = sum(r_v2["skip_reasons"].values())
    for reason, count in r_v2["skip_reasons"].most_common():
        pct = count / total_v2_skips * 100 if total_v2_skips else 0
        print(f"{reason:<45}{count:>10,}{pct:>9.1f}%")

    print(f"\n{'v2 PERFORMANCE BY STRENGTH LABEL':<35}{'trades':>10}{'wins':>8}{'wr':>10}")
    print("-" * 72)
    for label in ["VERY_STRONG", "STRONG", "MODERATE", "WEAK"]:
        t, w = r_v2["by_strength"][label]
        rate = (w / t * 100) if t > 0 else 0
        print(f"  {label:<33}{t:>10,}{w:>8,}{rate:>9.2f}%")

    print(f"\n{'CONSISTENCY METRICS':<72}")
    print("-" * 72)
    print(f"  Variance reduction: v2 trades {(1-r_v2['trades']/r_v1['trades'])*100:.1f}% fewer signals")
    print(f"  Per-trade reliability: v2 wins {(v2_wr-v1_wr)*100:+.1f}pp more often")
    print(f"  Per-trade profit: v2 earns {(exp_bps(v2_wr)-exp_bps(v1_wr)):+.3f} more bps per trade")
    print(f"  Drawdown risk: v2 loss-streak probability ≈ {(1-v2_wr)**5*100:.2f}% for 5-loss run")
    print(f"                 vs v1                       ≈ {(1-v1_wr)**5*100:.2f}% for 5-loss run")

    print(f"\n{'V2 ACTIVATION READINESS CHECK':<72}")
    print("-" * 72)
    settings = v2.default_v2_settings()
    v2_stats = {
        "n_closed": r_v2["trades"],
        "win_rate": v2_wr,
        "avg_net_bps": exp_bps(v2_wr),
        "max_hold_pct": 0.20,
        "directional_accuracy_1m": v2_wr,
    }
    from scalping_agent_v2_integration import is_ready_for_live_v2
    ready = is_ready_for_live_v2(v2_stats, settings)
    if ready["ready"]:
        print("  STATUS: READY FOR LIVE (all v2 activation criteria met)")
    else:
        print("  STATUS: NOT READY")
        for reason in ready["reasons_failing"]:
            print(f"    - {reason}")

    print("=" * 72)


if __name__ == "__main__":
    n = 20000
    results = run_simulation(n_signals=n, seed=42)
    print_report(results, n)
