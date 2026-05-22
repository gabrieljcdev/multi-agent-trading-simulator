"""
config/settings.py

Every parameter the bot uses lives here.
Nothing is hardcoded anywhere else — this is the single tuning instrument.

During sim, change freely and restart to test different configurations.
The goal is to find YOUR optimal settings through data, not guesswork upfront.

APPROVAL MODES
--------------
  per_trade   — you approve every individual signal before execution
  window      — you approve a session brief; Claude trades freely within it
  autonomous  — Claude runs freely within hard safety limits; no approval needed

Start with per_trade to understand signal quality, then graduate to window
or autonomous once you trust the signal engine.
"""

from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
DB_PATH  = DATA_DIR / "cryptobot.db"

# ══════════════════════════════════════════════════════════════════════════════
# CORE MODE
# ══════════════════════════════════════════════════════════════════════════════

SIM_MODE        = True          # True = paper trading. False = real money.
ACTIVE_PROFILE  = "balanced"    # conservative | balanced | aggressive | custom
ACTIVE_STRATEGY = "default"     # default | arb_only | scalper | custom

# How much autonomy Claude has
# per_trade | window | autonomous
APPROVAL_MODE = "autonomus"

# ══════════════════════════════════════════════════════════════════════════════
# APPROVAL MODE SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

# ── Per-trade mode ─────────────────────────────────────────────────────────
PER_TRADE_SHOW_FULL_REASONING  = True   # Full brief or summary only
PER_TRADE_AUTO_EXPIRE_SECONDS  = 600    # Signal auto-skips if you don't respond

# ── Window mode ────────────────────────────────────────────────────────────
# Claude presents a session analysis; you approve; Claude trades freely inside it
WINDOW_BRIEF_INCLUDES = [
    "regime", "sentiment", "recommended_pairs",
    "recommended_strategies", "risk_level",
    "expected_trade_count", "market_conditions", "news_events",
]
WINDOW_DEFAULT_DURATION_MINUTES  = 120   # How long an approved window lasts
WINDOW_MIN_DURATION_MINUTES      = 15
WINDOW_MAX_DURATION_MINUTES      = 480   # 8 hours max
WINDOW_AUTO_RENEW                = False  # Ask to renew when window expires
WINDOW_PAUSE_ON_CIRCUIT_BREAKER  = True
WINDOW_MAX_TRADES_PER_HOUR       = 6     # Hard cap within window (0 = unlimited)
WINDOW_REAPPROVE_ON_REGIME_SHIFT = True  # Re-present brief if regime changes significantly

# ── Autonomous mode ────────────────────────────────────────────────────────
# No approval required. Claude runs within safety limits only.
AUTO_MAX_TRADES_PER_HOUR   = 4      # 0 = unlimited
AUTO_MAX_TRADES_PER_DAY    = 20     # 0 = unlimited
AUTO_NOTIFY_ON_ENTRY       = True   # Terminal notification on every entry
AUTO_NOTIFY_ON_EXIT        = True   # Terminal notification on every exit
AUTO_PAUSE_ON_LOSS_STREAK  = 3      # Alert + pause after N consecutive losses (0 = never)
AUTO_SUMMARY_INTERVAL_MIN  = 60     # Rolling summary every N minutes (0 = off)

# ── Session floor (window + autonomous) ───────────────────────────────────
# Claude won't open a window or start autonomous trading unless these pass.
# Set any to None to disable that check.
SESSION_MIN_SENTIMENT_SCORE  = 40   # Minimum composite sentiment score
SESSION_BLOCK_CHOPPY_REGIME  = True
SESSION_BLOCK_EXTREME_FEAR   = True   # Block if F&G < 15
SESSION_BLOCK_EXTREME_GREED  = False  # Optionally block if F&G > 88
SESSION_REQUIRE_CLEAR_NEWS   = True
SESSION_MIN_ACTIVE_PAIRS     = 2      # Need at least N viable pairs

# ══════════════════════════════════════════════════════════════════════════════
# CAPITAL
# ══════════════════════════════════════════════════════════════════════════════

EXCHANGE_BALANCES = {
    "binance": 100.0,
    "kraken":  100.0,
    "bybit":   100.0,
    "kucoin":  100.0,
}

# ══════════════════════════════════════════════════════════════════════════════
# EXCHANGES
# ══════════════════════════════════════════════════════════════════════════════

ENABLED_EXCHANGES  = ["binance", "kraken", "bybit", "kucoin"]
MIN_LIQUIDITY_USD  = 50_000     # Minimum order book depth to trade a pair
ORDER_BOOK_DEPTH   = 10         # Levels to stream per side for OFI

# ══════════════════════════════════════════════════════════════════════════════
# PAIR UNIVERSE
# ══════════════════════════════════════════════════════════════════════════════

PAIR_UNIVERSE        = "auto"   # "auto" = top N by volume | "manual" = list below
PAIR_UNIVERSE_TOP_N  = 50
PAIR_MIN_MARKET_CAP  = "large"  # large | mid | all

FALLBACK_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
    "XRP/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT",
    "MATIC/USDT", "LINK/USDT", "UNI/USDT", "ATOM/USDT",
    "LTC/USDT", "NEAR/USDT", "FIL/USDT", "APT/USDT",
]

