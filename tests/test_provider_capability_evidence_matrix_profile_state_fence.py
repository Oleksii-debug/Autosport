from __future__ import annotations

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
from autosport.provider_capability_evidence_matrix import (
    ProviderCapabilityTruthGrade,
    build_provider_capability_evidence_matrix,
    issue_provider_capability_evidence,
)


T0 = "2026-10-03T00:00:00+00:00"
T1 = "2026-10-03T00:01:00+00:00"
T2 = "2026-10-03T00:02:00+00:00"
T3 = "2026-10-03T00:03:00+00:00"


@pytest.mark.parametrize(
    "profile_state",
    [BookmakerCapabilityState.UNKNOWN, BookmakerCapabilityState.UNSUPPORTED],
)
@pytest.mark.parametrize(
    "grade",
    [
        ProviderCapabilityTruthGrade.DECLARED_DOCUMENTED,
        ProviderCapabilityTruthGrade.CONFIGURED,
    ],
)
def test_weaker_fact_cannot_qualify_against_non_supported_profile(
    profile_state: BookmakerCapabilityState,
    grade: ProviderCapabilityTruthGrade,
) -> None:
    profile = BookmakerCapabilityProfile(
        venue_id="provider-a",
        account_id="account-a",
        adapter_id="official-api",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.LIVE_QUOTES_READ,
                profile_state,
            ),
        ),
        observed_at=T0,
        source_ref="profile://provider-a/account-a",
        source_payload_sha256="a" * 64,
    )
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=T1,
        source_ref="integration://provider-a/account-a",
        source_payload_sha256="b" * 64,
    )
    fact = issue_provider_capability_evidence(
        capability=BookmakerCapability.LIVE_QUOTES_READ,
        profile_state=profile_state,
        grade=grade,
        profile_id=profile.profile_id,
        integration_evidence_id=integration.evidence_id,
        environment="production",
        application_mode="readonly",
        observed_at=T2,
        evidence_ref="evidence://weak/live-quotes",
        evidence_sha256="c" * 64,
        endpoint_operation="listMarketBook",
    )
    matrix = build_provider_capability_evidence_matrix(
        profile,
        integration,
        environment="production",
        application_mode="readonly",
        matrix_version=1,
        as_of=T3,
        matrix_ref="matrix-profile-state-fence",
        evidence=(fact,),
    )
    accepted = frozenset({grade})

    assert not matrix.qualifies(
        BookmakerCapability.LIVE_QUOTES_READ,
        accepted_grades=accepted,
        at_time=T3,
    )
    assert not matrix.qualifies_scoped(
        BookmakerCapability.LIVE_QUOTES_READ,
        accepted_grades=accepted,
        at_time=T3,
        sport="tennis",
    )
