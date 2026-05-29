# Module Report: macro

## Purpose
The `macro/` package is the project's macro-economy awareness layer. It turns external macro inputs (VIX, DXY, the 10y-2y yield curve, fed funds rate, CPI YoY) into:

- A **three-layer regime model**: dimensional flag enums (`DollarStrength`, `RiskAppetite`, `RateEnvironment`, `VolRegime`) → a single-label `MacroScenario` from a priority-ordered ladder (`CRISIS` first, `NEUTRAL` last) → a clamped numeric `macro_score` in `[-100, +100]`.
- A **step-ladder signal modifier** (`get_signal_modifier()`) returning one of `{-15, -10, -5, 0, +5, +10, +15}`, intentionally mirroring `sentiment._composite_to_modifier`.
- A **hard-block flag** (`is_hard_blocked()` / `MacroRegime.is_hard_block`) that fires on `MacroScenario.CRISIS`, mirroring the sentiment news guard.
- An **economic-calendar awareness layer** through a plugin registry of `BaseCalendarSource` subclasses; the monitor exposes upcoming `PendingEvent`s for the dashboard's pending-events panel and the bot's pre-event pause.
- A list of discrete `MacroSignal`s (e.g. `VIX_CRISIS`, `YIELD_CURVE_INVERSION`, `DOLLAR_STRENGTH`) generated each refresh for a future `MacroAgent` consumer (currently computed but unused).

**Feed into QualityGate.** In `signals/quality_gate.py:142` (section "5b. Macro modifier"), the gate calls `macro_monitor.get_signal_modifier()`, adds it to `score`, and stamps `signal.indicators["macro_modifier"]` plus `signal.indicators["macro_scenario"]` when a regime is cached. This sits **after** the regime modifier (5a) and **before** OFI/sentiment in the additive composition described by `CLAUDE.md`. The CRISIS hard-block (`MACRO_HARD_BLOCK_SKIP_REASON = "MACRO_HARD_BLOCK"`) is wired separately. The session/pre-event pause referenced in the spec is sourced from `MACRO_PRE_EVENT_PAUSE_MINUTES` against `get_pending_events()`. Macro never owns the `session_modifier` — that's a separate concern; macro contributes only the score modifier and CRISIS short-circuit.

## Subpackages
- `macro/sources/` — calendar/event feeds. `BaseCalendarSource` ABC + concrete sources (`FREDCalendarSource`, `StubCalendarSource`) + an ordered `REGISTERED_CALENDAR_SOURCES` registry. Same 5-step plugin pattern as `sentiment/` and `data_sources/` (documented in `macro/sources/__init__.py`).

## Files
| File | LOC | One-sentence summary |
|---|---|---|
| `macro/__init__.py` | 48 | Re-exports the public surface (`MacroMonitor`, `macro_monitor` singleton, regime enums, signal dataclasses, `BaseCalendarSource`) and instantiates the module-level singleton. |
| `macro/monitor.py` | 517 | `MacroMonitor` orchestrator: per-component score curves, dimensional classifiers, scenario priority ladder, `refresh()`/`refresh_calendar()`/`run_refresh_loop()`, signal modifier, hard-block flag, pending-events read-through with DB fallback, derived `MacroSignal` list. |
| `macro/regime.py` | 98 | Dimensional enums (`DollarStrength`, `RiskAppetite`, `RateEnvironment`, `VolRegime`), the `MacroScenario` enum (priority-ordered), and the `MacroRegime` dataclass with clamped post-init invariants and `is_hard_block` property. |
| `macro/signals.py` | 110 | `EventImpact` enum + `CalendarEvent` (plugin-emitted), `PendingEvent` (dashboard view with computed `minutes_until`), and `MacroSignal` (discrete event for the future `MacroAgent`). |
| `macro/sources/__init__.py` | 42 | Ordered `REGISTERED_CALENDAR_SOURCES` registry + 5-step plugin docstring for adding a source. |
| `macro/sources/base.py` | 46 | `BaseCalendarSource` ABC: class attrs (`source_id`, `display_name`, `refresh_interval`, `optional`, `requires_api_key`, `api_key_env_var`), default `is_available()` that env-var-checks when `requires_api_key`, abstract `fetch_events()`. |
| `macro/sources/fred_calendar.py` | 165 | Real source: FRED `/fred/releases/dates` endpoint, keyword→`EventImpact` mapping, default 13:30 UTC release time with FOMC override to 18:00, drops `LOW`-impact entries. |
| `macro/sources/stub_calendar.py` | 51 | Placeholder source — always `is_available() == False`, exists in the registry to keep the plugin docstring discoverable. |