# ══════════════════════════════════════════════════════════════════════════════
# TIMEFRAMES
# ══════════════════════════════════════════════════════════════════════════════

TIMEFRAMES      = ["5m", "15m", "1h"]
FAST_TIMEFRAME  = "5m"
MID_TIMEFRAME   = "15m"
SLOW_TIMEFRAME  = "1h"
CANDLE_LOOKBACK = {"5m": 200, "15m": 200, "1h": 200}

# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL QUALITY GATE
# ══════════════════════════════════════════════════════════════════════════════

SIGNAL_SCORE_THRESHOLD    = 65   # Minimum score to pass gate     test range: 50–80
MAX_ACTIVE_SIGNALS        = 3    # Max signals surfaced at once    test range: 1–5
SIGNAL_EXPIRY_MINUTES     = 10   # Signal expires if not acted on  test range: 5–20
REQUIRE_MULTI_TF_CONFIRM  = True
MIN_TF_CONFIRMATIONS      = 2    # TFs that must agree (1, 2, or 3)

# ══════════════════════════════════════════════════════════════════════════════
# REGIME DETECTION
# ══════════════════════════════════════════════════════════════════════════════

ADX_TRENDING_MIN   = 25    # test: 20–30
ADX_STRONG_TREND   = 40    # test: 35–45
ADX_RANGING_MAX    = 20    # test: 18–25
ADX_CHOPPY_MAX     = 15    # test: 12–18
ADX_GRID_MIN       = 15
ADX_GRID_MAX       = 25
ADX_GRID_RESET     = 30    # Dynamic grid resets when ADX crosses this

HURST_TRENDING_MIN   = 0.55   # test: 0.52–0.62
HURST_REVERTING_MAX  = 0.48   # test: 0.42–0.50
HURST_LOOKBACK_BARS  = 200    # test: 100–300
HURST_RANDOM_ZONE    = 0.04

ATR_HIGH_VOL_PERCENTILE  = 90   # test: 80–95
ATR_LOW_VOL_PERCENTILE   = 20
ATR_PERCENTILE_LOOKBACK  = 100

BB_WIDTH_EXPANDING_FACTOR = 1.3  # test: 1.2–1.5

# ══════════════════════════════════════════════════════════════════════════════
# ORDER FLOW IMBALANCE (OFI)
# ══════════════════════════════════════════════════════════════════════════════

OFI_LEVELS             = 5      # Order book levels in OFI calc   test: 3–10
OFI_EMA_PERIOD         = 20     # Smoothing window                test: 10–30
OFI_BULLISH_THRESHOLD  = 0.65   # test: 0.60–0.72
OFI_BEARISH_THRESHOLD  = 0.35   # test: 0.28–0.40
OFI_BOOST_AMOUNT       = 10     # Score boost when OFI confirms   test: 5–15
OFI_PENALTY_AMOUNT     = 15     # Score penalty when contradicts  test: 10–20
VPIN_HIGH_THRESHOLD    = 0.75   # test: 0.65–0.85

# ══════════════════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

RSI_PERIOD            = 14      # test: 9–21
RSI_MOMENTUM_MIN      = 40      # test: 35–50
RSI_MOMENTUM_MAX      = 70      # test: 65–75
RSI_OVERBOUGHT        = 70
RSI_OVERSOLD          = 30

MACD_FAST             = 12
MACD_SLOW             = 26
MACD_SIGNAL           = 9

BB_PERIOD             = 20
BB_STDDEV             = 2.0     # test: 1.8–2.5
BB_REVERSION_ENTRY    = 2.0     # σ outside band to trigger reversion

EMA_FAST              = 9
EMA_SLOW              = 21
EMA_TREND             = 50

VOLUME_SURGE_MULTIPLIER = 2.0   # test: 1.5–3.0
VWAP_STRETCH_PCT        = 0.8   # test: 0.5–1.2

# ══════════════════════════════════════════════════════════════════════════════
# ARBITRAGE (signal track + dedicated arb engine)
# ══════════════════════════════════════════════════════════════════════════════
# ARB_MIN_GAP_PCT is the bitget-special-case threshold used by the dedicated
# arb engine (execution/arb_engine.py). The signal track (signals/arbitrage.py)
# uses ARB_MIN_GAP_PCT_FALLBACK to keep its original 0.35% behaviour.

ARB_MIN_GAP_PCT          = 0.03  # test: 0.02–0.10   (bitget special-case)
ARB_MIN_GAP_PCT_FALLBACK = 0.35  # test: 0.25–0.50   (non-bitget — also signal track)
ARB_FEE_ESTIMATE_PCT     = 0.20
ARB_MAX_TRANSFER_SECONDS = 60
ARB_MIN_LIQUIDITY_MULT   = 2.0

# Dedicated arb engine
ARB_SCAN_INTERVAL_MS      = 500       # test: 250–2000
ARB_MAX_POSITION_USD      = 25.0      # test: 10–100
ARB_MIN_LIQUIDITY_USD     = 500.0     # test: 250–2000   (sum of top 3 book levels)
ARB_MAX_CONCURRENT        = 3         # test: 1–5
ARB_DAILY_LOSS_HALT_USD   = 10.0      # test: 5–50
ARB_CONSECUTIVE_LOSS_HALT = 5         # test: 3–10

