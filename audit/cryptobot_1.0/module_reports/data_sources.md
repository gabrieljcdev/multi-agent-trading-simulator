# Module Report: data_sources

## Purpose
Pluggable aggregator for raw quantitative time-series feeds — crypto derivatives (funding, OI, liquidations, long/short), broad crypto market context (dominance, mcap), traditional-market equities/bonds/FX, central-bank rates, sovereign yield curves, and slow-moving global macro indicators (GDP, inflation, unemployment). The package owns a per-key cache, source-level staleness, error fallback, and a fnmatch-based pub/sub bus. Concrete sources never touch caching or notification — they only emit `DataPoint` objects from `fetch_all()`.

How this differs from the two sibling source packages:

- `sentiment/sources/` — text/news/social feeds (cryptopanic, reddit, telegram, fear_greed, google_trends). Emits a `SentimentScore` (-1..+1 with confidence) per source via `sentiment.aggregator`. About narrative.
- `macro/sources/` — economic calendar events only (FRED release calendar + stub). Emits `CalendarEvent` objects with scheduled timestamps consumed by `macro.monitor`. About *when* an event will happen.
- `data_sources/sources/` — numeric *readings* (price, rate, level, ratio). About *what the number is right now*. Consumed downstream by `macro.monitor`, the funding-rate arb engine, the dashboard, and the quality gate.

## Subpackages
- `data_sources/sources/` — plugin-style external feeds. One file per remote API, each implementing `BaseDataSource.fetch_all()` + `list_metrics()`. Registry is `data_sources/sources/__init__.py:REGISTERED_SOURCES`.

## Files
| File | LOC | One-sentence summary |
|---|---:|---|
| `data_sources/__init__.py` | 288 | `DataSources` orchestrator + module-level singleton `data_sources`; pub/sub bus, refresh loop, `get_funding_rates()` aggregation helper, DB logging of every refreshed point. |
| `data_sources/base.py` | 213 | Abstract `BaseDataSource` + `DataPoint` dataclass; owns cache, staleness, error-state fallback, and change-detection notification. |
| `data_sources/sources/__init__.py` | 70 | Plugin registry — imports each `*Source` class and appends an instance to `REGISTERED_SOURCES`. |
| `data_sources/sources/alpha_vantage.py` | 170 | SPY/QQQ/GLD/TLT via Alpha Vantage `GLOBAL_QUOTE`; daily-call budget gate + inter-call pacing for the 25/day free tier. |
| `data_sources/sources/binance_futures.py` | 141 | Binance USDT-M perp open interest, latest funding rate, and top-trader long/short ratio per pair; no auth. |
| `data_sources/sources/bybit_derivs.py` | 137 | Bybit v5 `/market/tickers` for funding, OI, mark price, 24h %; one HTTP call per symbol. |
| `data_sources/sources/cftc_cot.py` | 134 | CFTC Commitments of Traders disaggregated report — non-commercial Bitcoin futures long/short/net, weekly. |
| `data_sources/sources/coingecko.py` | 140 | CoinGecko `/global` — BTC/ETH dominance, total market cap/volume, mcap 24h %; supports public/demo/pro key tiers. |
| `data_sources/sources/coinglass.py` | 189 | Coinglass funding/OI/liquidations/L-S ratio per pair; semaphore caps concurrent HTTP for free-tier quota. |
| `data_sources/sources/cryptocompare.py` | 139 | CryptoCompare `pricemultifull` — price, 24h volume, mcap, 24h % per coin (cross-check vs CoinGecko). |
| `data_sources/sources/ecb.py` | 119 | ECB SDMX-JSON — refi rate, deposit-facility rate, EUR/USD spot; one HTTP call per series. |
| `data_sources/sources/frankfurter.py` | 237 | FX via frankfurter.dev (ECB reference rates) + ICE-style geometric DXY proxy; pulls today and yesterday for 24h change. |
| `data_sources/sources/fred.py` | 214 | FRED observations API for CPI, yields (2y/10y/30y), fed funds, M2, unemployment, 2-10 spread, VIX; uses `units=pc1` for CPI YoY; carries the VIX-based `get_risk_sentiment()` classifier. |
| `data_sources/sources/imf.py` | 118 | IMF Datamapper — GDP per cap, inflation YoY, unemployment per country; one call per indicator. |
| `data_sources/sources/us_treasury.py` | 133 | US Treasury daily yield-curve CSV — tenors 1m..30y + derived 2y/10y spread (FRED backup). |
| `data_sources/sources/world_bank.py` | 123 | World Bank Open Data — GDP growth, inflation, unemployment per (country × indicator); 5-year lookback for last published value. |

