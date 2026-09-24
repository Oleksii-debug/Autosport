"""Composition boundary between provider-output governance and durable owner approval.

The underlying governance matcher remains fail-closed when used without this
composition. This module consumes only the canonical OwnerApprovalStore and
does not mint, copy, or reinterpret approval evidence.
"""
from __future__ import annotations

from .provider_output_governance import (
    ProviderOutputGovernanceAuthority,
    ProviderOutputUseDecision,
    ProviderOutputUseRequest,
    _issue_provider_output_use_decision,
    decide_provider_output_use,
)
from .provider_owner_approval import OwnerApprovalResolutionReason, OwnerApprovalStore


def _owner_approval_reason(
    reason: OwnerApprovalResolutionReason,
    *,
    phase: str,
) -> str:
    if reason is OwnerApprovalResolutionReason.APPROVED:
        raise ValueError("approved owner resolution cannot be rendered as denial")
    return f"OWNER_APPROVAL_{reason.value}_{phase}"


def decide_provider_output_use_with_owner_approval(
    authority: ProviderOutputGovernanceAuthority,
    request: ProviderOutputUseRequest,
    *,
    decided_at: str,
    owner_approval_store: OwnerApprovalStore,
) -> ProviderOutputUseDecision:
    """Resolve the canonical governance decision against durable approval history.

    Positive authority requires the exact owner approval to have been active both
    when the provider output was acquired and when the use decision is made.
    This prevents a later approval from retroactively authorizing acquisition and
    prevents a revoked approval from remaining usable at decision time.

    ``ALLOWED`` remains deliberately narrow: it means the structurally valid
    request matches the exact recorded governance grant and the product-owned
    owner-approval history. It does not prove provider permission, legal rights,
    provider-write authority, settlement authority, or real-money authority.
    """

    baseline = decide_provider_output_use(
        authority,
        request,
        decided_at=decided_at,
    )
    if baseline.reason != "OWNER_APPROVAL_UNRESOLVED":
        return baseline
    if not isinstance(owner_approval_store, OwnerApprovalStore):
        raise ValueError("owner_approval_store must be OwnerApprovalStore")

    identity = dict(
        governance_authority_id=authority.authority_id,
        provider_id=authority.provider_id,
        service_id=authority.service_id,
        owner_approval_reference=authority.owner_approval_reference,
        owner_approval_sha256=authority.owner_approval_sha256,
    )
    acquisition_resolution = owner_approval_store.resolve(
        **identity,
        as_of=request.acquired_at,
    )
    if not acquisition_resolution.approved:
        return _issue_provider_output_use_decision(
            allowed=False,
            reason=_owner_approval_reason(
                acquisition_resolution.reason,
                phase="AT_ACQUISITION",
            ),
            authority_id=baseline.authority_id,
            artifact_sha256=baseline.artifact_sha256,
            purpose=baseline.purpose,
            artifact_class=baseline.artifact_class,
            decided_at=baseline.decided_at,
            retention_policy=baseline.retention_policy,
            max_retention_seconds=baseline.max_retention_seconds,
        )

    decision_resolution = owner_approval_store.resolve(
        **identity,
        as_of=baseline.decided_at,
    )
    if not decision_resolution.approved:
        return _issue_provider_output_use_decision(
            allowed=False,
            reason=_owner_approval_reason(
                decision_resolution.reason,
                phase="AT_DECISION",
            ),
            authority_id=baseline.authority_id,
            artifact_sha256=baseline.artifact_sha256,
            purpose=baseline.purpose,
            artifact_class=baseline.artifact_class,
            decided_at=baseline.decided_at,
            retention_policy=baseline.retention_policy,
            max_retention_seconds=baseline.max_retention_seconds,
        )

    return _issue_provider_output_use_decision(
        allowed=True,
        reason="ALLOWED",
        authority_id=baseline.authority_id,
        artifact_sha256=baseline.artifact_sha256,
        purpose=baseline.purpose,
        artifact_class=baseline.artifact_class,
        decided_at=baseline.decided_at,
        retention_policy=baseline.retention_policy,
        max_retention_seconds=baseline.max_retention_seconds,
    )