# Pairs the arb engine watches. Distinct from signal-track FALLBACK_PAIRS.
ARB_WATCH_PAIRS = [
    "BTC/USDT",  "ETH/USDT",  "SOL/USDT",  "BNB/USDT",
    "AVAX/USDT", "LINK/USDT", "DOT/USDT",  "MATIC/USDT",
    "NEAR/USDT", "APT/USDT",  "INJ/USDT",  "ARB/USDT",
    "OP/USDT",   "SUI/USDT",  "ATOM/USDT", "ADA/USDT",
]

# Maker+taker effective fee per exchange (fractional, not %). bitget's
# ultra-low fee is the reason ARB_MIN_GAP_PCT can be set to 0.03%.
ARB_FEE_MAP = {
    "bitget":   0.0001,    # 0.01% — game changer
    "kraken":   0.0026,
    "bitstamp": 0.0050,
    "gateio":   0.0020,
    "bitfinex": 0.0020,
    "bybit":    0.0010,
}

# ══════════════════════════════════════════════════════════════════════════════
# MOMENTUM SIGNAL (TRACK B)
# ══════════════════════════════════════════════════════════════════════════════

MOMENTUM_MIN_VOLUME_RATIO   = 2.0   # test: 1.5–3.0
MOMENTUM_REQUIRE_LARGE_CAP  = True  # Academic finding: only reliable on large caps
MOMENTUM_REQUIRE_HURST      = True
MOMENTUM_REQUIRE_OFI        = False  # Stricter — enable once confident
MOMENTUM_MIN_ADX            = 22    # test: 18–30
MOMENTUM_BREAKOUT_LOOKBACK  = 20    # test: 10–30

# ══════════════════════════════════════════════════════════════════════════════
# MEAN REVERSION SIGNAL (TRACK C)
# ══════════════════════════════════════════════════════════════════════════════

REVERSION_BB_THRESHOLD        = 2.0   # test: 1.5–2.5
REVERSION_REQUIRE_DIVERGENCE  = True
REVERSION_REQUIRE_HURST       = True
REVERSION_MAX_ADX             = 22    # test: 18–28
REVERSION_VWAP_CONFIRM        = True

# ══════════════════════════════════════════════════════════════════════════════
# LIQUIDITY SWEEP (TRACK D)
# ══════════════════════════════════════════════════════════════════════════════

SWEEP_MIN_WICK_PCT        = 0.5   # test: 0.3–0.8
SWEEP_OFI_FLIP_REQUIRED   = True
SWEEP_VOLUME_SPIKE_MULT   = 2.5   # test: 2.0–3.5
SWEEP_REVERSAL_CANDLES    = 3     # test: 2–5
SWEEP_KEY_LEVEL_LOOKBACK  = 50    # test: 30–100

# ══════════════════════════════════════════════════════════════════════════════
# DYNAMIC GRID
# ══════════════════════════════════════════════════════════════════════════════

GRID_ENABLED            = True
GRID_SPACING_PCT        = 0.5    # test: 0.3–1.0
GRID_LEVELS_EACH_SIDE   = 5      # test: 3–8
GRID_ORDER_SIZE_PCT     = 0.5    # Portfolio % per level
GRID_AUTO_RESET         = True
GRID_RESET_COOLDOWN_MIN = 30

# ══════════════════════════════════════════════════════════════════════════════
# SENTIMENT
# ══════════════════════════════════════════════════════════════════════════════

SENTIMENT_ENABLED                 = True
SENTIMENT_UPDATE_INTERVAL_SECONDS = 300
SENTIMENT_LOOKBACK_HOURS          = 4

SENTIMENT_WEIGHTS = {
    "reddit":        0.30,
    "telegram":      0.20,
    "news":          0.20,
    "fear_greed":    0.15,
    "google_trends": 0.15,
}

SENTIMENT_BOOST_THRESHOLD  = 70   # test: 60–80
SENTIMENT_BLOCK_THRESHOLD  = 30   # test: 20–40
SENTIMENT_BOOST_AMOUNT     = 15   # test: 5–20
SENTIMENT_SUPPRESS_AMOUNT  = 20   # test: 10–25
SENTIMENT_VELOCITY_WINDOW  = 2    # Hours
SENTIMENT_VELOCITY_BOOST   = True

REDDIT_SUBREDDITS  = [
    "cryptocurrency", "bitcoin", "ethtrader",
    "altcoin", "CryptoMarkets", "solana",
]
REDDIT_POST_LIMIT  = 100

TELEGRAM_CHANNELS  = [
    "crypto_news_channel", "bitcoin_signals", "altcoin_alerts",
]

NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]

# ══════════════════════════════════════════════════════════════════════════════
# NEWS GUARD
# ══════════════════════════════════════════════════════════════════════════════

NEWS_GUARD_ENABLED          = True
NEWS_GUARD_LOOKBACK_MINUTES = 60

