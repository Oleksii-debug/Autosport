from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport.provider_output_governance import (
    ProviderOutputGovernanceAuthority,
    ProviderOutputGrant,
    ProviderOutputUseRequest,
    RetentionPolicy,
)
from autosport.provider_output_governance_resolution import (
    decide_provider_output_use_with_owner_approval,
)
from autosport.provider_owner_approval import (
    OwnerApprovalResolutionReason,
    OwnerApprovalStore,
)


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_DECIDED_AT = "2026-09-10T13:00:00Z"


def _authority() -> ProviderOutputGovernanceAuthority:
    return ProviderOutputGovernanceAuthority(
        provider_id="provider-a",
        service_id="market-data",
        authority_version="owner-v1",
        terms_reference="https://provider.example/legal/terms",
        terms_sha256=_SHA_A,
        owner_approval_reference="urn:autosport:owner-approval:2026-09-09",
        owner_approval_sha256=_SHA_B,
        valid_from="2026-09-01T00:00:00Z",
        valid_until="2026-10-01T00:00:00Z",
        grants=(
            ProviderOutputGrant(
                "research",
                "odds_snapshot",
                RetentionPolicy.BOUNDED,
                86400,
            ),
        ),
    )


def _request(authority: ProviderOutputGovernanceAuthority) -> ProviderOutputUseRequest:
    return ProviderOutputUseRequest(
        authority_id=authority.authority_id,
        provider_id=authority.provider_id,
        service_id=authority.service_id,
        artifact_sha256=_SHA_C,
        purpose="research",
        artifact_class="odds_snapshot",
        acquired_at="2026-09-10T12:00:00Z",
        requested_retain_until="2026-09-11T12:00:00Z",
    )


def _store(tmp_path: Path, *, cls=OwnerApprovalStore) -> OwnerApprovalStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return cls(
        (workspace / "provider-owner-approvals.json").resolve(),
        authority_root=(tmp_path / "machine-authority").resolve(),
    )


def _forged_approved_resolution():
    return SimpleNamespace(
        approved=True,
        reason=OwnerApprovalResolutionReason.APPROVED,
    )


def test_owner_approval_store_subclass_cannot_replace_canonical_resolver(
    tmp_path: Path,
) -> None:
    class ForgedOwnerApprovalStore(OwnerApprovalStore):
        def resolve(self, **kwargs):  # type: ignore[override]
            return _forged_approved_resolution()

    authority = _authority()
    store = _store(tmp_path, cls=ForgedOwnerApprovalStore)

    with pytest.raises(ValueError, match="exact canonical OwnerApprovalStore"):
        decide_provider_output_use_with_owner_approval(
            authority,
            _request(authority),
            decided_at=_DECIDED_AT,
            owner_approval_store=store,
        )


def test_instance_resolve_shadow_cannot_mint_owner_approval(
    tmp_path: Path,
) -> None:
    authority = _authority()
    store = _store(tmp_path)
    store.resolve = lambda **kwargs: _forged_approved_resolution()  # type: ignore[method-assign]

    outcome = decide_provider_output_use_with_owner_approval(
        authority,
        _request(authority),
        decided_at=_DECIDED_AT,
        owner_approval_store=store,
    )

    assert outcome.allowed is False
    assert outcome.reason == "OWNER_APPROVAL_UNRESOLVED_AT_ACQUISITION"