## Public surface

### `data_sources/__init__.py`
- Module docstring: pluggable macro+on-chain+FX aggregator; PULL / PUSH / REFRESH usage examples.
- `class _Subscription` — `__slots__=("sub_id","pattern","callback","filter")`; `__init__(sub_id, pattern, callback, filter=None)`; `matches(key) -> bool` (fnmatchcase).
- `class DataSources`
  - `__init__(sources: Optional[list[BaseDataSource]] = None)` — defaults to deferred `from data_sources.sources import REGISTERED_SOURCES`; sets each source as attribute by `source_id` and wires `_notify_callback`.
  - `async refresh_all() -> None` — filters by `is_available()`, gathers per-source `_safe_refresh`, then `db_queries.log_data_point(point)` for every cached point.
  - `async _safe_refresh(source) -> None` — wraps `refresh_if_stale()` in try/except.
  - `async get(source_id, metric, symbol=None) -> Optional[DataPoint]`.
  - `latest_snapshot() -> dict[str, DataPoint]` — flatten every cache by key.
  - `get_latest(source_id, metric, symbol=None) -> Optional[DataPoint]` — sync cached read.
  - `get_funding_rates() -> dict[str, float]` — `{symbol: rate}` across all available sources, newest-timestamp wins; skips error points.
  - `subscribe(pattern, callback, filter=None) -> str` — fnmatch wildcards; returns sub_id.
  - `unsubscribe(subscription_id) -> bool`.
  - `async _on_source_change(new, previous) -> None` — wired into base; fires matching subs via `asyncio.create_task`.
  - `async _invoke(sub, new, previous) -> None` — awaits coroutine callbacks.
  - `async run_refresh_loop(interval_sec) -> None` — infinite loop.
- Module-level singleton: `data_sources = DataSources()`.
- `__all__ = ["DataSources", "BaseDataSource", "DataPoint", "data_sources"]`.

### `data_sources/base.py`
- `@dataclass DataPoint`: fields `source_id: str`, `metric: str`, `value: float`, `symbol: Optional[str]=None`, `timestamp: float=0.0` (auto-filled `__post_init__`), `raw_data: dict=field(default_factory=dict)`, `error: Optional[str]=None`. Property `key` → `"{source_id}.{metric}"` or `"{source_id}.{metric}.{symbol}"`.
- `class BaseDataSource(ABC)`. Class attrs (override): `source_id="base"`, `display_name="Base"`, `refresh_interval=300`, `optional=True`, `requires_api_key=False`, `api_key_env_var=""`.
  - `__init__()` — instance-level `_cache: dict[str, DataPoint]`, `_last_fetch_time=0.0`, `_notify_callback=None`.
  - `@abstractmethod async fetch_all() -> list[DataPoint]` — must never raise.
  - `@abstractmethod list_metrics() -> list[str]`.
  - `is_available() -> bool` — default: True unless `requires_api_key` and env var unset.
  - `async get(metric, symbol=None) -> Optional[DataPoint]`.
  - `async refresh_if_stale() -> None`.
  - `async _do_fetch() -> None` — runs `fetch_all`, merges, does NOT clobber a good cached value with an error placeholder, fires `_notify_callback` only on value/error change.
  - `_make_key(metric, symbol=None) -> str`.
  - `cached(metric, symbol=None) -> Optional[DataPoint]` — sync read.
  - `cached_value(metric, symbol=None, default=None) -> Optional[float]` — None/default if point missing or has error.
  - `latest_points() -> list[DataPoint]`.

### `data_sources/sources/__init__.py`
- Constants: `REGISTERED_SOURCES: list` — 13 source instances (see §Plugin registrations).

