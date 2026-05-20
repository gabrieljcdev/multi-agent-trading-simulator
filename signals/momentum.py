"""
signals/momentum.py
Momentum scanner — Track B.
"""
import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
from signals.base import Signal
from core.regime_detector import regime_detector
from signals.ofi import ofi_scorer
from config import settings

logger = logging.getLogger(__name__)

class MomentumScanner:
    def __init__(self, market_data):
        self._market_data = market_data
        self._last_signals: dict[str, datetime] = {}

    async def scan(self, sentiment_scores: dict) -> list:
        signals = []
        for pair in self._market_data.active_pairs():
            for exchange in settings.ENABLED_EXCHANGES:
                s = await self._evaluate_pair(pair, exchange, sentiment_scores)
                if s:
                    signals.append(s)
                    break
        return signals

    async def _evaluate_pair(self, pair, exchange, sentiment_scores):
        dedup_key = f"mom_{pair}"
        if dedup_key in self._last_signals:
            if datetime.utcnow() - self._last_signals[dedup_key] < timedelta(minutes=settings.SIGNAL_EXPIRY_MINUTES):
                return None
        df_fast = self._market_data.get_candles(exchange, pair, settings.FAST_TIMEFRAME)
        df_mid  = self._market_data.get_candles(exchange, pair, settings.MID_TIMEFRAME)
        df_slow = self._market_data.get_candles(exchange, pair, settings.SLOW_TIMEFRAME)
        if df_fast is None or len(df_fast) < 30:
            return None
        latest = df_fast.iloc[-1]
        regime = regime_detector.get_primary(pair)
        if regime and not regime.momentum_ok:
            return None
        rsi = latest.get("rsi")
        if rsi is None or pd.isna(rsi):
            return None
        if not (settings.RSI_MOMENTUM_MIN <= float(rsi) <= settings.RSI_MOMENTUM_MAX):
            return None
        volume     = latest.get("volume", 0)
        volume_sma = latest.get("volume_sma")
        if volume_sma is None or pd.isna(volume_sma) or float(volume_sma) == 0:
            return None
        volume_ratio = float(volume) / float(volume_sma)
        if volume_ratio < settings.MOMENTUM_MIN_VOLUME_RATIO:
            return None
        close = float(latest["close"])
        direction, breakout_level = self._detect_breakout(df_fast)
        if direction is None:
            return None
        tf_5m  = self._confirms(df_fast, direction)
        tf_15m = self._confirms(df_mid,  direction) if df_mid  is not None else False
        tf_1h  = self._confirms(df_slow, direction) if df_slow is not None else False
        if sum([tf_5m, tf_15m, tf_1h]) < settings.MIN_TF_CONFIRMATIONS:
            return None
        ofi = ofi_scorer.get_best(pair)
        ofi_mod = ofi.signal_modifier(direction) if ofi else 0.0
        if settings.MOMENTUM_REQUIRE_OFI and ofi_mod < 0:
            return None
        raw_score = 50.0
        raw_score += min(20.0, (volume_ratio - settings.MOMENTUM_MIN_VOLUME_RATIO) * 8)
        raw_score += sum([tf_5m, tf_15m, tf_1h]) * 5
        adx = latest.get("adx")
        if adx and not pd.isna(adx):
            raw_score += 8 if float(adx) >= settings.ADX_STRONG_TREND else 4 if float(adx) >= settings.ADX_TRENDING_MIN else 0
        raw_score = min(95.0, raw_score)
        coin = pair.split("/")[0]
        sentiment = sentiment_scores.get(coin, sentiment_scores.get("MARKET", {}))
        composite = sentiment.get("composite", 50)
        velocity  = sentiment.get("velocity", 0)
        sent_mod = 0.0
        if composite >= settings.SENTIMENT_BOOST_THRESHOLD:
            sent_mod += settings.SENTIMENT_BOOST_AMOUNT
        elif composite <= settings.SENTIMENT_BLOCK_THRESHOLD:
            sent_mod -= settings.SENTIMENT_SUPPRESS_AMOUNT
        signal = Signal(
            pair=pair, signal_type="momentum", direction=direction,
            exchange=exchange, raw_score=raw_score,
            sentiment_mod=sent_mod + ofi_mod,
            rsi=float(rsi), volume_ratio=volume_ratio,
            sentiment_score=composite, sentiment_velocity=velocity,
            tf_5m=tf_5m, tf_15m=tf_15m, tf_1h=tf_1h,
            indicators={"close": close, "breakout_level": breakout_level,
                       "volume_ratio": volume_ratio,
                       "adx": float(adx) if adx and not pd.isna(adx) else None,
                       "ofi": ofi.ema_ofi if ofi else None},
        )
        signal.expires_at = datetime.utcnow() + timedelta(minutes=settings.SIGNAL_EXPIRY_MINUTES)
        atr = latest.get("atr")
        if atr and not pd.isna(atr):
            atr = float(atr)
            signal.suggested_entry = close
            signal.suggested_sl = close - atr*1.5 if direction=="long" else close + atr*1.5
            signal.suggested_tp = close + atr*3.0 if direction=="long" else close - atr*3.0
            risk = abs(close - signal.suggested_sl)
            reward = abs(close - signal.suggested_tp)
            signal.risk_reward = reward/risk if risk > 0 else None
        self._last_signals[dedup_key] = datetime.utcnow()
        logger.info(f"MOMENTUM: {pair} {direction} score={raw_score:.0f} vol={volume_ratio:.1f}x")
        return signal

    def _detect_breakout(self, df):
        if len(df) < settings.MOMENTUM_BREAKOUT_LOOKBACK + 2:
            return None, None
        lookback = df.iloc[-(settings.MOMENTUM_BREAKOUT_LOOKBACK+1):-1]
        close = df.iloc[-1]["close"]
        recent_high = lookback["high"].max()
        recent_low  = lookback["low"].min()
        if close > recent_high * 0.999:
            return "long", float(recent_high)
        if close < recent_low * 1.001:
            return "short", float(recent_low)
        return None, None

    def _confirms(self, df, direction):
        if df is None or len(df) < 2:
            return False
        ema_fast = df.iloc[-1].get("ema_fast")
        ema_slow = df.iloc[-1].get("ema_slow")
        if ema_fast is None or ema_slow is None or pd.isna(ema_fast) or pd.isna(ema_slow):
            return False
        return float(ema_fast) > float(ema_slow) if direction=="long" else float(ema_fast) < float(ema_slow)
