# CryptoBot — Situational Trading Assistant

Claude-powered crypto trading bot. Situational, not autonomous.
Claude suggests. You decide. Kill switch always available.

## Philosophy
- Claude scans markets 24/7 and surfaces high-conviction setups
- Every trade requires your approval [G]o / [S]kip / [M]odify
- 1–2% daily target via arbitrage, momentum, and mean reversion
- Compound slowly, never be greedy, cut losses fast

## Quick Start

```bash
# 1. Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and fill in your API keys
cp config/keys.example.env config/keys.env

# 4. Run in sim mode (default)
python main.py

# 5. When ready for live — edit config/settings.py
#    Set SIM_MODE = False
```

## Keyboard Controls (terminal)
- [G] Approve trade
- [S] Skip signal
- [M] Modify before sending
- [I] Ask Claude for more info
- [P] Pause bot (lets open trades finish)
- [R] Resume from pause
- [K] KILL SWITCH — close everything immediately

## Project Structure
```
cryptobot/
├── main.py                  # Entry point
├── config/
│   ├── settings.py          # All tunable parameters
│   ├── keys.env             # Your API keys (never commit this)
│   └── keys.example.env     # Template
├── core/
│   ├── bot.py               # Main bot loop
│   ├── market_data.py       # Exchange connections + streaming
│   └── clock.py             # Timing, candle alignment
├── signals/
│   ├── engine.py            # Orchestrates all signal tracks
│   ├── arbitrage.py         # Track A
│   ├── momentum.py          # Track B
│   ├── reversion.py         # Track C
│   ├── quality_gate.py      # Score filter + deduplication
│   └── base.py              # Signal dataclass
├── sentiment/
│   ├── aggregator.py        # Combines all sources → score
│   ├── reddit.py
│   ├── telegram.py
│   ├── news.py
│   └── fear_greed.py
├── database/
│   ├── db.py                # SQLAlchemy setup
│   ├── models.py            # All table definitions
│   └── queries.py           # Common queries
├── execution/
│   ├── router.py            # Order routing (sim + live)
│   ├── position_manager.py  # SL/TP watching
│   └── kill_switch.py       # Emergency close all
├── strategies/
│   ├── base_strategy.py     # Strategy interface
│   ├── default.py           # Standard 3-track strategy
│   ├── arb_only.py          # Arbitrage only
│   ├── scalper.py           # High frequency scalping
│   └── custom.py            # User-defined strategy slot
├── profiles/
│   ├── profile_manager.py   # Load/save/switch profiles
│   ├── conservative.json    # Low risk, smaller size
│   ├── balanced.json        # Default
│   └── aggressive.json      # Higher risk tolerance
├── predictive/
│   ├── trainer.py           # Train model on historical data
│   ├── predictor.py         # Run predictions on live signals
│   └── features.py          # Feature engineering
├── ui/
│   ├── dashboard.py         # Rich terminal dashboard
│   ├── panels.py            # Individual UI panels
│   └── prompts.py           # User input handling
└── utils/
    ├── logger.py            # Structured logging
    ├── helpers.py           # Shared utilities
    └── validators.py        # Input validation
```

## Profiles
Switch risk profiles without restarting:
- **conservative** — 1% max per trade, score ≥ 70 gate, arb priority
- **balanced** — 3% max per trade, score ≥ 65 gate, all strategies
- **aggressive** — 5% max per trade, score ≥ 55 gate, momentum focus
- **custom** — define your own in profiles/custom.json

## Strategies
- **default** — arb + momentum + mean reversion (recommended)
- **arb_only** — cross-exchange arbitrage only
- **scalper** — short timeframe momentum
- **custom** — implement your own in strategies/custom.py

## Agents

Trading is split across independent agents under a single coordinator.
Each agent has its own capital pool, circuit breakers, and lifecycle.

### What's Built
- **SignalAgent** — wraps the main CryptoBot loop (per-trade approval,
  arb + momentum + reversion + sweep tracks)
- **ArbAgent** — dedicated cross-exchange arbitrage engine, fee-aware
- **ScalpingAgent** — OFI-primary signal, fee-aware TP/SL, exchange
  routing (observation mode — data collection before activation)
- **MacroAgent / SentimentAgent / OnChainAgent** — placeholders;
  reserved slots on the dashboard until implementation lands

### Coordinator diagram

```
Coordinator
├── SignalAgent       ($400)     Claude-evaluated, per-trade approval
├── ArbAgent          ($600)     Cross-exchange arbitrage engine
├── ScalpingAgent     ($0 obs)   Rule-based, OFI-primary
├── MacroAgent        (—)        Placeholder
├── SentimentAgent    (—)        Placeholder
└── OnChainAgent      (—)        Placeholder
```

See `GLOSSARY.md` for ScalpingAgent / FeeManager / OFI Engine
definitions, `PLUGIN_PATTERN.md` for the registry pattern every
agent follows, and `RUNBOOK.md` for activation checklists.

## Data & Predictive Engine
All signals, trades, candles, and sentiment are stored in SQLite.
After ~4 weeks of sim data, run:
```bash
python -m predictive.trainer
```
This trains an XGBoost model on your historical data.
Win probability scores then appear in Claude's signal briefs.
