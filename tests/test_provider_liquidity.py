from decimal import Decimal

import pytest

from autosport.provider_liquidity import (
    GlobalLiquiditySnapshot,
    LiquidityTransfer,
    LiquidityTransferDirection,
    LiquidityTransferStage,
    ProviderLiquidityAccount,
    ProviderLiquidityBalance,
    ProviderLiquidityShortfall,
    build_liquidity_rebalance_plan,
)


D = Decimal


class _HostileDecimal(Decimal):
    def is_finite(self):
        raise AssertionError("Decimal subclass virtual method must not execute")

    def __lt__(self, other):
        raise AssertionError("Decimal subclass comparison must not execute")

    def as_tuple(self):
        raise AssertionError("Decimal subclass canonicalization must not execute")

    def __sub__(self, other):
        raise AssertionError("Decimal subclass arithmetic must not execute")


def account(
    provider_id: str,
    cash: str,
    committed: str,
    minimum: str,
    target: str,
    priority: int = 0,
    *,
    account_id: str = "account-main",
    currency: str = "EUR",
) -> ProviderLiquidityAccount:
    return ProviderLiquidityAccount(
        provider_id=provider_id,
        account_id=account_id,
        currency=currency,
        cash_balance=D(cash),
        committed_cash=D(committed),
        minimum_cash=D(minimum),
        target_cash=D(target),
        funding_priority=priority,
    )


def snapshot(
    bank_cash: str,
    bank_reserve: str,
    provider_accounts: tuple[ProviderLiquidityAccount, ...],
    *,
    bankroll_id: str = "bankroll-main",
    currency: str = "EUR",
) -> GlobalLiquiditySnapshot:
    return GlobalLiquiditySnapshot(
        bankroll_id=bankroll_id,
        currency=currency,
        bank_cash=D(bank_cash),
        bank_reserve=D(bank_reserve),
        provider_accounts=provider_accounts,
    )


def test_rebalance_sweeps_excess_then_restores_minimums_and_targets() -> None:
    state = snapshot(
        "50",
        "20",
        (
            account("provider-a", "200", "50", "80", "100"),
            account("provider-b", "10", "0", "50", "80", 10),
            account("provider-c", "20", "0", "30", "60"),
        ),
    )

    plan = build_liquidity_rebalance_plan(state)

    assert plan.bankroll_id == "bankroll-main"
    assert plan.currency == "EUR"
    assert plan.conserves_cash
    assert plan.total_cash_before == D("280")
    assert plan.total_cash_after == D("280")
    assert plan.bank_cash_after == D("40")
    assert plan.bank_reserve_satisfied
    assert plan.minimums_fully_funded
    assert plan.targets_fully_funded
    assert {
        (balance.provider_id, balance.account_id): balance.cash_balance
        for balance in plan.balances_after
    } == {
        ("provider-a", "account-main"): D("100"),
        ("provider-b", "account-main"): D("80"),
        ("provider-c", "account-main"): D("60"),
    }
    assert plan.transfers[0].direction is LiquidityTransferDirection.PROVIDER_TO_BANK
    assert plan.transfers[0].stage is LiquidityTransferStage.SWEEP_EXCESS
    assert plan.transfers[0].amount == D("100")
    assert plan.transfers[0].currency == "EUR"
    assert plan.balances_verified is False
    assert plan.commitments_verified is False
    assert plan.funds_reserved is False
    assert plan.grants_transfer_authority is False
    assert plan.grants_execution_authority is False


def test_scarcity_preserves_bank_reserve_and_prioritizes_minimums() -> None:
    state = snapshot(
        "40",
        "20",
        (
            account("low-priority", "10", "0", "30", "50", 0),
            account("high-priority", "10", "0", "30", "50", 5),
        ),
    )

    plan = build_liquidity_rebalance_plan(state)

    assert plan.bank_cash_after == D("20")
    assert plan.bank_reserve_satisfied
    assert {
        balance.provider_id: balance.cash_balance for balance in plan.balances_after
    } == {"high-priority": D("30"), "low-priority": D("10")}
    assert [
        (item.provider_id, item.account_id, item.amount)
        for item in plan.minimum_shortfalls_after
    ] == [("low-priority", "account-main", D("20"))]
    assert plan.targets_fully_funded is False
    assert all(
        transfer.stage is LiquidityTransferStage.RESTORE_MINIMUM
        for transfer in plan.transfers
    )


def test_committed_cash_is_never_swept_below_commitment() -> None:
    state = snapshot(
        "10",
        "0",
        (account("provider-a", "120", "110", "20", "100"),),
    )

    plan = build_liquidity_rebalance_plan(state)

    assert plan.transfers[0].amount == D("10")
    assert plan.balances_after[0].cash_balance == D("110")
    assert plan.total_cash_before == plan.total_cash_after == D("130")


