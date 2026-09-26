from __future__ import annotations

"""Compose frozen collector schedule coverage with canonical cycle-universe truth.

This module owns no scheduler, cycle ledger, provider universe, promotion decision, or
money authority. Positive fields are valid only as the direct return value of
resolve_scheduled_source_universe against the product-expected store/source/run/slot
scope; callers must not trust a separately supplied resolution object.
"""

import hashlib
import inspect
import json
from dataclasses import dataclass
from pathlib import Path

from .causal_collector import CollectorDeltaStore
from .source_universe_commitment import (
    SourceUniverseCommitment,
    SourceUniverseCommitmentError,
    verify_source_universe_commitment,
)


_CANONICAL_SCHEDULE_EVIDENCE = CollectorDeltaStore.collector_schedule_evidence
_CANONICAL_SCHEDULE_READ_SEAMS = frozenset(
    {
        "collector_schedule_evidence",
        "_connect",
        "_connect_path",
        "_path_file_identity",
        "_collector_schedule_id",
        "_collector_schedule_due_at",
        "_schedule_max_items",
        "_schedule_evaluation_window",
        "_cycle_terminal_payload_json",
    }
)
_CANONICAL_SCHEDULE_CLASS_READ_SEAMS = {
    name: inspect.getattr_static(CollectorDeltaStore, name)
    for name in _CANONICAL_SCHEDULE_READ_SEAMS
}
_SCHEDULE_KEYS = frozenset(
    {
        "schema_version",
        "schedule_id",
        "policy",
        "source_id",
        "run_id",
        "stream_epoch",
        "anchor_at",
        "interval_seconds",
        "max_items",
        "evaluation_start_slot_ordinal",
        "evaluation_end_slot_ordinal",
        "start_slot_ordinal",
        "end_slot_ordinal",
        "expected_slot_count",
        "bound_start_count",
        "missing_start_count",
        "early_start_count",
        "late_start_count",
        "slots",
        "commitment_sha256",
    }
)
_SLOT_KEYS = frozenset(
    {
        "slot_ordinal",
        "due_at",
        "cycle_seq",
        "stream_epoch",
        "attempted_at",
        "started_before_due",
        "started_late",
    }
)
_HEX = frozenset("0123456789abcdef")


class ScheduledSourceUniverseError(ValueError):
    """Frozen schedule and canonical source-universe evidence do not compose."""


def _require_canonical_schedule_class_read_seams() -> None:
    """Reject runtime replacement of transitive schedule read authority."""

    rebound = sorted(
        name
        for name, expected in _CANONICAL_SCHEDULE_CLASS_READ_SEAMS.items()
        if inspect.getattr_static(CollectorDeltaStore, name, None) is not expected
    )
    if rebound:
        raise ScheduledSourceUniverseError(
            "store canonical schedule read seam is class-rebound: "
            + ", ".join(rebound)
        )


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ScheduledSourceUniverseError(
            f"{name} must be a non-empty trimmed string"
        )
    return value


