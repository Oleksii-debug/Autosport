from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.provider_funds_feasibility import (
    ProviderFundsAllocation,
    ProviderFundsAssessment,
    ProviderFundsFeasibilityError,
    ProviderFundsFeasibilityReport,
    ProviderFundsRequirementSemantics,
    ProviderFundsState,
    assess_provider_funds,
)


_HASH = "a" * 64
_T1 = "2026-09-21T08:00:00+00:00"
_T2 = "2026-09-21T08:00:05+00:00"
_T3 = "2026-09-21T08:01:00+00:00"


def _profile(
    *,
    venue_id: str,
    account_id: str,
    adapter_id: str = "adapter",
    balance_supported: bool = True,
) -> BookmakerCapabilityProfile:
    facts = (
        (
            BookmakerCapabilityFact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        )
        if balance_supported
        else ()
    )
    return BookmakerCapabilityProfile(
        venue_id=venue_id,
        account_id=account_id,
        adapter_id=adapter_id,
        adapter_version="1",
        profile_version=1,
        facts=facts,
        observed_at=_T1,
        source_ref="provider-probe",
        source_payload_sha256=_HASH,
    )


def _snapshot(
    *,
    venue_id: str,
    account_id: str,
    amount: str | None,
    currency: str = "EUR",
    adapter_id: str = "adapter",
    snapshot_at: str = _T1,
    balance_at: str = _T1,
) -> BookmakerAccountSnapshot:
    if amount is None:
        return BookmakerAccountSnapshot(
            profile=_profile(
                venue_id=venue_id,
                account_id=account_id,
                adapter_id=adapter_id,
                balance_supported=False,
            ),
            observed_capabilities=frozenset(),
            observed_at=snapshot_at,
        )
    balance = BookmakerBalanceObservation(
        venue_id=venue_id,
        account_id=account_id,
        adapter_id=adapter_id,
        observation_id=f"balance-{venue_id}-{account_id}-{amount}",
        currency=currency,
        available_balance=Decimal(amount),
        observed_at=balance_at,
        source_payload_sha256=_HASH,
    )
    return BookmakerAccountSnapshot(
        profile=_profile(
            venue_id=venue_id,
            account_id=account_id,
            adapter_id=adapter_id,
        ),
        observed_capabilities=frozenset({BookmakerCapability.BALANCE_READ}),
        observed_at=snapshot_at,
        balance=balance,
    )


def _allocation(
    allocation_id: str,
    *,
    venue_id: str = "A",
    account_id: str = "acct-a",
    adapter_id: str = "adapter",
    currency: str = "EUR",
    amount: str = "50",
) -> ProviderFundsAllocation:
    return ProviderFundsAllocation(
        allocation_id=allocation_id,
        venue_id=venue_id,
        account_id=account_id,
        adapter_id=adapter_id,
        currency=currency,
        amount=Decimal(amount),
    )


def test_fresh_balance_coverage_is_descriptive_until_cash_requirement_is_authoritative() -> None:
    report = assess_provider_funds(
        (_allocation("leg-1"),),
        (_snapshot(venue_id="A", account_id="acct-a", amount="100"),),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )

    assessment = report.assessments[0]
    assert assessment.state is ProviderFundsState.UNKNOWN
    assert assessment.available_balance == Decimal("100")
    assert assessment.requested_amount == Decimal("50")
    assert assessment.balance_covers_supplied_cash_assertion is True
    assert report.all_balances_cover_supplied_cash_assertions is True
    assert report.all_snapshot_sufficient is False
    report.assert_authoritative_projection()
    assert report.funds_reserved is False
    assert report.transfer_authorized is False
    assert report.requirement_authority_verified is False
    assert report.provider_write_authorized is False
    assert report.real_money_execution is False
    assert report.allocations[0].required_account_cash == Decimal("50")
    assert report.allocations[0].requirement_authority_verified is False
    assert (
        report.allocations[0].requirement_semantics
        is ProviderFundsRequirementSemantics.CALLER_ASSERTED_REQUIRED_ACCOUNT_CASH
    )


