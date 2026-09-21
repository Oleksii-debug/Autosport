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


_SCHEDULES_TABLE = "collector_frozen_schedules_v1"
_SLOTS_TABLE = "collector_frozen_schedule_slots_v1"
_MAX_SLOTS = 1_000_000


def _json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ScheduledCycleCoverageError("evidence is not canonical JSON") from exc


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


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


def _duration_us(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScheduledCycleCoverageError(f"{name} must be a finite positive number")
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise ScheduledCycleCoverageError(f"{name} must be a finite positive number")
    try:
        micros = Decimal(str(value)) * Decimal(1_000_000)
    except InvalidOperation as exc:
        raise ScheduledCycleCoverageError(f"{name} is not canonical") from exc
    integral = micros.to_integral_value()
    if micros != integral or integral <= 0:
        raise ScheduledCycleCoverageError(
            f"{name} must resolve exactly to positive whole microseconds"
        )
    return int(integral)


def _delta_us(value: timedelta) -> int:
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


def _config_payload(config: CollectorServiceConfig) -> dict[str, object]:
    if not isinstance(config, CollectorServiceConfig):
        raise TypeError("collector_config must be CollectorServiceConfig")
    return {
        "max_items": config.max_items,
        "poll_interval_microseconds": _duration_us(
            config.poll_interval_seconds, "poll_interval_seconds"
        ),
        "retry_attempts": config.retry_attempts,
        "initial_backoff_microseconds": _duration_us(
            config.initial_backoff_seconds, "initial_backoff_seconds"
        ),
        "max_backoff_microseconds": _duration_us(
            config.max_backoff_seconds, "max_backoff_seconds"
        ),
        "jitter_fraction": str(Decimal(str(config.jitter_fraction))),
        "max_store_bytes": config.max_store_bytes,
    }


def _store(store: CollectorDeltaStore) -> CollectorDeltaStore:
    if not isinstance(store, CollectorDeltaStore):
        raise TypeError("store must be the canonical CollectorDeltaStore")
    return store


def _connect(store: CollectorDeltaStore) -> sqlite3.Connection:
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _ensure_schema(store: CollectorDeltaStore) -> None:
    connection = _connect(store)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {_SCHEDULES_TABLE} ("
            "schedule_id TEXT PRIMARY KEY NOT NULL,"
            "logical_key_sha256 TEXT UNIQUE NOT NULL,"
            "commitment_sha256 TEXT NOT NULL,"
            "payload_json TEXT NOT NULL)"
        )
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {_SLOTS_TABLE} ("
            "schedule_id TEXT NOT NULL,"
            "slot_index INTEGER NOT NULL CHECK(slot_index >= 0),"
            "slot_id TEXT UNIQUE NOT NULL,"
            "due_at TEXT NOT NULL,"
            "window_end TEXT NOT NULL,"
            "PRIMARY KEY(schedule_id,slot_index),"
            f"FOREIGN KEY(schedule_id) REFERENCES {_SCHEDULES_TABLE}(schedule_id))"
        )
        for table in (_SCHEDULES_TABLE, _SLOTS_TABLE):
            for operation in ("UPDATE", "DELETE"):
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()} "
                    f"BEFORE {operation} ON {table} BEGIN "
                    "SELECT RAISE(ABORT, 'frozen acquisition schedule is immutable'); END"
                )
        connection.commit()
    except sqlite3.DatabaseError as exc:
        if connection.in_transaction:
            connection.rollback()
        raise ScheduledCycleCoverageError(
            "cannot establish frozen acquisition schedule authority"
        ) from exc
    finally:
        connection.close()


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
    collector_run_id: str
    stream_epoch: str
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
        raise TypeError("FrozenAcquisitionSchedule is product-issued")

    @classmethod
    def _issue(cls, values: dict[str, object]) -> "FrozenAcquisitionSchedule":
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance


