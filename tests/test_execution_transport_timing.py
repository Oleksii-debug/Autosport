from __future__ import annotations

from hashlib import sha256

import pytest

from autosport.execution_transport_timing import (
    MAX_ELAPSED_NS,
    MeasuredTransportFailure,
    PRODUCTION_CLOCK_SOURCE,
    TEST_CLOCK_SOURCE,
    TIMING_SCOPE,
    TransportRoundTripOutcome,
    TransportRoundTripWitness,
    TransportTimingEvidenceError,
    measure_transport_round_trip,
)


class FakeClock:
    def __init__(self, *ticks: object) -> None:
        self._ticks = list(ticks)
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        return self._ticks.pop(0)


def _measure(*, clock: FakeClock, operation, request: bytes = b'{"x":1}'):
    return measure_transport_round_trip(
        attempt_id="attempt-1",
        action_id="action-1",
        provider_id="betfair",
        account_id="account-1",
        request_body=request,
        operation=operation,
        monotonic_ns=clock,
    )


def test_returned_witness_binds_exact_request_response_and_duration() -> None:
    calls = 0

    def operation() -> bytes:
        nonlocal calls
        calls += 1
        return b'{"ok":true}'

    response, witness = _measure(clock=FakeClock(100, 125), operation=operation)

    assert calls == 1
    assert response == b'{"ok":true}'
    assert witness.outcome is TransportRoundTripOutcome.RETURNED
    assert witness.elapsed_ns == 25
    assert witness.request_sha256 == sha256(b'{"x":1}').hexdigest()
    assert witness.response_sha256 == sha256(response).hexdigest()
    assert witness.clock_source == TEST_CLOCK_SOURCE
    assert witness.clock_is_product_default is False
    assert witness.canonical_transport_binding_proven is False
    assert witness.timing_scope == TIMING_SCOPE
    assert witness.provider_acceptance_proven is False
    assert witness.external_effect_proven is False
    assert "start" not in witness.to_dict()
    assert "end" not in witness.to_dict()


def test_zero_duration_is_valid() -> None:
    _, witness = _measure(clock=FakeClock(5, 5), operation=lambda: b"ok")
    assert witness.elapsed_ns == 0


def test_timeout_preserves_duration_but_not_external_effect_truth() -> None:
    def operation() -> bytes:
        raise TimeoutError("ambiguous provider effect")

    with pytest.raises(MeasuredTransportFailure) as caught:
        _measure(clock=FakeClock(10, 30), operation=operation)

    witness = caught.value.witness
    assert witness.outcome is TransportRoundTripOutcome.TIMEOUT
    assert witness.elapsed_ns == 20
    assert witness.response_sha256 is None
    assert witness.external_effect_proven is False
    assert isinstance(caught.value.__cause__, TimeoutError)


def test_generic_transport_error_is_measured_without_becoming_rejection() -> None:
    def operation() -> bytes:
        raise OSError("connection reset")

    with pytest.raises(MeasuredTransportFailure) as caught:
        _measure(clock=FakeClock(7, 19), operation=operation)

    witness = caught.value.witness
    assert witness.outcome is TransportRoundTripOutcome.TRANSPORT_ERROR
    assert witness.elapsed_ns == 12
    assert witness.provider_acceptance_proven is False
    assert witness.external_effect_proven is False


def test_invalid_response_type_is_measured_and_fail_closed() -> None:
    with pytest.raises(MeasuredTransportFailure) as caught:
        _measure(clock=FakeClock(1, 2), operation=lambda: bytearray(b"ok"))

    witness = caught.value.witness
    assert witness.outcome is TransportRoundTripOutcome.INVALID_RESPONSE
    assert witness.elapsed_ns == 1
    assert witness.response_sha256 is None


def test_backwards_clock_fails_closed() -> None:
    with pytest.raises(TransportTimingEvidenceError, match="moved backwards"):
        _measure(clock=FakeClock(20, 19), operation=lambda: b"ok")


def test_bool_and_non_integer_ticks_are_rejected() -> None:
    called = False

    def operation() -> bytes:
        nonlocal called
        called = True
        return b"ok"

    with pytest.raises(TransportTimingEvidenceError):
        _measure(clock=FakeClock(True), operation=operation)
    assert called is False

    with pytest.raises(TransportTimingEvidenceError):
        _measure(clock=FakeClock(1, 1.5), operation=lambda: b"ok")


