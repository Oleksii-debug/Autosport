import pytest

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
    CapabilityLifecycleState,
    CapabilityRequirement,
    CapabilityScope,
)

_HASH = "a" * 64
_TS = "2026-09-21T10:00:00+00:00"


def _profile(
    capability=BookmakerCapability.BALANCE_READ,
    state=BookmakerCapabilityState.SUPPORTED,
    observed_at=_TS,
):
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1",
        profile_version=1,
        facts=(BookmakerCapabilityFact(capability, state),),
        observed_at=observed_at,
        source_ref="probe",
        source_payload_sha256=_HASH,
    )


def _scope(*, account="acct-a", credential="app-v1"):
    return CapabilityScope(
        venue_id="betfair",
        account_id=account,
        environment="production",
        jurisdiction="SK",
        sport="soccer",
        market_family="match_odds",
        live_mode="live",
        credential_identity=credential,
    )


def _evidence(profile=None, **overrides):
    profile = profile or _profile()
    values = {
        "profile_id": profile.profile_id,
        "capability": BookmakerCapability.BALANCE_READ,
        "support_state": BookmakerCapabilityState.SUPPORTED,
        "strength": CapabilityEvidenceStrength.OBSERVED_ACCOUNT_SCOPED,
        "source": CapabilityEvidenceSource.ACCOUNT_OBSERVATION,
        "scope": _scope(),
        "observed_at": profile.observed_at,
        "committed_at": "2026-09-21T10:01:00+00:00",
        "review_due_at": "2026-09-22T10:01:00+00:00",
        "validation_policy_version": "v1",
        "source_contract_ref": "api-v1",
        "source_payload_sha256": _HASH,
    }
    values.update(overrides)
    return CapabilityEvidence(**values)


def _requirement(**overrides):
    values = {
        "capability": BookmakerCapability.BALANCE_READ,
        "minimum_strength": CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED,
        "scope": _scope(),
        "validation_policy_version": "v1",
        "source_contract_ref": "api-v1",
        "max_observation_age_seconds": 86400,
    }
    values.update(overrides)
    return CapabilityRequirement(**values)


def _availability(evidence, state=CapabilityAvailabilityState.AVAILABLE, **overrides):
    values = {
        "evidence_id": evidence.evidence_id,
        "state": state,
        "observed_at": "2026-09-21T10:02:00+00:00",
        "source_ref": "health",
        "source_payload_sha256": _HASH,
    }
    values.update(overrides)
    return CapabilityAvailability(**values)


def _journal(profile=None, evidence=None, availability=True):
    profile = profile or _profile()
    evidence = evidence or _evidence(profile)
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    if availability:
        journal.publish_availability(_availability(evidence))
    return profile, evidence, journal


def _resolve(journal, profile, requirement=None, as_of="2026-09-21T10:03:00+00:00"):
    return journal.resolve(
        requirement or _requirement(),
        {profile.profile_id: profile},
        as_of=as_of,
    )


def test_caller_authenticated_account_scope_requires_upstream_authority():
    profile, _, journal = _journal()
    decision = _resolve(journal, profile)
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert decision.availability is CapabilityAvailabilityState.UNKNOWN
    assert "product-owned upstream authority" in decision.reason


def test_caller_available_state_cannot_mint_runtime_health_authority():
    profile = _profile()
    scope = CapabilityScope("betfair", None, "production")
    evidence = _evidence(
        profile,
        strength=CapabilityEvidenceStrength.DOCUMENTED_ONLY,
        source=CapabilityEvidenceSource.OFFICIAL_DOCUMENT,
        scope=scope,
    )
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    journal.publish_availability(_availability(evidence))
    requirement = CapabilityRequirement(
        BookmakerCapability.BALANCE_READ,
        CapabilityEvidenceStrength.DOCUMENTED_ONLY,
        scope,
        "v1",
        "api-v1",
        86400,
        require_available=True,
    )
    decision = journal.resolve(
        requirement,
        {profile.profile_id: profile},
        as_of="2026-09-21T10:03:00+00:00",
    )
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.UNKNOWN
    assert decision.availability is CapabilityAvailabilityState.UNKNOWN
    assert "product-owned provenance authority" in decision.reason


