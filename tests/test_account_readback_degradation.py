from __future__ import annotations

import dataclasses
import hashlib
import re

import pytest

TEST_SCOPE_SHA256 = hashlib.sha256(b"test-account-read-scope").hexdigest()
ALT_SCOPE_SHA256 = hashlib.sha256(b"alternate-account-read-scope").hexdigest()


from autosport.account_readback_degradation import (
    AccountReadbackDegradationEvidence,
    ReadbackDegradationError,
    ReadbackDegradationState,
    ReadbackFailureKind,
    ReadbackFailureSignal,
    classify_account_readback_degradation,
)


def _signal(**overrides: object) -> ReadbackFailureSignal:
    values: dict[str, object] = {
        "provider_id": "betfair",
        "adapter_id": "betfair-readonly",
        "operation": "listCurrentOrders",
        "scope_sha256": TEST_SCOPE_SHA256,
        "kind": ReadbackFailureKind.PROVIDER_ERROR,
        "provider_code": "TIMEOUT_ERROR",
    }
    values.update(overrides)
    return ReadbackFailureSignal(**values)  # type: ignore[arg-type]


def _assert_never_positive(result: AccountReadbackDegradationEvidence) -> None:
    assert result.freshness_proven is False
    assert result.fresh_complete_proven is False
    assert result.empty_scope_proven is False
    assert result.current_balance_proven is False
    assert result.current_exposure_proven is False
    assert result.may_release_contingent_exposure is False
    assert result.may_authorize_provider_failover is False
    assert result.may_authorize_new_stake is False


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("INVALID_SESSION_INFORMATION", ReadbackDegradationState.UNAUTHORIZED_OR_SESSION_EXPIRED),
        ("NO_SESSION", ReadbackDegradationState.UNAUTHORIZED_OR_SESSION_EXPIRED),
        ("INVALID_APP_KEY", ReadbackDegradationState.CONFIGURATION_DENIED),
        ("NO_APP_KEY", ReadbackDegradationState.CONFIGURATION_DENIED),
        ("ACCESS_DENIED", ReadbackDegradationState.CONFIGURATION_DENIED),
        ("SUBSCRIPTION_EXPIRED", ReadbackDegradationState.CONFIGURATION_DENIED),
        ("INVALID_SUBSCRIPTION_TOKEN", ReadbackDegradationState.CONFIGURATION_DENIED),
        ("USER_NOT_SUBSCRIBED", ReadbackDegradationState.CONFIGURATION_DENIED),
        ("TOO_MANY_REQUESTS", ReadbackDegradationState.UNAVAILABLE_TRANSIENT),
        ("SERVICE_BUSY", ReadbackDegradationState.UNAVAILABLE_TRANSIENT),
        ("TIMEOUT_ERROR", ReadbackDegradationState.UNAVAILABLE_TRANSIENT),
    ],
)
def test_betfair_provider_codes_are_fail_closed(
    code: str, expected: ReadbackDegradationState
) -> None:
    result = classify_account_readback_degradation(_signal(provider_code=code))
    assert result.state is expected
    _assert_never_positive(result)


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_http_failures_never_mean_empty(status: int) -> None:
    result = classify_account_readback_degradation(
        _signal(provider_id="betdaq", provider_code=None, http_status=status)
    )
    assert result.state is ReadbackDegradationState.UNAVAILABLE_TRANSIENT
    _assert_never_positive(result)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_http_failures_are_explicit_not_empty(status: int) -> None:
    result = classify_account_readback_degradation(
        _signal(provider_id="betdaq", provider_code=None, http_status=status)
    )
    assert result.state is ReadbackDegradationState.UNAUTHORIZED_OR_SESSION_EXPIRED
    _assert_never_positive(result)


def test_mid_pagination_failure_preserves_partial_observation_without_completeness() -> None:
    result = classify_account_readback_degradation(
        _signal(
            kind=ReadbackFailureKind.INCOMPLETE_PAGINATION,
            provider_code=None,
            pages_completed=1,
        )
    )
    assert result.state is ReadbackDegradationState.INCOMPLETE_PARTIAL
    assert result.partial_observation_present is True
    _assert_never_positive(result)


def test_incomplete_pagination_requires_completed_prefix() -> None:
    with pytest.raises(ReadbackDegradationError):
        _signal(
            kind=ReadbackFailureKind.INCOMPLETE_PAGINATION,
            provider_code=None,
            pages_completed=0,
        )


def test_mid_pagination_timeout_preserves_both_outage_and_partial_prefix() -> None:
    result = classify_account_readback_degradation(
        _signal(
            kind=ReadbackFailureKind.INCOMPLETE_PAGINATION,
            provider_code="TIMEOUT_ERROR",
            pages_completed=1,
        )
    )
    assert result.state is ReadbackDegradationState.UNAVAILABLE_TRANSIENT
    assert result.partial_observation_present is True
    _assert_never_positive(result)


def test_mid_pagination_auth_expiry_remains_explicitly_unauthorized() -> None:
    result = classify_account_readback_degradation(
        _signal(
            kind=ReadbackFailureKind.INCOMPLETE_PAGINATION,
            provider_code="NO_SESSION",
            pages_completed=1,
        )
    )
    assert result.state is ReadbackDegradationState.UNAUTHORIZED_OR_SESSION_EXPIRED
    assert result.partial_observation_present is True
    _assert_never_positive(result)