def _ordinal(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ScheduledSourceUniverseError(
            f"{name} must be a non-negative integer"
        )
    return value


def _sha256(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(character not in _HEX for character in value)
    ):
        raise ScheduledSourceUniverseError(
            f"{name} must be a canonical SHA-256 hex string"
        )
    return value


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ScheduledSourceUniverseError(
            "scheduled source-universe resolution is not canonical JSON"
        ) from exc


def _require_expected_store_path(
    store: CollectorDeltaStore,
    expected_store_path: str | Path,
) -> Path:
    if isinstance(expected_store_path, str):
        if not expected_store_path or expected_store_path.strip() != expected_store_path:
            raise ScheduledSourceUniverseError(
                "expected_store_path must be a non-empty trimmed path"
            )
        expected = Path(expected_store_path)
    elif isinstance(expected_store_path, Path):
        expected = expected_store_path
    else:
        raise TypeError("expected_store_path must be str or Path")
    current = getattr(store, "path", None)
    if not isinstance(current, Path):
        raise ScheduledSourceUniverseError(
            "canonical collector store path identity is unavailable"
        )
    if current != expected:
        raise ScheduledSourceUniverseError(
            "collector store path does not match product-expected authority path"
        )
    return expected


@dataclass(frozen=True, slots=True, init=False)
class ScheduledSourceUniverseResolution:
    """Read-only projection returned by canonical re-resolution, not authority by possession."""

    schema_version: int
    source_id: str
    run_id: str
    stream_epoch: str
    schedule_id: str
    schedule_policy: str
    start_slot_ordinal: int
    end_slot_ordinal: int
    expected_slot_count: int
    cycle_sequences: tuple[int, ...]
    start_cycle_seq: int
    end_cycle_seq: int
    late_start_count: int
    schedule_commitment_sha256: str
    source_universe_commitment_sha256: str
    source_cycle_evidence_sha256: str
    scheduled_start_coverage_complete: bool
    observation_ledger_complete: bool
    scheduled_provider_observation_complete: bool
    external_provider_universe_complete: bool
    promotion_ready: bool
    resolution_sha256: str

    def __new__(
        cls, *args: object, **kwargs: object
    ) -> "ScheduledSourceUniverseResolution":
        raise TypeError(
            "ScheduledSourceUniverseResolution is resolver-issued; "
            "call resolve_scheduled_source_universe"
        )

    @classmethod
    def _issue(
        cls, payload: dict[str, object]
    ) -> "ScheduledSourceUniverseResolution":
        instance = object.__new__(cls)
        for field_name, value in payload.items():
            object.__setattr__(instance, field_name, value)
        return instance

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "run_id": self.run_id,
            "stream_epoch": self.stream_epoch,
            "schedule_id": self.schedule_id,
            "schedule_policy": self.schedule_policy,
            "start_slot_ordinal": self.start_slot_ordinal,
            "end_slot_ordinal": self.end_slot_ordinal,
            "expected_slot_count": self.expected_slot_count,
            "cycle_sequences": list(self.cycle_sequences),
            "start_cycle_seq": self.start_cycle_seq,
            "end_cycle_seq": self.end_cycle_seq,
            "late_start_count": self.late_start_count,
            "schedule_commitment_sha256": self.schedule_commitment_sha256,
            "source_universe_commitment_sha256": self.source_universe_commitment_sha256,
            "source_cycle_evidence_sha256": self.source_cycle_evidence_sha256,
            "scheduled_start_coverage_complete": self.scheduled_start_coverage_complete,
            "observation_ledger_complete": self.observation_ledger_complete,
            "scheduled_provider_observation_complete": (
                self.scheduled_provider_observation_complete
            ),
            "external_provider_universe_complete": self.external_provider_universe_complete,
            "promotion_ready": self.promotion_ready,
            "resolution_sha256": self.resolution_sha256,
        }


def _read_schedule_evidence(
    store: CollectorDeltaStore,
    *,
    source_id: str,
    run_id: str,
    start_slot_ordinal: int,
    end_slot_ordinal: int,
) -> dict[str, object]:
    _require_canonical_schedule_class_read_seams()
    instance_state = vars(store)
    rebound = sorted(
        name for name in _CANONICAL_SCHEDULE_READ_SEAMS if name in instance_state
    )
    if rebound:
        raise TypeError(
            "store canonical schedule read seam is instance-rebound: "
            + ", ".join(rebound)
        )
    evidence = _CANONICAL_SCHEDULE_EVIDENCE(
        store,
        source_id=source_id,
        run_id=run_id,
        start_slot_ordinal=start_slot_ordinal,
        end_slot_ordinal=end_slot_ordinal,
    )
    _require_canonical_schedule_class_read_seams()
    if type(evidence) is not dict or set(evidence) != _SCHEDULE_KEYS:
        raise ScheduledSourceUniverseError(
            "collector schedule evidence schema is not canonical"
        )
    return evidence


