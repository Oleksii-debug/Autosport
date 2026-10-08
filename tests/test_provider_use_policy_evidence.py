from __future__ import annotations

import hashlib

import pytest

from autosport.provider_use_policy_evidence import (
    ProviderUseAuthorizationError,
    ProviderUseAuthorizationState,
    ProviderUseDecisionBasis,
    ProviderUsePolicyError,
    ProviderUsePolicyEvidence,
    ProviderUsePolicyStatus,
    ProviderUsePurpose,
    ProviderUsePurposeDecision,
    ProviderUseRequest,
    ProviderWrittenPermissionEvidence,
    evaluate_provider_use,
    require_policy_successor,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _decisions(**overrides: ProviderUsePolicyStatus) -> tuple[ProviderUsePurposeDecision, ...]:
    out = []
    for purpose in ProviderUsePurpose:
        status = overrides.get(purpose.name, ProviderUsePolicyStatus.UNRESOLVED_REQUIRES_REVIEW)
        out.append(
            ProviderUsePurposeDecision(
                purpose=purpose,
                status=status,
                source_section_ref=f"section:{purpose.value}",
            )
        )
    return tuple(reversed(out))


def _policy(
    *,
    provider_id: str = "matchbook",
    decisions: tuple[ProviderUsePurposeDecision, ...] | None = None,
    observed_at: str = "2026-09-22T05:00:00Z",
    effective_at: str | None = "2026-09-22T00:00:00Z",
    review_due: str = "2026-10-22T05:00:00Z",
    account_scope: str | None = "account:primary",
    application_scope: str | None = "app:autosport",
    jurisdiction_scope: str | None = "jurisdiction:account",
    supersedes: str | None = None,
    document_label: str = "policy-v1",
) -> ProviderUsePolicyEvidence:
    if decisions is None:
        decisions = _decisions(
            EXECUTION_RUNTIME=ProviderUsePolicyStatus.ALLOWED_BY_DOCUMENTED_POLICY,
            INTERNAL_ANALYSIS=ProviderUsePolicyStatus.ALLOWED_BY_DOCUMENTED_POLICY,
            MODEL_TRAINING=ProviderUsePolicyStatus.UNRESOLVED_REQUIRES_REVIEW,
            RETENTION=ProviderUsePolicyStatus.UNRESOLVED_REQUIRES_REVIEW,
            REDISTRIBUTION=ProviderUsePolicyStatus.REQUIRES_WRITTEN_PERMISSION,
            BENCHMARKING=ProviderUsePolicyStatus.REQUIRES_WRITTEN_PERMISSION,
            THIRD_PARTY_EXPORT=ProviderUsePolicyStatus.REQUIRES_WRITTEN_PERMISSION,
        )
    return ProviderUsePolicyEvidence(
        provider_id=provider_id,
        source_url="https://developers.matchbook.com/docs/fair-usage-policy",
        document_sha256=_sha(document_label),
        observed_at_utc=observed_at,
        effective_at_utc=effective_at,
        review_due_at_utc=review_due,
        decisions=decisions,
        account_scope_ref=account_scope,
        application_scope_ref=application_scope,
        jurisdiction_scope_ref=jurisdiction_scope,
        license_ref="license:account-plan",
        supersedes_evidence_id=supersedes,
    )


def _request(
    purpose: ProviderUsePurpose,
    *,
    provider_id: str = "matchbook",
    use_at: str = "2026-09-23T00:00:00Z",
    account_scope: str | None = "account:primary",
    application_scope: str | None = "app:autosport",
    jurisdiction_scope: str | None = "jurisdiction:account",
) -> ProviderUseRequest:
    return ProviderUseRequest(
        provider_id=provider_id,
        purpose=purpose,
        use_at_utc=use_at,
        account_scope_ref=account_scope,
        application_scope_ref=application_scope,
        jurisdiction_scope_ref=jurisdiction_scope,
    )


def _permission(
    policy: ProviderUsePolicyEvidence,
    *,
    purposes: tuple[ProviderUsePurpose, ...] = (ProviderUsePurpose.REDISTRIBUTION,),
    observed_at: str = "2026-09-22T06:00:00Z",
    valid_from: str = "2026-09-22T06:00:00Z",
    valid_until: str | None = "2026-09-30T00:00:00Z",
    provider_id: str = "matchbook",
    policy_evidence_id: str | None = None,
    account_scope: str | None = "account:primary",
    application_scope: str | None = "app:autosport",
    jurisdiction_scope: str | None = "jurisdiction:account",
) -> ProviderWrittenPermissionEvidence:
    return ProviderWrittenPermissionEvidence(
        provider_id=provider_id,
        policy_evidence_id=policy_evidence_id or policy.evidence_id,
        permission_ref="permission:mb-2026-001",
        document_sha256=_sha("permission-doc"),
        observed_at_utc=observed_at,
        valid_from_utc=valid_from,
        valid_until_utc=valid_until,
        purposes=purposes,
        account_scope_ref=account_scope,
        application_scope_ref=application_scope,
        jurisdiction_scope_ref=jurisdiction_scope,
    )


def test_policy_canonicalizes_decision_order_and_timezone() -> None:
    a = _policy()
    b = _policy(
        observed_at="2026-09-22T07:00:00+02:00",
        decisions=tuple(reversed(a.decisions)),
    )
    assert a == b
    assert a.evidence_id == b.evidence_id
    assert a.observed_at_utc == "2026-09-22T05:00:00Z"


def test_policy_requires_explicit_classification_of_every_purpose() -> None:
    decisions = _decisions()[:-1]
    with pytest.raises(ProviderUsePolicyError, match="every provider-use purpose"):
        _policy(decisions=decisions)


def test_policy_rejects_duplicate_purpose_decision() -> None:
    decisions = _decisions()
    with pytest.raises(ProviderUsePolicyError, match="duplicated"):
        _policy(decisions=decisions + (decisions[0],))


@pytest.mark.parametrize(
    "url",
    [
        "http://developers.matchbook.com/docs/fair-usage-policy",
        "https://user:secret@developers.matchbook.com/docs/fair-usage-policy",
        "https://developers.matchbook.com/docs/fair-usage-policy?token=secret",
        "https://developers.matchbook.com/docs/fair-usage-policy#section",
        "https://DEVELOPERS.matchbook.com/docs/fair-usage-policy",
    ],
)
def test_policy_source_url_rejects_secret_bearing_or_noncanonical_forms(url: str) -> None:
    with pytest.raises(ProviderUsePolicyError, match="source_url"):
        ProviderUsePolicyEvidence(
            provider_id="matchbook",
            source_url=url,
            document_sha256=_sha("policy"),
            observed_at_utc="2026-09-22T05:00:00Z",
            effective_at_utc="2026-09-22T00:00:00Z",
            review_due_at_utc="2026-10-22T05:00:00Z",
            decisions=_decisions(),
        )


def test_policy_rejects_review_deadline_before_observation() -> None:
    with pytest.raises(ProviderUsePolicyError, match="review_due"):
        _policy(review_due="2026-09-22T04:59:59Z")


def test_policy_rejects_effective_time_after_review_deadline() -> None:
    with pytest.raises(ProviderUsePolicyError, match="effective_at"):
        _policy(effective_at="2026-11-01T00:00:00Z")


def test_reviewer_interpretation_requires_exact_reference() -> None:
    with pytest.raises(ProviderUsePolicyError, match="reviewer"):
        ProviderUsePurposeDecision(
            purpose=ProviderUsePurpose.MODEL_TRAINING,
            status=ProviderUsePolicyStatus.ALLOWED_BY_DOCUMENTED_POLICY,
            source_section_ref="section:ambiguous",
            basis=ProviderUseDecisionBasis.REVIEWER_DECISION,
        )


def test_document_text_basis_cannot_smuggle_reviewer_reference() -> None:
    with pytest.raises(ProviderUsePolicyError, match="documented-policy"):
        ProviderUsePurposeDecision(
            purpose=ProviderUsePurpose.MODEL_TRAINING,
            status=ProviderUsePolicyStatus.UNRESOLVED_REQUIRES_REVIEW,
            source_section_ref="section:ambiguous",
            reviewer_decision_ref="review:123",
        )


def test_documented_allowed_purpose_authorizes_only_that_purpose() -> None:
    policy = _policy()
    allowed = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.INTERNAL_ANALYSIS),
    )
    benchmark = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.BENCHMARKING),
    )

    assert allowed.authorized is True
    assert allowed.state is ProviderUseAuthorizationState.AUTHORIZED_BY_DOCUMENTED_POLICY
    assert benchmark.authorized is False
    assert benchmark.state is ProviderUseAuthorizationState.WRITTEN_PERMISSION_REQUIRED