def test_stale_last_known_requires_prior_snapshot_identity() -> None:
    with_prior = classify_account_readback_degradation(
        _signal(
            kind=ReadbackFailureKind.STALE_CACHE,
            provider_code=None,
            prior_snapshot_id="snapshot-0001",
        )
    )
    assert with_prior.state is ReadbackDegradationState.STALE_LAST_KNOWN
    assert with_prior.prior_snapshot_id == "snapshot-0001"
    _assert_never_positive(with_prior)

    with pytest.raises(ReadbackDegradationError):
        _signal(kind=ReadbackFailureKind.STALE_CACHE, provider_code=None)


def test_identity_conflict_is_not_reclassified_as_provider_outage() -> None:
    result = classify_account_readback_degradation(
        _signal(
            kind=ReadbackFailureKind.IDENTITY_CONFLICT,
            provider_code="TIMEOUT_ERROR",
            http_status=503,
        )
    )
    assert result.state is ReadbackDegradationState.CONFLICTING
    _assert_never_positive(result)


def test_malformed_response_remains_unknown_even_when_http_transport_succeeded() -> None:
    result = classify_account_readback_degradation(
        _signal(
            kind=ReadbackFailureKind.MALFORMED_RESPONSE,
            provider_code=None,
            http_status=200,
        )
    )
    assert result.state is ReadbackDegradationState.UNKNOWN
    _assert_never_positive(result)


def test_unknown_provider_code_stays_unknown() -> None:
    result = classify_account_readback_degradation(
        _signal(provider_code="SOME_FUTURE_ERROR")
    )
    assert result.state is ReadbackDegradationState.UNKNOWN
    _assert_never_positive(result)


def test_transport_failure_is_transient_even_without_http_status() -> None:
    result = classify_account_readback_degradation(
        _signal(kind=ReadbackFailureKind.TRANSPORT, provider_code=None)
    )
    assert result.state is ReadbackDegradationState.UNAVAILABLE_TRANSIENT
    _assert_never_positive(result)


def test_digest_is_deterministic_and_changes_with_authority_bearing_inputs() -> None:
    a = classify_account_readback_degradation(_signal())
    b = classify_account_readback_degradation(_signal())
    c = classify_account_readback_degradation(_signal(operation="getAccountFunds"))
    d = classify_account_readback_degradation(_signal(scope_sha256=ALT_SCOPE_SHA256))
    assert a.evidence_sha256 == b.evidence_sha256
    assert a.evidence_sha256 != c.evidence_sha256
    assert a.evidence_sha256 != d.evidence_sha256
    assert re.fullmatch(r"[0-9a-f]{64}", a.evidence_sha256)


def test_frozen_evidence_cannot_be_mutated_into_positive_truth() -> None:
    result = classify_account_readback_degradation(_signal())
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.state = ReadbackDegradationState.UNKNOWN  # type: ignore[misc]


def test_bool_is_rejected_as_http_status_and_page_count() -> None:
    with pytest.raises(ReadbackDegradationError):
        _signal(http_status=True)
    with pytest.raises(ReadbackDegradationError):
        _signal(pages_completed=False)


def test_unbounded_or_sensitive_shaped_tokens_are_rejected() -> None:
    with pytest.raises(ReadbackDegradationError):
        _signal(provider_id="betfair invalid material")
    with pytest.raises(ReadbackDegradationError):
        _signal(provider_code="TIMEOUT ERROR raw payload")


def test_scope_digest_must_be_exact_lowercase_sha256() -> None:
    with pytest.raises(ReadbackDegradationError):
        _signal(scope_sha256="abc")
    with pytest.raises(ReadbackDegradationError):
        _signal(scope_sha256="A" * 64)


def test_provider_id_case_alias_is_rejected_not_normalized() -> None:
    with pytest.raises(ReadbackDegradationError):
        _signal(provider_id="Betfair")


def test_direct_evidence_construction_cannot_mint_classifier_result() -> None:
    with pytest.raises(ReadbackDegradationError):
        AccountReadbackDegradationEvidence(
            state=ReadbackDegradationState.UNKNOWN,
            provider_id="betfair",
            adapter_id="betfair-readonly",
            operation="listCurrentOrders",
            scope_sha256=TEST_SCOPE_SHA256,
            provider_code=None,
            http_status=None,
            partial_observation_present=False,
            prior_snapshot_id=None,
            evidence_sha256=hashlib.sha256(b"forged-evidence").hexdigest(),
        )


def test_direct_non_exact_signal_subclass_is_rejected() -> None:
    class ForgedSignal(ReadbackFailureSignal):
        pass

    forged = ForgedSignal(
        provider_id="betfair",
        adapter_id="betfair-readonly",
        operation="listCurrentOrders",
        scope_sha256=TEST_SCOPE_SHA256,
        kind=ReadbackFailureKind.PROVIDER_ERROR,
        provider_code="TIMEOUT_ERROR",
    )
    with pytest.raises(ReadbackDegradationError):
        classify_account_readback_degradation(forged)
