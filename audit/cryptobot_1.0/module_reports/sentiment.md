# Module Report: sentiment

## Purpose

`sentiment/` is the pluggable sentiment-aggregation layer of the bot. It owns a small set of plugin "sources" (Fear & Greed Index, CryptoPanic/RSS news, Reddit, Google Trends, Telegram) that each return a `-100..+100` score plus optional metadata. The `SentimentAggregator` blends these into:

- a single **composite score** (`-100..+100`),
- a step-function **modifier** (`-20..+20`) added to every Signal's score by the quality gate,
- a set of **session-floor checks** (extreme fear, news guard, BTC dump guard) that can suppress trading entirely,
- a **hard-block** flag any source can raise (e.g. catastrophic news keywords) to short-circuit the system.

Per CLAUDE.md, scores feed in at the quality gate (`signals/quality_gate.py:116`) where `QualityGate.evaluate` adds the sentiment modifier to `raw_score` before applying the score threshold. The `core/bot.py` startup path also lazy-imports the singleton (`core/bot.py:178, 501, 851`) for session-floor evaluation and BTC-change updates. The module-level singleton instance is `sentiment.aggregator.sentiment`.

## Subpackages

- `sentiment/sources/` — plugin-style sources. Adding a new source means subclassing `BaseSentimentSource`, setting class-level attrs, implementing `fetch()` / `is_available()`, and appending the class to `REGISTERED_SOURCES` in `sentiment/sources/__init__.py`. The aggregator never needs to change.

## Files

| File | LOC | One-sentence summary |
|---|---|---|
| `sentiment/__init__.py` | 27 | Public re-exports: `SentimentAggregator`, `SentimentData`, `sentiment` singleton, `BaseSentimentSource`, `SourceResult`. |
| `sentiment/base.py` | 111 | Defines `SourceResult` dataclass and `BaseSentimentSource` ABC with cache-or-fetch `get()` method. |
| `sentiment/aggregator.py` | 322 | `SentimentAggregator`, `SentimentData`, `_composite_to_modifier`, module-level `sentiment` singleton. |
| `sentiment/sources/__init__.py` | 47 | Plugin registry — `REGISTERED_SOURCES` list of 5 source classes, with a "how to add" docstring. |
| `sentiment/sources/cryptopanic.py` | 179 | News scorer: CryptoPanic API or RSS fallback + keyword scorer; raises hard_block on catastrophic-event keywords. |
| `sentiment/sources/fear_greed.py` | 60 | Alternative.me Fear & Greed Index poller; the only non-optional source. |
| `sentiment/sources/google_trends.py` | 85 | pytrends-based search-interest signal, wrapped in run_in_executor (slow, light weight). |
| `sentiment/sources/reddit.py` | 145 | PRAW-based hot-post bull/bear keyword classifier across configured subreddits. |
| `sentiment/sources/telegram.py` | 42 | STUB — declares itself unavailable, returns neutral; TODO for Telethon. |

Total: 1018 LOC.

## Public surface

### `sentiment/__init__.py`
- Module docstring: "sentiment/ — pluggable sentiment aggregator." with public surface listed.
- Re-exports: `SentimentAggregator`, `SentimentData`, `sentiment` (singleton), `BaseSentimentSource`, `SourceResult` via `__all__`.

### `sentiment/base.py`
- Module docstring: explains the two contracts (`SourceResult`, `BaseSentimentSource`), `-100..+100` score range, `hard_block` short-circuit semantics, and that base `get()` handles caching/retry so subclasses only implement `fetch()`.
- `@dataclass class SourceResult`:
  - Fields: `source_id: str`, `score: float`, `raw_data: dict = field(default_factory=dict)`, `hard_block: bool = False`, `block_reason: str = ""`, `confidence: float = 1.0`, `timestamp: float = 0.0`, `error: Optional[str] = None`.
  - `__post_init__(self)` — defaults `timestamp` to `time.time()`, clamps `score` to `[-100, 100]`, clamps `confidence` to `[0, 1]`.
