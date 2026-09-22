from __future__ import annotations

import hashlib

from autosport.provider_use_policy_evidence import (
    ProviderUseAuthorizationError,
    ProviderUseDecisionBasis,
    ProviderUsePolicyError,
    ProviderUsePolicyEvidence,
    ProviderUsePolicyStatus,
    ProviderUsePurpose,
    ProviderUsePurposeDecision,
    ProviderUseRequest,
    ProviderWrittenPermissionEvidence,
    evaluate_provider_use,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _decisions(
    *,
    target_status: ProviderUsePolicyStatus,
    target_basis: ProviderUseDecisionBasis = ProviderUseDecisionBasis.DOCUMENTED_POLICY_TEXT,
) -> tuple[ProviderUsePurposeDecision, ...]:
    decisions: list[ProviderUsePurposeDecision] = []
    for purpose in ProviderUsePurpose:
        if purpose is ProviderUsePurpose.MODEL_TRAINING:
            decisions.append(
                ProviderUsePurposeDecision(
                    purpose=purpose,
                    status=target_status,
                    source_section_ref="section:model-training",
                    basis=target_basis,
                    reviewer_decision_ref=(
                        "review:caller-authored"
                        if target_basis is ProviderUseDecisionBasis.REVIEWER_DECISION
                        else None
                    ),
                    reviewer_decision_sha256=(
                        _sha("caller-authored-review")
                        if target_basis is ProviderUseDecisionBasis.REVIEWER_DECISION
                        else None
                    ),
                )
            )
        else:
            decisions.append(
                ProviderUsePurposeDecision(
                    purpose=purpose,
                    status=ProviderUsePolicyStatus.UNRESOLVED_REQUIRES_REVIEW,
                    source_section_ref=f"section:{purpose.value}",
                )
            )
    return tuple(decisions)


def _policy(
    *,
    target_status: ProviderUsePolicyStatus,
    target_basis: ProviderUseDecisionBasis = ProviderUseDecisionBasis.DOCUMENTED_POLICY_TEXT,
) -> ProviderUsePolicyEvidence:
    return ProviderUsePolicyEvidence(
        provider_id="matchbook",
        source_url="https://developers.matchbook.com/docs/fair-usage-policy",
        document_sha256=_sha("caller-authored-policy-bytes"),
        observed_at_utc="2026-09-22T06:00:00Z",
        effective_at_utc="2026-09-22T06:00:00Z",
        review_due_at_utc="2026-10-22T06:00:00Z",
        decisions=_decisions(
            target_status=target_status,
            target_basis=target_basis,
        ),
    )


def _request() -> ProviderUseRequest:
    return ProviderUseRequest(
        provider_id="matchbook",
        purpose=ProviderUsePurpose.MODEL_TRAINING,
        use_at_utc="2026-09-22T07:00:00Z",
    )


def _assert_caller_only_path_fails_closed(build_authorization) -> None:
    try:
        authorization = build_authorization()
    except (ProviderUsePolicyError, ProviderUseAuthorizationError):
        return
    assert not authorization.authorized, (
        "ordinary caller-authored policy/reviewer/permission objects minted a "
        "positive provider-use authorization without product-owned origin authority"
    )


def test_caller_authored_documented_policy_cannot_mint_authorization() -> None:
    """A digest of caller-selected bytes is identity, not policy-origin proof."""

    def build():
        return evaluate_provider_use(
            policy=_policy(
                target_status=ProviderUsePolicyStatus.ALLOWED_BY_DOCUMENTED_POLICY
            ),
            request=_request(),
        )

    _assert_caller_only_path_fails_closed(build)


def test_caller_authored_reviewer_reference_cannot_mint_authorization() -> None:
    """A reviewer ref + SHA must resolve to an issued review, not self-assert authority."""

    def build():
        return evaluate_provider_use(
            policy=_policy(
                target_status=ProviderUsePolicyStatus.ALLOWED_BY_DOCUMENTED_POLICY,
                target_basis=ProviderUseDecisionBasis.REVIEWER_DECISION,
            ),
            request=_request(),
        )

    _assert_caller_only_path_fails_closed(build)


def test_caller_authored_written_permission_cannot_mint_authorization() -> None:
    """Hash-bound permission fields still need provider/product-owned issuance origin."""

    def build():
        policy = _policy(
            target_status=ProviderUsePolicyStatus.REQUIRES_WRITTEN_PERMISSION
        )
        permission = ProviderWrittenPermissionEvidence(
            provider_id="matchbook",
            policy_evidence_id=policy.evidence_id,
            permission_ref="permission:caller-authored",
            document_sha256=_sha("caller-authored-permission-bytes"),
            observed_at_utc="2026-09-22T06:00:00Z",
            valid_from_utc="2026-09-22T06:00:00Z",
            valid_until_utc="2026-09-22T08:00:00Z",
            purposes=(ProviderUsePurpose.MODEL_TRAINING,),
        )
        return evaluate_provider_use(
            policy=policy,
            request=_request(),
            written_permission=permission,
        )

    _assert_caller_only_path_fails_closed(build)
