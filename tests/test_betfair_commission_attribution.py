from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from autosport.betfair_commission_attribution import (
    BetAttributedNet,
    BetCommissionAllocation,
    BetGrossAmount,
    BetfairCommissionAttributionError,
    CommissionAllocationPolicyRef,
    MarketCommissionAmount,
    MarketCommissionAttribution,
)


A = "a" * 64
B = "b" * 64
C = "c" * 64


def _bet(
    bet_id: str,
    gross: str,
    *,
    venue: str = "betfair",
    account: str = "account-a",
    market: str = "1.234",
    currency: str = "EUR",
) -> BetGrossAmount:
    return BetGrossAmount(
        venue_id=venue,
        account_id=account,
        bet_id=bet_id,
        market_id=market,
        currency=currency,
        gross_profit=Decimal(gross),
        evidence_sha256=A if bet_id == "bet-1" else B,
    )


def _commission(
    value: str = "1.5",
    *,
    venue: str = "betfair",
    account: str = "account-a",
    market: str = "1.234",
    currency: str = "EUR",
) -> MarketCommissionAmount:
    return MarketCommissionAmount(
        venue_id=venue,
        account_id=account,
        market_id=market,
        currency=currency,
        commission_charge=Decimal(value),
        evidence_sha256=C,
    )


def _policy() -> CommissionAllocationPolicyRef:
    return CommissionAllocationPolicyRef(
        policy_id="explicit-market-commission-allocation",
        policy_version=1,
        policy_sha256=B,
    )


def test_market_net_is_available_without_fabricating_per_bet_net() -> None:
    value = MarketCommissionAttribution(
        gross_bets=(_bet("bet-1", "10"), _bet("bet-2", "-4")),
        market_commission=_commission("1.5"),
    )

    assert value.market_gross_profit == Decimal("6")
    assert value.market_net_profit == Decimal("4.5")
    assert value.grants_provider_origin_authority is False
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="requires explicit versioned commission allocation",
    ):
        value.per_bet_net()


def test_explicit_allocation_conserves_market_commission_and_is_derived() -> None:
    value = MarketCommissionAttribution(
        gross_bets=(_bet("bet-1", "10"), _bet("bet-2", "-4")),
        market_commission=_commission("1.5"),
        allocations=(
            BetCommissionAllocation("bet-1", Decimal("1.0")),
            BetCommissionAllocation("bet-2", Decimal("0.5")),
        ),
        allocation_policy=_policy(),
    )

    per_bet = value.per_bet_net()
    assert tuple(item.bet_id for item in per_bet) == ("bet-1", "bet-2")
    assert tuple(item.derived_net_profit for item in per_bet) == (
        Decimal("9"),
        Decimal("-4.5"),
    )
    assert all(item.provider_exact is False for item in per_bet)
    assert all(item.account_id == "account-a" for item in per_bet)
    assert all(item.venue_id == "betfair" for item in per_bet)
    for item in per_bet:
        item.assert_derived_from(value)
    assert (
        sum((item.derived_net_profit for item in per_bet), Decimal("0"))
        == value.market_net_profit
    )


def test_allocations_require_policy_identity() -> None:
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="require exact allocation policy identity",
    ):
        MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", "10"),),
            market_commission=_commission("1"),
            allocations=(BetCommissionAllocation("bet-1", Decimal("1")),),
        )


def test_policy_without_allocations_is_rejected() -> None:
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="requires explicit per-bet allocations",
    ):
        MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", "10"),),
            market_commission=_commission("1"),
            allocation_policy=_policy(),
        )


def test_allocation_must_cover_exact_bet_set() -> None:
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="cover exactly",
    ):
        MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", "10"), _bet("bet-2", "5")),
            market_commission=_commission("1"),
            allocations=(BetCommissionAllocation("bet-1", Decimal("1")),),
            allocation_policy=_policy(),
        )


def test_allocation_must_exactly_conserve_market_commission() -> None:
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="exactly conserve",
    ):
        MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", "10"), _bet("bet-2", "5")),
            market_commission=_commission("1"),
            allocations=(
                BetCommissionAllocation("bet-1", Decimal("0.7")),
                BetCommissionAllocation("bet-2", Decimal("0.2")),
            ),
            allocation_policy=_policy(),
        )


