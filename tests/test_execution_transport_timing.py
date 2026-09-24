from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json

import pytest

from autosport.execution_transport_timing import (
    MAX_ELAPSED_NS,
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


class RaiseOnSecondClock:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        if self.calls == 1:
            return 10
        raise RuntimeError("clock unavailable")


def _measure(*, clock, operation, request: bytes = b'{"x":1}'):
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

    measurement = _measure(clock=FakeClock(100, 125), operation=operation)

    assert calls == 1
    assert measurement.unwrap() == b'{"ok":true}'
    assert measurement.transport_error is None
    assert measurement.timing_available is True
    witness = measurement.witness
    assert witness is not None
    assert witness.outcome is TransportRoundTripOutcome.RETURNED
    assert witness.elapsed_ns == 25
    assert witness.request_sha256 == sha256(b'{"x":1}').hexdigest()
    assert witness.response_sha256 == sha256(b'{"ok":true}').hexdigest()
    assert witness.clock_source == TEST_CLOCK_SOURCE
    assert witness.clock_is_product_default is False
    assert witness.canonical_transport_binding_proven is False
    assert witness.provider_acceptance_proven is False
    assert witness.external_effect_proven is False
    assert witness.timing_scope == TIMING_SCOPE
    assert "start" not in witness.to_dict()
    assert "end" not in witness.to_dict()


def test_successful_tuple_unpacking_remains_compatible() -> None:
    response, witness = _measure(
        clock=FakeClock(5, 5),
        operation=lambda: b"ok",
    )
    assert response == b"ok"
    assert witness is not None
    assert witness.elapsed_ns == 0


@pytest.mark.parametrize(
    ("ticks", "reason"),
    (
        ((20, 19), "CLOCK_MOVED_BACKWARDS"),
        ((0, MAX_ELAPSED_NS + 1), "DURATION_OVERFLOW"),
        ((1, True), "END_CLOCK_INVALID"),
    ),
)
def test_bad_end_clock_cannot_convert_successful_transport_to_failure(
    ticks: tuple[object, object],
    reason: str,
) -> None:
    measurement = _measure(
        clock=FakeClock(*ticks),
        operation=lambda: b"provider-response",
    )

    assert measurement.unwrap() == b"provider-response"
    assert measurement.transport_error is None
    assert measurement.witness is None
    assert measurement.timing_available is False
    assert measurement.timing_unavailable_reason == reason


def test_end_clock_exception_cannot_convert_successful_transport_to_failure() -> None:
    measurement = _measure(
        clock=RaiseOnSecondClock(),
        operation=lambda: b"provider-response",
    )

    assert measurement.unwrap() == b"provider-response"
    assert measurement.witness is None
    assert measurement.timing_unavailable_reason == "END_CLOCK_SAMPLE_FAILED"


def test_bad_start_clock_prevents_transport_call() -> None:
    called = False

    def operation() -> bytes:
        nonlocal called
        called = True
        return b"should-not-run"

    with pytest.raises(TransportTimingEvidenceError):
        _measure(clock=FakeClock(True), operation=operation)

    assert called is False


def test_timeout_preserves_exact_original_exception_and_timing() -> None:
    error = TimeoutError("ambiguous provider effect")

    def operation() -> bytes:
        raise error

    measurement = _measure(clock=FakeClock(10, 30), operation=operation)

    assert measurement.transport_error is error
    assert measurement.response is None
    witness = measurement.witness
    assert witness is not None
    assert witness.outcome is TransportRoundTripOutcome.TIMEOUT
    assert witness.elapsed_ns == 20
    assert witness.response_sha256 is None
    assert witness.external_effect_proven is False

    with pytest.raises(TimeoutError) as caught:
        measurement.unwrap()
    assert caught.value is error


def test_timeout_with_bad_end_clock_still_preserves_exact_timeout() -> None:
    error = TimeoutError("ambiguous provider effect")

    def operation() -> bytes:
        raise error

    measurement = _measure(clock=FakeClock(9, 8), operation=operation)

    assert measurement.transport_error is error
    assert measurement.witness is None
    assert measurement.timing_unavailable_reason == "CLOCK_MOVED_BACKWARDS"
    with pytest.raises(TimeoutError) as caught:
        measurement.unwrap()
    assert caught.value is error


def test_generic_transport_error_is_preserved_without_becoming_rejection() -> None:
    error = OSError("connection reset")

    def operation() -> bytes:
        raise error

    measurement = _measure(clock=FakeClock(7, 19), operation=operation)

    assert measurement.transport_error is error
    witness = measurement.witness
    assert witness is not None
    assert witness.outcome is TransportRoundTripOutcome.TRANSPORT_ERROR
    assert witness.elapsed_ns == 12
    assert witness.provider_acceptance_proven is False
    assert witness.external_effect_proven is False

    with pytest.raises(OSError) as caught:
        measurement.unwrap()
    assert caught.value is error


def test_generic_transport_error_with_bad_end_clock_is_not_masked() -> None:
    error = OSError("connection reset")

    def operation() -> bytes:
        raise error

    measurement = _measure(clock=RaiseOnSecondClock(), operation=operation)

    assert measurement.transport_error is error
    assert measurement.witness is None
    assert measurement.timing_unavailable_reason == "END_CLOCK_SAMPLE_FAILED"
    with pytest.raises(OSError) as caught:
        measurement.unwrap()
    assert caught.value is error


def test_invalid_response_type_is_transport_contract_error_not_timing_error() -> None:
    measurement = _measure(
        clock=FakeClock(1, 2),
        operation=lambda: bytearray(b"ok"),
    )

    assert measurement.response is None
    assert isinstance(measurement.transport_error, TypeError)
    witness = measurement.witness
    assert witness is not None
    assert witness.outcome is TransportRoundTripOutcome.INVALID_RESPONSE
    assert witness.elapsed_ns == 1
    assert witness.response_sha256 is None
    with pytest.raises(TypeError, match="non-bytes"):
        measurement.unwrap()


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


def test_direct_witness_construction_cannot_mint_product_clock_provenance() -> None:
    with pytest.raises(
        TransportTimingEvidenceError,
        match="must be product-issued",
    ):
        TransportRoundTripWitness(
            attempt_id="attempt-1",
            action_id="action-1",
            provider_id="betfair",
            account_id="account-1",
            request_sha256="a" * 64,
            outcome=TransportRoundTripOutcome.RETURNED,
            elapsed_ns=1,
            response_sha256="b" * 64,
            clock_source=PRODUCTION_CLOCK_SOURCE,
            clock_is_product_default=True,
        )


def test_copy_style_witness_forgery_is_rejected() -> None:
    measurement = _measure(
        clock=FakeClock(10, 12),
        operation=lambda: b"A",
        request=b"R",
    )
    witness = measurement.witness
    assert witness is not None

    with pytest.raises(
        TransportTimingEvidenceError,
        match="must be product-issued",
    ):
        replace(witness, elapsed_ns=999)

    with pytest.raises(
        TransportTimingEvidenceError,
        match="must be product-issued",
    ):
        replace(witness, provider_acceptance_proven=True)


def test_measurement_copy_cannot_mismatch_response_and_witness_digest() -> None:
    measurement = _measure(
        clock=FakeClock(10, 12),
        operation=lambda: b"A",
        request=b"R",
    )
    with pytest.raises(
        TransportTimingEvidenceError,
        match="response digest",
    ):
        replace(measurement, response=b"B")


def test_digest_is_deterministic_and_changes_with_bound_bytes() -> None:
    first = _measure(clock=FakeClock(10, 12), operation=lambda: b"A", request=b"R")
    same = _measure(clock=FakeClock(10, 12), operation=lambda: b"A", request=b"R")
    changed_request = _measure(
        clock=FakeClock(10, 12),
        operation=lambda: b"A",
        request=b"R2",
    )
    changed_response = _measure(
        clock=FakeClock(10, 12),
        operation=lambda: b"B",
        request=b"R",
    )

    assert first.witness is not None
    assert same.witness is not None
    assert changed_request.witness is not None
    assert changed_response.witness is not None
    assert first.witness.evidence_sha256 == same.witness.evidence_sha256
    assert first.witness.evidence_sha256 != changed_request.witness.evidence_sha256
    assert first.witness.evidence_sha256 != changed_response.witness.evidence_sha256


def test_rebinding_module_perf_counter_cannot_mint_fake_product_clock(
    monkeypatch,
) -> None:
    def forged_clock() -> int:
        raise AssertionError("mutable module clock must not be consulted")

    monkeypatch.setattr(
        "autosport.execution_transport_timing.time.perf_counter_ns",
        forged_clock,
    )

    measurement = measure_transport_round_trip(
        attempt_id="attempt-1",
        action_id="action-1",
        provider_id="betfair",
        account_id="account-1",
        request_body=b"req",
        operation=lambda: b"resp",
    )

    assert measurement.unwrap() == b"resp"
    witness = measurement.witness
    assert witness is not None
    assert witness.clock_source == PRODUCTION_CLOCK_SOURCE
    assert witness.clock_is_product_default is True
    assert witness.canonical_transport_binding_proven is False
    assert witness.provider_acceptance_proven is False
    assert witness.external_effect_proven is False


def test_injected_clock_is_always_test_only() -> None:
    measurement = _measure(
        clock=FakeClock(1000, 1010),
        operation=lambda: b"resp",
    )
    witness = measurement.witness
    assert witness is not None
    assert witness.clock_source == TEST_CLOCK_SOURCE
    assert witness.clock_is_product_default is False
    assert witness.elapsed_ns == 10


def test_issued_witness_revalidation_rejects_in_place_mutation() -> None:
    measurement = _measure(
        clock=FakeClock(10, 12),
        operation=lambda: b"A",
        request=b"R",
    )
    witness = measurement.witness
    assert witness is not None
    witness.assert_authoritative()

    object.__setattr__(witness, "elapsed_ns", 999)

    with pytest.raises(
        TransportTimingEvidenceError,
        match="changed after canonical issuance",
    ):
        witness.assert_authoritative()
    with pytest.raises(
        TransportTimingEvidenceError,
        match="changed after canonical issuance",
    ):
        _ = measurement.timing_available
    with pytest.raises(
        TransportTimingEvidenceError,
        match="changed after canonical issuance",
    ):
        replace(measurement)


def test_recomputed_digest_cannot_reseal_mutated_issued_witness() -> None:
    measurement = _measure(
        clock=FakeClock(20, 25),
        operation=lambda: b"response",
        request=b"request",
    )
    witness = measurement.witness
    assert witness is not None

    object.__setattr__(witness, "elapsed_ns", 777)
    forged_payload = witness.to_dict(include_evidence_sha256=False)
    forged_digest = sha256(
        json.dumps(
            forged_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    object.__setattr__(witness, "evidence_sha256", forged_digest)

    with pytest.raises(
        TransportTimingEvidenceError,
        match="changed after canonical issuance",
    ):
        witness.assert_authoritative()


def test_unmodified_issued_witness_remains_authoritative_through_measurement_use() -> None:
    measurement = _measure(
        clock=FakeClock(30, 44),
        operation=lambda: b"ok",
    )
    witness = measurement.witness
    assert witness is not None

    witness.assert_authoritative()
    assert measurement.timing_available is True
    response, unpacked = measurement
    assert response == b"ok"
    assert unpacked is witness
    witness.assert_authoritative()
