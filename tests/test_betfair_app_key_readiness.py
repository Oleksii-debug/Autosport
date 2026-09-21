from dataclasses import fields, replace
import json

import pytest

from autosport.betfair_app_key_readiness import (
    BetfairAppKeyPurposeAssessment,
    BetfairAppKeyPurposeEvidence,
    BetfairAppKeyReadinessError,
    BetfairApplicationKeyClass,
    BetfairDistributionMode,
    BetfairKeyPurposeState,
    BetfairLicencePath,
    BetfairUsageIntent,
    assess_betfair_app_key_purpose,
)


_TS = "2026-09-21T18:30:00+00:00"
_HASH = "a" * 64


def _evidence(**overrides) -> BetfairAppKeyPurposeEvidence:
    values = {
        "key_class": BetfairApplicationKeyClass.DELAYED,
        "usage_intent": BetfairUsageIntent.READ_ONLY,
        "distribution_mode": BetfairDistributionMode.PERSONAL,
        "licence_path": BetfairLicencePath.NOT_PROVEN,
        "observed_at": _TS,
        "evidence_manifest_sha256": _HASH,
    }
    values.update(overrides)
    return BetfairAppKeyPurposeEvidence(**values)


def test_live_key_with_read_only_intent_fails_closed() -> None:
    assessment = assess_betfair_app_key_purpose(
        _evidence(
            key_class=BetfairApplicationKeyClass.LIVE,
            usage_intent=BetfairUsageIntent.READ_ONLY,
        )
    )

    assert assessment.state is BetfairKeyPurposeState.READINESS_BLOCKED
    assert assessment.key_purpose_gate_passed is False
    assert assessment.development_read_only_supported is False
    assert assessment.live_betting_key_purpose_compatible is False
    assert "LIVE_KEY_READ_ONLY_PURPOSE_CONFLICT" in assessment.reasons
    assert assessment.realtime_price_authority_granted is False
    assert assessment.provider_write_authority_granted is False
    assert assessment.execution_authority_granted is False


def test_delayed_read_only_is_development_only_even_on_production_exchange() -> None:
    assessment = assess_betfair_app_key_purpose(_evidence())

    assert assessment.production_exchange_endpoint is True
    assert assessment.state is BetfairKeyPurposeState.READ_ONLY_DEVELOPMENT
    assert assessment.key_purpose_gate_passed is True
    assert assessment.development_read_only_supported is True
    assert assessment.live_betting_key_purpose_compatible is False
    assert assessment.realtime_price_authority_granted is False
    assert "DELAYED_KEY_HAS_NO_REALTIME_PRICE_AUTHORITY" in assessment.reasons


def test_live_betting_key_purpose_never_grants_execution_or_whole_product_readiness() -> None:
    assessment = assess_betfair_app_key_purpose(
        _evidence(
            key_class=BetfairApplicationKeyClass.LIVE,
            usage_intent=BetfairUsageIntent.BETTING,
            licence_path=BetfairLicencePath.PERSONAL_BETTING,
        )
    )

    assert assessment.state is BetfairKeyPurposeState.BETTING_KEY_PURPOSE_COMPATIBLE
    assert assessment.key_purpose_gate_passed is True
    assert assessment.live_betting_key_purpose_compatible is True
    assert assessment.realtime_price_authority_granted is False
    assert assessment.provider_write_authority_granted is False
    assert assessment.execution_authority_granted is False
    assert assessment.whole_product_readiness_granted is False
    assert assessment.reasons == ("KEY_PURPOSE_EVIDENCE_GRANTS_NO_EXECUTION_AUTHORITY",)


def test_live_betting_requires_explicit_personal_betting_licence_path() -> None:
    assessment = assess_betfair_app_key_purpose(
        _evidence(
            key_class=BetfairApplicationKeyClass.LIVE,
            usage_intent=BetfairUsageIntent.BETTING,
            licence_path=BetfairLicencePath.NOT_PROVEN,
        )
    )

    assert assessment.state is BetfairKeyPurposeState.READINESS_BLOCKED
    assert assessment.live_betting_key_purpose_compatible is False
    assert "PERSONAL_BETTING_LICENCE_REQUIRED" in assessment.reasons


def test_distributed_software_requires_software_vendor_licence_path() -> None:
    insufficient = assess_betfair_app_key_purpose(
        _evidence(
            distribution_mode=BetfairDistributionMode.DISTRIBUTED_SOFTWARE,
            licence_path=BetfairLicencePath.PERSONAL_BETTING,
        )
    )
    qualified_development = assess_betfair_app_key_purpose(
        _evidence(
            distribution_mode=BetfairDistributionMode.DISTRIBUTED_SOFTWARE,
            licence_path=BetfairLicencePath.SOFTWARE_VENDOR,
        )
    )

    assert insufficient.state is BetfairKeyPurposeState.READINESS_BLOCKED
    assert "SOFTWARE_VENDOR_LICENCE_REQUIRED" in insufficient.reasons
    assert qualified_development.state is BetfairKeyPurposeState.READ_ONLY_DEVELOPMENT
    assert qualified_development.realtime_price_authority_granted is False