def test_duplicate_gross_bet_id_is_rejected() -> None:
    with pytest.raises(BetfairCommissionAttributionError, match="duplicate gross"):
        MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", "10"), _bet("bet-1", "5")),
            market_commission=_commission("1"),
        )


def test_duplicate_allocation_bet_id_is_rejected() -> None:
    with pytest.raises(BetfairCommissionAttributionError, match="duplicate allocated"):
        MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", "10"), _bet("bet-2", "5")),
            market_commission=_commission("1"),
            allocations=(
                BetCommissionAllocation("bet-1", Decimal("0.5")),
                BetCommissionAllocation("bet-1", Decimal("0.5")),
            ),
            allocation_policy=_policy(),
        )


@pytest.mark.parametrize(
    ("bet", "commission", "match"),
    [
        (_bet("bet-1", "1", venue="other"), _commission(), "venue_id differ"),
        (_bet("bet-1", "1", account="account-b"), _commission(), "account_id differ"),
        (_bet("bet-1", "1", market="other"), _commission(), "market_id differ"),
        (_bet("bet-1", "1", currency="GBP"), _commission(), "currency differ"),
    ],
)
def test_provider_account_market_and_currency_scope_must_match(
    bet, commission, match
) -> None:
    with pytest.raises(BetfairCommissionAttributionError, match=match):
        MarketCommissionAttribution(
            gross_bets=(bet,),
            market_commission=commission,
        )


def test_negative_commission_models_credit_or_reversal_without_new_policy() -> None:
    value = MarketCommissionAttribution(
        gross_bets=(_bet("bet-1", "6"),),
        market_commission=_commission("-1.25"),
    )
    assert value.market_net_profit == Decimal("7.25")


def test_attribution_identity_is_order_invariant() -> None:
    one = MarketCommissionAttribution(
        gross_bets=(_bet("bet-1", "10"), _bet("bet-2", "5")),
        market_commission=_commission("1"),
        allocations=(
            BetCommissionAllocation("bet-1", Decimal("0.6")),
            BetCommissionAllocation("bet-2", Decimal("0.4")),
        ),
        allocation_policy=_policy(),
    )
    two = MarketCommissionAttribution(
        gross_bets=tuple(reversed(one.gross_bets)),
        market_commission=one.market_commission,
        allocations=tuple(reversed(one.allocations)),
        allocation_policy=one.allocation_policy,
    )
    assert one.attribution_id == two.attribution_id



def test_market_economics_are_decimal_context_independent() -> None:
    gross_one = "123456789012345678901234567890.12345"
    gross_two = "0.87655"
    commission = "0.11111"
    with localcontext() as context:
        context.prec = 6
        low = MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", gross_one), _bet("bet-2", gross_two)),
            market_commission=_commission(commission),
        )
        low_gross = low.market_gross_profit
        low_net = low.market_net_profit
    with localcontext() as context:
        context.prec = 60
        high = MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", gross_one), _bet("bet-2", gross_two)),
            market_commission=_commission(commission),
        )
        high_gross = high.market_gross_profit
        high_net = high.market_net_profit
    assert low_gross == high_gross == Decimal(
        "123456789012345678901234567891"
    )
    assert low_net == high_net == Decimal(
        "123456789012345678901234567890.88889"
    )


def test_allocation_conservation_is_decimal_context_independent() -> None:
    allocations = (
        BetCommissionAllocation(
            "bet-1", Decimal("12345678901234567890.12345")
        ),
        BetCommissionAllocation("bet-2", Decimal("0.87655")),
    )
    commission = _commission("12345678901234567891")
    with localcontext() as context:
        context.prec = 6
        value = MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", "20"), _bet("bet-2", "30")),
            market_commission=commission,
            allocations=allocations,
            allocation_policy=_policy(),
        )
        low = value.per_bet_net()
    with localcontext() as context:
        context.prec = 60
        high = value.per_bet_net()
    assert low == high