## Public surface

### `macro/__init__.py`
- Module docstring: lists the public surface and points at `macro/sources/__init__.py` for the plugin docstring.
- Module-level singleton: `macro_monitor = MacroMonitor()` (CLAUDE.md singleton pattern).
- `__all__`: `MacroMonitor`, `macro_monitor`, `MacroRegime`, `MacroScenario`, `DollarStrength`, `RiskAppetite`, `RateEnvironment`, `VolRegime`, `CalendarEvent`, `PendingEvent`, `EventImpact`, `MacroSignal`, `BaseCalendarSource`.

### `macro/monitor.py`
Module docstring: "reads data_sources singleton + calendar plugin, computes a MacroRegime each tick, exposes a step-ladder signal modifier". Notes that the monitor never fetches external APIs directly except calendar events.

Module-level functions (per-component score curves, all returning floats in `[-100, +100]`):
- `_vix_score(vix: Optional[float]) -> float` — bands keyed off `settings.MACRO_VIX_CALM / MACRO_VIX_ELEVATED / MACRO_VIX_CRISIS`; returns 100 (<calm), 30 (calm-elevated), -60 (elevated-crisis), -100 (>=crisis), 0 if None.
- `_dollar_score(dxy: Optional[float]) -> float` — 100 if `dxy <= MACRO_DXY_WEAK`, -100 if `dxy >= MACRO_DXY_STRONG`, 0 otherwise (and 0 if None).
- `_yield_curve_score(spread: Optional[float]) -> float` — -100 if `spread < MACRO_YIELD_CURVE_INVERSION`, 60 if `spread > 0.5`, 0 otherwise.
- `_rate_env_score(rates: RateEnvironment) -> float` — 100 for `EASING`, -100 for `TIGHTENING`, 0 for `NEUTRAL`.
- `_inflation_score(cpi_yoy: Optional[float]) -> float` — 100 if `cpi_yoy < MACRO_INFLATION_LOW`, -100 if `cpi_yoy >= MACRO_INFLATION_HIGH`, 0 otherwise.
- `_score_to_modifier(macro_score: float) -> int` — step ladder: `>=60`→+15, `>=40`→+10, `>=20`→+5, `<=-60`→-15, `<=-40`→-10, `<=-20`→-5, else 0. Docstring: "mirrors sentiment._composite_to_modifier".

