from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Final


_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_OPEN_STATE: Final = "OPEN"


class LiveMarketBackpressureError(RuntimeError):
    """Raised when hot-path market work cannot be classified safely."""


@dataclass(frozen=True, slots=True, order=True)
class MarketStreamKey:
    """Provider-scoped identity for one independently ordered market stream."""

    provider_id: str
    stream_id: str
    market_id: str

    def __post_init__(self) -> None:
        for label, value in (
            ("provider_id", self.provider_id),
            ("stream_id", self.stream_id),
            ("market_id", self.market_id),
        ):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise LiveMarketBackpressureError(
                    f"{label} must be canonical non-blank text"
                )


@dataclass(frozen=True, slots=True)
class CompleteMarketSnapshot:
    """Opaque atomic provider snapshot already validated by source ingestion.

    This DTO does not create provider authority. ``payload_sha256`` commits to the
    complete atomic market state owned by the upstream source layer.
    """

    key: MarketStreamKey
    generation: str
    sequence: int
    source_available_at: datetime
    payload_sha256: str
    market_state: str

    def __post_init__(self) -> None:
        if type(self.key) is not MarketStreamKey:
            raise LiveMarketBackpressureError("key must be exact MarketStreamKey")
        if not isinstance(self.generation, str) or not self.generation.strip():
            raise LiveMarketBackpressureError("generation must be non-blank")
        if self.generation != self.generation.strip():
            raise LiveMarketBackpressureError("generation must be canonical")
        if type(self.sequence) is not int or self.sequence < 0:
            raise LiveMarketBackpressureError("sequence must be a non-negative integer")
        _utc(self.source_available_at, "source_available_at")
        _sha256(self.payload_sha256, "payload_sha256")
        if not isinstance(self.market_state, str) or not self.market_state.strip():
            raise LiveMarketBackpressureError("market_state must be non-blank")
        if self.market_state != self.market_state.strip().upper():
            raise LiveMarketBackpressureError(
                "market_state must be canonical uppercase text"
            )

    @property
    def identity_sha256(self) -> str:
        raw = "\x1f".join(
            (
                self.key.provider_id,
                self.key.stream_id,
                self.key.market_id,
                self.generation,
                str(self.sequence),
                self.source_available_at.isoformat(),
                self.payload_sha256,
                self.market_state,
            )
        )
        return sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    accepted: bool
    coalesced: bool
    local_capacity_drop: bool
    gap: bool
    resnapshot_required: bool
    reason: str


@dataclass(frozen=True, slots=True)
class CoalescedMarketView:
    snapshot: CompleteMarketSnapshot
    trusted_current_view: bool
    resnapshot_required: bool
    causal_replay_safe: bool
    coalesced_snapshot_count: int
    safety_transition_count: int

    def is_fresh(self, *, now: datetime, max_age_seconds: float) -> bool:
        """Use source availability time only; queue/consumer timing grants no freshness."""

        _utc(now, "now")
        if isinstance(max_age_seconds, bool) or not isinstance(
            max_age_seconds, (int, float)
        ):
            raise LiveMarketBackpressureError("max_age_seconds must be numeric")
        if max_age_seconds < 0:
            raise LiveMarketBackpressureError("max_age_seconds must be non-negative")
        age = (now - self.snapshot.source_available_at).total_seconds()
        return self.trusted_current_view and age >= 0 and age <= float(max_age_seconds)


@dataclass(frozen=True, slots=True)
class BackpressureStats:
    submitted_snapshots: int
    submitted_deltas: int
    coalesced_snapshots: int
    local_capacity_drops: int
    provider_gap_events: int
    recovery_events: int
    out_of_order_rejections: int
    conflicting_sequence_events: int
    safety_transitions: int


@dataclass(slots=True)
class _KeyState:
    generation: str
    last_sequence: int
    last_payload_sha256: str
    last_market_state: str
    gap: bool = False
    local_loss: bool = False
    coalesced_since_drain: int = 0
    safety_since_drain: int = 0


