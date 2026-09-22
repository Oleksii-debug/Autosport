from __future__ import annotations

import pytest

from autosport.betfair_account_readonly import BetfairReadOnlyError
from autosport.betfair_read_completeness import (
    BetfairObservationCompleteness,
    _classify_failure,
)


def _error(message: str, provider_error_code: object | None) -> BetfairReadOnlyError:
    exc = BetfairReadOnlyError(message)
    # #1259 adds this as a sanitized constructor field.  Keep this composition
    # regression runnable on the #1214 branch before that transport lineage is
    # reconverged by attaching the same public machine-readable attribute.
    exc.provider_error_code = provider_error_code  # type: ignore[attr-defined]
    return exc


@pytest.mark.parametrize(
    "provider_error_code",
    ("TOO_MANY_REQUESTS", "SERVICE_BUSY", "TIMEOUT_ERROR"),
)
def test_typed_transient_provider_code_beats_conflicting_auth_message(
    provider_error_code: str,
) -> None:
    completeness, reason = _classify_failure(
        _error("INVALID_APP_KEY", provider_error_code),
        partial=False,
    )

    assert completeness is BetfairObservationCompleteness.UNAVAILABLE_TRANSIENT
    assert reason == "provider_transient"


@pytest.mark.parametrize(
    "provider_error_code",
    ("INVALID_SESSION_INFORMATION", "INVALID_APP_KEY", "NO_SESSION", "NO_APP_KEY"),
)
def test_typed_auth_provider_code_beats_conflicting_transient_message(
    provider_error_code: str,
) -> None:
    completeness, reason = _classify_failure(
        _error("TOO_MANY_REQUESTS", provider_error_code),
        partial=False,
    )

    assert completeness is BetfairObservationCompleteness.AUTH_INVALID_OR_EXPIRED
    assert reason == "provider_auth"


@pytest.mark.parametrize("provider_error_code", ("SOME_NEW_CODE", 123, True))
def test_unknown_or_noncanonical_typed_provider_code_fails_closed_without_message_fallback(
    provider_error_code: object,
) -> None:
    completeness, reason = _classify_failure(
        _error("TOO_MANY_REQUESTS", provider_error_code),
        partial=False,
    )

    assert completeness is BetfairObservationCompleteness.INVALID_REQUEST_OR_CONTRACT
    assert reason == "provider_contract"


def test_no_typed_provider_code_retains_bounded_transport_fallback() -> None:
    completeness, reason = _classify_failure(
        _error("Betfair network request failed", None),
        partial=False,
    )

    assert completeness is BetfairObservationCompleteness.UNAVAILABLE_TRANSIENT
    assert reason == "provider_transient"


def test_partial_acquisition_stays_partial_even_with_typed_provider_failure() -> None:
    completeness, reason = _classify_failure(
        _error("ANGX-0007", "TOO_MANY_REQUESTS"),
        partial=True,
    )

    assert completeness is BetfairObservationCompleteness.PARTIAL
    assert reason == "acquisition_interrupted"
