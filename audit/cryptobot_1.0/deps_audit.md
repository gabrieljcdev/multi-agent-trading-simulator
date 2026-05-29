# Dependency audit — CryptoBot 1.0

## Environment

- **Python:** 3.11.15
- **venv path:** `~/cryptobot/venv` (Linux/WSL)
- **requirements.txt direct deps:** 33
- **pip freeze installed packages:** 99
- **Drifted versions (req vs installed):** 22
- **Missing from venv (req entry, no install):** 6
- **Incidental in venv (installed, not in req):** 72 (transitive + a few first-class additions)

## Direct dependencies — version comparison

| Package | requirements.txt | pip freeze | Drift |
|---------|------------------|------------|-------|
| `ccxt` | 4.5.54 | 4.5.54 | — |
| `protobuf` | 5.29.5 | 5.29.5 | — |
| `pandas-ta` | 0.3.14b | **MISSING** | — |
| `pandas` | 2.2.0 | 3.0.3 | major↑ |
| `numpy` | 1.26.4 | 2.4.6 | major↑ |
| `sqlalchemy` | 2.0.28 | 2.0.49 | minor |
| `alembic` | 1.13.1 | 1.18.4 | minor |
| `praw` | 7.7.1 | 7.8.1 | patch |
| `telethon` | 1.34.0 | 1.43.2 | minor |
| `feedparser` | 6.0.11 | 6.0.12 | patch |
| `pytrends` | 4.9.2 | 4.9.2 | — |
| `vaderSentiment` | 3.3.2 | 3.3.2 | — |
| `transformers` | 4.38.2 | **MISSING** | — |
| `torch` | 2.2.1 | **MISSING** | — |
| `anthropic` | 0.21.3 | 0.103.0 | **major↑ (≈80 minor releases)** |
| `web3` | 7.16.0 | 7.16.0 | — |
| `rich` | 13.7.1 | 15.0.0 | major↑ |
| `scikit-learn` | 1.4.1 | 1.8.0 | minor↑ |
| `xgboost` | 2.0.3 | 3.2.0 | major↑ |
| `joblib` | 1.3.3 | 1.5.3 | minor↑ |
| `python-dotenv` | 1.0.1 | 1.2.2 | minor↑ |
| `aiohttp` | 3.9.3 | 3.13.5 | minor↑ |
| `asyncio-throttle` | 1.0.2 | **MISSING** | — |
| `httpx` | 0.27.0 | 0.28.1 | minor↑ |
| `tenacity` | 8.2.3 | 9.1.4 | major↑ |
| `python-dateutil` | 2.9.0 | 2.9.0.post0 | post-release |
| `pytz` | 2024.1 | **MISSING** | — |
| `colorama` | 0.4.6 | **MISSING** | — |
| `click` | 8.1.7 | 8.4.0 | minor↑ |
| `pydantic` | 2.6.3 | 2.13.4 | minor↑ |
| `pytest` | 8.1.0 | 9.0.3 | major↑ |
| `pytest-asyncio` | 0.23.5 | 1.3.0 | major↑ |
| `pytest-mock` | 3.12.0 | 3.15.1 | minor↑ |

22 of 33 direct deps are drifted; 11 are at the version declared (or close enough — protobuf, ccxt, pytrends, vaderSentiment, web3 match exactly).

## Missing from venv but listed in requirements.txt — usage check

For each "missing" dep, grep the source for any import. If the dep is unused, the requirements.txt entry is stale.

| Dep | Imported anywhere in source? | Verdict |
|-----|------------------------------|---------|
| `pandas-ta` | **No** (`grep pandas_ta` returns 0 hits) | Stale requirements.txt entry. Actual TA library in use is `ta==0.11.0` (incidental install), imported in `core/market_data.py`. |
| `transformers` | **No** (`grep transformers/FinBERT` returns 0 hits) | Stale — FinBERT path was planned (see comment in requirements.txt: *"FinBERT (optional, heavier)"*) but never wired into `sentiment/`. |
| `torch` | **No** (`grep "^import torch\|^from torch"` returns 0 hits) | Stale — only needed by `transformers`, which isn't wired either. |
| `asyncio-throttle` | **No** (`grep asyncio_throttle` returns 0 hits) | Stale — `tenacity` is the retry/backoff lib actually used. |
| `pytz` | **No** (`grep "^import pytz\|^from pytz"` returns 0 hits) | Stale — code uses `datetime.timezone.utc` / `python-dateutil` (incidentally installed as `python-dateutil==2.9.0.post0`). |
| `colorama` | **No** (`grep colorama` returns 0 hits) | Stale — `rich` provides colour on every platform. |

