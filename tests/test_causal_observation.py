from __future__ import annotations

from dataclasses import replace

import pytest

from autosport.causal_observation import (
    CausalEligibility,
    CausalObservationEnvelope,
    EntitlementGrant,
    EntitlementPermission,
    EntitlementVerification,
    EvidenceUse,
    ObservationLatencyClass,
    ProviderEntitlementProfile,
    evaluate_causal_eligibility,
)


DOC_SHA = "1" * 64
PAYLOAD_SHA = "2" * 64


def _profile(
    *,
    verification: EntitlementVerification = EntitlementVerification.VERIFIED_DOCUMENT_BOUND,
    research: EntitlementPermission = EntitlementPermission.ALLOWED,
    forward: EntitlementPermission = EntitlementPermission.ALLOWED,
    live: EntitlementPermission = EntitlementPermission.ALLOWED,
) -> ProviderEntitlementProfile:
    return ProviderEntitlementProfile(
        profile_id="sportradar-tt-contract-2026",
        provider_id="sportradar",
        product_id="table-tennis-api",
        access_level="licensed-test-profile",
        grants=(
            EntitlementGrant(EvidenceUse.INTERNAL_RESEARCH, research),
            EntitlementGrant(EvidenceUse.FORWARD_ECONOMIC_PROOF, forward),
            EntitlementGrant(EvidenceUse.LIVE_EXECUTION, live),
        ),
        retention_constraints=("contract-governed",),
        redistribution_constraints=("no-public-redistribution",),
        jurisdiction_account_prerequisites=("account-in-good-standing",),
        rate_quota_class="contract-plan",
        effective_from="2026-01-01T00:00:00+00:00",
        effective_to="2026-12-31T23:59:59+00:00",
        source_document_identity="order-form:test-fixture",
        source_document_sha256=DOC_SHA,
        verification=verification,
    )


def _observation(
    *,
    observation_id: str = "obs-1",
    received_at: str = "2026-09-22T12:00:01+00:00",
    published_at: str | None = "2026-09-22T12:00:00+00:00",
    latency: ObservationLatencyClass = ObservationLatencyClass.LIVE,
    ordinal: int = 1,
    payload_sha: str = PAYLOAD_SHA,
    mapping_version: str | None = None,
    mapping_observed_at: str | None = None,
    correction_of: str | None = None,
) -> CausalObservationEnvelope:
    return CausalObservationEnvelope(
        observation_id=observation_id,
        provider_id="sportradar",
        provider_event_id="sr:match:100",
        market_id="match-winner",
        subject_id="sr:competitor:7",
        endpoint_or_feed="table-tennis/live-summaries",
        provider_schema_version="v1",
        provider_published_at=published_at,
        received_at_utc=received_at,
        receipt_clock_id="ingest-process-42",
        receipt_ordinal=ordinal,
        provider_revision_or_market_version="rev-5",
        latency_class=latency,
        payload_sha256=payload_sha,
        raw_artifact_ref="artifact://provider/obs-1.json",
        entitlement_profile_id="sportradar-tt-contract-2026",
        identity_mapping_version=mapping_version,
        mapping_observed_at=mapping_observed_at,
        correction_of_observation_id=correction_of,
    )


def _decision(
    observation: CausalObservationEnvelope,
    *,
    use: EvidenceUse = EvidenceUse.FORWARD_ECONOMIC_PROOF,
    profile: ProviderEntitlementProfile | None = None,
    cutoff: str = "2026-09-22T12:00:02+00:00",
):
    return evaluate_causal_eligibility(
        observation,
        entitlement=profile or _profile(),
        use=use,
        decision_cutoff_utc=cutoff,
    )


def test_live_observation_with_document_bound_permission_is_eligible() -> None:
    decision = _decision(_observation())
    assert decision.eligibility is CausalEligibility.ELIGIBLE
    assert decision.reason == "ELIGIBLE"
    assert decision.quarantined is False


def test_later_mapping_cannot_rewrite_earlier_observation_identity() -> None:
    original = _observation()
    later_mapping = _observation(
        observation_id="obs-late-map",
        mapping_version="merge-map-2",
        mapping_observed_at="2026-09-22T12:05:00+00:00",
    )
    decision = _decision(later_mapping, cutoff="2026-09-22T12:10:00+00:00")
    assert decision.eligibility is CausalEligibility.UNKNOWN
    assert decision.reason == "IDENTITY_MAPPING_OBSERVED_AFTER_RECEIPT"
    assert decision.quarantined is True
    assert original.canonical_sha256 != later_mapping.canonical_sha256


