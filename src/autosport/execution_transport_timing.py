"""Fail-closed local transport round-trip timing evidence.

This module measures only the synchronous local transport call boundary.  It does
not prove provider acceptance time, network one-way latency, an external effect,
or any execution outcome.  The production clock is ``time.perf_counter_ns``;
injected clocks are deliberately marked test-only so deterministic tests cannot
mint production timing evidence.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import json
import time
from typing import Callable
from weakref import ref

SCHEMA_VERSION = 1
TIMING_SCOPE = "transport_round_trip"
PRODUCTION_CLOCK_SOURCE = "PROCESS_PERF_COUNTER_NS"
TEST_CLOCK_SOURCE = "INJECTED_TEST_CLOCK"
MAX_ELAPSED_NS = (1 << 63) - 1

def _make_witness_issuance_gate():
    gate: ContextVar[bool] = ContextVar(
        "autosport_transport_timing_witness_issuance",
        default=False,
    )
    issued: dict[int, tuple[object, str]] = {}

    def active() -> bool:
        return gate.get()

    def issue(builder: Callable[[], "TransportRoundTripWitness"]):
        token = gate.set(True)
        try:
            witness = builder()
        finally:
            gate.reset(token)

        witness_key = id(witness)

        def forget(_weakref: object, *, key: int = witness_key) -> None:
            issued.pop(key, None)

        issued[witness_key] = (
            ref(witness, forget),
            witness.evidence_sha256,
        )
        return witness

    def assert_authoritative(witness: "TransportRoundTripWitness") -> None:
        if not isinstance(witness, TransportRoundTripWitness):
            raise TransportTimingEvidenceError(
                "transport timing witness type is not canonical"
            )
        record = issued.get(id(witness))
        if record is None or record[0]() is not witness:
            raise TransportTimingEvidenceError(
                "transport timing witness was not issued by the canonical measurement path"
            )
        current_fingerprint = _digest(
            witness.to_dict(include_evidence_sha256=False)
        )
        if (
            record[1] != current_fingerprint
            or witness.evidence_sha256 != current_fingerprint
        ):
            raise TransportTimingEvidenceError(
                "transport timing witness changed after canonical issuance"
            )

    return active, issue, assert_authoritative


(
    _witness_issuance_active,
    _issue_transport_timing_witness,
    _assert_transport_timing_witness_authoritative,
) = _make_witness_issuance_gate()
del _make_witness_issuance_gate


class TransportTimingEvidenceError(RuntimeError):
    """Transport timing evidence is malformed or cannot be measured safely."""


class TransportRoundTripOutcome(str, Enum):
    RETURNED = "RETURNED"
    TIMEOUT = "TIMEOUT"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise TransportTimingEvidenceError(f"{name} must be non-empty canonical text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TransportTimingEvidenceError(f"{name} must be UTF-8 encodable") from exc
    return value


def _sha(value: object, name: str) -> str:
    raw = _text(value, name)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise TransportTimingEvidenceError(f"{name} must be lowercase SHA-256 hex")
    return raw


def _tick(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise TransportTimingEvidenceError(f"{name} must be non-negative int")
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise TransportTimingEvidenceError("timing evidence is not canonical JSON") from exc


def _digest(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True)
class TransportRoundTripWitness:
    """One exact local synchronous transport-call timing witness.

    Raw monotonic epochs are intentionally not persisted.  ``elapsed_ns`` is the
    only clock-derived value because raw monotonic tick values are process-local
    implementation details and are not comparable across restart.
    """

    attempt_id: str
    action_id: str
    provider_id: str
    account_id: str
    request_sha256: str
    outcome: TransportRoundTripOutcome
    elapsed_ns: int
    response_sha256: str | None
    clock_source: str
    clock_is_product_default: bool
    schema_version: int = SCHEMA_VERSION
    timing_scope: str = TIMING_SCOPE
    canonical_transport_binding_proven: bool = False
    provider_acceptance_proven: bool = False
    external_effect_proven: bool = False
    evidence_sha256: str = field(init=False)

    def __post_init__(
        self,
        _issuance_active: Callable[[], bool] = _witness_issuance_active,
    ) -> None:
        if not _issuance_active():
            raise TransportTimingEvidenceError(
                "transport timing witness must be product-issued"
            )
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise TransportTimingEvidenceError(
                f"schema_version must be exactly {SCHEMA_VERSION}"
            )
        for name in ("attempt_id", "action_id", "provider_id", "account_id"):
            _text(getattr(self, name), name)
        _sha(self.request_sha256, "request_sha256")
        if type(self.outcome) is not TransportRoundTripOutcome:
            raise TransportTimingEvidenceError(
                "outcome must be TransportRoundTripOutcome"
            )
        _tick(self.elapsed_ns, "elapsed_ns")
        if self.elapsed_ns > MAX_ELAPSED_NS:
            raise TransportTimingEvidenceError("elapsed_ns exceeds int64")
        if self.response_sha256 is not None:
            _sha(self.response_sha256, "response_sha256")
        if self.clock_source not in {PRODUCTION_CLOCK_SOURCE, TEST_CLOCK_SOURCE}:
            raise TransportTimingEvidenceError("unsupported clock_source")
        if type(self.clock_is_product_default) is not bool:
            raise TransportTimingEvidenceError("clock_is_product_default must be bool")
        if self.clock_is_product_default != (self.clock_source == PRODUCTION_CLOCK_SOURCE):
            raise TransportTimingEvidenceError(
                "clock default marker must match the product-owned clock source"
            )
        if self.timing_scope != TIMING_SCOPE:
            raise TransportTimingEvidenceError(
                f"timing_scope must be exactly {TIMING_SCOPE!r}"
            )
        if (
            type(self.canonical_transport_binding_proven) is not bool
            or self.canonical_transport_binding_proven
        ):
            raise TransportTimingEvidenceError(
                "generic timing primitive never proves canonical provider transport binding"
            )
        if type(self.provider_acceptance_proven) is not bool or self.provider_acceptance_proven:
            raise TransportTimingEvidenceError(
                "transport round-trip timing never proves provider acceptance"
            )
        if type(self.external_effect_proven) is not bool or self.external_effect_proven:
            raise TransportTimingEvidenceError(
                "transport round-trip timing never proves an external effect"
            )
        if self.outcome is TransportRoundTripOutcome.RETURNED:
            if self.response_sha256 is None:
                raise TransportTimingEvidenceError(
                    "RETURNED timing witness requires exact response digest"
                )
        elif self.response_sha256 is not None:
            raise TransportTimingEvidenceError(
                "non-returned timing outcome cannot claim a response digest"
            )
        object.__setattr__(
            self,
            "evidence_sha256",
            _digest(self.to_dict(include_evidence_sha256=False)),
        )

    def assert_authoritative(
        self,
        _assertion: Callable[["TransportRoundTripWitness"], None] = (
            _assert_transport_timing_witness_authoritative
        ),
    ) -> None:
        """Reject copies, caller-minted objects and in-place post-issuance mutation."""

        _assertion(self)

    def to_dict(self, *, include_evidence_sha256: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": "autosport.transport_round_trip_witness",
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "action_id": self.action_id,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "request_sha256": self.request_sha256,
            "outcome": self.outcome.value,
            "elapsed_ns": self.elapsed_ns,
            "response_sha256": self.response_sha256,
            "clock_source": self.clock_source,
            "clock_is_product_default": self.clock_is_product_default,
            "timing_scope": self.timing_scope,
            "canonical_transport_binding_proven": self.canonical_transport_binding_proven,
            "provider_acceptance_proven": self.provider_acceptance_proven,
            "external_effect_proven": self.external_effect_proven,
        }
        if include_evidence_sha256:
            payload["evidence_sha256"] = self.evidence_sha256
        return payload



class MeasuredTransportFailure(TransportTimingEvidenceError):
    """Compatibility wrapper retained for pre-envelope imports only."""

    def __init__(self, message: str, witness: TransportRoundTripWitness) -> None:
        super().__init__(message)
        self.witness = witness


class _InvalidTransportResponse(TypeError):
    """The wrapped transport returned something other than exact bytes."""


def _sample(clock: Callable[[], int], name: str) -> int:
    try:
        value = clock()
    except Exception as exc:
        raise TransportTimingEvidenceError(f"{name} clock sample failed") from exc
    return _tick(value, name)


def _elapsed(start_ns: int, end_ns: int) -> int:
    if end_ns < start_ns:
        raise TransportTimingEvidenceError("monotonic clock moved backwards")
    elapsed = end_ns - start_ns
    if elapsed > MAX_ELAPSED_NS:
        raise TransportTimingEvidenceError("measured transport duration exceeds int64")
    return elapsed


def _witness(
    *,
    attempt_id: str,
    action_id: str,
    provider_id: str,
    account_id: str,
    request_sha256: str,
    outcome: TransportRoundTripOutcome,
    elapsed_ns: int,
    response_sha256: str | None,
    production_clock: bool,
    issuer: Callable[
        [Callable[[], TransportRoundTripWitness]],
        TransportRoundTripWitness,
    ],
) -> TransportRoundTripWitness:
    def build() -> TransportRoundTripWitness:
        return TransportRoundTripWitness(
            attempt_id=attempt_id,
            action_id=action_id,
            provider_id=provider_id,
            account_id=account_id,
            request_sha256=request_sha256,
            outcome=outcome,
            elapsed_ns=elapsed_ns,
            response_sha256=response_sha256,
            clock_source=(
                PRODUCTION_CLOCK_SOURCE if production_clock else TEST_CLOCK_SOURCE
            ),
            clock_is_product_default=production_clock,
        )

    return issuer(build)


def _timing_reason(exc: TransportTimingEvidenceError) -> str:
    message = str(exc)
    if "clock sample failed" in message:
        return "END_CLOCK_SAMPLE_FAILED"
    if "non-negative int" in message:
        return "END_CLOCK_INVALID"
    if "moved backwards" in message:
        return "CLOCK_MOVED_BACKWARDS"
    if "exceeds int64" in message:
        return "DURATION_OVERFLOW"
    return "TIMING_EVIDENCE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class TransportRoundTripMeasurement:
    """Transport truth plus independent optional timing evidence."""

    response: bytes | None
    transport_error: Exception | None
    witness: TransportRoundTripWitness | None
    timing_unavailable_reason: str | None

    def __post_init__(self) -> None:
        if (self.response is None) == (self.transport_error is None):
            raise TransportTimingEvidenceError(
                "measurement requires exactly one transport response or error"
            )
        if self.response is not None and type(self.response) is not bytes:
            raise TransportTimingEvidenceError(
                "measurement response must be exact bytes"
            )
        if self.transport_error is not None and not isinstance(
            self.transport_error, Exception
        ):
            raise TransportTimingEvidenceError(
                "transport_error must be an Exception"
            )
        if self.witness is not None and not isinstance(
            self.witness, TransportRoundTripWitness
        ):
            raise TransportTimingEvidenceError(
                "witness must be TransportRoundTripWitness or None"
            )
        if self.witness is not None:
            self.witness.assert_authoritative()
        if self.timing_unavailable_reason is not None:
            _text(self.timing_unavailable_reason, "timing_unavailable_reason")
        if (self.witness is None) == (self.timing_unavailable_reason is None):
            raise TransportTimingEvidenceError(
                "measurement requires exactly one timing witness or unavailable reason"
            )

        if self.response is not None and self.witness is not None:
            if self.witness.outcome is not TransportRoundTripOutcome.RETURNED:
                raise TransportTimingEvidenceError(
                    "returned transport bytes require RETURNED timing outcome"
                )
            if self.witness.response_sha256 != sha256(self.response).hexdigest():
                raise TransportTimingEvidenceError(
                    "timing witness response digest does not match transport bytes"
                )

        if self.transport_error is not None and self.witness is not None:
            expected = (
                TransportRoundTripOutcome.TIMEOUT
                if isinstance(self.transport_error, TimeoutError)
                else (
                    TransportRoundTripOutcome.INVALID_RESPONSE
                    if isinstance(self.transport_error, _InvalidTransportResponse)
                    else TransportRoundTripOutcome.TRANSPORT_ERROR
                )
            )
            if self.witness.outcome is not expected:
                raise TransportTimingEvidenceError(
                    "timing witness outcome does not match transport outcome"
                )

    @property
    def timing_available(self) -> bool:
        if self.witness is not None:
            self.witness.assert_authoritative()
        return self.witness is not None

    def unwrap(self) -> bytes:
        if self.transport_error is not None:
            raise self.transport_error
        assert self.response is not None
        return self.response

    def __iter__(self):
        yield self.unwrap()
        if self.witness is not None:
            self.witness.assert_authoritative()
        yield self.witness


def _finish_timing(
    *,
    clock: Callable[[], int],
    start_ns: int,
    attempt_id: str,
    action_id: str,
    provider_id: str,
    account_id: str,
    request_sha256: str,
    outcome: TransportRoundTripOutcome,
    response_sha256: str | None,
    production_clock: bool,
    issuer: Callable[
        [Callable[[], TransportRoundTripWitness]],
        TransportRoundTripWitness,
    ],
) -> tuple[TransportRoundTripWitness | None, str | None]:
    try:
        end_ns = _sample(clock, "transport_end_ns")
        elapsed_ns = _elapsed(start_ns, end_ns)
        return (
            _witness(
                attempt_id=attempt_id,
                action_id=action_id,
                provider_id=provider_id,
                account_id=account_id,
                request_sha256=request_sha256,
                outcome=outcome,
                elapsed_ns=elapsed_ns,
                response_sha256=response_sha256,
                production_clock=production_clock,
                issuer=issuer,
            ),
            None,
        )
    except TransportTimingEvidenceError as exc:
        return None, _timing_reason(exc)


def _measure_transport_round_trip_impl(
    *,
    attempt_id: str,
    action_id: str,
    provider_id: str,
    account_id: str,
    request_body: bytes,
    operation: Callable[[], bytes],
    monotonic_ns: Callable[[], int] | None,
    product_clock: Callable[[], int],
    issuer: Callable[
        [Callable[[], TransportRoundTripWitness]],
        TransportRoundTripWitness,
    ],
) -> TransportRoundTripMeasurement:
    for name, value in (
        ("attempt_id", attempt_id),
        ("action_id", action_id),
        ("provider_id", provider_id),
        ("account_id", account_id),
    ):
        _text(value, name)
    if type(request_body) is not bytes:
        raise TypeError("request_body must be exact bytes")
    if not callable(operation):
        raise TypeError("operation must be callable")
    if monotonic_ns is not None and not callable(monotonic_ns):
        raise TypeError("monotonic_ns must be callable or None")

    request_sha256 = sha256(request_body).hexdigest()
    production_clock = monotonic_ns is None
    clock = product_clock if production_clock else monotonic_ns
    assert clock is not None

    start_ns = _sample(clock, "transport_start_ns")

    try:
        response = operation()
    except Exception as exc:
        outcome = (
            TransportRoundTripOutcome.TIMEOUT
            if isinstance(exc, TimeoutError)
            else TransportRoundTripOutcome.TRANSPORT_ERROR
        )
        witness, timing_reason = _finish_timing(
            clock=clock,
            start_ns=start_ns,
            attempt_id=attempt_id,
            action_id=action_id,
            provider_id=provider_id,
            account_id=account_id,
            request_sha256=request_sha256,
            outcome=outcome,
            response_sha256=None,
            production_clock=production_clock,
            issuer=issuer,
        )
        return TransportRoundTripMeasurement(
            response=None,
            transport_error=exc,
            witness=witness,
            timing_unavailable_reason=timing_reason,
        )

    if type(response) is not bytes:
        invalid = _InvalidTransportResponse(
            "transport returned a non-bytes response"
        )
        witness, timing_reason = _finish_timing(
            clock=clock,
            start_ns=start_ns,
            attempt_id=attempt_id,
            action_id=action_id,
            provider_id=provider_id,
            account_id=account_id,
            request_sha256=request_sha256,
            outcome=TransportRoundTripOutcome.INVALID_RESPONSE,
            response_sha256=None,
            production_clock=production_clock,
            issuer=issuer,
        )
        return TransportRoundTripMeasurement(
            response=None,
            transport_error=invalid,
            witness=witness,
            timing_unavailable_reason=timing_reason,
        )

    witness, timing_reason = _finish_timing(
        clock=clock,
        start_ns=start_ns,
        attempt_id=attempt_id,
        action_id=action_id,
        provider_id=provider_id,
        account_id=account_id,
        request_sha256=request_sha256,
        outcome=TransportRoundTripOutcome.RETURNED,
        response_sha256=sha256(response).hexdigest(),
        production_clock=production_clock,
        issuer=issuer,
    )
    return TransportRoundTripMeasurement(
        response=response,
        transport_error=None,
        witness=witness,
        timing_unavailable_reason=timing_reason,
    )


def _bind_product_clock(
    product_clock: Callable[[], int],
    issuer: Callable[
        [Callable[[], TransportRoundTripWitness]],
        TransportRoundTripWitness,
    ],
) -> Callable[..., TransportRoundTripMeasurement]:
    """Capture the production clock so later module time rebinding cannot forge it."""

    def measured(
        *,
        attempt_id: str,
        action_id: str,
        provider_id: str,
        account_id: str,
        request_body: bytes,
        operation: Callable[[], bytes],
        monotonic_ns: Callable[[], int] | None = None,
    ) -> TransportRoundTripMeasurement:
        return _measure_transport_round_trip_impl(
            attempt_id=attempt_id,
            action_id=action_id,
            provider_id=provider_id,
            account_id=account_id,
            request_body=request_body,
            operation=operation,
            monotonic_ns=monotonic_ns,
            product_clock=product_clock,
            issuer=issuer,
        )

    measured.__name__ = "measure_transport_round_trip"
    measured.__qualname__ = "measure_transport_round_trip"
    return measured


measure_transport_round_trip = _bind_product_clock(
    time.perf_counter_ns,
    _issue_transport_timing_witness,
)
del _bind_product_clock
del _issue_transport_timing_witness
del _witness_issuance_active
del _assert_transport_timing_witness_authoritative