**All 6 "missing" deps are unused in source.** They are vestigial entries in `requirements.txt` that should be pruned. The bot does not actually depend on them.

## Incidental installs (in venv but not in requirements.txt)

72 packages. The majority are transitive — installed automatically by `web3`, `anthropic`, `pandas`, `xgboost`, `praw`, `telethon`, etc. A handful are notable:

| Package | Likely reason |
|---------|---------------|
| `ta==0.11.0` | **First-class import** — `core/market_data.py` uses `from ta...`. Should be **added** to `requirements.txt`. |
| `websockets==15.0.1` | Transitive via `ccxt.pro` (ccxt 4.x bundles `ccxt.pro` which uses `websockets`). |
| `websocket-client==1.9.0` | Transitive via `praw` / Reddit auth flow. |
| `requests==2.34.2` | Transitive — used by `praw`, `pytrends`, `cryptocompare` and others. |
| `cryptography==48.0.0`, `eth-*`, `coincurve`, `ckzg`, `pycryptodome`, `bitarray`, `parsimonious`, `rlp`, `pyunormalize` | Transitive via `web3==7.16.0`. |
| `nvidia-nccl-cu12==2.30.4` | Unexpected — pulled by some `xgboost` or `scipy` variant. Worth confirming and potentially excluding via `--no-deps` if the box has no GPU. |
| `py-spy==0.4.2` | Developer profiling tool — not part of the bot. Manual install. |
| `types-requests==2.33.0.20260518` | Type stubs — developer-time, harmless. |
| `scipy==1.17.1` | Transitive via `scikit-learn` and possibly `numpy`-aware code. |
| `lxml==6.1.1` | Transitive via `feedparser` HTML parsing. |
| `Telethon==1.43.2` | This IS in `requirements.txt` (`telethon==1.34.0`); case difference (`telethon` vs `Telethon`) made it appear absent in the simple diff. Treat as drift, not incidental. |

## Notable risks

1. **`anthropic` drifted from 0.21.3 → 0.103.0 (~80 minor releases).** Major breaking changes likely (tool use, streaming, message format, model IDs). `core/agent.py` is written against the older SDK — needs review for compatibility. Tests passing (552 / 552) suggest the surface in use is small enough to survive the drift, but message construction at `core/agent.py:21-46` should be checked against current SDK.
2. **`numpy 1.x → 2.x` and `pandas 2.x → 3.x` are major-major upgrades.** Both have broken APIs (numpy 2 dropped `np.bool` aliases, pandas 3 dropped some implicit type coercions). Tests pass, so the surface is small, but recreating the venv from `requirements.txt` would land on numpy 1.x / pandas 2.x — a different runtime than what's been tested.
3. **`pytest 8.1.0 → 9.0.3` and `pytest-asyncio 0.23.5 → 1.3.0` are major upgrades.** The whole test suite was written and is being run against pytest 9. Re-installing per `requirements.txt` would downgrade and may break async-fixture wiring.
4. **`xgboost 2.0.3 → 3.2.0` and `scikit-learn 1.4.1 → 1.8.0` matter for the predictive engine** — except `predictive/` is empty, so the drift is currently moot. Worth realigning when that package gets built.
5. **`ta==0.11.0` is in use but undeclared.** A `pip install -r requirements.txt` on a clean machine that doesn't auto-pull `ta` as a transitive will fail at import time in `core/market_data.py`.
6. **Six unused deps inflate the surface.** Removing them speeds installs and avoids security-scanner noise.

## Recommendation (not actioned — audit is read-only)

The current `requirements.txt` does **not** reproduce the running venv. To match reality, the file would need to:

- Bump every drifted version to the installed one.
- Remove `pandas-ta`, `transformers`, `torch`, `asyncio-throttle`, `pytz`, `colorama`.
- Add `ta==0.11.0`.

But "match reality" and "what we want to ship" are different goals — the audit just records the gap. The decision belongs to the operator.
