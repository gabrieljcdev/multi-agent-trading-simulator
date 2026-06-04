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
APPROVAL_MODE = "autonomous"

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

# Simulated per-venue cash ledger (sim mode). Spans every venue the funds
# touch and sums to STARTING_CAPITAL (5000) so the fund allocations are
# fully backed in sim. NOTE: this is a per-venue ledger, NOT a per-fund one
# — funds share venues (MEXC backs scalp + mexc-arb; kraken/bybit back
# signal + arb). Per-fund sizing lives with each agent (e.g. OrderRouter
# sizes the signal agent off FUND_SIGNAL_CAPITAL, not this sum), so a
# bigger ledger never lets one fund risk beyond its own allocation.
#
# Ring-fence coverage (each fund's venue subset ≥ its allocation):
#   signal venues  binance+kraken+bybit              = 2600  ≥ 1600
#   arb venues     kraken+bybit+bitget+bitstamp+
#                  gateio+bitfinex                   = 3400  ≥ 2000
#   mexc-scalp     mexc                              = 1000  ≥  500
#   mexc-arb       mexc                              = 1000  ≥  500
# The 400 reserve is co-located on kraken + bybit (the regulated shared
# venues), 200 each.
EXCHANGE_BALANCES = {
    "binance":   600.0,   # signal-only
    "kraken":   1000.0,   # signal + arb (shared) + 200 reserve
    "bybit":    1000.0,   # signal + arb (shared) + 200 reserve
    "kucoin":      0.0,   # unused
    "bitget":    400.0,   # arb
    "bitstamp":  350.0,   # arb
    "gateio":    350.0,   # arb
    "bitfinex":  300.0,   # arb
    "mexc":     1000.0,   # mexc-scalp 500 + mexc-arb 500 (counterparty-capped)
}

# ══════════════════════════════════════════════════════════════════════════════
# EXCHANGES
# ══════════════════════════════════════════════════════════════════════════════

ENABLED_EXCHANGES  = ["binance", "kraken", "bybit", "kucoin", "mexc"]
MIN_LIQUIDITY_USD  = 50_000     # Minimum order book depth to trade a pair
ORDER_BOOK_DEPTH   = 10         # Levels to stream per side for OFI
ORDER_BOOK_STREAM_PAIRS    = 20    # test: 5-50   (top-N active pairs to stream books for)
ORDER_BOOK_WATCH_TIMEOUT_S = 30.0  # test: 10-60  (max wait for one symbol's book update)
ORDER_BOOK_ERROR_BACKOFF_S = 1.0   # test: 0.5-5  (backoff after a book-stream error — prevents busy-spin)
ORDER_BOOK_MAX_STREAMS_PER_CONN = 24   # test: 12-30  (per-ws-connection subscription cap; MEXC silently drops subs above ~30, so book streams shard across this many symbols per connection)

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
ARB_BASE_POSITION_USD     = 60.0      # test: 10.0–80.0  (base for gap-proportional sizing; ~3% of FUND_ARB_CAPITAL=2000)
ARB_SIZE_MULTIPLIER_CAP   = 4.0       # test: 2.0–6.0    (max gap/threshold scale-up)
ARB_MIN_LIQUIDITY_USD     = 500.0     # test: 250–2000   (sum of top 3 book levels)
ARB_MAX_CONCURRENT        = 3         # test: 1–5
ARB_DAILY_LOSS_HALT_PCT   = 2.0       # test: 1-5    (% of arb fund allocation)
ARB_CONSECUTIVE_LOSS_HALT = 5         # test: 3–10

# Pre-execution capital verification — query both exchange balances via
# CCXT before firing. Buffer keeps a margin above the bare requirement.
ARB_BALANCE_BUFFER_PCT    = 5.0       # test: 2.0–10.0

# Depth-aware slippage model — sim mode applies this to both legs.
#   slippage = base_spread * (size_usd / depth_usd) ** 0.5
# clamped to [min, max]. Replaces the legacy flat ±0.02% sim slippage.
ARB_SLIPPAGE_MIN_PCT      = 0.01      # test: 0.005–0.02
ARB_SLIPPAGE_MAX_PCT      = 0.25      # test: 0.1–0.5

# Funding-rate arb (FundingRateArbEngine). Open spot-long + perp-short
# when funding rate exceeds MIN, close when it drops below EXIT. Same
# circuit-breaker shape as ArbEngine with independent thresholds.
ARB_FUNDING_RATE_MIN_PCT          = 0.05   # test: 0.03–0.10  (per 8h funding)
ARB_FUNDING_RATE_EXIT_PCT         = 0.02   # test: 0.01–0.05
ARB_FUNDING_DAILY_LOSS_HALT_PCT   = 3.0    # test: 1-5    (% of arb fund allocation)
ARB_FUNDING_CONSECUTIVE_LOSS_HALT = 4

# ══════════════════════════════════════════════════════════════════════════════
# FUNDING-RATE ARB AGENT (Phase 1 — observation mode, delta-neutral, binance)
# ══════════════════════════════════════════════════════════════════════════════
# Dedicated agent that scans Binance funding rates via CCXT public endpoints,
# builds a delta-neutral (spot-long + perp-short) opportunity per symbol, and
# logs every would-enter to funding_arb_observations. OBSERVATION MODE is a
# hard gate in Phase 1 — _place is unreachable while FUNDING_OBSERVATION_MODE
# is True. Distinct from the FundingRateArbEngine (ARB_FUNDING_* above): that
# one is a passive rate reader inside the arb fund; this is its own agent
# with its own observation ledger and circuit breakers.

