"""
Scalping agent v2 — ATR-aware stop loss calculator.

Replaces the fixed-bps SL in FeeManager with a volatility-adjusted SL that:
  - Floor: never tighter than SCALP_ATR_SL_FLOOR_BPS (prevents noise stop-outs)
  - Ceiling: never wider than SCALP_ATR_SL_CEILING_BPS (preserves RR)
  - Inner: SCALP_ATR_SL_MULTIPLIER × ATR(period, timeframe) in bps

TP stays at round_trip_fees + SCALP_NET_PROFIT_TARGET_BPS.
RR ratio is recomputed and returned so the agent can log actual_rr per trade.

Integration: replace the existing FeeManager.compute_tp_sl() call with
ATRStopCalculator.compute_tp_sl_v2() — keeps the same return shape.
"""

from dataclasses import dataclass
import logging

logger = logging.getLogger("scalping_v2.atr_sl")


@dataclass
class TpSlV2:
    """Return shape for compute_tp_sl_v2 — drop-in compatible with old TpSl plus extras."""

    tp_bps: float
    sl_bps: float
    rr_actual: float
    base_sl_bps: float       # what FeeManager would have given (fixed-bps SL)
    atr_bps: float           # the ATR reading in bps, for logging
    atr_adjusted: bool       # whether ATR moved the SL away from base
    sl_clamped: str          # "FLOOR", "CEILING", or "" if not clamped


class ATRStopCalculator:
    """Volatility-aware TP/SL computation."""

    def __init__(self, market_data, settings):
        self.md = market_data
        self.s = settings

    def compute_tp_sl_v2(self, symbol: str, exchange: str,
                         round_trip_bps: float) -> TpSlV2:
        """Compute TP/SL given the exchange's round-trip fee cost.

        TP = round_trip_fees + net_profit_target
        Base SL = TP / RR_ratio
        ATR-adjusted SL = ATR_bps × multiplier, clamped to [floor, ceiling]
        Final SL = max(base_sl, atr_sl)  — wider of the two, never tighter than base

        The wider-of-two rule preserves the breakeven win rate guarantee: the
        agent will never have an SL tighter than what FeeManager originally
        derived from the venue's fee structure.
        """
        target = getattr(self.s, "SCALP_NET_PROFIT_TARGET_BPS", 3.0)
        rr = getattr(self.s, "SCALP_RR_RATIO", 1.6)
        floor = getattr(self.s, "SCALP_ATR_SL_FLOOR_BPS", 1.5)
        ceiling = getattr(self.s, "SCALP_ATR_SL_CEILING_BPS", 8.0)
        mult = getattr(self.s, "SCALP_ATR_SL_MULTIPLIER", 0.3)
        use_atr = getattr(self.s, "SCALP_USE_ATR_AWARE_SL", True)

        tp = round_trip_bps + target
        base_sl = tp / rr if rr > 0 else target / 1.6

        if not use_atr:
            return TpSlV2(tp_bps=tp, sl_bps=base_sl, rr_actual=rr,
                          base_sl_bps=base_sl, atr_bps=0.0,
                          atr_adjusted=False, sl_clamped="")

        atr_bps = self._safe_atr_bps(symbol, exchange)
        if atr_bps is None:
            # graceful fallback — use the FeeManager-derived base SL
            return TpSlV2(tp_bps=tp, sl_bps=base_sl, rr_actual=rr,
                          base_sl_bps=base_sl, atr_bps=0.0,
                          atr_adjusted=False, sl_clamped="")

        atr_sl_raw = atr_bps * mult
        clamped = ""
        atr_sl = atr_sl_raw
        if atr_sl < floor:
            atr_sl = floor
            clamped = "FLOOR"
        elif atr_sl > ceiling:
            atr_sl = ceiling
            clamped = "CEILING"

        # Take the wider of base_sl and atr_sl — never go below the
        # FeeManager-derived minimum
        final_sl = max(base_sl, atr_sl)
        adjusted = final_sl > base_sl
        actual_rr = tp / final_sl if final_sl > 0 else 0.0

        return TpSlV2(tp_bps=tp, sl_bps=final_sl, rr_actual=actual_rr,
                      base_sl_bps=base_sl, atr_bps=atr_bps,
                      atr_adjusted=adjusted, sl_clamped=clamped)

    def compute_tp_sl_vol(self, symbol: str, exchange: str,
                          round_trip_bps: float):
        """Vol-scaled TP/SL candidate for the HIGH-FEE conditional path
        (gate 4b). Returns a TpSlV2, or None when ATR is unavailable (caller
        falls back to the static fee skip).

        Unlike compute_tp_sl_v2 — whose TP is fee-fixed (rt + target) and
        where ATR only ever WIDENS the SL — this lets TP scale with realised
        volatility:

            tp = max(rt + net_target, SCALP_ATR_TP_MULTIPLIER × atr_bps)
            sl = clamp(SCALP_ATR_SL_MULTIPLIER × atr_bps, floor, ceiling)

        That is the only geometry under which a high-fee venue can clear the
        breakeven cap: fees shrink relative to the achievable move. The SL is
        the ATR-clamped stop alone — NOT widened to tp/RR (a vol-scaled TP
        would drag tp/RR to tens of bps and push breakeven back above the
        cap). The breakeven-win-rate guarantee that the wider-of-two rule
        provides in compute_tp_sl_v2 is enforced DIRECTLY here instead: the
        caller (gate 4b) recomputes breakeven on this exact geometry and
        refuses the entry unless it clears SCALP_MAX_BREAKEVEN_WIN_RATE.
        """
        target = getattr(self.s, "SCALP_NET_PROFIT_TARGET_BPS", 3.0)
        rr = getattr(self.s, "SCALP_RR_RATIO", 1.6)
        floor = getattr(self.s, "SCALP_ATR_SL_FLOOR_BPS", 1.5)
        ceiling = getattr(self.s, "SCALP_ATR_SL_CEILING_BPS", 8.0)
        mult_sl = getattr(self.s, "SCALP_ATR_SL_MULTIPLIER", 0.3)
        mult_tp = getattr(self.s, "SCALP_ATR_TP_MULTIPLIER", 1.2)

        atr_bps = self._safe_atr_bps(symbol, exchange)
        if atr_bps is None:
            return None

        base_tp = round_trip_bps + target
        tp = max(base_tp, mult_tp * atr_bps)

        sl = atr_bps * mult_sl
        clamped = ""
        if sl < floor:
            sl, clamped = floor, "FLOOR"
        elif sl > ceiling:
            sl, clamped = ceiling, "CEILING"

        base_sl = base_tp / rr if rr > 0 else base_tp
        actual_rr = tp / sl if sl > 0 else 0.0
        return TpSlV2(tp_bps=tp, sl_bps=sl, rr_actual=actual_rr,
                      base_sl_bps=base_sl, atr_bps=atr_bps,
                      atr_adjusted=True, sl_clamped=clamped)

    def _safe_atr_bps(self, symbol: str, exchange: str):
        """Fetch ATR as bps-of-mid, return None on any failure."""
        try:
            period = getattr(self.s, "SCALP_ATR_PERIOD", 20)
            tf = getattr(self.s, "SCALP_ATR_TIMEFRAME", "1m")
            atr = self.md.get_atr(symbol, exchange, period=period, timeframe=tf)
            mid = self.md.get_mid_price(symbol, exchange)
            if atr is None or mid is None or mid <= 0:
                return None
            return (atr / mid) * 10000
        except Exception as e:
            logger.debug("ATR fetch error: %s", e)
            return None