def test_delayed_key_cannot_qualify_live_betting_readiness() -> None:
    assessment = assess_betfair_app_key_purpose(
        _evidence(
            key_class=BetfairApplicationKeyClass.DELAYED,
            usage_intent=BetfairUsageIntent.BETTING,
            licence_path=BetfairLicencePath.PERSONAL_BETTING,
        )
    )

    assert assessment.state is BetfairKeyPurposeState.READINESS_BLOCKED
    assert assessment.key_purpose_gate_passed is False
    assert "DELAYED_KEY_CANNOT_QUALIFY_LIVE_BETTING_READINESS" in assessment.reasons


def test_configuration_drift_invalidates_prior_assessment_until_reresolved() -> None:
    evidence = _evidence()
    assessment = assess_betfair_app_key_purpose(evidence)

    assessment.verify_current(replace(evidence, observed_at="2026-09-21T19:00:00+00:00"))

    for changed in (
        replace(evidence, key_class=BetfairApplicationKeyClass.LIVE),
        replace(evidence, usage_intent=BetfairUsageIntent.BETTING),
        replace(evidence, distribution_mode=BetfairDistributionMode.DISTRIBUTED_SOFTWARE),
        replace(evidence, licence_path=BetfairLicencePath.PERSONAL_BETTING),
    ):
        with pytest.raises(BetfairAppKeyReadinessError, match="must be re-resolved"):
            assessment.verify_current(changed)


def test_personal_evidence_cannot_be_relabelled_as_software_vendor_without_identity_change() -> None:
    personal = _evidence(
        key_class=BetfairApplicationKeyClass.LIVE,
        usage_intent=BetfairUsageIntent.BETTING,
        licence_path=BetfairLicencePath.PERSONAL_BETTING,
    )
    vendor = replace(
        personal,
        distribution_mode=BetfairDistributionMode.DISTRIBUTED_SOFTWARE,
        licence_path=BetfairLicencePath.SOFTWARE_VENDOR,
    )

    assert personal.configuration_id != vendor.configuration_id
    with pytest.raises(BetfairAppKeyReadinessError, match="must be re-resolved"):
        assess_betfair_app_key_purpose(personal).verify_current(vendor)


def test_durable_shapes_contain_no_raw_credential_fields_or_authority_flags() -> None:
    evidence_names = {field.name for field in fields(BetfairAppKeyPurposeEvidence)}
    assessment_names = {field.name for field in fields(BetfairAppKeyPurposeAssessment)}

    assert "application_key" not in evidence_names
    assert "session_token" not in evidence_names
    assert "credentials" not in evidence_names
    assert assessment_names == {"evidence"}

    payload = json.dumps(_evidence().to_canonical_dict(), sort_keys=True)
    assert "application_key" not in payload
    assert "session_token" not in payload
    assert "credentials" not in payload


def test_evidence_and_projection_are_deterministic_and_non_secret() -> None:
    evidence = _evidence()
    copied = replace(evidence)
    assessment = assess_betfair_app_key_purpose(evidence)

    assert evidence.configuration_id == copied.configuration_id
    assert evidence.evidence_id == copied.evidence_id
    assert assessment.to_canonical_dict() == assess_betfair_app_key_purpose(copied).to_canonical_dict()
    assert assessment.to_canonical_dict()["execution_authority_granted"] is False
    assert assessment.to_canonical_dict()["whole_product_readiness_granted"] is False


def test_malformed_evidence_fails_closed() -> None:
    with pytest.raises(BetfairAppKeyReadinessError, match="key_class"):
        _evidence(key_class="delayed")
    with pytest.raises(BetfairAppKeyReadinessError, match="usage_intent"):
        _evidence(usage_intent="read_only")
    with pytest.raises(BetfairAppKeyReadinessError, match="distribution_mode"):
        _evidence(distribution_mode="personal")
    with pytest.raises(BetfairAppKeyReadinessError, match="licence_path"):
        _evidence(licence_path="not_proven")
    with pytest.raises(BetfairAppKeyReadinessError, match="timezone"):
        _evidence(observed_at="2026-09-21T18:30:00")
    with pytest.raises(BetfairAppKeyReadinessError, match="SHA-256"):
        _evidence(evidence_manifest_sha256="not-a-hash")
    with pytest.raises(BetfairAppKeyReadinessError, match="schema_version"):
        _evidence(schema_version=2)
