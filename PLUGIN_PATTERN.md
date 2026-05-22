# Plugin Pattern

CryptoBot uses one repeating pattern for every pluggable module: an
abstract base class, a registry list, and a shared discovery layer
that picks up new entries with no caller-side code changes.

---

## The Five Steps

To add a new plugin to any of the modules listed below:

1. **Create your file** in the module's `*/` (or `*/sources/`) directory.
2. **Subclass the base class** for that module (`BaseAgent`,
   `BaseDataSource`, `BaseSentimentSource`, `BaseCalendarSource`).
3. **Set the class attributes** that identify the plugin
   (`source_id`/`agent_id`, `display_name`, refresh interval, capital
   allocation, etc.).
4. **Implement the abstract methods.** Most are async; signatures are
   pinned by the base class.
5. **Append your instance to the registry list** in the module's
   `__init__.py` (or `sources/__init__.py`).

That's it. The orchestrator (coordinator / aggregator / monitor)
discovers it automatically — no changes to the orchestrator code, no
changes to the dashboard, no changes to settings unless your plugin
introduces new tunables.

---

## Where The Pattern Is Used

### Currently implemented

| Module          | Base class             | Registry list                | Implementations |
|-----------------|------------------------|------------------------------|-----------------|
| `agents/`       | `BaseAgent`            | `REGISTERED_AGENTS`          | Signal, Arb, Macro, Sentiment, OnChain, **Scalp** |
| `data_sources/` | `BaseDataSource`       | `REGISTERED_SOURCES`         | Coinglass, CoinGecko, CryptoCompare, FRED, AlphaVantage, Frankfurter, BinanceFutures, BybitDerivs, CFTC COT, WorldBank, IMF, ECB, USTreasury |
| `sentiment/`    | `BaseSentimentSource`  | `REGISTERED_SOURCES`         | FearGreed, CryptoPanic, Reddit, GoogleTrends, Telegram |
| `macro/sources/`| `BaseCalendarSource`   | `REGISTERED_CALENDAR_SOURCES`| FREDCalendar, StubCalendar |

### Pattern guarantees

- The orchestrator **never imports concrete plugin classes** — only the
  registry list. Tests pin this with `inspect.getsource()` checks.
- A plugin's `is_available()` is consulted before `start()` /
  `fetch()`. Unavailable plugins are skipped silently, not by raising.
- Plugin lifecycle errors are isolated — one failing source must not
  break the orchestrator or any sibling plugin.
- Tunable parameters live in `config/settings.py` with `# test:`
  ranges. The plugin reads them at module load.

See each module's `*/__init__.py` (or `sources/__init__.py`) for its
specific add-a-source docstring — they document the exact attrs and
method signatures expected.