def test_rich_other_account_cannot_fund_target_account() -> None:
    report = assess_provider_funds(
        (_allocation("leg-a", amount="50"),),
        (
            _snapshot(venue_id="A", account_id="acct-a", amount="10"),
            _snapshot(venue_id="B", account_id="acct-b", amount="500"),
        ),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )

    assessment = report.assessments[0]
    assert assessment.venue_id == "A"
    assert assessment.state is ProviderFundsState.INSUFFICIENT
    assert assessment.available_balance == Decimal("10")
    assert assessment.balance_covers_supplied_cash_assertion is False
    assert report.all_snapshot_sufficient is False


def test_multiple_allocations_same_account_are_aggregated_exactly() -> None:
    report = assess_provider_funds(
        (
            _allocation("leg-1", amount="60.00"),
            _allocation("leg-2", amount="40.01"),
        ),
        (_snapshot(venue_id="A", account_id="acct-a", amount="100"),),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )

    assessment = report.assessments[0]
    assert assessment.requested_amount == Decimal("100.01")
    assert assessment.state is ProviderFundsState.INSUFFICIENT


def test_missing_required_account_snapshot_is_unknown() -> None:
    report = assess_provider_funds(
        (_allocation("leg-1", venue_id="A", account_id="acct-a"),),
        (_snapshot(venue_id="B", account_id="acct-b", amount="500"),),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )

    assert report.assessments[0].state is ProviderFundsState.UNKNOWN
    assert report.assessments[0].available_balance is None
    assert report.all_balances_cover_supplied_cash_assertions is False


def test_snapshot_without_complete_balance_read_is_unknown_not_zero() -> None:
    report = assess_provider_funds(
        (_allocation("leg-1"),),
        (_snapshot(venue_id="A", account_id="acct-a", amount=None),),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )

    assessment = report.assessments[0]
    assert assessment.state is ProviderFundsState.UNKNOWN
    assert assessment.available_balance is None
    assert "not completely observed" in assessment.reason


def test_stale_balance_is_unknown_even_when_amount_would_be_enough() -> None:
    report = assess_provider_funds(
        (_allocation("leg-1"),),
        (_snapshot(venue_id="A", account_id="acct-a", amount="100"),),
        decision_ts=_T3,
        max_balance_age_seconds=Decimal("10"),
    )

    assert report.assessments[0].state is ProviderFundsState.UNKNOWN
    assert report.assessments[0].available_balance is None


def test_future_snapshot_is_unknown() -> None:
    report = assess_provider_funds(
        (_allocation("leg-1"),),
        (
            _snapshot(
                venue_id="A",
                account_id="acct-a",
                amount="100",
                snapshot_at=_T3,
                balance_at=_T1,
            ),
        ),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )

    assert report.assessments[0].state is ProviderFundsState.UNKNOWN
    assert "future" in report.assessments[0].reason


def test_currency_mismatch_is_unknown_not_cross_currency_funding() -> None:
    report = assess_provider_funds(
        (_allocation("leg-1", currency="EUR"),),
        (
            _snapshot(
                venue_id="A",
                account_id="acct-a",
                amount="100",
                currency="USD",
            ),
        ),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )

    assert report.assessments[0].state is ProviderFundsState.UNKNOWN
    assert "currency" in report.assessments[0].reason


def test_adapter_identity_mismatch_is_unknown() -> None:
    report = assess_provider_funds(
        (_allocation("leg-1", adapter_id="adapter-a"),),
        (
            _snapshot(
                venue_id="A",
                account_id="acct-a",
                adapter_id="adapter-b",
                amount="100",
            ),
        ),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )

    assert report.assessments[0].state is ProviderFundsState.UNKNOWN


def test_duplicate_snapshot_for_same_account_is_rejected_instead_of_silently_selected() -> None:
    first = _snapshot(venue_id="A", account_id="acct-a", amount="100")
    second = _snapshot(
        venue_id="A",
        account_id="acct-a",
        amount="101",
        snapshot_at=_T2,
        balance_at=_T2,
    )
    with pytest.raises(
        ProviderFundsFeasibilityError,
        match="multiple snapshots",
    ):
        assess_provider_funds(
            (_allocation("leg-1"),),
            (first, second),
            decision_ts=_T2,
            max_balance_age_seconds=Decimal("10"),
        )