@dataclass(frozen=True, slots=True, init=False)
class ScheduledCycleCoverage:
    schema_version: int
    schedule_id: str
    source_id: str
    collector_run_id: str
    stream_epoch: str
    slot_count: int
    started_slot_count: int
    started_cycle_count: int
    pending_cycle_count: int
    missing_slot_ids: tuple[str, ...]
    duplicate_slot_ids: tuple[str, ...]
    slot_cycle_ids: tuple[tuple[str, tuple[str, ...]], ...]
    schedule_commitment_sha256: str
    coverage_commitment_sha256: str
    schedule_coverage_complete: bool
    external_provider_universe_complete: bool
    promotion_ready: bool

    def __new__(cls, *args: object, **kwargs: object) -> "ScheduledCycleCoverage":
        raise TypeError("ScheduledCycleCoverage is product-issued")

    @classmethod
    def _issue(cls, values: dict[str, object]) -> "ScheduledCycleCoverage":
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        return instance


def _policy(
    *,
    source_id: str,
    collector_run_id: str,
    stream_epoch: str,
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
        "schema": "autosport.frozen_acquisition_schedule_policy",
        "schema_version": 1,
        "source_id": source_id,
        "collector_run_id": collector_run_id,
        "stream_epoch": stream_epoch,
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


def _build_slots(
    logical_key_sha256: str,
    start: datetime,
    end: datetime,
    cadence_us: int,
) -> tuple[FrozenScheduleSlot, ...]:
    total_us = _delta_us(end - start)
    count = (total_us + cadence_us - 1) // cadence_us
    if count <= 0 or count > _MAX_SLOTS:
        raise ScheduledCycleCoverageError(
            f"frozen schedule must contain 1..{_MAX_SLOTS} due slots"
        )
    cadence = timedelta(microseconds=cadence_us)
    slots: list[FrozenScheduleSlot] = []
    for index in range(count):
        due = start + index * cadence
        slot_end = min(due + cadence, end)
        due_text = _instant_text(due)
        end_text = _instant_text(slot_end)
        slot_id = _sha(
            {
                "schema": "autosport.frozen_acquisition_schedule_slot",
                "schema_version": 1,
                "logical_key_sha256": logical_key_sha256,
                "slot_index": index,
                "due_at": due_text,
                "window_end": end_text,
            }
        )
        slots.append(FrozenScheduleSlot(index, slot_id, due_text, end_text))
    return tuple(slots)


def _commitment_payload(
    policy: dict[str, object],
    schedule_id: str,
    logical_key_sha256: str,
    frozen_at: str,
    slots: tuple[FrozenScheduleSlot, ...],
) -> dict[str, object]:
    return {
        "schema": "autosport.frozen_acquisition_schedule",
        "schema_version": 1,
        "schedule_id": schedule_id,
        "logical_key_sha256": logical_key_sha256,
        "policy": policy,
        "frozen_at": frozen_at,
        "slots": [
            {
                "slot_index": item.slot_index,
                "slot_id": item.slot_id,
                "due_at": item.due_at,
                "window_end": item.window_end,
            }
            for item in slots
        ],
    }


def freeze_acquisition_schedule(
    store: CollectorDeltaStore,
    *,
    source_id: str,
    collector_run_id: str,
    stream_epoch: str,
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
    """Freeze cadence membership before outcomes; never accept a caller slot list."""

    store = _store(store)
    source_id = _text(source_id, "source_id")
    collector_run_id = _text(collector_run_id, "collector_run_id")
    stream_epoch = _text(stream_epoch, "stream_epoch")
    adapter_id = _text(adapter_id, "adapter_id")
    sport = _text(sport, "sport")
    schedule_policy_version = _text(schedule_policy_version, "schedule_policy_version")
    campaign_id = _text(campaign_id, "campaign_id")
    protocol_id = _text(protocol_id, "protocol_id")
    start = _instant(window_start, "window_start")
    end = _instant(window_end, "window_end")
    if end <= start:
        raise ScheduledCycleCoverageError("window_end must be after window_start")
    canonical_scope = json.loads(_json(query_scope))

    config = _config_payload(collector_config)
    config_sha = _sha(
        {"schema": "autosport.collector_service_config", "version": 1, "config": config}
    )
    cadence_us = int(config["poll_interval_microseconds"])
    policy = _policy(
        source_id=source_id,
        collector_run_id=collector_run_id,
        stream_epoch=stream_epoch,
        adapter_id=adapter_id,
        sport=sport,
        query_scope=canonical_scope,
        schedule_policy_version=schedule_policy_version,
        window_start=_instant_text(start),
        window_end=_instant_text(end),
        campaign_id=campaign_id,
        protocol_id=protocol_id,
        collector_config_sha256=config_sha,
        cadence_microseconds=cadence_us,
    )
    logical_key = _sha(policy)
    schedule_id = f"schedule-{logical_key[:32]}"

    _ensure_schema(store)
    connection = _connect(store)
    try:
        existing = connection.execute(
            f"SELECT schedule_id FROM {_SCHEDULES_TABLE} WHERE logical_key_sha256=?",
            (logical_key,),
        ).fetchone()
    finally:
        connection.close()
    if existing is not None:
        return load_frozen_acquisition_schedule(store, existing["schedule_id"])

    now = _instant(
        (_clock or (lambda: datetime.now(timezone.utc).isoformat()))(),
        "frozen_at",
    )
    if now > start:
        raise ScheduledCycleCoverageError(
            "new acquisition schedule must be frozen no later than its first due slot"
        )
    slots = _build_slots(logical_key, start, end, cadence_us)
    payload = _commitment_payload(
        policy, schedule_id, logical_key, _instant_text(now), slots
    )
    payload_json = _json(payload)
    commitment = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    connection = _connect(store)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"INSERT INTO {_SCHEDULES_TABLE}"
            "(schedule_id,logical_key_sha256,commitment_sha256,payload_json)"
            " VALUES(?,?,?,?)",
            (schedule_id, logical_key, commitment, payload_json),
        )
        connection.executemany(
            f"INSERT INTO {_SLOTS_TABLE}"
            "(schedule_id,slot_index,slot_id,due_at,window_end) VALUES(?,?,?,?,?)",
            [
                (
                    schedule_id,
                    item.slot_index,
                    item.slot_id,
                    item.due_at,
                    item.window_end,
                )
                for item in slots
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
    """Re-resolve exact durable schedule authority and verify every slot."""

    store = _store(store)
    schedule_id = _text(schedule_id, "schedule_id")
    _ensure_schema(store)
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
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ScheduledCycleCoverageError("frozen schedule payload is malformed") from exc
    if _json(payload) != row["payload_json"]:
        raise ScheduledCycleCoverageError("frozen schedule payload is not canonical")
    if _sha(payload) != row["commitment_sha256"]:
        raise ScheduledCycleCoverageError("frozen schedule commitment digest mismatch")
    if set(payload) != {
        "schema",
        "schema_version",
        "schedule_id",
        "logical_key_sha256",
        "policy",
        "frozen_at",
        "slots",
    }:
        raise ScheduledCycleCoverageError("frozen schedule payload shape is invalid")
    if (
        payload["schema"] != "autosport.frozen_acquisition_schedule"
        or payload["schema_version"] != 1
        or payload["schedule_id"] != schedule_id
    ):
        raise ScheduledCycleCoverageError("frozen schedule identity/schema mismatch")

    policy = payload["policy"]
    if type(policy) is not dict:
        raise ScheduledCycleCoverageError("frozen schedule policy is malformed")
    logical_key = _sha(policy)
    if (
        logical_key != row["logical_key_sha256"]
        or logical_key != payload["logical_key_sha256"]
        or schedule_id != f"schedule-{logical_key[:32]}"
    ):
        raise ScheduledCycleCoverageError("frozen schedule logical identity mismatch")

    required = {
        "schema",
        "schema_version",
        "source_id",
        "collector_run_id",
        "stream_epoch",
        "adapter_id",
        "sport",
        "query_scope",
        "schedule_policy_version",
        "window_start",
        "window_end",
        "campaign_id",
        "protocol_id",
        "collector_config_sha256",
        "cadence_microseconds",
    }
    if set(policy) != required:
        raise ScheduledCycleCoverageError("frozen schedule policy shape is invalid")
    for name in (
        "source_id",
        "collector_run_id",
        "stream_epoch",
        "adapter_id",
        "sport",
        "schedule_policy_version",
        "campaign_id",
        "protocol_id",
        "collector_config_sha256",
    ):
        _text(policy[name], name)
    start = _instant(policy["window_start"], "window_start")
    end = _instant(policy["window_end"], "window_end")
    frozen_at = _instant(payload["frozen_at"], "frozen_at")
    if end <= start or frozen_at > start:
        raise ScheduledCycleCoverageError("frozen schedule temporal authority is invalid")
    cadence_us = policy["cadence_microseconds"]
    if type(cadence_us) is not int or cadence_us <= 0:
        raise ScheduledCycleCoverageError("frozen cadence is invalid")

    expected = _build_slots(logical_key, start, end, cadence_us)
    observed = tuple(
        FrozenScheduleSlot(
            item["slot_index"], item["slot_id"], item["due_at"], item["window_end"]
        )
        for item in slot_rows
    )
    payload_slots = tuple(
        FrozenScheduleSlot(
            item["slot_index"], item["slot_id"], item["due_at"], item["window_end"]
        )
        for item in payload["slots"]
    )
    if observed != expected or payload_slots != expected:
        raise ScheduledCycleCoverageError(
            "frozen schedule slot membership is incomplete or mutated"
        )

    return FrozenAcquisitionSchedule._issue(
        {
            "schema_version": 1,
            "schedule_id": schedule_id,
            "logical_key_sha256": logical_key,
            "source_id": policy["source_id"],
            "collector_run_id": policy["collector_run_id"],
            "stream_epoch": policy["stream_epoch"],
            "adapter_id": policy["adapter_id"],
            "sport": policy["sport"],
            "query_scope": policy["query_scope"],
            "schedule_policy_version": policy["schedule_policy_version"],
            "window_start": _instant_text(start),
            "window_end": _instant_text(end),
            "campaign_id": policy["campaign_id"],
            "protocol_id": policy["protocol_id"],
            "collector_config_sha256": policy["collector_config_sha256"],
            "cadence_microseconds": cadence_us,
            "frozen_at": _instant_text(frozen_at),
            "slots": expected,
            "commitment_sha256": row["commitment_sha256"],
        }
    )


def _candidate_sequences(
    store: CollectorDeltaStore,
    schedule: FrozenAcquisitionSchedule,
) -> tuple[int, ...]:
    start = _instant(schedule.window_start, "window_start")
    end = _instant(schedule.window_end, "window_end")
    connection = _connect(store)
    try:
        try:
            rows = connection.execute(
                "SELECT cycle_seq,run_id,stream_epoch,attempted_at "
                "FROM collector_cycle_starts_v1 WHERE source_id=? ORDER BY cycle_seq",
                (schedule.source_id,),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ScheduledCycleCoverageError(
                "canonical collector cycle START authority is unavailable"
            ) from exc
    finally:
        connection.close()

    selected: list[int] = []
    for row in rows:
        if (
            row["run_id"] != schedule.collector_run_id
            or row["stream_epoch"] != schedule.stream_epoch
        ):
            continue
        attempted = _instant(row["attempted_at"], "cycle attempted_at")
        if start <= attempted < end:
            sequence = row["cycle_seq"]
            if type(sequence) is not int or sequence <= 0:
                raise ScheduledCycleCoverageError("collector cycle sequence is malformed")
            selected.append(sequence)
    return tuple(selected)


def resolve_scheduled_cycle_coverage(
    store: CollectorDeltaStore,
    *,
    schedule_id: str,
) -> ScheduledCycleCoverage:
    """Map each due slot only to exact canonical #1180 START evidence."""

    store = _store(store)
    schedule = load_frozen_acquisition_schedule(store, schedule_id)
    candidates = _candidate_sequences(store, schedule)
    if not candidates:
        evidence: tuple[dict[str, object], ...] = ()
    else:
        all_evidence = store.collector_cycle_evidence(
            source_id=schedule.source_id,
            start_cycle_seq=min(candidates),
            end_cycle_seq=max(candidates),
        )
        selected = set(candidates)
        evidence = tuple(
            item for item in all_evidence if item.get("cycle_seq") in selected
        )
        if len(evidence) != len(candidates):
            raise ScheduledCycleCoverageError(
                "canonical collector cycle START evidence is incomplete"
            )

    start = _instant(schedule.window_start, "window_start")
    end = _instant(schedule.window_end, "window_end")
    by_slot: list[list[str]] = [[] for _ in schedule.slots]
    pending = 0
    for item in evidence:
        if (
            item.get("source_id") != schedule.source_id
            or item.get("run_id") != schedule.collector_run_id
            or item.get("stream_epoch") != schedule.stream_epoch
        ):
            raise ScheduledCycleCoverageError(
                "collector cycle crosses frozen source/run/epoch authority"
            )
        sequence = item.get("cycle_seq")
        if type(sequence) is not int or sequence <= 0:
            raise ScheduledCycleCoverageError("collector cycle sequence is malformed")
        attempted = _instant(item.get("attempted_at"), "cycle attempted_at")
        if not (start <= attempted < end):
            raise ScheduledCycleCoverageError(
                "collector cycle lies outside frozen schedule window"
            )
        slot_index = _delta_us(attempted - start) // schedule.cadence_microseconds
        if slot_index >= len(schedule.slots):
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
        by_slot[slot_index].append(f"{schedule.source_id}:{sequence}")
        if item.get("terminal") is None:
            pending += 1

    mappings = tuple(
        (slot.slot_id, tuple(by_slot[slot.slot_index])) for slot in schedule.slots
    )
    missing = tuple(slot_id for slot_id, cycle_ids in mappings if not cycle_ids)
    duplicate = tuple(
        slot_id for slot_id, cycle_ids in mappings if len(cycle_ids) > 1
    )
    started_slots = sum(bool(cycle_ids) for _, cycle_ids in mappings)
    started_cycles = sum(len(cycle_ids) for _, cycle_ids in mappings)
    complete = not missing and not duplicate
    authority = {
        "schema": "autosport.scheduled_cycle_coverage",
        "schema_version": 1,
        "schedule_id": schedule.schedule_id,
        "schedule_commitment_sha256": schedule.commitment_sha256,
        "source_id": schedule.source_id,
        "collector_run_id": schedule.collector_run_id,
        "stream_epoch": schedule.stream_epoch,
        "slot_count": len(schedule.slots),
        "started_slot_count": started_slots,
        "started_cycle_count": started_cycles,
        "pending_cycle_count": pending,
        "missing_slot_ids": list(missing),
        "duplicate_slot_ids": list(duplicate),
        "slot_cycle_ids": [
            {"slot_id": slot_id, "cycle_ids": list(cycle_ids)}
            for slot_id, cycle_ids in mappings
        ],
        "schedule_coverage_complete": complete,
        "external_provider_universe_complete": False,
        "promotion_ready": False,
    }
    return ScheduledCycleCoverage._issue(
        {
            "schema_version": 1,
            "schedule_id": schedule.schedule_id,
            "source_id": schedule.source_id,
            "collector_run_id": schedule.collector_run_id,
            "stream_epoch": schedule.stream_epoch,
            "slot_count": len(schedule.slots),
            "started_slot_count": started_slots,
            "started_cycle_count": started_cycles,
            "pending_cycle_count": pending,
            "missing_slot_ids": missing,
            "duplicate_slot_ids": duplicate,
            "slot_cycle_ids": mappings,
            "schedule_commitment_sha256": schedule.commitment_sha256,
            "coverage_commitment_sha256": _sha(authority),
            "schedule_coverage_complete": complete,
            "external_provider_universe_complete": False,
            "promotion_ready": False,
        }
    )
