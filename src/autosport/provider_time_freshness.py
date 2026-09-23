from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum


_SQLITE_SEQUENCE_MIN = -(1 << 63)
_SQLITE_SEQUENCE_MAX = (1 << 63) - 1
_MAX_OPAQUE_SEQUENCE_UTF8 = 512


class ProviderTimeStatus(str, Enum):
    """Fail-closed status for provider time/freshness evidence."""

    FRESH = "fresh"
    STALE = "stale"
    NEGATIVE_MONOTONIC_LATENCY = "negative_monotonic_latency"
    NEGATIVE_WALL_LATENCY = "negative_wall_latency"
    CLOCK_SKEW_EXCEEDED = "clock_skew_exceeded"
    FUTURE_RECEIPT = "future_receipt"


def _aware_datetime(value: object, field_name: str) -> datetime:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field_name} must be a non-empty trimmed ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware ISO-8601")
    return parsed.astimezone(timezone.utc)


def _monotonic_ns(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be a non-boolean int")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _sequence_id(value: object) -> int | str:
    if type(value) is int:
        if value < _SQLITE_SEQUENCE_MIN or value > _SQLITE_SEQUENCE_MAX:
            raise ValueError("integer sequence_id must fit signed 64-bit SQLite INTEGER")
        return value
    if type(value) is str:
        if not value or value != value.strip():
            raise ValueError("opaque sequence_id must be non-empty trimmed text")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("opaque sequence_id must be valid UTF-8 text") from exc
        if len(encoded) > _MAX_OPAQUE_SEQUENCE_UTF8:
            raise ValueError("opaque sequence_id exceeds UTF-8 size bound")
        return value
    raise TypeError("sequence_id must be an exact int or opaque string")


def _non_negative_duration(value: object, field_name: str) -> timedelta:
    if type(value) is not timedelta:
        raise TypeError(f"{field_name} must be an exact timedelta")
    if value < timedelta(0):
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _decision_time(value: object) -> datetime:
    if type(value) is not datetime:
        raise TypeError("decision_at must be an exact datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("decision_at must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ProviderTimeEvidence:
    """Exact provider/local timing evidence for one observed provider update.

    ``source_updated_at`` is provider-declared source/update wall time.
    ``received_wall_at`` is the local wall-clock instant after the response/update was
    received. The two monotonic values are a local acquisition interval and are never
    inferred from wall-clock timestamps. ``sequence_id`` is bound as provider update
    identity only; continuity/gap/resync semantics belong to a separate live contract.
    """

    source_updated_at: str
    received_wall_at: str
    acquisition_started_monotonic_ns: int
    received_monotonic_ns: int
    sequence_id: int | str

    def __post_init__(self) -> None:
        _aware_datetime(self.source_updated_at, "source_updated_at")
        _aware_datetime(self.received_wall_at, "received_wall_at")
        _monotonic_ns(
            self.acquisition_started_monotonic_ns,
            "acquisition_started_monotonic_ns",
        )
        _monotonic_ns(self.received_monotonic_ns, "received_monotonic_ns")
        _sequence_id(self.sequence_id)

    @property
    def evidence_id(self) -> str:
        payload = json.dumps(
            {
                "acquisition_started_monotonic_ns": self.acquisition_started_monotonic_ns,
                "received_monotonic_ns": self.received_monotonic_ns,
                "received_wall_at": self.received_wall_at,
                "sequence_id": self.sequence_id,
                "sequence_id_kind": (
                    "integer" if type(self.sequence_id) is int else "opaque"
                ),
                "source_updated_at": self.source_updated_at,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderFreshnessAssessment:
    """A diagnostic result that grants no provider-write or execution authority."""

    evidence_id: str
    sequence_id: int | str
    status: ProviderTimeStatus
    quote_age: timedelta
    source_to_receive_delay: timedelta
    transport_elapsed_ns: int
    source_clock_skew: timedelta
    max_quote_age: timedelta
    max_source_clock_skew: timedelta


def assess_provider_time_freshness(
    evidence: ProviderTimeEvidence,
    *,
    decision_at: datetime,
    max_quote_age: timedelta,
    max_source_clock_skew: timedelta = timedelta(0),
) -> ProviderFreshnessAssessment:
    """Classify exact provider timing evidence without correcting any clock.

    ``max_source_clock_skew`` is a diagnostic boundary, not an allowance that turns
    negative source-to-receive wall latency into fresh evidence. Any provider source
    time later than the local receive wall time fails closed. The boundary only
    distinguishes a bounded negative-latency diagnostic from skew that exceeds the
    configured contract.

    The local monotonic interval is independent of wall-clock/source time. A negative
    interval is an impossible local timing observation and also fails closed. Quote
    age is measured from provider source/update time to ``decision_at`` only after the
    receipt and latency invariants are valid. No hidden offset or clock correction is
    applied.
    """

    if not isinstance(evidence, ProviderTimeEvidence):
        raise TypeError("evidence must be ProviderTimeEvidence")
    boundary = _decision_time(decision_at)
    age_limit = _non_negative_duration(max_quote_age, "max_quote_age")
    skew_limit = _non_negative_duration(max_source_clock_skew, "max_source_clock_skew")

    source_time = _aware_datetime(evidence.source_updated_at, "source_updated_at")
    received_time = _aware_datetime(evidence.received_wall_at, "received_wall_at")
    transport_elapsed_ns = (
        evidence.received_monotonic_ns - evidence.acquisition_started_monotonic_ns
    )
    source_to_receive_delay = received_time - source_time
    source_clock_skew = max(source_time - received_time, timedelta(0))
    quote_age = boundary - source_time

    if transport_elapsed_ns < 0:
        status = ProviderTimeStatus.NEGATIVE_MONOTONIC_LATENCY
    elif received_time > boundary:
        status = ProviderTimeStatus.FUTURE_RECEIPT
    elif source_to_receive_delay < timedelta(0):
        if source_clock_skew > skew_limit:
            status = ProviderTimeStatus.CLOCK_SKEW_EXCEEDED
        else:
            status = ProviderTimeStatus.NEGATIVE_WALL_LATENCY
    elif quote_age > age_limit:
        status = ProviderTimeStatus.STALE
    else:
        status = ProviderTimeStatus.FRESH

    return ProviderFreshnessAssessment(
        evidence_id=evidence.evidence_id,
        sequence_id=evidence.sequence_id,
        status=status,
        quote_age=quote_age,
        source_to_receive_delay=source_to_receive_delay,
        transport_elapsed_ns=transport_elapsed_ns,
        source_clock_skew=source_clock_skew,
        max_quote_age=age_limit,
        max_source_clock_skew=skew_limit,
    )