NEWS_GUARD_BLOCK_KEYWORDS = [
    "hack", "exploit", "breach", "stolen", "rug pull",
    "SEC", "ban", "shutdown", "bankrupt", "insolvent",
    "FOMC", "CPI", "rate decision", "fed meeting",
]
NEWS_GUARD_WARN_KEYWORDS = [
    "investigation", "lawsuit", "regulation", "crackdown",
    "whale", "dump", "selloff", "liquidation cascade",
]
NEWS_GUARD_PENALTY_BLOCK = 999   # Effectively blocks signal
NEWS_GUARD_PENALTY_WARN  = 20

# ══════════════════════════════════════════════════════════════════════════════
# BTC CORRELATION GUARD
# ══════════════════════════════════════════════════════════════════════════════

BTC_GUARD_ENABLED        = True
BTC_CRASH_PCT            = 2.0   # test: 1.5–3.0
BTC_CRASH_WINDOW_MINUTES = 30    # test: 15–60
BTC_GUARD_SCORE_PENALTY  = 25    # test: 15–35
# Heartbeat-loop snapshot window — match BTC_CRASH_WINDOW_MINUTES so the
# guard sees the same horizon end-to-end. Allow a small drift below the
# target before reusing the older snapshot.
BTC_GUARD_LOOKBACK_MINUTES = 30  # test: 15-60
BTC_GUARD_LOOKBACK_DRIFT_MINUTES = 2  # test: 0-5 (acceptable snapshot age slack)

# Skip-reason constants used in logs + Signal.skip_reason column.
SENTIMENT_HARD_BLOCK_SKIP_REASON = "SENTIMENT_HARD_BLOCK"
MACRO_HARD_BLOCK_SKIP_REASON     = "MACRO_HARD_BLOCK"
PRE_EVENT_PAUSE_SKIP_REASON      = "PRE_EVENT_PAUSE"

# ══════════════════════════════════════════════════════════════════════════════
# POSITION CORRELATION GUARD
# ══════════════════════════════════════════════════════════════════════════════

CORR_GUARD_ENABLED    = True
CORR_HIGH_THRESHOLD   = 0.80   # test: 0.70–0.90
CORR_PENALTY_AMOUNT   = 15     # test: 10–25
CORR_BLOCK_THRESHOLD  = 0.95
CORR_LOOKBACK_HOURS   = 24
CORR_ARB_EXEMPT       = True

# ══════════════════════════════════════════════════════════════════════════════
# SESSION TIMING
# ══════════════════════════════════════════════════════════════════════════════

SESSION_SCORING_ENABLED = True

# All times UTC. Adjust boosts based on what sim data shows.
SESSION_WINDOWS = {
    "london_open":  {"start": "07:00", "end": "10:00", "score_boost":  8},
    "london_ny":    {"start": "13:00", "end": "17:00", "score_boost": 12},
    "ny_afternoon": {"start": "17:00", "end": "20:00", "score_boost":  4},
    "asia_open":    {"start": "00:00", "end": "02:00", "score_boost":  2},
    "dead_zone":    {"start": "02:00", "end": "06:00", "score_boost": -15},
}

# ══════════════════════════════════════════════════════════════════════════════
# RISK MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

MAX_POSITION_SIZE_PCT   = 0.03   # test: 0.01–0.05
MAX_OPEN_POSITIONS      = 3      # test: 1–5
DEFAULT_STOP_LOSS_PCT   = 0.01   # test: 0.005–0.02
DEFAULT_TAKE_PROFIT_PCT = 0.02   # test: 0.01–0.04
MIN_RISK_REWARD_RATIO   = 1.5    # test: 1.0–2.5

TRAILING_STOP_ENABLED  = False
TRAILING_STOP_PCT      = 0.008   # test: 0.005–0.015
TRAILING_STOP_ACTIVATE = 0.01    # Activate after N% profit

# ══════════════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKERS
# ══════════════════════════════════════════════════════════════════════════════

CIRCUIT_BREAKERS = {
    "daily_loss": {
        "enabled":       True,
        "threshold_pct": 2.0,    # test: 1.0–4.0
        "action":        "halt",
    },
    "consecutive_loss": {
        "enabled": True,
        "count":   3,            # test: 2–5
        "action":  "pause",
    },
    "drawdown": {
        "enabled":       True,
        "threshold_pct": 5.0,    # test: 3.0–10.0
        "action":        "halt",
    },
    "rapid_loss": {
        "enabled":         True,
        "threshold_pct":   1.5,  # test: 0.5–2.0
        "window_minutes":  60,
        "action":          "pause",
    },
    "win_rate_floor": {
        "enabled":       False,  # Enable once you have trade history
        "min_trades":    10,
        "threshold_pct": 35.0,   # test: 30–50
        "action":        "pause",
    },
}

CIRCUIT_BREAKER_PAUSE_MINUTES        = 30   # 0 = manual resume only
CIRCUIT_BREAKER_HALT_REQUIRES_MANUAL = True

# Persist an agent_events row on every shutdown (sigint / `q` / kill).
# Off by default in tests via monkeypatch; on for normal runs.
SHUTDOWN_LOG_EVENT = True

# ══════════════════════════════════════════════════════════════════════════════
# EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

