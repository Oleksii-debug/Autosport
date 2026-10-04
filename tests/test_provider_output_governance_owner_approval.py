from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import autosport.provider_owner_approval as approval_module
from autosport.provider_output_governance import (
    ProviderOutputGovernanceAuthority,
    ProviderOutputGrant,
    ProviderOutputUseRequest,
    RetentionPolicy,
    decide_provider_output_use,
)
from autosport.provider_output_governance_resolution import (
    decide_provider_output_use_with_owner_approval,
)
from autosport.provider_owner_approval import OwnerApprovalStore


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
T0 = "2026-09-01T00:00:00Z"
APPROVED_AT = "2026-09-09T00:00:00Z"
ACQUIRED_AT = "2026-09-10T12:00:00Z"
DECIDED_AT = "2026-09-10T13:00:00Z"
REVOKED_AT = "2026-09-10T12:30:00Z"
T2 = "2026-10-01T00:00:00Z"


@pytest.fixture
def product_clock(monkeypatch: pytest.MonkeyPatch):
    value = [datetime.fromisoformat(APPROVED_AT.replace("Z", "+00:00"))]
    monkeypatch.setattr(approval_module, "_utc_now", lambda: value[0])

    def set_clock(text: str) -> None:
        value[0] = datetime.fromisoformat(text.replace("Z", "+00:00"))

    return set_clock


def _authority(**changes) -> ProviderOutputGovernanceAuthority:
    values = dict(
        provider_id="provider-a",
        service_id="market-data",
        authority_version="owner-v1",
        terms_reference="https://provider.example/legal/terms",
        terms_sha256=SHA_A,
        owner_approval_reference="urn:autosport:owner-approval:2026-09-09",
        owner_approval_sha256=SHA_B,
        valid_from=T0,
        valid_until=T2,
        grants=(
            ProviderOutputGrant(
                "research",
                "odds_snapshot",
                RetentionPolicy.BOUNDED,
                86400,
            ),
        ),
    )
    values.update(changes)
    return ProviderOutputGovernanceAuthority(**values)


def _request(
    authority: ProviderOutputGovernanceAuthority,
    **changes,
) -> ProviderOutputUseRequest:
    values = dict(
        authority_id=authority.authority_id,
        provider_id=authority.provider_id,
        service_id=authority.service_id,
        artifact_sha256=SHA_C,
        purpose="research",
        artifact_class="odds_snapshot",
        acquired_at=ACQUIRED_AT,
        requested_retain_until="2026-09-11T12:00:00Z",
    )
    values.update(changes)
    return ProviderOutputUseRequest(**values)


def _store(tmp_path: Path) -> OwnerApprovalStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return OwnerApprovalStore(
        (workspace / "provider-owner-approvals.json").resolve(),
        authority_root=(tmp_path / "machine-authority").resolve(),
    )


def _approve(
    store: OwnerApprovalStore,
    authority: ProviderOutputGovernanceAuthority,
    **changes,
):
    values = dict(
        governance_authority_id=authority.authority_id,
        provider_id=authority.provider_id,
        service_id=authority.service_id,
        owner_approval_reference=authority.owner_approval_reference,
        owner_approval_sha256=authority.owner_approval_sha256,
    )
    values.update(changes)
    return store.approve(**values)


def _decide(
    store: OwnerApprovalStore,
    authority: ProviderOutputGovernanceAuthority,
    request: ProviderOutputUseRequest | None = None,
    *,
    decided_at: str = DECIDED_AT,
):
    return decide_provider_output_use_with_owner_approval(
        authority,
        request or _request(authority),
        decided_at=decided_at,
        owner_approval_store=store,
    )


def test_legacy_matcher_remains_fail_closed_without_resolver() -> None:
    authority = _authority()
    outcome = decide_provider_output_use(
        authority,
        _request(authority),
        decided_at=DECIDED_AT,
    )
    assert outcome.allowed is False
    assert outcome.reason == "OWNER_APPROVAL_UNRESOLVED"


