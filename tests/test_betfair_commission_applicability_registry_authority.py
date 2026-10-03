from __future__ import annotations

from hashlib import sha256

import pytest

import autosport.betfair_commission_applicability as applicability


def _forge_self_consistent_negative_assessment():
    assessment_type = applicability.BetfairCommissionApplicabilityAssessment
    assessment = object.__new__(assessment_type)
    object.__setattr__(assessment, "venue_id", "betfair-exchange")
    object.__setattr__(assessment, "account_id", "caller-forged-account")
    object.__setattr__(assessment, "market_id", "1.234567890")
    object.__setattr__(assessment, "currency_code", "GBP")
    object.__setattr__(assessment, "region", "GBR")
    object.__setattr__(assessment, "fee_input_sha256", "1" * 64)
    object.__setattr__(assessment, "account_source_payload_sha256", "2" * 64)
    object.__setattr__(assessment, "market_source_payload_sha256", "3" * 64)
    object.__setattr__(
        assessment,
        "status",
        applicability.BetfairCommissionApplicabilityStatus.UNPROVEN,
    )
    object.__setattr__(assessment, "reasons", applicability._CANONICAL_REASONS)
    object.__setattr__(assessment, "ruleset_id", applicability._RULESET_ID)
    object.__setattr__(
        assessment,
        "provider_rule_sources",
        (
            applicability._PROVIDER_COMMISSION_SOURCE,
            applicability._PROVIDER_CHARGES_SOURCE,
            applicability._PROVIDER_MBR_SOURCE,
        ),
    )
    object.__setattr__(assessment, "prospective_commission_amount_authorized", False)
    object.__setattr__(assessment, "complete_execution_fee_cost_authorized", False)
    object.__setattr__(assessment, "provider_write_authorized", False)
    object.__setattr__(assessment, "real_money_execution_authorized", False)

    payload = {
        "schema": applicability._SCHEMA,
        "schema_version": applicability._SCHEMA_VERSION,
        "venue_id": assessment.venue_id,
        "account_id": assessment.account_id,
        "market_id": assessment.market_id,
        "currency_code": assessment.currency_code,
        "region": assessment.region,
        "fee_input_sha256": assessment.fee_input_sha256,
        "account_source_payload_sha256": assessment.account_source_payload_sha256,
        "market_source_payload_sha256": assessment.market_source_payload_sha256,
        "status": assessment.status.value,
        "reasons": [reason.value for reason in assessment.reasons],
        "ruleset_id": assessment.ruleset_id,
        "provider_rule_sources": list(assessment.provider_rule_sources),
        "prospective_commission_amount_authorized": False,
        "complete_execution_fee_cost_authorized": False,
        "provider_write_authorized": False,
        "real_money_execution_authorized": False,
    }
    assessment_id = sha256(applicability._canonical_json(payload)).hexdigest()
    object.__setattr__(assessment, "assessment_id", assessment_id)
    return assessment


def test_validation_has_no_mutable_registry_provenance_dependency() -> None:
    forged = _forge_self_consistent_negative_assessment()
    validator = applicability.validate_betfair_commission_applicability_assessment

    # A self-consistent caller-created value may satisfy the negative structural
    # contract. This is deliberate: validation is not an authenticity claim. The
    # important invariant is that such a value cannot mint any positive authority.
    assert validator(forged) is forged
    assert forged.prospective_commission_amount_authorized is False
    assert forged.complete_execution_fee_cost_authorized is False
    assert forged.provider_write_authorized is False
    assert forged.real_money_execution_authorized is False

    # The old diagnostic registry remains import-compatible but is explicitly
    # non-authoritative. Mutation cannot change validation in either direction.
    applicability._ISSUED_BY_ID[id(forged)] = (object(), "attacker-selected")
    try:
        assert validator(forged) is forged
    finally:
        applicability._ISSUED_BY_ID.pop(id(forged), None)

    for boundary in (
        applicability.assess_betfair_commission_applicability,
        validator,
    ):
        assert "issued_by_id" not in boundary.__code__.co_freevars
        assert "issue_lock" not in boundary.__code__.co_freevars

    # Historical callers see the same structural validator, not a hidden process-local
    # provenance checker. New code must use the explicit validate_* name.
    assert (
        applicability.require_product_betfair_commission_applicability
        is validator
    )


def test_positive_authority_cannot_be_laundered_by_registry_mutation() -> None:
    forged = _forge_self_consistent_negative_assessment()
    object.__setattr__(forged, "provider_write_authorized", True)
    applicability._ISSUED_BY_ID[id(forged)] = (object(), forged.assessment_id)
    try:
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match="exceeds the canonical fail-closed applicability boundary",
        ):
            applicability.validate_betfair_commission_applicability_assessment(forged)
    finally:
        applicability._ISSUED_BY_ID.pop(id(forged), None)


def test_module_level_issuer_bearer_cannot_mint_product_authority() -> None:
    assert applicability._ISSUE_ASSESSMENT_CAPABILITY is applicability._issue_assessment
    with pytest.raises(
        applicability.BetfairCommissionApplicabilityError,
        match="not product authority",
    ):
        applicability._ISSUE_ASSESSMENT_CAPABILITY(object())