def test_api_capability_does_not_authorize_unresolved_model_training() -> None:
    policy = _policy()
    result = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.MODEL_TRAINING),
    )
    assert result.authorized is False
    assert result.state is ProviderUseAuthorizationState.REVIEW_REQUIRED


def test_prohibited_status_cannot_be_overridden_by_written_permission() -> None:
    decisions = _decisions(
        REDISTRIBUTION=ProviderUsePolicyStatus.PROHIBITED_BY_DOCUMENTED_POLICY
    )
    policy = _policy(decisions=decisions)
    permission = _permission(policy)

    result = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.REDISTRIBUTION),
        written_permission=permission,
    )

    assert result.authorized is False
    assert result.state is ProviderUseAuthorizationState.PROHIBITED_BY_DOCUMENTED_POLICY
    assert result.permission_evidence_id is None


def test_permission_required_stays_closed_without_written_evidence() -> None:
    policy = _policy()
    result = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.REDISTRIBUTION),
    )
    assert result.authorized is False
    assert result.state is ProviderUseAuthorizationState.WRITTEN_PERMISSION_REQUIRED


def test_exact_written_permission_authorizes_exact_purpose_scope_and_time() -> None:
    policy = _policy()
    permission = _permission(policy)
    result = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.REDISTRIBUTION),
        written_permission=permission,
    )

    assert result.authorized is True
    assert result.state is ProviderUseAuthorizationState.AUTHORIZED_BY_WRITTEN_PERMISSION
    assert result.permission_evidence_id == permission.permission_evidence_id
    result.require_authorized()


