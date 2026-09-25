from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


_SQLITE_SEQUENCE_MIN = -(1 << 63)
_SQLITE_SEQUENCE_MAX = (1 << 63) - 1


class ProviderContinuityStatus(str, Enum):
    """Fail-closed continuity state for one provider stream scope."""

    UNINITIALIZED = "uninitialized"
    SYNCHRONIZED = "synchronized"
    GAP_DETECTED = "gap_detected"
    OUT_OF_ORDER = "out_of_order"
    SEQUENCE_CONFLICT = "sequence_conflict"
    MONOTONIC_REGRESSION = "monotonic_regression"
    STALE = "stale"
    DISCONNECTED = "disconnected"
    AWAITING_RESYNC = "awaiting_resync"


class ProviderContinuityOutcome(str, Enum):
    """Result of consuming one provider delta."""

    APPLIED = "applied"
    DUPLICATE = "duplicate"
    GAP_DETECTED = "gap_detected"
    OUT_OF_ORDER = "out_of_order"
    SEQUENCE_CONFLICT = "sequence_conflict"
    MONOTONIC_REGRESSION = "monotonic_regression"
    STALE = "stale"
    RESYNC_REQUIRED = "resync_required"


def _canonical_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be UTF-8 encodable") from exc
    return value


def _sequence_id(value: object) -> int:
    if type(value) is not int:
        raise TypeError("sequence_id must be a non-boolean int")
    if value < _SQLITE_SEQUENCE_MIN or value > _SQLITE_SEQUENCE_MAX:
        raise ValueError("sequence_id must fit signed 64-bit SQLite INTEGER")
    return value


