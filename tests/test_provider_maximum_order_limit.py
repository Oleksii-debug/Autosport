from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.provider_maximum_order_limit import (
    ProviderMaximumOrderLimitError,
    ProviderMaximumOrderLimitEvidence,
    ProviderMaximumOrderLimitKind,
    ProviderMaximumOrderLimitSourceKind,
    ProviderMaximumOrderLimitState,
    assess_provider_maximum_order_limit,
    assert_provider_maximum_order_limit_evidence_authoritative,
    assert_provider_maximum_order_limit_structure_sealed,
    seal_provider_maximum_order_limit_structure,
    verify_provider_maximum_order_limit_evidence,
)

NOW = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
SHA = "a" * 64
ACTION = "b" * 64


def evidence(**changes):
    values = dict(
        provider_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-api-v1",
        jurisdiction="INTL",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        order_family="LIMIT",
        currency="EUR",
        limit_kind=ProviderMaximumOrderLimitKind.BACK_STAKE_PER_ORDER,
        maximum_amount=Decimal("50.00"),
        observed_at=NOW - timedelta(seconds=10),
        valid_until=NOW + timedelta(seconds=50),
        source_kind=ProviderMaximumOrderLimitSourceKind.AUTHENTICATED_ACTION_QUOTE,
        source_ref="provider-limit-response-1",
        source_payload_sha256=SHA,
        action_binding_sha256=ACTION,
    )
    values.update(changes)
    return ProviderMaximumOrderLimitEvidence(**values)


def seal(item, **changes):
    values = dict(
        as_of=NOW,
        provider_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-api-v1",
        jurisdiction="INTL",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        order_family="LIMIT",
        currency="EUR",
        limit_kind=ProviderMaximumOrderLimitKind.BACK_STAKE_PER_ORDER,
        action_binding_sha256=ACTION,
    )
    values.update(changes)
    return seal_provider_maximum_order_limit_structure(item, **values)


def verify(item, **changes):
    values = dict(
        as_of=NOW,
        provider_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-api-v1",
        jurisdiction="INTL",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        order_family="LIMIT",
        currency="EUR",
        limit_kind=ProviderMaximumOrderLimitKind.BACK_STAKE_PER_ORDER,
        action_binding_sha256=ACTION,
    )
    values.update(changes)
    return verify_provider_maximum_order_limit_evidence(item, **values)


def test_unsealed_caller_record_cannot_be_assessed():
    item = evidence()
    with pytest.raises(ProviderMaximumOrderLimitError, match="lacks structural seal"):
        assess_provider_maximum_order_limit(\n            as_of=NOW, evidence=item, requested_amount=Decimal("1")\n        )


def test_structural_exact_boundary_is_numerically_within_but_not_provider_support():
    item = seal(evidence())
    result = assess_provider_maximum_order_limit(
        as_of=NOW,
        evidence=item, requested_amount=Decimal("50.00")
    )
    assert result.state is ProviderMaximumOrderLimitState.WITHIN_LIMIT
    assert result.maximum_amount == Decimal("50.00")
    assert result.provider_origin_proven is False
    assert result.supports_requested_amount is False
    assert result.reason == "requested_amount_within_structural_maximum_only"
    assert result.execution_authority is False


def test_structural_one_quantum_above_is_numerically_exceeds_but_not_provider_truth():
    item = seal(evidence())
    result = assess_provider_maximum_order_limit(
        as_of=NOW,
        evidence=item, requested_amount=Decimal("50.01")
    )
    assert result.state is ProviderMaximumOrderLimitState.EXCEEDS_LIMIT
    assert result.provider_origin_proven is False
    assert result.supports_requested_amount is False
    assert result.reason == "requested_amount_exceeds_structural_maximum_only"


def test_dataclass_replace_cannot_copy_structural_seal():
    item = seal(evidence())
    copied = replace(item, maximum_amount=Decimal("500"))
    with pytest.raises(ProviderMaximumOrderLimitError, match="lacks structural seal"):
        assert_provider_maximum_order_limit_structure_sealed(copied)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("provider_id", "other", "provider_id scope mismatch"),
        ("account_id", "acct-2", "account_id scope mismatch"),
        ("market_id", "market-2", "market_id scope mismatch"),
        ("selection_id", "selection-2", "selection_id scope mismatch"),
        ("side", "LAY", "side scope mismatch"),
        ("currency", "GBP", "currency scope mismatch"),
    ],
)
def test_scope_substitution_fails_closed(field, replacement, message):
    item = evidence(**{field: replacement})
    with pytest.raises(ProviderMaximumOrderLimitError, match=message):
        seal(item)


def test_back_stake_cannot_be_sealed_as_lay_liability():
    item = evidence()
    with pytest.raises(ProviderMaximumOrderLimitError, match="limit_kind scope mismatch"):
        seal(
            item,
            limit_kind=ProviderMaximumOrderLimitKind.LAY_LIABILITY_PER_ORDER,
        )