def test_permission_does_not_generalize_to_another_purpose() -> None:
    policy = _policy()
    permission = _permission(policy, purposes=(ProviderUsePurpose.REDISTRIBUTION,))
    result = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.BENCHMARKING),
        written_permission=permission,
    )
    assert result.authorized is False
    assert result.state is ProviderUseAuthorizationState.WRITTEN_PERMISSION_SCOPE_MISMATCH


def test_permission_must_bind_exact_policy_version() -> None:
    policy = _policy()
    permission = _permission(policy, policy_evidence_id=_sha("other-policy"))
    result = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.REDISTRIBUTION),
        written_permission=permission,
    )
    assert result.state is ProviderUseAuthorizationState.WRITTEN_PERMISSION_SCOPE_MISMATCH


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_scope", "account:other"),
        ("application_scope", "app:other"),
        ("jurisdiction_scope", "jurisdiction:other"),
    ],
)
def test_permission_must_match_exact_request_scope(field: str, value: str) -> None:
    policy = _policy()
    kwargs = {field: value}
    permission = _permission(policy, **kwargs)
    result = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.REDISTRIBUTION),
        written_permission=permission,
    )
    assert result.state is ProviderUseAuthorizationState.WRITTEN_PERMISSION_SCOPE_MISMATCH


def test_policy_scope_mismatch_fails_before_policy_status() -> None:
    policy = _policy()
    result = evaluate_provider_use(
        policy=policy,
        request=_request(
            ProviderUsePurpose.INTERNAL_ANALYSIS,
            account_scope="account:other",
        ),
    )
    assert result.authorized is False
    assert result.state is ProviderUseAuthorizationState.POLICY_SCOPE_MISMATCH


def test_policy_cannot_authorize_use_before_evidence_was_observed() -> None:
    policy = _policy()
    result = evaluate_provider_use(
        policy=policy,
        request=_request(
            ProviderUsePurpose.INTERNAL_ANALYSIS,
            use_at="2026-09-22T04:59:59Z",
        ),
    )
    assert result.state is ProviderUseAuthorizationState.POLICY_NOT_OBSERVED_AT_USE


def test_unknown_effective_time_fails_closed() -> None:
    policy = _policy(effective_at=None)
    result = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.INTERNAL_ANALYSIS),
    )
    assert result.state is ProviderUseAuthorizationState.POLICY_EFFECTIVE_TIME_UNRESOLVED


