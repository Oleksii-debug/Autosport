from __future__ import annotations

import pytest

from autosport.betfair_account_readonly import BetfairReadOnlyError
from autosport.betfair_read_completeness import (
    BetfairObservationCompleteness,
    _classify_failure,
)


@pytest.mark.parametrize(
    ("provider_error_code", "expected_failure_code"),
    [
        ("TIMEOUT_ERROR", "partial_provider_transient"),
        ("TOO_MANY_REQUESTS", "partial_provider_transient"),
        ("SERVICE_BUSY", "partial_provider_transient"),
        ("INVALID_SESSION_INFORMATION", "partial_provider_auth"),
        ("INVALID_APP_KEY", "partial_provider_auth"),
        ("UNRECOGNIZED_PROVIDER_CODE", "partial_provider_contract"),
    ],
)
def test_partial_read_preserves_typed_terminal_failure_cause(
    provider_error_code: str,
    expected_failure_code: str,
) -> None:
    error = BetfairReadOnlyError("opaque provider failure")
    error.provider_error_code = provider_error_code  # type: ignore[attr-defined]

    completeness, failure_code = _classify_failure(error, partial=True)

    assert completeness is BetfairObservationCompleteness.PARTIAL
    assert failure_code == expected_failure_code


@pytest.mark.parametrize(
    ("message", "expected_failure_code"),
    [
        ("Betfair network request failed", "partial_provider_transient"),
        ("Betfair HTTP request failed with status 503", "partial_provider_transient"),
        ("Betfair HTTP request failed with status 403", "partial_provider_auth"),
        ("malformed provider payload", "partial_provider_contract"),
    ],
)
def test_partial_read_preserves_legacy_terminal_failure_cause(
    message: str,
    expected_failure_code: str,
) -> None:
    completeness, failure_code = _classify_failure(
        BetfairReadOnlyError(message),
        partial=True,
    )

    assert completeness is BetfairObservationCompleteness.PARTIAL
    assert failure_code == expected_failure_code


def test_nonpartial_failure_classification_semantics_are_unchanged() -> None:
    error = BetfairReadOnlyError("opaque provider failure")
    error.provider_error_code = "INVALID_SESSION_INFORMATION"  # type: ignore[attr-defined]

    completeness, failure_code = _classify_failure(error, partial=False)

    assert completeness is BetfairObservationCompleteness.AUTH_INVALID_OR_EXPIRED
    assert failure_code == "provider_auth"
