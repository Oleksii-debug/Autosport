from __future__ import annotations

from hashlib import sha256
from weakref import ref

import pytest

import autosport.betfair_commission_applicability as applicability


def _forge_self_consistent_assessment():
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


def test_caller_cannot_mint_product_issuance_by_mutating_reachable_registry() -> None:
    forged = _forge_self_consistent_assessment()
    object_id = id(forged)
    assert object_id not in applicability._ISSUED_BY_ID

    applicability._ISSUED_BY_ID[object_id] = (ref(forged), forged.assessment_id)
    try:
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match="product-issued",
        ):
            applicability.require_product_betfair_commission_applicability(forged)
    finally:
        applicability._ISSUED_BY_ID.pop(object_id, None)
