from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.provider_payout_cap import (
    ProviderPayoutCapAssessmentState,
    ProviderPayoutCapError,
    ProviderPayoutCapEvidence,
    ProviderPayoutCapMeasure,
    ProviderPayoutCapScope,
    ProviderPayoutCapSourceKind,
    assess_provider_payout_cap,
    assert_provider_payout_cap_evidence_authoritative,
    seal_provider_payout_cap_structure,
    verify_provider_payout_cap_evidence,
)


NOW = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)
SHA_SCOPE = "1" * 64
SHA_SETTLEMENT = "2" * 64
SHA_SOURCE = "3" * 64
SHA_HEADROOM = "4" * 64
SHA_ACTION = "5" * 64


def _evidence(**changes: object) -> ProviderPayoutCapEvidence:
    values: dict[str, object] = {
        "provider_id": "provider-a",
        "account_id": "account-a",
        "adapter_id": "adapter-a",
        "jurisdiction": "SK",
        "currency": "EUR",
        "scope_kind": ProviderPayoutCapScope.PER_BET,
        "cap_measure": ProviderPayoutCapMeasure.GROSS_RETURN,
        "maximum_amount": Decimal("100.00"),
        "scope_binding_sha256": SHA_SCOPE,
        "settlement_rule_sha256": SHA_SETTLEMENT,
        "rule_version": "rule-v1",
        "observed_at": NOW - timedelta(minutes=5),
        "valid_until": NOW + timedelta(minutes=5),
        "source_kind": ProviderPayoutCapSourceKind.VERSIONED_PROVIDER_RULE,
        "source_ref": "provider-rule-v1",
        "source_payload_sha256": SHA_SOURCE,
    }
    values.update(changes)
    return ProviderPayoutCapEvidence(**values)


def _seal(
    evidence: ProviderPayoutCapEvidence,
    *,
    as_of: datetime = NOW,
    **expected_changes: object,
) -> ProviderPayoutCapEvidence:
    expected: dict[str, object] = {
        "provider_id": evidence.provider_id,
        "account_id": evidence.account_id,
        "adapter_id": evidence.adapter_id,
        "jurisdiction": evidence.jurisdiction,
        "currency": evidence.currency,
        "scope_kind": evidence.scope_kind,
        "cap_measure": evidence.cap_measure,
        "scope_binding_sha256": evidence.scope_binding_sha256,
        "settlement_rule_sha256": evidence.settlement_rule_sha256,
        "action_binding_sha256": evidence.action_binding_sha256,
    }
    expected.update(expected_changes)
    return seal_provider_payout_cap_structure(
        evidence,
        as_of=as_of,
        **expected,
    )


def test_per_bet_exact_boundary_is_structurally_within() -> None:
    evidence = _seal(_evidence())

    result = assess_provider_payout_cap(
        evidence=evidence,
        candidate_amount=Decimal("100.00"),
    )

    assert result.state is ProviderPayoutCapAssessmentState.WITHIN_STRUCTURAL_CAP
    assert result.effective_headroom == Decimal("100.00")
    assert result.provider_origin_proven is False
    assert result.supports_exact_execution is False
    assert result.no_cap_proven is False
    assert result.execution_authority is False


def test_one_exact_quantum_over_cap_is_structurally_exceeded() -> None:
    evidence = _seal(_evidence())

    result = assess_provider_payout_cap(
        evidence=evidence,
        candidate_amount=Decimal("100.01"),
    )

    assert result.state is ProviderPayoutCapAssessmentState.EXCEEDS_STRUCTURAL_CAP
    assert result.effective_headroom == Decimal("100.00")


def test_cumulative_nominal_cap_without_headroom_is_unknown() -> None:
    evidence = _seal(
        _evidence(
            scope_kind=ProviderPayoutCapScope.PER_DAY,
            maximum_amount=Decimal("1000"),
        )
    )

    result = assess_provider_payout_cap(
        evidence=evidence,
        candidate_amount=Decimal("10"),
    )

    assert result.state is ProviderPayoutCapAssessmentState.HEADROOM_UNKNOWN
    assert result.effective_headroom is None
    assert result.provider_origin_proven is False
    assert result.supports_exact_execution is False