def test_future_effective_time_fails_closed() -> None:
    policy = _policy(effective_at="2026-09-24T00:00:00Z")
    result = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.INTERNAL_ANALYSIS),
    )
    assert result.state is ProviderUseAuthorizationState.POLICY_NOT_EFFECTIVE


def test_policy_review_expiry_fails_closed_even_for_documented_allowed_use() -> None:
    policy = _policy(review_due="2026-09-22T12:00:00Z")
    result = evaluate_provider_use(
        policy=policy,
        request=_request(
            ProviderUsePurpose.INTERNAL_ANALYSIS,
            use_at="2026-09-23T00:00:00Z",
        ),
    )
    assert result.state is ProviderUseAuthorizationState.POLICY_REVIEW_EXPIRED


def test_permission_must_be_observed_before_use() -> None:
    policy = _policy()
    permission = _permission(policy, observed_at="2026-09-24T00:00:00Z")
    result = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.REDISTRIBUTION),
        written_permission=permission,
    )
    assert result.state is ProviderUseAuthorizationState.WRITTEN_PERMISSION_NOT_OBSERVED_AT_USE


def test_permission_future_validity_and_expiry_fail_closed() -> None:
    policy = _policy()
    future = _permission(
        policy,
        observed_at="2026-09-22T06:00:00Z",
        valid_from="2026-09-24T00:00:00Z",
    )
    before = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.REDISTRIBUTION),
        written_permission=future,
    )
    assert before.state is ProviderUseAuthorizationState.WRITTEN_PERMISSION_NOT_EFFECTIVE

    expired = _permission(
        policy,
        valid_from="2026-09-22T06:00:00Z",
        valid_until="2026-09-22T12:00:00Z",
    )
    after = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.REDISTRIBUTION),
        written_permission=expired,
    )
    assert after.state is ProviderUseAuthorizationState.WRITTEN_PERMISSION_EXPIRED


def test_non_authorized_result_raises_only_when_consumer_requires_authority() -> None:
    policy = _policy()
    result = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.MODEL_TRAINING),
    )
    with pytest.raises(ProviderUseAuthorizationError, match="review_required"):
        result.require_authorized()


def test_policy_identity_changes_when_purpose_status_changes() -> None:
    original = _policy()
    changed = _policy(
        decisions=_decisions(
            EXECUTION_RUNTIME=ProviderUsePolicyStatus.ALLOWED_BY_DOCUMENTED_POLICY,
            INTERNAL_ANALYSIS=ProviderUsePolicyStatus.ALLOWED_BY_DOCUMENTED_POLICY,
            MODEL_TRAINING=ProviderUsePolicyStatus.ALLOWED_BY_DOCUMENTED_POLICY,
        )
    )
    assert original.evidence_id != changed.evidence_id


def test_historical_policy_is_immutable_when_successor_is_created() -> None:
    previous = _policy(document_label="policy-v1")
    previous_id = previous.evidence_id
    successor = _policy(
        observed_at="2026-09-24T05:00:00Z",
        effective_at="2026-09-24T00:00:00Z",
        review_due="2026-10-24T05:00:00Z",
        supersedes=previous.evidence_id,
        document_label="policy-v2",
    )

    require_policy_successor(previous=previous, successor=successor)

    assert previous.evidence_id == previous_id
    assert successor.evidence_id != previous.evidence_id


def test_successor_must_bind_exact_previous_policy() -> None:
    previous = _policy(document_label="policy-v1")
    successor = _policy(
        observed_at="2026-09-24T05:00:00Z",
        effective_at="2026-09-24T00:00:00Z",
        review_due="2026-10-24T05:00:00Z",
        supersedes=_sha("not-previous"),
        document_label="policy-v2",
    )
    with pytest.raises(ProviderUsePolicyError, match="does not bind"):
        require_policy_successor(previous=previous, successor=successor)


def test_authorization_binding_contains_ids_not_raw_policy_or_permission_documents() -> None:
    policy = _policy()
    permission = _permission(policy)
    result = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.REDISTRIBUTION),
        written_permission=permission,
    )
    payload = result.to_payload()

    assert payload["policy_evidence_id"] == policy.evidence_id
    assert payload["permission_evidence_id"] == permission.permission_evidence_id
    assert "source_url" not in payload
    assert "permission_ref" not in payload
    assert "document_sha256" not in payload
    assert "session-token" not in str(payload)