ORDER_TYPE             = "limit"   # limit | market
LIMIT_SLIPPAGE_PCT     = 0.05
ORDER_TIMEOUT_SECONDS  = 30
ORDER_RETRY_ON_FAIL    = True
ORDER_RETRY_COUNT      = 2

# ══════════════════════════════════════════════════════════════════════════════
# CLAUDE AGENT
# ══════════════════════════════════════════════════════════════════════════════

CLAUDE_MODEL       = "claude-sonnet-4-5"
CLAUDE_MAX_TOKENS  = 1024
CLAUDE_TEMPERATURE = 0.2   # test: 0.1–0.4

CLAUDE_CONTEXT = {
    "include_regime":        True,
    "include_hurst":         True,
    "include_ofi":           True,
    "include_sentiment":     True,
    "include_session":       True,
    "include_news_guard":    True,
    "include_correlation":   True,
    "include_past_similar":  True,
    "include_self_review":   True,
    "past_similar_lookback": 30,
    "past_similar_count":    5,
    "self_review_count":     3,
}

CLAUDE_WINDOW_BRIEF = {
    "include_regime_all_pairs":    True,
    "include_strategy_map":        True,
    "include_sentiment_overview":  True,
    "include_expected_conditions": True,
    "include_risk_assessment":     True,
    "include_recommended_pairs":   True,
    "max_recommended_pairs":       5,
}

SELF_REVIEW_ENABLED    = True
SELF_REVIEW_MAX_TOKENS = 512

CLAUDE_MAX_DAILY_COST_USD = 2.00   # Alert only — does not halt trading

# ══════════════════════════════════════════════════════════════════════════════
# PREDICTIVE ENGINE (phase 2)
# ══════════════════════════════════════════════════════════════════════════════

PREDICTIVE_ENABLED              = False
PREDICTIVE_MIN_TRAINING_TRADES  = 50
PREDICTIVE_WIN_PROB_THRESHOLD   = 0.55
PREDICTIVE_RETRAIN_EVERY_N_DAYS = 7
PREDICTIVE_MODEL_PATH           = DATA_DIR / "model.joblib"
PREDICTIVE_BOOST_STRONG         = 10
PREDICTIVE_PENALTY_WEAK         = 15

# ══════════════════════════════════════════════════════════════════════════════
# MULTI-AGENT COORDINATOR
# ══════════════════════════════════════════════════════════════════════════════

# Total starting equity for the portfolio. Drives CircuitBreakerState
# baseline + falls back as initial value when the DB has no prior
# portfolio_snapshot to recover from (see core/bot.py startup).
STARTING_CAPITAL     = 1000.0    # test: 200–10000

# Per-agent capital allocation (USD). Sum should match STARTING_CAPITAL;
# the coordinator warns at startup if their sum exceeds total
# EXCHANGE_BALANCES.
SIGNAL_AGENT_CAPITAL = 400.0     # test: 100–800
ARB_AGENT_CAPITAL    = 600.0     # test: 100–1000  (sum of per-exchange budgets)

# Arb engine reserves this much per exchange leg — caps how aggressive
# any single venue can get. 6 configured exchanges × this = ARB_AGENT_CAPITAL.
ARB_CAPITAL_PER_EXCHANGE = 100.0   # test: 50–200

# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY → EXCHANGE ROUTING
# ══════════════════════════════════════════════════════════════════════════════
# Encodes which exchanges are approved for each strategy type. Scalping
# is fee-sensitive — only near-zero-fee venues are listed. Adding a new
# exchange to a strategy = one line change here, no code changes (the
# agent reads this map directly).
STRATEGY_EXCHANGE_MAP = {
    "scalp":     ["mexc", "bitget"],
    "arb":       ["kraken", "bybit", "bitget", "bitstamp", "gateio", "bitfinex"],
    "momentum":  ["binance", "bybit", "kraken"],
    "reversion": ["binance", "bybit", "kraken"],
    "sweep":     ["binance", "bybit"],
}

# ══════════════════════════════════════════════════════════════════════════════
# SCALPING AGENT (agents/scalping_agent.py)
# ══════════════════════════════════════════════════════════════════════════════
# SCALP_CAPITAL = 0.0 → observation mode only. No orders placed.
# Activate: set to e.g. 50.0 AFTER DB confirms edge (win_rate > 52%,
# avg_net_bps > 0). Also requires MEXC API key in keys.env.

SCALP_CAPITAL             = 0.0     # test: 0-100   ($0 = observation only)
SCALP_PAIRS               = ["BTC/USDT", "ETH/USDT"]

# Fee-aware profit targeting — TP/SL are computed dynamically, not fixed.
SCALP_NET_PROFIT_TARGET_BPS  = 3.0   # test: 2.0-8.0   (net profit after fees)
SCALP_RR_RATIO               = 1.6   # test: 1.3-2.5   (tp_bps / sl_bps)
SCALP_MAX_BREAKEVEN_WIN_RATE = 0.65  # test: 0.55-0.75 (block if math needs >65% wr)

