import json

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_capability_lifecycle import (
    CapabilityAvailability,
    CapabilityAvailabilityState,
    CapabilityEvidence,
    CapabilityEvidenceError,
    CapabilityEvidenceJournal,
    CapabilityEvidenceSource,
    CapabilityEvidenceStrength,
    CapabilityRequirement,
    CapabilityScope,
)

_HASH = "a" * 64


def _profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at="2026-09-21T10:00:00+00:00",
        source_ref="caller-chosen-profile-source",
        source_payload_sha256=_HASH,
    )


def _scope() -> CapabilityScope:
    return CapabilityScope(
        venue_id="betfair",
        account_id="acct-a",
        environment="production",
        jurisdiction="SK",
        sport="soccer",
        market_family="match_odds",
        live_mode="live",
        credential_identity="caller-chosen-credential-v1",
    )


def _evidence(profile: BookmakerCapabilityProfile) -> CapabilityEvidence:
    return CapabilityEvidence(
        profile_id=profile.profile_id,
        capability=BookmakerCapability.BALANCE_READ,
        support_state=BookmakerCapabilityState.SUPPORTED,
        strength=CapabilityEvidenceStrength.OBSERVED_ACCOUNT_SCOPED,
        source=CapabilityEvidenceSource.ACCOUNT_OBSERVATION,
        scope=_scope(),
        observed_at=profile.observed_at,
        committed_at="2026-09-21T10:01:00+00:00",
        review_due_at="2026-09-22T10:01:00+00:00",
        validation_policy_version="v1",
        source_contract_ref="caller-chosen-api-v1",
        source_payload_sha256=_HASH,
    )


def _availability(evidence: CapabilityEvidence) -> CapabilityAvailability:
    return CapabilityAvailability(
        evidence_id=evidence.evidence_id,
        state=CapabilityAvailabilityState.AVAILABLE,
        observed_at="2026-09-21T10:02:00+00:00",
        source_ref="caller-chosen-health",
        source_payload_sha256=_HASH,
    )


def _requirement() -> CapabilityRequirement:
    return CapabilityRequirement(
        capability=BookmakerCapability.BALANCE_READ,
        minimum_strength=CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED,
        scope=_scope(),
        validation_policy_version="v1",
        source_contract_ref="caller-chosen-api-v1",
        max_observation_age_seconds=86400,
    )


def _assert_not_authorized(
    journal: CapabilityEvidenceJournal,
    profile: BookmakerCapabilityProfile,
) -> None:
    decision = journal.resolve(
        _requirement(),
        {profile.profile_id: profile},
        as_of="2026-09-21T10:03:00+00:00",
    )
    assert not decision.allowed, (
        "caller-authored structural DTO/hash consistency must not mint "
        "positive authenticated/account-scoped capability authority"
    )


def test_public_evidence_and_availability_dtos_cannot_mint_positive_authority():
    profile = _profile()
    evidence = _evidence(profile)
    availability = _availability(evidence)
    journal = CapabilityEvidenceJournal()

    try:
        journal.publish(evidence)
        journal.publish_availability(availability)
    except CapabilityEvidenceError:
        # A production-owned issuer boundary may reject caller DTOs at publication.
        return

    _assert_not_authorized(journal, profile)


def test_restart_json_reconstruction_cannot_restore_positive_authority():
    profile = _profile()
    evidence = _evidence(profile)
    availability = _availability(evidence)

    caller_serialized_journal = json.dumps(
        {
            "schema_version": 1,
            "evidence": [evidence.payload()],
            "availability": [availability.payload()],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )

    try:
        restored = CapabilityEvidenceJournal.from_json(caller_serialized_journal)
    except CapabilityEvidenceError:
        # Failing closed while re-resolving persisted evidence is acceptable.
        return

    _assert_not_authorized(restored, profile)
