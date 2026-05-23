"""
tests/test_mexc_key_router.py — MEXC per-pair key routing.

MEXC supports per-key pair allowlists, so one MEXC account holds N API
keys each restricted to a subset of pairs. The router maps symbol →
ccxt.mexc client built with the right credentials. Tests pin:

  - missing-from-map and missing-from-env both return None
  - one client cached per key index (no re-construction)
  - any_client picks the lowest available key for FeeManager pre-warm
  - has_route_for is pure-sync (no client construction)
  - close_all empties the pool and is idempotent
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from config import settings
from execution.mexc_key_router import MexcKeyRouter


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_ccxt(monkeypatch):
    """Replace the module-level ccxt with a stub that records mexc() construction
    and returns a uniquely-tagged MagicMock per call so tests can assert
    which key index built which client."""
    constructed = []

    def _mexc_factory(cfg):
        m = MagicMock(name=f"mexc_client_{len(constructed)}")
        m._cfg = dict(cfg)         # snapshot for assertions
        m.close = MagicMock(return_value=None)
        constructed.append(m)
        return m

    stub = MagicMock()
    stub.mexc.side_effect = _mexc_factory
    monkeypatch.setattr("execution.mexc_key_router.ccxt", stub)
    return stub, constructed


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every MEXC_KEY_*_API_KEY/SECRET from the env so each test
    declares exactly which keys it considers provisioned."""
    import os
    for k in list(os.environ.keys()):
        if k.startswith("MEXC_KEY_"):
            monkeypatch.delenv(k, raising=False)
    return monkeypatch


# ─────────────────────────────────────────────────────────────────────────
# get_client_for — symbol → client routing
# ─────────────────────────────────────────────────────────────────────────

def test_returns_none_when_symbol_not_in_map(monkeypatch, fake_ccxt, clean_env):
    monkeypatch.setattr(settings, "MEXC_PAIR_KEY_MAP", {"BTC/USDT": 1})
    clean_env.setenv("MEXC_KEY_1_API_KEY", "k1")
    clean_env.setenv("MEXC_KEY_1_SECRET", "s1")
    r = MexcKeyRouter()
    assert r.get_client_for("DOGE/USDT") is None


def test_returns_none_when_env_var_missing(monkeypatch, fake_ccxt, clean_env):
    """Symbol is mapped to key index 2, but MEXC_KEY_2_* env vars are
    absent — the router must report None, not raise."""
    monkeypatch.setattr(settings, "MEXC_PAIR_KEY_MAP", {"BTC/USDT": 2})
    # No env vars set
    r = MexcKeyRouter()
    assert r.get_client_for("BTC/USDT") is None


def test_returns_client_when_key_provisioned(monkeypatch, fake_ccxt, clean_env):
    stub_ccxt, constructed = fake_ccxt
    monkeypatch.setattr(settings, "MEXC_PAIR_KEY_MAP", {"BTC/USDT": 1})
    clean_env.setenv("MEXC_KEY_1_API_KEY", "my-key")
    clean_env.setenv("MEXC_KEY_1_SECRET", "my-secret")

    r = MexcKeyRouter()
    client = r.get_client_for("BTC/USDT")
    assert client is not None
    # Constructed with the right credentials
    assert client._cfg["apiKey"] == "my-key"
    assert client._cfg["secret"] == "my-secret"
    assert client._cfg["enableRateLimit"] is True
    assert client._cfg["options"]["defaultType"] == "spot"


def test_one_client_cached_per_index(monkeypatch, fake_ccxt, clean_env):
    """Two symbols mapped to the same key index must share one client —
    the router never constructs the same key twice."""
    stub_ccxt, constructed = fake_ccxt
    monkeypatch.setattr(
        settings, "MEXC_PAIR_KEY_MAP",
        {"BTC/USDT": 1, "ETH/USDT": 1, "SOL/USDT": 2},
    )
    clean_env.setenv("MEXC_KEY_1_API_KEY", "k1"); clean_env.setenv("MEXC_KEY_1_SECRET", "s1")
    clean_env.setenv("MEXC_KEY_2_API_KEY", "k2"); clean_env.setenv("MEXC_KEY_2_SECRET", "s2")

    r = MexcKeyRouter()
    c1 = r.get_client_for("BTC/USDT")
    c1_again = r.get_client_for("ETH/USDT")
    c2 = r.get_client_for("SOL/USDT")

    assert c1 is c1_again                # same key index → same client
    assert c1 is not c2                  # different key index → different client
    assert len(constructed) == 2         # only two constructions across three lookups