def test_plan_is_deterministic_across_input_order_and_decimal_scale() -> None:
    a = account("provider-a", "10.00", "0", "20", "30", 1)
    b = account("provider-b", "50.0", "0", "10", "20", 0)
    first = build_liquidity_rebalance_plan(snapshot("40.00", "10.0", (a, b)))
    second = build_liquidity_rebalance_plan(
        snapshot(
            "40",
            "10",
            (
                account("provider-b", "50", "0.0", "10.0", "20.00", 0),
                account("provider-a", "10", "0.00", "20.0", "30.000", 1),
            ),
        )
    )

    assert first.transfers == second.transfers
    assert first.balances_after == second.balances_after
    assert first.evidence_id == second.evidence_id


def test_mixed_currency_nominal_aggregation_fails_closed() -> None:
    with pytest.raises(ValueError, match="exact snapshot currency"):
        snapshot(
            "100",
            "10",
            (
                account(
                    "provider-a",
                    "100",
                    "0",
                    "10",
                    "20",
                    currency="GBP",
                ),
            ),
            currency="EUR",
        )


def test_two_accounts_at_one_provider_are_distinct_and_supported() -> None:
    state = snapshot(
        "20",
        "0",
        (
            account(
                "provider-a",
                "0",
                "0",
                "10",
                "10",
                5,
                account_id="account-1",
            ),
            account(
                "provider-a",
                "0",
                "0",
                "10",
                "10",
                1,
                account_id="account-2",
            ),
        ),
    )

    plan = build_liquidity_rebalance_plan(state)

    assert [
        (transfer.provider_id, transfer.account_id, transfer.amount)
        for transfer in plan.transfers
    ] == [
        ("provider-a", "account-1", D("10")),
        ("provider-a", "account-2", D("10")),
    ]
    assert [
        (balance.provider_id, balance.account_id, balance.cash_balance)
        for balance in plan.balances_after
    ] == [
        ("provider-a", "account-1", D("10")),
        ("provider-a", "account-2", D("10")),
    ]


def test_duplicate_provider_account_identity_fails_closed() -> None:
    with pytest.raises(ValueError, match="account identities must be unique"):
        snapshot(
            "10",
            "0",
            (
                account(
                    "provider-a",
                    "10",
                    "0",
                    "1",
                    "2",
                    account_id="same-account",
                ),
                account(
                    "provider-a",
                    "20",
                    "0",
                    "1",
                    "2",
                    account_id="same-account",
                ),
            ),
        )