@pytest.mark.parametrize(
    ("strength", "source"),
    [
        (
            CapabilityEvidenceStrength.DOCUMENTED_ONLY,
            CapabilityEvidenceSource.OFFICIAL_DOCUMENT,
        ),
        (
            CapabilityEvidenceStrength.OBSERVED_PUBLIC,
            CapabilityEvidenceSource.PUBLIC_OBSERVATION,
        ),
    ],
)
def test_caller_low_grade_evidence_cannot_mint_positive_authority_without_provenance(
    strength, source
):
    profile = _profile()
    scope = CapabilityScope("betfair", None, "production")
    evidence = _evidence(
        profile,
        strength=strength,
        source=source,
        scope=scope,
    )
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    requirement = CapabilityRequirement(
        BookmakerCapability.BALANCE_READ,
        strength,
        scope,
        "v1",
        "api-v1",
        86400,
        require_available=False,
    )

    decision = journal.resolve(
        requirement,
        {profile.profile_id: profile},
        as_of="2026-09-21T10:03:00+00:00",
    )

    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.UNKNOWN
    assert decision.availability is CapabilityAvailabilityState.UNKNOWN
    assert "product-owned provenance authority" in decision.reason


def test_low_grade_restart_does_not_strengthen_unproven_provenance():
    profile = _profile()
    scope = CapabilityScope("betfair", None, "production")
    evidence = _evidence(
        profile,
        strength=CapabilityEvidenceStrength.DOCUMENTED_ONLY,
        source=CapabilityEvidenceSource.OFFICIAL_DOCUMENT,
        scope=scope,
    )
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    restored = CapabilityEvidenceJournal.from_json(journal.to_json())
    requirement = CapabilityRequirement(
        BookmakerCapability.BALANCE_READ,
        CapabilityEvidenceStrength.DOCUMENTED_ONLY,
        scope,
        "v1",
        "api-v1",
        86400,
        require_available=False,
    )

    decision = restored.resolve(
        requirement,
        {profile.profile_id: profile},
        as_of="2026-09-21T10:03:00+00:00",
    )

    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.UNKNOWN
    assert decision.evidence_id == evidence.evidence_id
    assert "product-owned provenance authority" in decision.reason


def test_documented_place_bet_cannot_satisfy_authenticated_requirement():
    profile = _profile(BookmakerCapability.PLACE_BET)
    evidence = _evidence(
        profile,
        capability=BookmakerCapability.PLACE_BET,
        strength=CapabilityEvidenceStrength.DOCUMENTED_ONLY,
        source=CapabilityEvidenceSource.OFFICIAL_DOCUMENT,
        scope=CapabilityScope("betfair", None, "production"),
    )
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    journal.publish_availability(_availability(evidence))
    requirement = CapabilityRequirement(
        BookmakerCapability.PLACE_BET,
        CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED,
        CapabilityScope("betfair", None, "production"),
        "v1",
        "api-v1",
        86400,
    )
    decision = journal.resolve(
        requirement, {profile.profile_id: profile}, as_of="2026-09-21T10:03:00+00:00"
    )
    assert not decision.allowed
    assert "strength" in decision.reason


def test_unproven_requirement_cannot_authorize_any_capability():
    with pytest.raises(CapabilityEvidenceError, match="UNPROVEN"):
        _requirement(minimum_strength=CapabilityEvidenceStrength.UNPROVEN)


def test_republishing_old_profile_cannot_extend_observation_freshness():
    profile = _profile()
    republished = _evidence(
        profile,
        committed_at="2026-09-21T10:59:00+00:00",
        review_due_at="2026-09-22T10:59:00+00:00",
        source_payload_sha256="b" * 64,
    )
    journal = CapabilityEvidenceJournal()
    journal.publish(republished)
    journal.publish_availability(
        _availability(republished, observed_at="2026-09-21T10:59:30+00:00")
    )
    decision = _resolve(
        journal,
        profile,
        _requirement(max_observation_age_seconds=3600),
        as_of="2026-09-21T11:00:00+00:00",
    )
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert "observation age" in decision.reason


