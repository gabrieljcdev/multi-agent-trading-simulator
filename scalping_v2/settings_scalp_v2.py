"""
Scalping agent v2 — settings additions.

To integrate: append these constants to config/settings.py, or import this module
and use the values directly. All constants follow the existing SCALP_* naming.

Each section is independently togglable so you can A/B test in observation mode.
"""

# === ENTRY THRESHOLD TIGHTENING ===
# Raise the bar for what counts as an actionable OFI signal. The literature
# (Globe Research 2020 on Coinbase BTC, 71% accuracy) measured accuracy at
# extreme readings. Median-strength signals have meaningfully worse forecasting.

SCALP_OFI_Z_ENTRY = 2.0              # was 1.5 — only act on stronger imbalances
SCALP_OFI_PERSIST_TICKS = 5          # was 3 — require longer persistence

# === SESSION WINDOW TIGHTENING ===
# London/NY overlap is where liquidity is deepest and spreads tightest.
# Outside this window, signal quality drops without compensating volatility.

SCALP_SESSION_START_UTC = 12         # was 7
SCALP_SESSION_END_UTC = 16           # was 17

# === CONFLUENCE GATES (VWAP + HTF + Volume) ===
# Require 2 of 3 independent confirmations before entering. Reduces trade count
# but raises win rate by filtering out trades that fight the broader picture.

SCALP_USE_CONFLUENCE = True
SCALP_CONFLUENCE_REQUIRED = 2        # of 3 (VWAP, HTF trend, volume)

SCALP_USE_VWAP_GATE = True
SCALP_USE_HTF_TREND_GATE = True
SCALP_HTF_TIMEFRAME = "5m"
SCALP_HTF_EMA_FAST = 8
SCALP_HTF_EMA_SLOW = 21

SCALP_USE_VOLUME_GATE = True
SCALP_VOLUME_LOOKBACK_MIN = 20
SCALP_VOLUME_THRESHOLD_RATIO = 1.0   # current 1m volume >= rolling median

# === CROSS-EXCHANGE OFI CONFIRMATION ===
# When two independent venues show the same imbalance direction, conviction
# is much higher. When venues disagree, expectation is reversion — skip.

SCALP_USE_CROSS_EXCHANGE_OFI = True
SCALP_CROSS_EXCHANGE_DISAGREE_BLOCK = True
SCALP_CROSS_EXCHANGE_AGREE_Z_MIN = 0.5   # other venue's |z| must exceed this to count

# === BTC DIRECTIONAL GATE FOR ALTS ===
# Alts that fight BTC's order flow are statistically worse. Apply Quantitative
# Finance 2023 cross-impact result: BTC OFI direction filters alt scalp signals.

SCALP_USE_BTC_DIRECTIONAL = True
SCALP_BTC_OFI_NEUTRAL_BAND = 0.5     # |BTC z| < this is treated as neutral

# === ADVERSE SELECTION GUARD ===
# If mid moved against the signal direction in the last 100ms, the order book
# is moving away from us — skip. Brogaard/Hendershott 2014: HFT-supplied
# liquidity is adversely selected. We don't want to be that liquidity supplier
# crossing the spread at exactly the wrong moment.

SCALP_USE_ADVERSE_SELECTION_GUARD = True
SCALP_ADVERSE_MID_MOVE_BPS = 1.0
SCALP_ADVERSE_MOVE_WINDOW_MS = 100

# === DEPTH ADEQUACY ===
# Don't enter trades where our own position would meaningfully move the book.
# Top-5 levels must hold N× our position size; top-1 can't be consumed >X%.

SCALP_USE_DEPTH_GATE = True
SCALP_MIN_TOP5_DEPTH_MULTIPLIER = 5.0
SCALP_MAX_TOP1_CONSUME_PCT = 20.0

# === ATR-AWARE STOP LOSS ===
# Fixed bps SL is too tight on volatile pairs (stopped out by noise) and too
# loose on calm pairs (gives back profit). Scale SL by recent realised volatility.

SCALP_USE_ATR_AWARE_SL = True
SCALP_ATR_PERIOD = 20
SCALP_ATR_TIMEFRAME = "1m"
SCALP_ATR_SL_MULTIPLIER = 0.3
SCALP_ATR_SL_FLOOR_BPS = 1.5         # never go below this regardless of ATR
SCALP_ATR_SL_CEILING_BPS = 8.0       # never go above this either

# === ACTIVATION CRITERIA v2 (tighter than v1) ===
# Activation thresholds rise to reflect the higher-quality signal set we're
# expecting from the v2 selectivity gates.

SCALP_MIN_OBSERVATIONS_FOR_LIVE_V2 = 300       # was 200
SCALP_MIN_WIN_RATE_FOR_LIVE_V2 = 0.55          # was 0.52
SCALP_MIN_AVG_NET_BPS_FOR_LIVE_V2 = 0.5        # was 0
SCALP_MAX_HOLD_EXIT_PCT_V2 = 0.25              # was 0.30
SCALP_MIN_DIRECTIONAL_ACC_1M_V2 = 0.57         # was 0.55


# === Convenience accessor for tests / demo ===
class V2Settings:
    """Snapshot of v2 settings, accessible as attributes."""

    def __init__(self):
        import sys
        module = sys.modules[__name__]
        for name in dir(module):
            if name.startswith("SCALP_"):
                setattr(self, name, getattr(module, name))


def default_v2_settings():
    """Return a settings object with all v2 defaults loaded."""
    return V2Settings()
