"""Read-only probe: discover each MEXC key's API-tradeable symbol allowlist.

Hits MEXC with the 4 keys in config/keys.env, calls the per-key
selfSymbols endpoint, and cross-references the candidate pair list.
Prints ONLY symbol data + counts — never the API secrets.
"""
import os
import json
import ccxt
from dotenv import load_dotenv

load_dotenv("config/keys.env")

# Candidate list supplied by the user (102 entries; NEIROCTOUSDT normalised).
CANDIDATES_RAW = """
BTC/USDT ETH/USDT SOL/USDT XRP/USDT BNB/USDT DOGE/USDT ADA/USDT TON/USDT
AVAX/USDT LINK/USDT DOT/USDT POL/USDT LTC/USDT SHIB/USDT TRX/USDT NEAR/USDT
UNI/USDT APT/USDT SUI/USDT ARB/USDT OP/USDT INJ/USDT ATOM/USDT PEPE/USDT
WIF/USDT BONK/USDT FTM/USDT TAO/USDT RENDER/USDT FET/USDT JUP/USDT TIA/USDT
SEI/USDT PYTH/USDT ONDO/USDT WLD/USDT FLOKI/USDT BOME/USDT BRETT/USDT
TURBO/USDT GALA/USDT SAND/USDT MANA/USDT AXS/USDT ICP/USDT FIL/USDT VET/USDT
HBAR/USDT ALGO/USDT XLM/USDT EOS/USDT AAVE/USDT MKR/USDT CAKE/USDT RUNE/USDT
GRT/USDT LDO/USDT SNX/USDT CRV/USDT DYDX/USDT NOT/USDT DOGS/USDT HMSTR/USDT
CATI/USDT MAJOR/USDT ORDI/USDT SATS/USDT ENA/USDT ETHFI/USDT EIGEN/USDT
IO/USDT ZRO/USDT STRK/USDT MANTA/USDT REZ/USDT BLAST/USDT PNUT/USDT ACT/USDT
GOAT/USDT MOODENG/USDT NEIROCTO/USDT POPCAT/USDT MOG/USDT LUNC/USDT CFX/USDT
ROSE/USDT JASMY/USDT HOT/USDT AR/USDT CHZ/USDT ENJ/USDT MAGIC/USDT RON/USDT
BEAM/USDT PORTAL/USDT HNT/USDT KAVA/USDT EGLD/USDT FLOW/USDT ONE/USDT
ZIL/USDT KSM/USDT
"""
CANDIDATES = CANDIDATES_RAW.split()


def get_self_symbols(client):
    """Return the raw selfSymbols payload, or a diagnostic dict."""
    for name in ("spotPrivateGetSelfSymbols", "spot_private_get_self_symbols"):
        fn = getattr(client, name, None)
        if fn:
            try:
                return fn()
            except Exception as e:
                return {"_error": f"{name} raised: {e!r}"}
    matches = [m for m in dir(client) if "elf" in m.lower()]
    return {"_error": "no selfSymbols method found", "_dir_matches": matches}


def normalize_ids(payload):
    """selfSymbols returns {'code':0,'data':[...]} or a bare list of ids."""
    if isinstance(payload, dict):
        if "_error" in payload:
            return None, payload
        data = payload.get("data", payload)
    else:
        data = payload
    ids = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict):
                ids.append(item.get("symbol") or item.get("id") or "")
    return [i for i in ids if i], None


def main():
    out = {"candidates_total": len(CANDIDATES), "keys": {}}
    markets = None
    markets_by_id = None
    usdt_symbols = set()

    for idx in (1, 2, 3, 4):
        api = os.getenv(f"MEXC_KEY_{idx}_API_KEY")
        sec = os.getenv(f"MEXC_KEY_{idx}_SECRET")
        if not api or not sec:
            out["keys"][idx] = {"status": "no_env"}
            continue
        client = ccxt.mexc({
            "apiKey": api, "secret": sec,
            "enableRateLimit": True, "options": {"defaultType": "spot"},
        })
        try:
            m = client.load_markets()
            if markets is None:
                markets = m
                markets_by_id = client.markets_by_id
                usdt_symbols = {
                    s for s, mk in m.items()
                    if mk.get("spot") and s.endswith("/USDT") and mk.get("active", True)
                }
        except Exception as e:
            out["keys"][idx] = {"status": f"load_markets_failed: {e!r}"}
            continue

        raw = get_self_symbols(client)
        ids, err = normalize_ids(raw)
        if err is not None:
            out["keys"][idx] = {"status": "selfsymbols_error", "detail": err}
            continue

        # Map MEXC ids -> ccxt unified symbols (USDT spot only).
        syms = []
        for sid in ids:
            mk_list = (markets_by_id or {}).get(sid)
            if not mk_list:
                continue
            mk_list = mk_list if isinstance(mk_list, list) else [mk_list]
            for mk in mk_list:
                sym = mk.get("symbol")
                if sym and sym.endswith("/USDT") and mk.get("spot"):
                    syms.append(sym)
        out["keys"][idx] = {
            "status": "ok",
            "selfsymbols_total": len(ids),
            "usdt_tradeable": len(set(syms)),
            "symbols": sorted(set(syms)),
        }

    # Build coverage: symbol -> first key index that can trade it.
    coverage = {}
    for idx in (1, 2, 3, 4):
        info = out["keys"].get(idx, {})
        for s in info.get("symbols", []):
            coverage.setdefault(s, idx)

    out["mexc_usdt_spot_total"] = len(usdt_symbols)
    out["candidates_covered"] = sorted([c for c in CANDIDATES if c in coverage])
    out["candidates_not_tradeable"] = sorted([c for c in CANDIDATES if c not in coverage])
    out["candidates_not_listed"] = sorted([c for c in CANDIDATES if c not in usdt_symbols])
    out["proposed_map"] = {c: coverage[c] for c in CANDIDATES if c in coverage}
    out["covered_count"] = len(out["candidates_covered"])

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
