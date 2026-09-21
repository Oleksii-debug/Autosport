from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from .causal_collector import CollectorDeltaStore
from .collector_service import CollectorServiceConfig


class ScheduledCycleCoverageError(ValueError):
    """Frozen schedule/cycle evidence cannot support canonical coverage truth."""


_SCHEDULE_SCHEMA = "autosport.frozen_acquisition_schedule"
_SCHEDULE_VERSION = 1
_COVERAGE_SCHEMA = "autosport.scheduled_cycle_coverage"
_COVERAGE_VERSION = 1
_SCHEDULES_TABLE = "collector_frozen_schedules_v1"
_SLOTS_TABLE = "collector_frozen_schedule_slots_v1"
_MAX_SLOTS = 1_000_000


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
        raise ScheduledCycleCoverageError("schedule evidence is not canonical JSON") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ScheduledCycleCoverageError(f"{name} must be a non-empty trimmed string")
    return value


def _instant(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduledCycleCoverageError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScheduledCycleCoverageError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _instant_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _timedelta_microseconds(value: timedelta) -> int:
    return (
        value.days * 86_400_000_000
        + value.seconds * 1_000_000
        + value.microseconds
    )


def _seconds_to_microseconds(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScheduledCycleCoverageError(f"{name} must be a finite positive number")
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise ScheduledCycleCoverageError(f"{name} must be a finite positive number")
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation as exc:
        raise ScheduledCycleCoverageError(f"{name} is not canonical") from exc
    micros = decimal_value * Decimal(1_000_000)
    integral = micros.to_integral_value()
    if micros != integral:
        raise ScheduledCycleCoverageError(
            f"{name} must resolve exactly to whole microseconds"
        )
    result = int(integral)
    if result <= 0:
        raise ScheduledCycleCoverageError(f"{name} must be positive")
    return result


def _decimal_text(value: object, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScheduledCycleCoverageError(f"{name} must be finite")
    if not math.isfinite(float(value)):
        raise ScheduledCycleCoverageError(f"{name} must be finite")
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation as exc:
        raise ScheduledCycleCoverageError(f"{name} is not canonical") from exc
    return format(decimal_value, "f")


def _collector_config_payload(config: CollectorServiceConfig) -> dict[str, object]:
    if not isinstance(config, CollectorServiceConfig):
        raise TypeError("collector_config must be CollectorServiceConfig")
    return {
        "max_items": config.max_items,
        "poll_interval_microseconds": _seconds_to_microseconds(
            config.poll_interval_seconds, "poll_interval_seconds"
        ),
        "retry_attempts": config.retry_attempts,
        "initial_backoff_microseconds": _seconds_to_microseconds(
            config.initial_backoff_seconds, "initial_backoff_seconds"
        ),
        "max_backoff_microseconds": _seconds_to_microseconds(
            config.max_backoff_seconds, "max_backoff_seconds"
        ),
        "jitter_fraction": _decimal_text(config.jitter_fraction, "jitter_fraction"),
        "max_store_bytes": config.max_store_bytes,
    }


def _require_store(store: CollectorDeltaStore) -> CollectorDeltaStore:
    if not isinstance(store, CollectorDeltaStore):
        raise TypeError("store must be the canonical CollectorDeltaStore")
    return store


@dataclass(frozen=True, slots=True)
class FrozenScheduleSlot:
    slot_index: int
    slot_id: str
    due_at: str
    window_end: str


@dataclass(frozen=True, slots=True, init=False)
class FrozenAcquisitionSchedule:
    schema_version: int
    schedule_id: str
    logical_key_sha256: str
    source_id: str
    adapter_id: str
    sport: str
    query_scope: object
    schedule_policy_version: str
    window_start: str
    window_end: str
    campaign_id: str
    protocol_id: str
    collector_config_sha256: str
    cadence_microseconds: int
    frozen_at: str
    slots: tuple[FrozenScheduleSlot, ...]
    commitment_sha256: str

    def __new__(cls, *args: object, **kwargs: object) -> "FrozenAcquisitionSchedule":
        raise TypeError(
            "FrozenAcquisitionSchedule is product-issued; use freeze_acquisition_schedule "
            "or load_frozen_acquisition_schedule"
        )

    @classmethod
    def _issue(cls, payload: dict[str, object]) -> "FrozenAcquisitionSchedule":
        instance = object.__new__(cls)
        for field_name, value in payload.items():
            object.__setattr__(instance, field_name, value)
        return instance


@dataclass(frozen=True, slots=True, init=False)
class ScheduledCycleCoverage:
    schema_version: int
    schedule_id: str
    schedule_commitment_sha256: str
    source_id: str
    slot_count: int
    started_slot_count: int
    started_cycle_count: int
    pending_cycle_count: int
    missing_slot_ids: tuple[str, ...]
    duplicate_slot_ids: tuple[str, ...]
    slot_cycle_ids: tuple[tuple[str, tuple[str, ...]], ...]
    coverage_commitment_sha256: str
    schedule_coverage_complete: bool
    external_provider_universe_complete: bool
    promotion_ready: bool

    def __new__(cls, *args: object, **kwargs: object) -> "ScheduledCycleCoverage":
        raise TypeError(
            "ScheduledCycleCoverage is product-issued; "
            "use resolve_scheduled_cycle_coverage"
        )

    @classmethod
    def _issue(cls, payload: dict[str, object]) -> "ScheduledCycleCoverage":
        instance = object.__new__(cls)
        for field_name, value in payload.items():
            object.__setattr__(instance, field_name, value)
        return instance


def _connect(store: CollectorDeltaStore) -> sqlite3.Connection:
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _ensure_schedule_schema(store: CollectorDeltaStore) -> None:
    connection = _connect(store)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {_SCHEDULES_TABLE} ("
            "schedule_id TEXT PRIMARY KEY NOT NULL,"
            "logical_key_sha256 TEXT UNIQUE NOT NULL,"
            "source_id TEXT NOT NULL,"
            "adapter_id TEXT NOT NULL,"
            "sport TEXT NOT NULL,"
            "query_scope_json TEXT NOT NULL,"
            "schedule_policy_version TEXT NOT NULL,"
            "window_start TEXT NOT NULL,"
            "window_end TEXT NOT NULL,"
            "campaign_id TEXT NOT NULL,"
            "protocol_id TEXT NOT NULL,"
            "collector_config_sha256 TEXT NOT NULL,"
            "cadence_microseconds INTEGER NOT NULL CHECK(cadence_microseconds > 0),"
            "frozen_at TEXT NOT NULL,"
            "slot_count INTEGER NOT NULL CHECK(slot_count > 0),"
            "commitment_sha256 TEXT NOT NULL)"
        )
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {_SLOTS_TABLE} ("
            "schedule_id TEXT NOT NULL,"
            "slot_index INTEGER NOT NULL CHECK(slot_index >= 0),"
            "slot_id TEXT UNIQUE NOT NULL,"
            "due_at TEXT NOT NULL,"
            "window_end TEXT NOT NULL,"
            "PRIMARY KEY(schedule_id, slot_index),"
            f"FOREIGN KEY(schedule_id) REFERENCES {_SCHEDULES_TABLE}(schedule_id))"
        )
        for table in (_SCHEDULES_TABLE, _SLOTS_TABLE):
            for operation in ("UPDATE", "DELETE"):
                trigger = f"{table}_immutable_{operation.lower()}"
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {trigger} "
                    f"BEFORE {operation} ON {table} BEGIN "
                    "SELECT RAISE(ABORT, 'frozen acquisition schedule is immutable'); END"
                )
        connection.commit()
    except sqlite3.DatabaseError as exc:
        if connection.in_transaction:
            connection.rollback()
        raise ScheduledCycleCoverageError(
            "cannot establish frozen schedule authority"
        ) from exc
    finally:
        connection.close()


def _schedule_policy_payload(
    *,
    source_id: str,
    adapter_id: str,
    sport: str,
    query_scope: object,
    schedule_policy_version: str,
    window_start: str,
    window_end: str,
    campaign_id: str,
    protocol_id: str,
    collector_config_sha256: str,
    cadence_microseconds: int,
) -> dict[str, object]:
    return {
        "schema": _SCHEDULE_SCHEMA,
        "schema_version": _SCHEDULE_VERSION,
        "source_id": source_id,
        "adapter_id": adapter_id,
        "sport": sport,
        "query_scope": query_scope,
        "schedule_policy_version": schedule_policy_version,
        "window_start": window_start,
        "window_end": window_end,
        "campaign_id": campaign_id,
        "protocol_id": protocol_id,
        "collector_config_sha256": collector_config_sha256,
        "cadence_microseconds": cadence_microseconds,
    }


def _slot_id(
    logical_key_sha256: str,
    slot_index: int,
    due_at: str,
    window_end: str,
) -> str:
    return _sha256(
        {
            "schema": "autosport.frozen_acquisition_schedule_slot",
            "schema_version": 1,
            "logical_key_sha256": logical_key_sha256,
            "slot_index": slot_index,
            "due_at": due_at,
            "window_end": window_end,
        }
    )


def _build_slots(
    *,
    logical_key_sha256: str,
    window_start: datetime,
    window_end: datetime,
    cadence_microseconds: int,
) -> tuple[FrozenScheduleSlot, ...]:
    duration_us = _timedelta_microseconds(window_end - window_start)
    slot_count = (duration_us + cadence_microseconds - 1) // cadence_microseconds
    if slot_count <= 0 or slot_count > _MAX_SLOTS:
        raise ScheduledCycleCoverageError(
            f"frozen schedule must contain 1..{_MAX_SLOTS} slots"
        )
    slots: list[FrozenScheduleSlot] = []
    cadence = timedelta(microseconds=cadence_microseconds)
    for index in range(slot_count):
        due = window_start + index * cadence
        end = min(due + cadence, window_end)
        due_text = _instant_text(due)
        end_text = _instant_text(end)
        slots.append(
            FrozenScheduleSlot(
                slot_index=index,
                slot_id=_slot_id(logical_key_sha256, index, due_text, end_text),
                due_at=due_text,
                window_end=end_text,
            )
        )
    return tuple(slots)


def _schedule_commitment_payload(
    *,
    policy_payload: dict[str, object],
    logical_key_sha256: str,
    schedule_id: str,
    frozen_at: str,
    slots: tuple[FrozenScheduleSlot, ...],
) -> dict[str, object]:
    return {
        **policy_payload,
        "logical_key_sha256": logical_key_sha256,
        "schedule_id": schedule_id,
        "frozen_at": frozen_at,
        "slots": [
            {
                "slot_index": slot.slot_index,
                "slot_id": slot.slot_id,
                "due_at": slot.due_at,
                "window_end": slot.window_end,
            }
            for slot in slots
        ],
    }


def freeze_acquisition_schedule(
    store: CollectorDeltaStore,
    *,
    source_id: str,
    adapter_id: str,
    sport: str,
    query_scope: object,
    schedule_policy_version: str,
    window_start: str,
    window_end: str,
    campaign_id: str,
    protocol_id: str,
    collector_config: CollectorServiceConfig,
    _clock: Callable[[], str] | None = None,
) -> FrozenAcquisitionSchedule:
    """Durably freeze deterministic collector cadence before the first due slot.

    Membership is generated from the canonical collector poll cadence, never from a
    caller-supplied slot list. Repeating the same logical policy re-resolves the
    existing immutable schedule even after the window has begun.
    """

    store = _require_store(store)
    source_id = _text(source_id, "source_id")
    adapter_id = _text(adapter_id, "adapter_id")
    sport = _text(sport, "sport")
    schedule_policy_version = _text(
        schedule_policy_version, "schedule_policy_version"
    )
    campaign_id = _text(campaign_id, "campaign_id")
    protocol_id = _text(protocol_id, "protocol_id")
    start_dt = _instant(window_start, "window_start")
    end_dt = _instant(window_end, "window_end")
    if end_dt <= start_dt:
        raise ScheduledCycleCoverageError("window_end must be after window_start")

    query_scope_json = _canonical_json(query_scope).decode("utf-8")
    query_scope = json.loads(query_scope_json)

    config_payload = _collector_config_payload(collector_config)
    config_sha = _sha256(
        {
            "schema": "autosport.collector_service_config",
            "schema_version": 1,
            "config": config_payload,
        }
    )
    cadence_us = int(config_payload["poll_interval_microseconds"])
    start_text = _instant_text(start_dt)
    end_text = _instant_text(end_dt)
    policy_payload = _schedule_policy_payload(
        source_id=source_id,
        adapter_id=adapter_id,
        sport=sport,
        query_scope=query_scope,
        schedule_policy_version=schedule_policy_version,
        window_start=start_text,
        window_end=end_text,
        campaign_id=campaign_id,
        protocol_id=protocol_id,
        collector_config_sha256=config_sha,
        cadence_microseconds=cadence_us,
    )
    logical_key_sha256 = _sha256(policy_payload)
    schedule_id = f"schedule-{logical_key_sha256[:32]}"

    _ensure_schedule_schema(store)
    connection = _connect(store)
    try:
        existing = connection.execute(
            f"SELECT schedule_id FROM {_SCHEDULES_TABLE} "
            "WHERE logical_key_sha256=?",
            (logical_key_sha256,),
        ).fetchone()
    finally:
        connection.close()
    if existing is not None:
        return load_frozen_acquisition_schedule(store, existing["schedule_id"])

    clock = _clock or (lambda: datetime.now(timezone.utc).isoformat())
    frozen_dt = _instant(clock(), "frozen_at")
    if frozen_dt > start_dt:
        raise ScheduledCycleCoverageError(
            "new acquisition schedule must be frozen no later than its first due slot"
        )
    frozen_text = _instant_text(frozen_dt)
    slots = _build_slots(
        logical_key_sha256=logical_key_sha256,
        window_start=start_dt,
        window_end=end_dt,
        cadence_microseconds=cadence_us,
    )
    commitment_payload = _schedule_commitment_payload(
        policy_payload=policy_payload,
        logical_key_sha256=logical_key_sha256,
        schedule_id=schedule_id,
        frozen_at=frozen_text,
        slots=slots,
    )
    commitment_sha256 = _sha256(commitment_payload)

    connection = _connect(store)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"INSERT INTO {_SCHEDULES_TABLE}("
            "schedule_id,logical_key_sha256,source_id,adapter_id,sport,"
            "query_scope_json,schedule_policy_version,window_start,window_end,"
            "campaign_id,protocol_id,collector_config_sha256,cadence_microseconds,"
            "frozen_at,slot_count,commitment_sha256"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                schedule_id,
                logical_key_sha256,
                source_id,
                adapter_id,
                sport,
                query_scope_json,
                schedule_policy_version,
                start_text,
                end_text,
                campaign_id,
                protocol_id,
                config_sha,
                cadence_us,
                frozen_text,
                len(slots),
                commitment_sha256,
            ),
        )
        connection.executemany(
            f"INSERT INTO {_SLOTS_TABLE}("
            "schedule_id,slot_index,slot_id,due_at,window_end"
            ") VALUES(?,?,?,?,?)",
            [
                (
                    schedule_id,
                    slot.slot_index,
                    slot.slot_id,
                    slot.due_at,
                    slot.window_end,
                )
                for slot in slots
            ],
        )
        connection.commit()
    except sqlite3.IntegrityError:
        if connection.in_transaction:
            connection.rollback()
        return load_frozen_acquisition_schedule(store, schedule_id)
    except sqlite3.DatabaseError as exc:
        if connection.in_transaction:
            connection.rollback()
        raise ScheduledCycleCoverageError(
            "cannot persist frozen acquisition schedule"
        ) from exc
    finally:
        connection.close()

    return load_frozen_acquisition_schedule(store, schedule_id)