- `class BaseSentimentSource(ABC)`:
  - Class-level attrs (override in subclass): `source_id: str = "base"`, `weight: float = 0.0`, `refresh_interval: int = 300`, `optional: bool = True`.
  - `__init__(self)` — initialises `_last_result: Optional[SourceResult] = None`, `_last_fetch_time: float = 0.0`.
  - `@abstractmethod async def fetch(self) -> SourceResult` — must never raise; subclass returns a SourceResult with `error` set on failure.
  - `def is_available(self) -> bool` — default `True`; subclasses override to gate on env vars / deps.
  - `async def get(self) -> Optional[SourceResult]` — cache-or-fetch wrapper. Uses `refresh_interval` as TTL. On exception returns previous cached result (or `None` if none).
  - `def last(self) -> Optional[SourceResult]` — for tests + dashboard.

### `sentiment/aggregator.py`
- Module docstring describes orchestration + composite + session-floor responsibilities.
- `@dataclass class SentimentData` — aggregated picture for dashboard/engine. Fields:
  - `composite_score: float = 0.0`, `sentiment_modifier: float = 0.0`, `hard_block: bool = False`, `block_reason: str = ""`.
  - F&G surfaces: `fear_greed_value: Optional[int] = None`, `fear_greed_label: str = "unknown"`.
  - News surfaces: `news_score`, `news_guard_active`, `blocking_headline`, `top_headlines: list = field(default_factory=list)`.
  - Reddit: `reddit_score`, `reddit_bullish_ratio`.
  - Other: `google_trends_score`, `btc_change_30m`, `sources_active: int = 0`, `sources_available: int = 0`, `last_updated: Optional[datetime] = None`.
- `def _composite_to_modifier(composite: float) -> float` — step-function mapping composite `-100..+100` to additive modifier `-20..+20` with breakpoints at ±20/±40/±60.
- `class SentimentAggregator`:
  - `__init__(self, sources: Optional[list[BaseSentimentSource]] = None)` — defaults to `[cls() for cls in REGISTERED_SOURCES]`; tests can inject pre-instantiated sources.
  - `async def refresh(self) -> SentimentData` — fans out `s.get()` over `is_available()` sources via `asyncio.gather(return_exceptions=True)`, computes composite, populates `SentimentData`, persists each `SourceResult` via `db_queries.log_sentiment_result(result, composite_score=composite)`.
  - `async def get_current(self) -> SentimentData` — returns cached `_data`, refreshing if older than `settings.SENTIMENT_REFRESH_INTERVAL_SEC`.
  - `def get_signal_modifier(self) -> int` — returns `int(self._data.sentiment_modifier or 0)`. Mirrors `macro_monitor.get_signal_modifier()`. Zero before first refresh.
  - `def is_hard_blocked(self) -> tuple[bool, str]` — true if any source raised `hard_block` or if `news_guard_active`.
  - `def passes_session_floor(self) -> tuple[bool, str]` — three independent floors: hard_block; news_guard_active; `fear_greed_value < SENTIMENT_FEAR_GREED_FLOOR`; `composite_score < SENTIMENT_COMPOSITE_FLOOR`.
  - `def set_btc_change_30m(self, pct: float) -> None` — stores a 30m BTC % change for the BTC-dump guard.
  - `def btc_guard_penalty(self, signal_type: str, pair: str) -> float` — returns `0` for `arb`, `0` when no data, `0` if `_btc_change_30m > SENTIMENT_BTC_GUARD_PCT`, otherwise `SENTIMENT_BTC_GUARD_PENALTY`.
  - `@property def fear_greed(self) -> Optional[BaseSentimentSource]` — returns the `FearGreedSource` instance for direct access from `bot.py`.
  - Private: `_compute_composite(pairs)` — weighted average `sum(score*weight*conf)/sum(weight*conf)`, clamped ±100. `_hard_block_from(pairs)` — first source with `hard_block=True` wins. `_populate_per_source_fields(data, pairs)` — fans `source_id` to specific `SentimentData` fields. `_to_dashboard_dict(d)` — dashboard mirror.
- Module singleton: `sentiment = SentimentAggregator()` (constructed at import time, so REGISTERED_SOURCES are instantiated immediately on first `from sentiment import sentiment`).