def test_positive_requires_durable_approval_at_acquisition_and_decision(
    tmp_path: Path,
    product_clock,
) -> None:
    authority = _authority()
    store = _store(tmp_path)
    _approve(store, authority)

    outcome = _decide(store, authority)

    assert outcome.allowed is True
    assert outcome.reason == "ALLOWED"
    assert outcome.retention_policy is RetentionPolicy.BOUNDED
    assert outcome.max_retention_seconds == 86400


def test_missing_approval_fails_at_acquisition(tmp_path: Path) -> None:
    authority = _authority()
    outcome = _decide(_store(tmp_path), authority)

    assert outcome.allowed is False
    assert outcome.reason == "OWNER_APPROVAL_UNRESOLVED_AT_ACQUISITION"


def test_late_approval_cannot_retroactively_authorize_acquisition(
    tmp_path: Path,
    product_clock,
) -> None:
    authority = _authority()
    store = _store(tmp_path)
    product_clock("2026-09-10T12:00:01Z")
    _approve(store, authority)

    outcome = _decide(store, authority)

    assert outcome.allowed is False
    assert outcome.reason == "OWNER_APPROVAL_NOT_YET_APPROVED_AT_ACQUISITION"


def test_revocation_after_acquisition_blocks_later_decision(
    tmp_path: Path,
    product_clock,
) -> None:
    authority = _authority()
    store = _store(tmp_path)
    _approve(store, authority)
    product_clock(REVOKED_AT)
    store.revoke(governance_authority_id=authority.authority_id)

    before = _decide(
        store,
        authority,
        decided_at="2026-09-10T12:29:59.999999Z",
    )
    at_revocation = _decide(store, authority, decided_at=REVOKED_AT)

    assert before.allowed is True
    assert at_revocation.allowed is False
    assert at_revocation.reason == "OWNER_APPROVAL_REVOKED_AT_DECISION"


def test_approval_identity_mismatch_cannot_authorize_matching_governance(
    tmp_path: Path,
    product_clock,
) -> None:
    authority = _authority()
    store = _store(tmp_path)
    _approve(store, authority, provider_id="other-provider")

    outcome = _decide(store, authority)

    assert outcome.allowed is False
    assert outcome.reason == "OWNER_APPROVAL_IDENTITY_MISMATCH_AT_ACQUISITION"


def test_structural_denial_precedes_owner_resolution() -> None:
    authority = _authority()
    mismatched = _request(authority, authority_id="d" * 64)

    outcome = decide_provider_output_use_with_owner_approval(
        authority,
        mismatched,
        decided_at=DECIDED_AT,
        owner_approval_store=None,  # type: ignore[arg-type]
    )

    assert outcome.allowed is False
    assert outcome.reason == "AUTHORITY_ID_MISMATCH"


def test_wrong_store_type_is_rejected_only_for_otherwise_authorizable_request() -> None:
    authority = _authority()
    with pytest.raises(ValueError, match="OwnerApprovalStore"):
        decide_provider_output_use_with_owner_approval(
            authority,
            _request(authority),
            decided_at=DECIDED_AT,
            owner_approval_store=None,  # type: ignore[arg-type]
        )


def test_retention_denial_is_not_overridden_by_durable_approval(
    tmp_path: Path,
    product_clock,
) -> None:
    authority = _authority()
    store = _store(tmp_path)
    _approve(store, authority)
    request = _request(
        authority,
        requested_retain_until="2026-09-11T12:00:00.000001Z",
    )

    outcome = _decide(store, authority, request)

    assert outcome.allowed is False
    assert outcome.reason == "RETENTION_HORIZON_EXCEEDS_GRANT"


def test_composed_decision_exposes_no_execution_or_legal_permission_authority(
    tmp_path: Path,
    product_clock,
) -> None:
    authority = _authority()
    store = _store(tmp_path)
    _approve(store, authority)
    outcome = _decide(store, authority)

    assert outcome.allowed is True
    for forbidden in (
        "provider_permission_proven",
        "legal_rights_proven",
        "execution_authorized",
        "provider_write_authorized",
        "settlement_authorized",
        "real_money_execution",
    ):
        assert not hasattr(outcome, forbidden)