# Fee management
SCALP_FEE_DEFAULT_BPS     = 10.0   # fallback if CCXT lookup fails (conservative)
SCALP_FEE_OVERRIDES       = {      # overrides CCXT data where known to be wrong
    "mexc":   {"maker": 0.0,  "taker": 0.0},    # 0% confirmed standard rate
    "bitget": {"maker": 1.0,  "taker": 1.0},    # 0.01% confirmed
}

# OFI signal parameters
SCALP_OFI_Z_ENTRY         = 1.5    # test: 1.0-2.5   (z-score entry threshold)
SCALP_OFI_Z_EXIT          = 0.3    # test: 0.1-0.7   (OFI exhaustion exit)
SCALP_OFI_Z_CONTRADICT    = -0.8   # test: -0.4 to -1.5 (OFI flip exit)
SCALP_OFI_PERSIST_TICKS   = 3      # test: 2-6       (consecutive ticks above threshold)
SCALP_OFI_LEVELS          = 5      # test: 1-10      (book depth levels)
SCALP_OFI_WINDOW_SEC      = 20     # test: 10-40     (bucket accumulation window seconds)
SCALP_ZSCORE_WINDOW       = 80     # test: 40-150    (rolling z-score normalisation periods)

# Execution parameters
SCALP_MAX_SPREAD_BPS      = 3.0    # test: 1.5-6.0   (max bid-ask spread bps)
SCALP_MAX_HOLD_SEC        = 180    # test: 60-300    (force exit after N seconds)
SCALP_SCAN_INTERVAL_MS    = 100    # test: 50-500    (main loop interval ms)
SCALP_MAX_CONCURRENT      = 1      # test: 1-3       (max open scalp positions)
SCALP_POSITION_SIZE_USD   = 25.0   # test: 10-50     (per-trade size USD)

# Circuit breakers
SCALP_DAILY_LOSS_HALT     = 5.0    # test: 2-10      (daily loss halt USD)
SCALP_CONSEC_LOSS_PAUSE   = 4      # test: 3-6       (consecutive loss pause count)

# Depth weights for multi-level OFI (exponential decay ~λ=0.3).
SCALP_DEPTH_WEIGHTS       = {0: 1.0, 1: 0.70, 2: 0.50, 3: 0.35, 4: 0.25}

# Portfolio-level circuit breakers — sit on TOP of per-agent breakers.
# Per-agent CBs (in settings.CIRCUIT_BREAKERS) fire first; these catch
# the case where every agent drifts just under its own limit but together
# they punch through a portfolio threshold.
PORTFOLIO_DAILY_LOSS_HALT_PCT  = 3.0     # test: 2.0–5.0
PORTFOLIO_MAX_EXPOSURE_PCT     = 80.0    # test: 60–95
PORTFOLIO_MONITOR_INTERVAL_SEC = 30      # test: 15–120

# Kill switch
KILL_SWITCH_CONFIRM_REQUIRED = False     # set True in live mode for safety

# ══════════════════════════════════════════════════════════════════════════════
# PLUGGABLE SENTIMENT AGGREGATOR
# ══════════════════════════════════════════════════════════════════════════════
# Composite scores in this system live on -100..+100. The aggregator
# converts to the 0–100 dict scanners consume in core/bot.py.

SENTIMENT_COMPOSITE_FLOOR    = -60     # test: -50 to -70   (session-block threshold)
SENTIMENT_FEAR_GREED_FLOOR   = 15      # test: 10–25        (extreme-fear cutoff)
SENTIMENT_BTC_GUARD_PCT      = -2.0    # test: -1.5 to -3.0 (30m BTC drop trigger)
SENTIMENT_BTC_GUARD_PENALTY  = -15     # test: -10 to -25   (penalty on alt signals)
SENTIMENT_NEWS_GUARD_ENABLED = True
SENTIMENT_REFRESH_INTERVAL_SEC = 300   # test: 60–900       (min between get_current() refreshes)
SENTIMENT_HTTP_TIMEOUT_SEC   = 10      # test: 5–30

# Per-source weights — override the class defaults at __init__ time
SENTIMENT_WEIGHT_FEAR_GREED    = 0.4
SENTIMENT_WEIGHT_CRYPTOPANIC   = 0.25
SENTIMENT_WEIGHT_REDDIT        = 0.2
SENTIMENT_WEIGHT_GOOGLE_TRENDS = 0.1
SENTIMENT_WEIGHT_TELEGRAM      = 0.05

# ══════════════════════════════════════════════════════════════════════════════
# PLUGGABLE DATA SOURCES (macro / on-chain / fx / etc.)
# ══════════════════════════════════════════════════════════════════════════════
# Same plugin pattern as the sentiment aggregator: each source publishes
# DataPoints into a shared cache; the dashboard, quality gate, and any agent
# can pull (data_sources.<id>.get_*) or subscribe (pub/sub on DataPoint keys).

# ─── Refresh intervals ──────────────────────
COINGLASS_REFRESH_SEC     = 300     # test: 60-600
COINGECKO_REFRESH_SEC     = 300     # test: 60-900
FRED_REFRESH_SEC          = 3600    # test: 1800-7200
ALPHA_VANTAGE_REFRESH_SEC = 900     # test: 300-1800
FRANKFURTER_REFRESH_SEC   = 3600    # test: 1800-7200