def test_error_messages_do_not_echo_secret_like_bad_reference() -> None:
    secret = "token=VERY-SECRET-VALUE"
    with pytest.raises(ProviderUsePolicyError) as exc:
        ProviderWrittenPermissionEvidence(
            provider_id="matchbook",
            policy_evidence_id=_sha("policy"),
            permission_ref=secret,
            document_sha256=_sha("doc"),
            observed_at_utc="2026-09-22T06:00:00Z",
            valid_from_utc="2026-09-22T06:00:00Z",
            valid_until_utc=None,
            purposes=(ProviderUsePurpose.REDISTRIBUTION,),
        )
    assert "VERY-SECRET-VALUE" not in str(exc.value)


def test_reviewer_decision_is_hash_bound_into_policy_identity() -> None:
    reviewer_decision = ProviderUsePurposeDecision(
        purpose=ProviderUsePurpose.MODEL_TRAINING,
        status=ProviderUsePolicyStatus.ALLOWED_BY_DOCUMENTED_POLICY,
        source_section_ref="section:ambiguous",
        basis=ProviderUseDecisionBasis.REVIEWER_DECISION,
        reviewer_decision_ref="review:legal-42",
        reviewer_decision_sha256=_sha("review-artifact"),
    )
    decisions = tuple(
        reviewer_decision if decision.purpose is ProviderUsePurpose.MODEL_TRAINING else decision
        for decision in _decisions()
    )
    policy = _policy(decisions=decisions)

    result = evaluate_provider_use(
        policy=policy,
        request=_request(ProviderUsePurpose.MODEL_TRAINING),
    )

    assert result.authorized is True
    assert "reviewer_decision_sha256" in policy.to_payload()["decisions"][4]
    changed = ProviderUsePurposeDecision(
        purpose=ProviderUsePurpose.MODEL_TRAINING,
        status=ProviderUsePolicyStatus.ALLOWED_BY_DOCUMENTED_POLICY,
        source_section_ref="section:ambiguous",
        basis=ProviderUseDecisionBasis.REVIEWER_DECISION,
        reviewer_decision_ref="review:legal-42",
        reviewer_decision_sha256=_sha("different-review-artifact"),
    )
    changed_decisions = tuple(
        changed if decision.purpose is ProviderUsePurpose.MODEL_TRAINING else decision
        for decision in _decisions()
    )
    assert _policy(decisions=changed_decisions).evidence_id != policy.evidence_id


def test_reviewer_reference_without_review_artifact_digest_fails_closed() -> None:
    with pytest.raises(ProviderUsePolicyError, match="reviewer_decision_sha256"):
        ProviderUsePurposeDecision(
            purpose=ProviderUsePurpose.MODEL_TRAINING,
            status=ProviderUsePolicyStatus.ALLOWED_BY_DOCUMENTED_POLICY,
            source_section_ref="section:ambiguous",
            basis=ProviderUseDecisionBasis.REVIEWER_DECISION,
            reviewer_decision_ref="review:legal-42",
        )


def test_review_due_boundary_is_exclusive() -> None:
    policy = _policy(review_due="2026-09-23T00:00:00Z")
    result = evaluate_provider_use(
        policy=policy,
        request=_request(
            ProviderUsePurpose.INTERNAL_ANALYSIS,
            use_at="2026-09-23T00:00:00Z",
        ),
    )
    assert result.state is ProviderUseAuthorizationState.POLICY_REVIEW_EXPIRED


def test_written_permission_valid_until_boundary_is_exclusive() -> None:
    policy = _policy()
    permission = _permission(
        policy,
        valid_from="2026-09-22T06:00:00Z",
        valid_until="2026-09-23T00:00:00Z",
    )
    result = evaluate_provider_use(
        policy=policy,
        request=_request(
            ProviderUsePurpose.REDISTRIBUTION,
            use_at="2026-09-23T00:00:00Z",
        ),
        written_permission=permission,
    )
    assert result.state is ProviderUseAuthorizationState.WRITTEN_PERMISSION_EXPIRED