Class `MacroMonitor`:
- Docstring: "Reads macro inputs from data_sources, computes a MacroRegime, exposes pull API for the bot loop + dashboard. Construct with no args to use REGISTERED_CALENDAR_SOURCES. Tests inject their own list to pin behaviour."
- Instance attrs: `_calendar_sources: list`, `_regime: Optional[MacroRegime]`, `_signals: list[MacroSignal]`, `_events: list[CalendarEvent]`, `_last_calendar_fetch: float`, `_running: bool`.
- `__init__(self, calendar_sources: Optional[list] = None)` — instantiates each class in `REGISTERED_CALENDAR_SOURCES` when no list is given.
- `async refresh(self) -> MacroRegime` — pulls inputs via deferred `from data_sources import data_sources`, derives flags + scenario + score + confidence, persists via `db_queries.save_macro_regime` (best-effort), caches `_regime` and `_signals`. Always returns a regime even when everything fails.
- `get_current_regime(self) -> Optional[MacroRegime]` — latest cached regime or `None`.
- `get_signal_modifier(self) -> int` — `_score_to_modifier(self._regime.macro_score)` or `0` when no regime is cached.
- `is_hard_blocked(self) -> bool` — `True` iff cached regime's scenario is `CRISIS`.
- `get_macro_signals(self) -> list[MacroSignal]` — copy of last derived `MacroSignal` list.
- `async refresh_calendar(self) -> list[CalendarEvent]` — iterates `_calendar_sources`, skips when `is_available() == False`, swallows per-source exceptions, persists via `db_queries.save_calendar_events`, updates `_last_calendar_fetch`.
- `get_pending_events(self, n: int = 3) -> list[PendingEvent]` — in-memory future events first, then DB fallback via `db_queries.get_pending_events(hours_ahead=72)`, sorted ascending by `scheduled_utc`, capped at `n`.
- `async run_refresh_loop(self) -> None` — refreshes the calendar once before the first regime read, then loops `refresh()` + hourly `refresh_calendar()` with `await asyncio.sleep(settings.MACRO_REFRESH_INTERVAL_SEC)`. Caller cancels.
- `stop(self) -> None` — flips `_running = False`.
- `_classify_dollar(self, dxy) -> DollarStrength`, `_classify_vol(self, vix) -> VolRegime`, `async _classify_rates(self, fed_funds) -> RateEnvironment` (looks up `db_queries.get_data_at_time("fred", "fed_funds", ref_time)` for delta vs `MACRO_RATE_LOOKBACK_DAYS` ago), `_classify_risk(self, vol, dollar) -> RiskAppetite`.
- `_scenario_for(self, dollar, risk, rates, vol, yield_curve, cpi_yoy) -> MacroScenario` — priority ladder: CRISIS → RISK_OFF (elevated vol + inverted-or-strong-dollar) → STAGFLATION → TIGHTENING_CYCLE → EASING_CYCLE → REFLATION → GOLDILOCKS → NEUTRAL.
- `_safe(self, ds, source_id, method) -> Optional[float]` — wraps `getattr(ds, source_id).method()` in try/except.
- `_confidence(self, **fields) -> float` — fraction of macro inputs present; halved when `db_queries.get_latest_data_point("fred", "vix")` is older than `MACRO_CONFIDENCE_STALE_HOURS`.
- `_derive_signals(self, regime) -> list[MacroSignal]` — emits `VIX_CRISIS`/`VIX_ELEVATED`, `YIELD_CURVE_INVERSION`, `DOLLAR_STRENGTH` events.

Module constants (used as score-band reference for tests that pin behaviour): all bands route through `settings.MACRO_*` — there are no in-module magic numbers in the score curves.

### `macro/regime.py`
Module docstring: documents the three-layer model (dimensional flags → scenario → numeric score).
- `class DollarStrength(enum.Enum)`: `STRONG`, `NEUTRAL`, `WEAK`.
- `class RiskAppetite(enum.Enum)`: `RISK_ON`, `NEUTRAL`, `RISK_OFF`.
- `class RateEnvironment(enum.Enum)`: `TIGHTENING`, `NEUTRAL`, `EASING`.
- `class VolRegime(enum.Enum)`: `CALM`, `ELEVATED`, `CRISIS`.
- `class MacroScenario(enum.Enum)`: priority-ordered `CRISIS`, `RISK_OFF`, `STAGFLATION`, `TIGHTENING_CYCLE`, `EASING_CYCLE`, `REFLATION`, `GOLDILOCKS`, `NEUTRAL`. Docstring: "most severe scenarios are tested first in MacroMonitor.scenario_for() so they win when multiple labels could apply."
- `@dataclass class MacroRegime`:
  - Required fields: `scenario: MacroScenario`, `dollar: DollarStrength`, `risk: RiskAppetite`, `rates: RateEnvironment`, `vol: VolRegime`, `macro_score: float`.
  - Optional fields (default `None`): `dxy`, `vix`, `yield_10y`, `yield_2y`, `yield_curve`, `fed_funds_rate`, `cpi_yoy`.
  - `last_updated: float = 0.0`, `confidence: float = 0.0`, `raw_data: dict = field(default_factory=dict)`.
  - `__post_init__(self)` — defaults `last_updated` to `time.time()` and clamps `macro_score` to `[-100, 100]`, `confidence` to `[0, 1]`.
  - `@property is_hard_block(self) -> bool` — `self.scenario is MacroScenario.CRISIS`.

