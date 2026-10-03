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
_CANONICAL_PATH_EQUALITY = Path.__eq__
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
    expected_store_path: Path,
) -> Path:
    current = getattr(store, "path", None)
    if not isinstance(current, Path):
        raise ScheduledSourceUniverseError(
            "canonical collector store path identity is unavailable"
        )
    if type(expected_store_path) is not type(current):
        raise TypeError(
            "expected_store_path must be the exact canonical Path type"
        )
    if _CANONICAL_PATH_EQUALITY(current, expected_store_path) is not True:
        raise ScheduledSourceUniverseError(
            "collector store path does not match product-expected authority path"
        )
    return expected_store_path


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
    expected_store_path: Path,
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

def _seal_scheduled_source_universe_dispatch() -> None:
    """Seal positive schedule-coverage dispatch against ordinary runtime rebinding."""

    module_globals = globals()
    expected_store_type = CollectorDeltaStore
    expected_commitment_type = SourceUniverseCommitment
    expected_source_error_type = SourceUniverseCommitmentError
    expected_resolution_type = ScheduledSourceUniverseResolution
    expected_resolution_field_names = tuple(expected_resolution_type.__slots__)
    expected_error_type = ScheduledSourceUniverseError
    expected_path_type = Path
    expected_hashlib = hashlib
    expected_sha256_fn = hashlib.sha256
    expected_json = json
    expected_json_dumps = json.dumps
    expected_json_dumps_code = getattr(expected_json_dumps, "__code__", None)
    expected_inspect = inspect
    expected_getattr_static = inspect.getattr_static
    expected_getattr_static_code = getattr(
        expected_getattr_static, "__code__", None
    )
    expected_path_equality = _CANONICAL_PATH_EQUALITY
    expected_path_equality_code = getattr(
        expected_path_equality, "__code__", None
    )
    expected_schedule_evidence = _CANONICAL_SCHEDULE_EVIDENCE
    expected_schedule_evidence_code = getattr(
        expected_schedule_evidence, "__code__", None
    )
    # The schedule reader and its witnessed helper methods share one defining-module
    # globals dictionary.  Seal the authority-bearing bindings used for frozen scope,
    # due-time classification and commitment construction so unchanged method code
    # cannot late-dispatch through a hostile module-global replacement.
    expected_schedule_globals = expected_schedule_evidence.__globals__
    expected_schedule_text = expected_schedule_globals.get("_text")
    expected_schedule_text_code = getattr(expected_schedule_text, "__code__", None)
    expected_schedule_instant = expected_schedule_globals.get("_instant")
    expected_schedule_instant_code = getattr(
        expected_schedule_instant, "__code__", None
    )
    expected_schedule_policy = expected_schedule_globals.get("_SCHEDULE_POLICY")
    expected_schedule_max_slots = expected_schedule_globals.get(
        "_MAX_SCHEDULE_EVIDENCE_SLOTS"
    )
    expected_schedule_hashlib = expected_schedule_globals.get("hashlib")
    expected_schedule_sha256 = getattr(expected_schedule_hashlib, "sha256", None)
    expected_schedule_json = expected_schedule_globals.get("json")
    expected_schedule_json_dumps = getattr(expected_schedule_json, "dumps", None)
    expected_schedule_json_dumps_code = getattr(
        expected_schedule_json_dumps, "__code__", None
    )
    expected_schedule_math = expected_schedule_globals.get("math")
    expected_schedule_isfinite = getattr(expected_schedule_math, "isfinite", None)
    expected_schedule_timedelta = expected_schedule_globals.get("timedelta")
    expected_schedule_read_names = _CANONICAL_SCHEDULE_READ_SEAMS
    expected_schedule_class_seams = _CANONICAL_SCHEDULE_CLASS_READ_SEAMS
    expected_schedule_class_witnesses = tuple(
        (
            name,
            expected,
            getattr(expected, "__func__", expected),
            getattr(
                getattr(expected, "__func__", expected),
                "__code__",
                None,
            ),
        )
        for name, expected in sorted(expected_schedule_class_seams.items())
    )
    expected_store_base_type = expected_store_type.__mro__[1]
    expected_base_connect_surface = expected_getattr_static(
        expected_store_base_type, "_connect"
    )
    expected_base_connect_callable = getattr(
        expected_base_connect_surface, "__func__", expected_base_connect_surface
    )
    expected_base_connect_code = getattr(
        expected_base_connect_callable, "__code__", None
    )
    expected_base_connect_path_surface = expected_getattr_static(
        expected_store_base_type, "_connect_path"
    )
    expected_base_connect_path_callable = getattr(
        expected_base_connect_path_surface,
        "__func__",
        expected_base_connect_path_surface,
    )
    expected_base_connect_path_code = getattr(
        expected_base_connect_path_callable, "__code__", None
    )
    expected_base_connect_path_globals = expected_base_connect_path_callable.__globals__
    expected_base_sqlite3 = expected_base_connect_path_globals.get("sqlite3")
    expected_base_sqlite_connect = getattr(expected_base_sqlite3, "connect", None)
    expected_base_sqlite_row = getattr(expected_base_sqlite3, "Row", None)
    expected_path_identity_surface = expected_class_seams["_path_file_identity"]
    expected_path_identity_callable = getattr(
        expected_path_identity_surface, "__func__", expected_path_identity_surface
    )
    expected_path_identity_globals = expected_path_identity_callable.__globals__
    expected_path_os = expected_path_identity_globals.get("os")
    expected_path_os_stat = getattr(expected_path_os, "stat", None)
    expected_schedule_keys = _SCHEDULE_KEYS
    expected_slot_keys = _SLOT_KEYS
    expected_hex = _HEX
    expected_verifier = verify_source_universe_commitment
    expected_verifier_code = expected_verifier.__code__
    expected_resolution_field_surfaces = tuple(
        (
            name,
            expected_getattr_static(expected_resolution_type, name),
        )
        for name in expected_resolution_field_names
    )
    expected_resolution_issue_surface = expected_getattr_static(
        expected_resolution_type, "_issue"
    )
    expected_resolution_issue_function = getattr(
        expected_resolution_issue_surface, "__func__", None
    )
    expected_resolution_issue_function_code = getattr(
        expected_resolution_issue_function, "__code__", None
    )
    helper_witnesses = tuple(
        (
            name,
            helper,
            getattr(helper, "__code__", None),
        )
        for name, helper in (
            (
                "_require_canonical_schedule_class_read_seams",
                _require_canonical_schedule_class_read_seams,
            ),
            ("_text", _text),
            ("_ordinal", _ordinal),
            ("_sha256", _sha256),
            ("_canonical_json", _canonical_json),
            ("_require_expected_store_path", _require_expected_store_path),
            ("_read_schedule_evidence", _read_schedule_evidence),
        )
    )
    original_resolver = resolve_scheduled_source_universe
    original_resolver_code = original_resolver.__code__

    def require_dispatch_integrity() -> None:
        if module_globals.get("CollectorDeltaStore") is not expected_store_type:
            raise expected_error_type(
                "scheduled source-universe collector type authority is rebound"
            )
        if (
            module_globals.get("SourceUniverseCommitment")
            is not expected_commitment_type
        ):
            raise expected_error_type(
                "scheduled source-universe commitment type authority is rebound"
            )
        if (
            module_globals.get("SourceUniverseCommitmentError")
            is not expected_source_error_type
        ):
            raise expected_error_type(
                "scheduled source-universe source error authority is rebound"
            )
        if (
            module_globals.get("ScheduledSourceUniverseResolution")
            is not expected_resolution_type
        ):
            raise expected_error_type(
                "scheduled source-universe result type authority is rebound"
            )
        if module_globals.get("ScheduledSourceUniverseError") is not expected_error_type:
            raise expected_error_type(
                "scheduled source-universe error authority is rebound"
            )
        if module_globals.get("Path") is not expected_path_type:
            raise expected_error_type(
                "scheduled source-universe path authority is rebound"
            )
        if (
            module_globals.get("_CANONICAL_PATH_EQUALITY")
            is not expected_path_equality
            or getattr(expected_path_equality, "__code__", None)
            is not expected_path_equality_code
        ):
            raise expected_error_type(
                "scheduled source-universe path comparison authority is rebound or mutated"
            )
        if (
            module_globals.get("hashlib") is not expected_hashlib
            or expected_hashlib.sha256 is not expected_sha256_fn
        ):
            raise expected_error_type(
                "scheduled source-universe digest authority is rebound"
            )
        if (
            module_globals.get("json") is not expected_json
            or expected_json.dumps is not expected_json_dumps
            or getattr(expected_json_dumps, "__code__", None)
            is not expected_json_dumps_code
        ):
            raise expected_error_type(
                "scheduled source-universe canonical JSON authority is rebound"
            )
        if (
            module_globals.get("inspect") is not expected_inspect
            or expected_inspect.getattr_static is not expected_getattr_static
            or getattr(expected_getattr_static, "__code__", None)
            is not expected_getattr_static_code
        ):
            raise expected_error_type(
                "scheduled source-universe reflection authority is rebound"
            )
        if (
            module_globals.get("_CANONICAL_SCHEDULE_EVIDENCE")
            is not expected_schedule_evidence
            or getattr(expected_schedule_evidence, "__code__", None)
            is not expected_schedule_evidence_code
        ):
            raise expected_error_type(
                "scheduled source-universe schedule evidence authority is rebound"
            )
        current_schedule_globals = getattr(
            expected_schedule_evidence, "__globals__", None
        )
        if current_schedule_globals is not expected_schedule_globals:
            raise expected_error_type(
                "scheduled source-universe collector global authority drifted"
            )
        if (
            current_schedule_globals.get("_text") is not expected_schedule_text
            or getattr(expected_schedule_text, "__code__", None)
            is not expected_schedule_text_code
        ):
            raise expected_error_type(
                "scheduled source-universe collector text authority is rebound or mutated"
            )
        if (
            current_schedule_globals.get("_instant") is not expected_schedule_instant
            or getattr(expected_schedule_instant, "__code__", None)
            is not expected_schedule_instant_code
        ):
            raise expected_error_type(
                "scheduled source-universe collector time authority is rebound or mutated"
            )
        if (
            current_schedule_globals.get("_SCHEDULE_POLICY")
            is not expected_schedule_policy
            or current_schedule_globals.get("_MAX_SCHEDULE_EVIDENCE_SLOTS")
            is not expected_schedule_max_slots
        ):
            raise expected_error_type(
                "scheduled source-universe collector schedule constants drifted"
            )
        if (
            current_schedule_globals.get("hashlib") is not expected_schedule_hashlib
            or getattr(expected_schedule_hashlib, "sha256", None)
            is not expected_schedule_sha256
        ):
            raise expected_error_type(
                "scheduled source-universe collector digest authority is rebound"
            )
        current_schedule_json = current_schedule_globals.get("json")
        if (
            current_schedule_json is not expected_schedule_json
            or getattr(expected_schedule_json, "dumps", None)
            is not expected_schedule_json_dumps
            or getattr(expected_schedule_json_dumps, "__code__", None)
            is not expected_schedule_json_dumps_code
        ):
            raise expected_error_type(
                "scheduled source-universe collector JSON authority is rebound or mutated"
            )
        current_schedule_math = current_schedule_globals.get("math")
        if (
            current_schedule_math is not expected_schedule_math
            or getattr(expected_schedule_math, "isfinite", None)
            is not expected_schedule_isfinite
            or current_schedule_globals.get("timedelta")
            is not expected_schedule_timedelta
        ):
            raise expected_error_type(
                "scheduled source-universe collector due-time authority is rebound"
            )
        if (
            module_globals.get("_CANONICAL_SCHEDULE_READ_SEAMS")
            is not expected_schedule_read_names
        ):
            raise expected_error_type(
                "scheduled source-universe read-seam identity is rebound"
            )
        current_class_seams = module_globals.get(
            "_CANONICAL_SCHEDULE_CLASS_READ_SEAMS"
        )
        if (
            current_class_seams is not expected_schedule_class_seams
            or type(current_class_seams) is not dict
        ):
            raise expected_error_type(
                "scheduled source-universe class read-seam witness map is rebound"
            )
        for (
            name,
            expected_surface,
            expected_callable,
            expected_code,
        ) in expected_schedule_class_witnesses:
            current_surface = expected_getattr_static(
                expected_store_type, name, None
            )
            current_callable = getattr(
                current_surface,
                "__func__",
                current_surface,
            )
            if (
                current_class_seams.get(name) is not expected_surface
                or current_surface is not expected_surface
                or current_callable is not expected_callable
                or getattr(current_callable, "__code__", None) is not expected_code
            ):
                raise expected_error_type(
                    "scheduled source-universe canonical read seam drifted: " + name
                )
        if expected_store_type.__mro__[1] is not expected_store_base_type:
            raise expected_error_type(
                "scheduled source-universe collector base type authority is rebound"
            )
        current_base_connect_surface = expected_getattr_static(
            expected_store_base_type, "_connect", None
        )
        current_base_connect_callable = getattr(
            current_base_connect_surface,
            "__func__",
            current_base_connect_surface,
        )
        if (
            current_base_connect_surface is not expected_base_connect_surface
            or current_base_connect_callable is not expected_base_connect_callable
            or getattr(current_base_connect_callable, "__code__", None)
            is not expected_base_connect_code
        ):
            raise expected_error_type(
                "scheduled source-universe collector base connection authority is rebound or mutated"
            )
        current_base_connect_path_surface = expected_getattr_static(
            expected_store_base_type, "_connect_path", None
        )
        current_base_connect_path_callable = getattr(
            current_base_connect_path_surface,
            "__func__",
            current_base_connect_path_surface,
        )
        if (
            current_base_connect_path_surface is not expected_base_connect_path_surface
            or current_base_connect_path_callable
            is not expected_base_connect_path_callable
            or getattr(current_base_connect_path_callable, "__code__", None)
            is not expected_base_connect_path_code
            or getattr(current_base_connect_path_callable, "__globals__", None)
            is not expected_base_connect_path_globals
            or expected_base_connect_path_globals.get("sqlite3")
            is not expected_base_sqlite3
            or getattr(expected_base_sqlite3, "connect", None)
            is not expected_base_sqlite_connect
            or getattr(expected_base_sqlite3, "Row", None)
            is not expected_base_sqlite_row
        ):
            raise expected_error_type(
                "scheduled source-universe collector SQLite connection authority drifted"
            )
        current_path_identity_surface = expected_getattr_static(
            expected_store_type, "_path_file_identity", None
        )
        current_path_identity_callable = getattr(
            current_path_identity_surface,
            "__func__",
            current_path_identity_surface,
        )
        if (
            current_path_identity_surface is not expected_path_identity_surface
            or current_path_identity_callable is not expected_path_identity_callable
            or getattr(current_path_identity_callable, "__globals__", None)
            is not expected_path_identity_globals
            or expected_path_identity_globals.get("os") is not expected_path_os
            or getattr(expected_path_os, "stat", None) is not expected_path_os_stat
        ):
            raise expected_error_type(
                "scheduled source-universe collector file-identity authority drifted"
            )
        if module_globals.get("_SCHEDULE_KEYS") is not expected_schedule_keys:
            raise expected_error_type(
                "scheduled source-universe schedule schema authority is rebound"
            )
        if module_globals.get("_SLOT_KEYS") is not expected_slot_keys:
            raise expected_error_type(
                "scheduled source-universe slot schema authority is rebound"
            )
        if module_globals.get("_HEX") is not expected_hex:
            raise expected_error_type(
                "scheduled source-universe digest alphabet authority is rebound"
            )
        if (
            module_globals.get("verify_source_universe_commitment")
            is not expected_verifier
            or expected_verifier.__code__ is not expected_verifier_code
        ):
            raise expected_error_type(
                "scheduled source-universe verifier authority is rebound"
            )
        for name, expected_surface in expected_resolution_field_surfaces:
            if (
                expected_getattr_static(expected_resolution_type, name, None)
                is not expected_surface
            ):
                raise expected_error_type(
                    "scheduled source-universe result field surface is rebound: "
                    + name
                )
        current_resolution_issue_surface = expected_getattr_static(
            expected_resolution_type, "_issue", None
        )
        if (
            current_resolution_issue_surface is not expected_resolution_issue_surface
            or getattr(current_resolution_issue_surface, "__func__", None)
            is not expected_resolution_issue_function
            or getattr(expected_resolution_issue_function, "__code__", None)
            is not expected_resolution_issue_function_code
        ):
            raise expected_error_type(
                "scheduled source-universe result issuance surface is rebound or mutated"
            )
        for name, expected_helper, expected_code in helper_witnesses:
            current_helper = module_globals.get(name)
            if (
                current_helper is not expected_helper
                or getattr(expected_helper, "__code__", None) is not expected_code
            ):
                raise expected_error_type(
                    "scheduled source-universe helper authority is rebound: " + name
                )
        if original_resolver.__code__ is not original_resolver_code:
            raise expected_error_type(
                "scheduled source-universe resolver implementation drifted"
            )

    def sealed_resolve_scheduled_source_universe(
        store: CollectorDeltaStore,
        candidate_source_universe: SourceUniverseCommitment,
        *,
        expected_store_path: Path,
        expected_source_id: str,
        expected_run_id: str,
        expected_start_slot_ordinal: int,
        expected_end_slot_ordinal: int,
    ) -> ScheduledSourceUniverseResolution:
        if (
            module_globals.get("resolve_scheduled_source_universe")
            is not sealed_resolve_scheduled_source_universe
        ):
            raise expected_error_type(
                "scheduled source-universe public resolver authority is rebound"
            )
        require_dispatch_integrity()
        result = original_resolver(
            store,
            candidate_source_universe,
            expected_store_path=expected_store_path,
            expected_source_id=expected_source_id,
            expected_run_id=expected_run_id,
            expected_start_slot_ordinal=expected_start_slot_ordinal,
            expected_end_slot_ordinal=expected_end_slot_ordinal,
        )
        require_dispatch_integrity()
        if type(result) is not expected_resolution_type:
            raise expected_error_type(
                "scheduled source-universe resolver returned non-canonical result type"
            )
        return result

    sealed_resolve_scheduled_source_universe.__name__ = original_resolver.__name__
    sealed_resolve_scheduled_source_universe.__qualname__ = (
        original_resolver.__qualname__
    )
    sealed_resolve_scheduled_source_universe.__doc__ = original_resolver.__doc__
    sealed_resolve_scheduled_source_universe.__module__ = original_resolver.__module__
    sealed_resolve_scheduled_source_universe.__annotations__ = dict(
        original_resolver.__annotations__
    )
    module_globals["resolve_scheduled_source_universe"] = (
        sealed_resolve_scheduled_source_universe
    )


_seal_scheduled_source_universe_dispatch()
del _seal_scheduled_source_universe_dispatch