FUNDING_OBSERVATION_MODE       = True       # test: True/False  (NEVER False this phase)
FUNDING_CAPITAL_USD            = 0.0        # test: 0-5000
# Widened from majors-only after 2026-05-29 observation pull showed BTC at
# 4% APR / ETH at 7% — both below the original 12% gate, leaving the obs
# table empty. Altcoin perps (esp. memecoins + L2 governance) routinely
# push 15-30% APR and ALSO produce rich negative-funding inversions
# (reverse_carry variant) the engine now also accepts. All symbols below
# verified live on Binance USD-M perp markets.
FUNDING_SYMBOLS                = [
    "BTC/USDT",  "ETH/USDT",  "SOL/USDT",  "DOGE/USDT",
    "ARB/USDT",  "OP/USDT",   "INJ/USDT",  "TIA/USDT",
    "SUI/USDT",  "APT/USDT",  "AVAX/USDT", "LINK/USDT",
]
FUNDING_SCAN_INTERVAL_SEC      = 60         # test: 30-300
# Threshold is now symmetric — `|funding_apr| >= FUNDING_MIN_APR` admits
# both delta_neutral (positive carry, long-spot + short-perp) and
# reverse_carry (negative carry, long-perp + short-spot) variants. Lowered
# from 0.12 → 0.06 so the more numerous mid-APR opportunities populate
# funding_arb_observations during the soak.
FUNDING_MIN_APR                = 0.06       # test: 0.03-0.30 (now |apr| >= floor)
FUNDING_MIN_OI_MULT            = 10.0       # test: 5-50
FUNDING_MAX_NOTIONAL_USD       = 250.0      # test: 100-5000
FUNDING_MAX_CONCURRENT         = 2          # test: 1-5  (open-concurrency cap; live only)
# How many of a scan's opportunities the OBSERVATION loop persists per tick.
# Decoupled from FUNDING_MAX_CONCURRENT (which bounds in-flight live opens):
# the observation layer must log the WHOLE discovered frontier so long-tail /
# HIP-3 pairs accumulate the repeat per-pair samples their crowding_verdict
# needs to leave UNKNOWN. Discovery is already bounded by
# FUNDING_MAX_DISCOVERED_PAIRS; this is the DB-write safety cap on top. 0 =
# unlimited (log every opp the scan returns).
FUNDING_MAX_OBSERVED_PER_SCAN  = 100        # test: 0, 25, 50, 100, 200  (0 = unlimited)
FUNDING_MAX_OI_FRACTION        = 0.001      # test: 0.0005-0.01
FUNDING_TARGET_LEVERAGE        = 2.0        # test: 1.5-3.0
FUNDING_FLIP_EXIT_APR          = 0.0        # test: -0.05-0.03
FUNDING_BASIS_SIGMA_EXIT       = 1.5        # test: 1.0-3.0
FUNDING_MARGIN_ALERT_RATIO     = 1.5        # test: 1.2-2.0
FUNDING_MAX_HOLD_SEC           = 1209600    # test: 86400-2592000
FUNDING_SIM_SLIPPAGE_PCT       = 0.0002     # test: 0.0001-0.001
FUNDING_DAILY_LOSS_HALT_PCT    = 2.0        # test: 1-5    (% of FUNDING_CAPITAL_USD; 0-alloc → no-op)
FUNDING_CONSECUTIVE_LOSS_HALT  = 4          # test: 3-6

# ── Funding frontier (observation; extends the Phase-1 funding observer) ──
# Adds funding venues (Hyperliquid + room for more), cross-venue carry
# observation, and a long-tail / HIP-3 discovery + openness signal. STILL
# OBSERVATION ONLY: FUNDING_OBSERVATION_MODE stays True, FUNDING_CAPITAL_USD
# stays 0.0, no venue can place an order. These tune what is OBSERVED, never
# whether anything trades.
FUNDING_VENUES_ENABLED            = ["binance", "hyperliquid"]  # test: ["binance"] first, then add hyperliquid
FUNDING_HL_BULK_TTL_SEC           = 30     # test: 10, 30, 60  (Hyperliquid bulk-fetch cache TTL; rate-limit transport detail)

FUNDING_CROSS_VENUE_ENABLED       = True   # test: True, False
FUNDING_FUNDING_INTERVAL_RISK_BPS = 1.5    # test: 0.5, 1.0, 1.5, 3.0  (buffer per interval for funding-flip risk)
FUNDING_REQUIRE_MAKER_FEES        = True   # test: True, False  (use maker fee in break-even; flag if only taker available)

FUNDING_LONGTAIL_ENABLED          = True   # test: True, False
FUNDING_MAX_DISCOVERED_PAIRS      = 40     # test: 10, 25, 40, 80
FUNDING_DECAY_WINDOW_HOURS        = 48     # test: 24, 48, 96  (rolling window for spread-decay slope)
FUNDING_CROWDED_DECAY_BPS_DAY     = 8.0    # test: 4, 8, 15    (>= this daily compression → CROWDED)
FUNDING_OPEN_MAX_DECAY_BPS_DAY    = 2.0    # test: 1, 2, 4     (<= this → OPEN, if age/OI agree)
FUNDING_YOUNG_PAIR_MAX_AGE_DAYS   = 21     # test: 14, 21, 30  (the richness-window age cutoff)

# Pairs the arb engine watches. Distinct from signal-track FALLBACK_PAIRS.
ARB_WATCH_PAIRS = [
    "BTC/USDT",  "ETH/USDT",  "SOL/USDT",  "BNB/USDT",
    "AVAX/USDT", "LINK/USDT", "DOT/USDT",  "MATIC/USDT",
    "NEAR/USDT", "APT/USDT",  "INJ/USDT",  "ARB/USDT",
    "OP/USDT",   "SUI/USDT",  "ATOM/USDT", "ADA/USDT",
]

