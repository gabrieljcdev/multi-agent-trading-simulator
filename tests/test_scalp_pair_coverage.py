"""SCALP_PAIRS ↔ MEXC_PAIR_KEY_MAP coverage invariants.

The map is data-derived (each MEXC key's selfSymbols allowlist, via
scripts/mexc_probe.py). These checks make sure the two lists can't drift
apart silently — every scalp pair must route to exactly one key, and the
map must not carry pairs we don't actually scan.
"""
from config import settings


def test_scalp_pairs_have_no_duplicates():
    pairs = settings.SCALP_PAIRS
    assert len(pairs) == len(set(pairs)), "duplicate entries in SCALP_PAIRS"


def test_every_scalp_pair_has_a_key_route():
    missing = [p for p in settings.SCALP_PAIRS if p not in settings.MEXC_PAIR_KEY_MAP]
    assert not missing, f"SCALP_PAIRS not routed by MEXC_PAIR_KEY_MAP: {missing}"


def test_map_has_no_pairs_outside_scalp_pairs():
    scalp = set(settings.SCALP_PAIRS)
    orphans = [p for p in settings.MEXC_PAIR_KEY_MAP if p not in scalp]
    assert not orphans, f"MEXC_PAIR_KEY_MAP routes pairs absent from SCALP_PAIRS: {orphans}"


def test_pairs_and_map_are_a_bijection():
    assert set(settings.SCALP_PAIRS) == set(settings.MEXC_PAIR_KEY_MAP)


def test_key_indices_are_one_based_and_in_range():
    # 1-based, matching MEXC_KEY_{N}_API_KEY / _SECRET env var naming.
    bad = {p: i for p, i in settings.MEXC_PAIR_KEY_MAP.items() if not (isinstance(i, int) and i >= 1)}
    assert not bad, f"key indices must be 1-based ints: {bad}"