### `data_sources/sources/alpha_vantage.py`
- Constants: `ENDPOINT = "https://www.alphavantage.co/query"`.
- `class AlphaVantageSource(BaseDataSource)`: `source_id="alpha_vantage"`, `display_name="Alpha Vantage"`, `refresh_interval=settings.ALPHA_VANTAGE_REFRESH_SEC`, `optional=True`, `requires_api_key=True`, `api_key_env_var="ALPHA_VANTAGE_API_KEY"`.
  - `__init__()` — `_call_date: Optional[str]=None`, `_calls_today: int=0`.
  - `list_metrics() -> list[str]` — `["spy","spy_change_pct","qqq","qqq_change_pct","gld","tlt"]`.
  - `async fetch_all() -> list[DataPoint]` — budget gate against `ALPHA_VANTAGE_DAILY_CALL_BUDGET`; `asyncio.sleep(ALPHA_VANTAGE_PACE_SEC)` between symbols (not before first).
  - `async _fetch_symbol(session, symbol, api_key) -> list[DataPoint]` — parses `"05. price"`, `"10. change percent"`.
  - `_metric_for(symbol) -> str`.
  - `_reset_budget_if_new_day() -> None`.
  - `get_spy() / get_spy_change_pct() / get_qqq() / get_gold() -> Optional[float]`.

### `data_sources/sources/binance_futures.py`
- `class BinanceFuturesSource(BaseDataSource)`: `source_id="binance_futures"`, `refresh_interval=settings.BINANCE_FUTURES_REFRESH_SEC`, `requires_api_key=False`.
  - `list_metrics()` → `["open_interest","funding_rate","long_short_ratio"]`.
  - `async fetch_all()` — gathers `_fetch_for_symbol` per `settings.BINANCE_FUTURES_SYMBOLS`.
  - `async _fetch_for_symbol(session, symbol)` — hits `/fapi/v1/openInterest`, `/fapi/v1/fundingRate?limit=1`, `/futures/data/topLongShortAccountRatio?period=1h&limit=1`.
  - `_fmt(symbol)` → `"BTC/USDT"`; `_float(v)`.
  - `get_open_interest(pair="BTC/USDT")`, `get_funding(...)`, `get_long_short_ratio(...)`.

### `data_sources/sources/bybit_derivs.py`
- `class BybitDerivsSource(BaseDataSource)`: `source_id="bybit_derivs"`, `refresh_interval=settings.BYBIT_REFRESH_SEC`.
  - `list_metrics()` → `["open_interest","funding_rate","mark_price","change_pct_24h"]`.
  - `async fetch_all()` — gathers per `settings.BYBIT_SYMBOLS`.
  - `async _fetch_for_symbol(session, symbol)` — `/v5/market/tickers?category=linear&symbol=...`; converts `price24hPcnt` decimal to %.
  - `_fmt`, `_float`; `get_funding`, `get_open_interest`, `get_mark_price`.

### `data_sources/sources/cftc_cot.py`
- `class CFTCCOTSource(BaseDataSource)`: `source_id="cftc_cot"`, `refresh_interval=settings.CFTC_REFRESH_SEC`.
  - `list_metrics()` → `["spec_net","spec_long","spec_short","open_interest","long_short_ratio"]`.
  - `async fetch_all()` — SODA query on `settings.CFTC_BASE_URL` filtered by `market_and_exchange_names=settings.CFTC_CONTRACT`, ordered desc by date, limit 1.
  - `get_spec_net / get_spec_long / get_spec_short / get_long_short_ratio`.

### `data_sources/sources/coingecko.py`
- Constants: `PUBLIC_BASE = "https://api.coingecko.com/api/v3"`, `PRO_BASE = "https://pro-api.coingecko.com/api/v3"`.
- `class CoinGeckoSource(BaseDataSource)`: `source_id="coingecko"`, `refresh_interval=settings.COINGECKO_REFRESH_SEC`, `requires_api_key=False`, `api_key_env_var="COINGECKO_API_KEY"`.
  - `list_metrics()` → `["btc_dominance","eth_dominance","total_market_cap","total_volume_24h","market_cap_change_pct_24h"]`.
  - `async fetch_all()` — calls `/global`.
  - `_auth() -> tuple[str, dict]` — picks base+header by `settings.COINGECKO_USE_PRO` and key presence (`x-cg-pro-api-key` / `x-cg-demo-api-key`).
  - `get_btc_dominance / get_eth_dominance / get_total_market_cap / get_total_volume_24h / get_market_cap_change_pct_24h`.

