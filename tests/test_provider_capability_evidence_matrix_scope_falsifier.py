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
    ProviderCapabilityEvidenceMatrixError,
    ProviderCapabilityTruthGrade,
    build_provider_capability_evidence_matrix,
    issue_provider_capability_evidence,
)


T0 = "2026-09-22T00:00:00+00:00"
T1 = "2026-09-22T00:01:00+00:00"
T2 = "2026-09-22T00:02:00+00:00"
T3 = "2026-09-22T00:03:00+00:00"
T4 = "2026-09-22T00:04:00+00:00"


def _profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
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


def _positive_scoped_fact(profile, integration, *, environment, application_mode):
    return issue_provider_capability_evidence(
        capability=BookmakerCapability.PLACE_BET,
        profile_state=BookmakerCapabilityState.SUPPORTED,
        grade=ProviderCapabilityTruthGrade.CONFIGURED,
        profile_id=profile.profile_id,
        integration_evidence_id=integration.evidence_id,
        environment=environment,
        application_mode=application_mode,
        observed_at=T2,
        expires_at=T4,
        evidence_ref="evidence://configured/provider-a/acct-a",
        evidence_sha256="c" * 64,
        endpoint_operation="configured-place-bet-adapter",
    )


def _matrix(profile, integration, fact, *, environment, application_mode, ref):
    return build_provider_capability_evidence_matrix(
        profile,
        integration,
        environment=environment,
        application_mode=application_mode,
        matrix_version=1,
        as_of=T3,
        matrix_ref=ref,
        evidence=(fact,),
    )


@pytest.mark.parametrize(
    ("first_environment", "first_mode", "second_environment", "second_mode"),
    [
        ("sandbox", "live-key", "production", "live-key"),
        ("production", "readonly-audit", "production", "live-key"),
    ],
)
def test_positive_capability_fact_cannot_qualify_across_matrix_scope(
    first_environment,
    first_mode,
    second_environment,
    second_mode,
) -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=T1,
        source_ref="integration://provider-a/acct-a",
        source_payload_sha256="b" * 64,
    )
    fact = _positive_scoped_fact(
        profile,
        integration,
        environment=first_environment,
        application_mode=first_mode,
    )

    first = _matrix(
        profile,
        integration,
        fact,
        environment=first_environment,
        application_mode=first_mode,
        ref="first-scope",
    )
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="environment/application scope mismatch",
    ):
        _matrix(
            profile,
            integration,
            fact,
            environment=second_environment,
            application_mode=second_mode,
            ref="second-scope",
        )

    accepted = frozenset({ProviderCapabilityTruthGrade.CONFIGURED})
    assert first.qualifies(
        BookmakerCapability.PLACE_BET,
        accepted_grades=accepted,
        at_time=T3,
    )
