# Module Report: `utils/`

## Purpose

Cross-cutting utility helpers used throughout the bot. Currently provides two
self-contained capabilities:

1. **Rolling Hurst exponent** (`utils/hurst.py`) — Rescaled-Range (R/S) analysis
   for regime classification (trending / reverting / random). Consumed by
   `core/regime_detector.py` to bias the score modifier applied inside
   `QualityGate`.
2. **Structured logging setup** (`utils/logger.py`) — root-logger configuration
   with console + rotating-file handlers and noisy-library silencing. Called
   exactly once from `main.py` at startup.

The package has no shared state, no re-exports, and the `__init__.py` is empty
(zero bytes), so `utils` is purely a namespace for individually-imported
sub-modules.

## Files

| Path | LOC | Summary |
|---|---:|---|
| `utils/__init__.py` | 0 | Empty package marker. No exports, no docstring. |
| `utils/hurst.py` | 130 | R/S Hurst exponent: `hurst_rs()` function + `RollingHurst` stateful wrapper + private `_compute_rs()` helper. |
| `utils/logger.py` | 44 | `setup_logging(debug)` — configures root logger with console + `TimedRotatingFileHandler` and silences ccxt/asyncio/urllib3/telethon/praw. |
| **Total** | **174** | |

## Public Surface

### `utils/__init__.py`

Empty file (0 bytes). No docstring, no `__all__`, no re-exports. Acts purely as
a package marker so `utils.hurst` and `utils.logger` are importable.

### `utils/hurst.py`

**Module docstring** (lines 1–12):

> Rolling Hurst exponent via Rescaled Range (R/S) analysis.
>
> H > 0.55 → trending (persistent, momentum favoured)
> H < 0.48 → reverting (anti-persistent, mean reversion favoured)
> H ≈ 0.50 → random walk (no edge, avoid directional trades)
>
> Academic basis: Lo (1991) R/S analysis. Widely validated on crypto time
> series showing regime-dependent Hurst behaviour.

Note: the 0.55 / 0.48 numbers in the docstring are illustrative — actual
thresholds come from `config/settings.py` (`HURST_TRENDING_MIN`,
`HURST_REVERTING_MAX`) read lazily inside `RollingHurst.classify`.

**Module constants**: none.

**Module-level logger**: `logger = logging.getLogger(__name__)` (line 18).

#### Functions

- `hurst_rs(series: np.ndarray) -> Optional[float]` (lines 21–71)
  Compute Hurst exponent via R/S analysis on a price series. Minimum 50 values
  required. Internally converts to log returns (`np.diff(np.log(series + 1e-12))`),
  builds a list of sub-period lags starting at `min_lag = 10` and growing by
  `int(lag * 1.5) + 1` up to `n // 2`. Computes mean R/S per lag, then fits
  `log(R/S) ~ slope * log(n)` via `np.polyfit` of degree 1. Result clamped to
  `[0.0, 1.0]`. Returns `None` if `< 50` samples, `< 4` valid lag points, or on
  any exception (which is logged at DEBUG).

- `_compute_rs(returns: np.ndarray, lag: int) -> Optional[float]` (lines 74–93)
  Private helper. Chunks `returns` into `n // lag` non-overlapping segments;
  for each chunk computes range of mean-centred cumulative sum divided by
  sample std (`ddof=1`). Returns mean R/S across chunks, or `None` if no valid
  chunks. Skips chunks with `s == 0` and chunks shorter than 2.

#### Classes

- **`RollingHurst`** (lines 96–130)

  > Maintains a rolling Hurst exponent for a single price series. Call
  > `update()` on each new candle close.

  | Method | Signature | Notes |
  |---|---|---|
  | `__init__` | `(self, lookback: int = 200)` | Stores `self._prices: list[float] = []` and `self._current: Optional[float] = None`. |
  | `update` | `(self, price: float) -> Optional[float]` | Appends `price`, pops front when over `lookback`, recomputes Hurst once buffer holds ≥ 50 prices. Returns current value. |
  | `value` (property) | `-> Optional[float]` | Cached `_current` value without recomputation. |
  | `classify` | `(self) -> str` | Returns `"trending"` / `"reverting"` / `"random"` / `"unknown"`. **Lazy import** of `HURST_TRENDING_MIN` and `HURST_REVERTING_MAX` from `config.settings` inside the method body (line 125). |

### `utils/logger.py`

**Module docstring** (lines 1–4):

> Structured logging setup. Logs to console and rotating file.

**Module constants**: none. Pulls `LOGS_DIR`, `LOG_LEVEL`, `LOG_TO_FILE` from
`config.settings` at import time.

#### Functions

