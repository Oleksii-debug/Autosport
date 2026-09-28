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
    issue_provider_capability_evidence,
)


def test_caller_metadata_cannot_mint_current_read_authority() -> None:
    profile = BookmakerCapabilityProfile(
        venue_id="provider-a",
        account_id="account-a",
        adapter_id="official-api",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at="2026-09-22T00:00:00+00:00",
        source_ref="profile-test",
        source_payload_sha256="a" * 64,
    )
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-22T00:01:00+00:00",
        source_ref="integration-test",
        source_payload_sha256="b" * 64,
    )

    for grade in (
        ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN,
        ProviderCapabilityTruthGrade.OBSERVED_OPERATIONAL,
    ):
        with pytest.raises(
            ProviderCapabilityEvidenceMatrixError,
            match="sealed upstream provider verification",
        ):
            issue_provider_capability_evidence(
                capability=BookmakerCapability.BALANCE_READ,
                profile_state=BookmakerCapabilityState.SUPPORTED,
                grade=grade,
                profile_id=profile.profile_id,
                integration_evidence_id=integration.evidence_id,
                environment="production",
                application_mode="live-key-readonly",
                observed_at="2026-09-22T00:02:00+00:00",
                expires_at="2026-09-22T00:04:00+00:00",
                evidence_ref="caller://claims/current-balance-read",
                evidence_sha256="c" * 64,
                endpoint_operation="get-account-funds",
            )