### `data_sources/sources/coinglass.py`
- Constants: `BASE = "https://open-api.coinglass.com/public/v2"`.
- `class CoinglassSource(BaseDataSource)`: `source_id="coinglass"`, `refresh_interval=settings.COINGLASS_REFRESH_SEC`.
  - `__init__()` — `_rate_limit = asyncio.Semaphore(settings.COINGLASS_RATE_LIMIT_PER_MIN)`.
  - `list_metrics()` → `["funding_rate","open_interest","liquidations_long_24h","liquidations_short_24h","long_short_ratio"]`.
  - `async fetch_all()` — gathers per `settings.COINGLASS_WATCH_PAIRS`.
  - `async _fetch_for_symbol(session, pair)` — strips `/USDT` to base symbol, hits `/funding_rates_chart`, `/open_interest_chart`, `/liquidation_chart`, `/long_short_ratio`.
  - `async _safe_get(session, path, params) -> dict` — semaphore-guarded.
  - `_extract_latest(payload, key) -> Optional[float]` — tolerates dict/list shapes.
  - `get_funding(pair) / get_open_interest / get_long_short_ratio / get_liquidations_24h(pair) -> dict` (long/short/total).

### `data_sources/sources/cryptocompare.py`
- `class CryptoCompareSource(BaseDataSource)`: `source_id="cryptocompare"`, `refresh_interval=settings.CRYPTOCOMPARE_REFRESH_SEC`, `requires_api_key=True`, `api_key_env_var="CRYPTOCOMPARE_API_KEY"`.
  - `list_metrics()` → `["price","volume_24h","market_cap","change_pct_24h"]`.
  - `async fetch_all()` — `/pricemultifull?fsyms=...&tsyms=...`; parses `RAW[fsym][tsym]`.
  - `get_price(fsym="BTC") / get_volume_24h / get_market_cap / get_change_pct_24h`.

### `data_sources/sources/ecb.py`
- `class ECBSource(BaseDataSource)`: `source_id="ecb"`, `refresh_interval=settings.ECB_REFRESH_SEC`.
  - `list_metrics()` — derived from `settings.ECB_SERIES` (tuples `(dataflow, key, metric)`).
  - `async fetch_all()` — gathers `_fetch_one` per series tuple.
  - `async _fetch_one(session, dataflow, key) -> Optional[float]` — SDMX-JSON `lastNObservations=1`.
  - `get_refinancing_rate / get_deposit_facility_rate / get_eur_usd`.

### `data_sources/sources/frankfurter.py`
- Constants: `DXY_LEGS: list[tuple[str, float]]` (6 pairs with signed exponents), `DXY_BASE = 50.14348112`, `_PAIR_FETCH: dict[str, tuple[str, str]]` (pair → base/quote).
- `class FrankfurterSource(BaseDataSource)`: `source_id="frankfurter"`, `refresh_interval=settings.FRANKFURTER_REFRESH_SEC`.
  - Class attr `METRICS = ("fx_rate","fx_change_24h","dxy","dxy_change_24h")`.
  - `list_metrics()` → list(METRICS).
  - `async fetch_all()` — `/latest` and `/<prev_date>` for 24h delta.
  - `get_dxy / get_dxy_change_24h / get_eur_usd / get_gbp_usd / get_usd_jpy`.
  - `is_dxy_strong() -> bool` — `dxy >= settings.DATA_DXY_STRONG_THRESHOLD`.
  - `is_dxy_weak() -> bool` — `dxy <= settings.DATA_DXY_WEAK_THRESHOLD`.
  - `async _fetch_rates(pairs, target_date=None) -> dict` — groups by base currency for fewer round trips.
  - `_dxy_proxy(rates) -> Optional[float]` — `DXY_BASE * Π rate^exp`; None if any leg missing.