def test_dynamic_action_limit_cannot_cross_action_binding():
    item = evidence()
    with pytest.raises(ProviderMaximumOrderLimitError, match="action binding scope mismatch"):
        seal(item, action_binding_sha256="c" * 64)


def test_future_evidence_fails_closed():
    item = evidence(
        observed_at=NOW + timedelta(seconds=1),
        valid_until=NOW + timedelta(seconds=30),
    )
    with pytest.raises(ProviderMaximumOrderLimitError, match="future"):
        seal(item)


def test_expired_evidence_fails_closed_at_exact_boundary():
    item = evidence(observed_at=NOW - timedelta(seconds=60), valid_until=NOW)
    with pytest.raises(ProviderMaximumOrderLimitError, match="expired"):
        seal(item)


def test_structural_seal_does_not_keep_evidence_active_after_expiry():
    item = seal(
        evidence(
            observed_at=NOW - timedelta(seconds=10),
            valid_until=NOW + timedelta(seconds=1),
        )
    )
    with pytest.raises(
        ProviderMaximumOrderLimitError,
        match="expired at assessment",
    ):
        assess_provider_maximum_order_limit(
            as_of=NOW + timedelta(seconds=1),
            evidence=item,
            requested_amount=Decimal("10"),
        )


def test_assessment_cannot_backdate_evidence_before_observation():
    item = seal(evidence())
    with pytest.raises(
        ProviderMaximumOrderLimitError,
        match="future at assessment",
    ):
        assess_provider_maximum_order_limit(
            as_of=NOW - timedelta(seconds=11),
            evidence=item,
            requested_amount=Decimal("10"),
        )


def test_action_quote_requires_exact_action_binding():
    with pytest.raises(ProviderMaximumOrderLimitError, match="requires action_binding_sha256"):
        evidence(action_binding_sha256=None)


def test_versioned_rule_may_be_structurally_scope_bound_without_action_digest():
    item = evidence(
        source_kind=ProviderMaximumOrderLimitSourceKind.VERSIONED_PROVIDER_RULE,
        action_binding_sha256=None,
    )
    sealed = seal(item, action_binding_sha256=None)
    result = assess_provider_maximum_order_limit(
        as_of=NOW,
        evidence=sealed, requested_amount=Decimal("10")
    )
    assert result.state is ProviderMaximumOrderLimitState.WITHIN_LIMIT
    assert result.provider_origin_proven is False
    assert result.supports_requested_amount is False


def test_float_bool_and_nonpositive_money_are_rejected():
    for value in (50.0, True, Decimal("0"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ProviderMaximumOrderLimitError):
            evidence(maximum_amount=value)
    item = seal(evidence())
    for value in (1.0, True, Decimal("0")):
        with pytest.raises(ProviderMaximumOrderLimitError):
            assess_provider_maximum_order_limit(\n                as_of=NOW, evidence=item, requested_amount=value\n            )


def test_source_hash_must_be_canonical_sha256():
    with pytest.raises(ProviderMaximumOrderLimitError, match="lowercase SHA-256"):
        evidence(source_payload_sha256="A" * 64)


def test_equivalent_decimal_aliases_have_same_evidence_identity():
    one = evidence(maximum_amount=Decimal("50.0"))
    two = evidence(maximum_amount=Decimal("50.000"))
    assert one.evidence_sha256 == two.evidence_sha256


def test_reconstructed_restart_record_has_same_identity_but_not_runtime_seal():
    original = seal(evidence())
    reconstructed = evidence(maximum_amount=Decimal("50.000"))
    assert reconstructed.evidence_sha256 == original.evidence_sha256
    with pytest.raises(ProviderMaximumOrderLimitError, match="lacks structural seal"):
        assess_provider_maximum_order_limit(
            as_of=NOW,
            evidence=reconstructed, requested_amount=Decimal("10")
        )


def test_execution_authority_cannot_be_promoted():
    with pytest.raises(ProviderMaximumOrderLimitError, match="never grants execution"):
        evidence(execution_authority=True)


def test_public_verifier_cannot_turn_caller_dto_into_provider_origin_truth():
    item = evidence(maximum_amount=Decimal("999999"))
    with pytest.raises(
        ProviderMaximumOrderLimitError,
        match="provider-origin verification requires independent product-owned",
    ):
        verify(item)


def test_fabricated_maximum_can_be_structurally_compared_but_never_supports_request():
    item = seal(evidence(maximum_amount=Decimal("999999")))
    result = assess_provider_maximum_order_limit(
        as_of=NOW,
        evidence=item, requested_amount=Decimal("500000")
    )
    assert result.state is ProviderMaximumOrderLimitState.WITHIN_LIMIT
    assert result.provider_origin_proven is False
    assert result.supports_requested_amount is False
    assert result.reason == "requested_amount_within_structural_maximum_only"
    with pytest.raises(
        ProviderMaximumOrderLimitError,
        match="origin authority is not proven",
    ):
        assert_provider_maximum_order_limit_evidence_authoritative(item)