### `macro/signals.py`
Module docstring: "Dataclasses for calendar events + macro signal generation." Notes that `CalendarEvent` lives in the macro module rather than `database/models` so plugins don't need a SQLAlchemy dependency.
- `class EventImpact(enum.Enum)`: `HIGH`, `MEDIUM`, `LOW`.
- `@dataclass class CalendarEvent`:
  - Fields: `event_id: str`, `title: str`, `country: str`, `scheduled_utc: datetime`, `impact: EventImpact`, `source_id: str`, optional `actual / forecast / previous: Optional[float] = None`.
  - Docstring: `event_id` must be stable across refreshes for DB upserts (e.g. `"fred:release_<id>:2026-05-21"`).
  - `minutes_until(self, now: Optional[datetime] = None) -> int` — handles both tz-aware and naive `scheduled_utc`, returns negative when past.
- `@dataclass class PendingEvent`:
  - Fields: `title`, `country`, `scheduled_utc`, `minutes_until: int`, `impact: EventImpact`.
  - `@classmethod from_calendar_event(cls, ev: CalendarEvent, now: Optional[datetime] = None) -> PendingEvent`.
- `@dataclass class MacroSignal`:
  - Fields: `signal_id: str`, `signal_type: str` (e.g. `"VIX_ELEVATED"`, `"YIELD_CURVE_INVERSION"`), `direction: str` (`"RISK_ON" | "RISK_OFF"`), `strength: float` (0..1), `description: str`, `source_metrics: dict = field(default_factory=dict)`, `timestamp: float = 0.0`.
  - `__post_init__(self)` — defaults `timestamp` to `time.time()` and clamps `strength` to `[0, 1]`.

### `macro/sources/__init__.py`
Module docstring is the 5-step plugin recipe.
- Module constant: `REGISTERED_CALENDAR_SOURCES: list[type] = [FREDCalendarSource, StubCalendarSource]`. Stub stays in the list so its docstring is greppable.

### `macro/sources/base.py`
Module docstring: ABC mirrors `BaseSentimentSource` / `BaseDataSource`; intentionally omits cache/staleness because calendar events are discrete.
- `class BaseCalendarSource(ABC)`:
  - Class attrs (override per source): `source_id: str = "base_calendar"`, `display_name: str = "Base Calendar"`, `refresh_interval: int = 3600`, `optional: bool = True`, `requires_api_key: bool = False`, `api_key_env_var: str = ""`.
  - `@abstractmethod async fetch_events(self) -> list` — docstring requires returning `[]` on failure, never raising.
  - `is_available(self) -> bool` — default: `True` if `not requires_api_key`, else `bool(os.getenv(self.api_key_env_var, ""))`.

### `macro/sources/fred_calendar.py`
Module docstring lists caveats: FRED publishes dates only (defaults to 13:30 UTC, 18:00 UTC for FOMC), `include_release_dates_with_no_data=true`, all releases are hard-coded `country = "US"`.

Module constants:
- `RELEASES_ENDPOINT = "https://api.stlouisfed.org/fred/releases/dates"`.
- `_HIGH_KEYWORDS = ("fomc", "consumer price index", "employment situation", "personal consumption expenditures", "gross domestic product")`.
- `_MEDIUM_KEYWORDS = ("producer price index", "retail", "initial claims", "unemployment insurance", "housing starts", "existing home sales", "new residential sales", "industrial production", "ism manufacturing", "consumer sentiment")`.
- `_DEFAULT_HOUR_UTC = 13`, `_DEFAULT_MINUTE_UTC = 30`, `_FOMC_HOUR_UTC = 18`.