def test_account_scope_cannot_generalize():
    profile, _, journal = _journal()
    decision = _resolve(
        journal,
        profile,
        _requirement(scope=_scope(account="acct-b")),
    )
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.UNKNOWN


def test_credential_policy_and_contract_changes_require_revalidation():
    profile, _, journal = _journal()
    credential = _resolve(
        journal,
        profile,
        _requirement(scope=_scope(credential="app-v2")),
    )
    policy = _resolve(
        journal,
        profile,
        _requirement(validation_policy_version="v2"),
    )
    contract = _resolve(
        journal,
        profile,
        _requirement(source_contract_ref="api-v2"),
    )
    assert credential.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert policy.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert contract.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED


def test_expiry_and_review_boundaries_are_closed():
    profile = _profile()
    expiring = _evidence(
        profile,
        provider_expires_at="2026-09-21T11:00:00+00:00",
        review_due_at="2026-09-21T12:00:00+00:00",
    )
    _, _, journal = _journal(profile, expiring)
    before_expiry = _resolve(
        journal, profile, as_of="2026-09-21T10:59:59+00:00"
    )
    assert not before_expiry.allowed
    assert before_expiry.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert "product-owned upstream authority" in before_expiry.reason
    expired = _resolve(journal, profile, as_of="2026-09-21T11:00:00+00:00")
    assert expired.lifecycle is CapabilityLifecycleState.STALE

    review = _evidence(profile, review_due_at="2026-09-21T11:00:00+00:00")
    _, _, journal = _journal(profile, review)
    due = _resolve(journal, profile, as_of="2026-09-21T11:00:00+00:00")
    assert due.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED


def test_transient_outage_does_not_revoke_durable_support():
    profile = _profile()
    evidence = _evidence(profile)
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)
    journal.publish_availability(
        _availability(
            evidence,
            CapabilityAvailabilityState.TEMPORARILY_UNAVAILABLE,
        )
    )
    decision = _resolve(journal, profile)
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert decision.availability is CapabilityAvailabilityState.TEMPORARILY_UNAVAILABLE
    assert "product-owned upstream authority" in decision.reason
    assert decision.lifecycle is not CapabilityLifecycleState.REVOKED_OR_UNSUPPORTED


def test_later_unsupported_evidence_supersedes_historical_positive():
    first = _profile()
    positive = _evidence(first)
    journal = CapabilityEvidenceJournal()
    journal.publish(positive)

    second = _profile(
        state=BookmakerCapabilityState.UNSUPPORTED,
        observed_at="2026-09-21T10:05:00+00:00",
    )
    revoked = _evidence(
        second,
        support_state=BookmakerCapabilityState.UNSUPPORTED,
        observed_at=second.observed_at,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-22T10:06:00+00:00",
        predecessor_id=positive.evidence_id,
    )
    journal.publish(revoked)
    journal.publish_availability(
        _availability(revoked, observed_at="2026-09-21T10:07:00+00:00")
    )
    decision = journal.resolve(
        _requirement(),
        {first.profile_id: first, second.profile_id: second},
        as_of="2026-09-21T10:08:00+00:00",
    )
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVOKED_OR_UNSUPPORTED


def test_restart_roundtrip_preserves_age_and_identity():
    profile, evidence, journal = _journal()
    restored = CapabilityEvidenceJournal.from_json(journal.to_json())
    assert restored.to_json() == journal.to_json()
    decision = restored.resolve(
        _requirement(),
        {profile.profile_id: profile},
        as_of="2026-09-22T10:01:00+00:00",
    )
    assert decision.evidence_id == evidence.evidence_id
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED


def test_missing_profile_after_restart_fails_closed():
    _, _, journal = _journal()
    decision = journal.resolve(
        _requirement(), {}, as_of="2026-09-21T10:03:00+00:00"
    )
    assert not decision.allowed
    assert "re-resolved" in decision.reason


def test_subclass_and_execution_authority_cannot_be_minted():
    class ForgedEvidence(CapabilityEvidence):
        pass

    profile = _profile()
    valid = _evidence(profile)
    forged = ForgedEvidence(**{
        field: getattr(valid, field)
        for field in valid.__dataclass_fields__
    })
    with pytest.raises(CapabilityEvidenceError, match="exact CapabilityEvidence"):
        CapabilityEvidenceJournal().publish(forged)
    with pytest.raises(CapabilityEvidenceError, match="real execution authority"):
        _evidence(profile, strength=CapabilityEvidenceStrength.EXECUTION_PROVEN)


def test_identical_revalidation_is_idempotent_but_same_time_conflict_fails():
    profile = _profile()
    evidence = _evidence(profile)
    journal = CapabilityEvidenceJournal()
    assert journal.publish(evidence) == evidence.evidence_id
    assert journal.publish(evidence) == evidence.evidence_id
    conflicting = _evidence(profile, source_payload_sha256="b" * 64)
    with pytest.raises(CapabilityEvidenceError, match="conflicting"):
        journal.publish(conflicting)


def test_credential_rotation_revalidation_can_link_predecessor_and_recover():
    first = _profile()
    old = _evidence(first)
    journal = CapabilityEvidenceJournal()
    journal.publish(old)

    second = _profile(observed_at="2026-09-21T10:05:00+00:00")
    fresh = _evidence(
        second,
        scope=_scope(credential="app-v2"),
        observed_at=second.observed_at,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-22T10:06:00+00:00",
        predecessor_id=old.evidence_id,
    )
    journal.publish(fresh)
    journal.publish_availability(
        _availability(fresh, observed_at="2026-09-21T10:07:00+00:00")
    )
    decision = journal.resolve(
        _requirement(scope=_scope(credential="app-v2")),
        {first.profile_id: first, second.profile_id: second},
        as_of="2026-09-21T10:08:00+00:00",
    )
    assert not decision.allowed
    assert decision.lifecycle is CapabilityLifecycleState.REVALIDATION_REQUIRED
    assert "product-owned upstream authority" in decision.reason
    assert decision.evidence_id == fresh.evidence_id

def test_successor_must_link_exact_latest_same_scope_predecessor():
    first = _profile()
    positive = _evidence(first)
    journal = CapabilityEvidenceJournal()
    journal.publish(positive)

    revoked_profile = _profile(
        state=BookmakerCapabilityState.UNSUPPORTED,
        observed_at="2026-09-21T10:05:00+00:00",
    )
    revoked = _evidence(
        revoked_profile,
        support_state=BookmakerCapabilityState.UNSUPPORTED,
        observed_at=revoked_profile.observed_at,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-22T10:06:00+00:00",
        predecessor_id=positive.evidence_id,
    )
    journal.publish(revoked)

    recovered_profile = _profile(observed_at="2026-09-21T10:10:00+00:00")
    common = {
        "observed_at": recovered_profile.observed_at,
        "committed_at": "2026-09-21T10:11:00+00:00",
        "review_due_at": "2026-09-22T10:11:00+00:00",
    }

    unlinked_recovery = _evidence(
        recovered_profile,
        predecessor_id=None,
        **common,
    )
    with pytest.raises(CapabilityEvidenceError, match="exact latest predecessor"):
        journal.publish(unlinked_recovery)

    stale_link_recovery = _evidence(
        recovered_profile,
        predecessor_id=positive.evidence_id,
        **common,
    )
    with pytest.raises(CapabilityEvidenceError, match="exact latest predecessor"):
        journal.publish(stale_link_recovery)

    linked_recovery = _evidence(
        recovered_profile,
        predecessor_id=revoked.evidence_id,
        **common,
    )
    assert journal.publish(linked_recovery) == linked_recovery.evidence_id