def test_duplicate_allocation_identity_is_rejected() -> None:
    with pytest.raises(
        ProviderFundsFeasibilityError,
        match="allocation_id values must be unique",
    ):
        assess_provider_funds(
            (_allocation("same"), _allocation("same")),
            (_snapshot(venue_id="A", account_id="acct-a", amount="100"),),
            decision_ts=_T2,
            max_balance_age_seconds=Decimal("10"),
        )


def test_report_identity_is_deterministic_across_allocation_input_order() -> None:
    snapshots = (
        _snapshot(venue_id="A", account_id="acct-a", amount="100"),
        _snapshot(venue_id="B", account_id="acct-b", amount="100"),
    )
    first = assess_provider_funds(
        (
            _allocation("a", venue_id="A", account_id="acct-a", amount="10"),
            _allocation("b", venue_id="B", account_id="acct-b", amount="20"),
        ),
        snapshots,
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )
    second = assess_provider_funds(
        (
            _allocation("b", venue_id="B", account_id="acct-b", amount="20"),
            _allocation("a", venue_id="A", account_id="acct-a", amount="10"),
        ),
        tuple(reversed(snapshots)),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )

    assert first == second
    assert first.report_sha256 == second.report_sha256
    assert first.all_snapshot_sufficient is False
    assert second.all_snapshot_sufficient is False
    assert first.all_balances_cover_supplied_cash_assertions is True
    assert second.all_balances_cover_supplied_cash_assertions is True


def test_snapshot_subclass_is_rejected_as_positive_authority() -> None:
    class ForgedSnapshot(BookmakerAccountSnapshot):
        pass

    canonical = _snapshot(
        venue_id="A",
        account_id="acct-a",
        amount="100",
    )
    forged = ForgedSnapshot(
        profile=canonical.profile,
        observed_capabilities=canonical.observed_capabilities,
        observed_at=canonical.observed_at,
        balance=canonical.balance,
    )

    with pytest.raises(
        ProviderFundsFeasibilityError,
        match="exact BookmakerAccountSnapshot",
    ):
        assess_provider_funds(
            (_allocation("leg-1"),),
            (forged,),
            decision_ts=_T2,
            max_balance_age_seconds=Decimal("10"),
        )


def test_provider_currency_contract_accepts_non_iso_provider_code() -> None:
    report = assess_provider_funds(
        (_allocation("leg-1", currency="USDT"),),
        (
            _snapshot(
                venue_id="A",
                account_id="acct-a",
                amount="100",
                currency="USDT",
            ),
        ),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )

    assert report.assessments[0].state is ProviderFundsState.UNKNOWN
    assert report.assessments[0].currency == "USDT"
    assert report.all_balances_cover_supplied_cash_assertions is True


def test_report_identity_binds_allocation_ids_not_only_account_aggregate() -> None:
    snapshot = _snapshot(
        venue_id="A",
        account_id="acct-a",
        amount="100",
    )
    first = assess_provider_funds(
        (_allocation("leg-original", amount="50"),),
        (snapshot,),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )
    second = assess_provider_funds(
        (_allocation("leg-different", amount="50"),),
        (snapshot,),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )

    assert first.assessments == second.assessments
    assert first.allocations != second.allocations
    assert first.report_sha256 != second.report_sha256


def test_direct_positive_report_construction_cannot_mint_projection_authority() -> None:
    allocation = _allocation("leg-1", amount="50")
    forged_assessment = ProviderFundsAssessment(
        venue_id="A",
        account_id="acct-a",
        adapter_id="adapter",
        currency="EUR",
        requested_amount=Decimal("50"),
        state=ProviderFundsState.SNAPSHOT_SUFFICIENT_BUT_UNRESERVED,
        reason="caller-forged positive state",
        available_balance=Decimal("100"),
        balance_observation_id="obs",
        balance_source_payload_sha256=_HASH,
        balance_observed_at=_T1,
    )
    forged = ProviderFundsFeasibilityReport(
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
        allocations=(allocation,),
        assessments=(forged_assessment,),
    )

    assert forged.all_snapshot_sufficient is False
    assert forged.all_balances_cover_supplied_cash_assertions is False
    with pytest.raises(
        ProviderFundsFeasibilityError,
        match="not issued by canonical evaluator",
    ):
        forged.assert_authoritative_projection()