### `sentiment/sources/__init__.py`
- Docstring is a "how to add a new sentiment source" recipe.
- Imports: `FearGreedSource`, `CryptoPanicSource`, `RedditSource`, `GoogleTrendsSource`, `TelegramSource`.
- `REGISTERED_SOURCES: list[type] = [FearGreedSource, CryptoPanicSource, RedditSource, GoogleTrendsSource, TelegramSource]`.

### `sentiment/sources/fear_greed.py`
- Module docstring: "Alternative.me Fear & Greed Index — the only source the aggregator treats as non-optional."
- `ENDPOINT = "https://api.alternative.me/fng/?limit=1"`.
- `class FearGreedSource(BaseSentimentSource)`:
  - `source_id = "fear_greed"`, `refresh_interval = 900`, `optional = False`.
  - `__init__` reads `weight = getattr(settings, "SENTIMENT_WEIGHT_FEAR_GREED", 0.4)`.
  - `async def fetch(self) -> SourceResult` — `aiohttp` GET, maps `value (0..100)` to `(value-50)*2.0`, returns score + `raw_data={value, label, timestamp}`. On exception logs warning and returns neutral SourceResult with `error` set.
  - No `is_available()` override — always available.

### `sentiment/sources/cryptopanic.py`
- Module docstring explains API/RSS dual path and catastrophic-event hard blocks.
- Constants: `POSITIVE_KEYWORDS` (10), `NEGATIVE_KEYWORDS` (15), `BLOCKING_KEYWORDS` (6 catastrophic phrases). `RSS_FEEDS` (cointelegraph, coindesk, decrypt). `CRYPTOPANIC_ENDPOINT` URL template.
- `def _score_headlines(headlines: list[str]) -> tuple[float, float, bool, str, list[str]]` — returns `(score, confidence, hard_block, block_reason, top)`. Score is `net * 100 * matched_fraction + net*30`. Confidence is `min(1, len(headlines)/25)`. First blocking keyword match wins.
- `class CryptoPanicSource(BaseSentimentSource)`:
  - `source_id = "cryptopanic"`, `refresh_interval = 300`, `optional = True`.
  - `__init__` reads `weight = getattr(settings, "SENTIMENT_WEIGHT_CRYPTOPANIC", 0.25)`.
  - `def is_available(self) -> bool` — returns `True` (RSS fallback always works).
  - `async def fetch(self) -> SourceResult` — picks API path if `CRYPTOPANIC_API_KEY` env var is set, otherwise RSS fallback; runs keyword scorer; returns SourceResult with `hard_block`, `block_reason`, `raw_data={feed, headlines, article_count}`.
  - `async def _fetch_api(self, api_key)` — `aiohttp` GET, returns titles from `results`.
  - `async def _fetch_rss(self)` — fetches each RSS URL via aiohttp, parses bytes with `feedparser`, returns up to 20 titles per feed.

### `sentiment/sources/reddit.py`
- Module docstring: PRAW wrapped in `run_in_executor`.
- Constants: `DEFAULT_SUBREDDITS = ["CryptoCurrency", "Bitcoin", "ethtrader"]`, `BULLISH_KEYWORDS` (11), `BEARISH_KEYWORDS` (11).
- `def _classify(text: str) -> str` — returns `"bull"`, `"bear"`, or `"neutral"` based on first keyword hit.
- `class RedditSource(BaseSentimentSource)`:
  - `source_id = "reddit"`, `refresh_interval = 600`, `optional = True`.
  - `__init__` reads `weight = getattr(settings, "SENTIMENT_WEIGHT_REDDIT", 0.2)`.
  - `def is_available(self) -> bool` — returns `True` only if `praw` importable AND `REDDIT_CLIENT_ID` env var set.
  - `async def fetch(self) -> SourceResult` — runs `_scrape_sync` in executor.
  - `def _scrape_sync(self) -> dict` — uses `praw.Reddit` (with `REDDIT_CLIENT_ID/SECRET/USER_AGENT` env vars), pulls 25 hot posts per subreddit from `settings.REDDIT_SUBREDDITS` or `DEFAULT_SUBREDDITS`, computes `bull_ratio` and centered-on-zero score `(bull_ratio - 0.5) * 200`. Confidence `min(1, post_count/50)`.

