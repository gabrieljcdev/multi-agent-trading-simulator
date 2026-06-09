"""
tests/test_meme_buyer_derivation.py — live early-buyer derivation (v1).

MemeScorer._derive_buyer_funders was a stub returning {} (the documented v1
gap). It now crawls the rate-limited public RPC: the mint's newest signatures ->
sol_parse.early_buyer (the fee payer of a tx that moves the token, excluding the
create) -> cluster_funder (first funder, stop-listed). These cover the parse
helper and the two-hop derivation with a fake RPC (no network).
"""

from __future__ import annotations

import pytest

from follow import meme_scorer, sol_parse
from follow.meme_scorer import MemeScorer, Launch


def _buy_tx(buyer: str, mint: str, block_time: int) -> dict:
    """A jsonParsed buy: `buyer` signs/pays, an SPL transfer moves `mint`."""
    return {
        "blockTime": block_time,
        "transaction": {"message": {
            "accountKeys": [
                {"pubkey": buyer, "signer": True},
                {"pubkey": "PoolAcct", "signer": False},
            ],
            "instructions": [{
                "program": "spl-token",
                "parsed": {"type": "transfer", "info": {
                    "authority": "PoolAuthority", "source": "src",
                    "destination": "dst", "mint": mint, "amount": "1000000"}},
            }],
        }},
    }


def _pump_buy_tx(buyer: str, mint: str, block_time: int,
                 curve: str = "BONDING_CURVE", gain: int = 99) -> dict:
    """A pump.fun-style buy: the token moves via CPI (no top-level spl-token
    transfer), so the only honest signal is the pre/post token-balance delta —
    the buyer's mint balance goes up, the bonding curve's goes down."""
    return {
        "blockTime": block_time,
        "transaction": {"message": {
            "accountKeys": [{"pubkey": buyer, "signer": True}],
            "instructions": [
                {"program": "spl-associated-token-account",
                 "parsed": {"type": "createIdempotent"}},
                {"programId": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"},  # pump.fun, unparsed
            ],
        }},
        "meta": {
            "preTokenBalances": [
                {"owner": curve, "mint": mint, "uiTokenAmount": {"amount": str(1000 + gain)}},
                {"owner": buyer, "mint": mint, "uiTokenAmount": {"amount": "0"}},
            ],
            "postTokenBalances": [
                {"owner": curve, "mint": mint, "uiTokenAmount": {"amount": "1000"}},
                {"owner": buyer, "mint": mint, "uiTokenAmount": {"amount": str(gain)}},
            ],
        },
    }


def _create_tx(creator: str, mint: str) -> dict:
    """The launchpad create — initializeMint by the creator (NOT a buy)."""
    return {
        "blockTime": 1000,
        "transaction": {"message": {
            "accountKeys": [{"pubkey": creator, "signer": True}],
            "instructions": [{
                "program": "spl-token",
                "parsed": {"type": "initializeMint", "info": {"mint": mint}}}],
        }},
    }


# ── sol_parse.early_buyer ─────────────────────────────────────────────────

def test_early_buyer_returns_fee_payer_for_a_buy():
    assert sol_parse.early_buyer(_buy_tx("BUYER1", "MINT", 1001), "MINT") == "BUYER1"


def test_early_buyer_from_balance_delta_pumpfun_cpi():
    # token moved via CPI (no top-level transfer) — buyer identified by the
    # net token-balance gain; the bonding curve loses tokens, so it's not picked
    assert sol_parse.early_buyer(_pump_buy_tx("BUYER1", "MINT", 1001), "MINT") == "BUYER1"


def test_early_buyer_excludes_the_create_tx():
    # the mint-initialization is the creator, never a buyer
    assert sol_parse.early_buyer(_create_tx("CREATOR", "MINT"), "MINT") is None


def test_early_buyer_none_when_tx_does_not_touch_mint():
    assert sol_parse.early_buyer(_buy_tx("BUYER1", "OTHER_MINT", 1001), "MINT") is None


def test_early_buyer_tolerant_of_garbage():
    assert sol_parse.early_buyer(None, "MINT") is None
    assert sol_parse.early_buyer({}, "MINT") is None
    assert sol_parse.early_buyer(_buy_tx("B", "MINT", 1), "") is None


# ── MemeScorer._derive_buyer_funders (two-hop crawl, fake RPC) ────────────

class _FakeRpc:
    def __init__(self, sigs, txs):
        self._sigs, self._txs = sigs, txs

    async def get_signatures_for_address(self, address, *, before=None, limit=1000):
        return self._sigs

    async def get_transaction(self, signature):
        return self._txs.get(signature)


@pytest.mark.asyncio
async def test_derive_buyer_funders_maps_buyers_to_funders(monkeypatch):
    mint, detected = "MINT", 1000.0
    sigs = [
        {"signature": "sc", "blockTime": 1000},          # the create
        {"signature": "s1", "blockTime": 1001},          # buyer 1
        {"signature": "s2", "blockTime": 1002},          # buyer 2
        {"signature": "sc2", "blockTime": 1003},         # creator self-buy (excluded)
    ]
    txs = {
        "sc":  _create_tx("CREATOR", mint),
        "s1":  _buy_tx("BUYER1", mint, 1001),
        "s2":  _buy_tx("BUYER2", mint, 1002),
        "sc2": _buy_tx("CREATOR", mint, 1003),
    }

    async def _fake_cluster_funder(wallet, rpc, **kw):
        return {"BUYER1": "FUNDER_A", "BUYER2": "FUNDER_A"}.get(wallet)

    monkeypatch.setattr(meme_scorer, "cluster_funder", _fake_cluster_funder)
    sc = MemeScorer(rpc=_FakeRpc(sigs, txs))
    out = await sc._derive_buyer_funders(
        Launch(mint=mint, detected_at=detected, creator="CREATOR"))

    # create + creator self-buy excluded; both buyers share FUNDER_A (a cluster)
    assert out == {"BUYER1": "FUNDER_A", "BUYER2": "FUNDER_A"}


@pytest.mark.asyncio
async def test_derive_buyer_funders_drops_buys_past_the_early_window(monkeypatch):
    from config import settings
    mint, detected = "MINT", 1000.0
    window = float(settings.MEME_EARLY_BUYER_WINDOW_S)
    sigs = [
        {"signature": "s1", "blockTime": int(detected + 10)},          # inside
        {"signature": "s2", "blockTime": int(detected + window + 50)},  # too late
    ]
    txs = {"s1": _buy_tx("BUYER1", mint, 1010),
           "s2": _buy_tx("LATE", mint, 9999)}
    monkeypatch.setattr(meme_scorer, "cluster_funder",
                        lambda w, r, **k: _aw("FUNDER_A" if w == "BUYER1" else None))
    sc = MemeScorer(rpc=_FakeRpc(sigs, txs))
    out = await sc._derive_buyer_funders(Launch(mint=mint, detected_at=detected))
    assert "BUYER1" in out and "LATE" not in out


@pytest.mark.asyncio
async def test_derive_buyer_funders_empty_without_rpc():
    sc = MemeScorer(rpc=None)
    out = await sc._derive_buyer_funders(Launch(mint="MINT", detected_at=1.0))
    assert out == {}


async def _aw(v):
    return v