def test_elapsed_overflow_is_rejected() -> None:
    with pytest.raises(TransportTimingEvidenceError, match="exceeds int64"):
        _measure(clock=FakeClock(0, MAX_ELAPSED_NS + 1), operation=lambda: b"ok")


def test_request_must_be_exact_bytes_before_operation_runs() -> None:
    called = False

    def operation() -> bytes:
        nonlocal called
        called = True
        return b"ok"

    with pytest.raises(TypeError, match="exact bytes"):
        measure_transport_round_trip(
            attempt_id="attempt-1",
            action_id="action-1",
            provider_id="betfair",
            account_id="account-1",
            request_body=bytearray(b"secret"),
            operation=operation,
        )
    assert called is False


def test_injected_clock_cannot_be_relabelled_as_product_default() -> None:
    with pytest.raises(TransportTimingEvidenceError, match="default marker"):
        TransportRoundTripWitness(
            attempt_id="attempt-1",
            action_id="action-1",
            provider_id="betfair",
            account_id="account-1",
            request_sha256="a" * 64,
            outcome=TransportRoundTripOutcome.RETURNED,
            elapsed_ns=1,
            response_sha256="b" * 64,
            clock_source=TEST_CLOCK_SOURCE,
            clock_is_product_default=True,
        )


def test_witness_cannot_claim_provider_acceptance_or_external_effect() -> None:
    for field in (
        "canonical_transport_binding_proven",
        "provider_acceptance_proven",
        "external_effect_proven",
    ):
        kwargs = {
            "attempt_id": "attempt-1",
            "action_id": "action-1",
            "provider_id": "betfair",
            "account_id": "account-1",
            "request_sha256": "a" * 64,
            "outcome": TransportRoundTripOutcome.RETURNED,
            "elapsed_ns": 1,
            "response_sha256": "b" * 64,
            "clock_source": TEST_CLOCK_SOURCE,
            "clock_is_product_default": False,
            field: True,
        }
        with pytest.raises(TransportTimingEvidenceError):
            TransportRoundTripWitness(**kwargs)


def test_nonreturned_outcome_cannot_claim_response_digest() -> None:
    with pytest.raises(TransportTimingEvidenceError, match="cannot claim a response digest"):
        TransportRoundTripWitness(
            attempt_id="attempt-1",
            action_id="action-1",
            provider_id="betfair",
            account_id="account-1",
            request_sha256="a" * 64,
            outcome=TransportRoundTripOutcome.TIMEOUT,
            elapsed_ns=1,
            response_sha256="b" * 64,
            clock_source=TEST_CLOCK_SOURCE,
            clock_is_product_default=False,
        )


def test_digest_is_deterministic_and_changes_with_bound_bytes() -> None:
    _, first = _measure(clock=FakeClock(10, 12), operation=lambda: b"A", request=b"R")
    _, same = _measure(clock=FakeClock(10, 12), operation=lambda: b"A", request=b"R")
    _, changed_request = _measure(
        clock=FakeClock(10, 12), operation=lambda: b"A", request=b"R2"
    )
    _, changed_response = _measure(
        clock=FakeClock(10, 12), operation=lambda: b"B", request=b"R"
    )

    assert first.evidence_sha256 == same.evidence_sha256
    assert first.evidence_sha256 != changed_request.evidence_sha256
    assert first.evidence_sha256 != changed_response.evidence_sha256


def test_default_clock_is_product_default_but_not_transport_authority(monkeypatch) -> None:
    ticks = iter((1000, 1010))
    monkeypatch.setattr(
        "autosport.execution_transport_timing.time.perf_counter_ns",
        lambda: next(ticks),
    )
    _, witness = measure_transport_round_trip(
        attempt_id="attempt-1",
        action_id="action-1",
        provider_id="betfair",
        account_id="account-1",
        request_body=b"req",
        operation=lambda: b"resp",
    )
    assert witness.clock_source == PRODUCTION_CLOCK_SOURCE
    assert witness.clock_is_product_default is True
    assert witness.canonical_transport_binding_proven is False
    assert witness.elapsed_ns == 10


def test_error_path_with_untrustworthy_end_clock_does_not_emit_witness() -> None:
    def operation() -> bytes:
        raise TimeoutError("ambiguous")

    with pytest.raises(TransportTimingEvidenceError, match="moved backwards") as caught:
        _measure(clock=FakeClock(9, 8), operation=operation)
    assert isinstance(caught.value.__cause__, TimeoutError)
