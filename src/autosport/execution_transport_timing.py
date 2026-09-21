"""Fail-closed local transport round-trip timing evidence.

This module measures only the synchronous local transport call boundary.  It does
not prove provider acceptance time, network one-way latency, an external effect,
or any execution outcome.  The production clock is ``time.perf_counter_ns``;
injected clocks are deliberately marked test-only so deterministic tests cannot
mint production timing evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import json
import time
from typing import Callable

SCHEMA_VERSION = 1
TIMING_SCOPE = "transport_round_trip"
PRODUCTION_CLOCK_SOURCE = "PROCESS_PERF_COUNTER_NS"
TEST_CLOCK_SOURCE = "INJECTED_TEST_CLOCK"
MAX_ELAPSED_NS = (1 << 63) - 1


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


@dataclass(frozen=True, slots=True)
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

    def __post_init__(self) -> None:
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
    """The transport call failed after a truthful local duration was measured."""

    def __init__(self, message: str, witness: TransportRoundTripWitness) -> None:
        super().__init__(message)
        self.witness = witness


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
) -> TransportRoundTripWitness:
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


def measure_transport_round_trip(
    *,
    attempt_id: str,
    action_id: str,
    provider_id: str,
    account_id: str,
    request_body: bytes,
    operation: Callable[[], bytes],
    monotonic_ns: Callable[[], int] | None = None,
) -> tuple[bytes, TransportRoundTripWitness]:
    """Measure one synchronous local transport call exactly once.

    The default clock is product-owned ``time.perf_counter_ns`` and yields a
    product-default-clock witness. Passing ``monotonic_ns`` is intentionally
    marked as an injected test clock. Neither case proves that the caller is the
    canonical provider transport; a later integration authority must bind that
    separately. On timeout/transport failure, :class:`MeasuredTransportFailure`
    carries the measured witness while the original exception remains chained.
    """

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
    clock = time.perf_counter_ns if production_clock else monotonic_ns
    assert clock is not None

    start_ns = _sample(clock, "transport_start_ns")
    try:
        response = operation()
    except Exception as exc:
        try:
            end_ns = _sample(clock, "transport_end_ns")
            elapsed_ns = _elapsed(start_ns, end_ns)
            outcome = (
                TransportRoundTripOutcome.TIMEOUT
                if isinstance(exc, TimeoutError)
                else TransportRoundTripOutcome.TRANSPORT_ERROR
            )
            witness = _witness(
                attempt_id=attempt_id,
                action_id=action_id,
                provider_id=provider_id,
                account_id=account_id,
                request_sha256=request_sha256,
                outcome=outcome,
                elapsed_ns=elapsed_ns,
                response_sha256=None,
                production_clock=production_clock,
            )
        except TransportTimingEvidenceError as timing_exc:
            raise timing_exc from exc
        raise MeasuredTransportFailure(
            "transport failed after local round-trip duration was measured",
            witness,
        ) from exc

    end_ns = _sample(clock, "transport_end_ns")
    elapsed_ns = _elapsed(start_ns, end_ns)
    if type(response) is not bytes:
        witness = _witness(
            attempt_id=attempt_id,
            action_id=action_id,
            provider_id=provider_id,
            account_id=account_id,
            request_sha256=request_sha256,
            outcome=TransportRoundTripOutcome.INVALID_RESPONSE,
            elapsed_ns=elapsed_ns,
            response_sha256=None,
            production_clock=production_clock,
        )
        raise MeasuredTransportFailure(
            "transport returned a non-bytes response",
            witness,
        )

    witness = _witness(
        attempt_id=attempt_id,
        action_id=action_id,
        provider_id=provider_id,
        account_id=account_id,
        request_sha256=request_sha256,
        outcome=TransportRoundTripOutcome.RETURNED,
        elapsed_ns=elapsed_ns,
        response_sha256=sha256(response).hexdigest(),
        production_clock=production_clock,
    )
    return response, witness
