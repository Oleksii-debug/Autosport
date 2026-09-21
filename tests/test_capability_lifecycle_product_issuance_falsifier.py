from __future__ import annotations

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


def test_caller_constructed_profile_evidence_and_availability_cannot_mint_current_capability() -> None:
    """Canonical-looking DTOs are assertions until product-owned issuance is resolved."""

    try:
        profile = BookmakerCapabilityProfile(
            venue_id="betfair",
            account_id="caller-account",
            adapter_id="caller-adapter",
            adapter_version="1",
            profile_version=1,
            facts=(
                BookmakerCapabilityFact(
                    BookmakerCapability.BALANCE_READ,
                    BookmakerCapabilityState.SUPPORTED,
                ),
            ),
            observed_at="2026-09-21T10:00:00+00:00",
            source_ref="caller://profile",
            source_payload_sha256="a" * 64,
        )
        scope = CapabilityScope(
            venue_id="betfair",
            account_id="caller-account",
            environment="production",
            credential_identity="caller-credential",
        )
        evidence = CapabilityEvidence(
            profile_id=profile.profile_id,
            capability=BookmakerCapability.BALANCE_READ,
            support_state=BookmakerCapabilityState.SUPPORTED,
            strength=CapabilityEvidenceStrength.OBSERVED_ACCOUNT_SCOPED,
            source=CapabilityEvidenceSource.ACCOUNT_OBSERVATION,
            scope=scope,
            observed_at=profile.observed_at,
            committed_at="2026-09-21T10:01:00+00:00",
            review_due_at="2026-09-22T10:01:00+00:00",
            validation_policy_version="v1",
            source_contract_ref="caller-contract-v1",
            source_payload_sha256="b" * 64,
        )
        availability = CapabilityAvailability(
            evidence_id=evidence.evidence_id,
            state=CapabilityAvailabilityState.AVAILABLE,
            observed_at="2026-09-21T10:02:00+00:00",
            source_ref="caller://health",
            source_payload_sha256="c" * 64,
        )
        requirement = CapabilityRequirement(
            capability=BookmakerCapability.BALANCE_READ,
            minimum_strength=CapabilityEvidenceStrength.OBSERVED_AUTHENTICATED,
            scope=scope,
            validation_policy_version="v1",
            source_contract_ref="caller-contract-v1",
            max_observation_age_seconds=86400,
        )
        journal = CapabilityEvidenceJournal()
        journal.publish(evidence)
        journal.publish_availability(availability)
        decision = journal.resolve(
            requirement,
            {profile.profile_id: profile},
            as_of="2026-09-21T10:03:00+00:00",
        )
    except (CapabilityEvidenceError, TypeError, ValueError):
        # Rejecting caller-authored authority at any boundary is a safe repair.
        return

    assert not decision.allowed