`class FREDCalendarSource(BaseCalendarSource)`:
- Class attrs: `source_id = "fred_calendar"`, `display_name = "FRED Release Calendar"`, `refresh_interval = 3600`, `optional = True`, `requires_api_key = True`, `api_key_env_var = "FRED_API_KEY"`.
- `__init__(self, lookahead_days: int = 14)`.
- `async fetch_events(self) -> list[CalendarEvent]` — uses `aiohttp` with timeout `settings.DATA_SOURCES_HTTP_TIMEOUT_SEC`; catches any HTTP failure to `return []` and log a warning.
- `_parse_entry(self, entry: dict) -> Optional[CalendarEvent]` — drops `EventImpact.LOW` events to keep the panel focused; emits `event_id=f"fred:{release_id}:{date_str}"`.
- `_impact_for(self, name: str) -> EventImpact` — lowercase substring match; HIGH checked before MEDIUM by design.
- `_time_for(self, name: str) -> tuple[int, int]` — FOMC → 18:00 UTC, everything else → 13:30 UTC.

### `macro/sources/stub_calendar.py`
Module docstring: placeholder + 5-step "to implement a real source against this slot" recipe (Trading Economics / Investing.com / Forex Factory / econoday / MarketAux).
- `class StubCalendarSource(BaseCalendarSource)`:
  - Class attrs: `source_id = "stub_calendar"`, `display_name = "Stub Calendar (placeholder)"`, `refresh_interval = 86400`, `optional = True`.
  - `is_available(self) -> bool` — always `False`.
  - `async fetch_events(self) -> list[CalendarEvent]` — returns `[]`.

## Plugin registrations
`REGISTERED_CALENDAR_SOURCES` in `macro/sources/__init__.py` (ordered):

| # | Class | `source_id` | API key env var | `is_available()` | Notes |
|---|---|---|---|---|---|
| 1 | `FREDCalendarSource` | `fred_calendar` | `FRED_API_KEY` | default — `True` iff env var set | Real source. Hourly refresh, 14-day lookahead, US-only, drops LOW impact. |
| 2 | `StubCalendarSource` | `stub_calendar` | — (no `api_key_env_var`) | overridden — always `False` | Placeholder; documented plugin slot. |

Both subclasses set `optional = True`. No source sets `requires_api_key = True` other than FRED.

## Imports graph

**Imports from project (within `macro/`):**
- `macro/__init__.py` → `macro.monitor`, `macro.regime`, `macro.signals`, `macro.sources.base`.
- `macro/monitor.py` → `config.settings`, `database.queries` (as `db_queries`), `macro.regime`, `macro.signals`, `macro.sources` (for `REGISTERED_CALENDAR_SOURCES`). **Deferred import** inside `refresh()`: `from data_sources import data_sources` (avoids `macro→data_sources→…` cycles at module load).
- `macro/regime.py` → stdlib only (`enum`, `time`, `dataclasses`, `typing`).
- `macro/signals.py` → stdlib only.
- `macro/sources/__init__.py` → `macro.sources.fred_calendar`, `macro.sources.stub_calendar`.
- `macro/sources/base.py` → stdlib only.
- `macro/sources/fred_calendar.py` → `config.settings`, `macro.signals`, `macro.sources.base`, third-party `aiohttp`.
- `macro/sources/stub_calendar.py` → `macro.signals`, `macro.sources.base`.

**Imported by (grep `from macro` / `import macro`):**
- `core/bot.py:287` and `core/bot.py:514` — `from macro import macro_monitor` (lazy; spawns `run_refresh_loop()` and consults `is_hard_blocked()` / `get_pending_events()` in the bot loop).
- `signals/quality_gate.py:142` — `from macro import macro_monitor` inside `evaluate()`; the QualityGate consumer (see Purpose).
- `ui/dashboard.py:604`, `ui/dashboard.py:873` — `from macro import macro_monitor` for the macro panel and pending-events panel.
- `prompts/build_macro.md:398` — documentation reference (`python -c "from macro import macro_monitor; ..."`).
- `tests/test_macro.py` — exercises every public symbol; see Tests.
- `tests/test_dashboard.py:363` — imports `macro.regime` enums for the dashboard fixture.

No circular-import cycles observed at import time; the `data_sources` dependency is lazily imported inside `refresh()`, which the module docstring explicitly calls out.

