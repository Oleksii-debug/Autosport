from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from .causal_collector_legacy import (
    CollectorDelta,
    CursorRegressionError,
    DeltaConflictError,
    StreamCheckpoint,
    _instant,
    _text,
)
from .collector_sqlite_store import (
    CollectorDeltaStore as _SQLiteCollectorDeltaStore,
    _canonical_delta_json,
    _payload_digest,
)


_SQLITE_HEADER = b"SQLite format 3\x00"
_PROJECTION_INTEGRITY_META_KEY = "indexed_projection_integrity_v1"
_PROJECTION_IMMUTABILITY_TRIGGER = "collector_deltas_projection_immutable_v1"
_INDEXED_PROJECTION_FIELDS = (
    "delta_id",
    "source_id",
    "stream_epoch",
    "cursor_position",
    "revision_number",
    "desktop_available_at",
    "collector_committed_at",
)
_DELTA_SELECT_COLUMNS = ", ".join(
    ("commit_seq", *_INDEXED_PROJECTION_FIELDS, "payload_sha256", "payload_json")
)

_CYCLE_TERMINAL_STATUSES = frozenset(
    {"SUCCESS", "PROVIDER_UNAVAILABLE", "LOCAL_FAILURE", "STOP_REQUESTED"}
)
_CYCLE_START_IMMUTABLE_UPDATE_TRIGGER = "collector_cycle_starts_immutable_update_v1"
_CYCLE_START_IMMUTABLE_DELETE_TRIGGER = "collector_cycle_starts_immutable_delete_v1"
_CYCLE_TERMINAL_IMMUTABLE_UPDATE_TRIGGER = "collector_cycle_terminals_immutable_update_v1"
_CYCLE_TERMINAL_IMMUTABLE_DELETE_TRIGGER = "collector_cycle_terminals_immutable_delete_v1"
_SCHEDULE_POLICY = "fixed_interval_v1"
_SCHEDULE_IMMUTABLE_UPDATE_TRIGGER = "collector_schedules_immutable_update_v1"
_SCHEDULE_IMMUTABLE_DELETE_TRIGGER = "collector_schedules_immutable_delete_v1"
_SCHEDULE_SLOT_IMMUTABLE_UPDATE_TRIGGER = "collector_schedule_slots_immutable_update_v1"
_SCHEDULE_SLOT_IMMUTABLE_DELETE_TRIGGER = "collector_schedule_slots_immutable_delete_v1"
_MAX_SCHEDULE_EVIDENCE_SLOTS = 1_000_000