def test_missing_env_caches_negative_lookup(monkeypatch, fake_ccxt, clean_env):
    """Once we've established a key index has no env credentials, future
    lookups must NOT re-read os.environ for it — cheap fast-path on
    every scan tick."""
    monkeypatch.setattr(settings, "MEXC_PAIR_KEY_MAP", {"BTC/USDT": 3})
    # No MEXC_KEY_3_* set
    r = MexcKeyRouter()
    assert r.get_client_for("BTC/USDT") is None
    assert 3 in r._missing_indices


# ─────────────────────────────────────────────────────────────────────────
# any_client — for FeeManager pre-warm
# ─────────────────────────────────────────────────────────────────────────

def test_any_client_picks_lowest_configured_index(monkeypatch, fake_ccxt, clean_env):
    """any_client iterates the map's key indices in ascending order and
    returns the first one with env credentials. With KEY_1 missing and
    KEY_2 present, it should return the KEY_2 client."""
    monkeypatch.setattr(
        settings, "MEXC_PAIR_KEY_MAP",
        {"BTC/USDT": 1, "ETH/USDT": 2},
    )
    clean_env.setenv("MEXC_KEY_2_API_KEY", "k2"); clean_env.setenv("MEXC_KEY_2_SECRET", "s2")
    r = MexcKeyRouter()
    client = r.any_client()
    assert client is not None
    assert client._cfg["apiKey"] == "k2"


def test_any_client_falls_back_to_unmapped_key_scan(monkeypatch, fake_ccxt, clean_env):
    """Even with an empty MEXC_PAIR_KEY_MAP, any_client must surface a
    client when env credentials exist — fresh installs boot before the
    operator has populated the pair map."""
    monkeypatch.setattr(settings, "MEXC_PAIR_KEY_MAP", {})
    clean_env.setenv("MEXC_KEY_5_API_KEY", "k5"); clean_env.setenv("MEXC_KEY_5_SECRET", "s5")
    r = MexcKeyRouter()
    client = r.any_client()
    assert client is not None
    assert client._cfg["apiKey"] == "k5"


def test_any_client_returns_none_with_no_keys(monkeypatch, fake_ccxt, clean_env):
    monkeypatch.setattr(settings, "MEXC_PAIR_KEY_MAP", {})
    r = MexcKeyRouter()
    assert r.any_client() is None


# ─────────────────────────────────────────────────────────────────────────
# has_route_for — sync filter without client construction
# ─────────────────────────────────────────────────────────────────────────

def test_has_route_for_does_not_construct_client(monkeypatch, fake_ccxt, clean_env):
    stub_ccxt, constructed = fake_ccxt
    monkeypatch.setattr(settings, "MEXC_PAIR_KEY_MAP", {"BTC/USDT": 1})
    clean_env.setenv("MEXC_KEY_1_API_KEY", "k1"); clean_env.setenv("MEXC_KEY_1_SECRET", "s1")
    r = MexcKeyRouter()
    assert r.has_route_for("BTC/USDT") is True
    assert constructed == []  # purely a credential probe — no ccxt.mexc() call


def test_has_route_for_missing_symbol(monkeypatch, fake_ccxt, clean_env):
    monkeypatch.setattr(settings, "MEXC_PAIR_KEY_MAP", {})
    r = MexcKeyRouter()
    assert r.has_route_for("BTC/USDT") is False


# ─────────────────────────────────────────────────────────────────────────
# close_all
# ─────────────────────────────────────────────────────────────────────────

def test_close_all_clears_pool(monkeypatch, fake_ccxt, clean_env):
    monkeypatch.setattr(settings, "MEXC_PAIR_KEY_MAP", {"BTC/USDT": 1})
    clean_env.setenv("MEXC_KEY_1_API_KEY", "k1"); clean_env.setenv("MEXC_KEY_1_SECRET", "s1")
    r = MexcKeyRouter()
    client = r.get_client_for("BTC/USDT")
    assert client is not None
    asyncio.run(r.close_all())
    assert r._clients == {}
    # Calling again is idempotent — must not raise
    asyncio.run(r.close_all())
