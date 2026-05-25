"""tests/test_funds.py — ring-fenced fund architecture invariants.

Three independent funds (signal / arb / mexc-scalp), each a capital pool
mapped 1:1 to an agent. Exchanges may be shared (MEXC backs the scalp fund
and is also an arb venue); capital never is. These guard the wiring so the
funds can't silently drift.
"""
from config import settings


def test_fund_constants_present_and_positive():
    for name in ("FUND_SIGNAL_CAPITAL", "FUND_ARB_CAPITAL",
                 "FUND_MEXC_SCALP_CAPITAL", "FUND_DAILY_LOSS_HALT_PCT"):
        assert hasattr(settings, name), f"missing {name}"
        assert getattr(settings, name) > 0


def test_mexc_arb_fund_constant_removed():
    """MEXC-ARB was folded into the main ARB fund — its constant is gone."""
    assert not hasattr(settings, "FUND_MEXC_ARB_CAPITAL")


def test_starting_capital_is_sum_of_funds():
    total = (settings.FUND_SIGNAL_CAPITAL + settings.FUND_ARB_CAPITAL
             + settings.FUND_MEXC_SCALP_CAPITAL)
    assert total == 1000.0
    assert settings.STARTING_CAPITAL == total


def test_legacy_aliases_track_fund_constants():
    assert settings.SIGNAL_AGENT_CAPITAL == settings.FUND_SIGNAL_CAPITAL
    assert settings.ARB_AGENT_CAPITAL == settings.FUND_ARB_CAPITAL


def test_mexc_folded_into_arb_routing_and_fee_map():
    assert settings.STRATEGY_EXCHANGE_MAP["scalp"] == ["mexc"]
    assert "mexc" in settings.STRATEGY_EXCHANGE_MAP["arb"]   # MEXC is an arb venue
    assert "mexc" in settings.ARB_FEE_MAP


def test_registered_funds_carry_their_allocation():
    from agents import REGISTERED_AGENTS
    by_id = {a.agent_id: a for a in REGISTERED_AGENTS}
    assert by_id["signal"].capital_allocation == settings.FUND_SIGNAL_CAPITAL
    assert by_id["arb"].capital_allocation == settings.FUND_ARB_CAPITAL
    assert by_id["scalp"].capital_allocation == settings.FUND_MEXC_SCALP_CAPITAL
    # MEXC-ARB agent removed entirely.
    assert "mexc-arb" not in by_id


def test_mexc_arb_wrapper_removed():
    import agents
    assert not hasattr(agents, "MexcArbAgentWrapper")


def test_scalp_fund_size_decoupled_from_trading_budget():
    """Scalp shows the $100 MEXC-scalp fund as its allocation while its
    trading budget stays SCALP_CAPITAL (observation when 0)."""
    from agents import REGISTERED_AGENTS
    scalp = next(a for a in REGISTERED_AGENTS if a.agent_id == "scalp")
    assert scalp.capital_allocation == settings.FUND_MEXC_SCALP_CAPITAL
    assert scalp._capital == settings.SCALP_CAPITAL


def test_sim_balance_ledger_sums_to_starting_capital_and_includes_mexc():
    """In sim, the per-venue ledger fully backs the funds: it spans every
    venue the funds touch and sums to STARTING_CAPITAL ($1,000)."""
    assert sum(settings.EXCHANGE_BALANCES.values()) == settings.STARTING_CAPITAL
    assert settings.EXCHANGE_BALANCES["mexc"] == settings.FUND_MEXC_SCALP_CAPITAL


def test_order_router_sizes_off_signal_fund_not_total():
    """Ring-fence: the signal agent's OrderRouter sizes off its own fund,
    not the whole-portfolio sim ledger."""
    from execution.router import OrderRouter
    r = OrderRouter()
    assert r._portfolio_value == settings.FUND_SIGNAL_CAPITAL
    assert r._portfolio_value != sum(settings.EXCHANGE_BALANCES.values())