### `data_sources/sources/fred.py`
- Constants: `OBS_ENDPOINT = "https://api.stlouisfed.org/fred/series/observations"`, `_SERIES_TO_METRIC` (9 entries: CPIAUCSL, DGS10, DGS2, DGS30, DFF, M2SL, UNRATE, T10Y2Y, VIXCLS), `_YOY_SERIES_TO_METRIC = {"CPIAUCSL": "cpi_yoy"}`.
- `class FREDSource(BaseDataSource)`: `source_id="fred"`, `refresh_interval=settings.FRED_REFRESH_SEC`, `requires_api_key=True`, `api_key_env_var="FRED_API_KEY"`.
  - `list_metrics()` — base + YoY metrics filtered by `settings.FRED_SERIES`.
  - `async fetch_all()` — per-series fetch + YoY (`units=pc1`) fetch.
  - `async _fetch_latest(session, series_id, api_key, units=None) -> tuple[Optional[float], str]` — skips `"."` placeholder, limit=5.
  - Accessors: `get_cpi / get_cpi_yoy / get_10y_yield / get_2y_yield / get_30y_yield / get_fed_funds / get_m2 / get_unemployment / get_yield_curve_spread` (falls back to 10y-2y subtraction), `is_yield_curve_inverted() -> bool`, `get_vix() -> Optional[float]`.
  - `get_risk_sentiment() -> str` — `"RISK_ON"|"NEUTRAL"|"RISK_OFF"|"CRISIS"|"UNKNOWN"` against `settings.DATA_VIX_RISK_ON_MAX / DATA_VIX_RISK_OFF_MIN / DATA_VIX_CRISIS_MIN`.

### `data_sources/sources/imf.py`
- `class IMFSource(BaseDataSource)`: `source_id="imf"`, `refresh_interval=settings.IMF_REFRESH_SEC`.
  - `list_metrics()` — `settings.IMF_INDICATORS.keys()`.
  - `async fetch_all()` — one HTTP per indicator; emits one DataPoint per (indicator × country) symbol.
  - `async _fetch_indicator(session, metric, imf_code) -> dict`.
  - `_latest(series) -> tuple[Optional[str], Optional[float]]` — most recent year with a value.
  - `get_gdp_per_capita(country="USA") / get_inflation_yoy / get_unemployment`.

### `data_sources/sources/us_treasury.py`
- Constants: `_HEADER_TO_METRIC` (9 tenors 1m..30y).
- `class USTreasurySource(BaseDataSource)`: `source_id="us_treasury"`, `refresh_interval=settings.US_TREASURY_REFRESH_SEC`.
  - `list_metrics()` — header values + `"yield_curve_2_10_spread"`.
  - `async fetch_all()` — fetches current YYYYMM CSV; parses first data row; derives 2y/10y spread.
  - `get_10y_yield / get_2y_yield / get_30y_yield / get_yield_curve_spread`.

### `data_sources/sources/world_bank.py`
- `class WorldBankSource(BaseDataSource)`: `source_id="world_bank"`, `refresh_interval=settings.WORLD_BANK_REFRESH_SEC`.
  - `list_metrics()` — `settings.WORLD_BANK_INDICATORS.keys()`.
  - `async fetch_all()` — gathers one HTTP per (country × indicator) pair.
  - `async _fetch_one(session, country, wb_code) -> tuple[Optional[float], Optional[str]]` — 5-year lookback, walks page for first non-null value.
  - `get_gdp_growth(country="USA") / get_inflation / get_unemployment`.

## Plugin registrations
`data_sources/sources/__init__.py:REGISTERED_SOURCES` — 13 entries instantiated at import time:

| Order | source_id | Class | refresh setting | API key env var | `is_available()` |
|---:|---|---|---|---|---|
| 1 | `coinglass` | `CoinglassSource` | `COINGLASS_REFRESH_SEC` | — | always True (no key) |
| 2 | `coingecko` | `CoinGeckoSource` | `COINGECKO_REFRESH_SEC` | `COINGECKO_API_KEY` (optional; raises rate limits only) | always True (`requires_api_key=False`) |
| 3 | `cryptocompare` | `CryptoCompareSource` | `CRYPTOCOMPARE_REFRESH_SEC` | `CRYPTOCOMPARE_API_KEY` | True iff key set |
| 4 | `binance_futures` | `BinanceFuturesSource` | `BINANCE_FUTURES_REFRESH_SEC` | — | always True |
| 5 | `bybit_derivs` | `BybitDerivsSource` | `BYBIT_REFRESH_SEC` | — | always True |
| 6 | `cftc_cot` | `CFTCCOTSource` | `CFTC_REFRESH_SEC` | — | always True |
| 7 | `fred` | `FREDSource` | `FRED_REFRESH_SEC` | `FRED_API_KEY` | True iff key set |
| 8 | `alpha_vantage` | `AlphaVantageSource` | `ALPHA_VANTAGE_REFRESH_SEC` | `ALPHA_VANTAGE_API_KEY` | True iff key set |
| 9 | `frankfurter` | `FrankfurterSource` | `FRANKFURTER_REFRESH_SEC` | — | always True |
| 10 | `ecb` | `ECBSource` | `ECB_REFRESH_SEC` | — | always True |
| 11 | `us_treasury` | `USTreasurySource` | `US_TREASURY_REFRESH_SEC` | — | always True |
| 12 | `world_bank` | `WorldBankSource` | `WORLD_BANK_REFRESH_SEC` | — | always True |
| 13 | `imf` | `IMFSource` | `IMF_REFRESH_SEC` | — | always True |

`is_available()` is the base implementation everywhere — no source overrides it. The base returns `True` when `requires_api_key=False`, else `bool(os.getenv(api_key_env_var, ""))`. CoinGecko explicitly leaves `requires_api_key=False` even though it exposes `api_key_env_var="COINGECKO_API_KEY"` so the source still runs on the public tier when no key is configured.

## API key env vars required

| Source | Env var | Required? |
|---|---|---|
| `alpha_vantage` | `ALPHA_VANTAGE_API_KEY` | yes (source disabled without it) |
| `cryptocompare` | `CRYPTOCOMPARE_API_KEY` | yes (source disabled without it) |
| `fred` | `FRED_API_KEY` | yes (source disabled without it) |
| `coingecko` | `COINGECKO_API_KEY` | optional — only switches header/base to demo/pro tier |
| `coinglass`, `binance_futures`, `bybit_derivs`, `cftc_cot`, `frankfurter`, `ecb`, `us_treasury`, `world_bank`, `imf` | — | none |

## Imports graph

### Imports from project
- `data_sources/__init__.py` → `database.queries` (for `log_data_point`), `data_sources.base`, lazy `data_sources.sources.REGISTERED_SOURCES`.
- `data_sources/base.py` → stdlib only (`abc`, `dataclasses`, `logging`, `time`, `typing`).
- `data_sources/sources/__init__.py` → all 13 concrete source modules.
- Every concrete source → `config.settings`, `data_sources.base`, `aiohttp`, stdlib.

### Imported by (project-wide)
- `core/bot.py` — `from data_sources import data_sources as ds` (3 lazy imports inside methods).
- `macro/monitor.py` — `from data_sources import data_sources as ds` (lazy inside `_refresh_inputs`).
- `execution/arb_engine.py` — `from data_sources import data_sources as ds` (2 lazy imports).
- `tests/test_data_sources.py`, `tests/test_arb_engine.py` (`import data_sources as ds_mod`).

Documentation references: `prompts/build_data_sources.md`, `prompts/build_data_sources_coinglass.md`, `prompts/build_funding_arb.md`, `prompts/build_macro.md`.

## Tests
Only `tests/test_data_sources.py` imports directly from `data_sources/`. Test breakdown (one line each):