# ─── CoinGecko tier ─────────────────────────
# Public free + demo key both live on api.coingecko.com. Set True to
# route through pro-api.coingecko.com (changes the auth header name too).
COINGECKO_USE_PRO = False           # test: True/False

DATA_SOURCES_REFRESH_LOOP_SEC = 60  # test: 30-300  (top-level refresh_all tick)
DATA_SOURCES_HTTP_TIMEOUT_SEC = 10  # test: 5-30

# ─── Watch lists ────────────────────────────
COINGLASS_WATCH_PAIRS = [
    "BTC/USDT",  "ETH/USDT",  "SOL/USDT",  "BNB/USDT",
    "AVAX/USDT", "LINK/USDT", "DOT/USDT",  "MATIC/USDT",
    "NEAR/USDT", "APT/USDT",  "INJ/USDT",  "ARB/USDT",
    "OP/USDT",   "SUI/USDT",  "ATOM/USDT", "ADA/USDT",
]

ALPHA_VANTAGE_SYMBOLS = [
    # VIX intentionally excluded — Alpha Vantage's GLOBAL_QUOTE doesn't
    # cover non-tradeable indices. VIX lives at FRED (series VIXCLS).
    "SPY", "QQQ", "GLD", "TLT",
]

FRED_SERIES = [
    "CPIAUCSL", "DGS10",  "DGS2",  "DGS30",
    "DFF",      "M2SL",   "UNRATE", "T10Y2Y",
    "VIXCLS",   # CBOE VIX (daily, EOD)
]

FRANKFURTER_PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF",
    "USD/SEK",   # needed for an accurate DXY proxy (~4% basket weight)
    "AUD/USD", "USD/CAD", "NZD/USD",
]

# ─── Risk regime thresholds ─────────────────
DATA_VIX_RISK_ON_MAX     = 15      # test: 12-18
DATA_VIX_RISK_OFF_MIN    = 25      # test: 22-30
DATA_VIX_CRISIS_MIN      = 35      # test: 30-40
DATA_DXY_STRONG_THRESHOLD = 105    # test: 102-108
DATA_DXY_WEAK_THRESHOLD   = 95     # test: 92-98

# ─── Rate limiting ──────────────────────────
# Alpha Vantage free tier is 25/day and ~1/sec burst. Leave headroom on
# the daily budget and pace inter-call sleeps to dodge the per-sec block.
ALPHA_VANTAGE_DAILY_CALL_BUDGET = 20   # test: 10-25
ALPHA_VANTAGE_PACE_SEC          = 1.3  # test: 1.1-3.0  (sleep between calls)

# ─── Base URLs (free, no-key APIs unless noted) ─────────────
# Surfaced as settings so per-host failovers and version bumps don't
# require touching source files. The Frankfurter "app" host 301-redirects
# to "dev/v1" (legacy path) — we hit the redirect target directly.
FRANKFURTER_BASE_URL      = "https://api.frankfurter.dev/v1"
WORLD_BANK_BASE_URL       = "https://api.worldbank.org/v2"
IMF_DATAMAPPER_URL        = "https://www.imf.org/external/datamapper/api/v1"
ECB_BASE_URL              = "https://data-api.ecb.europa.eu/service/data"
# US Treasury exposes a daily-rates CSV at this base path; we read the
# current month and pull the most recent row. Mostly redundant with FRED.
US_TREASURY_BASE_URL      = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/all"
# CFTC publishes the disaggregated COT report via a Socrata-style
# resource endpoint; the user-provided "exports/json" path is a bulk
# dataset download, not queryable.
CFTC_BASE_URL             = "https://publicreporting.cftc.gov/resource/jun7-fc8e.json"
BINANCE_FUTURES_BASE_URL  = "https://fapi.binance.com"
BYBIT_BASE_URL            = "https://api.bybit.com"
CRYPTOCOMPARE_BASE_URL    = "https://min-api.cryptocompare.com/data"

# ─── New-source refresh intervals + watch lists ─────────────
CRYPTOCOMPARE_REFRESH_SEC = 300   # test: 60-900
CRYPTOCOMPARE_FSYMS       = ["BTC", "ETH", "SOL", "BNB", "XRP"]
CRYPTOCOMPARE_TSYM        = "USD"

BINANCE_FUTURES_REFRESH_SEC = 60  # test: 15-300
BINANCE_FUTURES_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

BYBIT_REFRESH_SEC         = 60    # test: 15-300
BYBIT_SYMBOLS             = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

CFTC_REFRESH_SEC          = 3600 * 6   # COT is weekly; check every 6h
CFTC_CONTRACT             = "BITCOIN - CHICAGO MERCANTILE EXCHANGE"

WORLD_BANK_REFRESH_SEC    = 86400      # yearly data — daily refresh ample
WORLD_BANK_COUNTRIES      = ["USA", "EUU", "CHN", "JPN", "GBR"]
WORLD_BANK_INDICATORS = {
    "gdp_growth_pct":  "NY.GDP.MKTP.KD.ZG",
    "inflation_pct":   "FP.CPI.TOTL.ZG",
    "unemployment_pct":"SL.UEM.TOTL.ZS",
}