def test_dataclass_replace_loses_projection_authority() -> None:
    canonical = assess_provider_funds(
        (_allocation("leg-1"),),
        (_snapshot(venue_id="A", account_id="acct-a", amount="100"),),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )
    assert canonical.all_balances_cover_supplied_cash_assertions is True

    copied = replace(canonical)

    assert copied == canonical
    assert copied.all_balances_cover_supplied_cash_assertions is False
    with pytest.raises(ProviderFundsFeasibilityError):
        copied.assert_authoritative_projection()


def test_report_rejects_assessment_amount_not_derived_from_allocations() -> None:
    allocation = _allocation("leg-1", amount="50")
    forged_assessment = ProviderFundsAssessment(
        venue_id="A",
        account_id="acct-a",
        adapter_id="adapter",
        currency="EUR",
        requested_amount=Decimal("1"),
        state=ProviderFundsState.SNAPSHOT_SUFFICIENT_BUT_UNRESERVED,
        reason="forged smaller requirement",
        available_balance=Decimal("100"),
        balance_observation_id="obs",
        balance_source_payload_sha256=_HASH,
        balance_observed_at=_T1,
    )

    with pytest.raises(
        ProviderFundsFeasibilityError,
        match="requested_amount does not match grouped allocation cash",
    ):
        ProviderFundsFeasibilityReport(
            decision_ts=_T2,
            max_balance_age_seconds=Decimal("10"),
            allocations=(allocation,),
            assessments=(forged_assessment,),
        )


def test_known_assessment_state_requires_complete_balance_evidence() -> None:
    with pytest.raises(
        ProviderFundsFeasibilityError,
        match="requires exact balance evidence",
    ):
        ProviderFundsAssessment(
            venue_id="A",
            account_id="acct-a",
            adapter_id="adapter",
            currency="EUR",
            requested_amount=Decimal("50"),
            state=ProviderFundsState.SNAPSHOT_SUFFICIENT_BUT_UNRESERVED,
            reason="forged positive without provider evidence",
        )


def test_assessment_state_must_match_available_balance_arithmetic() -> None:
    with pytest.raises(
        ProviderFundsFeasibilityError,
        match="insufficient assessment contradicts",
    ):
        ProviderFundsAssessment(
            venue_id="A",
            account_id="acct-a",
            adapter_id="adapter",
            currency="EUR",
            requested_amount=Decimal("50"),
            state=ProviderFundsState.INSUFFICIENT,
            reason="forged insufficient",
            available_balance=Decimal("100"),
            balance_observation_id="obs",
            balance_source_payload_sha256=_HASH,
            balance_observed_at=_T1,
        )


def test_mutating_issued_report_payload_revokes_projection_authority() -> None:
    report = assess_provider_funds(
        (_allocation("leg-1"),),
        (_snapshot(venue_id="A", account_id="acct-a", amount="100"),),
        decision_ts=_T2,
        max_balance_age_seconds=Decimal("10"),
    )
    assert report.all_balances_cover_supplied_cash_assertions is True
    report.assert_authoritative_projection()

    object.__setattr__(report.assessments[0], "reason", "mutated after issuance")

    assert report.all_balances_cover_supplied_cash_assertions is False
    with pytest.raises(ProviderFundsFeasibilityError):
        report.assert_authoritative_projection()


def test_allocation_amount_is_explicitly_non_authoritative_required_account_cash() -> None:
    allocation = _allocation("leg-1", amount="25")

    assert allocation.required_account_cash == Decimal("25")
    assert (
        allocation.requirement_semantics
        is ProviderFundsRequirementSemantics.CALLER_ASSERTED_REQUIRED_ACCOUNT_CASH
    )
    assert allocation.requirement_authority_verified is False

    with pytest.raises(
        ProviderFundsFeasibilityError,
        match="requirement_semantics",
    ):
        ProviderFundsAllocation(
            allocation_id="bad",
            venue_id="A",
            account_id="acct-a",
            adapter_id="adapter",
            currency="EUR",
            amount=Decimal("25"),
            requirement_semantics="stake",  # type: ignore[arg-type]
        )