class LiveMarketBackpressure:
    """Bounded hot-path work coalescer; never a source-truth or replay authority.

    Complete superseding snapshots for the exact same key and generation may
    latest-win. Deltas, generation ambiguity, or conflicting equal-sequence
    snapshots set a sticky gap that only an explicit complete recovery snapshot
    may clear. Capacity pressure is reported as a local work drop and never as a
    fabricated provider continuity failure. A capacity drop for an already-known
    key is nevertheless sticky local causal loss and requires explicit recovery
    before the current view can be trusted again.
    """

    def __init__(self, *, capacity: int) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise LiveMarketBackpressureError("capacity must be a positive integer")
        self._capacity = capacity
        self._pending: dict[MarketStreamKey, CompleteMarketSnapshot] = {}
        self._state: dict[MarketStreamKey, _KeyState] = {}
        self._submitted_snapshots = 0
        self._submitted_deltas = 0
        self._coalesced_snapshots = 0
        self._local_capacity_drops = 0
        self._provider_gap_events = 0
        self._recovery_events = 0
        self._out_of_order_rejections = 0
        self._conflicting_sequence_events = 0
        self._safety_transitions = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def stats(self) -> BackpressureStats:
        return BackpressureStats(
            submitted_snapshots=self._submitted_snapshots,
            submitted_deltas=self._submitted_deltas,
            coalesced_snapshots=self._coalesced_snapshots,
            local_capacity_drops=self._local_capacity_drops,
            provider_gap_events=self._provider_gap_events,
            recovery_events=self._recovery_events,
            out_of_order_rejections=self._out_of_order_rejections,
            conflicting_sequence_events=self._conflicting_sequence_events,
            safety_transitions=self._safety_transitions,
        )

    def submit_snapshot(
        self,
        snapshot: CompleteMarketSnapshot,
        *,
        recovery: bool = False,
    ) -> SubmissionResult:
        if type(snapshot) is not CompleteMarketSnapshot:
            raise TypeError("snapshot must be exact CompleteMarketSnapshot")
        if type(recovery) is not bool:
            raise TypeError("recovery must be bool")
        self._submitted_snapshots += 1

        state = self._state.get(snapshot.key)
        if state is None:
            if not self._reserve_new_key(snapshot.key):
                return SubmissionResult(
                    accepted=False,
                    coalesced=False,
                    local_capacity_drop=True,
                    gap=False,
                    resnapshot_required=False,
                    reason="local work capacity exhausted",
                )
            self._state[snapshot.key] = _KeyState(
                generation=snapshot.generation,
                last_sequence=snapshot.sequence,
                last_payload_sha256=snapshot.payload_sha256,
                last_market_state=snapshot.market_state,
            )
            if recovery:
                self._recovery_events += 1
            self._pending[snapshot.key] = snapshot
            return SubmissionResult(
                True,
                False,
                False,
                False,
                False,
                "recovered" if recovery else "accepted",
            )

        if snapshot.generation != state.generation:
            if not recovery:
                self._mark_gap(state)
                return SubmissionResult(
                    accepted=False,
                    coalesced=False,
                    local_capacity_drop=False,
                    gap=True,
                    resnapshot_required=True,
                    reason="stream generation changed without explicit recovery snapshot",
                )
            if snapshot.key not in self._pending and len(self._pending) >= self._capacity:
                self._local_capacity_drops += 1
                state.local_loss = True
                return SubmissionResult(
                    accepted=False,
                    coalesced=False,
                    local_capacity_drop=True,
                    gap=state.gap,
                    resnapshot_required=True,
                    reason="local work capacity exhausted",
                )
            self._record_safety_transition(state, snapshot.market_state)
            state.generation = snapshot.generation
            state.last_sequence = snapshot.sequence
            state.last_payload_sha256 = snapshot.payload_sha256
            state.last_market_state = snapshot.market_state
            state.gap = False
            state.local_loss = False
            state.coalesced_since_drain = 0
            self._recovery_events += 1
            self._pending[snapshot.key] = snapshot
            return SubmissionResult(
                True, False, False, False, False, "generation recovered"
            )

        if snapshot.sequence < state.last_sequence:
            self._out_of_order_rejections += 1
            return SubmissionResult(
                accepted=False,
                coalesced=False,
                local_capacity_drop=False,
                gap=state.gap,
                resnapshot_required=state.gap or state.local_loss,
                reason="out-of-order snapshot rejected",
            )

        if snapshot.sequence == state.last_sequence:
            if (state.gap or state.local_loss) and recovery:
                if snapshot.key not in self._pending and len(self._pending) >= self._capacity:
                    self._local_capacity_drops += 1
                    state.local_loss = True
                    return SubmissionResult(
                        accepted=False,
                        coalesced=False,
                        local_capacity_drop=True,
                        gap=state.gap,
                        resnapshot_required=True,
                        reason="local work capacity exhausted",
                    )
                self._record_safety_transition(state, snapshot.market_state)
                state.last_payload_sha256 = snapshot.payload_sha256
                state.last_market_state = snapshot.market_state
                state.gap = False
                state.local_loss = False
                state.coalesced_since_drain = 0
                self._recovery_events += 1
                self._pending[snapshot.key] = snapshot
                return SubmissionResult(True, False, False, False, False, "recovered")
            if state.local_loss:
                return SubmissionResult(
                    accepted=False,
                    coalesced=False,
                    local_capacity_drop=False,
                    gap=state.gap,
                    resnapshot_required=True,
                    reason="local capacity loss is sticky until explicit complete recovery snapshot",
                )
            if snapshot.payload_sha256 != state.last_payload_sha256:
                self._conflicting_sequence_events += 1
                self._mark_gap(state)
                return SubmissionResult(
                    accepted=False,
                    coalesced=False,
                    local_capacity_drop=False,
                    gap=True,
                    resnapshot_required=True,
                    reason="same sequence carried conflicting atomic snapshot identity",
                )
            return SubmissionResult(
                accepted=False,
                coalesced=False,
                local_capacity_drop=False,
                gap=state.gap,
                resnapshot_required=state.gap,
                reason="duplicate snapshot",
            )

        if state.local_loss and not recovery:
            return SubmissionResult(
                accepted=False,
                coalesced=False,
                local_capacity_drop=False,
                gap=state.gap,
                resnapshot_required=True,
                reason="local capacity loss is sticky until explicit complete recovery snapshot",
            )

        if state.gap and not recovery:
            state.last_sequence = snapshot.sequence
            state.last_payload_sha256 = snapshot.payload_sha256
            self._record_safety_transition(state, snapshot.market_state)
            state.last_market_state = snapshot.market_state
            return SubmissionResult(
                accepted=False,
                coalesced=False,
                local_capacity_drop=False,
                gap=True,
                resnapshot_required=True,
                reason="gap is sticky until explicit complete recovery snapshot",
            )

        coalesced = snapshot.key in self._pending
        if not coalesced and len(self._pending) >= self._capacity:
            self._local_capacity_drops += 1
            state.local_loss = True
            return SubmissionResult(
                accepted=False,
                coalesced=False,
                local_capacity_drop=True,
                gap=state.gap,
                resnapshot_required=True,
                reason="local work capacity exhausted",
            )

        self._record_safety_transition(state, snapshot.market_state)
        state.last_sequence = snapshot.sequence
        state.last_payload_sha256 = snapshot.payload_sha256
        state.last_market_state = snapshot.market_state
        if recovery:
            state.gap = False
            state.local_loss = False
            self._recovery_events += 1

        if coalesced:
            self._coalesced_snapshots += 1
            state.coalesced_since_drain += 1
        self._pending[snapshot.key] = snapshot
        return SubmissionResult(
            accepted=True,
            coalesced=coalesced,
            local_capacity_drop=False,
            gap=state.gap,
            resnapshot_required=state.gap or state.local_loss,
            reason="recovered" if recovery else "accepted",
        )

    def submit_delta(
        self,
        *,
        key: MarketStreamKey,
        generation: str,
        sequence: int,
        delta_sha256: str,
    ) -> SubmissionResult:
        """Reject lossy delta coalescing and require authoritative resnapshot."""

        if type(key) is not MarketStreamKey:
            raise TypeError("key must be exact MarketStreamKey")
        if not isinstance(generation, str) or not generation.strip():
            raise LiveMarketBackpressureError("generation must be non-blank")
        if type(sequence) is not int or sequence < 0:
            raise LiveMarketBackpressureError("sequence must be a non-negative integer")
        _sha256(delta_sha256, "delta_sha256")
        self._submitted_deltas += 1
        state = self._state.get(key)
        if state is None:
            self._provider_gap_events += 1
            self._state[key] = _KeyState(
                generation=generation,
                last_sequence=sequence,
                last_payload_sha256=delta_sha256,
                last_market_state="UNKNOWN",
                gap=True,
            )
        else:
            if generation != state.generation:
                state.generation = generation
                state.last_sequence = sequence
            elif sequence > state.last_sequence:
                state.last_sequence = sequence
            self._mark_gap(state)
        self._pending.pop(key, None)
        return SubmissionResult(
            accepted=False,
            coalesced=False,
            local_capacity_drop=False,
            gap=True,
            resnapshot_required=True,
            reason="delta cannot be latest-win coalesced; complete recovery snapshot required",
        )

    def drain(self) -> tuple[CoalescedMarketView, ...]:
        """Return deterministic provider/key order; draining grants no new truth."""

        views: list[CoalescedMarketView] = []
        for key in sorted(self._pending):
            snapshot = self._pending[key]
            state = self._state[key]
            resnapshot_required = state.gap or state.local_loss
            views.append(
                CoalescedMarketView(
                    snapshot=snapshot,
                    trusted_current_view=not resnapshot_required,
                    resnapshot_required=resnapshot_required,
                    causal_replay_safe=False,
                    coalesced_snapshot_count=state.coalesced_since_drain,
                    safety_transition_count=state.safety_since_drain,
                )
            )
            state.coalesced_since_drain = 0
            state.safety_since_drain = 0
        self._pending.clear()
        return tuple(views)

    def requires_resnapshot(self, key: MarketStreamKey) -> bool:
        if type(key) is not MarketStreamKey:
            raise TypeError("key must be exact MarketStreamKey")
        state = self._state.get(key)
        return state is not None and (state.gap or state.local_loss)

    def _reserve_new_key(self, key: MarketStreamKey) -> bool:
        if key in self._pending or len(self._pending) < self._capacity:
            return True
        self._local_capacity_drops += 1
        return False

    def _mark_gap(self, state: _KeyState) -> None:
        if not state.gap:
            self._provider_gap_events += 1
        state.gap = True

    def _record_safety_transition(self, state: _KeyState, next_state: str) -> None:
        if next_state != state.last_market_state and (
            next_state != _OPEN_STATE or state.last_market_state != _OPEN_STATE
        ):
            self._safety_transitions += 1
            state.safety_since_drain += 1


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise LiveMarketBackpressureError(f"{label} must be lowercase sha256 hex")


def _utc(value: datetime, label: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise LiveMarketBackpressureError(f"{label} must be timezone-aware UTC")