IMF_REFRESH_SEC           = 86400      # quarterly data
IMF_COUNTRIES             = ["USA", "EUR", "CHN", "JPN", "GBR"]
IMF_INDICATORS = {
    "gdp_per_capita":     "NGDPDPC",
    "inflation_yoy_pct":  "PCPIPCH",
    "unemployment_pct":   "LUR",
}

ECB_REFRESH_SEC           = 3600       # daily series
# (dataflow, key, metric_name). MRR_RT.LEV is the main refinancing rate.
ECB_SERIES = [
    ("FM",  "D.U2.EUR.4F.KR.MRR_RT.LEV", "refinancing_rate"),
    ("FM",  "B.U2.EUR.4F.KR.DFR.LEV",    "deposit_facility_rate"),
    ("EXR", "D.USD.EUR.SP00.A",          "eur_usd_spot"),
]

US_TREASURY_REFRESH_SEC   = 3600

BTC_FUTURES_FSYM          = "BTC"      # convenience constant for cross-source BTC accessors

# ─── Macro modifiers (quality gate, legacy direct-from-data_sources) ─
# Used as a fallback if macro/ module is not active. The macro module's
# step-ladder modifier supersedes these once macro_monitor is wired.
MACRO_RISK_OFF_PENALTY        = -5     # test: -10 to -2
MACRO_CRISIS_PENALTY          = -20    # test: -25 to -15
MACRO_DXY_STRONG_LONG_PENALTY = -5     # test: -10 to -2
MACRO_YIELD_INVERTED_PENALTY  = -3     # test: -6 to -1

# ══════════════════════════════════════════════════════════════════════════════
# MACRO REGIME MONITOR (macro/)
# ══════════════════════════════════════════════════════════════════════════════
# Reads from data_sources singleton + calendar plugin, computes a regime
# (scenario label + numeric score + dimensional flags), exposes a step-
# ladder signal modifier mirroring the sentiment aggregator's shape.

MACRO_REFRESH_INTERVAL_SEC     = 300    # test: 60-600
MACRO_PRE_EVENT_PAUSE_MINUTES  = 30     # test: 15-60
MACRO_CONFIDENCE_STALE_HOURS   = 4      # test: 1-12
MACRO_RATE_LOOKBACK_DAYS       = 90     # test: 30-180 (rate-env change window)

# Dimensional thresholds — used to label DollarStrength, VolRegime,
# RateEnvironment. These mirror DATA_VIX_* / DATA_DXY_* with macro-tuned
# defaults; the macro module reads MACRO_* exclusively.
MACRO_DXY_STRONG               = 104.0  # test: 102-106
MACRO_DXY_WEAK                 = 99.0   # test: 97-101
MACRO_VIX_CALM                 = 15.0   # test: 12-18
MACRO_VIX_ELEVATED             = 25.0   # test: 20-30
MACRO_VIX_CRISIS               = 35.0   # test: 30-40
MACRO_YIELD_CURVE_INVERSION    = 0.0    # test: -0.5 to 0.5  (10y - 2y)
MACRO_RATE_CHANGE_TIGHTENING   = 0.25   # test: 0.1-0.5      (fed funds Δ%)
MACRO_RATE_CHANGE_EASING       = -0.25  # test: -0.5 to -0.1

# Inflation bands (CPI YoY %). 2.5% is roughly the Fed's tolerance band
# top; > 4% is the territory that historically forces tightening cycles.
MACRO_INFLATION_LOW            = 2.5    # test: 1.5-3.5
MACRO_INFLATION_HIGH           = 4.0    # test: 3.0-6.0

# Composite-score component weights — must sum to 1.0.
MACRO_WEIGHT_VIX               = 0.30   # test: 0.2-0.4
MACRO_WEIGHT_DOLLAR            = 0.25   # test: 0.15-0.35
MACRO_WEIGHT_YIELD_CURVE       = 0.20   # test: 0.1-0.3
MACRO_WEIGHT_RATE_ENV          = 0.15   # test: 0.1-0.2
MACRO_WEIGHT_INFLATION         = 0.10   # test: 0.05-0.15

# ══════════════════════════════════════════════════════════════════════════════
# BOT LOOP TIMING
# ══════════════════════════════════════════════════════════════════════════════

BOT_LOOP_INTERVAL_SEC             = 30      # test: 10–120
HEARTBEAT_INTERVAL_SEC            = 300     # test: 60–600
POSITION_WATCHER_INTERVAL_SEC     = 5       # test: 2–15
FUTURE_PRICE_TRACKER_INTERVAL_SEC = 3600    # test: 1800–7200
SELF_REVIEW_EVERY_N_TRADES        = 5       # test: 3–10

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING & UI
# ══════════════════════════════════════════════════════════════════════════════

LOG_LEVEL    = "INFO"
LOG_TO_FILE  = True
LOG_ROTATION = "midnight"

UI_REFRESH_RATE          = 1.0
UI_MAX_TRADE_LOG_ROWS    = 20
UI_SHOW_CLAUDE_REASONING = True
UI_SHOW_REGIME_DETAILS   = True
UI_SHOW_OFI_BARS         = True
UI_SHOW_HURST_BARS       = True
UI_DEFAULT_TAB           = "overview"
