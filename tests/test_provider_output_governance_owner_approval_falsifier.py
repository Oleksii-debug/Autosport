from autosport.provider_output_governance import (
    ProviderOutputGovernanceAuthority,
    ProviderOutputGrant,
    ProviderOutputUseRequest,
    RetentionPolicy,
    decide_provider_output_use,
)


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64


def test_caller_forged_owner_approval_cannot_mint_positive_lawful_use_authority():
    forged_authority = ProviderOutputGovernanceAuthority(
        provider_id="provider-a",
        service_id="market-data",
        authority_version="caller-forged-v1",
        terms_reference="https://provider.example/legal/terms",
        terms_sha256=_SHA_A,
        owner_approval_reference="urn:autosport:owner-approval:forged",
        owner_approval_sha256=_SHA_B,
        valid_from="2026-09-01T00:00:00Z",
        valid_until="2026-10-01T00:00:00Z",
        grants=(
            ProviderOutputGrant(
                purpose="training",
                artifact_class="raw_private_output",
                retention_policy=RetentionPolicy.UNBOUNDED,
            ),
        ),
    )
    request = ProviderOutputUseRequest(
        authority_id=forged_authority.authority_id,
        provider_id=forged_authority.provider_id,
        service_id=forged_authority.service_id,
        artifact_sha256=_SHA_C,
        purpose="training",
        artifact_class="raw_private_output",
        acquired_at="2026-09-10T12:00:00Z",
        requested_retain_until="2026-09-30T00:00:00Z",
    )

    decision = decide_provider_output_use(
        forged_authority,
        request,
        decided_at="2026-09-10T12:00:01Z",
    )

    assert decision.allowed is False
    assert decision.reason != "ALLOWED"