- `test_base_caches_within_refresh_interval` — second `get` reuses cache within `refresh_interval`.
- `test_base_refreshes_when_stale` — `refresh_interval=0` forces a fresh fetch each call.
- `test_base_fetch_failure_keeps_cached_value` — `fetch_all` raising must not clobber the cache.
- `test_cached_value_is_sync` — `cached_value` returns the float; default kicks in on miss.
- `test_datapoint_key_with_and_without_symbol` — key format `source.metric[.symbol]`.
- `test_frankfurter_is_always_available` — keyless source is always available.
- `test_alpha_vantage_requires_key` — gated by `ALPHA_VANTAGE_API_KEY`.
- `test_fred_requires_key` — gated by `FRED_API_KEY`.
- `test_coinglass_no_key_required` — always available.
- `test_coingecko_works_without_key` — public-tier availability.
- `test_coingecko_auth_picks_right_header` — empty key → no header; demo key → `x-cg-demo`; pro flag → `x-cg-pro` on pro base.
- `test_coingecko_metrics_and_convenience_methods` — accessor wires through `_cache`.
- `test_coingecko_parses_global_payload` — happy-path payload → expected DataPoints.
- `test_coingecko_returns_error_point_on_empty_payload` — empty/error payload → DataPoint with `.error`.
- `test_each_source_lists_expected_metrics` — `list_metrics` sanity per registered source.
- `test_cryptocompare_requires_key` — gated by key env var.
- `test_no_key_sources_are_available` — eight keyless sources all True.
- `test_binance_futures_symbol_format` — `BTCUSDT` → `BTC/USDT`.
- `test_us_treasury_csv_parsing` — first data row parsed, spread derived.
- `test_cftc_parses_latest_row` — non-comm long/short/net/ratio extracted.
- `test_fred_risk_sentiment_thresholds` — VIX 10/20/28/40 → RISK_ON/NEUTRAL/RISK_OFF/CRISIS.
- `test_alpha_vantage_does_not_carry_vix` — explicitly verifies VIX moved to FRED.
- `test_alpha_vantage_paces_calls_between_symbols` — `PACE_SEC` sleep between consecutive symbols, not before first.
- `test_frankfurter_dxy_strength` — `is_dxy_strong/weak` thresholds.
- `test_frankfurter_dxy_proxy_formula` — geometric DXY against May-2026-ish snapshot lands in 95-105.
- `test_frankfurter_dxy_proxy_missing_leg_returns_none` — partial input → None.
- `test_fred_yield_curve_inversion_falls_back_to_legs` — when spread series absent, subtract 10y-2y.
- `test_aggregator_refresh_all_succeeds_concurrently` — concurrent refresh + DB log.
- `test_aggregator_isolates_one_failing_source` — bad source doesn't break good source.
- `test_aggregator_unavailable_source_is_skipped` — `is_available=False` → `fetch_all` not called.
- `test_aggregator_get_returns_datapoint` — `get` returns cached DataPoint; unknown source → None.
- `test_latest_snapshot_flattens_every_cache` — snapshot keys include every source.metric.
- `test_aggregator_never_imports_concrete_sources` — `inspect.getsource(data_sources)` must not name any concrete source class.
- `test_registered_sources_picked_up_automatically` — passing a new instance to `DataSources` exposes it as an attribute.
- `test_subscribe_fires_on_value_change` — first fire has `previous=None`; subsequent has populated `previous`.
- `test_subscribe_wildcard_matches_any_symbol` — `cg.funding_rate.*` matches both BTC and ETH symbols.
- `test_subscribe_filter_blocks_callback` — filter predicate False → callback skipped.
- `test_callback_exception_does_not_break_source` — throwing callback doesn't prevent the safe one.
- `test_unsubscribe_stops_notifications` — `unsubscribe(sub_id)` removes the listener.
- `test_multiple_subscribers_same_pattern` — both fire.
- `test_quality_gate_consumes_macro_modifier` — score = 80 raw − 10 macro = 70.
- `test_quality_gate_missing_macro_does_not_block` — macro raising doesn't block gate.
- `test_log_data_point_round_trip` — `log_data_point` + `get_latest_data_point` symmetry.
- `test_get_data_history_returns_chronological` — three logged points come back in chrono order.
- `test_get_data_at_time` — picks the row immediately preceding the cutoff.
- `test_get_funding_rates_empty_when_nothing_cached` — empty dict, not None.
- `test_get_funding_rates_returns_symbol_to_rate_map` — `{symbol: rate}` shape.
- `test_get_funding_rates_skips_errored_points` — error-state points excluded.
- `test_get_funding_rates_prefers_newest_when_multiple_sources` — newest timestamp wins.
- `test_get_latest_returns_cached_datapoint` — sync read, no fetch.
- `test_get_latest_returns_none_for_unknown_source` — unknown source → None.
- `test_coinglass_source_has_rate_limit_semaphore` — semaphore sized to `COINGLASS_RATE_LIMIT_PER_MIN`.