### `sentiment/sources/google_trends.py`
- Module docstring notes pytrends is sync; signal is "rising-vs-baseline"; lightly weighted at 0.1.
- Constants: `KEYWORDS = ["bitcoin", "crypto", "ethereum"]`, `TIMEFRAME = "now 7-d"`.
- `class GoogleTrendsSource(BaseSentimentSource)`:
  - `source_id = "google_trends"`, `refresh_interval = 3600`, `optional = True`.
  - `__init__` reads `weight = getattr(settings, "SENTIMENT_WEIGHT_GOOGLE_TRENDS", 0.1)`.
  - `def is_available(self) -> bool` — `True` iff `pytrends` importable.
  - `async def fetch(self)` — runs `_fetch_sync` in executor, sets `confidence=0.6` (fixed per spec).
  - `def _fetch_sync(self) -> dict` — `pytrends.request.TrendReq`, `build_payload(KEYWORDS, ...)`, takes mean across KEYWORDS of latest interest reading, scores `(latest-50)*2`, clamped ±100.

### `sentiment/sources/telegram.py`
- Module docstring: "Telegram channel sentiment — STUB" with TODO implementation notes for Telethon.
- `class TelegramSource(BaseSentimentSource)`:
  - `source_id = "telegram"`, `refresh_interval = 0`, `optional = True`.
  - `__init__` reads `weight = getattr(settings, "SENTIMENT_WEIGHT_TELEGRAM", 0.05)`.
  - `def is_available(self) -> bool` — returns `False` (stub).
  - `async def fetch(self) -> SourceResult` — returns neutral SourceResult with `error="Telegram source not yet implemented"`.

## Plugin registrations

`REGISTERED_SOURCES` in `sentiment/sources/__init__.py:41`. Each entry, with API key env var and `is_available()` behaviour:

| source_id | class | weight (default) | refresh_interval | optional | env vars / deps | is_available() |
|---|---|---|---|---|---|---|
| `fear_greed` | `FearGreedSource` | `SENTIMENT_WEIGHT_FEAR_GREED` (0.4) | 900s | False (core) | none | always True (no override) |
| `cryptopanic` | `CryptoPanicSource` | `SENTIMENT_WEIGHT_CRYPTOPANIC` (0.25) | 300s | True | `CRYPTOPANIC_API_KEY` (optional; RSS fallback) | True (always — RSS fallback) |
| `reddit` | `RedditSource` | `SENTIMENT_WEIGHT_REDDIT` (0.2) | 600s | True | `praw` package + `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | True iff `praw` importable AND `REDDIT_CLIENT_ID` set |
| `google_trends` | `GoogleTrendsSource` | `SENTIMENT_WEIGHT_GOOGLE_TRENDS` (0.1) | 3600s | True | `pytrends` package | True iff `pytrends` importable |
| `telegram` | `TelegramSource` | `SENTIMENT_WEIGHT_TELEGRAM` (0.05) | 0 (event-driven) | True | (would be `telethon`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`) | False (stub) |

Sum of default weights = 1.00. With `telegram` always unavailable and `google_trends`/`reddit` dependent on optional deps, in a vanilla install the aggregator typically runs with only `fear_greed` + `cryptopanic` (weight sum 0.65 of the documented ladder).

## Imports graph

### Imports from project

- `sentiment/aggregator.py` imports `config.settings`, `database.queries as db_queries`, `sentiment.base.{BaseSentimentSource, SourceResult}`, `sentiment.sources.REGISTERED_SOURCES`, `sentiment.sources.fear_greed.FearGreedSource`.
- `sentiment/__init__.py` imports `sentiment.aggregator.{SentimentAggregator, SentimentData, sentiment}`, `sentiment.base.{BaseSentimentSource, SourceResult}`.
- `sentiment/sources/__init__.py` imports each source class.
- Every `sentiment/sources/*.py` imports `config.settings` and `sentiment.base.{BaseSentimentSource, SourceResult}`. No other project imports.
- `sentiment/base.py` imports only stdlib.