class CollectorDeltaStore(_SQLiteCollectorDeltaStore):
    """Canonical indexed SQLite store with bounded product-compatible durability."""

    @staticmethod
    def _path_file_identity(path: Path) -> tuple[int, int]:
        """Return one cross-platform identity for the file currently at path."""

        try:
            result = os.stat(path, follow_symlinks=True)
        except OSError as exc:
            raise ValueError(
                "canonical collector store file identity is unavailable"
            ) from exc
        device = result.st_dev
        inode = result.st_ino
        if (
            type(device) is not int
            or device < 0
            or type(inode) is not int
            or inode <= 0
        ):
            raise ValueError(
                "canonical collector store file identity is unavailable"
            )
        return device, inode

    def _connect(self) -> sqlite3.Connection:
        """Open only the same live SQLite file object captured at initialization.

        Construction-time calls deliberately run before _canonical_file_identity_v1
        exists. Once initialization has succeeded, every later open checks the path
        both before and after sqlite3.connect(). The second check closes the ordinary
        stat -> replace -> connect race: a same-path replacement is rejected before
        its connection can carry authoritative reads or writes.
        """

        expected = vars(self).get("_canonical_file_identity_v1")
        if expected is None:
            return super()._connect()

        if self._path_file_identity(self.path) != expected:
            raise ValueError("canonical collector store file identity changed")

        connection = super()._connect()
        try:
            if self._path_file_identity(self.path) != expected:
                raise ValueError("canonical collector store file identity changed")
            return connection
        except BaseException:
            connection.close()
            raise

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                with self.path.open("rb") as handle:
                    prefix = handle.read(len(_SQLITE_HEADER))
            except OSError as exc:
                raise ValueError("invalid causal collector store") from exc
            if prefix == _SQLITE_HEADER:
                self._verify_sqlite_schema()
            else:
                self._migrate_legacy_json()
        else:
            self._initialize_sqlite(self.path, wal=False)
        self._activate_wal()
        self._verify_sqlite_schema()
        self._ensure_projection_integrity_guard()
        self._canonical_file_identity_v1 = self._path_file_identity(self.path)

    def _migrate_legacy_json(self) -> None:
        """Delegate migration to the canonical stale-winner-fenced switch.

        The base store deliberately builds a candidate before taking its
        crash-releasing WorkspaceEconomicLock, then re-reads the canonical path
        inside that lock immediately before the authority switch. Keeping a second
        outer process lock here would serialize candidate construction too and
        prevent the deterministic delayed-loser race that the switch must survive.
        """
        super()._migrate_legacy_json()

    def _activate_wal(self) -> None:
        """Keep rollback-journal durability so the existing byte budget stays exact.

        The headless collector's production backpressure authority measures the
        canonical store path itself. A steady-state WAL would move committed bytes
        into sidecars and could therefore admit data beyond ``max_store_bytes``.
        Indexed SQLite removes the quadratic rewrite without weakening that existing
        safety boundary, so normal operation deliberately uses DELETE + FULL.
        """
        connection = self._connect()
        try:
            selected = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(selected).lower() != "delete":
                raise ValueError(
                    "collector SQLite rollback journal mode could not be established"
                )
            connection.execute("PRAGMA synchronous=FULL")
        except sqlite3.DatabaseError as exc:
            raise ValueError("invalid causal collector store") from exc
        finally:
            connection.close()

    def _ensure_projection_integrity_guard(self) -> None:
        """One-time reconcile indexed projections, then make them SQL-immutable.

        Existing stores created by an earlier branch head receive one bounded upgrade
        scan. Once the marker and trigger are committed, normal reopen stays O(1) in
        retained history while all future SQL projection updates fail before they can
        create a second routing truth beside the digest-authenticated payload.
        """

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS collector_cycle_starts_v1 ("
                "source_id TEXT NOT NULL,"
                "cycle_seq INTEGER NOT NULL CHECK(cycle_seq > 0),"
                "run_id TEXT NOT NULL,"
                "stream_epoch TEXT NOT NULL,"
                "attempted_at TEXT NOT NULL,"
                "PRIMARY KEY(source_id, cycle_seq))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS collector_cycle_terminals_v1 ("
                "source_id TEXT NOT NULL,"
                "cycle_seq INTEGER NOT NULL CHECK(cycle_seq > 0),"
                "payload_sha256 TEXT NOT NULL,"
                "payload_json TEXT NOT NULL,"
                "PRIMARY KEY(source_id, cycle_seq),"
                "FOREIGN KEY(source_id, cycle_seq) "
                "REFERENCES collector_cycle_starts_v1(source_id, cycle_seq))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS collector_schedules_v1 ("
                "source_id TEXT NOT NULL,"
                "run_id TEXT NOT NULL,"
                "schedule_id TEXT NOT NULL UNIQUE,"
                "policy TEXT NOT NULL,"
                "stream_epoch TEXT NOT NULL,"
                "anchor_at TEXT NOT NULL,"
                "interval_seconds TEXT NOT NULL,"
                "max_items INTEGER,"
                "evaluation_start_slot_ordinal INTEGER,"
                "evaluation_end_slot_ordinal INTEGER,"
                "PRIMARY KEY(source_id, run_id))"
            )
            schedule_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(collector_schedules_v1)"
                ).fetchall()
            }
            if "stream_epoch" not in schedule_columns:
                # The earlier prospective-schedule prototype did not freeze the
                # acquisition epoch. Preserve that historical uncertainty as NULL:
                # callers must not launder a current source epoch into old evidence.
                connection.execute(
                    "ALTER TABLE collector_schedules_v1 "
                    "ADD COLUMN stream_epoch TEXT"
                )
            if "max_items" not in schedule_columns:
                # Historical schedules did not freeze the provider-facing page
                # bound. Preserve that uncertainty as NULL rather than inferring
                # request scope after outcomes already exist.
                connection.execute(
                    "ALTER TABLE collector_schedules_v1 "
                    "ADD COLUMN max_items INTEGER"
                )
            for column_name in (
                "evaluation_start_slot_ordinal",
                "evaluation_end_slot_ordinal",
            ):
                if column_name not in schedule_columns:
                    # Historical schedules remain explicitly unbounded for scientific
                    # resolution. The immutable row cannot be upgraded post-outcome.
                    connection.execute(
                        "ALTER TABLE collector_schedules_v1 "
                        f"ADD COLUMN {column_name} INTEGER"
                    )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS collector_schedule_slots_v1 ("
                "source_id TEXT NOT NULL,"
                "run_id TEXT NOT NULL,"
                "slot_ordinal INTEGER NOT NULL CHECK(slot_ordinal >= 0),"
                "due_at TEXT NOT NULL,"
                "cycle_seq INTEGER NOT NULL CHECK(cycle_seq > 0),"
                "PRIMARY KEY(source_id, run_id, slot_ordinal),"
                "UNIQUE(source_id, cycle_seq),"
                "FOREIGN KEY(source_id, run_id) "
                "REFERENCES collector_schedules_v1(source_id, run_id),"
                "FOREIGN KEY(source_id, cycle_seq) "
                "REFERENCES collector_cycle_starts_v1(source_id, cycle_seq))"
            )
            for trigger_name, table_name, timing in (
                (_SCHEDULE_IMMUTABLE_UPDATE_TRIGGER, "collector_schedules_v1", "UPDATE"),
                (_SCHEDULE_IMMUTABLE_DELETE_TRIGGER, "collector_schedules_v1", "DELETE"),
                (
                    _SCHEDULE_SLOT_IMMUTABLE_UPDATE_TRIGGER,
                    "collector_schedule_slots_v1",
                    "UPDATE",
                ),
                (
                    _SCHEDULE_SLOT_IMMUTABLE_DELETE_TRIGGER,
                    "collector_schedule_slots_v1",
                    "DELETE",
                ),
            ):
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {trigger_name} "
                    f"BEFORE {timing} ON {table_name} BEGIN "
                    "SELECT RAISE(ABORT, 'collector schedule evidence is immutable'); END"
                )
            for trigger_name, timing in (
                (_CYCLE_START_IMMUTABLE_UPDATE_TRIGGER, "UPDATE"),
                (_CYCLE_START_IMMUTABLE_DELETE_TRIGGER, "DELETE"),
            ):
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {trigger_name} "
                    f"BEFORE {timing} ON collector_cycle_starts_v1 BEGIN "
                    "SELECT RAISE(ABORT, 'collector cycle starts are immutable'); END"
                )
            for trigger_name, timing in (
                (_CYCLE_TERMINAL_IMMUTABLE_UPDATE_TRIGGER, "UPDATE"),
                (_CYCLE_TERMINAL_IMMUTABLE_DELETE_TRIGGER, "DELETE"),
            ):
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {trigger_name} "
                    f"BEFORE {timing} ON collector_cycle_terminals_v1 BEGIN "
                    "SELECT RAISE(ABORT, 'collector cycle terminals are immutable'); END"
                )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS collector_delta_tombstones_v1 ("
                "delta_id TEXT PRIMARY KEY NOT NULL,"
                "source_id TEXT NOT NULL,"
                "stream_epoch TEXT NOT NULL,"
                "payload_sha256 TEXT NOT NULL,"
                "compacted_at TEXT NOT NULL,"
                "plan_id TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS collector_epoch_activations_v1 ("
                "source_id TEXT NOT NULL,"
                "generation INTEGER NOT NULL CHECK(generation > 0),"
                "stream_epoch TEXT NOT NULL,"
                "activated_at TEXT NOT NULL,"
                "PRIMARY KEY(source_id, generation))"
            )
            marker = connection.execute(
                "SELECT value FROM collector_meta WHERE key=?",
                (_PROJECTION_INTEGRITY_META_KEY,),
            ).fetchone()
            trigger = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
                (_PROJECTION_IMMUTABILITY_TRIGGER,),
            ).fetchone()

            if marker is None:
                rows = connection.execute(
                    f"SELECT {_DELTA_SELECT_COLUMNS} "
                    "FROM collector_deltas ORDER BY commit_seq"
                ).fetchall()
                for row in rows:
                    self._row_delta(row)
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {_PROJECTION_IMMUTABILITY_TRIGGER} "
                    "BEFORE UPDATE OF "
                    + ", ".join(_INDEXED_PROJECTION_FIELDS)
                    + " ON collector_deltas BEGIN "
                    "SELECT RAISE(ABORT, 'collector delta indexed projections are immutable'); "
                    "END"
                )
                connection.execute(
                    "INSERT INTO collector_meta(key, value) VALUES(?, '1')",
                    (_PROJECTION_INTEGRITY_META_KEY,),
                )
            elif marker[0] != "1" or trigger is None:
                raise ValueError("collector indexed projection integrity guard is missing")
            connection.commit()
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ValueError("invalid causal collector store") from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row_delta(row: sqlite3.Row) -> CollectorDelta:
        delta = _SQLiteCollectorDeltaStore._row_delta(row)
        keys = set(row.keys())
        projection_keys = set(_INDEXED_PROJECTION_FIELDS)
        if keys & projection_keys:
            if not projection_keys.issubset(keys):
                raise ValueError("collector delta indexed projection is incomplete")
            for field in _INDEXED_PROJECTION_FIELDS:
                if row[field] != getattr(delta, field):
                    raise ValueError(
                        f"collector delta indexed projection conflicts with payload: {field}"
                    )
        return delta

    @staticmethod
    def _cycle_terminal_payload_sha256(payload_json: str) -> str:
        return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    @staticmethod
    def _cycle_terminal_payload_json(payload: dict[str, object]) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _schedule_interval_text(value: object) -> str:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            raise ValueError("schedule interval_seconds must be positive and finite")
        return repr(float(value))

    @staticmethod
    def _schedule_max_items(value: object) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("collector schedule max_items must be a positive integer")
        return value

    @staticmethod
    def _schedule_evaluation_window(
        start_slot_ordinal: object,
        end_slot_ordinal: object,
    ) -> tuple[int | None, int | None]:
        if start_slot_ordinal is None and end_slot_ordinal is None:
            return None, None
        if start_slot_ordinal is None or end_slot_ordinal is None:
            raise ValueError(
                "collector schedule evaluation window must bind both start and end"
            )
        for name, value in (
            ("evaluation_start_slot_ordinal", start_slot_ordinal),
            ("evaluation_end_slot_ordinal", end_slot_ordinal),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if end_slot_ordinal < start_slot_ordinal:
            raise ValueError(
                "evaluation_end_slot_ordinal cannot precede "
                "evaluation_start_slot_ordinal"
            )
        if (
            end_slot_ordinal - start_slot_ordinal + 1
            > _MAX_SCHEDULE_EVIDENCE_SLOTS
        ):
            raise ValueError(
                "collector schedule evaluation window exceeds bounded slot limit"
            )
        return start_slot_ordinal, end_slot_ordinal

    @classmethod
    def _collector_schedule_id(
        cls,
        *,
        source_id: str,
        run_id: str,
        stream_epoch: str,
        anchor_at: str,
        interval_seconds: str,
        max_items: int,
        evaluation_start_slot_ordinal: int | None,
        evaluation_end_slot_ordinal: int | None,
    ) -> str:
        payload = cls._cycle_terminal_payload_json(
            {
                "schema_version": 4,
                "policy": _SCHEDULE_POLICY,
                "source_id": source_id,
                "run_id": run_id,
                "stream_epoch": stream_epoch,
                "anchor_at": anchor_at,
                "interval_seconds": interval_seconds,
                "max_items": cls._schedule_max_items(max_items),
                "evaluation_start_slot_ordinal": evaluation_start_slot_ordinal,
                "evaluation_end_slot_ordinal": evaluation_end_slot_ordinal,
            }
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _collector_schedule_due_at(
        *,
        anchor_at: str,
        interval_seconds: str,
        slot_ordinal: int,
    ) -> str:
        if (
            isinstance(slot_ordinal, bool)
            or not isinstance(slot_ordinal, int)
            or slot_ordinal < 0
        ):
            raise ValueError("slot_ordinal must be a non-negative integer")
        anchor = _instant(anchor_at, "anchor_at")
        interval = float(interval_seconds)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("stored collector schedule interval is invalid")
        return (anchor + timedelta(seconds=interval * slot_ordinal)).isoformat()

    def _ensure_collector_schedule(
        self,
        *,
        source_id: str,
        run_id: str,
        stream_epoch: str,
        anchor_at: str,
        interval_seconds: float,
        max_items: int,
        evaluation_start_slot_ordinal: int | None = None,
        evaluation_end_slot_ordinal: int | None = None,
    ) -> dict[str, object]:
        """Create or re-resolve one immutable prospective schedule for a durable run."""

        source_id = _text(source_id, "source_id")
        run_id = _text(run_id, "run_id")
        stream_epoch = _text(stream_epoch, "stream_epoch")
        canonical_anchor = _instant(anchor_at, "anchor_at").isoformat()
        interval_text = self._schedule_interval_text(interval_seconds)
        canonical_max_items = self._schedule_max_items(max_items)
        evaluation_start, evaluation_end = self._schedule_evaluation_window(
            evaluation_start_slot_ordinal,
            evaluation_end_slot_ordinal,
        )
        candidate_id = self._collector_schedule_id(
            source_id=source_id,
            run_id=run_id,
            stream_epoch=stream_epoch,
            anchor_at=canonical_anchor,
            interval_seconds=interval_text,
            max_items=canonical_max_items,
            evaluation_start_slot_ordinal=evaluation_start,
            evaluation_end_slot_ordinal=evaluation_end,
        )

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT schedule_id, policy, stream_epoch, anchor_at, interval_seconds, max_items, "
                "evaluation_start_slot_ordinal, evaluation_end_slot_ordinal "
                "FROM collector_schedules_v1 WHERE source_id=? AND run_id=?",
                (source_id, run_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO collector_schedules_v1("
                    "source_id, run_id, schedule_id, policy, stream_epoch, "
                    "anchor_at, interval_seconds, max_items, evaluation_start_slot_ordinal, "
                    "evaluation_end_slot_ordinal"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        source_id,
                        run_id,
                        candidate_id,
                        _SCHEDULE_POLICY,
                        stream_epoch,
                        canonical_anchor,
                        interval_text,
                        canonical_max_items,
                        evaluation_start,
                        evaluation_end,
                    ),
                )
                schedule_id = candidate_id
                stored_epoch = stream_epoch
                stored_anchor = canonical_anchor
                stored_interval = interval_text
                stored_max_items = canonical_max_items
                stored_evaluation_start = evaluation_start
                stored_evaluation_end = evaluation_end
            else:
                if row["stream_epoch"] is None:
                    raise ValueError(
                        "legacy collector schedule lacks frozen stream_epoch authority"
                    )
                if row["max_items"] is None:
                    raise ValueError(
                        "legacy collector schedule lacks frozen max_items authority"
                    )
                stored_epoch = _text(row["stream_epoch"], "stream_epoch")
                stored_max_items = self._schedule_max_items(row["max_items"])
                stored_anchor = _instant(row["anchor_at"], "anchor_at").isoformat()
                stored_interval = self._schedule_interval_text(
                    float(row["interval_seconds"])
                )
                (
                    stored_evaluation_start,
                    stored_evaluation_end,
                ) = self._schedule_evaluation_window(
                    row["evaluation_start_slot_ordinal"],
                    row["evaluation_end_slot_ordinal"],
                )
                expected_id = self._collector_schedule_id(
                    source_id=source_id,
                    run_id=run_id,
                    stream_epoch=stored_epoch,
                    anchor_at=stored_anchor,
                    interval_seconds=stored_interval,
                    max_items=stored_max_items,
                    evaluation_start_slot_ordinal=stored_evaluation_start,
                    evaluation_end_slot_ordinal=stored_evaluation_end,
                )
                if (
                    row["policy"] != _SCHEDULE_POLICY
                    or row["schedule_id"] != expected_id
                    or row["stream_epoch"] != stored_epoch
                    or row["anchor_at"] != stored_anchor
                    or row["interval_seconds"] != stored_interval
                    or row["max_items"] != stored_max_items
                    or row["evaluation_start_slot_ordinal"]
                    != stored_evaluation_start
                    or row["evaluation_end_slot_ordinal"]
                    != stored_evaluation_end
                ):
                    raise ValueError("collector schedule identity is corrupt")
                if stored_epoch != stream_epoch:
                    raise ValueError(
                        "collector schedule stream_epoch cannot change within a durable run"
                    )
                if stored_interval != interval_text:
                    raise ValueError(
                        "collector schedule interval cannot change within a durable run"
                    )
                if stored_max_items != canonical_max_items:
                    raise ValueError(
                        "collector schedule max_items cannot change within a durable run"
                    )
                if (
                    stored_evaluation_start,
                    stored_evaluation_end,
                ) != (evaluation_start, evaluation_end):
                    raise ValueError(
                        "collector schedule evaluation window cannot change "
                        "within a durable run"
                    )
                schedule_id = row["schedule_id"]
            connection.commit()
            return {
                "schema_version": 4,
                "schedule_id": schedule_id,
                "policy": _SCHEDULE_POLICY,
                "source_id": source_id,
                "run_id": run_id,
                "stream_epoch": stored_epoch,
                "anchor_at": stored_anchor,
                "interval_seconds": stored_interval,
                "max_items": stored_max_items,
                "evaluation_start_slot_ordinal": stored_evaluation_start,
                "evaluation_end_slot_ordinal": stored_evaluation_end,
            }
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ValueError("cannot establish collector schedule authority") from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _next_collector_schedule_slot(
        self,
        *,
        source_id: str,
        run_id: str,
    ) -> dict[str, object]:
        """Resolve the next immutable logical due slot without minting a START."""

        source_id = _text(source_id, "source_id")
        run_id = _text(run_id, "run_id")
        connection = self._connect()
        try:
            schedule = connection.execute(
                "SELECT schedule_id, policy, stream_epoch, anchor_at, interval_seconds, max_items, "
                "evaluation_start_slot_ordinal, evaluation_end_slot_ordinal "
                "FROM collector_schedules_v1 WHERE source_id=? AND run_id=?",
                (source_id, run_id),
            ).fetchone()
            if schedule is None:
                raise ValueError("collector schedule authority is missing")
            if schedule["policy"] != _SCHEDULE_POLICY:
                raise ValueError("collector schedule policy is unsupported")
            if schedule["stream_epoch"] is None:
                raise ValueError(
                    "legacy collector schedule lacks frozen stream_epoch authority"
                )
            stream_epoch = _text(schedule["stream_epoch"], "stream_epoch")
            if schedule["max_items"] is None:
                raise ValueError(
                    "legacy collector schedule lacks frozen max_items authority"
                )
            frozen_max_items = self._schedule_max_items(schedule["max_items"])
            expected_id = self._collector_schedule_id(
                source_id=source_id,
                run_id=run_id,
                stream_epoch=stream_epoch,
                anchor_at=schedule["anchor_at"],
                interval_seconds=schedule["interval_seconds"],
                max_items=frozen_max_items,
                evaluation_start_slot_ordinal=schedule[
                    "evaluation_start_slot_ordinal"
                ],
                evaluation_end_slot_ordinal=schedule[
                    "evaluation_end_slot_ordinal"
                ],
            )
            if schedule["schedule_id"] != expected_id:
                raise ValueError("collector schedule identity digest mismatch")
            row = connection.execute(
                "SELECT MAX(slot_ordinal) FROM collector_schedule_slots_v1 "
                "WHERE source_id=? AND run_id=?",
                (source_id, run_id),
            ).fetchone()
            slot_ordinal = (
                0 if row is None or row[0] is None else int(row[0]) + 1
            )
            due_at = self._collector_schedule_due_at(
                anchor_at=schedule["anchor_at"],
                interval_seconds=schedule["interval_seconds"],
                slot_ordinal=slot_ordinal,
            )
            return {
                "schedule_id": schedule["schedule_id"],
                "stream_epoch": stream_epoch,
                "max_items": frozen_max_items,
                "slot_ordinal": slot_ordinal,
                "due_at": due_at,
            }
        finally:
            connection.close()

    def _begin_scheduled_collector_cycle(
        self,
        *,
        source_id: str,
        run_id: str,
        stream_epoch: str,
        max_items: int,
        slot_ordinal: int,
        due_at: str,
        attempted_at: str,
    ) -> int:
        """Atomically bind one due slot to exactly one canonical collector START."""

        source_id = _text(source_id, "source_id")
        run_id = _text(run_id, "run_id")
        stream_epoch = _text(stream_epoch, "stream_epoch")
        canonical_max_items = self._schedule_max_items(max_items)
        if (
            isinstance(slot_ordinal, bool)
            or not isinstance(slot_ordinal, int)
            or slot_ordinal < 0
        ):
            raise ValueError("slot_ordinal must be a non-negative integer")
        canonical_due = _instant(due_at, "due_at").isoformat()
        canonical_attempt = _instant(attempted_at, "attempted_at").isoformat()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            schedule = connection.execute(
                "SELECT schedule_id, policy, stream_epoch, anchor_at, interval_seconds, max_items, "
                "evaluation_start_slot_ordinal, evaluation_end_slot_ordinal "
                "FROM collector_schedules_v1 WHERE source_id=? AND run_id=?",
                (source_id, run_id),
            ).fetchone()
            if schedule is None or schedule["policy"] != _SCHEDULE_POLICY:
                raise ValueError("collector schedule authority is missing or invalid")
            if schedule["stream_epoch"] is None:
                raise ValueError(
                    "legacy collector schedule lacks frozen stream_epoch authority"
                )
            frozen_epoch = _text(schedule["stream_epoch"], "stream_epoch")
            if schedule["max_items"] is None:
                raise ValueError(
                    "legacy collector schedule lacks frozen max_items authority"
                )
            frozen_max_items = self._schedule_max_items(schedule["max_items"])
            if canonical_max_items != frozen_max_items:
                raise ValueError(
                    "collector schedule max_items does not match current acquisition config"
                )
            if stream_epoch != frozen_epoch:
                raise ValueError(
                    "collector schedule stream_epoch does not match current source"
                )
            expected_id = self._collector_schedule_id(
                source_id=source_id,
                run_id=run_id,
                stream_epoch=frozen_epoch,
                anchor_at=schedule["anchor_at"],
                interval_seconds=schedule["interval_seconds"],
                max_items=frozen_max_items,
                evaluation_start_slot_ordinal=schedule[
                    "evaluation_start_slot_ordinal"
                ],
                evaluation_end_slot_ordinal=schedule[
                    "evaluation_end_slot_ordinal"
                ],
            )
            if schedule["schedule_id"] != expected_id:
                raise ValueError("collector schedule identity digest mismatch")
            expected_due = self._collector_schedule_due_at(
                anchor_at=schedule["anchor_at"],
                interval_seconds=schedule["interval_seconds"],
                slot_ordinal=slot_ordinal,
            )
            if canonical_due != expected_due:
                raise ValueError("collector schedule slot due_at is not canonical")
            attempted_instant = _instant(
                canonical_attempt,
                "attempted_at",
            )
            expected_due_instant = _instant(
                expected_due,
                "expected_due_at",
            )
            if attempted_instant < expected_due_instant:
                raise ValueError(
                    "collector schedule START cannot precede frozen due_at"
                )
            previous_start = connection.execute(
                "SELECT starts.attempted_at "
                "FROM collector_schedule_slots_v1 AS slots "
                "JOIN collector_cycle_starts_v1 AS starts "
                "ON starts.source_id=slots.source_id "
                "AND starts.cycle_seq=slots.cycle_seq "
                "WHERE slots.source_id=? AND slots.run_id=? "
                "ORDER BY slots.slot_ordinal DESC LIMIT 1",
                (source_id, run_id),
            ).fetchone()
            if previous_start is not None:
                previous_attempt = _instant(
                    previous_start["attempted_at"],
                    "previous_attempted_at",
                )
                if attempted_instant < previous_attempt:
                    raise ValueError(
                        "collector schedule START time cannot regress"
                    )
            last = connection.execute(
                "SELECT MAX(slot_ordinal) FROM collector_schedule_slots_v1 "
                "WHERE source_id=? AND run_id=?",
                (source_id, run_id),
            ).fetchone()
            next_ordinal = (
                0 if last is None or last[0] is None else int(last[0]) + 1
            )
            if slot_ordinal != next_ordinal:
                raise ValueError(
                    "collector schedule slot is duplicate, skipped, or out of order"
                )

            row = connection.execute(
                "SELECT MAX(cycle_seq) FROM collector_cycle_starts_v1 "
                "WHERE source_id=?",
                (source_id,),
            ).fetchone()
            cycle_seq = 1 if row is None or row[0] is None else int(row[0]) + 1
            connection.execute(
                "INSERT INTO collector_cycle_starts_v1("
                "source_id, cycle_seq, run_id, stream_epoch, attempted_at"
                ") VALUES(?,?,?,?,?)",
                (
                    source_id,
                    cycle_seq,
                    run_id,
                    frozen_epoch,
                    canonical_attempt,
                ),
            )
            connection.execute(
                "INSERT INTO collector_schedule_slots_v1("
                "source_id, run_id, slot_ordinal, due_at, cycle_seq"
                ") VALUES(?,?,?,?,?)",
                (
                    source_id,
                    run_id,
                    slot_ordinal,
                    canonical_due,
                    cycle_seq,
                ),
            )
            connection.commit()
            return cycle_seq
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ValueError(
                "cannot atomically bind collector schedule slot to cycle START"
            ) from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def collector_schedule_evidence(
        self,
        *,
        source_id: str,
        run_id: str,
        start_slot_ordinal: int,
        end_slot_ordinal: int,
    ) -> dict[str, object]:
        """Commit one expected schedule window, including explicit missing STARTs."""

        source_id = _text(source_id, "source_id")
        run_id = _text(run_id, "run_id")
        for name, value in (
            ("start_slot_ordinal", start_slot_ordinal),
            ("end_slot_ordinal", end_slot_ordinal),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        if end_slot_ordinal < start_slot_ordinal:
            raise ValueError("end_slot_ordinal cannot precede start_slot_ordinal")
        expected_count = end_slot_ordinal - start_slot_ordinal + 1
        if expected_count > _MAX_SCHEDULE_EVIDENCE_SLOTS:
            raise ValueError(
                "collector schedule evidence window exceeds bounded slot limit"
            )

        connection = self._connect()
        try:
            schedule = connection.execute(
                "SELECT schedule_id, policy, stream_epoch, anchor_at, interval_seconds, max_items, "
                "evaluation_start_slot_ordinal, evaluation_end_slot_ordinal "
                "FROM collector_schedules_v1 WHERE source_id=? AND run_id=?",
                (source_id, run_id),
            ).fetchone()
            if schedule is None or schedule["policy"] != _SCHEDULE_POLICY:
                raise ValueError("collector schedule authority is missing or invalid")
            if schedule["stream_epoch"] is None:
                raise ValueError(
                    "legacy collector schedule lacks frozen stream_epoch authority"
                )
            frozen_epoch = _text(schedule["stream_epoch"], "stream_epoch")
            if schedule["max_items"] is None:
                raise ValueError(
                    "legacy collector schedule lacks frozen max_items authority"
                )
            frozen_max_items = self._schedule_max_items(schedule["max_items"])
            expected_id = self._collector_schedule_id(
                source_id=source_id,
                run_id=run_id,
                stream_epoch=frozen_epoch,
                anchor_at=schedule["anchor_at"],
                interval_seconds=schedule["interval_seconds"],
                max_items=frozen_max_items,
                evaluation_start_slot_ordinal=schedule[
                    "evaluation_start_slot_ordinal"
                ],
                evaluation_end_slot_ordinal=schedule[
                    "evaluation_end_slot_ordinal"
                ],
            )
            if schedule["schedule_id"] != expected_id:
                raise ValueError("collector schedule identity digest mismatch")

            rows = connection.execute(
                "SELECT b.slot_ordinal, b.due_at, b.cycle_seq, "
                "s.run_id AS start_run_id, s.stream_epoch, s.attempted_at "
                "FROM collector_schedule_slots_v1 AS b "
                "JOIN collector_cycle_starts_v1 AS s "
                "ON s.source_id=b.source_id AND s.cycle_seq=b.cycle_seq "
                "WHERE b.source_id=? AND b.run_id=? "
                "AND b.slot_ordinal>=? AND b.slot_ordinal<=? "
                "ORDER BY b.slot_ordinal",
                (
                    source_id,
                    run_id,
                    start_slot_ordinal,
                    end_slot_ordinal,
                ),
            ).fetchall()
            by_ordinal: dict[int, sqlite3.Row] = {}
            for row in rows:
                ordinal = int(row["slot_ordinal"])
                if ordinal in by_ordinal:
                    raise ValueError("collector schedule slot identity is duplicated")
                if row["start_run_id"] != run_id:
                    raise ValueError(
                        "collector schedule slot is bound to another run START"
                    )
                if row["stream_epoch"] != frozen_epoch:
                    raise ValueError(
                        "collector schedule slot is bound to another stream_epoch"
                    )
                by_ordinal[ordinal] = row

            slots: list[dict[str, object]] = []
            bound_start_count = 0
            missing_start_count = 0
            late_start_count = 0
            early_start_count = 0
            for ordinal in range(start_slot_ordinal, end_slot_ordinal + 1):
                canonical_due = self._collector_schedule_due_at(
                    anchor_at=schedule["anchor_at"],
                    interval_seconds=schedule["interval_seconds"],
                    slot_ordinal=ordinal,
                )
                row = by_ordinal.get(ordinal)
                if row is None:
                    missing_start_count += 1
                    slots.append(
                        {
                            "slot_ordinal": ordinal,
                            "due_at": canonical_due,
                            "cycle_seq": None,
                            "stream_epoch": None,
                            "attempted_at": None,
                            "started_before_due": None,
                            "started_late": None,
                        }
                    )
                    continue
                if row["due_at"] != canonical_due:
                    raise ValueError(
                        "collector schedule slot due_at conflicts with schedule"
                    )
                attempted = _instant(row["attempted_at"], "attempted_at")
                due = _instant(canonical_due, "due_at")
                started_before_due = attempted < due
                started_late = attempted > due
                bound_start_count += 1
                early_start_count += int(started_before_due)
                late_start_count += int(started_late)
                slots.append(
                    {
                        "slot_ordinal": ordinal,
                        "due_at": canonical_due,
                        "cycle_seq": int(row["cycle_seq"]),
                        "stream_epoch": row["stream_epoch"],
                        "attempted_at": attempted.isoformat(),
                        "started_before_due": started_before_due,
                        "started_late": started_late,
                    }
                )

            (
                evaluation_start,
                evaluation_end,
            ) = self._schedule_evaluation_window(
                schedule["evaluation_start_slot_ordinal"],
                schedule["evaluation_end_slot_ordinal"],
            )
            commitment_payload = {
                "schema_version": 4,
                "schedule_id": schedule["schedule_id"],
                "policy": schedule["policy"],
                "source_id": source_id,
                "run_id": run_id,
                "stream_epoch": frozen_epoch,
                "anchor_at": schedule["anchor_at"],
                "interval_seconds": schedule["interval_seconds"],
                "max_items": frozen_max_items,
                "evaluation_start_slot_ordinal": evaluation_start,
                "evaluation_end_slot_ordinal": evaluation_end,
                "start_slot_ordinal": start_slot_ordinal,
                "end_slot_ordinal": end_slot_ordinal,
                "expected_slot_count": expected_count,
                "bound_start_count": bound_start_count,
                "missing_start_count": missing_start_count,
                "early_start_count": early_start_count,
                "late_start_count": late_start_count,
                "slots": slots,
            }
            commitment_json = self._cycle_terminal_payload_json(commitment_payload)
            return {
                **commitment_payload,
                "commitment_sha256": hashlib.sha256(
                    commitment_json.encode("utf-8")
                ).hexdigest(),
            }
        finally:
            connection.close()

    def _begin_collector_cycle(
        self,
        *,
        source_id: str,
        run_id: str,
        stream_epoch: str,
        attempted_at: str,
    ) -> int:
        """Append one immutable acquisition-attempt START to the canonical store."""

        source_id = _text(source_id, "source_id")
        run_id = _text(run_id, "run_id")
        stream_epoch = _text(stream_epoch, "stream_epoch")
        _instant(attempted_at, "attempted_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT MAX(cycle_seq) FROM collector_cycle_starts_v1 "
                "WHERE source_id=?",
                (source_id,),
            ).fetchone()
            cycle_seq = 1 if row is None or row[0] is None else int(row[0]) + 1
            connection.execute(
                "INSERT INTO collector_cycle_starts_v1("
                "source_id, cycle_seq, run_id, stream_epoch, attempted_at"
                ") VALUES(?,?,?,?,?)",
                (source_id, cycle_seq, run_id, stream_epoch, attempted_at),
            )
            connection.commit()
            return cycle_seq
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ValueError("cannot append collector cycle START evidence") from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _finish_collector_cycle(
        self,
        *,
        source_id: str,
        cycle_seq: int,
        status: str,
        completed_at: str,
        catalog_changes: tuple[str, ...],
        observed_delta_ids: tuple[str, ...],
        committed_delta_ids: tuple[str, ...],
        duplicate_delta_ids: tuple[str, ...],
        error_code: str | None = None,
    ) -> None:
        """Append one immutable terminal receipt for an already-started cycle."""

        source_id = _text(source_id, "source_id")
        if isinstance(cycle_seq, bool) or not isinstance(cycle_seq, int) or cycle_seq <= 0:
            raise ValueError("cycle_seq must be a positive integer")
        if status not in _CYCLE_TERMINAL_STATUSES:
            raise ValueError("unsupported collector cycle terminal status")
        _instant(completed_at, "completed_at")
        for name, values in (
            ("catalog_changes", catalog_changes),
            ("observed_delta_ids", observed_delta_ids),
            ("committed_delta_ids", committed_delta_ids),
            ("duplicate_delta_ids", duplicate_delta_ids),
        ):
            if not isinstance(values, tuple):
                raise TypeError(f"{name} must be a tuple")
            for value in values:
                _text(value, f"{name} item")
        if len(observed_delta_ids) != (
            len(committed_delta_ids) + len(duplicate_delta_ids)
        ) or sorted(observed_delta_ids) != sorted(
            committed_delta_ids + duplicate_delta_ids
        ):
            raise ValueError(
                "observed deltas must equal the committed/duplicate classification"
            )
        if error_code is not None:
            error_code = _text(error_code, "error_code")
        if status == "SUCCESS" and error_code is not None:
            raise ValueError("successful collector cycle cannot carry error_code")
        if status != "SUCCESS" and error_code is None:
            raise ValueError("non-success collector cycle requires error_code")
        if status == "PROVIDER_UNAVAILABLE" and observed_delta_ids:
            raise ValueError(
                "provider-unavailable cycle cannot claim observed delta evidence"
            )

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            start = connection.execute(
                "SELECT run_id, stream_epoch, attempted_at "
                "FROM collector_cycle_starts_v1 "
                "WHERE source_id=? AND cycle_seq=?",
                (source_id, cycle_seq),
            ).fetchone()
            if start is None:
                raise ValueError("collector cycle START evidence is missing")
            if _instant(completed_at, "completed_at") < _instant(
                start["attempted_at"], "attempted_at"
            ):
                raise ValueError("collector cycle terminal predates its START")
            existing = connection.execute(
                "SELECT 1 FROM collector_cycle_terminals_v1 "
                "WHERE source_id=? AND cycle_seq=?",
                (source_id, cycle_seq),
            ).fetchone()
            if existing is not None:
                raise ValueError("collector cycle already has terminal evidence")

            observed_deltas: list[dict[str, str]] = []
            for delta_id in observed_delta_ids:
                row = connection.execute(
                    "SELECT source_id, payload_sha256 FROM collector_deltas "
                    "WHERE delta_id=?",
                    (delta_id,),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        "SELECT source_id, payload_sha256 "
                        "FROM collector_delta_tombstones_v1 WHERE delta_id=?",
                        (delta_id,),
                    ).fetchone()
                if row is None or row["source_id"] != source_id:
                    raise ValueError(
                        "collector cycle references delta without canonical source evidence"
                    )
                digest = row["payload_sha256"]
                if (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or digest != digest.lower()
                    or any(character not in "0123456789abcdef" for character in digest)
                ):
                    raise ValueError("collector delta evidence digest is malformed")
                observed_deltas.append(
                    {"delta_id": delta_id, "payload_sha256": digest}
                )

            payload: dict[str, object] = {
                "schema": "autosport.collector_cycle_terminal",
                "schema_version": 1,
                "source_id": source_id,
                "cycle_seq": cycle_seq,
                "run_id": start["run_id"],
                "stream_epoch": start["stream_epoch"],
                "attempted_at": start["attempted_at"],
                "completed_at": completed_at,
                "status": status,
                "catalog_changes": list(catalog_changes),
                "observed_deltas": observed_deltas,
                "committed_delta_ids": list(committed_delta_ids),
                "duplicate_delta_ids": list(duplicate_delta_ids),
                "error_code": error_code,
            }
            payload_json = self._cycle_terminal_payload_json(payload)
            payload_sha256 = self._cycle_terminal_payload_sha256(payload_json)
            connection.execute(
                "INSERT INTO collector_cycle_terminals_v1("
                "source_id, cycle_seq, payload_sha256, payload_json"
                ") VALUES(?,?,?,?)",
                (source_id, cycle_seq, payload_sha256, payload_json),
            )
            connection.commit()
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ValueError("cannot append collector cycle terminal evidence") from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def collector_cycle_evidence(
        self,
        *,
        source_id: str,
        start_cycle_seq: int,
        end_cycle_seq: int,
    ) -> tuple[dict[str, object], ...]:
        """Read and authenticate one explicit source-local cycle sequence window."""

        source_id = _text(source_id, "source_id")
        for name, value in (
            ("start_cycle_seq", start_cycle_seq),
            ("end_cycle_seq", end_cycle_seq),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if end_cycle_seq < start_cycle_seq:
            raise ValueError("end_cycle_seq cannot precede start_cycle_seq")

        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT s.cycle_seq, s.run_id, s.stream_epoch, s.attempted_at, "
                "t.payload_sha256, t.payload_json "
                "FROM collector_cycle_starts_v1 AS s "
                "LEFT JOIN collector_cycle_terminals_v1 AS t "
                "ON t.source_id=s.source_id AND t.cycle_seq=s.cycle_seq "
                "WHERE s.source_id=? AND s.cycle_seq>=? AND s.cycle_seq<=? "
                "ORDER BY s.cycle_seq",
                (source_id, start_cycle_seq, end_cycle_seq),
            ).fetchall()
            evidence: list[dict[str, object]] = []
            for row in rows:
                terminal: dict[str, object] | None = None
                payload_json = row["payload_json"]
                payload_sha256 = row["payload_sha256"]
                if (payload_json is None) != (payload_sha256 is None):
                    raise ValueError(
                        "collector cycle terminal evidence is structurally incomplete"
                    )
                if payload_json is not None:
                    if (
                        self._cycle_terminal_payload_sha256(payload_json)
                        != payload_sha256
                    ):
                        raise ValueError(
                            "collector cycle terminal evidence digest mismatch"
                        )
                    try:
                        parsed = json.loads(payload_json)
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise ValueError(
                            "collector cycle terminal evidence JSON is malformed"
                        ) from exc
                    if not isinstance(parsed, dict):
                        raise ValueError(
                            "collector cycle terminal evidence must be an object"
                        )
                    expected_start = {
                        "source_id": source_id,
                        "cycle_seq": int(row["cycle_seq"]),
                        "run_id": row["run_id"],
                        "stream_epoch": row["stream_epoch"],
                        "attempted_at": row["attempted_at"],
                    }
                    if any(parsed.get(key) != value for key, value in expected_start.items()):
                        raise ValueError(
                            "collector cycle terminal conflicts with immutable START"
                        )
                    if parsed.get("status") not in _CYCLE_TERMINAL_STATUSES:
                        raise ValueError(
                            "collector cycle terminal status is unsupported"
                        )
                    terminal = parsed
                evidence.append(
                    {
                        "source_id": source_id,
                        "cycle_seq": int(row["cycle_seq"]),
                        "run_id": row["run_id"],
                        "stream_epoch": row["stream_epoch"],
                        "attempted_at": row["attempted_at"],
                        "terminal": terminal,
                    }
                )
            return tuple(evidence)
        except sqlite3.DatabaseError as exc:
            raise ValueError("cannot read collector cycle evidence") from exc
        finally:
            connection.close()

    def runtime_stream_epoch(self, source_id: str) -> tuple[str, int] | None:
        """Return the latest product-owned active epoch and monotonic generation."""

        source_id = _text(source_id, "source_id")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT generation, stream_epoch FROM collector_epoch_activations_v1 "
                "WHERE source_id=? ORDER BY generation DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            if row is None:
                return None
            return (_text(row["stream_epoch"], "stream_epoch"), int(row["generation"]))
        except sqlite3.DatabaseError as exc:
            raise ValueError("invalid causal collector active-epoch authority") from exc
        finally:
            connection.close()

    def _bootstrap_or_recover_runtime_stream_epoch(
        self,
        *,
        source_id: str,
        stream_epoch: str,
        activated_at: str,
    ) -> int | None:
        """Establish epoch authority only from empty-state or immutable evidence.

        On a genuinely empty source history, the configured service epoch is harmless
        bootstrap metadata because there is nothing retention could delete. Once any
        durable delta exists, a transition is allowed only when the newest immutable
        retained commit proves this exact source/epoch. This also heals predecessor
        crash prefixes without trusting mutable source metadata by itself.
        """

        source_id = _text(source_id, "source_id")
        stream_epoch = _text(stream_epoch, "stream_epoch")
        _instant(activated_at, "activated_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT generation, stream_epoch FROM collector_epoch_activations_v1 "
                "WHERE source_id=? ORDER BY generation DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            if current is not None and current["stream_epoch"] == stream_epoch:
                connection.commit()
                return int(current["generation"])

            latest = connection.execute(
                f"SELECT {_DELTA_SELECT_COLUMNS} FROM collector_deltas "
                "WHERE source_id=? ORDER BY commit_seq DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            if latest is None:
                if current is not None:
                    connection.commit()
                    return int(current["generation"])
                generation = 1
            else:
                durable = self._row_delta(latest)
                if (
                    durable.source_id != source_id
                    or durable.stream_epoch != stream_epoch
                ):
                    connection.commit()
                    return None if current is None else int(current["generation"])
                generation = 1 if current is None else int(current["generation"]) + 1

            connection.execute(
                "INSERT INTO collector_epoch_activations_v1("
                "source_id, generation, stream_epoch, activated_at"
                ") VALUES(?,?,?,?)",
                (source_id, generation, stream_epoch, activated_at),
            )
            connection.commit()
            return generation
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ValueError("invalid causal collector active-epoch recovery") from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _append_with_runtime_stream_epoch(
        self,
        delta: CollectorDelta,
        *,
        activated_at: str,
    ) -> bool:
        """Atomically admit one canonical delta and its active-epoch authority."""

        if not isinstance(delta, CollectorDelta):
            raise TypeError("delta must be CollectorDelta")
        delta.validate()
        _instant(activated_at, "activated_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = self._append_connection(connection, delta)
            if changed:
                self._write({"schema_version": self.schema_version})

            activation_evidence = changed
            if not activation_evidence:
                latest = connection.execute(
                    "SELECT delta_id, stream_epoch FROM collector_deltas "
                    "WHERE source_id=? ORDER BY commit_seq DESC LIMIT 1",
                    (delta.source_id,),
                ).fetchone()
                activation_evidence = (
                    latest is not None
                    and latest["delta_id"] == delta.delta_id
                    and latest["stream_epoch"] == delta.stream_epoch
                )

            if activation_evidence:
                current = connection.execute(
                    "SELECT generation, stream_epoch FROM collector_epoch_activations_v1 "
                    "WHERE source_id=? ORDER BY generation DESC LIMIT 1",
                    (delta.source_id,),
                ).fetchone()
                if current is None or current["stream_epoch"] != delta.stream_epoch:
                    generation = 1 if current is None else int(current["generation"]) + 1
                    connection.execute(
                        "INSERT INTO collector_epoch_activations_v1("
                        "source_id, generation, stream_epoch, activated_at"
                        ") VALUES(?,?,?,?)",
                        (
                            delta.source_id,
                            generation,
                            delta.stream_epoch,
                            activated_at,
                        ),
                    )
            connection.commit()
            return changed
        except sqlite3.IntegrityError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise DeltaConflictError(
                "collector delta violates durable identity constraints"
            ) from exc
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ValueError("invalid causal collector store") from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @classmethod
    def _delta_by_id(
        cls,
        connection: sqlite3.Connection,
        delta_id: str,
    ) -> CollectorDelta | None:
        row = connection.execute(
            f"SELECT {_DELTA_SELECT_COLUMNS} FROM collector_deltas WHERE delta_id=?",
            (delta_id,),
        ).fetchone()
        if row is None:
            return None
        delta = cls._row_delta(row)
        if delta.delta_id != delta_id:
            raise ValueError("collector delta indexed identity conflicts with payload")
        return delta

    @staticmethod
    def _retained_stream_epochs(
        connection: sqlite3.Connection,
        source_id: str,
    ) -> tuple[str, ...]:
        """Enumerate retained epochs with index seeks, never a retained-delta scan.

        The causal index starts with ``(source_id, stream_epoch, ...)``. Repeated
        range seeks therefore visit one row per distinct epoch instead of scanning
        every delta or materializing ``DISTINCT`` history. Explicit epoch rollover
        stays O(number_of_retained_epochs), while ordinary same-epoch append remains
        on the single-checkpoint fast path.
        """

        epochs: list[str] = []
        previous: str | None = None
        while True:
            if previous is None:
                row = connection.execute(
                    "SELECT stream_epoch FROM collector_deltas "
                    "WHERE source_id=? ORDER BY stream_epoch LIMIT 1",
                    (source_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT stream_epoch FROM collector_deltas "
                    "WHERE source_id=? AND stream_epoch>? "
                    "ORDER BY stream_epoch LIMIT 1",
                    (source_id, previous),
                ).fetchone()
            if row is None:
                return tuple(epochs)
            epoch = row["stream_epoch"]
            if previous is not None and epoch <= previous:
                raise ValueError("collector retained epoch index is not monotonic")
            epochs.append(epoch)
            previous = epoch

    @classmethod
    def _verified_stream_checkpoint(
        cls,
        connection: sqlite3.Connection,
        source_id: str,
        stream_epoch: str,
    ) -> StreamCheckpoint | None:
        """Bind one stream projection to immutable history using indexed probes only."""

        row = connection.execute(
            "SELECT last_cursor, last_position, last_delta_id "
            "FROM collector_streams WHERE source_id=? AND stream_epoch=?",
            (source_id, stream_epoch),
        ).fetchone()
        if row is None:
            historical = connection.execute(
                "SELECT 1 FROM collector_deltas "
                "WHERE source_id=? AND stream_epoch=? LIMIT 1",
                (source_id, stream_epoch),
            ).fetchone()
            if historical is not None:
                raise ValueError(
                    "collector stream checkpoint conflicts with immutable delta history"
                )

            retained_epochs = cls._retained_stream_epochs(connection, source_id)
            if retained_epochs:
                checkpoint_rows = connection.execute(
                    "SELECT stream_epoch FROM collector_streams "
                    "WHERE source_id=? ORDER BY stream_epoch",
                    (source_id,),
                ).fetchall()
                checkpoint_epochs = tuple(
                    checkpoint_row["stream_epoch"] for checkpoint_row in checkpoint_rows
                )
                if checkpoint_epochs != retained_epochs:
                    raise ValueError(
                        "collector stream checkpoint conflicts with immutable delta history"
                    )

                # A fresh epoch may inherit authority only when every retained prior
                # epoch still has exactly one checkpoint and every checkpoint remains
                # bound to its immutable terminal delta. A valid newer checkpoint can
                # therefore never hide deletion of an older retained epoch.
                for retained_epoch in retained_epochs:
                    cls._verified_stream_checkpoint(
                        connection,
                        source_id,
                        retained_epoch,
                    )
            return None

        checkpoint = StreamCheckpoint(
            source_id=source_id,
            stream_epoch=stream_epoch,
            last_cursor=row["last_cursor"],
            last_position=row["last_position"],
            last_delta_id=row["last_delta_id"],
        )
        checkpoint.validate()
        target = cls._delta_by_id(connection, checkpoint.last_delta_id)
        if (
            target is None
            or target.source_id != source_id
            or target.stream_epoch != stream_epoch
            or target.source_cursor != checkpoint.last_cursor
            or target.cursor_position != checkpoint.last_position
            or target.revision_of is not None
        ):
            raise ValueError(
                "collector stream checkpoint conflicts with immutable delta history"
            )
        higher = connection.execute(
            "SELECT 1 FROM collector_deltas "
            "WHERE source_id=? AND stream_epoch=? AND cursor_position>? LIMIT 1",
            (source_id, stream_epoch, checkpoint.last_position),
        ).fetchone()
        if higher is not None:
            raise ValueError(
                "collector stream checkpoint conflicts with immutable delta history"
            )
        return checkpoint

    @classmethod
    def _append_connection(
        cls,
        connection: sqlite3.Connection,
        delta: CollectorDelta,
    ) -> bool:
        delta.validate()
        encoded = _canonical_delta_json(delta)
        digest = _payload_digest(encoded)
        existing = connection.execute(
            f"SELECT {_DELTA_SELECT_COLUMNS} FROM collector_deltas WHERE delta_id=?",
            (delta.delta_id,),
        ).fetchone()
        if existing is not None:
            existing_delta = cls._row_delta(existing)
            if _canonical_delta_json(existing_delta) != encoded:
                raise DeltaConflictError(
                    f"delta {delta.delta_id} conflicts with immutable evidence"
                )
            return False
        tombstone = connection.execute(
            "SELECT source_id, stream_epoch, payload_sha256 "
            "FROM collector_delta_tombstones_v1 WHERE delta_id=?",
            (delta.delta_id,),
        ).fetchone()
        if tombstone is not None:
            if (
                tombstone["source_id"] != delta.source_id
                or tombstone["stream_epoch"] != delta.stream_epoch
                or tombstone["payload_sha256"] != digest
            ):
                raise DeltaConflictError(
                    f"delta {delta.delta_id} conflicts with compacted immutable evidence"
                )
            return False
        cls._verified_stream_checkpoint(
            connection,
            delta.source_id,
            delta.stream_epoch,
        )
        changed = super()._append_connection(connection, delta)
        if changed:
            cls._verified_stream_checkpoint(
                connection,
                delta.source_id,
                delta.stream_epoch,
            )
        return changed

    def _all(self) -> list[CollectorDelta]:
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT {_DELTA_SELECT_COLUMNS} "
                "FROM collector_deltas ORDER BY commit_seq"
            ).fetchall()
            return [self._row_delta(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            raise ValueError("invalid causal collector store") from exc
        finally:
            connection.close()

    def deltas_after_commit(
        self,
        *,
        source_id: str,
        after_delta_id: str | None = None,
        max_items: int = 1000,
    ) -> tuple[CollectorDelta, ...]:
        _text(source_id, "source_id")
        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or max_items <= 0
        ):
            raise ValueError("max_items must be a positive integer")
        connection = self._connect()
        try:
            after_seq = 0
            if after_delta_id is not None:
                _text(after_delta_id, "after_delta_id")
                anchor = connection.execute(
                    f"SELECT {_DELTA_SELECT_COLUMNS} "
                    "FROM collector_deltas WHERE delta_id=?",
                    (after_delta_id,),
                ).fetchone()
                if anchor is None:
                    raise CursorRegressionError(
                        "delivery cursor delta is not present for this source"
                    )
                anchor_delta = self._row_delta(anchor)
                if anchor_delta.source_id != source_id:
                    raise CursorRegressionError(
                        "delivery cursor delta is not present for this source"
                    )
                after_seq = anchor["commit_seq"]
            rows = connection.execute(
                f"SELECT {_DELTA_SELECT_COLUMNS} FROM collector_deltas "
                "WHERE source_id=? AND commit_seq>? ORDER BY commit_seq LIMIT ?",
                (source_id, after_seq, max_items),
            ).fetchall()
            return tuple(self._row_delta(row) for row in rows)
        except sqlite3.DatabaseError as exc:
            raise ValueError("invalid causal collector store") from exc
        finally:
            connection.close()

    def stream_checkpoint(
        self,
        source_id: str,
        stream_epoch: str,
    ) -> StreamCheckpoint | None:
        _text(source_id, "source_id")
        _text(stream_epoch, "stream_epoch")
        connection = self._connect()
        try:
            return self._verified_stream_checkpoint(
                connection,
                source_id,
                stream_epoch,
            )
        except sqlite3.DatabaseError as exc:
            raise ValueError("invalid causal collector store") from exc
        finally:
            connection.close()

    def _write(self, raw: dict[str, Any]) -> None:
        """Retain the legacy pre-commit fault-injection seam without rewriting state.

        Historical crash tests monkeypatch ``_write`` to fail immediately before the
        durable commit. SQLite is now the authority, so the default hook is a no-op;
        a raised exception rolls back the surrounding transaction exactly where the
        old atomic-replace test expects the durability boundary.
        """
        if not isinstance(raw, dict):
            raise TypeError("raw must be a dict")

    def append(self, delta) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = self._append_connection(connection, delta)
            if changed:
                self._write({"schema_version": self.schema_version})
            connection.commit()
            return changed
        except sqlite3.IntegrityError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise DeltaConflictError(
                "collector delta violates durable identity constraints"
            ) from exc
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise ValueError("invalid causal collector store") from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