def test_attribution_identity_is_decimal_context_independent() -> None:
    with localcontext() as context:
        context.prec = 6
        low = MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", "123456789.1234500"),),
            market_commission=_commission("0.1234500"),
        ).attribution_id
    with localcontext() as context:
        context.prec = 50
        high = MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", "123456789.1234500"),),
            market_commission=_commission("0.1234500"),
        ).attribution_id
    assert low == high


def test_per_bet_child_is_parent_and_policy_bound_and_copy_fails_closed() -> None:
    value = MarketCommissionAttribution(
        gross_bets=(_bet("bet-1", "10"),),
        market_commission=_commission("1"),
        allocations=(BetCommissionAllocation("bet-1", Decimal("1")),),
        allocation_policy=_policy(),
    )
    child = value.per_bet_net()[0]

    child.assert_derived_from(value)
    assert child.attribution_id == value.attribution_id
    assert child.allocation_policy_sha256 == value.allocation_policy.policy_sha256

    copied = replace(child)
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="not issued",
    ):
        copied.assert_derived_from(value)

    other_policy = CommissionAllocationPolicyRef(
        policy_id=value.allocation_policy.policy_id,
        policy_version=value.allocation_policy.policy_version,
        policy_sha256="d" * 64,
    )
    other_parent = MarketCommissionAttribution(
        gross_bets=value.gross_bets,
        market_commission=value.market_commission,
        allocations=value.allocations,
        allocation_policy=other_policy,
    )
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="not bound",
    ):
        child.assert_derived_from(other_parent)


def test_direct_per_bet_child_cannot_mint_accepted_derivation() -> None:
    value = MarketCommissionAttribution(
        gross_bets=(_bet("bet-1", "10"),),
        market_commission=_commission("1"),
        allocations=(BetCommissionAllocation("bet-1", Decimal("1")),),
        allocation_policy=_policy(),
    )
    forged = BetAttributedNet(
        venue_id="betfair",
        account_id="account-a",
        market_id="1.234",
        bet_id="bet-1",
        gross_profit=Decimal("10"),
        allocated_commission=Decimal("1"),
        derived_net_profit=Decimal("9"),
        attribution_id=value.attribution_id,
        allocation_policy_id=value.allocation_policy.policy_id,
        allocation_policy_version=value.allocation_policy.policy_version,
        allocation_policy_sha256=value.allocation_policy.policy_sha256,
        provider_exact=False,
    )
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="not issued",
    ):
        forged.assert_derived_from(value)


@pytest.mark.parametrize(
    "value",
    (
        "1E+100000000",
        "1E-100000000",
        "0E+100000000",
        "0E-100000000",
        "1E+37",
        "1E-19",
    ),
)
def test_money_domain_rejects_exponent_resource_escape(value: str) -> None:
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="bounded monetary Decimal domain",
    ):
        _bet("bet-1", value)


def test_money_domain_accepts_bounded_large_and_scaled_values() -> None:
    high = "9999999999999999999999999999999999999"
    scaled = "0.000000000000000001"
    assert _bet("bet-1", high).gross_profit == Decimal(high)
    assert _bet("bet-1", scaled).gross_profit == Decimal(scaled)


def test_nonfinite_money_is_rejected() -> None:
    with pytest.raises(BetfairCommissionAttributionError, match="finite Decimal"):
        _bet("bet-1", "NaN")
    with pytest.raises(BetfairCommissionAttributionError, match="finite Decimal"):
        _commission("Infinity")


def test_policy_version_rejects_bool_alias() -> None:
    with pytest.raises(BetfairCommissionAttributionError, match="positive integer"):
        CommissionAllocationPolicyRef("policy", True, A)


def test_subclasses_cannot_mint_canonical_economic_components() -> None:
    class ForgedBet(BetGrossAmount):
        pass

    forged = ForgedBet(
        venue_id="betfair",
        account_id="account-a",
        bet_id="bet-1",
        market_id="1.234",
        currency="EUR",
        gross_profit=Decimal("2"),
        evidence_sha256=A,
    )
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="exact BetGrossAmount",
    ):
        MarketCommissionAttribution(
            gross_bets=(forged,),
            market_commission=_commission("0.2"),
        )