def test_cumulative_current_headroom_not_nominal_ceiling_controls_arithmetic() -> None:
    evidence = _seal(
        _evidence(
            scope_kind=ProviderPayoutCapScope.PER_EVENT,
            maximum_amount=Decimal("1000"),
            current_headroom=Decimal("20"),
            headroom_evidence_sha256=SHA_HEADROOM,
        )
    )

    within = assess_provider_payout_cap(
        evidence=evidence,
        candidate_amount=Decimal("20"),
    )
    over = assess_provider_payout_cap(
        evidence=evidence,
        candidate_amount=Decimal("20.01"),
    )

    assert within.state is ProviderPayoutCapAssessmentState.WITHIN_STRUCTURAL_CAP
    assert over.state is ProviderPayoutCapAssessmentState.EXCEEDS_STRUCTURAL_CAP
    assert within.maximum_amount == Decimal("1000")
    assert within.effective_headroom == Decimal("20")


def test_per_bet_cap_rejects_cumulative_headroom_field() -> None:
    with pytest.raises(ProviderPayoutCapError, match="PER_BET"):
        _evidence(
            current_headroom=Decimal("50"),
            headroom_evidence_sha256=SHA_HEADROOM,
        )


def test_cumulative_headroom_requires_evidence_digest() -> None:
    with pytest.raises(
        ProviderPayoutCapError,
        match="headroom_evidence_sha256",
    ):
        _evidence(
            scope_kind=ProviderPayoutCapScope.PER_EVENT,
            current_headroom=Decimal("50"),
        )


def test_headroom_digest_without_headroom_is_rejected() -> None:
    with pytest.raises(
        ProviderPayoutCapError,
        match="requires current_headroom",
    ):
        _evidence(
            scope_kind=ProviderPayoutCapScope.PER_EVENT,
            headroom_evidence_sha256=SHA_HEADROOM,
        )


def test_headroom_cannot_exceed_nominal_maximum() -> None:
    with pytest.raises(ProviderPayoutCapError, match="cannot exceed"):
        _evidence(
            scope_kind=ProviderPayoutCapScope.PER_ACCOUNT,
            current_headroom=Decimal("100.01"),
            headroom_evidence_sha256=SHA_HEADROOM,
        )


def test_gross_return_and_net_winnings_are_distinct_evidence_identities() -> None:
    gross = _evidence(
        cap_measure=ProviderPayoutCapMeasure.GROSS_RETURN,
    )
    winnings = _evidence(
        cap_measure=ProviderPayoutCapMeasure.NET_WINNINGS,
    )

    assert gross.evidence_sha256 != winnings.evidence_sha256


def test_per_bet_and_per_event_are_distinct_evidence_identities() -> None:
    per_bet = _evidence()
    per_event = _evidence(
        scope_kind=ProviderPayoutCapScope.PER_EVENT,
    )

    assert per_bet.evidence_sha256 != per_event.evidence_sha256


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("provider_id", "provider-b"),
        ("account_id", "account-b"),
        ("adapter_id", "adapter-b"),
        ("jurisdiction", "IT"),
        ("currency", "GBP"),
    ],
)
def test_structural_seal_rejects_scope_substitution(
    field: str,
    wrong: str,
) -> None:
    evidence = _evidence()

    with pytest.raises(ProviderPayoutCapError, match="scope mismatch"):
        _seal(evidence, **{field: wrong})


def test_structural_seal_rejects_settlement_rule_substitution() -> None:
    evidence = _evidence()

    with pytest.raises(ProviderPayoutCapError, match="settlement rule"):
        _seal(evidence, settlement_rule_sha256="9" * 64)


def test_structural_seal_rejects_cap_measure_substitution() -> None:
    evidence = _evidence()

    with pytest.raises(ProviderPayoutCapError, match="cap_measure"):
        _seal(
            evidence,
            cap_measure=ProviderPayoutCapMeasure.NET_WINNINGS,
        )


def test_expired_and_future_evidence_fail_closed() -> None:
    evidence = _evidence()

    with pytest.raises(ProviderPayoutCapError, match="expired"):
        _seal(evidence, as_of=evidence.valid_until)

    with pytest.raises(ProviderPayoutCapError, match="future"):
        _seal(evidence, as_of=evidence.observed_at - timedelta(microseconds=1))


