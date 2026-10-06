from dataclasses import fields, replace

import pytest

from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationEvidence,
    BookmakerIntegrationKind,
)
from autosport.betfair_distribution_policy import (
    BetfairDistributionPolicyError,
    BetfairUseMode,
    DataRedistributionState,
    VendorCertificationState,
    bind_betfair_distribution_policy,
)


_CHECKED = "2026-09-21T13:30:00+00:00"
_TERMS_SHA = "c" * 64
_AUTH_SHA = "d" * 64


def _integration(**overrides) -> BookmakerIntegrationEvidence:
    values = {
        "venue_id": "betfair-exchange",
        "adapter_id": "betfair-api-ng",
        "adapter_version": "1.0",
        "profile_id": "a" * 64,
        "integration_kind": BookmakerIntegrationKind.OFFICIAL_API,
        "observed_at": "2026-09-21T13:00:00+00:00",
        "source_ref": "autosport-betfair-adapter",
        "source_payload_sha256": "b" * 64,
    }
    values.update(overrides)
    return BookmakerIntegrationEvidence(**values)


def _bind(integration=None, **overrides):
    integration = integration or _integration()
    values = {
        "use_mode": BetfairUseMode.UNPROVEN,
        "distribution_to_other_betfair_customers": False,
        "vendor_certification_state": VendorCertificationState.UNPROVEN,
        "data_redistribution_state": DataRedistributionState.UNPROVEN,
        "checked_at": _CHECKED,
        "provider_terms_evidence_ref": "betfair-developer-program-licensing",
        "provider_terms_evidence_sha256": _TERMS_SHA,
    }
    values.update(overrides)
    return bind_betfair_distribution_policy(integration, **values)


def test_unproven_policy_binds_exact_integration_and_never_grants_authority() -> None:
    integration = _integration()
    policy = _bind(integration)

    policy.verify_integration(integration)
    assert len(policy.evidence_id) == 64
    assert policy.provider_permission_authorized is False
    assert policy.provider_write_authorized is False
    assert policy.execution_authorized is False
    assert policy.real_money_authorized is False


def test_personal_private_mode_rejects_distribution_or_redistribution_claims() -> None:
    base = {
        "use_mode": BetfairUseMode.PERSONAL_PRIVATE,
        "vendor_certification_state": VendorCertificationState.NOT_APPLICABLE,
    }
    policy = _bind(**base)
    assert policy.distribution_to_other_betfair_customers is False

    with pytest.raises(BetfairDistributionPolicyError, match="personal-private"):
        _bind(**base, distribution_to_other_betfair_customers=True)
    with pytest.raises(BetfairDistributionPolicyError, match="personal-private"):
        _bind(
            **base,
            data_redistribution_state=DataRedistributionState.DOCUMENTED,
            external_authorization_ref="external-data-licence",
            external_authorization_sha256=_AUTH_SHA,
        )


def test_software_vendor_distribution_requires_documented_certification() -> None:
    base = {
        "use_mode": BetfairUseMode.SOFTWARE_VENDOR,
        "data_redistribution_state": DataRedistributionState.UNPROVEN,
    }
    for state in (
        VendorCertificationState.UNPROVEN,
        VendorCertificationState.IN_PROGRESS,
    ):
        with pytest.raises(BetfairDistributionPolicyError, match="requires documented"):
            _bind(
                **base,
                distribution_to_other_betfair_customers=True,
                vendor_certification_state=state,
            )

    policy = _bind(
        **base,
        distribution_to_other_betfair_customers=True,
        vendor_certification_state=VendorCertificationState.DOCUMENTED,
        external_authorization_ref="betfair-vendor-certification-record",
        external_authorization_sha256=_AUTH_SHA,
    )
    assert policy.vendor_certification_state is VendorCertificationState.DOCUMENTED
    assert policy.provider_permission_authorized is False


def test_software_vendor_mode_does_not_substitute_for_data_redistribution() -> None:
    with pytest.raises(BetfairDistributionPolicyError, match="cannot substitute"):
        _bind(
            use_mode=BetfairUseMode.SOFTWARE_VENDOR,
            distribution_to_other_betfair_customers=False,
            vendor_certification_state=VendorCertificationState.UNPROVEN,
            data_redistribution_state=DataRedistributionState.DOCUMENTED,
            external_authorization_ref="data-licence",
            external_authorization_sha256=_AUTH_SHA,
        )


