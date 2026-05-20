"""
signals/ofi.py

Order Flow Imbalance (OFI) scorer.

Academic basis: Cont, Kukanov & Stoikov (2014) showed OFI has a
near-linear relationship with short-horizon price changes. An EMA
on imbalance yields reliable directional signals for scalping.

OFI = bid_volume / (bid_volume + ask_volume) across top N levels.
0.65+ = strong bid pressure (bullish confirmation)
0.35− = strong ask pressure (bearish confirmation)
"""

import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class OFISnapshot:
    pair:         str
    exchange:     str
    raw_ofi:      float          # Instantaneous imbalance 0–1
    ema_ofi:      float          # EMA-smoothed imbalance
    direction:    str            # bullish | bearish | neutral
    score_mod:    float          # Score modifier to apply to signals
    bid_depth:    float          # Total bid USD depth (top N levels)
    ask_depth:    float          # Total ask USD depth (top N levels)
    vpin:         Optional[float] = None   # Volume-sync probability of informed trading

    def confirms_long(self) -> bool:
        return self.ema_ofi >= settings.OFI_BULLISH_THRESHOLD

    def confirms_short(self) -> bool:
        return self.ema_ofi <= settings.OFI_BEARISH_THRESHOLD

    def signal_modifier(self, trade_direction: str) -> float:
        """
        Return score modifier for a given trade direction.
        Positive if OFI confirms the direction, negative if contradicts.
        """
        if trade_direction == "long":
            if self.confirms_long():
                return settings.OFI_BOOST_AMOUNT
            elif self.confirms_short():
                return -settings.OFI_PENALTY_AMOUNT
        elif trade_direction == "short":
            if self.confirms_short():
                return settings.OFI_BOOST_AMOUNT
            elif self.confirms_long():
                return -settings.OFI_PENALTY_AMOUNT
        return 0.0


class OFIScorer:
    """
    Maintains rolling OFI state per (pair, exchange).
    Feed raw order book updates via update_book() on each WebSocket tick.
    """

    def __init__(self):
        # EMA state per (pair, exchange)
        self._ema: dict[tuple, float] = {}

        # VPIN: rolling trade volume buckets for informed trading probability
        self._buy_vol:  dict[tuple, deque]  = {}
        self._sell_vol: dict[tuple, deque]  = {}
        self._vpin_window = 50  # Buckets for VPIN calculation

        self._latest: dict[tuple, OFISnapshot] = {}

    def update_book(
        self,
        pair:     str,
        exchange: str,
        bids:     list[tuple[float, float]],   # [(price, size), ...]
        asks:     list[tuple[float, float]],
    ) -> OFISnapshot:
        """
        Process a fresh order book snapshot and return updated OFI.

        Parameters
        ----------
        bids / asks : list of (price, size) tuples, top N levels
        """
        key = (pair, exchange)
        levels = settings.OFI_LEVELS

        # Sum top-N depth (price × size = USD value)
        bid_depth = sum(p * s for p, s in bids[:levels]) if bids else 0.0
        ask_depth = sum(p * s for p, s in asks[:levels]) if asks else 0.0
        total = bid_depth + ask_depth

        if total == 0:
            raw_ofi = 0.5
        else:
            raw_ofi = bid_depth / total

        # EMA smoothing
        alpha = 2 / (settings.OFI_EMA_PERIOD + 1)
        if key not in self._ema:
            self._ema[key] = raw_ofi
        else:
            self._ema[key] = alpha * raw_ofi + (1 - alpha) * self._ema[key]

        ema_ofi = self._ema[key]

        # Direction classification
        if ema_ofi >= settings.OFI_BULLISH_THRESHOLD:
            direction = "bullish"
        elif ema_ofi <= settings.OFI_BEARISH_THRESHOLD:
            direction = "bearish"
        else:
            direction = "neutral"

        # Score modifier (before we know trade direction — directional mod applied later)
        score_mod = 0.0
        if direction == "bullish":
            score_mod = settings.OFI_BOOST_AMOUNT * 0.5    # Partial until direction known
        elif direction == "bearish":
            score_mod = -settings.OFI_PENALTY_AMOUNT * 0.5

        snap = OFISnapshot(
            pair=pair,
            exchange=exchange,
            raw_ofi=raw_ofi,
            ema_ofi=ema_ofi,
            direction=direction,
            score_mod=score_mod,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
        )

        self._latest[key] = snap
        return snap

    def update_trades(
        self,
        pair:     str,
        exchange: str,
        buy_vol:  float,
        sell_vol: float,
    ) -> Optional[float]:
        """
        Update VPIN with trade volume data.
        Returns VPIN value (0–1) once enough buckets accumulated.
        """
        key = (pair, exchange)

        if key not in self._buy_vol:
            self._buy_vol[key]  = deque(maxlen=self._vpin_window)
            self._sell_vol[key] = deque(maxlen=self._vpin_window)

        self._buy_vol[key].append(buy_vol)
        self._sell_vol[key].append(sell_vol)

        if len(self._buy_vol[key]) < self._vpin_window:
            return None

        buy_arr  = list(self._buy_vol[key])
        sell_arr = list(self._sell_vol[key])
        total_vol = sum(b + s for b, s in zip(buy_arr, sell_arr))

        if total_vol == 0:
            return None

        # VPIN = |buy - sell| / total per bucket, averaged
        vpin = sum(
            abs(b - s) / (b + s) if (b + s) > 0 else 0
            for b, s in zip(buy_arr, sell_arr)
        ) / self._vpin_window

        # Attach to latest snapshot
        if key in self._latest:
            self._latest[key].vpin = vpin

        return vpin

    def get(self, pair: str, exchange: str) -> Optional[OFISnapshot]:
        return self._latest.get((pair, exchange))

    def get_best(self, pair: str) -> Optional[OFISnapshot]:
        """Return the OFI snapshot with highest liquidity across all exchanges."""
        candidates = [
            snap for (p, _), snap in self._latest.items() if p == pair
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.bid_depth + s.ask_depth)

    def is_jump_risk(self, pair: str, exchange: str) -> bool:
        """Return True if VPIN suggests elevated informed trading / jump risk."""
        snap = self.get(pair, exchange)
        if snap is None or snap.vpin is None:
            return False
        return snap.vpin >= settings.VPIN_HIGH_THRESHOLD


# Module-level singleton
ofi_scorer = OFIScorer()