def test_late_correction_is_new_evidence_not_backdated_into_decision() -> None:
    original = _observation()
    original_decision = _decision(original)
    correction = _observation(
        observation_id="obs-2",
        received_at="2026-09-22T12:30:00+00:00",
        published_at="2026-09-22T12:29:59+00:00",
        ordinal=2,
        payload_sha="3" * 64,
        correction_of="obs-1",
    )
    correction_decision = _decision(correction)
    assert original_decision.eligibility is CausalEligibility.ELIGIBLE
    assert correction_decision.eligibility is CausalEligibility.INELIGIBLE
    assert correction_decision.reason == "RECEIVED_AFTER_DECISION_CUTOFF"
    assert correction.correction_of_observation_id == original.observation_id
    assert correction.canonical_sha256 != original.canonical_sha256


def test_delayed_observation_cannot_be_live_execution_evidence() -> None:
    decision = _decision(
        _observation(latency=ObservationLatencyClass.DELAYED),
        use=EvidenceUse.LIVE_EXECUTION,
    )
    assert decision.eligibility is CausalEligibility.INELIGIBLE
    assert decision.reason == "DELAYED_NOT_LIVE_EXECUTION_EVIDENCE"


def test_declared_delay_can_remain_forward_causal_with_explicit_label() -> None:
    decision = _decision(_observation(latency=ObservationLatencyClass.DELAYED))
    assert decision.eligibility is CausalEligibility.ELIGIBLE
    assert decision.reason == "ELIGIBLE_WITH_DECLARED_DELAY"


def test_historical_data_is_not_forward_economic_evidence() -> None:
    observation = _observation(
        latency=ObservationLatencyClass.HISTORICAL,
        received_at="2026-09-22T12:00:01+00:00",
        published_at="2026-01-01T00:00:00+00:00",
    )
    decision = _decision(observation)
    assert decision.eligibility is CausalEligibility.INELIGIBLE
    assert decision.reason == "HISTORICAL_NOT_FORWARD_ECONOMIC_EVIDENCE"


def test_historical_data_can_be_internal_research_when_rights_allow() -> None:
    decision = _decision(
        _observation(latency=ObservationLatencyClass.HISTORICAL),
        use=EvidenceUse.INTERNAL_RESEARCH,
    )
    assert decision.eligibility is CausalEligibility.ELIGIBLE
    assert decision.reason == "ELIGIBLE_HISTORICAL_RESEARCH_ONLY"


def test_observation_received_after_cutoff_cannot_be_backdated_by_provider_timestamp() -> None:
    observation = _observation(
        received_at="2026-09-22T13:00:00+00:00",
        published_at="2026-09-22T11:00:00+00:00",
    )
    decision = _decision(observation)
    assert decision.eligibility is CausalEligibility.INELIGIBLE
    assert decision.reason == "RECEIVED_AFTER_DECISION_CUTOFF"


def test_unverified_entitlement_fails_closed_for_forward_proof() -> None:
    decision = _decision(
        _observation(),
        profile=_profile(verification=EntitlementVerification.UNVERIFIED),
    )
    assert decision.eligibility is CausalEligibility.UNKNOWN
    assert decision.reason == "ENTITLEMENT_UNVERIFIED"


@pytest.mark.parametrize(
    ("permission", "expected", "reason"),
    [
        (
            EntitlementPermission.UNKNOWN,
            CausalEligibility.UNKNOWN,
            "ENTITLEMENT_USE_UNKNOWN",
        ),
        (
            EntitlementPermission.DENIED,
            CausalEligibility.INELIGIBLE,
            "ENTITLEMENT_DENIES_USE",
        ),
    ],
)
def test_unknown_or_denied_use_permission_never_upgrades_to_positive(
    permission: EntitlementPermission,
    expected: CausalEligibility,
    reason: str,
) -> None:
    decision = _decision(_observation(), profile=_profile(forward=permission))
    assert decision.eligibility is expected
    assert decision.reason == reason


