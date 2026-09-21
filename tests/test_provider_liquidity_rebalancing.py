from decimal import Decimal

import pytest

from autosport.provider_liquidity_rebalancing import (
    LiquidityReviewState,
    ProviderLiquidityAccount,
    review_provider_liquidity,
)


D = Decimal


def account(
    provider_id: str,
    available: str,
    target: str,
    *,
    currency: str = "EUR",
) -> ProviderLiquidityAccount:
    return ProviderLiquidityAccount(provider_id, currency, D(available), D(target))


def review(*accounts: ProviderLiquidityAccount, central: str, reserve: str = "0"):
    return review_provider_liquidity(
        currency="EUR",
        central_cash=D(central),
        protected_reserve=D(reserve),
        accounts=accounts,
    )


def test_balanced_accounts_need_no_rebalancing_authority() -> None:
    result = review(
        account("beta", "40", "30"),
        account("alpha", "20", "20"),
        central="25",
        reserve="10",
    )

    assert result.state is LiquidityReviewState.BALANCED
    assert result.currency == "EUR"
    assert [item.provider_id for item in result.accounts] == ["alpha", "beta"]
    assert result.total_provider_need == D("0")
    assert result.modeled_provider_surplus == D("10")
    assert result.requires_transfer_feasibility_check is False
    assert result.authorizes_transfer is False
    assert result.authorizes_execution is False


def test_central_cash_can_cover_provider_need_without_spending_reserve() -> None:
    result = review(account("book-a", "5", "30"), central="50", reserve="20")

    assert result.state is LiquidityReviewState.CENTRAL_CASH_COVERS
    assert result.central_deployable_cash == D("30")
    assert result.total_provider_need == D("25")
    assert result.need_after_central_cash == D("0")
    assert result.modeled_shortfall_after_surplus == D("0")
    assert result.requires_transfer_feasibility_check is False


def test_provider_surplus_is_only_rebalance_candidate_not_transfer_authority() -> None:
    result = review(
        account("exchange-a", "80", "30"),
        account("book-b", "10", "55"),
        central="10",
        reserve="10",
    )

    assert result.state is LiquidityReviewState.PROVIDER_REBALANCE_CANDIDATE
    assert result.total_provider_need == D("45")
    assert result.modeled_provider_surplus == D("50")
    assert result.need_after_central_cash == D("45")
    assert result.modeled_shortfall_after_surplus == D("0")
    assert result.requires_transfer_feasibility_check is True
    assert result.authorizes_transfer is False
    assert result.authorizes_execution is False


def test_global_shortfall_preserves_partial_rebalance_feasibility_truth() -> None:
    result = review(
        account("book-a", "10", "50"),
        account("book-b", "35", "20"),
        central="15",
        reserve="5",
    )

    assert result.state is LiquidityReviewState.GLOBAL_SHORTFALL
    assert result.central_deployable_cash == D("10")
    assert result.total_provider_need == D("40")
    assert result.modeled_provider_surplus == D("15")
    assert result.need_after_central_cash == D("30")
    assert result.modeled_shortfall_after_surplus == D("15")
    assert result.requires_transfer_feasibility_check is True
    assert result.authorizes_transfer is False


def test_protected_reserve_cannot_be_silently_used() -> None:
    result = review(account("book-a", "0", "20"), central="100", reserve="90")

    assert result.state is LiquidityReviewState.GLOBAL_SHORTFALL
    assert result.central_deployable_cash == D("10")
    assert result.modeled_shortfall_after_surplus == D("10")


def test_mixed_currency_fails_closed_instead_of_summing_nominal_amounts() -> None:
    with pytest.raises(ValueError, match="currency mismatch"):
        review_provider_liquidity(
            currency="EUR",
            central_cash=D("10"),
            protected_reserve=D("0"),
            accounts=[
                account("eur-book", "5", "20", currency="EUR"),
                account("gbp-book", "100", "0", currency="GBP"),
            ],
        )


def test_currency_is_normalized_once_and_bound_into_result() -> None:
    result = review_provider_liquidity(
        currency=" eur ",
        central_cash=D("0"),
        protected_reserve=D("0"),
        accounts=[account("book", "1", "1", currency="eur")],
    )

    assert result.currency == "EUR"
    assert result.accounts[0].currency == "EUR"


@pytest.mark.parametrize("currency", ["", "EU", "EURO", "12A", "€UR"])
def test_invalid_currency_code_fails_closed(currency: str) -> None:
    with pytest.raises(ValueError, match="currency"):
        review_provider_liquidity(
            currency=currency,
            central_cash=D("0"),
            protected_reserve=D("0"),
            accounts=[],
        )


