from decimal import Decimal

import pytest

from autosport.betfair_commission_attribution import (
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


def _single_visible_bet() -> BetGrossAmount:
    # This is intentionally only one caller-supplied BET row. The provider
    # account may contain another bet on the same market that is absent from
    # this structural DTO. Structural membership is not provider completeness.
    return BetGrossAmount(
        bet_id="visible-bet",
        market_id="1.234567",
        currency="EUR",
        gross_profit=Decimal("10"),
        evidence_sha256=A,
    )


def _whole_market_commission() -> MarketCommissionAmount:
    # MARKET-level commission is provider-wide for the market. Nothing in the
    # current structural type proves that _single_visible_bet() is the complete
    # BET population corresponding to this market receipt.
    return MarketCommissionAmount(
        market_id="1.234567",
        currency="EUR",
        commission_charge=Decimal("2"),
        evidence_sha256=B,
    )


def _allocation_policy() -> CommissionAllocationPolicyRef:
    return CommissionAllocationPolicyRef(
        policy_id="visible-subset-allocation",
        policy_version=1,
        policy_sha256=C,
    )


def test_structural_subset_cannot_mint_exact_market_net_without_complete_population_authority():
    value = MarketCommissionAttribution(
        gross_bets=(_single_visible_bet(),),
        market_commission=_whole_market_commission(),
    )

    # A safe contract must not expose exact MARKET net from arbitrary caller
    # membership. It needs product-owned provider/account/market completeness
    # evidence binding the full BET population to the MARKET rollup.
    with pytest.raises(BetfairCommissionAttributionError):
        _ = value.market_net_profit


def test_structural_subset_cannot_allocate_full_market_commission_to_visible_rows():
    # Exact conservation over a caller-supplied subset is insufficient:
    # allocating the whole MARKET commission to the only visible row would
    # silently charge any unseen same-market bets to this subset.
    with pytest.raises(BetfairCommissionAttributionError):
        MarketCommissionAttribution(
            gross_bets=(_single_visible_bet(),),
            market_commission=_whole_market_commission(),
            allocations=(
                BetCommissionAllocation(
                    bet_id="visible-bet",
                    allocated_commission=Decimal("2"),
                ),
            ),
            allocation_policy=_allocation_policy(),
        )