# Taker fee per leg per exchange (fractional, not %) — arb takes liquidity.
# NB (2026-06-03): bitget's public symbols endpoint reports 0.2% maker/taker on
# majors (0.1% on some alts) — the old 0.01% "game changer" entry was wrong by
# 20x (scripts/exchange_fee_check.py bitget). ARB_MIN_GAP_PCT needs no change:
# it is a NET-of-fees floor and these per-leg fees are subtracted before it
# applies — but bitget-leg arbs now correctly need ~0.49%+ gross to clear it.
ARB_FEE_MAP = {
    "bitget":   0.0020,    # 0.20% — verified public standard rate (was wrongly 0.01%)
    "kraken":   0.0026,
    "bitstamp": 0.0050,
    "gateio":   0.0020,
    "bitfinex": 0.0020,
    "bybit":    0.0010,
    "mexc":     0.0005,    # 0.05% taker — verified (scripts/mexc_fee_check.py); arb takes liquidity so taker applies, not the 0% maker
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

# Max seconds to wait for a graceful teardown (coordinator.stop + web server)
# on SIGINT/SIGTERM before forcing task cancellation. Bounds the shutdown so a
# stuck ws close can't leave the process hanging until SIGKILL.
SHUTDOWN_TIMEOUT_SEC = 15   # test: 5-30

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

# ── Ring-fenced fund architecture ───────────────────────────────────────
# Ring-fenced funds, each an independent capital pool mapped 1:1 to an agent.
# Exchanges may be SHARED across funds, but capital is NEVER shared: a loss
# in one fund cannot draw from another, profits stay in their own fund, and
# circuit breakers are per-fund. MEXC is shared between MEXC-scalp and MEXC-arb
# (un-mitigated counterparty risk → both pools stay capped per
# build_balance_agent.md). XCHAIN + FUNDING stay observation-only this soak.
FUND_SIGNAL_CAPITAL     = 1600.0  # test: 0–3000  signal agent — binance/bybit/kraken
FUND_ARB_CAPITAL        = 2000.0  # test: 0–4000  cross-exchange arb — kraken/bybit/bitget/bitstamp/gateio/bitfinex
FUND_MEXC_SCALP_CAPITAL = 500.0   # test: 0–1000  scalping, MEXC only (observation until SCALP_CAPITAL>0)
FUND_MEXC_ARB_CAPITAL   = 500.0   # test: 0–1000  MEXC-only arb (counterparty-capped; un-wired until soak data justifies a dedicated agent)
FUND_XCHAIN_CAPITAL     = 0.0     # test: 0       observation-only this soak — see XCHAIN_CAPITAL (line ~932)
FUND_FUNDING_CAPITAL    = 0.0     # test: 0       observation-only this soak — see FUNDING_CAPITAL_USD (line ~264)

# Per-fund daily-loss halt: each fund halts independently at this % of its
# OWN size. Enforced by Coordinator._check_fund_circuit_breakers, alongside
# the portfolio CB.
FUND_DAILY_LOSS_HALT_PCT = 10.0   # test: 5–20

# Total starting equity for the portfolio — sum of all FUND_* + uncommitted
# reserve. Drives CircuitBreakerState baseline + falls back as initial value
# when the DB has no prior portfolio_snapshot (see core/bot.py startup).
# Reserve = STARTING_CAPITAL - sum(FUND_*) = 400 (8%) held out of deployed
# pool; COMPOUND_RESERVE_PCT logic will absorb this once BalanceAgent ships.
STARTING_CAPITAL     = 5000.0    # test: 200–10000   (sum(FUND_*) = 4600; +400 reserve)

# Legacy aliases — existing code/tests read these names. Pointed at the
# fund constants so there is a single source of truth for each pool.
SIGNAL_AGENT_CAPITAL = FUND_SIGNAL_CAPITAL   # test: 100–800
ARB_AGENT_CAPITAL    = FUND_ARB_CAPITAL      # test: 100–1000

# Arb engine reserves this much per exchange leg — caps how aggressive any
# single venue can get.
ARB_CAPITAL_PER_EXCHANGE = 100.0   # test: 50–200

# ══════════════════════════════════════════════════════════════════════════════
# BALANCE AGENT (agents/balance_agent.py)
# ══════════════════════════════════════════════════════════════════════════════
# Operational agent. Owns the bot's capital position across funds × exchanges,
# compounds realised profit by scaling position sizes, and rebalances inventory
# under cost-derived Miller-Orr bands. Sim-first; live ccxt.withdraw is gated
# behind REBALANCE_LIVE_ENABLED, mirroring SIM_MODE discipline.
#
# Layers (strictly separated): policy → planner → rails, with safety rails
# wrapping the planner's output and dispatch. Adding a fund, exchange, policy,
# or rail touches exactly one layer.

# ── Feature flags ─────────────────────────────────────────────────────────
REBALANCE_LIVE_ENABLED            = False  # test: False         (hard gate; live ccxt.withdraw)
BALANCE_STRICT_OPEN_POSITION_BLOCK = True   # test: True/False    (rail 3; floor rail 2 is always-on)

# ── Compounding / sizing (start conservative; raise with data confidence) ──
KELLY_FRACTION         = 0.25     # test: 0.10-0.50  (fraction of full Kelly; never use full)
COMPOUND_RESERVE_PCT   = 0.05     # test: 0.02-0.15  (uncommitted buffer held out of the pool)

# ── Allocation policy ─────────────────────────────────────────────────────
ALLOCATION_CONFIDENCE  = 0.0      # test: 0.0-1.0    (0 = risk-parity, 1 = growth-optimal)
# Per-(fund, exchange) capacity ceiling in USD. MEXC nodes carry a hard cap
# (un-mitigated counterparty risk; no OES). Empty default → planner uses
# +inf for every cell, i.e. no capacity ceiling until measured book depth
# is wired in.
FUND_CAPACITY_CEILINGS_USD: dict = {}   # test: tune from depth data; MEXC nodes carry a hard cap

# ── Transfer minimisation ─────────────────────────────────────────────────
INTERNALIZE_WINDOW_S   = 300      # test: 60-1800    (wait for self-correction before transferring)
REBALANCE_DAILY_LIMIT  = 3        # test: 1-10       (max physical rebalances per UTC day)
# Control band is DERIVED, not chosen:
#   spread = (0.75 * transfer_cost * depletion_variance / opportunity_cost)^(1/3)
# depletion_variance is measured from fund_capital_efficiency; opportunity_cost
# from per-fund return-on-deployed. Do NOT add a band-width constant.

# ── Network routing (REQUIRED per-route map; identifiers differ per exchange) ─
# Map (from_exchange, to_exchange, asset) -> ordered [network_id...] (cheapest
# first). Seeded from ccxt.fetch_currencies() / fetch_deposit_withdraw_fees()
# in a periodic refresh; empty default keeps the live cex rail dormant until
# operator-pinned routes land. Mirrors WITHDRAWAL_ROUTES discipline.
WITHDRAWAL_ROUTES: dict = {}      # test: seeded from ccxt; live cex rail no-ops with empty map

# ── Sim realism + UX ──────────────────────────────────────────────────────
SIM_WITHDRAWAL_FEE_USD     = 1.0     # test: 0.04-1.6   (simulated per-transfer fee, route-dependent live)

# GreedyNetPlanner internalize step: a target whose |drift_pct| exceeds
# this hint is treated as STRUCTURAL (the strategy can't self-correct);
# below the hint, we let the strategy mop up the drift inside its own
# window. NOT the Miller-Orr band — band stays runtime-derived.
BALANCE_STRUCTURAL_DRIFT_HINT = 0.20   # test: 0.10-0.40
SIM_TRANSFER_DELAY_S       = 600     # test: 60-7200    (simulated in-transit time, sim_rail)
SIM_REBALANCE_FAILURE_RATE = 0.0     # test: 0.0-0.10   (inject failures to exercise auto-pause)
REBALANCE_CONFIRM_WINDOW_S = 3       # test: 2-30       (/action/rebalance arm→confirm window)

# Web UI v2 — rebalance arm token timeout. The server holds the token in
# memory only; expiry is independent of the confirm-click window so an
# operator that armed but then walked away gets a stale-token error
# rather than a successful confirm from a forgotten browser tab.
REBALANCE_ARM_TIMEOUT_S = 10         # test: 5-30

# Web UI v2 — operator-gated rebalance dispatch.
#   False : _scan_once() buffers targets/bands/transfers only; the operator
#           drives every real move via /action/rebalance confirm. Required
#           for the v2 web UI flow.
#   True  : auto-dispatch at end of _scan_once() (legacy). Use only when no
#           operator is in the loop and you trust the policy + planner to
#           move capital unattended.
BALANCE_AUTO_DISPATCH = False        # test: True/False

# Web UI v2 — fallback thresholds the HTML reads to colour panels.
# Above the stablecoin benchmark, funding's blended APR renders green; below
# it grey. Above the funding delta tolerance, an open funding position's
# delta_usd renders amber (the leg drifted off neutral).
STABLECOIN_BENCHMARK_APR_PCT = 5.0   # test: 3-8
FUNDING_DELTA_TOLERANCE_USD  = 5.0   # test: 1-20

# Default agent allocation for the BalanceAgent itself — it's operational
# (no alpha, no positions), so its capital_allocation is zero. The fund
# pools it manages live in FUND_*_CAPITAL above.
BALANCE_AGENT_CAPITAL  = 0.0      # test: 0.0       (BalanceAgent is operational, not alpha)
# Scan interval — runs much slower than alpha agents; one cycle = compound
# realised profit + maybe one rebalance.
BALANCE_SCAN_INTERVAL_SEC = 60    # test: 15-300

# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY → EXCHANGE ROUTING
# ══════════════════════════════════════════════════════════════════════════════
# Encodes which exchanges are approved for each strategy type. Scalping
# is fee-sensitive — only near-zero-fee venues are listed. Adding a new
# exchange to a strategy = one line change here, no code changes (the
# agent reads this map directly).
STRATEGY_EXCHANGE_MAP = {
    "scalp":     ["mexc"],
    "arb":       ["kraken", "bybit", "bitget", "bitstamp", "gateio", "bitfinex", "mexc"],
    "momentum":  ["binance", "bybit", "kraken"],
    "reversion": ["binance", "bybit", "kraken"],
    "sweep":     ["binance", "bybit"],
}

# ══════════════════════════════════════════════════════════════════════════════
# SCALPING AGENT (agents/scalping_agent.py)
# ══════════════════════════════════════════════════════════════════════════════
# SCALP_CAPITAL = 0.0 → observation mode only (no orders placed).
# >0 enables execution: in SIM_MODE it places simulated scalp trades sized
# at SCALP_POSITION_SIZE_USD and tracks P&L against this pool. Live
# execution (SIM_MODE=False) is still a follow-up. Set AFTER DB confirms
# edge (win_rate > 52%, avg_net_bps > 0); live also needs MEXC keys.

SCALP_CAPITAL             = 500.0   # test: 0-1000  ($0 = observation; >0 = sim execution)

# Universe = the 96 of 102 candidate pairs that at least one MEXC key is
# API-allowlisted to trade, discovered by probing each key's selfSymbols
# endpoint (2026-05-25). 6 candidates dropped — no key covers them:
# FTM, EOS, MKR, BRETT, HMSTR, NEIROCTO (FTM/EOS/MKR/BRETT aren't
# active MEXC USDT spot markets at all). Every pair here has a route in
# MEXC_PAIR_KEY_MAP below; re-run scripts/mexc_probe.py after changing
# a key's allowlist on MEXC and update both lists together.
SCALP_PAIRS = [
    # ── Key 1 (28) ──
    "BTC/USDT",  "ETH/USDT",  "SOL/USDT",  "XRP/USDT",  "BNB/USDT",
    "DOGE/USDT", "ADA/USDT",  "TON/USDT",  "AVAX/USDT", "LINK/USDT",
    "DOT/USDT",  "LTC/USDT",  "SHIB/USDT", "TRX/USDT",  "NEAR/USDT",
    "UNI/USDT",  "APT/USDT",  "SUI/USDT",  "ARB/USDT",  "OP/USDT",
    "INJ/USDT",  "ATOM/USDT", "PEPE/USDT", "WIF/USDT",  "BONK/USDT",
    "TAO/USDT",  "RENDER/USDT", "FET/USDT",
    # ── Key 2 (27) ──
    "JUP/USDT",  "TIA/USDT",  "SEI/USDT",  "PYTH/USDT", "ONDO/USDT",
    "WLD/USDT",  "FLOKI/USDT","BOME/USDT", "TURBO/USDT","GALA/USDT",
    "SAND/USDT", "MANA/USDT", "AXS/USDT",  "ICP/USDT",  "FIL/USDT",
    "VET/USDT",  "HBAR/USDT", "ALGO/USDT", "XLM/USDT",  "AAVE/USDT",
    "CAKE/USDT", "RUNE/USDT", "GRT/USDT",  "LDO/USDT",  "SNX/USDT",
    "CRV/USDT",  "DYDX/USDT",
    # ── Key 3 (27) ──
    "NOT/USDT",  "DOGS/USDT", "CATI/USDT", "MAJOR/USDT","ORDI/USDT",
    "SATS/USDT", "ENA/USDT",  "ETHFI/USDT","EIGEN/USDT","IO/USDT",
    "ZRO/USDT",  "STRK/USDT", "MANTA/USDT","REZ/USDT",  "BLAST/USDT",
    "PNUT/USDT", "ACT/USDT",  "GOAT/USDT", "MOODENG/USDT","POPCAT/USDT",
    "MOG/USDT",  "LUNC/USDT", "CFX/USDT",  "ROSE/USDT", "JASMY/USDT",
    "HOT/USDT",  "AR/USDT",
    # ── Key 4 (14) ──
    "CHZ/USDT",  "ENJ/USDT",  "MAGIC/USDT","RON/USDT",  "BEAM/USDT",
    "PORTAL/USDT","HNT/USDT", "KAVA/USDT", "EGLD/USDT", "FLOW/USDT",
    "ONE/USDT",  "ZIL/USDT",  "KSM/USDT",  "POL/USDT",
]

# MEXC supports per-key pair allowlists — one account can hold many API
# keys, each restricted to a different subset of pairs. The scalper
# routes each pair through the right key via this map. Key index is
# 1-based and matches the MEXC_KEY_{N}_API_KEY / _SECRET env vars in
# config/keys.env. Pairs not listed here can't be traded on MEXC;
# extend the map (and add the matching env vars) when you provision
# a new key. Populated from each key's MEXC selfSymbols allowlist (the
# real per-key cap, ~13–28 pairs each) via scripts/mexc_probe.py on
# 2026-05-25. Must stay 1:1 with SCALP_PAIRS — every scalp pair needs a
# route. Re-probe and regenerate both when an allowlist changes on MEXC.
MEXC_PAIR_KEY_MAP: dict[str, int] = {
    # ── Key 1 (MEXC_KEY_1_*) ──
    "BTC/USDT": 1,  "ETH/USDT": 1,  "SOL/USDT": 1,  "XRP/USDT": 1,
    "BNB/USDT": 1,  "DOGE/USDT": 1, "ADA/USDT": 1,  "TON/USDT": 1,
    "AVAX/USDT": 1, "LINK/USDT": 1, "DOT/USDT": 1,  "LTC/USDT": 1,
    "SHIB/USDT": 1, "TRX/USDT": 1,  "NEAR/USDT": 1, "UNI/USDT": 1,
    "APT/USDT": 1,  "SUI/USDT": 1,  "ARB/USDT": 1,  "OP/USDT": 1,
    "INJ/USDT": 1,  "ATOM/USDT": 1, "PEPE/USDT": 1, "WIF/USDT": 1,
    "BONK/USDT": 1, "TAO/USDT": 1,  "RENDER/USDT": 1, "FET/USDT": 1,
    # ── Key 2 (MEXC_KEY_2_*) ──
    "JUP/USDT": 2,  "TIA/USDT": 2,  "SEI/USDT": 2,  "PYTH/USDT": 2,
    "ONDO/USDT": 2, "WLD/USDT": 2,  "FLOKI/USDT": 2,"BOME/USDT": 2,
    "TURBO/USDT": 2,"GALA/USDT": 2, "SAND/USDT": 2, "MANA/USDT": 2,
    "AXS/USDT": 2,  "ICP/USDT": 2,  "FIL/USDT": 2,  "VET/USDT": 2,
    "HBAR/USDT": 2, "ALGO/USDT": 2, "XLM/USDT": 2,  "AAVE/USDT": 2,
    "CAKE/USDT": 2, "RUNE/USDT": 2, "GRT/USDT": 2,  "LDO/USDT": 2,
    "SNX/USDT": 2,  "CRV/USDT": 2,  "DYDX/USDT": 2,
    # ── Key 3 (MEXC_KEY_3_*) ──
    "NOT/USDT": 3,  "DOGS/USDT": 3, "CATI/USDT": 3, "MAJOR/USDT": 3,
    "ORDI/USDT": 3, "SATS/USDT": 3, "ENA/USDT": 3,  "ETHFI/USDT": 3,
    "EIGEN/USDT": 3,"IO/USDT": 3,   "ZRO/USDT": 3,  "STRK/USDT": 3,
    "MANTA/USDT": 3,"REZ/USDT": 3,  "BLAST/USDT": 3,"PNUT/USDT": 3,
    "ACT/USDT": 3,  "GOAT/USDT": 3, "MOODENG/USDT": 3,"POPCAT/USDT": 3,
    "MOG/USDT": 3,  "LUNC/USDT": 3, "CFX/USDT": 3,  "ROSE/USDT": 3,
    "JASMY/USDT": 3,"HOT/USDT": 3,  "AR/USDT": 3,
    # ── Key 4 (MEXC_KEY_4_*) ──
    "CHZ/USDT": 4,  "ENJ/USDT": 4,  "MAGIC/USDT": 4,"RON/USDT": 4,
    "BEAM/USDT": 4, "PORTAL/USDT": 4,"HNT/USDT": 4, "KAVA/USDT": 4,
    "EGLD/USDT": 4, "FLOW/USDT": 4, "ONE/USDT": 4,  "ZIL/USDT": 4,
    "KSM/USDT": 4,  "POL/USDT": 4,
}

# Fee-aware profit targeting — TP/SL are computed dynamically, not fixed.
SCALP_NET_PROFIT_TARGET_BPS  = 3.0   # test: 2.0-8.0   (net profit after fees)
SCALP_RR_RATIO               = 1.6   # test: 1.3-2.5   (tp_bps / sl_bps)
SCALP_MAX_BREAKEVEN_WIN_RATE = 0.65  # test: 0.55-0.75 (block if math needs >65% wr)

# Fee management
SCALP_FEE_DEFAULT_BPS     = 10.0   # fallback if CCXT lookup fails (conservative)
SCALP_FEE_OVERRIDES       = {      # overrides CCXT data where known to be wrong
    # MEXC spot is 0% MAKER / 5 bps TAKER — verified via the live private
    # fetch_trading_fee endpoint (scripts/mexc_fee_check.py, 2026-06-03), not
    # 0/0. Scalping is only viable here on MAKER fills (SCALP_USE_MAKER_EXECUTION
    # = True): gate 4 (_fee_viability) prices the round trip at the maker fee, so
    # it stays viable; taker execution correctly stands down at this fee.
    "mexc":   {"maker": 0.0,  "taker": 5.0},    # 0% maker / 0.05% taker (verified)
    # Bitget: 0.2% maker AND taker on majors per the exchange's public symbols
    # endpoint (scripts/exchange_fee_check.py bitget, 2026-06-03) — the old
    # "0.01% confirmed" was wrong by 20x. Some alts are 0.1%; we pin the majors
    # rate (conservative). 40bps round trip → breakeven WR 95.7% → bitget is
    # un-scalpable on either fee basis; gate 4 now correctly blocks it.
    "bitget": {"maker": 20.0, "taker": 20.0},   # 0.20% verified (was wrongly 0.01%)
}

# OFI signal parameters
SCALP_OFI_Z_ENTRY         = 2.0    # test: 1.0-2.5   (z-score entry threshold; v2 raised 1.5→2.0)
SCALP_OFI_Z_EXIT          = 0.3    # test: 0.1-0.7   (OFI exhaustion exit)
SCALP_OFI_Z_CONTRADICT    = -0.8   # test: -0.4 to -1.5 (OFI flip exit)
SCALP_OFI_PERSIST_TICKS   = 5      # test: 2-6       (consecutive ticks above threshold; v2 raised 3→5)
SCALP_OFI_LEVELS          = 10     # test: 1-10      (book depth levels)
SCALP_OFI_WINDOW_SEC      = 20     # test: 10-40     (bucket accumulation window seconds)
SCALP_ZSCORE_WINDOW       = 80     # test: 40-150    (rolling z-score normalisation periods)

# Execution parameters
SCALP_MAX_SPREAD_BPS      = 3.0    # test: 1.5-6.0   (max bid-ask spread bps)
SCALP_STALE_MID_THRESHOLD_SEC = 60 # test: 30-180    (skip entry if a symbol's mid hasn't moved for >= N sec — frozen feed = stale OFI)
SCALP_MAX_HOLD_SEC        = 180    # test: 60-300    (force exit after N seconds)
SCALP_SCAN_INTERVAL_MS    = 100    # test: 50-500    (main loop interval ms)
SCALP_MAX_CONCURRENT      = 2      # test: 1-3       (max open scalp positions)
SCALP_POSITION_SIZE_USD   = 20.0   # test: 10-50     (per-trade size USD; ~3% of FUND_MEXC_SCALP_CAPITAL=500 baseline)

# Circuit breakers
SCALP_DAILY_LOSS_HALT_PCT = 3.0    # test: 1-5       (% of scalp fund allocation; was abs USD)
SCALP_CONSEC_LOSS_PAUSE   = 4      # test: 3-6       (consecutive loss pause count)

# Session window — scalp edge depends on tight spreads + active flow,
# both of which thin out outside London/NY overlap. The main bot's
# dead zone (02:00-06:00 UTC) is contained inside this window — these
# are additive gates, not redundant.
SCALP_SESSION_START_UTC   = 0     # test: 6-13      (v2: London/NY overlap start, raised 7→12)
SCALP_SESSION_END_UTC     = 24     # test: 14-20     (v2: overlap end, lowered 17→16)

# News guard — when the sentiment aggregator's news_guard_active fires,
# scalp setups stop being reliable (correlated cross-pair flows + spread
# widening). Optional; falls through cleanly if sentiment isn't wired.
SCALP_RESPECT_NEWS_GUARD  = True   # test: True/False

# BTC correlation guard — alts gap when BTC moves sharply. Catches the
# fast move before the spread API ticks. Only applies to non-BTC symbols.
SCALP_BTC_GUARD_PCT       = 0.3    # test: 0.2-0.6   (block if |BTC 1m change| > N%)

# Micro price tracker — backfills price_30s/1m/3m/5m on closed
# observations so retrospective analysis can compare "did the signal
# predict correctly" vs "what was the realised P&L at exit". Same
# spirit as the main bot's future_price_tracker but on a tighter window.
SCALP_TRACKER_INTERVAL_SEC = 15    # test: 10-30

# Depth weights for multi-level OFI — exponential decay (≈λ=0.36) from 1.0
# at the top of book down to 0.04 at level 9 (Xu/Gould/Howison 2018,
# multi-level OFI). The engine uses min(SCALP_OFI_LEVELS, levels the book
# actually provides), so venues that stream fewer than 10 levels simply
# use what they send — no error.
SCALP_DEPTH_WEIGHTS       = {
    0: 1.0,  1: 0.70, 2: 0.50, 3: 0.35, 4: 0.25,
    5: 0.18, 6: 0.12, 7: 0.08, 8: 0.06, 9: 0.04,
}

# ── Scalp v2 selectivity layer (scalping_v2/SCALPING_V2.md) ─────────────────
# Additive gates + ATR-aware SL on top of the existing 13-gate flow, each
# individually togglable (all default ON). The entry-threshold / persistence /
# session-window tightening lives with the v1 OFI constants above
# (SCALP_OFI_Z_ENTRY, SCALP_OFI_PERSIST_TICKS, SCALP_SESSION_*), raised to v2
# values in place.

# Confluence gates (VWAP + HTF trend + volume): require N of 3.
SCALP_USE_CONFLUENCE          = True
SCALP_CONFLUENCE_REQUIRED     = 2      # test: 1-3
SCALP_USE_VWAP_GATE           = True
SCALP_USE_HTF_TREND_GATE      = True
SCALP_HTF_TIMEFRAME           = "5m"
SCALP_HTF_EMA_FAST            = 8
SCALP_HTF_EMA_SLOW            = 21
SCALP_USE_VOLUME_GATE         = True
SCALP_VOLUME_LOOKBACK_MIN     = 20     # test: 10-40
SCALP_VOLUME_THRESHOLD_RATIO  = 1.0    # test: 0.8-1.5  (current 1m vol ≥ ratio × median)

# Cross-exchange OFI confirmation — block when another venue strongly opposes.
SCALP_USE_CROSS_EXCHANGE_OFI        = True
SCALP_CROSS_EXCHANGE_DISAGREE_BLOCK = True
SCALP_CROSS_EXCHANGE_AGREE_Z_MIN    = 0.5   # test: 0.3-1.0

# BTC directional gate — alts can't fight BTC's order-flow direction.
SCALP_USE_BTC_DIRECTIONAL  = True
SCALP_BTC_OFI_NEUTRAL_BAND = 0.5    # test: 0.3-0.8  (|BTC z| below this = neutral)

# Adverse-selection guard — skip if mid moved against the signal recently.
SCALP_USE_ADVERSE_SELECTION_GUARD = True
SCALP_ADVERSE_MID_MOVE_BPS        = 1.0    # test: 0.5-2.0
SCALP_ADVERSE_MOVE_WINDOW_MS      = 100    # test: 50-300

# Depth adequacy — don't enter where our position would move the book.
SCALP_USE_DEPTH_GATE            = True
SCALP_MIN_TOP5_DEPTH_MULTIPLIER = 5.0    # test: 3-10
SCALP_MAX_TOP1_CONSUME_PCT      = 20.0   # test: 10-40

# ATR-aware stop loss — SL scales with realised volatility, clamped.
SCALP_USE_ATR_AWARE_SL   = True
SCALP_ATR_PERIOD         = 20     # test: 10-30
SCALP_ATR_TIMEFRAME      = "1m"
SCALP_ATR_SL_MULTIPLIER  = 0.3    # test: 0.2-0.5
SCALP_ATR_SL_FLOOR_BPS   = 1.5    # test: 1.0-3.0   (never tighter than this)
SCALP_ATR_SL_CEILING_BPS = 8.0    # test: 6-12      (never wider than this)

# ── Scalp v3: maker execution + microprice (gate 14) + toxicity (gate 15) ──
# Taker-to-maker pivot. MEXC is 0% maker / 5 bps taker (verified by
# scripts/mexc_fee_check.py), so executing as maker is what actually keeps the
# round trip near zero — the 0/0 fee override only looks free if fills are
# maker. These add an execution style + two confirmation gates ON TOP of gates
# 1-13; the OFI math, the FeeManager, and gates 1-13 are unchanged. All
# default-on, each individually toggleable.
SCALP_USE_MAKER_EXECUTION = True   # test: True/False  (place limit/maker orders; price the round trip at the maker fee, not taker)

# Gate 14 — Stoikov microprice fair-value filter. microprice = mid +
# (imbalance - 0.5) * spread must sit on the OFI direction's side of mid
# (LONG → above, SHORT → below); confirms resting size backs the move.
SCALP_USE_MICROPRICE_GATE = True   # test: True/False

# Gate 15 — toxicity stand-down. Adverse-selection risk spikes when the spread
# blows out or the mid moves fast; stand down on either. Absolute thresholds,
# fail-open on missing data. SCALP_TOXICITY_SPREAD_BPS is a softer ceiling than
# the hard gate-9 cap (SCALP_MAX_SPREAD_BPS) and the vol arm also covers BTC
# itself, which gate 13's BTC guard skips.
SCALP_USE_TOXICITY_GATE   = True   # test: True/False
SCALP_TOXICITY_SPREAD_BPS = 2.5    # test: 1.5-6.0  (stand down if top-of-book spread exceeds this)
SCALP_TOXICITY_VOL_BPS    = 40.0   # test: 20-80    (stand down if |1m mid move| exceeds this, in bps)

# Gate 4b — vol-conditional viability on HIGH-FEE venues (observation-first).
# When static gate-4 fee viability fails (e.g. bitget: 40bps round trip →
# 95.7% breakeven), a scalp can still be viable if current volatility makes
# the achievable move large relative to fees: TP scales with ATR
# (max(rt+target, SCALP_ATR_TP_MULTIPLIER × atr_bps)) and the breakeven is
# re-checked on that geometry. Admitted entries are OBSERVATION ROWS ONLY
# while SCALP_HIGH_FEE_OBSERVE_ONLY is True — no order, not even sim — until
# the accumulated rows prove the win rate. Anchor math: bitget rt=40bps,
# SL ceiling 8bps → needs tp_vol ≥ ~65.8bps → atr ≥ ~55bps at mult 1.2.
SCALP_USE_HIGH_FEE_VOL_GATE  = True    # test: True/False  (gate 4b: vol-conditional viability when static gate 4 fails)
SCALP_HIGH_FEE_OBSERVE_ONLY  = True    # test: True (NEVER False this phase — no orders on high-fee venues)
SCALP_ATR_TP_MULTIPLIER      = 1.2     # test: 0.8, 1.0, 1.2, 1.5  (achievable-move proxy: TP = mult × ATR)
SCALP_HIGH_FEE_VENUES        = []      # test: [], ["bitget"]  (extra venues evaluated via gate 4b only)

# Activation criteria — v1 (original) and v2 (tighter). Both readiness checks
# read these; see database/queries.get_scalp_activation_readiness[_v2].
SCALP_MIN_OBSERVATIONS_FOR_LIVE = 200
SCALP_MIN_WIN_RATE_FOR_LIVE     = 0.52
SCALP_MIN_AVG_NET_BPS_FOR_LIVE  = 0.0
SCALP_MAX_HOLD_EXIT_PCT         = 0.30
SCALP_MIN_DIRECTIONAL_ACC_1M    = 0.55

SCALP_MIN_OBSERVATIONS_FOR_LIVE_V2 = 300    # was 200
SCALP_MIN_WIN_RATE_FOR_LIVE_V2     = 0.55   # was 0.52
SCALP_MIN_AVG_NET_BPS_FOR_LIVE_V2  = 0.5    # was 0
SCALP_MAX_HOLD_EXIT_PCT_V2         = 0.25   # was 0.30
SCALP_MIN_DIRECTIONAL_ACC_1M_V2    = 0.57   # was 0.55

# ══════════════════════════════════════════════════════════════════════════════
# CROSS-CHAIN ARB AGENT (agents/crosschain_agent.py + execution/crosschain_engine.py)
# ══════════════════════════════════════════════════════════════════════════════
# NON-ATOMIC, inventory-pre-positioned arb over the same asset priced
# differently across L2s (Arbitrum / Base / Optimism for WETH-USDC). Distinct
# from the cross-EXCHANGE arb in execution/arb_engine.py — do not confuse them.
#
# Ships in OBSERVATION MODE: XCHAIN_CAPITAL=0 logs every evaluation to the
# xchain_observations table with the full cost breakdown, but never signs a
# tx. XCHAIN_LIVE_ENABLED is a hard gate kept False until the observation log
# confirms persistent positive net-edge (Gogol et al. 2024: L2 opps persist
# 10–20 blocks); flipping it on without supplying web3 signing is still a
# no-op — submit_swap raises NotImplementedError.
#
# Rationale: Öz et al. 2025 (arXiv:2501.17335) shows inventory arbs settle in
# ~9s vs ~242s for bridged, winning 66.96% of the time. We pre-position, never
# bridge mid-trade (XCHAIN_INVENTORY_DRIFT_PCT triggers the BalanceAgent to
# CCTP/canonical-bridge during idle windows).
XCHAIN_LIVE_ENABLED            = False  # test: False         (hard gate; keep False)
XCHAIN_CAPITAL                 = 0.0    # test: 0, 100, 250   (0 = observation mode)
XCHAIN_MIN_NET_EDGE_BPS        = 15.0   # test: 8, 12, 15, 20, 30
XCHAIN_GAS_BUDGET_BPS          = 5.0    # test: 3, 5, 8       (gas-as-%-of-notional cap)
XCHAIN_SLIPPAGE_TOLERANCE_BPS  = 10.0   # test: 5, 10, 20     (per-leg price impact tolerance)
XCHAIN_MAX_POSITION_USD        = 50.0   # test: 25, 50, 100, 250
XCHAIN_INVENTORY_DRIFT_PCT     = 0.20   # test: 0.10, 0.20, 0.30  (theta; rebalance trigger)
XCHAIN_SCAN_INTERVAL_MS        = 2000   # test: 1000, 2000, 5000
XCHAIN_DAILY_LOSS_HALT_PCT     = 2.0    # test: 1-5    (% of XCHAIN_CAPITAL; 0-alloc → no-op)
XCHAIN_CONSECUTIVE_LOSS_HALT   = 5      # test: 3, 5
XCHAIN_MAX_CONCURRENT          = 1      # test: 1, 2          (per-symbol scan concurrency cap)
# Pool block staleness cap. An L2 opportunity that hasn't refreshed within this
# many blocks is treated as not actionable (we cannot land a tx faster than
# the data is going stale).
XCHAIN_MAX_BLOCK_STALENESS     = 5      # test: 2, 5, 10
XCHAIN_SYMBOLS                 = ["WETH-USDC"]                  # test: keep single pair first
XCHAIN_CHAINS                  = ["arbitrum", "base", "optimism"]
# Per-chain venue + pool. fee_bps is also pulled from the pool at runtime
# (the math never trusts this number) — the value here is the documented
# tier so a misconfigured pool address fails loudly. Each pool address
# was verified by querying the canonical DEX factory contract on-chain
# (Uniswap V3 PoolFactory / Aerodrome PoolFactory / Velodrome V2
# PoolFactory) for (WETH, USDC native Circle, volatile/0.05%). Re-derive
# with `factory.getPool(WETH, USDC, …)` if you ever need to confirm.
XCHAIN_VENUES = {
    # Uniswap V3 0.05% — USDC (native Circle 0xaf88…) / WETH (0x82aF…).
    # Deepest WETH-USDC pool on Arbitrum (~$75M TVL, $150M+ daily vol).
    "arbitrum": {"venue": "uniswap_v3",
                 "pool_address": "0xC6962004f452bE9203591991D15f6b388e09E8D0",
                 "fee_bps":      5.0},
    # Aerodrome V1 vAMM (Solidly-volatile x*y=k) — WETH (0x4200…) / USDC
    # native Circle (0x8335…) on Base. NOT the Slipstream CL100 pool —
    # the connector inherits from _solidly_volatile so it expects the
    # constant-product vAMM, not concentrated liquidity. Default fee 30 bps.
    "base":     {"venue": "aerodrome",
                 "pool_address": "0xcDAC0d6c6C59727a65F871236188350531885C43",
                 "fee_bps":      30.0},
    # Velodrome V2 vAMM (Solidly-volatile x*y=k) — USDC native Circle
    # (0x0b2C…) / WETH (0x4200…) on Optimism. V2, not V1 (V1 is sunset).
    # Default fee 30 bps.
    "optimism": {"venue": "velodrome",
                 "pool_address": "0xF4F2657AE744354bAcA871E56775e5083F7276Ab",
                 "fee_bps":      30.0},
}
# RPC URL env vars (read via os.getenv from keys.env). Public endpoints have
# unbounded latency — production MUST use the operator's own nodes.
XCHAIN_RPC_ENV_VARS = {
    "arbitrum": "ARBITRUM_RPC_URL",
    "base":     "BASE_RPC_URL",
    "optimism": "OPTIMISM_RPC_URL",
}

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
# Reference exchange used when Coinglass returns per-exchange funding-rate
# series; the source falls back to the venue's own aggregate if the
# reference exchange isn't present in the payload.
COINGLASS_REFERENCE_EXCHANGE = "Binance"   # test: Binance | Bybit | OKX
# Free-tier rate limit. Implemented as an asyncio.Semaphore capping
# in-flight HTTP requests inside CoinglassSource — keeps a 16-pair ×
# 4-metric burst from punching through Coinglass's per-minute quota.
COINGLASS_RATE_LIMIT_PER_MIN = 6    # test: 3-12
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

# ── Web control panel (ui/web_server.py) ───────────────────────────────────
# Local operator tool only — no auth, no SSL, localhost/LAN. Started by the
# --web-ui flag; never exposed to the internet.
WEB_UI_HOST            = "localhost"   # test: "0.0.0.0" for LAN access
WEB_UI_PORT            = 8765          # test: any open port
WEB_UI_ENABLED         = False         # default off; enabled by --web-ui flag
WEB_UI_PUSH_INTERVAL_S = 0.5           # test: 0.25-2.0  (WebSocket push rate, seconds)
# Scalp feed history retention on the scalp agent page (Change 5). Closed
# scalp trades persist in the snapshot's scalp.closed_trades up to this many
# rows, newest first; the DB is the backing store so they survive restarts.
WEB_UI_SCALP_FEED_HISTORY = 30         # test: 10, 20, 30, 50

# ── LED price grid (web UI drawer; GET /api/ticker) ─────────────────────────
# Prices are PROXIED through the bot (browser never calls a venue directly).
# A dedicated worker thread (ui/web_server._TickerWorker, own event loop so
# heavy ccxt parsing never starves the dashboard) round-robins the venues
# with one bulk fetch_tickers() each: ALL pairs quoted in USD/USDT/USDC,
# deduped per base, ranked by 24h quote volume, each row carrying price /
# 24h % / 24h quote volume. The grid paginates client-side (‹ › arrows) in
# pages of TICKER_GRID_BOXES × TICKER_ROWS_PER_BOX. % move is the 24h change
# from the bulk payload — no per-pair candle calls. GET /api/ticker is a
# pure cache read of the worker's latest payload; /api/coinlogo/{coin}
# proxies + caches coin logos so the browser only talks to the bot.
TICKER_EXCHANGES        = ["kraken", "binance", "coinbase", "bybit", "hyperliquid"]
TICKER_DEFAULT_EXCHANGE = "kraken"     # test: any of TICKER_EXCHANGES (fresh each page load)
TICKER_GRID_BOXES       = 16           # test: 8, 12, 16  (4-wide grid of LED tiles per page)
TICKER_ROWS_PER_BOX     = 5            # test: 4-6        (pairs per tile; page size = boxes × rows)
TICKER_POLL_INTERVAL_S  = 15           # test: 10-30  (worker refresh cycle AND frontend poll cadence)
TICKER_FETCH_TIMEOUT_S  = 10           # test: 5-20   (per-venue bulk-fetch bound in the worker; 3× on the first, markets-loading call)

# ── Dashboard arb-opportunity panel colour ladder ───────────────────────────
# Execution rate = executed / above_threshold. Green when we're catching
# the majority of viable gaps; amber when half are slipping through;
# red when most viable gaps go unfilled (capital, latency, or routing
# bug — investigate immediately).
DASHBOARD_EXEC_RATE_GREEN_PCT = 50.0    # test: 30–70
DASHBOARD_EXEC_RATE_AMBER_PCT = 20.0    # test: 10–40

# ── Dashboard capital-gate miss colour ladder ───────────────────────────────
# Inline counter on the arb panel — how many would-be arbs the
# pre-execution balance check blocked today. 0 = green (healthy),
# 1–N = amber (worth watching), N+ = red (capital not pre-positioned).
DASHBOARD_BALANCE_MISS_AMBER = 1        # test: 1–3
DASHBOARD_BALANCE_MISS_RED   = 6        # test: 3–10