def resolve_scheduled_source_universe(
    store: CollectorDeltaStore,
    candidate_source_universe: SourceUniverseCommitment,
    *,
    expected_store_path: str | Path,
    expected_source_id: str,
    expected_run_id: str,
    expected_start_slot_ordinal: int,
    expected_end_slot_ordinal: int,
) -> ScheduledSourceUniverseResolution:
    """Re-resolve exact frozen schedule slots and the exact cycles they bound.

    A positive schedule-START coverage result exists only when every expected slot has
    exactly one canonical START, none starts before its frozen due time, and the
    scheduled cycle identities form exactly the contiguous cycle window verified by
    SourceUniverseCommitment. Late STARTs remain explicit rather than being
    relabelled on-time; terminal pending/failure truth remains owned by the source
    universe and prevents scheduled provider-observation completeness.
    """

    if type(store) is not CollectorDeltaStore:
        raise TypeError("store must be the exact canonical CollectorDeltaStore")
    if type(candidate_source_universe) is not SourceUniverseCommitment:
        raise TypeError(
            "candidate_source_universe must be an exact SourceUniverseCommitment"
        )
    expected_path = _require_expected_store_path(store, expected_store_path)
    source_id = _text(expected_source_id, "expected_source_id")
    run_id = _text(expected_run_id, "expected_run_id")
    start_slot = _ordinal(
        expected_start_slot_ordinal, "expected_start_slot_ordinal"
    )
    end_slot = _ordinal(expected_end_slot_ordinal, "expected_end_slot_ordinal")
    if end_slot < start_slot:
        raise ScheduledSourceUniverseError(
            "expected_end_slot_ordinal cannot precede expected_start_slot_ordinal"
        )

    schedule = _read_schedule_evidence(
        store,
        source_id=source_id,
        run_id=run_id,
        start_slot_ordinal=start_slot,
        end_slot_ordinal=end_slot,
    )
    if schedule["schema_version"] != 4:
        raise ScheduledSourceUniverseError(
            "collector schedule evidence schema_version is unsupported"
        )
    if schedule["source_id"] != source_id or schedule["run_id"] != run_id:
        raise ScheduledSourceUniverseError(
            "collector schedule evidence crosses product-expected source/run scope"
        )
    if (
        schedule["start_slot_ordinal"] != start_slot
        or schedule["end_slot_ordinal"] != end_slot
    ):
        raise ScheduledSourceUniverseError(
            "collector schedule evidence crosses product-expected slot scope"
        )
    frozen_start_raw = schedule["evaluation_start_slot_ordinal"]
    frozen_end_raw = schedule["evaluation_end_slot_ordinal"]
    if frozen_start_raw is None or frozen_end_raw is None:
        raise ScheduledSourceUniverseError(
            "collector schedule lacks prospectively frozen evaluation window"
        )
    frozen_start = _ordinal(
        frozen_start_raw, "evaluation_start_slot_ordinal"
    )
    frozen_end = _ordinal(
        frozen_end_raw, "evaluation_end_slot_ordinal"
    )
    if frozen_end < frozen_start:
        raise ScheduledSourceUniverseError(
            "collector schedule frozen evaluation window is invalid"
        )
    if (start_slot, end_slot) != (frozen_start, frozen_end):
        raise ScheduledSourceUniverseError(
            "product-expected slot assertions do not match prospectively "
            "frozen evaluation window"
        )

    schedule_id = _text(schedule["schedule_id"], "schedule_id")
    schedule_policy = _text(schedule["policy"], "schedule_policy")
    stream_epoch = _text(schedule["stream_epoch"], "stream_epoch")
    if type(schedule["max_items"]) is not int or schedule["max_items"] <= 0:
        raise ScheduledSourceUniverseError(
            "collector schedule max_items is invalid"
        )
    schedule_commitment = _sha256(
        schedule["commitment_sha256"], "schedule_commitment_sha256"
    )
    expected_count = end_slot - start_slot + 1
    if (
        type(schedule["expected_slot_count"]) is not int
        or schedule["expected_slot_count"] != expected_count
    ):
        raise ScheduledSourceUniverseError(
            "collector schedule expected-slot count is inconsistent"
        )
    for name in (
        "bound_start_count",
        "missing_start_count",
        "early_start_count",
        "late_start_count",
    ):
        if type(schedule[name]) is not int or schedule[name] < 0:
            raise ScheduledSourceUniverseError(
                f"collector schedule {name} is invalid"
            )
    if schedule["bound_start_count"] != expected_count:
        raise ScheduledSourceUniverseError(
            "frozen schedule window has missing canonical START coverage"
        )
    if schedule["missing_start_count"] != 0:
        raise ScheduledSourceUniverseError(
            "frozen schedule window has missing canonical START coverage"
        )
    if schedule["early_start_count"] != 0:
        raise ScheduledSourceUniverseError(
            "frozen schedule window contains START before due_at"
        )

    slots = schedule["slots"]
    if type(slots) is not list or len(slots) != expected_count:
        raise ScheduledSourceUniverseError(
            "collector schedule slot projection is incomplete"
        )
    cycle_sequences: list[int] = []
    computed_late_count = 0
    for index, slot in enumerate(slots):
        if type(slot) is not dict or set(slot) != _SLOT_KEYS:
            raise ScheduledSourceUniverseError(
                "collector schedule slot evidence schema is not canonical"
            )
        expected_ordinal = start_slot + index
        if slot["slot_ordinal"] != expected_ordinal:
            raise ScheduledSourceUniverseError(
                "collector schedule slot order/identity is not canonical"
            )
        if slot["stream_epoch"] != stream_epoch:
            raise ScheduledSourceUniverseError(
                "collector schedule slot crosses frozen stream_epoch"
            )
        cycle_seq = slot["cycle_seq"]
        if type(cycle_seq) is not int or cycle_seq <= 0:
            raise ScheduledSourceUniverseError(
                "collector schedule slot lacks canonical cycle START identity"
            )
        if slot["started_before_due"] is not False:
            raise ScheduledSourceUniverseError(
                "collector schedule slot contains START before due_at"
            )
        if type(slot["started_late"]) is not bool:
            raise ScheduledSourceUniverseError(
                "collector schedule slot late-start state is invalid"
            )
        if type(slot["due_at"]) is not str or type(slot["attempted_at"]) is not str:
            raise ScheduledSourceUniverseError(
                "collector schedule slot timestamps are incomplete"
            )
        computed_late_count += int(slot["started_late"])
        cycle_sequences.append(cycle_seq)

    cycle_tuple = tuple(cycle_sequences)
    if len(set(cycle_tuple)) != expected_count:
        raise ScheduledSourceUniverseError(
            "collector schedule reuses a canonical cycle START identity"
        )
    first_cycle = cycle_tuple[0]
    last_cycle = cycle_tuple[-1]
    if cycle_tuple != tuple(range(first_cycle, last_cycle + 1)):
        raise ScheduledSourceUniverseError(
            "scheduled cycle identities are not one exact contiguous cycle window"
        )
    if schedule["late_start_count"] != computed_late_count:
        raise ScheduledSourceUniverseError(
            "collector schedule late-start count is inconsistent"
        )

    try:
        verified_source = verify_source_universe_commitment(
            store,
            candidate_source_universe,
            expected_store_path=expected_path,
            expected_source_id=source_id,
            expected_start_cycle_seq=first_cycle,
            expected_end_cycle_seq=last_cycle,
        )
    except (SourceUniverseCommitmentError, TypeError, ValueError) as exc:
        raise ScheduledSourceUniverseError(
            "source-universe commitment does not match scheduled cycle window"
        ) from exc
    if verified_source.cycle_count != expected_count:
        raise ScheduledSourceUniverseError(
            "source-universe cycle count does not match frozen schedule"
        )

    _require_expected_store_path(store, expected_path)
    schedule_after = _read_schedule_evidence(
        store,
        source_id=source_id,
        run_id=run_id,
        start_slot_ordinal=start_slot,
        end_slot_ordinal=end_slot,
    )
    if schedule_after != schedule:
        raise ScheduledSourceUniverseError(
            "collector schedule evidence changed during canonical re-resolution"
        )
    _require_expected_store_path(store, expected_path)

    authority_payload: dict[str, object] = {
        "schema_version": 1,
        "source_id": source_id,
        "run_id": run_id,
        "stream_epoch": stream_epoch,
        "schedule_id": schedule_id,
        "schedule_policy": schedule_policy,
        "start_slot_ordinal": start_slot,
        "end_slot_ordinal": end_slot,
        "expected_slot_count": expected_count,
        "cycle_sequences": list(cycle_tuple),
        "start_cycle_seq": first_cycle,
        "end_cycle_seq": last_cycle,
        "late_start_count": computed_late_count,
        "schedule_commitment_sha256": schedule_commitment,
        "source_universe_commitment_sha256": (
            verified_source.commitment_sha256
        ),
        "source_cycle_evidence_sha256": verified_source.cycle_evidence_sha256,
        "scheduled_start_coverage_complete": True,
        "observation_ledger_complete": verified_source.observation_ledger_complete,
        "scheduled_provider_observation_complete": (
            verified_source.provider_observation_complete
            and computed_late_count == 0
        ),
        "external_provider_universe_complete": False,
        "promotion_ready": False,
    }
    resolution_sha256 = hashlib.sha256(_canonical_json(authority_payload)).hexdigest()
    return ScheduledSourceUniverseResolution._issue(
        {
            **authority_payload,
            "cycle_sequences": cycle_tuple,
            "resolution_sha256": resolution_sha256,
        }
    )