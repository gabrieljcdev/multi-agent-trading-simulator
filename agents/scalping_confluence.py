"""
Scalping agent v2 — selectivity / confluence gates.

These gates run AFTER the existing 13 gates in agents/scalping_agent.py.
Each returns a structured ConfluenceResult so the agent can log granular
skip reasons to the scalp_observations.skip_reason column.

Architecture: stateless functions wrapped in a class for dependency injection.
Each check fetches what it needs from market_data and ofi_engine via the
existing interfaces — no new infrastructure required.

Integration point: in _evaluate_signal(), after gate 13 (BTC 1m change),
call ConfluenceChecker.run_all_gates(symbol, exchange, direction, ofi_z, position_size_usd).
If result.passed is False, log skip_reason=result.reason and return.
If passed, proceed to FeeManager.compute_tp_sl() — now ATR-aware via
ATRStopCalculator below.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
import logging

logger = logging.getLogger("scalping_v2.confluence")


@dataclass
class ConfluenceResult:
    """Structured result from a single confluence check or the combined run."""

    passed: bool
    reason: str
    score: float = 0.0           # 0.0-1.0, contribution to combined confidence
    gate_name: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __str__(self):
        sym = "✓" if self.passed else "✗"
        return f"[{sym} {self.gate_name}] {self.reason} (score={self.score:.2f})"


@dataclass
class CombinedConfluenceResult:
    """Aggregate result over all gates — what the agent acts on."""

    passed: bool
    blocking_reason: str          # first failing critical gate, "" if passed
    confluence_score: int         # of 3 soft gates (VWAP/HTF/Volume)
    strength_label: str           # WEAK / MODERATE / STRONG / VERY_STRONG
    cross_exchange_agrees: bool
    btc_compatible: bool
    adverse_selection_ok: bool
    depth_ok: bool
    individual_results: List[ConfluenceResult] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class ConfluenceChecker:
    """Runs all v2 selectivity gates against a candidate scalp signal.

    Dependencies (duck-typed; mock for tests):
      - market_data:
          get_mid_price(symbol, exchange) -> float
          get_mid_price_at_offset(symbol, exchange, offset_ms) -> float
          get_session_vwap(symbol, exchange) -> float
          get_ema(symbol, exchange, timeframe, period) -> float
          get_current_minute_volume(symbol, exchange) -> float
          get_rolling_median_volume(symbol, exchange, timeframe, lookback) -> float
          get_order_book(symbol, exchange, levels) -> OrderBook(bids=[Level], asks=[Level])
          get_atr(symbol, exchange, period, timeframe) -> float
      - ofi_engine:
          get_z_score(symbol, exchange) -> float
          get_exchanges_for_symbol(symbol) -> List[str]
      - settings: object/module with SCALP_* attributes
    """

    def __init__(self, market_data, ofi_engine, settings):
        self.md = market_data
        self.ofi = ofi_engine
        self.s = settings

    # ---------- Individual gates ----------

    def check_vwap_alignment(self, symbol: str, exchange: str, direction: str) -> ConfluenceResult:
        """Soft gate: LONG only when mid > VWAP; SHORT only when mid < VWAP."""
        name = "VWAP"
        if not getattr(self.s, "SCALP_USE_VWAP_GATE", True):
            return ConfluenceResult(True, "disabled", score=0.0, gate_name=name)

        try:
            vwap = self.md.get_session_vwap(symbol, exchange)
            mid = self.md.get_mid_price(symbol, exchange)
            if vwap is None or mid is None:
                return ConfluenceResult(True, "data unavailable", score=0.5, gate_name=name)

            meta = {"vwap": vwap, "mid": mid}
            if direction == "LONG" and mid > vwap:
                return ConfluenceResult(True, f"mid {mid:.4f} > vwap {vwap:.4f}",
                                        score=1.0, gate_name=name, metadata=meta)
            if direction == "SHORT" and mid < vwap:
                return ConfluenceResult(True, f"mid {mid:.4f} < vwap {vwap:.4f}",
                                        score=1.0, gate_name=name, metadata=meta)
            return ConfluenceResult(False, f"misalign ({direction}: mid={mid:.4f}, vwap={vwap:.4f})",
                                    score=0.0, gate_name=name, metadata=meta)
        except Exception as e:
            logger.debug("VWAP gate error: %s", e)
            return ConfluenceResult(True, f"error - pass: {e}", score=0.5, gate_name=name)

    def check_htf_trend(self, symbol: str, exchange: str, direction: str) -> ConfluenceResult:
        """Soft gate: 5m EMA fast/slow trend must match signal direction."""
        name = "HTF"
        if not getattr(self.s, "SCALP_USE_HTF_TREND_GATE", True):
            return ConfluenceResult(True, "disabled", score=0.0, gate_name=name)

        try:
            tf = getattr(self.s, "SCALP_HTF_TIMEFRAME", "5m")
            fast_p = getattr(self.s, "SCALP_HTF_EMA_FAST", 8)
            slow_p = getattr(self.s, "SCALP_HTF_EMA_SLOW", 21)

            ema_fast = self.md.get_ema(symbol, exchange, tf, fast_p)
            ema_slow = self.md.get_ema(symbol, exchange, tf, slow_p)
            if ema_fast is None or ema_slow is None:
                return ConfluenceResult(True, "EMA unavailable", score=0.5, gate_name=name)

            htf_trend = "LONG" if ema_fast > ema_slow else "SHORT"
            meta = {"htf_trend": htf_trend, "ema_fast": ema_fast, "ema_slow": ema_slow,
                    "timeframe": tf}

            if htf_trend == direction:
                return ConfluenceResult(True, f"{tf} trend confirms {direction}",
                                        score=1.0, gate_name=name, metadata=meta)
            return ConfluenceResult(False, f"{tf} trend {htf_trend} opposes {direction}",
                                    score=0.0, gate_name=name, metadata=meta)
        except Exception as e:
            logger.debug("HTF gate error: %s", e)
            return ConfluenceResult(True, f"error - pass: {e}", score=0.5, gate_name=name)

    def check_volume(self, symbol: str, exchange: str) -> ConfluenceResult:
        """Soft gate: current 1m volume >= rolling median × threshold ratio."""
        name = "VOLUME"
        if not getattr(self.s, "SCALP_USE_VOLUME_GATE", True):
            return ConfluenceResult(True, "disabled", score=0.0, gate_name=name)

        try:
            current = self.md.get_current_minute_volume(symbol, exchange)
            lookback = getattr(self.s, "SCALP_VOLUME_LOOKBACK_MIN", 20)
            median = self.md.get_rolling_median_volume(symbol, exchange, "1m", lookback)

            if current is None or median is None or median == 0:
                return ConfluenceResult(True, "data unavailable", score=0.5, gate_name=name)

            ratio = current / median
            threshold = getattr(self.s, "SCALP_VOLUME_THRESHOLD_RATIO", 1.0)
            meta = {"current_vol": current, "median_vol": median, "ratio": ratio}

            if ratio >= threshold:
                return ConfluenceResult(True, f"volume {ratio:.2f}× median",
                                        score=1.0, gate_name=name, metadata=meta)
            return ConfluenceResult(False, f"volume {ratio:.2f}× < {threshold}",
                                    score=0.0, gate_name=name, metadata=meta)
        except Exception as e:
            logger.debug("Volume gate error: %s", e)
            return ConfluenceResult(True, f"error - pass: {e}", score=0.5, gate_name=name)

    def check_cross_exchange_ofi(self, symbol: str, primary_exchange: str,
                                 direction: str, primary_z: float) -> ConfluenceResult:
        """Hard-on-disagree gate: other venues' OFI must not strongly oppose."""
        name = "CROSS_EX"
        if not getattr(self.s, "SCALP_USE_CROSS_EXCHANGE_OFI", True):
            return ConfluenceResult(True, "disabled", score=0.0, gate_name=name)

        try:
            others = self.ofi.get_exchanges_for_symbol(symbol)
            others = [e for e in others if e != primary_exchange]

            if not others:
                return ConfluenceResult(True, "no other venues",
                                        score=0.5, gate_name=name,
                                        metadata={"others_count": 0})

            min_z = getattr(self.s, "SCALP_CROSS_EXCHANGE_AGREE_Z_MIN", 0.5)
            agree, disagree, zs = 0, 0, {}

            for ex in others:
                z = self.ofi.get_z_score(symbol, ex)
                if z is None:
                    continue
                zs[ex] = z
                if direction == "LONG":
                    if z >= min_z:
                        agree += 1
                    elif z <= -min_z:
                        disagree += 1
                else:  # SHORT
                    if z <= -min_z:
                        agree += 1
                    elif z >= min_z:
                        disagree += 1

            meta = {"primary_z": primary_z, "other_zs": zs,
                    "agree": agree, "disagree": disagree}

            block = getattr(self.s, "SCALP_CROSS_EXCHANGE_DISAGREE_BLOCK", True)
            if disagree > 0 and block:
                return ConfluenceResult(False, f"{disagree} venue(s) opposing",
                                        score=0.0, gate_name=name, metadata=meta)

            if agree > 0:
                return ConfluenceResult(True, f"{agree} venue(s) confirming",
                                        score=1.0, gate_name=name, metadata=meta)
            return ConfluenceResult(True, "neutral cross-exchange",
                                    score=0.5, gate_name=name, metadata=meta)
        except Exception as e:
            logger.debug("CrossEx gate error: %s", e)
            return ConfluenceResult(True, f"error - pass: {e}", score=0.5, gate_name=name)

    def check_btc_directional(self, symbol: str, exchange: str, direction: str) -> ConfluenceResult:
        """Hard gate: alts can't fight BTC's order flow direction."""
        name = "BTC_DIR"
        if not getattr(self.s, "SCALP_USE_BTC_DIRECTIONAL", True):
            return ConfluenceResult(True, "disabled", score=0.0, gate_name=name)

        if symbol.startswith("BTC"):
            return ConfluenceResult(True, "symbol is BTC", score=1.0, gate_name=name)

        try:
            btc_z = self.ofi.get_z_score("BTC/USDT", exchange)
            if btc_z is None:
                # fallback: try MEXC as the canonical BTC venue
                btc_z = self.ofi.get_z_score("BTC/USDT", "MEXC")
            if btc_z is None:
                return ConfluenceResult(True, "BTC z unavailable",
                                        score=0.5, gate_name=name)

            band = getattr(self.s, "SCALP_BTC_OFI_NEUTRAL_BAND", 0.5)
            meta = {"btc_z": btc_z, "band": band}

            if direction == "LONG" and btc_z < -band:
                return ConfluenceResult(False, f"BTC bearish z={btc_z:.2f}, blocking alt LONG",
                                        score=0.0, gate_name=name, metadata=meta)
            if direction == "SHORT" and btc_z > band:
                return ConfluenceResult(False, f"BTC bullish z={btc_z:.2f}, blocking alt SHORT",
                                        score=0.0, gate_name=name, metadata=meta)
            return ConfluenceResult(True, f"BTC z={btc_z:.2f} compatible",
                                    score=1.0, gate_name=name, metadata=meta)
        except Exception as e:
            logger.debug("BTC gate error: %s", e)
            return ConfluenceResult(True, f"error - pass: {e}", score=0.5, gate_name=name)

    def check_adverse_selection(self, symbol: str, exchange: str, direction: str) -> ConfluenceResult:
        """Hard gate: if mid moved against us in the last window, skip."""
        name = "ADVERSE"
        if not getattr(self.s, "SCALP_USE_ADVERSE_SELECTION_GUARD", True):
            return ConfluenceResult(True, "disabled", score=0.0, gate_name=name)

        try:
            window_ms = getattr(self.s, "SCALP_ADVERSE_MOVE_WINDOW_MS", 100)
            threshold = getattr(self.s, "SCALP_ADVERSE_MID_MOVE_BPS", 1.0)

            mid_now = self.md.get_mid_price(symbol, exchange)
            mid_then = self.md.get_mid_price_at_offset(symbol, exchange, offset_ms=window_ms)

            if mid_now is None or mid_then is None or mid_then == 0:
                return ConfluenceResult(True, "mid history unavailable",
                                        score=0.5, gate_name=name)

            move_bps = ((mid_now - mid_then) / mid_then) * 10000
            meta = {"mid_now": mid_now, "mid_then": mid_then,
                    "move_bps": move_bps, "window_ms": window_ms}

            if direction == "LONG" and move_bps < -threshold:
                return ConfluenceResult(False,
                                        f"mid dropped {abs(move_bps):.2f} bps in {window_ms}ms",
                                        score=0.0, gate_name=name, metadata=meta)
            if direction == "SHORT" and move_bps > threshold:
                return ConfluenceResult(False,
                                        f"mid rose {move_bps:.2f} bps in {window_ms}ms",
                                        score=0.0, gate_name=name, metadata=meta)
            return ConfluenceResult(True, f"mid stable ({move_bps:+.2f} bps)",
                                    score=1.0, gate_name=name, metadata=meta)
        except Exception as e:
            logger.debug("Adverse gate error: %s", e)
            return ConfluenceResult(True, f"error - pass: {e}", score=0.5, gate_name=name)

    def check_depth(self, symbol: str, exchange: str,
                    position_size_usd: float) -> ConfluenceResult:
        """Hard gate: book depth must be adequate for the position."""
        name = "DEPTH"
        if not getattr(self.s, "SCALP_USE_DEPTH_GATE", True):
            return ConfluenceResult(True, "disabled", score=0.0, gate_name=name)

        try:
            book = self.md.get_order_book(symbol, exchange, levels=5)
            if book is None or not book.bids or not book.asks:
                return ConfluenceResult(True, "book unavailable",
                                        score=0.5, gate_name=name)

            top5_bid = sum(b.price * b.size for b in book.bids[:5])
            top5_ask = sum(a.price * a.size for a in book.asks[:5])
            min_top5 = min(top5_bid, top5_ask)

            mult = getattr(self.s, "SCALP_MIN_TOP5_DEPTH_MULTIPLIER", 5.0)
            required = position_size_usd * mult

            top1_bid = book.bids[0].price * book.bids[0].size
            top1_ask = book.asks[0].price * book.asks[0].size
            min_top1 = min(top1_bid, top1_ask)
            consume_pct = (position_size_usd / min_top1 * 100) if min_top1 > 0 else 100.0

            max_consume = getattr(self.s, "SCALP_MAX_TOP1_CONSUME_PCT", 20.0)
            meta = {"top5_usd": min_top5, "top1_usd": min_top1,
                    "consume_pct": consume_pct, "required_usd": required}

            if min_top5 < required:
                return ConfluenceResult(False,
                                        f"top5 ${min_top5:.0f} < req ${required:.0f}",
                                        score=0.0, gate_name=name, metadata=meta)
            if consume_pct > max_consume:
                return ConfluenceResult(False,
                                        f"would consume {consume_pct:.1f}% of top (max {max_consume}%)",
                                        score=0.0, gate_name=name, metadata=meta)
            return ConfluenceResult(True, f"depth OK (top5=${min_top5:.0f}, consume={consume_pct:.1f}%)",
                                    score=1.0, gate_name=name, metadata=meta)
        except Exception as e:
            logger.debug("Depth gate error: %s", e)
            return ConfluenceResult(True, f"error - pass: {e}", score=0.5, gate_name=name)

    # ---------- Combined runner ----------

    def run_all_gates(self, symbol: str, exchange: str, direction: str,
                      primary_z: float, position_size_usd: float
                      ) -> CombinedConfluenceResult:
        """Run all v2 gates and return aggregate decision.

        Order matters for short-circuit performance but every check is fast.
        Returns CombinedConfluenceResult with both the verdict and full diagnostics.
        """
        results: List[ConfluenceResult] = []

        # Hard gates first (cheaper to fail fast)
        adverse = self.check_adverse_selection(symbol, exchange, direction)
        results.append(adverse)
        if not adverse.passed:
            return self._build_combined(False, adverse.reason, results,
                                        adverse, None, None, None)

        depth = self.check_depth(symbol, exchange, position_size_usd)
        results.append(depth)
        if not depth.passed:
            return self._build_combined(False, depth.reason, results,
                                        adverse, depth, None, None)

        btc = self.check_btc_directional(symbol, exchange, direction)
        results.append(btc)
        if not btc.passed:
            return self._build_combined(False, btc.reason, results,
                                        adverse, depth, btc, None)

        cross = self.check_cross_exchange_ofi(symbol, exchange, direction, primary_z)
        results.append(cross)
        if not cross.passed:
            return self._build_combined(False, cross.reason, results,
                                        adverse, depth, btc, cross)

        # Soft gates — combined 2-of-3 requirement
        vwap = self.check_vwap_alignment(symbol, exchange, direction)
        htf = self.check_htf_trend(symbol, exchange, direction)
        vol = self.check_volume(symbol, exchange)
        results.extend([vwap, htf, vol])

        soft_passed = sum(1 for r in (vwap, htf, vol) if r.passed and r.score >= 0.99)
        required = getattr(self.s, "SCALP_CONFLUENCE_REQUIRED", 2)

        if soft_passed < required:
            failing = "; ".join(r.reason for r in (vwap, htf, vol) if not r.passed)[:120]
            return self._build_combined(False,
                                        f"confluence {soft_passed}/{required}: {failing}",
                                        results, adverse, depth, btc, cross,
                                        soft_passed=soft_passed)

        return self._build_combined(True, "", results, adverse, depth, btc, cross,
                                    soft_passed=soft_passed)

    def _build_combined(self, passed, reason, results,
                        adverse, depth, btc, cross, soft_passed=0) -> CombinedConfluenceResult:
        strength = self._label_strength(soft_passed, cross)
        return CombinedConfluenceResult(
            passed=passed,
            blocking_reason=reason,
            confluence_score=soft_passed,
            strength_label=strength,
            cross_exchange_agrees=(cross is not None and cross.passed and cross.score >= 0.99),
            btc_compatible=(btc is None or btc.passed),
            adverse_selection_ok=(adverse is None or adverse.passed),
            depth_ok=(depth is None or depth.passed),
            individual_results=results,
            metadata={"soft_passed_count": soft_passed},
        )

    @staticmethod
    def _label_strength(soft_passed: int, cross: Optional[ConfluenceResult]) -> str:
        cross_strong = cross is not None and cross.passed and cross.score >= 0.99
        if soft_passed >= 3 and cross_strong:
            return "VERY_STRONG"
        if soft_passed >= 3:
            return "STRONG"
        if soft_passed >= 2 and cross_strong:
            return "STRONG"
        if soft_passed >= 2:
            return "MODERATE"
        return "WEAK"