def test_evidence_identity_binds_bankroll_account_and_currency_identity() -> None:
    base = build_liquidity_rebalance_plan(
        snapshot(
            "10",
            "0",
            (
                account(
                    "provider-a",
                    "10",
                    "0",
                    "1",
                    "2",
                    account_id="account-1",
                ),
            ),
        )
    )
    other_bankroll = build_liquidity_rebalance_plan(
        snapshot(
            "10",
            "0",
            (
                account(
                    "provider-a",
                    "10",
                    "0",
                    "1",
                    "2",
                    account_id="account-1",
                ),
            ),
            bankroll_id="bankroll-other",
        )
    )
    other_account = build_liquidity_rebalance_plan(
        snapshot(
            "10",
            "0",
            (
                account(
                    "provider-a",
                    "10",
                    "0",
                    "1",
                    "2",
                    account_id="account-2",
                ),
            ),
        )
    )
    other_currency = build_liquidity_rebalance_plan(
        snapshot(
            "10",
            "0",
            (
                account(
                    "provider-a",
                    "10",
                    "0",
                    "1",
                    "2",
                    account_id="account-1",
                    currency="GBP",
                ),
            ),
            currency="GBP",
        )
    )

    assert len(
        {
            base.evidence_id,
            other_bankroll.evidence_id,
            other_account.evidence_id,
            other_currency.evidence_id,
        }
    ) == 4


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cash_balance", 1.0),
        ("cash_balance", D("NaN")),
        ("cash_balance", D("-1")),
        ("committed_cash", D("-1")),
        ("minimum_cash", D("-1")),
        ("target_cash", D("-1")),
    ],
)
def test_account_rejects_unsafe_money_values(field: str, value: object) -> None:
    kwargs = {
        "provider_id": "provider-a",
        "account_id": "account-main",
        "currency": "EUR",
        "cash_balance": D("10"),
        "committed_cash": D("0"),
        "minimum_cash": D("1"),
        "target_cash": D("2"),
        "funding_priority": 0,
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        ProviderLiquidityAccount(**kwargs)


def test_money_ingress_rejects_decimal_subclass_before_virtual_dispatch() -> None:
    hostile = _HostileDecimal("10")

    with pytest.raises(ValueError, match="finite exact Decimal"):
        ProviderLiquidityAccount(
            provider_id="provider-a",
            account_id="account-main",
            currency="EUR",
            cash_balance=hostile,
            committed_cash=D("0"),
            minimum_cash=D("1"),
            target_cash=D("2"),
        )

    with pytest.raises(ValueError, match="finite exact Decimal"):
        GlobalLiquiditySnapshot(
            bankroll_id="bankroll-main",
            currency="EUR",
            bank_cash=hostile,
            bank_reserve=D("0"),
            provider_accounts=(),
        )

    with pytest.raises(ValueError, match="finite exact Decimal"):
        LiquidityTransfer(
            provider_id="provider-a",
            account_id="account-main",
            currency="EUR",
            direction=LiquidityTransferDirection.BANK_TO_PROVIDER,
            stage=LiquidityTransferStage.RESTORE_MINIMUM,
            amount=hostile,
        )


def test_account_and_snapshot_invariants_fail_closed() -> None:
    with pytest.raises(ValueError, match="committed_cash"):
        account("provider-a", "10", "11", "1", "2")
    with pytest.raises(ValueError, match="minimum_cash"):
        account("provider-a", "10", "0", "5", "4")
    with pytest.raises(ValueError, match="funding_priority"):
        account("provider-a", "10", "0", "1", "2", True)
    with pytest.raises(ValueError, match="provider_id"):
        account(" provider-a", "10", "0", "1", "2")
    with pytest.raises(ValueError, match="account_id"):
        account(
            "provider-a",
            "10",
            "0",
            "1",
            "2",
            account_id=" account-main",
        )
    with pytest.raises(ValueError, match="currency"):
        account(
            "provider-a",
            "10",
            "0",
            "1",
            "2",
            currency=" EUR",
        )
    with pytest.raises(ValueError, match="bankroll_id"):
        snapshot(
            "10",
            "0",
            (account("provider-a", "10", "0", "1", "2"),),
            bankroll_id=" bankroll-main",
        )


def test_public_evidence_records_reject_invalid_manual_construction() -> None:
    with pytest.raises(ValueError, match="provider_id"):
        LiquidityTransfer(
            provider_id=" provider-a",
            account_id="account-main",
            currency="EUR",
            direction=LiquidityTransferDirection.BANK_TO_PROVIDER,
            stage=LiquidityTransferStage.RESTORE_MINIMUM,
            amount=D("1"),
        )
    with pytest.raises(ValueError, match="account_id"):
        LiquidityTransfer(
            provider_id="provider-a",
            account_id=" account-main",
            currency="EUR",
            direction=LiquidityTransferDirection.BANK_TO_PROVIDER,
            stage=LiquidityTransferStage.RESTORE_MINIMUM,
            amount=D("1"),
        )
    with pytest.raises(ValueError, match="direction"):
        LiquidityTransfer(
            provider_id="provider-a",
            account_id="account-main",
            currency="EUR",
            direction="bank_to_provider",  # type: ignore[arg-type]
            stage=LiquidityTransferStage.RESTORE_MINIMUM,
            amount=D("1"),
        )
    with pytest.raises(ValueError, match="stage"):
        LiquidityTransfer(
            provider_id="provider-a",
            account_id="account-main",
            currency="EUR",
            direction=LiquidityTransferDirection.BANK_TO_PROVIDER,
            stage="restore_minimum",  # type: ignore[arg-type]
            amount=D("1"),
        )
    with pytest.raises(ValueError, match="greater than zero"):
        LiquidityTransfer(
            provider_id="provider-a",
            account_id="account-main",
            currency="EUR",
            direction=LiquidityTransferDirection.BANK_TO_PROVIDER,
            stage=LiquidityTransferStage.RESTORE_MINIMUM,
            amount=D("0"),
        )
    with pytest.raises(ValueError, match="cash_balance"):
        ProviderLiquidityBalance("provider-a", "account-main", "EUR", D("-1"))
    with pytest.raises(ValueError, match="shortfall"):
        ProviderLiquidityShortfall(
            "provider-a",
            "account-main",
            "EUR",
            D("0"),
        )


def test_degraded_bank_below_reserve_is_representable_but_deploys_nothing() -> None:
    state = snapshot(
        "5",
        "10",
        (account("provider-a", "2", "0", "5", "8"),),
    )

    plan = build_liquidity_rebalance_plan(state)

    assert plan.transfers == ()
    assert plan.bank_cash_after == D("5")
    assert plan.bank_reserve_satisfied is False
    assert plan.minimum_shortfalls_after[0].amount == D("3")
    assert plan.target_shortfalls_after[0].amount == D("6")
    assert plan.conserves_cash


def test_extreme_precision_fails_closed_instead_of_silently_rounding() -> None:
    state = snapshot(
        "1",
        "0",
        (
            account(
                "provider-a",
                "123456789012345678901234567890123456789012345678901",
                "0",
                "0",
                "0",
            ),
        ),
    )

    with pytest.raises(ValueError, match="exactly representable"):
        build_liquidity_rebalance_plan(state)