@pytest.mark.parametrize(
    ("central", "reserve"),
    [
        (D("-1"), D("0")),
        (D("NaN"), D("0")),
        (D("Infinity"), D("0")),
        (D("1"), D("-1")),
        (D("1"), D("2")),
    ],
)
def test_invalid_central_money_fails_closed(central: Decimal, reserve: Decimal) -> None:
    with pytest.raises((TypeError, ValueError)):
        review_provider_liquidity(
            currency="EUR",
            central_cash=central,
            protected_reserve=reserve,
            accounts=[],
        )


@pytest.mark.parametrize(
    "bad_account",
    [
        ProviderLiquidityAccount("", "EUR", D("1"), D("1")),
        ProviderLiquidityAccount("   ", "EUR", D("1"), D("1")),
        ProviderLiquidityAccount("book", "EUR", D("-1"), D("1")),
        ProviderLiquidityAccount("book", "EUR", D("1"), D("-1")),
        ProviderLiquidityAccount("book", "EUR", D("NaN"), D("1")),
    ],
)
def test_invalid_provider_observations_fail_closed(
    bad_account: ProviderLiquidityAccount,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        review_provider_liquidity(
            currency="EUR",
            central_cash=D("0"),
            protected_reserve=D("0"),
            accounts=[bad_account],
        )


def test_duplicate_provider_identity_fails_closed_after_whitespace_normalization() -> None:
    with pytest.raises(ValueError, match="duplicate provider_id"):
        review_provider_liquidity(
            currency="EUR",
            central_cash=D("0"),
            protected_reserve=D("0"),
            accounts=[
                account("book-a", "1", "1"),
                account(" book-a ", "1", "1"),
            ],
        )


def test_float_money_is_rejected_to_avoid_binary_money_semantics() -> None:
    with pytest.raises(TypeError, match="central_cash must be Decimal"):
        review_provider_liquidity(
            currency="EUR",
            central_cash=1.0,  # type: ignore[arg-type]
            protected_reserve=D("0"),
            accounts=[],
        )


def test_small_money_matrix_preserves_decomposition_and_state_invariants() -> None:
    values = (0, 1, 5)
    for central in values:
        for reserve in values:
            if reserve > central:
                continue
            for a_available in values:
                for a_target in values:
                    for b_available in values:
                        for b_target in values:
                            result = review_provider_liquidity(
                                currency="EUR",
                                central_cash=D(central),
                                protected_reserve=D(reserve),
                                accounts=[
                                    ProviderLiquidityAccount(
                                        "a", "EUR", D(a_available), D(a_target)
                                    ),
                                    ProviderLiquidityAccount(
                                        "b", "EUR", D(b_available), D(b_target)
                                    ),
                                ],
                            )

                            assert result.central_deployable_cash == D(
                                central - reserve
                            )
                            assert result.total_provider_need == sum(
                                (item.need for item in result.accounts), D("0")
                            )
                            assert result.modeled_provider_surplus == sum(
                                (
                                    item.modeled_surplus
                                    for item in result.accounts
                                ),
                                D("0"),
                            )
                            for item in result.accounts:
                                assert item.need == D("0") or (
                                    item.modeled_surplus == D("0")
                                )
                                assert item.need - item.modeled_surplus == (
                                    item.target_working_cash
                                    - item.available_cash
                                )

                            expected_need_after_central = max(
                                result.total_provider_need
                                - result.central_deployable_cash,
                                D("0"),
                            )
                            expected_shortfall = max(
                                expected_need_after_central
                                - result.modeled_provider_surplus,
                                D("0"),
                            )
                            assert (
                                result.need_after_central_cash
                                == expected_need_after_central
                            )
                            assert (
                                result.modeled_shortfall_after_surplus
                                == expected_shortfall
                            )
                            assert (
                                result.requires_transfer_feasibility_check
                                is (
                                    expected_need_after_central > D("0")
                                    and result.modeled_provider_surplus > D("0")
                                )
                            )
                            assert result.authorizes_transfer is False
                            assert result.authorizes_execution is False


@pytest.mark.parametrize(
    ("first_available", "first_target", "second_available", "second_target"),
    [
        ("0", "5", "8", "2"),
        ("100.01", "100", "0", "0.01"),
        ("7", "7", "9", "20"),
    ],
)
def test_provider_input_order_cannot_change_review(
    first_available: str,
    first_target: str,
    second_available: str,
    second_target: str,
) -> None:
    first = account("provider-a", first_available, first_target)
    second = account("provider-b", second_available, second_target)
    forward = review_provider_liquidity(
        currency="EUR",
        central_cash=D("10"),
        protected_reserve=D("2"),
        accounts=[first, second],
    )
    reverse = review_provider_liquidity(
        currency="EUR",
        central_cash=D("10"),
        protected_reserve=D("2"),
        accounts=[second, first],
    )

    assert forward == reverse