`tests/test_arb_engine.py` imports `data_sources as ds_mod` to monkeypatch the singleton when verifying the funding-rate arb engine; not a `data_sources` unit test.

## TODOs / FIXMEs / stubs
No `TODO`, `FIXME`, `XXX`, or `HACK` comments anywhere in `data_sources/`. (The string "stub" appears once at `data_sources/__init__.py:70` — `"Deferred import so test harnesses can stub REGISTERED_SOURCES"` — which is a docstring describing the deferred-import design and is not a stub marker.)

## Known issues observed
- `data_sources/sources/us_treasury.py:58` uses `datetime.utcnow()` (deprecated in Python 3.12+; the project pins 3.11 so it still works but will warn on future bumps). `world_bank.py:97` does the same via the locally-aliased `_dt`.
- `frankfurter.py` `_fetch_rates()` loop variable shadows the outer `base` URL: `for base, quotes in bases.items()` reuses the name of the URL `base = settings.FRANKFURTER_BASE_URL` defined a few lines earlier. Works because `path` is computed before the loop, but it's a footgun if anyone later moves URL construction inside the loop.
- `frankfurter.py` opens a fresh `aiohttp.ClientSession` inside `_fetch_rates` and `fetch_all` calls `_fetch_rates` twice (today + yesterday) → two sessions per refresh instead of one shared session. Minor: sessions are short-lived and the source-level cache means this fires once per `FRANKFURTER_REFRESH_SEC`.
- `binance_futures.py`/`bybit_derivs.py`/`coinglass.py` all use `or 0.0` / `or 1.0` defaults on numeric fields — masks the difference between "the API returned 0" and "the API returned nothing". The error flag is set correctly when the parse fails, but `cached_value()` returns `default` only when `error is not None`, so a genuine 0.0 reading and a missing-with-fallback 0.0 are indistinguishable to readers that don't inspect `point.error`.
- `coinglass.py:30` hardcodes `BASE = "https://open-api.coinglass.com/public/v2"` instead of going through `settings`. Every other source pulls its host from settings.
- `frankfurter.py` hardcodes `DXY_BASE = 50.14348112` and the leg exponents — by `CLAUDE.md` convention these are magic numbers that should live in `config/settings.py`. Defensible since they're ICE-spec constants and not tunable, but the file is the only place in `data_sources/` doing this.
- `coingecko.py`'s `_auth()` always picks `PRO_BASE` only when *both* `COINGECKO_USE_PRO` and a key are set, but the demo header is sent on the public base with any key — there's no validation that a pro-format key (`CG-…`) isn't being sent with `x-cg-demo-api-key`.
- `frankfurter.py:202` calls `base = settings.FRANKFURTER_BASE_URL` once and notes in the comment "v2 ... currently 404s on /latest" — implicit dependency on a settings choice that ties this source to a frozen v1 API.
- No source overrides `is_available()` beyond the default key-presence check — no network/import-reachability probe, so a configured source that can't actually reach its host will be silently treated as available and fail per-refresh through the base error path.
- `imf.py:73` and `world_bank.py:71` insert a DataPoint with `value=0.0, error="no_data"` when no observation exists. Because `BaseDataSource._do_fetch` won't clobber an existing good point with an error placeholder, this works on second runs — but on the *first* run for a brand-new country×indicator pair the cache will contain a `value=0.0` point that `cached_value()` then returns as `None` (because `error` is set). Consistent with intent but a future change to `cached_value()` would need to remember this contract.
- `data_sources/__init__.py:248` schedules subscriber callbacks with `asyncio.create_task` but no reference is kept — long-running callbacks can be garbage-collected mid-flight on Python 3.10+ if the loop has no other reference. Practical risk low because the tasks finish quickly, but worth noting.