def test_action_quote_requires_and_binds_exact_action_identity() -> None:
    with pytest.raises(ProviderPayoutCapError, match="requires action"):
        _evidence(
            source_kind=ProviderPayoutCapSourceKind.AUTHENTICATED_ACTION_QUOTE,
        )

    evidence = _evidence(
        source_kind=ProviderPayoutCapSourceKind.AUTHENTICATED_ACTION_QUOTE,
        action_binding_sha256=SHA_ACTION,
    )

    with pytest.raises(ProviderPayoutCapError, match="action binding"):
        _seal(evidence, action_binding_sha256="6" * 64)


def test_copy_does_not_inherit_process_local_structural_seal() -> None:
    evidence = _seal(_evidence())
    copied = replace(evidence)

    with pytest.raises(ProviderPayoutCapError, match="lacks structural seal"):
        assess_provider_payout_cap(
            evidence=copied,
            candidate_amount=Decimal("1"),
        )


def test_unsealed_record_cannot_be_assessed() -> None:
    with pytest.raises(ProviderPayoutCapError, match="lacks structural seal"):
        assess_provider_payout_cap(
            evidence=_evidence(),
            candidate_amount=Decimal("1"),
        )


def test_fabricated_large_cap_never_becomes_provider_origin_authority() -> None:
    evidence = _evidence(
        maximum_amount=Decimal("999999"),
        source_ref="caller-claims-provider-rule",
        source_payload_sha256="a" * 64,
    )

    with pytest.raises(
        ProviderPayoutCapError,
        match="provider-origin verification requires",
    ):
        verify_provider_payout_cap_evidence(
            evidence,
            as_of=NOW,
            provider_id=evidence.provider_id,
            account_id=evidence.account_id,
            adapter_id=evidence.adapter_id,
            jurisdiction=evidence.jurisdiction,
            currency=evidence.currency,
            scope_kind=evidence.scope_kind,
            cap_measure=evidence.cap_measure,
            scope_binding_sha256=evidence.scope_binding_sha256,
            settlement_rule_sha256=evidence.settlement_rule_sha256,
        )

    _seal(evidence)
    result = assess_provider_payout_cap(
        evidence=evidence,
        candidate_amount=Decimal("500000"),
    )
    assert result.state is ProviderPayoutCapAssessmentState.WITHIN_STRUCTURAL_CAP
    assert result.provider_origin_proven is False
    assert result.supports_exact_execution is False
    assert result.no_cap_proven is False


def test_public_authoritative_assertion_always_fails_for_caller_record() -> None:
    evidence = _seal(_evidence())

    with pytest.raises(ProviderPayoutCapError, match="origin authority"):
        assert_provider_payout_cap_evidence_authoritative(evidence)


def test_float_and_non_finite_values_are_rejected() -> None:
    with pytest.raises(ProviderPayoutCapError, match="exact Decimal"):
        _evidence(maximum_amount=100.0)

    evidence = _seal(_evidence())
    with pytest.raises(ProviderPayoutCapError, match="exact Decimal"):
        assess_provider_payout_cap(
            evidence=evidence,
            candidate_amount=1.0,
        )

    with pytest.raises(ProviderPayoutCapError, match="exact Decimal"):
        assess_provider_payout_cap(
            evidence=evidence,
            candidate_amount=Decimal("NaN"),
        )


def test_zero_candidate_is_valid_structural_amount_but_not_execution_proof() -> None:
    evidence = _seal(_evidence())

    result = assess_provider_payout_cap(
        evidence=evidence,
        candidate_amount=Decimal("0"),
    )

    assert result.state is ProviderPayoutCapAssessmentState.WITHIN_STRUCTURAL_CAP
    assert result.supports_exact_execution is False


def test_execution_authority_cannot_be_set_by_caller() -> None:
    with pytest.raises(ProviderPayoutCapError, match="never grants"):
        _evidence(execution_authority=True)


def test_evidence_digest_is_independent_of_ambient_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 4
        low_precision = _evidence(
            maximum_amount=Decimal("123456789.1234500"),
        ).evidence_sha256

    with localcontext() as context:
        context.prec = 50
        high_precision = _evidence(
            maximum_amount=Decimal("123456789.1234500"),
        ).evidence_sha256

    assert low_precision == high_precision


def test_structural_seal_registry_does_not_retain_evidence_forever() -> None:
    evidence = _seal(_evidence())
    weak = ref(evidence)

    del evidence
    gc.collect()

    assert weak() is None