def load_frozen_acquisition_schedule(
    store: CollectorDeltaStore,
    schedule_id: str,
) -> FrozenAcquisitionSchedule:
    """Re-resolve and authenticate one immutable product-owned schedule."""

    store = _require_store(store)
    schedule_id = _text(schedule_id, "schedule_id")
    _ensure_schedule_schema(store)
    connection = _connect(store)
    try:
        row = connection.execute(
            f"SELECT * FROM {_SCHEDULES_TABLE} WHERE schedule_id=?",
            (schedule_id,),
        ).fetchone()
        if row is None:
            raise ScheduledCycleCoverageError("frozen acquisition schedule is missing")
        slot_rows = connection.execute(
            f"SELECT slot_index,slot_id,due_at,window_end FROM {_SLOTS_TABLE} "
            "WHERE schedule_id=? ORDER BY slot_index",
            (schedule_id,),
        ).fetchall()
    finally:
        connection.close()

    try:
        query_scope = json.loads(row["query_scope_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ScheduledCycleCoverageError("frozen query scope JSON is malformed") from exc
    if _canonical_json(query_scope).decode("utf-8") != row["query_scope_json"]:
        raise ScheduledCycleCoverageError("frozen query scope is not canonical")

    start_dt = _instant(row["window_start"], "window_start")
    end_dt = _instant(row["window_end"], "window_end")
    frozen_dt = _instant(row["frozen_at"], "frozen_at")
    if end_dt <= start_dt or frozen_dt > start_dt:
        raise ScheduledCycleCoverageError("frozen schedule temporal authority is invalid")
    cadence_us = row["cadence_microseconds"]
    if isinstance(cadence_us, bool) or not isinstance(cadence_us, int) or cadence_us <= 0:
        raise ScheduledCycleCoverageError("frozen schedule cadence is invalid")

    policy_payload = _schedule_policy_payload(
        source_id=_text(row["source_id"], "source_id"),
        adapter_id=_text(row["adapter_id"], "adapter_id"),
        sport=_text(row["sport"], "sport"),
        query_scope=query_scope,
        schedule_policy_version=_text(
            row["schedule_policy_version"], "schedule_policy_version"
        ),
        window_start=_instant_text(start_dt),
        window_end=_instant_text(end_dt),
        campaign_id=_text(row["campaign_id"], "campaign_id"),
        protocol_id=_text(row["protocol_id"], "protocol_id"),
        collector_config_sha256=_text(
            row["collector_config_sha256"], "collector_config_sha256"
        ),
        cadence_microseconds=cadence_us,
    )
    logical_key = _sha256(policy_payload)
    if logical_key != row["logical_key_sha256"]:
        raise ScheduledCycleCoverageError("frozen schedule logical identity mismatch")
    if schedule_id != f"schedule-{logical_key[:32]}":
        raise ScheduledCycleCoverageError("frozen schedule_id is not canonical")

    expected_slots = _build_slots(
        logical_key_sha256=logical_key,
        window_start=start_dt,
        window_end=end_dt,
        cadence_microseconds=cadence_us,
    )
    observed_slots = tuple(
        FrozenScheduleSlot(
            slot_index=item["slot_index"],
            slot_id=item["slot_id"],
            due_at=item["due_at"],
            window_end=item["window_end"],
        )
        for item in slot_rows
    )
    if observed_slots != expected_slots or row["slot_count"] != len(expected_slots):
        raise ScheduledCycleCoverageError(
            "frozen schedule slot membership is incomplete or mutated"
        )

    commitment_payload = _schedule_commitment_payload(
        policy_payload=policy_payload,
        logical_key_sha256=logical_key,
        schedule_id=schedule_id,
        frozen_at=_instant_text(frozen_dt),
        slots=expected_slots,
    )
    commitment_sha256 = _sha256(commitment_payload)
    if commitment_sha256 != row["commitment_sha256"]:
        raise ScheduledCycleCoverageError("frozen schedule commitment digest mismatch")

    return FrozenAcquisitionSchedule._issue(
        {
            "schema_version": _SCHEDULE_VERSION,
            "schedule_id": schedule_id,
            "logical_key_sha256": logical_key,
            "source_id": row["source_id"],
            "adapter_id": row["adapter_id"],
            "sport": row["sport"],
            "query_scope": query_scope,
            "schedule_policy_version": row["schedule_policy_version"],
            "window_start": _instant_text(start_dt),
            "window_end": _instant_text(end_dt),
            "campaign_id": row["campaign_id"],
            "protocol_id": row["protocol_id"],
            "collector_config_sha256": row["collector_config_sha256"],
            "cadence_microseconds": cadence_us,
            "frozen_at": _instant_text(frozen_dt),
            "slots": expected_slots,
            "commitment_sha256": commitment_sha256,
        }
    )


def _candidate_cycle_sequences(
    store: CollectorDeltaStore,
    *,
    source_id: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[int, ...]:
    connection = _connect(store)
    try:
        try:
            rows = connection.execute(
                "SELECT cycle_seq, attempted_at FROM collector_cycle_starts_v1 "
                "WHERE source_id=? ORDER BY cycle_seq",
                (source_id,),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ScheduledCycleCoverageError(
                "canonical collector cycle START authority is unavailable"
            ) from exc
    finally:
        connection.close()

    selected: list[int] = []
    for row in rows:
        attempted = _instant(row["attempted_at"], "cycle attempted_at")
        if window_start <= attempted < window_end:
            sequence = row["cycle_seq"]
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
                raise ScheduledCycleCoverageError(
                    "canonical collector cycle sequence is malformed"
                )
            selected.append(sequence)
    return tuple(selected)


def resolve_scheduled_cycle_coverage(
    store: CollectorDeltaStore,
    *,
    schedule_id: str,
) -> ScheduledCycleCoverage:
    """Resolve each frozen due slot only from canonical #1180 START evidence.

    A START is mapped by its authenticated attempted_at into the deterministic
    half-open cadence slot [due_at, window_end). No caller-supplied cycle map,
    expected count, cancellation flag, or reason string participates in authority.
    """

    store = _require_store(store)
    schedule = load_frozen_acquisition_schedule(store, schedule_id)
    window_start = _instant(schedule.window_start, "window_start")
    window_end = _instant(schedule.window_end, "window_end")
    candidate_sequences = _candidate_cycle_sequences(
        store,
        source_id=schedule.source_id,
        window_start=window_start,
        window_end=window_end,
    )

    evidence: tuple[dict[str, object], ...]
    if not candidate_sequences:
        evidence = ()
    else:
        evidence = store.collector_cycle_evidence(
            source_id=schedule.source_id,
            start_cycle_seq=min(candidate_sequences),
            end_cycle_seq=max(candidate_sequences),
        )
        selected = set(candidate_sequences)
        evidence = tuple(
            item for item in evidence if item.get("cycle_seq") in selected
        )
        if len(evidence) != len(candidate_sequences):
            raise ScheduledCycleCoverageError(
                "canonical collector cycle START evidence is incomplete"
            )

    cycles_by_slot: list[list[str]] = [[] for _ in schedule.slots]
    pending_cycle_count = 0
    for item in evidence:
        if item.get("source_id") != schedule.source_id:
            raise ScheduledCycleCoverageError(
                "collector cycle evidence crosses frozen source authority"
            )
        sequence = item.get("cycle_seq")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise ScheduledCycleCoverageError(
                "collector cycle evidence has malformed sequence"
            )
        attempted = _instant(item.get("attempted_at"), "cycle attempted_at")
        if not (window_start <= attempted < window_end):
            raise ScheduledCycleCoverageError(
                "collector cycle candidate lies outside frozen schedule window"
            )
        offset_us = _timedelta_microseconds(attempted - window_start)
        slot_index = offset_us // schedule.cadence_microseconds
        if slot_index < 0 or slot_index >= len(schedule.slots):
            raise ScheduledCycleCoverageError(
                "collector cycle cannot be assigned to frozen cadence"
            )
        slot = schedule.slots[slot_index]
        if not (
            _instant(slot.due_at, "slot due_at")
            <= attempted
            < _instant(slot.window_end, "slot window_end")
        ):
            raise ScheduledCycleCoverageError(
                "collector cycle violates frozen slot boundaries"
            )
        cycle_id = f"{schedule.source_id}:{sequence}"
        cycles_by_slot[slot_index].append(cycle_id)
        if item.get("terminal") is None:
            pending_cycle_count += 1

    slot_cycle_ids = tuple(
        (slot.slot_id, tuple(cycles_by_slot[slot.slot_index]))
        for slot in schedule.slots
    )
    missing_slot_ids = tuple(
        slot_id for slot_id, cycle_ids in slot_cycle_ids if len(cycle_ids) == 0
    )
    duplicate_slot_ids = tuple(
        slot_id for slot_id, cycle_ids in slot_cycle_ids if len(cycle_ids) > 1
    )
    started_slot_count = sum(1 for _, cycle_ids in slot_cycle_ids if cycle_ids)
    started_cycle_count = sum(len(cycle_ids) for _, cycle_ids in slot_cycle_ids)
    complete = not missing_slot_ids and not duplicate_slot_ids

    authority_payload: dict[str, object] = {
        "schema": _COVERAGE_SCHEMA,
        "schema_version": _COVERAGE_VERSION,
        "schedule_id": schedule.schedule_id,
        "schedule_commitment_sha256": schedule.commitment_sha256,
        "source_id": schedule.source_id,
        "slot_count": len(schedule.slots),
        "started_slot_count": started_slot_count,
        "started_cycle_count": started_cycle_count,
        "pending_cycle_count": pending_cycle_count,
        "missing_slot_ids": list(missing_slot_ids),
        "duplicate_slot_ids": list(duplicate_slot_ids),
        "slot_cycle_ids": [
            {"slot_id": slot_id, "cycle_ids": list(cycle_ids)}
            for slot_id, cycle_ids in slot_cycle_ids
        ],
        "schedule_coverage_complete": complete,
        "external_provider_universe_complete": False,
        "promotion_ready": False,
    }
    coverage_commitment_sha256 = _sha256(authority_payload)
    return ScheduledCycleCoverage._issue(
        {
            "schema_version": _COVERAGE_VERSION,
            "schedule_id": schedule.schedule_id,
            "schedule_commitment_sha256": schedule.commitment_sha256,
            "source_id": schedule.source_id,
            "slot_count": len(schedule.slots),
            "started_slot_count": started_slot_count,
            "started_cycle_count": started_cycle_count,
            "pending_cycle_count": pending_cycle_count,
            "missing_slot_ids": missing_slot_ids,
            "duplicate_slot_ids": duplicate_slot_ids,
            "slot_cycle_ids": slot_cycle_ids,
            "coverage_commitment_sha256": coverage_commitment_sha256,
            "schedule_coverage_complete": complete,
            "external_provider_universe_complete": False,
            "promotion_ready": False,
        }
    )
