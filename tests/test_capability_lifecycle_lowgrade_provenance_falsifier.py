"""Falsifier for caller-minted low-grade capability lifecycle authority.

#1237 requires every evidence strength to be mechanically derived from product-owned
source evidence.  #1270 currently fails closed for caller-authored authenticated /
account-scoped observations, but DOCUMENTED_ONLY and OBSERVED_PUBLIC still have a
positive resolver path when a consumer does not require runtime availability.
"""

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_capability_lifecycle import (
    CapabilityEvidence,
    CapabilityEvidenceJournal,
    CapabilityEvidenceSource,
    CapabilityEvidenceStrength,
    CapabilityRequirement,
    CapabilityScope,
)


_HASH = "a" * 64
_OBSERVED_AT = "2026-09-21T10:00:00+00:00"


def _caller_profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.PLACE_BET,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=_OBSERVED_AT,
        source_ref="caller-claimed-profile-source",
        source_payload_sha256=_HASH,
    )


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
def test_caller_lowgrade_evidence_cannot_mint_positive_capability_authority(
    strength: CapabilityEvidenceStrength,
    source: CapabilityEvidenceSource,
) -> None:
    """Shape-valid caller records must not become product-owned capability truth."""

    profile = _caller_profile()
    scope = CapabilityScope(
        venue_id="betfair",
        account_id=None,
        environment="production",
        jurisdiction="SK",
        sport="soccer",
        market_family="match_odds",
        live_mode="prematch",
        credential_identity=None,
    )
    evidence = CapabilityEvidence(
        profile_id=profile.profile_id,
        capability=BookmakerCapability.PLACE_BET,
        support_state=BookmakerCapabilityState.SUPPORTED,
        strength=strength,
        source=source,
        scope=scope,
        observed_at=profile.observed_at,
        committed_at="2026-09-21T10:01:00+00:00",
        review_due_at="2026-09-22T10:01:00+00:00",
        validation_policy_version="policy-v1",
        source_contract_ref="caller-claimed-provider-contract",
        source_payload_sha256="b" * 64,
    )
    journal = CapabilityEvidenceJournal()
    journal.publish(evidence)

    requirement = CapabilityRequirement(
        capability=BookmakerCapability.PLACE_BET,
        minimum_strength=strength,
        scope=scope,
        validation_policy_version="policy-v1",
        source_contract_ref="caller-claimed-provider-contract",
        max_observation_age_seconds=86_400,
        require_available=False,
    )

    decision = journal.resolve(
        requirement,
        {profile.profile_id: profile},
        as_of="2026-09-21T10:02:00+00:00",
    )

    # Product-owned provenance is absent.  The lifecycle DTO, source reference,
    # digest and profile mapping above are all caller-authored and mutually
    # self-consistent; that must not be enough to mint positive provider truth.
    assert not decision.allowed
