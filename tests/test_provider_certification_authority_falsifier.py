"""Dependent falsifier for PR #988 provider-certification authority.

The parent contract currently proves structural consistency between caller-supplied
capability/integration/test-evidence values.  This regression freezes the stronger
product invariant: public DTO construction alone must never mint an executable
provider-certification use.
"""

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationEvidence,
    BookmakerIntegrationKind,
)
from autosport.provider_certification import (
    ProviderCertificationEvidenceRef,
    ProviderCertifiedUse,
    ProviderTestMode,
    build_provider_certification,
)


D1 = "1" * 64
D2 = "2" * 64
D3 = "3" * 64
D4 = "4" * 64
D5 = "5" * 64
D6 = "6" * 64


def _caller_profile() -> BookmakerCapabilityProfile:
    supported = (
        BookmakerCapability.ACCOUNT_IDENTITY_READ,
        BookmakerCapability.LIMITS_READ,
        BookmakerCapability.LIVE_QUOTES_READ,
        BookmakerCapability.BETSLIP_READ,
        BookmakerCapability.OPEN_POSITIONS_READ,
        BookmakerCapability.PLACE_BET,
        BookmakerCapability.BET_READBACK,
    )
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="caller-selected-account",
        adapter_id="betfair-rest-v1",
        adapter_version="caller-selected-version",
        profile_version=1,
        facts=tuple(
            BookmakerCapabilityFact(
                capability=capability,
                state=BookmakerCapabilityState.SUPPORTED,
            )
            for capability in supported
        ),
        observed_at="2026-09-21T12:00:00+00:00",
        source_ref="caller:capability-profile",
        source_payload_sha256=D1,
    )


def _caller_integration(
    profile: BookmakerCapabilityProfile,
) -> BookmakerIntegrationEvidence:
    return BookmakerIntegrationEvidence(
        venue_id=profile.venue_id,
        adapter_id=profile.adapter_id,
        adapter_version=profile.adapter_version,
        profile_id=profile.profile_id,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T12:01:00+00:00",
        source_ref="caller:integration-evidence",
        source_payload_sha256=D2,
    )


def _caller_ref(
    profile: BookmakerCapabilityProfile,
    integration: BookmakerIntegrationEvidence,
    mode: ProviderTestMode,
    *,
    evidence_sha256: str,
) -> ProviderCertificationEvidenceRef:
    return ProviderCertificationEvidenceRef(
        kind=f"{mode.value}-qualification",
        evidence_id=f"caller:{mode.value}:run-1",
        evidence_sha256=evidence_sha256,
        observed_at="2026-09-21T12:02:00+00:00",
        tested_mode=mode,
        account_scope=profile.account_id,
        profile_id=profile.profile_id,
        integration_evidence_id=integration.evidence_id,
        capability_manifest_ref="caller:provider-manifest",
        capability_manifest_version=1,
        capability_manifest_sha256=D3,
        adapter_code_ref="caller:adapter-code",
        adapter_code_sha256=D4,
        adapter_config_ref="caller:adapter-config",
        adapter_config_sha256=D5,
    )


def test_caller_constructed_provider_evidence_cannot_mint_executable_certification():
    profile = _caller_profile()
    integration = _caller_integration(profile)
    modes = (
        ProviderTestMode.OBSERVED_EXECUTABLE,
        ProviderTestMode.SUPERVISED_EXECUTION,
    )
    refs = (
        _caller_ref(
            profile,
            integration,
            ProviderTestMode.OBSERVED_EXECUTABLE,
            evidence_sha256=D6,
        ),
        _caller_ref(
            profile,
            integration,
            ProviderTestMode.SUPERVISED_EXECUTION,
            evidence_sha256=D1,
        ),
    )

    artifact = build_provider_certification(
        profile,
        integration,
        capability_manifest_ref="caller:provider-manifest",
        capability_manifest_version=1,
        capability_manifest_sha256=D3,
        adapter_code_ref="caller:adapter-code",
        adapter_code_sha256=D4,
        adapter_config_ref="caller:adapter-config",
        adapter_config_sha256=D5,
        tested_modes=modes,
        evidence_refs=refs,
        issued_at="2026-09-21T12:03:00+00:00",
    )

    # Structural self-consistency is not provider-observation authority.  Until the
    # parent re-resolves product-owned durable evidence, neither executable use may
    # be promoted from public caller-created DTOs and syntactically valid hashes.
    assert ProviderCertifiedUse.OBSERVED_EXECUTABLE not in artifact.allowed_uses
    assert (
        ProviderCertifiedUse.SUPERVISED_EXECUTION_CAPABLE
        not in artifact.allowed_uses
    )