- `setup_logging(debug: bool = False) -> None` (lines 12–44)
  Idempotency note: there is **no guard** against repeated calls — each
  invocation appends new handlers to the root logger. Behaviour:
  1. `LOGS_DIR.mkdir(parents=True, exist_ok=True)`
  2. Level = `DEBUG` if `debug` else `getattr(logging, LOG_LEVEL)`. No
     validation of `LOG_LEVEL`; an unknown string raises `AttributeError`.
  3. Formatter: `"%(asctime)s  %(levelname)-8s  %(name)-25s  %(message)s"` with
     `datefmt="%H:%M:%S"` (date is dropped from console output — file rotation
     by day still records date in filename).
  4. Console handler (`StreamHandler`) set to `level`.
  5. If `LOG_TO_FILE`: `TimedRotatingFileHandler` writing
     `LOGS_DIR / "cryptobot.log"`, rotating at midnight, keeping 30 backups,
     UTF-8 encoded, fixed at `DEBUG` regardless of console level. Comment on
     line 24 says "INFO+ only" but the code actually applies the parameterised
     `level`.
  6. Silences `ccxt`, `asyncio`, `urllib3`, `telethon`, `praw` to `WARNING`.

## Imports Graph

### Imports from project

- `utils/hurst.py`:
  - `from config.settings import HURST_TRENDING_MIN, HURST_REVERTING_MAX`
    (lazy, inside `RollingHurst.classify`, line 125).
- `utils/logger.py`:
  - `from config.settings import LOGS_DIR, LOG_LEVEL, LOG_TO_FILE` (line 9,
    top-level — settings must import cleanly before logging is set up).
- `utils/__init__.py`: no imports.

### Third-party / stdlib imports

- `hurst.py`: `numpy`, `logging`, `typing.Optional`.
- `logger.py`: `logging`, `logging.handlers`, `pathlib.Path` (imported but not
  used directly — `LOGS_DIR` is already a `Path` from settings).

### Imported by (project-wide grep of `from utils` / `import utils`)

- `core/regime_detector.py:24` — `from utils.hurst import RollingHurst`
- `main.py:38` — `from utils.logger import setup_logging`

No other project module imports anything from `utils`. `hurst_rs` (the standalone
function) and `_compute_rs` have no external callers; only `RollingHurst` is
consumed downstream.

## Tests

- No tests reference `utils.*`. A repository-wide grep for `utils` under
  `tests/` returns only one unrelated hit:
  `tests/test_web_server.py:22` — `from aiohttp.test_utils import TestClient, TestServer`
  (third-party aiohttp helper, not this `utils` package).
- Per CLAUDE.md the `tests/` directory currently contains only `__init__.py`
  fixtures; the audited `utils` module has zero direct test coverage.

## TODOs / FIXMEs / Stubs

A case-insensitive grep for `TODO|FIXME|XXX|HACK|stub` across `utils/` returned
no matches. The package has no explicit deferred-work markers.

Implicit stub: `utils/__init__.py` is a 0-byte file — not strictly a TODO, but
worth flagging if the project expects re-exports here.

## Known Issues Observed

1. **`setup_logging` is not idempotent.** Each call unconditionally appends a
   new `StreamHandler` (and optionally a `TimedRotatingFileHandler`) to the
   root logger. If anything ever re-invokes it (e.g. a test fixture, a reload
   path, the web UI restart), every log line will be duplicated per call. A
   simple `if root.handlers: return` guard or removal-before-add would fix it.

2. **Misleading inline comment in `logger.py`.** Line 24 says
   `# Console handler (clean, INFO+ only)` but the handler is set to the
   parameterised `level`, so passing `debug=True` sends DEBUG to the console
   despite the comment.

3. **Unused import in `logger.py`.** `from pathlib import Path` is imported on
   line 8 but never referenced — `LOGS_DIR` is already a `Path` provided by
   `config.settings`.

4. **`LOG_LEVEL` is `getattr`'d without validation.** A typo like
   `LOG_LEVEL = "INF0"` in settings raises `AttributeError` at startup instead
   of falling back to a sane default.

5. **`hurst_rs` swallows all exceptions to DEBUG.** The broad
   `except Exception as e` (line 69) hides genuine bugs (e.g. negative prices
   feeding `np.log`) behind a DEBUG-only message — only the `1e-12` epsilon
   shields against zero/negative input. Callers see `None` and continue
   silently.

6. **Hurst regime thresholds are split between docstring and settings.** The
   docstring quotes `0.55 / 0.48` but the live thresholds live in
   `config.settings.HURST_TRENDING_MIN` / `HURST_REVERTING_MAX`. If those
   settings drift, the docstring becomes lies.

7. **`RollingHurst._prices` uses `list.pop(0)`** (line 110) which is O(n) per
   call. With the default `lookback=200` this is irrelevant, but a
   `collections.deque(maxlen=lookback)` would be both simpler and O(1).

8. **`hurst_rs` recomputes from scratch on every `update()`** at every call
   once the buffer is full — there is no down-sampling, so for hot loops this
   is the dominant cost. Acceptable per-candle but not per-tick.

9. **No test coverage** at all for either module; Hurst math in particular is
   the kind of code that benefits from a synthetic-series sanity test
   (Brownian → ~0.5, persistent → > 0.5).