def test_duplicate_payload_identity_preserves_distinct_receipt_chronology() -> None:
    first = _observation(observation_id="obs-a", ordinal=10)
    replay = _observation(
        observation_id="obs-b",
        received_at="2026-09-22T12:00:01.500000+00:00",
        ordinal=11,
    )
    assert first.payload_identity_sha256 == replay.payload_identity_sha256
    assert first.canonical_sha256 != replay.canonical_sha256


def test_provider_publish_time_after_receipt_is_quarantined_not_normalized() -> None:
    decision = _decision(
        _observation(published_at="2026-09-22T12:00:02+00:00")
    )
    assert decision.eligibility is CausalEligibility.UNKNOWN
    assert decision.reason == "PROVIDER_PUBLISHED_AFTER_RECEIPT"
    assert decision.quarantined is True


def test_unknown_latency_fails_closed() -> None:
    decision = _decision(_observation(latency=ObservationLatencyClass.UNKNOWN))
    assert decision.eligibility is CausalEligibility.UNKNOWN
    assert decision.reason == "LATENCY_CLASS_UNKNOWN"


def test_entitlement_profile_identity_is_bound_to_observation() -> None:
    profile = replace(_profile(), profile_id="other-profile")
    decision = _decision(_observation(), profile=profile)
    assert decision.eligibility is CausalEligibility.INELIGIBLE
    assert decision.reason == "ENTITLEMENT_PROFILE_MISMATCH"


def test_entitlement_provider_mismatch_fails_closed() -> None:
    profile = replace(_profile(), provider_id="betfair")
    decision = _decision(_observation(), profile=profile)
    assert decision.eligibility is CausalEligibility.INELIGIBLE
    assert decision.reason == "ENTITLEMENT_PROVIDER_MISMATCH"


def test_expired_profile_cannot_authorize_later_observation() -> None:
    profile = replace(_profile(), effective_to="2026-09-21T23:59:59+00:00")
    decision = _decision(_observation(), profile=profile)
    assert decision.eligibility is CausalEligibility.UNKNOWN
    assert decision.reason == "ENTITLEMENT_EXPIRED"


def test_profile_requires_explicit_grant_for_every_use() -> None:
    with pytest.raises(ValueError, match="explicitly cover every EvidenceUse"):
        replace(_profile(), grants=(EntitlementGrant(
            EvidenceUse.INTERNAL_RESEARCH,
            EntitlementPermission.ALLOWED,
        ),))


def test_profile_hash_is_independent_of_grant_tuple_order() -> None:
    profile = _profile()
    reordered = replace(profile, grants=tuple(reversed(profile.grants)))
    assert profile.canonical_sha256 == reordered.canonical_sha256


def test_observation_hash_is_deterministic_and_changes_with_receipt_order() -> None:
    observation = _observation()
    assert observation.canonical_sha256 == _observation().canonical_sha256
    assert observation.canonical_sha256 != _observation(ordinal=2).canonical_sha256


@pytest.mark.parametrize(
    "timestamp_field",
    ["received_at_utc", "provider_published_at", "mapping_observed_at"],
)
def test_naive_timestamps_are_rejected(timestamp_field: str) -> None:
    kwargs = {}
    if timestamp_field == "received_at_utc":
        kwargs["received_at"] = "2026-09-22T12:00:01"
    elif timestamp_field == "provider_published_at":
        kwargs["published_at"] = "2026-09-22T12:00:00"
    else:
        kwargs["mapping_version"] = "map-1"
        kwargs["mapping_observed_at"] = "2026-09-22T12:00:00"
    with pytest.raises(ValueError, match="timezone-aware"):
        _observation(**kwargs)


def test_mapping_version_and_observed_time_are_atomic_pair() -> None:
    with pytest.raises(ValueError, match="must be present together"):
        _observation(mapping_version="map-1")


def test_self_correction_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot correct itself"):
        _observation(correction_of="obs-1")


def test_boolean_receipt_ordinal_is_rejected() -> None:
    observation = _observation()
    with pytest.raises(ValueError, match="non-negative non-boolean int"):
        replace(observation, receipt_ordinal=True)


def test_profile_and_decision_hashes_are_lowercase_sha256() -> None:
    profile = _profile()
    observation = _observation()
    decision = _decision(observation, profile=profile)
    for value in (
        profile.canonical_sha256,
        observation.payload_identity_sha256,
        observation.canonical_sha256,
        decision.observation_sha256,
        decision.entitlement_profile_sha256,
    ):
        assert len(value) == 64
        assert value == value.lower()
        int(value, 16)