def _monotonic_ns(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be a non-boolean int")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _positive_ns(value: object, field_name: str) -> int:
    result = _monotonic_ns(value, field_name)
    if result == 0:
        raise ValueError(f"{field_name} must be positive")
    return result


@dataclass(frozen=True, slots=True)
class ProviderStreamDelta:
    """One already-decoded provider stream update with exact sequence evidence."""

    source_id: str
    sequence_id: int
    evidence_id: str
    received_monotonic_ns: int

    def __post_init__(self) -> None:
        _canonical_text(self.source_id, "source_id")
        _sequence_id(self.sequence_id)
        _canonical_text(self.evidence_id, "evidence_id")
        _monotonic_ns(self.received_monotonic_ns, "received_monotonic_ns")


@dataclass(frozen=True, slots=True)
class ProviderResyncSnapshot:
    """Upstream-verified full snapshot/resync evidence for one provider scope.

    This value does not prove provider origin by itself. The caller must bind
    ``evidence_id`` to whatever provider-specific authority established that the
    snapshot is complete and authoritative for the intended stream scope.
    """

    source_id: str
    sequence_id: int
    evidence_id: str
    received_monotonic_ns: int

    def __post_init__(self) -> None:
        _canonical_text(self.source_id, "source_id")
        _sequence_id(self.sequence_id)
        _canonical_text(self.evidence_id, "evidence_id")
        _monotonic_ns(self.received_monotonic_ns, "received_monotonic_ns")


@dataclass(frozen=True, slots=True)
class ProviderContinuityState:
    """Immutable continuity state; it owns no quote payload or provider transport."""

    source_id: str
    max_silence_ns: int
    generation: int = 0
    status: ProviderContinuityStatus = ProviderContinuityStatus.UNINITIALIZED
    last_sequence_id: int | None = None
    last_evidence_id: str | None = None
    last_evidence_kind: str | None = None
    last_received_monotonic_ns: int | None = None
    # Local causal fence for reconnect. It is not provider evidence and is cleared
    # only when a full resync received at/after the latest reconnect boundary wins.
    resync_not_before_monotonic_ns: int | None = None

    def __post_init__(self) -> None:
        _canonical_text(self.source_id, "source_id")
        _positive_ns(self.max_silence_ns, "max_silence_ns")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be a non-negative non-boolean int")
        if not isinstance(self.status, ProviderContinuityStatus):
            raise TypeError("status must be ProviderContinuityStatus")
        if self.last_sequence_id is not None:
            _sequence_id(self.last_sequence_id)
        if self.last_evidence_id is not None:
            _canonical_text(self.last_evidence_id, "last_evidence_id")
        if self.last_evidence_kind not in {None, "delta", "snapshot"}:
            raise ValueError("last_evidence_kind must be None, 'delta', or 'snapshot'")
        if self.last_received_monotonic_ns is not None:
            _monotonic_ns(
                self.last_received_monotonic_ns,
                "last_received_monotonic_ns",
            )
        if self.resync_not_before_monotonic_ns is not None:
            _monotonic_ns(
                self.resync_not_before_monotonic_ns,
                "resync_not_before_monotonic_ns",
            )
        populated = (
            self.last_sequence_id is not None,
            self.last_evidence_id is not None,
            self.last_evidence_kind is not None,
            self.last_received_monotonic_ns is not None,
        )
        if any(populated) and not all(populated):
            raise ValueError("last provider evidence fields must be populated together")
        if self.generation == 0 and any(populated):
            raise ValueError("generation zero cannot contain provider evidence")
        if self.generation > 0 and not all(populated):
            raise ValueError("positive generation requires provider evidence")
        if self.generation == 0 and self.status is not ProviderContinuityStatus.UNINITIALIZED:
            raise ValueError("generation zero must be uninitialized")
        if self.generation > 0 and self.status is ProviderContinuityStatus.UNINITIALIZED:
            raise ValueError("positive generation cannot be uninitialized")
        if self.generation == 0 and self.resync_not_before_monotonic_ns is not None:
            raise ValueError("generation zero cannot carry a reconnect boundary")
        if self.status is ProviderContinuityStatus.SYNCHRONIZED and (
            self.resync_not_before_monotonic_ns is not None
        ):
            raise ValueError("synchronized state cannot retain a reconnect boundary")
        if self.status in {
            ProviderContinuityStatus.DISCONNECTED,
            ProviderContinuityStatus.AWAITING_RESYNC,
        } and self.resync_not_before_monotonic_ns is None:
            raise ValueError("disconnect/reconnect state requires a causal resync boundary")
        if (
            self.resync_not_before_monotonic_ns is not None
            and self.last_received_monotonic_ns is not None
            and self.resync_not_before_monotonic_ns < self.last_received_monotonic_ns
        ):
            raise ValueError("reconnect boundary cannot predate last provider evidence")

    @classmethod
    def initial(cls, source_id: str, *, max_silence_ns: int) -> ProviderContinuityState:
        return cls(source_id=source_id, max_silence_ns=max_silence_ns)


@dataclass(frozen=True, slots=True)
class ProviderContinuityTransition:
    state: ProviderContinuityState
    outcome: ProviderContinuityOutcome
    expected_sequence_id: int | None
    observed_sequence_id: int


@dataclass(frozen=True, slots=True)
class ProviderContinuityGate:
    status: ProviderContinuityStatus
    actionable: bool
    generation: int
    last_sequence_id: int | None
    silence_ns: int | None


def _same_source(state: ProviderContinuityState, source_id: str) -> None:
    if source_id != state.source_id:
        raise ValueError("provider continuity evidence source_id does not match state")


def _degraded_state(
    state: ProviderContinuityState,
    status: ProviderContinuityStatus,
) -> ProviderContinuityState:
    return ProviderContinuityState(
        source_id=state.source_id,
        max_silence_ns=state.max_silence_ns,
        generation=state.generation,
        status=status,
        last_sequence_id=state.last_sequence_id,
        last_evidence_id=state.last_evidence_id,
        last_evidence_kind=state.last_evidence_kind,
        last_received_monotonic_ns=state.last_received_monotonic_ns,
        resync_not_before_monotonic_ns=state.resync_not_before_monotonic_ns,
    )


def accept_authoritative_snapshot(
    state: ProviderContinuityState,
    snapshot: ProviderResyncSnapshot,
) -> ProviderContinuityState:
    """Start a new continuity generation from upstream-verified full state.

    A fresh synchronized generation cannot be silently replaced. The caller must
    first observe disconnect/degradation, or the old generation must have exceeded
    its explicit silence bound at the new snapshot's receive instant. A reconnect
    resync must additionally have been received at or after the latest reconnect
    observation, so a pre-gap/pre-reconnect cached snapshot cannot heal the gap.
    """

    if not isinstance(state, ProviderContinuityState):
        raise TypeError("state must be ProviderContinuityState")
    if not isinstance(snapshot, ProviderResyncSnapshot):
        raise TypeError("snapshot must be ProviderResyncSnapshot")
    _same_source(state, snapshot.source_id)

    previous_receive = state.last_received_monotonic_ns
    if previous_receive is not None and snapshot.received_monotonic_ns < previous_receive:
        raise ValueError("snapshot receive monotonic time regressed")
    resync_not_before = state.resync_not_before_monotonic_ns
    if (
        resync_not_before is not None
        and snapshot.received_monotonic_ns < resync_not_before
    ):
        raise ValueError("snapshot was received before the reconnect resync boundary")
    if state.status is ProviderContinuityStatus.SYNCHRONIZED:
        if previous_receive is None:
            raise AssertionError("synchronized state is missing receive evidence")
        silence = snapshot.received_monotonic_ns - previous_receive
        if silence <= state.max_silence_ns:
            raise RuntimeError(
                "fresh synchronized continuity cannot be replaced without a resync boundary"
            )

    return ProviderContinuityState(
        source_id=state.source_id,
        max_silence_ns=state.max_silence_ns,
        generation=state.generation + 1,
        status=ProviderContinuityStatus.SYNCHRONIZED,
        last_sequence_id=snapshot.sequence_id,
        last_evidence_id=snapshot.evidence_id,
        last_evidence_kind="snapshot",
        last_received_monotonic_ns=snapshot.received_monotonic_ns,
    )


def accept_delta(
    state: ProviderContinuityState,
    delta: ProviderStreamDelta,
) -> ProviderContinuityTransition:
    """Consume one delta only while the current generation is proven continuous."""

    if not isinstance(state, ProviderContinuityState):
        raise TypeError("state must be ProviderContinuityState")
    if not isinstance(delta, ProviderStreamDelta):
        raise TypeError("delta must be ProviderStreamDelta")
    _same_source(state, delta.source_id)

    if state.status is not ProviderContinuityStatus.SYNCHRONIZED:
        return ProviderContinuityTransition(
            state=state,
            outcome=ProviderContinuityOutcome.RESYNC_REQUIRED,
            expected_sequence_id=None,
            observed_sequence_id=delta.sequence_id,
        )

    if state.last_sequence_id is None or state.last_received_monotonic_ns is None:
        raise AssertionError("synchronized state is missing sequence/receive evidence")

    expected = state.last_sequence_id + 1
    if delta.received_monotonic_ns < state.last_received_monotonic_ns:
        degraded = _degraded_state(
            state,
            ProviderContinuityStatus.MONOTONIC_REGRESSION,
        )
        return ProviderContinuityTransition(
            degraded,
            ProviderContinuityOutcome.MONOTONIC_REGRESSION,
            expected,
            delta.sequence_id,
        )

    silence = delta.received_monotonic_ns - state.last_received_monotonic_ns
    if silence > state.max_silence_ns:
        degraded = _degraded_state(state, ProviderContinuityStatus.STALE)
        return ProviderContinuityTransition(
            degraded,
            ProviderContinuityOutcome.STALE,
            expected,
            delta.sequence_id,
        )

    if delta.sequence_id == state.last_sequence_id:
        if state.last_evidence_kind == "delta" and delta.evidence_id == state.last_evidence_id:
            # A retransmitted identical update is idempotent, but must not move the
            # freshness clock forward. Otherwise duplicate spam could keep stale
            # provider state indefinitely actionable.
            return ProviderContinuityTransition(
                state,
                ProviderContinuityOutcome.DUPLICATE,
                expected,
                delta.sequence_id,
            )
        degraded = _degraded_state(
            state,
            ProviderContinuityStatus.SEQUENCE_CONFLICT,
        )
        return ProviderContinuityTransition(
            degraded,
            ProviderContinuityOutcome.SEQUENCE_CONFLICT,
            expected,
            delta.sequence_id,
        )

    if delta.sequence_id < state.last_sequence_id:
        degraded = _degraded_state(state, ProviderContinuityStatus.OUT_OF_ORDER)
        return ProviderContinuityTransition(
            degraded,
            ProviderContinuityOutcome.OUT_OF_ORDER,
            expected,
            delta.sequence_id,
        )

    if delta.sequence_id != expected:
        degraded = _degraded_state(state, ProviderContinuityStatus.GAP_DETECTED)
        return ProviderContinuityTransition(
            degraded,
            ProviderContinuityOutcome.GAP_DETECTED,
            expected,
            delta.sequence_id,
        )

    advanced = ProviderContinuityState(
        source_id=state.source_id,
        max_silence_ns=state.max_silence_ns,
        generation=state.generation,
        status=ProviderContinuityStatus.SYNCHRONIZED,
        last_sequence_id=delta.sequence_id,
        last_evidence_id=delta.evidence_id,
        last_evidence_kind="delta",
        last_received_monotonic_ns=delta.received_monotonic_ns,
    )
    return ProviderContinuityTransition(
        advanced,
        ProviderContinuityOutcome.APPLIED,
        expected,
        delta.sequence_id,
    )


def mark_disconnected(
    state: ProviderContinuityState,
    *,
    observed_monotonic_ns: int,
) -> ProviderContinuityState:
    """Fence all action immediately while retaining the last proven generation."""

    if not isinstance(state, ProviderContinuityState):
        raise TypeError("state must be ProviderContinuityState")
    observed = _monotonic_ns(observed_monotonic_ns, "observed_monotonic_ns")
    if state.last_received_monotonic_ns is not None and observed < state.last_received_monotonic_ns:
        return _degraded_state(state, ProviderContinuityStatus.MONOTONIC_REGRESSION)
    if state.generation == 0:
        return state
    return ProviderContinuityState(
        source_id=state.source_id,
        max_silence_ns=state.max_silence_ns,
        generation=state.generation,
        status=ProviderContinuityStatus.DISCONNECTED,
        last_sequence_id=state.last_sequence_id,
        last_evidence_id=state.last_evidence_id,
        last_evidence_kind=state.last_evidence_kind,
        last_received_monotonic_ns=state.last_received_monotonic_ns,
        resync_not_before_monotonic_ns=observed,
    )


def begin_reconnect(
    state: ProviderContinuityState,
    *,
    observed_monotonic_ns: int,
) -> ProviderContinuityState:
    """Enter reconnect mode; deltas remain blocked until a full resync is accepted."""

    if not isinstance(state, ProviderContinuityState):
        raise TypeError("state must be ProviderContinuityState")
    observed = _monotonic_ns(observed_monotonic_ns, "observed_monotonic_ns")
    if state.status is not ProviderContinuityStatus.DISCONNECTED:
        raise RuntimeError("reconnect may begin only from disconnected state")
    if state.last_received_monotonic_ns is None:
        raise AssertionError("disconnected state is missing receive evidence")
    disconnect_boundary = state.resync_not_before_monotonic_ns
    if disconnect_boundary is None:
        raise AssertionError("disconnected state is missing resync boundary")
    if observed < state.last_received_monotonic_ns or observed < disconnect_boundary:
        return _degraded_state(state, ProviderContinuityStatus.MONOTONIC_REGRESSION)
    return ProviderContinuityState(
        source_id=state.source_id,
        max_silence_ns=state.max_silence_ns,
        generation=state.generation,
        status=ProviderContinuityStatus.AWAITING_RESYNC,
        last_sequence_id=state.last_sequence_id,
        last_evidence_id=state.last_evidence_id,
        last_evidence_kind=state.last_evidence_kind,
        last_received_monotonic_ns=state.last_received_monotonic_ns,
        resync_not_before_monotonic_ns=observed,
    )


def continuity_gate(
    state: ProviderContinuityState,
    *,
    now_monotonic_ns: int,
) -> ProviderContinuityGate:
    """Return actionability without ever inferring continuity from missing updates."""

    if not isinstance(state, ProviderContinuityState):
        raise TypeError("state must be ProviderContinuityState")
    now = _monotonic_ns(now_monotonic_ns, "now_monotonic_ns")
    last = state.last_received_monotonic_ns
    if state.status is not ProviderContinuityStatus.SYNCHRONIZED:
        return ProviderContinuityGate(
            status=state.status,
            actionable=False,
            generation=state.generation,
            last_sequence_id=state.last_sequence_id,
            silence_ns=None if last is None else max(now - last, 0),
        )
    if last is None:
        raise AssertionError("synchronized state is missing receive evidence")
    if now < last:
        return ProviderContinuityGate(
            status=ProviderContinuityStatus.MONOTONIC_REGRESSION,
            actionable=False,
            generation=state.generation,
            last_sequence_id=state.last_sequence_id,
            silence_ns=None,
        )
    silence = now - last
    if silence > state.max_silence_ns:
        return ProviderContinuityGate(
            status=ProviderContinuityStatus.STALE,
            actionable=False,
            generation=state.generation,
            last_sequence_id=state.last_sequence_id,
            silence_ns=silence,
        )
    return ProviderContinuityGate(
        status=ProviderContinuityStatus.SYNCHRONIZED,
        actionable=True,
        generation=state.generation,
        last_sequence_id=state.last_sequence_id,
        silence_ns=silence,
    )
