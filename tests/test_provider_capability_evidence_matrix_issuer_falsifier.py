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
from autosport.provider_capability_evidence_matrix import (
    ProviderCapabilityTruthGrade,
    build_provider_capability_evidence_matrix,
    issue_provider_capability_evidence,
)


T0 = "2026-09-22T00:00:00+00:00"
T1 = "2026-09-22T00:01:00+00:00"
T2 = "2026-09-22T00:02:00+00:00"
T3 = "2026-09-22T00:03:00+00:00"
T4 = "2026-09-22T00:04:00+00:00"


def test_arbitrary_caller_metadata_cannot_mint_write_permission_authority() -> None:
    profile = BookmakerCapabilityProfile(
        venue_id="provider-a",
        account_id="acct-a",
        adapter_id="official-api",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.PLACE_BET,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=T0,
        source_ref="profile://provider-a/acct-a",
        source_payload_sha256="a" * 64,
    )
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=T1,
        source_ref="integration://provider-a/acct-a",
        source_payload_sha256="b" * 64,
    )

    caller_minted = issue_provider_capability_evidence(
        capability=BookmakerCapability.PLACE_BET,
        profile_state=BookmakerCapabilityState.SUPPORTED,
        grade=ProviderCapabilityTruthGrade.WRITE_PERMISSION_PROVEN,
        profile_id=profile.profile_id,
        integration_evidence_id=integration.evidence_id,
        observed_at=T2,
        expires_at=T4,
        evidence_ref="caller://unverified-write-permission",
        evidence_sha256="c" * 64,
        endpoint_operation="caller_claimed_place_bet",
    )
    matrix = build_provider_capability_evidence_matrix(
        profile,
        integration,
        environment="production",
        application_mode="live-key",
        matrix_version=1,
        as_of=T3,
        matrix_ref="caller-minted-write-permission",
        evidence=(caller_minted,),
    )

    assert not matrix.qualifies(
        BookmakerCapability.PLACE_BET,
        accepted_grades=frozenset(
            {ProviderCapabilityTruthGrade.WRITE_PERMISSION_PROVEN}
        ),
        at_time=T3,
    ), (
        "calling the public issuer with arbitrary strings/hashes must not itself "
        "prove upstream provider write permission"
    )
