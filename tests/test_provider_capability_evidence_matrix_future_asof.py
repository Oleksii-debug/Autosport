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
T3_30 = "2026-09-22T00:03:30+00:00"
T4 = "2026-09-22T00:04:00+00:00"


def test_matrix_cannot_qualify_capability_after_its_own_as_of_cutoff() -> None:
    profile = BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-a",
        adapter_id="betfair-api",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=T0,
        source_ref="profile",
        source_payload_sha256="a" * 64,
    )
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=T1,
        source_ref="integration",
        source_payload_sha256="b" * 64,
    )
    fact = issue_provider_capability_evidence(
        capability=BookmakerCapability.BALANCE_READ,
        profile_state=BookmakerCapabilityState.SUPPORTED,
        grade=ProviderCapabilityTruthGrade.CONFIGURED,
        profile_id=profile.profile_id,
        integration_evidence_id=integration.evidence_id,
        environment="production",
        application_mode="live-key-readonly",
        observed_at=T2,
        expires_at=T4,
        evidence_ref="evidence://balance/read",
        evidence_sha256="c" * 64,
        endpoint_operation="getAccountFunds",
    )
    matrix = build_provider_capability_evidence_matrix(
        profile,
        integration,
        environment="production",
        application_mode="live-key-readonly",
        matrix_version=1,
        as_of=T3,
        matrix_ref="future-asof-falsifier",
        evidence=(fact,),
    )
    accepted = frozenset(
        {ProviderCapabilityTruthGrade.CONFIGURED}
    )

    assert matrix.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=accepted,
        at_time=T3,
    )
    assert not matrix.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=accepted,
        at_time=T3_30,
    )