### Imported by (`grep "from sentiment"`)

- `signals/quality_gate.py:116` — `from sentiment import sentiment as sentiment_aggregator` (lazy import inside method).
- `core/bot.py:178, 501, 851` — `from sentiment import sentiment as sentiment_singleton` / `as sentiment_agg` (three lazy import sites).
- `agents/base.py:9` — references the BaseSentimentSource pattern in its docstring (no import).
- `tests/test_sentiment.py:18, 19, 287, 298` — test imports.
- `prompts/build_sentiment.md`, `prompts/build_wiring.md` — design docs referencing the public surface.

## Tests

Only `tests/test_sentiment.py` imports from `sentiment/`. 382 LOC. Other tests touched by grep (`test_quality_gate`, `test_bot`, `test_macro`, etc.) merely mention the word "sentiment" in comments/fixtures.

Tests (one-liner each):

- `test_composite_weighted_average` — composite is `Σ(score·weight·conf) / Σ(weight·conf)`, clamped to ±100; +44 maps to modifier 10.
- `test_composite_clamps_to_bounds` — extreme inputs stay within ±100, +100 maps to modifier 20.
- `test_hard_block_propagates_from_any_source` — any source with `hard_block=True` sets `data.hard_block` and `block_reason`.
- `test_hard_block_blocks_session_floor` — `hard_block` causes `passes_session_floor()` to return False with reason containing `"hard_block"`.
- `test_unavailable_source_excluded_from_composite` — `is_available()==False` sources are skipped before fetch.
- `test_crashing_source_does_not_break_aggregator` — a source whose fetch raises is dropped, other sources still score.
- `test_passes_session_floor_blocks_on_extreme_fear` — F&G value below floor blocks session.
- `test_passes_session_floor_blocks_below_composite_floor` — composite below floor blocks session.
- `test_passes_session_floor_allows_normal_conditions` — neutral/positive conditions pass.
- `test_btc_guard_penalty_applies_below_threshold` — 30m BTC drop ≤ guard threshold yields penalty.
- `test_btc_guard_no_penalty_above_threshold` — small drops yield no penalty.
- `test_btc_guard_exempts_arb` — `signal_type=="arb"` always 0.
- `test_btc_guard_no_change_data_yet` — no `set_btc_change_30m()` call → 0.
- `test_new_source_plugs_into_aggregator` — a custom `BaseSentimentSource` subclass works without aggregator changes.
- `test_registered_sources_list_is_extensible` — asserts `len(REGISTERED_SOURCES) >= 5`.
- `test_fear_greed_property_returns_instance` — `aggregator.fear_greed` returns the live `FearGreedSource` instance.
- `test_dashboard_dict_populates` — `agg.latest` mirrors `_data` with the right keys after refresh.
- `test_get_signal_modifier_defaults_to_zero_before_refresh` — modifier is 0 before any refresh.
- `test_is_hard_blocked_false_before_refresh` — `is_hard_blocked()` is `(False, "")` before refresh.
- `test_hard_block_propagates_to_aggregator` — `hard_block=True` source surfaces via `is_hard_blocked()`.
- `test_get_signal_modifier_returns_step_value_after_refresh` — composite ≥+60 maps to modifier 20 (ladder boundary check).

Test fixtures rely on a `_patch_db_log` monkeypatch fixture that nulls `sentiment.aggregator.db_queries.log_sentiment_result` to avoid SQLite during tests.

## TODOs / FIXMEs / stubs

- `sentiment/sources/telegram.py:4` — module docstring: `"Telegram channel sentiment — STUB."`
- `sentiment/sources/telegram.py:6` — `"TODO: implement via Telethon. The Telethon client is event-driven, so this source should subscribe to TELEGRAM_CHANNELS at start-up and push incoming messages into a rolling buffer. fetch() would then score the buffer's recent contents rather than poll. For now the source declares itself unavailable and returns a neutral result."`
- `sentiment/sources/telegram.py:33` — `return False  # stub`
- `sentiment/sources/telegram.py:40` — `raw_data={"status": "stub"}`

Telegram source is the only declared stub in the package.

## Known issues observed

