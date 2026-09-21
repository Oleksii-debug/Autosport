import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationKind,
    bind_bookmaker_integration,
)
from autosport.provider_capability_manifest import (
    ProviderCapabilityEvidenceRef,
    ProviderCapabilityManifestError,
    ProviderCapabilityManifestFact,
    ProviderManifestCapability,
    ProviderManifestFactAuthority,
    ProviderManifestState,
    build_provider_capability_manifest,
)


_T0 = "2026-09-21T13:00:00+00:00"
_T1 = "2026-09-21T13:01:00+00:00"
_T_EVIDENCE = "2026-09-21T13:01:30+00:00"
_T2 = "2026-09-21T13:02:00+00:00"
_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64


def _profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-a",
        adapter_id="betfair-api",
        adapter_version="1.0",
        profile_version=3,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.LIVE_QUOTES_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
            BookmakerCapabilityFact(
                BookmakerCapability.SETTLED_POSITIONS_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
            BookmakerCapabilityFact(
                BookmakerCapability.PLACE_BET,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=_T0,
        source_ref="capability-profile",
        source_payload_sha256=_HASH_A,
    )


def test_caller_authored_extension_evidence_cannot_mint_proven_capability_truth() -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=_T1,
        source_ref="integration-manifest",
        source_payload_sha256=_HASH_B,
    )

    caller_authored = ProviderCapabilityEvidenceRef(
        kind="caller-self-asserted-stream-proof",
        evidence_ref="caller://self-authored-provider-proof",
        evidence_sha256=_HASH_C,
        observed_at=_T_EVIDENCE,
        profile_id=profile.profile_id,
        integration_evidence_id=integration.evidence_id,
    )
    forged_stream_fact = ProviderCapabilityManifestFact(
        capability=ProviderManifestCapability.STREAM,
        state=ProviderManifestState.PROVEN,
        authority=ProviderManifestFactAuthority.EXPLICIT_EVIDENCE,
        evidence=caller_authored,
    )

    # Binding a self-authored reference to genuine profile/integration IDs proves
    # identity compatibility, not provider-origin or product-owned evidence
    # authority. Positive extension capability truth must therefore fail closed
    # until the reference is resolved through a product-owned/re-verifiable
    # evidence authority rather than trusted by construction alone.
    with pytest.raises(ProviderCapabilityManifestError):
        build_provider_capability_manifest(
            profile,
            integration,
            manifest_ref="provider-capability-manifest",
            manifest_version=7,
            observed_at=_T2,
            source_ref="product-provider-capability-projection",
            source_payload_sha256=_HASH_C,
            extension_facts=(forged_stream_fact,),
        )
