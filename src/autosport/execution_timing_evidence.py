from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


class ExecutionTimingEvidenceError(RuntimeError):
    """Raised when local monotonic timing evidence cannot be issued safely."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ExecutionTimingEvidenceError(f"{name} must be non-empty canonical text")
    return value


def _strict_nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ExecutionTimingEvidenceError(f"{name} must be a non-negative integer")
    return value


def _wall_time(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ExecutionTimingEvidenceError(f"{name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionTimingEvidenceError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _wall_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ExecutionTimingEvidenceError("timing evidence is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class MonotonicTimingMarker:
    """One marker in one local monotonic clock/session issuer domain."""

    clock_domain_id: str
    session_id: str
    issuer_epoch_id: str
    sequence: int
    label: str
    monotonic_ns: int
    wall_anchor_at: str

    def __post_init__(self) -> None:
        _text(self.clock_domain_id, "clock_domain_id")
        _text(self.session_id, "session_id")
        _text(self.issuer_epoch_id, "issuer_epoch_id")
        _strict_nonnegative_int(self.sequence, "sequence")
        _text(self.label, "label")
        _strict_nonnegative_int(self.monotonic_ns, "monotonic_ns")
        try:
            parsed = datetime.fromisoformat(
                _text(self.wall_anchor_at, "wall_anchor_at").replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ExecutionTimingEvidenceError(
                "wall_anchor_at must be ISO-8601"
            ) from exc
        _wall_time(parsed, "wall_anchor_at")

    @property
    def marker_id(self) -> str:
        return _digest(
            {
                "schema": "autosport.local_monotonic_timing_marker",
                "schema_version": 1,
                "clock_domain_id": self.clock_domain_id,
                "session_id": self.session_id,
                "issuer_epoch_id": self.issuer_epoch_id,
                "sequence": self.sequence,
                "label": self.label,
                "monotonic_ns": self.monotonic_ns,
                "wall_anchor_at": self.wall_anchor_at,
            }
        )


@dataclass(frozen=True, slots=True)
class MonotonicTimingIntervalEvidence:
    """Exact local elapsed time over markers from one live issuer epoch.

    This DTO does not, by construction alone, carry positive product authority.
    Consumers that require positive evidence must re-resolve it through the
    issuing ``LocalMonotonicTimingSession.require_issued_interval`` method.
    """

    clock_domain_id: str
    session_id: str
    issuer_epoch_id: str
    start_marker_id: str
    end_marker_id: str
    start_sequence: int
    end_sequence: int
    duration_ns: int

    def __post_init__(self) -> None:
        _text(self.clock_domain_id, "clock_domain_id")
        _text(self.session_id, "session_id")
        _text(self.issuer_epoch_id, "issuer_epoch_id")
        _text(self.start_marker_id, "start_marker_id")
        _text(self.end_marker_id, "end_marker_id")
        start = _strict_nonnegative_int(self.start_sequence, "start_sequence")
        end = _strict_nonnegative_int(self.end_sequence, "end_sequence")
        _strict_nonnegative_int(self.duration_ns, "duration_ns")
        if end <= start:
            raise ExecutionTimingEvidenceError(
                "end marker sequence must follow start marker sequence"
            )

    @property
    def timing_class(self) -> str:
        return "LOCAL_MONOTONIC_SAME_SESSION"

    @property
    def duration_ms(self) -> Decimal:
        # Decimal arithmetic obeys the ambient process context and can round a
        # perfectly exact integer-nanosecond interval. Construct the base-10
        # value directly so nanoseconds -> milliseconds remains exact.
        return Decimal(f"{self.duration_ns}e-6")

    @property
    def evidence_id(self) -> str:
        return _digest(
            {
                "schema": "autosport.local_monotonic_timing_interval",
                "schema_version": 1,
                "timing_class": self.timing_class,
                "clock_domain_id": self.clock_domain_id,
                "session_id": self.session_id,
                "issuer_epoch_id": self.issuer_epoch_id,
                "start_marker_id": self.start_marker_id,
                "end_marker_id": self.end_marker_id,
                "start_sequence": self.start_sequence,
                "end_sequence": self.end_sequence,
                "duration_ns": self.duration_ns,
            }
        )


class LocalMonotonicTimingSession:
    """Issue exact same-session local timing without comparing unrelated clocks.

    The default clock is ``time.perf_counter_ns``. Exact local elapsed time is
    issued only while both markers are re-resolvable through this live session
    instance. Persisted marker-shaped data, a restarted process, or a different
    clock/session domain cannot mint a new exact interval.

    ``wall_anchor_at`` is provenance only. It is deliberately excluded from
    elapsed-time arithmetic, so wall-clock jumps cannot create negative or
    favorable local latency. Provider timestamps are outside this authority.
    The interval is generic local timing; a provider consumer must separately
    prove that its marker pair brackets the canonical transport interaction
    before classifying that observation as transport latency.
    """

    def __init__(
        self,
        *,
        monotonic_ns: Callable[[], int] = time.perf_counter_ns,
        wall_now: Callable[[], datetime] | None = None,
        clock_domain_id: str | None = None,
        session_id: str | None = None,
        issuer_epoch_id: str | None = None,
    ) -> None:
        if not callable(monotonic_ns):
            raise ExecutionTimingEvidenceError("monotonic_ns must be callable")
        if wall_now is None:
            wall_now = lambda: datetime.now(timezone.utc)
        if not callable(wall_now):
            raise ExecutionTimingEvidenceError("wall_now must be callable")

        self._monotonic_ns = monotonic_ns
        self._wall_now = wall_now
        self.clock_domain_id = _text(
            clock_domain_id or f"python-perf-counter:{uuid.uuid4().hex}",
            "clock_domain_id",
        )
        self.session_id = _text(
            session_id or f"local-execution-session:{uuid.uuid4().hex}",
            "session_id",
        )
        self.issuer_epoch_id = _text(
            issuer_epoch_id or f"issuer-epoch:{uuid.uuid4().hex}",
            "issuer_epoch_id",
        )
        self._state_lock = threading.RLock()
        self._sequence = 0
        self._last_monotonic_ns: int | None = None
        self._issued_markers: dict[str, MonotonicTimingMarker] = {}
        self._issued_intervals: dict[str, MonotonicTimingIntervalEvidence] = {}

    def mark(self, label: str) -> MonotonicTimingMarker:
        label = _text(label, "label")
        monotonic_ns = _strict_nonnegative_int(
            self._monotonic_ns(), "monotonic_ns result"
        )
        wall = _wall_time(self._wall_now(), "wall_now result")

        # Clock/wall sampling may overlap across callers, but issuance is one
        # linearizable state transition. Recheck the live high-water mark only
        # while holding the same lock that advances sequence and registry truth.
        with self._state_lock:
            if (
                self._last_monotonic_ns is not None
                and monotonic_ns < self._last_monotonic_ns
            ):
                raise ExecutionTimingEvidenceError("monotonic clock regressed")

            self._sequence += 1
            marker = MonotonicTimingMarker(
                clock_domain_id=self.clock_domain_id,
                session_id=self.session_id,
                issuer_epoch_id=self.issuer_epoch_id,
                sequence=self._sequence,
                label=label,
                monotonic_ns=monotonic_ns,
                wall_anchor_at=_wall_text(wall),
            )
            self._last_monotonic_ns = monotonic_ns
            self._issued_markers[marker.marker_id] = marker
            return marker

    def interval(
        self,
        start: MonotonicTimingMarker,
        end: MonotonicTimingMarker,
    ) -> MonotonicTimingIntervalEvidence:
        if not isinstance(start, MonotonicTimingMarker) or not isinstance(
            end, MonotonicTimingMarker
        ):
            raise ExecutionTimingEvidenceError(
                "interval endpoints must be MonotonicTimingMarker"
            )
        with self._state_lock:
            for marker, name in ((start, "start"), (end, "end")):
                if (
                    marker.clock_domain_id != self.clock_domain_id
                    or marker.session_id != self.session_id
                    or marker.issuer_epoch_id != self.issuer_epoch_id
                ):
                    raise ExecutionTimingEvidenceError(
                        f"{name} marker is from another clock/session issuer epoch"
                    )
                issued = self._issued_markers.get(marker.marker_id)
                if issued != marker:
                    raise ExecutionTimingEvidenceError(
                        f"{name} marker is not product-issued by this live session"
                    )
            if end.sequence <= start.sequence:
                raise ExecutionTimingEvidenceError(
                    "end marker must causally follow start marker"
                )
            if end.monotonic_ns < start.monotonic_ns:
                raise ExecutionTimingEvidenceError(
                    "negative monotonic duration is invalid evidence"
                )
            evidence = MonotonicTimingIntervalEvidence(
                clock_domain_id=self.clock_domain_id,
                session_id=self.session_id,
                issuer_epoch_id=self.issuer_epoch_id,
                start_marker_id=start.marker_id,
                end_marker_id=end.marker_id,
                start_sequence=start.sequence,
                end_sequence=end.sequence,
                duration_ns=end.monotonic_ns - start.monotonic_ns,
            )
            self._issued_intervals[evidence.evidence_id] = evidence
            return evidence

    def require_issued_interval(
        self,
        evidence: MonotonicTimingIntervalEvidence,
    ) -> MonotonicTimingIntervalEvidence:
        if not isinstance(evidence, MonotonicTimingIntervalEvidence):
            raise ExecutionTimingEvidenceError(
                "timing evidence must be MonotonicTimingIntervalEvidence"
            )
        with self._state_lock:
            issued = self._issued_intervals.get(evidence.evidence_id)
            if issued != evidence:
                raise ExecutionTimingEvidenceError(
                    "timing interval is not product-issued by this live session"
                )
            return issued
