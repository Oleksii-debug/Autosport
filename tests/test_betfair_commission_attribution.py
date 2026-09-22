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


def test_structural_bet_sum_is_available_but_exact_market_net_is_withheld() -> None:
    value = MarketCommissionAttribution(
        gross_bets=(_bet("bet-1", "10"), _bet("bet-2", "-4")),
        market_commission=_commission("1.5"),
    )

    assert value.supplied_gross_profit == Decimal("6")
    assert value.grants_provider_origin_authority is False
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="complete market population authority",
    ):
        _ = value.market_gross_profit
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="complete market population authority",
    ):
        _ = value.market_net_profit
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="complete market population authority",
    ):
        value.per_bet_net()


def test_well_formed_allocation_stays_blocked_until_population_authority() -> None:
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="complete market population authority",
    ):
        MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", "10"), _bet("bet-2", "-4")),
            market_commission=_commission("1.5"),
            allocations=(
                BetCommissionAllocation("bet-1", Decimal("1.0")),
                BetCommissionAllocation("bet-2", Decimal("0.5")),
            ),
            allocation_policy=_policy(),
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


def test_structural_subset_cannot_mint_exact_market_net() -> None:
    value = MarketCommissionAttribution(
        gross_bets=(_bet("visible-bet", "10"),),
        market_commission=_commission("2"),
    )
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="complete market population authority",
    ):
        _ = value.market_net_profit


def test_structural_subset_cannot_allocate_whole_market_commission() -> None:
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="complete market population authority",
    ):
        MarketCommissionAttribution(
            gross_bets=(_bet("visible-bet", "10"),),
            market_commission=_commission("2"),
            allocations=(
                BetCommissionAllocation("visible-bet", Decimal("2")),
            ),
            allocation_policy=_policy(),
        )


def test_negative_commission_remains_structural_without_minting_exact_net() -> None:
    value = MarketCommissionAttribution(
        gross_bets=(_bet("bet-1", "6"),),
        market_commission=_commission("-1.25"),
    )
    assert value.market_commission.commission_charge == Decimal("-1.25")
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="complete market population authority",
    ):
        _ = value.market_net_profit


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



def test_supplied_gross_is_decimal_context_independent() -> None:
    gross_one = "123456789012345678901234567890.12345"
    gross_two = "0.87655"
    commission = "0.11111"
    with localcontext() as context:
        context.prec = 6
        low = MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", gross_one), _bet("bet-2", gross_two)),
            market_commission=_commission(commission),
        )
        low_gross = low.supplied_gross_profit
    with localcontext() as context:
        context.prec = 60
        high = MarketCommissionAttribution(
            gross_bets=(_bet("bet-1", gross_one), _bet("bet-2", gross_two)),
            market_commission=_commission(commission),
        )
        high_gross = high.supplied_gross_profit
    assert low_gross == high_gross == Decimal(
        "123456789012345678901234567891"
    )

def test_valid_allocation_conservation_reaches_population_fence_in_any_context() -> None:
    allocations = (
        BetCommissionAllocation(
            "bet-1", Decimal("12345678901234567890.12345")
        ),
        BetCommissionAllocation("bet-2", Decimal("0.87655")),
    )
    commission = _commission("12345678901234567891")
    for precision in (6, 60):
        with localcontext() as context:
            context.prec = precision
            with pytest.raises(
                BetfairCommissionAttributionError,
                match="complete market population authority",
            ):
                MarketCommissionAttribution(
                    gross_bets=(_bet("bet-1", "20"), _bet("bet-2", "30")),
                    market_commission=commission,
                    allocations=allocations,
                    allocation_policy=_policy(),
                )

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


def test_direct_per_bet_child_cannot_mint_accepted_derivation() -> None:
    value = MarketCommissionAttribution(
        gross_bets=(_bet("bet-1", "10"),),
        market_commission=_commission("1"),
    )
    policy = _policy()
    forged = BetAttributedNet(
        venue_id="betfair",
        account_id="account-a",
        market_id="1.234",
        bet_id="bet-1",
        gross_profit=Decimal("10"),
        allocated_commission=Decimal("1"),
        derived_net_profit=Decimal("9"),
        attribution_id=value.attribution_id,
        allocation_policy_id=policy.policy_id,
        allocation_policy_version=policy.policy_version,
        allocation_policy_sha256=policy.policy_sha256,
        provider_exact=False,
    )
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="not issued",
    ):
        forged.assert_derived_from(value)
    copied = replace(forged)
    with pytest.raises(
        BetfairCommissionAttributionError,
        match="not issued",
    ):
        copied.assert_derived_from(value)


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