## Tests
Source: `tests/test_macro.py`. All tests import from `macro/`. (Also: `tests/test_dashboard.py` imports `macro.regime` for dashboard fixtures but isn't a macro-focused test.)

Score curves:
- `test_vix_score_curve` — pins the four `_vix_score` bands (100 / 30 / -60 / -100) plus the `None`→0 sentinel.
- `test_dollar_score_curve` — pins `_dollar_score` at WEAK / NEUTRAL / STRONG plus `None`→0.
- `test_yield_curve_score` — pins inverted (-100), flat (0), positive (60) bands plus `None`→0.
- `test_inflation_score` — pins low (100), mid (0), high (-100) bands plus `None`→0.
- `test_score_to_modifier_step_ladder` — pins the seven-rung modifier ladder; docstring notes "mirrors sentiment._composite_to_modifier".

Dimensional classifiers + scenario priority (`@pytest.mark.asyncio`):
- `test_crisis_fires_on_high_vix` — VIX=40 → `MacroScenario.CRISIS` + `is_hard_blocked() == True`.
- `test_goldilocks_fires_on_supportive_inputs` — weak DXY + calm VIX + low CPI → `GOLDILOCKS`, `RISK_ON`, modifier `+15`.
- `test_risk_off_fires_on_inverted_curve_plus_elevated_vix` — elevated VIX + inverted 10y-2y → `RISK_OFF`; verifies the `yield_curve` arithmetic at `-1.0`.
- `test_confidence_degrades_with_missing_data` — 3-of-6 inputs `None` → confidence in `(0.4, 0.6)`.
- `test_missing_data_source_does_not_crash` — even with `data_sources` import poisoned, `refresh()` still emits a regime with `confidence == 0.0`.

Calendar plugin:
- `test_stub_calendar_is_unavailable` — `StubCalendarSource().is_available() is False`.
- `test_stub_calendar_returns_empty` — `await fetch_events() == []`.
- `test_fred_calendar_requires_api_key` — `FREDCalendarSource().is_available()` flips with `FRED_API_KEY`.
- `test_fred_calendar_impact_keyword_mapping` — pins HIGH/MEDIUM/LOW keyword routing.
- `test_fred_calendar_parses_fomc_time_correctly` — FOMC → 18:00 UTC, HIGH impact.
- `test_fred_calendar_drops_low_impact_releases` — Commercial Paper returns `None` (LOW filter).
- `test_calendar_event_minutes_until` — positive when future.
- `test_new_calendar_source_picked_up_via_registry` — drop-in plugin gets included in `_calendar_sources`.

Pre-event pause:
- `test_get_pending_events_filters_past_and_orders_by_time` — past dropped, future sorted ascending.

DB round-trips (uses a `temp_db` fixture that swaps `settings.DB_PATH` and reloads `database.db` + `database.queries`):
- `test_save_macro_regime_round_trip` — `save_macro_regime` → `get_macro_history(hours=1)` returns the row with strings for the enums.
- `test_save_calendar_events_upserts_on_event_id` — same `event_id` overwrites the row (forecast populated post-release).
- `test_get_pending_events_filters_window` — `hours_ahead` window respected.

Dashboard panels:
- `test_macro_panel_renders_without_regime` — `_panel_macro()` renders to `/dev/null` console without raising when `get_current_regime()` returns `None`.
- `test_macro_panel_renders_with_regime` — same panel with a `REFLATION` sample regime.
- `test_pending_events_panel_renders_with_high_impact_soon` — `_panel_pending_events()` renders with FOMC+CPI pending.

## TODOs / FIXMEs / stubs
A `grep "TODO|FIXME|XXX|HACK"` of `macro/` returns no matches. There are no inline TODO/FIXME/XXX/HACK markers in the package.

Implicit stubs (documented but not real):
- `macro/sources/stub_calendar.py:40` — `class StubCalendarSource` exists only as a documented placeholder; `is_available()` always returns `False`.
- `macro/monitor.py:463 _derive_signals` (docstring) — "Discrete events for the future MacroAgent. Computed but not consumed — the agent picks these up when it lands." → `MacroSignal`s are produced on every refresh but no consumer exists yet (the `agents/` package doesn't import them).
- `macro/signals.py:91 MacroSignal` (class docstring) — "These are generated by MacroMonitor (…), not consumed yet — the agent is a placeholder."

## Known issues observed
- **Calendar-event timezones are silently coerced.** `CalendarEvent.minutes_until` accepts both tz-aware and naive `scheduled_utc`. `FREDCalendarSource._parse_entry` builds naive datetimes via `datetime.strptime(...).replace(hour=…)`, and `MacroMonitor.get_pending_events` compares with `datetime.utcnow()` (naive). Mixing tz-aware events from a future source with the naive `now` from the monitor will work by coincidence (the `astimezone(...).replace(tzinfo=None)` branch in `minutes_until` strips it) but the convention is implicit, not enforced. Documented at `macro/signals.py:55-58` ("source guarantees scheduled_utc is in UTC by convention").
- **`country` field of FRED events is hard-coded `"US"`.** Acknowledged in the source docstring (`fred_calendar.py:14`), but until a non-US source lands, the dashboard's country column will always be `US`.
- **FRED scheduled times are best-effort.** `_DEFAULT_HOUR_UTC = 13`, `_DEFAULT_MINUTE_UTC = 30`, `_FOMC_HOUR_UTC = 18` — magic numbers, documented in the module header but not configurable via `settings`. The `MACRO_PRE_EVENT_PAUSE_MINUTES` window therefore operates on times that may be off by hours for non-08:30-ET releases.
- **`_yield_curve_score` `0.5` boundary is a magic number.** `_yield_curve_score` returns `60` when `spread > 0.5` (line 70). The 0.5 threshold is not in `settings.MACRO_*` (which only exposes `MACRO_YIELD_CURVE_INVERSION`). Per `CLAUDE.md` ("Don't introduce magic numbers in other modules; add a constant in settings.py"), this is a small but real settings-discipline gap.
- **`_derive_signals` `strength=min(1.0, abs(yield_curve)/1.0)`** — dividing by `1.0` is a no-op; presumably a placeholder for a future settings-driven normaliser.
- **`MacroSignal` is dead-on-arrival**. `_derive_signals` runs every refresh, the result is cached in `self._signals` and surfaced via `get_macro_signals()`, but no code anywhere imports / consumes `get_macro_signals()` (no grep hits outside `macro/` and `tests/test_macro.py`). It's documented as "for the future MacroAgent" but right now is dead code in production.
- **`_confidence` staleness probe assumes the `fred` data source.** The check hardcodes `db_queries.get_latest_data_point("fred", "vix")` (line 453). If FRED is unreachable but other sources are fresh, confidence stays full. This is reasonable given VIX's weight in the composite, but is undocumented.
- **`refresh()`'s deferred import error path returns `None` from `getattr(ds, source_id, None)`.** If `data_sources` is importable but lacks one of the expected attribute names (`frankfurter`, `fred`), `_safe` silently returns `None` — feeds neutralised inputs into the regime without a warning. The fallback is debug-logged inside `_safe`, but a misnamed attribute will degrade the regime without an obvious operator signal.
- **`MacroMonitor.run_refresh_loop` swallows `refresh()` failures with `exc_info=True` but never backs off.** A persistently failing data source will log a traceback every `MACRO_REFRESH_INTERVAL_SEC` (default 300s) with no rate-limiting beyond that interval.
- **No tests exercise `_classify_rates` with non-None history.** `test_*` always patches `get_data_at_time` to return `None`, so `RateEnvironment.TIGHTENING`/`EASING` branches against `MACRO_RATE_CHANGE_TIGHTENING / EASING` thresholds are uncovered. Coverage for `TIGHTENING_CYCLE`, `EASING_CYCLE`, and `STAGFLATION` scenarios is therefore indirect.
- **`run_refresh_loop` uses `time.time()` directly** rather than an injectable clock, complicating deterministic timing tests. Minor.