def test_commercial_data_documentation_is_separate_from_vendor_distribution() -> None:
    policy = _bind(
        use_mode=BetfairUseMode.COMMERCIAL_DATA,
        distribution_to_other_betfair_customers=False,
        vendor_certification_state=VendorCertificationState.NOT_APPLICABLE,
        data_redistribution_state=DataRedistributionState.DOCUMENTED,
        external_authorization_ref="commercial-data-licence-record",
        external_authorization_sha256=_AUTH_SHA,
    )
    assert policy.data_redistribution_state is DataRedistributionState.DOCUMENTED
    assert policy.provider_permission_authorized is False

    with pytest.raises(BetfairDistributionPolicyError, match="cannot substitute"):
        _bind(
            use_mode=BetfairUseMode.COMMERCIAL_DATA,
            distribution_to_other_betfair_customers=True,
            vendor_certification_state=VendorCertificationState.NOT_APPLICABLE,
            data_redistribution_state=DataRedistributionState.UNPROVEN,
        )


def test_documented_external_state_requires_reference_and_digest_pair() -> None:
    with pytest.raises(BetfairDistributionPolicyError, match="must be supplied together"):
        _bind(
            use_mode=BetfairUseMode.SOFTWARE_VENDOR,
            vendor_certification_state=VendorCertificationState.DOCUMENTED,
            external_authorization_ref="vendor-record",
        )
    with pytest.raises(BetfairDistributionPolicyError, match="requires authorization"):
        _bind(
            use_mode=BetfairUseMode.SOFTWARE_VENDOR,
            vendor_certification_state=VendorCertificationState.DOCUMENTED,
        )
    with pytest.raises(BetfairDistributionPolicyError, match="allowed only"):
        _bind(
            external_authorization_ref="unrelated-record",
            external_authorization_sha256=_AUTH_SHA,
        )


def test_unproven_mode_cannot_self_promote_external_states() -> None:
    with pytest.raises(BetfairDistributionPolicyError, match="unproven use mode"):
        _bind(
            vendor_certification_state=VendorCertificationState.IN_PROGRESS,
        )


def test_policy_rejects_different_integration_identity() -> None:
    integration = _integration()
    policy = _bind(integration)
    changed = replace(integration, source_payload_sha256="e" * 64)

    with pytest.raises(BetfairDistributionPolicyError, match="does not match"):
        policy.verify_integration(changed)


def test_evidence_identity_is_deterministic_and_content_bound() -> None:
    policy = _bind()
    same = replace(policy)
    changed = replace(
        policy,
        provider_terms_evidence_ref="betfair-developer-program-licensing-revision-2",
    )

    assert same.evidence_id == policy.evidence_id
    assert changed.evidence_id != policy.evidence_id


def test_malformed_policy_metadata_fails_closed() -> None:
    with pytest.raises(BetfairDistributionPolicyError, match="use_mode"):
        _bind(use_mode="unproven")
    with pytest.raises(BetfairDistributionPolicyError, match="must be bool"):
        _bind(distribution_to_other_betfair_customers=0)
    with pytest.raises(BetfairDistributionPolicyError, match="timezone"):
        _bind(checked_at="2026-09-21T13:30:00")
    with pytest.raises(BetfairDistributionPolicyError, match="SHA-256"):
        _bind(provider_terms_evidence_sha256="not-a-digest")
    with pytest.raises(BetfairDistributionPolicyError, match="schema_version"):
        replace(_bind(), schema_version=2)


def test_contract_has_no_secret_or_execution_authority_fields() -> None:
    names = {field.name for field in fields(type(_bind()))}
    forbidden_fragments = {
        "password",
        "secret",
        "token",
        "cookie",
        "credential",
        "stake",
        "order",
        "session",
        "real_money",
    }
    assert not {
        name
        for name in names
        if any(fragment in name for fragment in forbidden_fragments)
    }