- **Singleton instantiated at import time.** `sentiment = SentimentAggregator()` at `sentiment/aggregator.py:322` runs at first import; this constructs every `REGISTERED_SOURCES` class. Side-effect-free today, but if any source class's `__init__` ever touches the network or files it would do so at module import. `import sentiment` from a test that monkeypatches settings *after* import will miss the override.
- **Composite floor vs F&G floor: F&G floor checked first.** `passes_session_floor` checks `fear_greed_value < SENTIMENT_FEAR_GREED_FLOOR` before `composite_score < SENTIMENT_COMPOSITE_FLOOR` — intentional per the comment, but it means an extreme F&G reading is reported as the reason even when composite also failed.
- **Stale settings constants.** `config/settings.py` contains *two* sets of sentiment knobs: an older block at lines 371–388 (`SENTIMENT_ENABLED`, `SENTIMENT_WEIGHTS` dict, `SENTIMENT_BOOST_THRESHOLD`, `SENTIMENT_BLOCK_THRESHOLD`, `SENTIMENT_BOOST_AMOUNT`, `SENTIMENT_SUPPRESS_AMOUNT`, `SENTIMENT_VELOCITY_WINDOW`, `SENTIMENT_VELOCITY_BOOST`, `SESSION_MIN_SENTIMENT_SCORE`, `SENTIMENT_HARD_BLOCK_SKIP_REASON`) and the *actually used* block at lines 1029–1042 (`SENTIMENT_COMPOSITE_FLOOR`, `SENTIMENT_FEAR_GREED_FLOOR`, `SENTIMENT_BTC_GUARD_PCT`, `SENTIMENT_BTC_GUARD_PENALTY`, `SENTIMENT_REFRESH_INTERVAL_SEC`, `SENTIMENT_HTTP_TIMEOUT_SEC`, `SENTIMENT_WEIGHT_*`). Nothing in `sentiment/` reads from the older block — it appears to be vestigial.
- **`prompts/build_wiring.md:72` references `sentiment_aggregator` import name** that doesn't exist (`from sentiment.aggregator import sentiment_aggregator`). The actual export is `sentiment`. Wiring doc is out-of-date.
- **In a vanilla install most sources are unavailable.** Without `praw` / `pytrends` installed and without `REDDIT_CLIENT_ID` set, only Fear & Greed (weight 0.4) and CryptoPanic via RSS (weight 0.25) contribute, leaving a composite driven mostly by F&G. The composite-floor and modifier-ladder tuning was set assuming all five sources active.
- **`asyncio.get_event_loop()` is deprecated for getting the running loop in 3.10+.** `reddit.py:76` and `google_trends.py:50` both use `asyncio.get_event_loop().run_in_executor(...)`. Should be `asyncio.get_running_loop()` or just `asyncio.to_thread()` on 3.11.
- **`datetime.utcnow()` deprecated.** `aggregator.py:143, 165` use `datetime.utcnow()`, which is deprecated in 3.12 in favour of `datetime.now(timezone.utc)`. Project targets 3.11 so this is a forward-compat concern only.
- **Singleton's `latest` dict is mutated in place.** `set_btc_change_30m` writes to both `_data.btc_change_30m` and `self.latest["btc_change_30m"]`, but `latest` is otherwise only rebuilt during `refresh()` — slightly asymmetric and could confuse a reader who expects `latest` to be a snapshot.
- **Confidence multiplier double-discounts on quiet days.** Composite uses `score * weight * confidence` and `weight_sum = sum(weight * confidence)`. A single source with low confidence pulls the denominator down too, so its weight-share in the average actually doesn't change — meaning low-confidence sources are not really down-weighted vs other sources, only the *total* signal. This may not match intuition for "confidence."
- **`asyncio.gather(return_exceptions=True)` swallows exceptions silently at DEBUG level** (`aggregator.py:124-126`), but the base `get()` method already catches and returns the previous cached result, so a true exception bubbling to `gather` indicates a bug in `get()` itself — worth a higher log level.
- **`log_sentiment_result` is called per-source per-refresh.** With 5 sources every 300s that's an SQLite write hot-path; check `database/queries.py` for batching.
