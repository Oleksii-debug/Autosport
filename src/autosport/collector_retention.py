from __future__ import annotations

"""Pin-aware bounded compaction for the canonical causal collector SQLite store.

Compaction is deliberately explicit and fail-closed.  It never invents a second
collector authority: the canonical CollectorDeltaStore remains the only delta
store, while durable decision/replay pins are metadata inside that same SQLite
database.  Desktop application evidence is consumed from the existing canonical
DesktopDeltaCheckpointStore.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .causal_collector_legacy import (
    DesktopDeltaCheckpointStore,
    _instant,
    _text,
)
from .collector_sqlite_active_store import CollectorDeltaStore as _CollectorDeltaStore


class RetentionPinKind(StrEnum):
    DECISION = "DECISION"
    REPLAY = "REPLAY"


class CollectorRetentionError(ValueError):
    """Base error for explicit collector retention/compaction operations."""


class CollectorRetentionPlanStaleError(CollectorRetentionError):
    """Raised when authority/pin/delta state moved after a compaction preview."""


@dataclass(frozen=True, slots=True)
class CollectorRetentionPlan:
    source_id: str
    stream_epoch: str
    current_stream_epoch: str
    plan_id: str
    delete_delta_ids: tuple[str, ...]
    retained_delta_ids: tuple[str, ...]
    pinned_delta_ids: tuple[str, ...]
    unacknowledged_delta_ids: tuple[str, ...]
    terminal_checkpoint_delta_id: str
    desktop_transport_anchor_delta_id: str | None
    max_commit_seq: int


@dataclass(frozen=True, slots=True)
class CollectorCompactionResult:
    plan_id: str
    source_id: str
    stream_epoch: str
    deleted_delta_ids: tuple[str, ...]
    retained_delta_ids: tuple[str, ...]
    bytes_before: int
    bytes_after: int
    compacted_at: str


class CollectorRetentionManager:
    """Explicit safety boundary for collector retention and physical compaction.

    Safety rules:
    - the current provider epoch is never compacted;
    - only deltas with a durable canonical desktop application acknowledgement
      may be deleted;
    - the stream terminal checkpoint and latest acknowledged transport anchor are
      retained so restart/delivery cursors remain usable;
    - durable DECISION/REPLAY pins are retained;
    - revision ancestors of every retained row are retained;
    - preview/apply uses a content-bound plan and revalidates under BEGIN IMMEDIATE;
    - deletion and the compaction journal are one SQLite transaction; VACUUM only
      reclaims pages after that durable semantic transition.
    """

    _PIN_TABLE = "collector_retention_pins_v1"
    _JOURNAL_TABLE = "collector_compaction_journal_v1"

    def __init__(self, collector: _CollectorDeltaStore) -> None:
        if type(collector) is not _CollectorDeltaStore:
            raise TypeError("collector must be the canonical CollectorDeltaStore")
        self.collector = collector
        self._ensure_schema()

    @staticmethod
    def _digest(value: str, field: str) -> str:
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError(f"{field} must be a sha256 hex digest")
        try:
            int(value, 16)
        except ValueError as exc:
            raise ValueError(f"{field} must be a sha256 hex digest") from exc
        return value

    def _ensure_schema(self) -> None:
        connection = self.collector._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {self._PIN_TABLE} ("
                "pin_kind TEXT NOT NULL CHECK(pin_kind IN ('DECISION','REPLAY')),"
                "owner_id TEXT NOT NULL,"
                "delta_id TEXT NOT NULL,"
                "canonical_event_digest TEXT NOT NULL,"
                "created_at TEXT NOT NULL,"
                "PRIMARY KEY(pin_kind, owner_id, delta_id))"
            )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS {self._PIN_TABLE}_delta "
                f"ON {self._PIN_TABLE}(delta_id)"
            )
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {self._JOURNAL_TABLE} ("
                "plan_id TEXT PRIMARY KEY NOT NULL,"
                "source_id TEXT NOT NULL,"
                "stream_epoch TEXT NOT NULL,"
                "compacted_at TEXT NOT NULL,"
                "deleted_count INTEGER NOT NULL CHECK(deleted_count >= 0),"
                "deleted_delta_ids_json TEXT NOT NULL,"
                "retained_delta_ids_json TEXT NOT NULL)"
            )
            connection.commit()
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise CollectorRetentionError(
                "cannot initialize collector retention metadata"
            ) from exc
        finally:
            connection.close()

    @staticmethod
    def _normalize_pin_kind(kind: RetentionPinKind | str) -> RetentionPinKind:
        try:
            return RetentionPinKind(kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported collector retention pin kind") from exc

    def pin(
        self,
        *,
        kind: RetentionPinKind | str,
        owner_id: str,
        delta_id: str,
        canonical_event_digest: str,
        created_at: str,
    ) -> bool:
        normalized = self._normalize_pin_kind(kind)
        owner_id = _text(owner_id, "owner_id")
        delta_id = _text(delta_id, "delta_id")
        expected_digest = self._digest(
            canonical_event_digest, "canonical_event_digest"
        )
        _instant(created_at, "created_at")
        connection = self.collector._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_sha256, payload_json "
                "FROM collector_deltas WHERE delta_id=?",
                (delta_id,),
            ).fetchone()
            if row is None:
                raise CollectorRetentionError(
                    "retention pin target is absent from collector history"
                )
            delta = self.collector._row_delta(row)
            if delta.canonical_event_digest != expected_digest:
                raise CollectorRetentionError(
                    "retention pin digest conflicts with collector evidence"
                )
            existing = connection.execute(
                f"SELECT canonical_event_digest, created_at FROM {self._PIN_TABLE} "
                "WHERE pin_kind=? AND owner_id=? AND delta_id=?",
                (normalized.value, owner_id, delta_id),
            ).fetchone()
            if existing is not None:
                if existing["canonical_event_digest"] != expected_digest:
                    raise CollectorRetentionError(
                        "existing retention pin digest conflicts"
                    )
                connection.commit()
                return False
            connection.execute(
                f"INSERT INTO {self._PIN_TABLE}("
                "pin_kind, owner_id, delta_id, canonical_event_digest, created_at"
                ") VALUES(?,?,?,?,?)",
                (
                    normalized.value,
                    owner_id,
                    delta_id,
                    expected_digest,
                    created_at,
                ),
            )
            connection.commit()
            return True
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise CollectorRetentionError("cannot persist collector retention pin") from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def release_pin(
        self,
        *,
        kind: RetentionPinKind | str,
        owner_id: str,
        delta_id: str,
        canonical_event_digest: str,
    ) -> bool:
        """Release exactly one authority-owned pin.

        The exact owner/kind/delta/digest tuple is required so a stale or unrelated
        caller cannot release another authority's retention protection.
        """

        normalized = self._normalize_pin_kind(kind)
        owner_id = _text(owner_id, "owner_id")
        delta_id = _text(delta_id, "delta_id")
        expected_digest = self._digest(
            canonical_event_digest, "canonical_event_digest"
        )
        connection = self.collector._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                f"SELECT canonical_event_digest FROM {self._PIN_TABLE} "
                "WHERE pin_kind=? AND owner_id=? AND delta_id=?",
                (normalized.value, owner_id, delta_id),
            ).fetchone()
            if existing is None:
                connection.commit()
                return False
            if existing["canonical_event_digest"] != expected_digest:
                raise CollectorRetentionError(
                    "retention pin release digest conflicts"
                )
            connection.execute(
                f"DELETE FROM {self._PIN_TABLE} "
                "WHERE pin_kind=? AND owner_id=? AND delta_id=? "
                "AND canonical_event_digest=?",
                (normalized.value, owner_id, delta_id, expected_digest),
            )
            connection.commit()
            return True
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise CollectorRetentionError("cannot release collector retention pin") from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _require_desktop_checkpoint(
        checkpoint: DesktopDeltaCheckpointStore,
    ) -> DesktopDeltaCheckpointStore:
        if type(checkpoint) is not DesktopDeltaCheckpointStore:
            raise TypeError(
                "desktop_checkpoint must be the canonical DesktopDeltaCheckpointStore"
            )
        return checkpoint

    def _build_plan(
        self,
        connection: sqlite3.Connection,
        *,
        source_id: str,
        stream_epoch: str,
        current_stream_epoch: str,
        desktop_checkpoint: DesktopDeltaCheckpointStore,
    ) -> CollectorRetentionPlan:
        if stream_epoch == current_stream_epoch:
            raise CollectorRetentionError(
                "current collector stream epoch cannot be compacted"
            )
        rows = connection.execute(
            "SELECT commit_seq, delta_id, source_id, stream_epoch, cursor_position, "
            "revision_number, desktop_available_at, collector_committed_at, "
            "payload_sha256, payload_json FROM collector_deltas "
            "WHERE source_id=? AND stream_epoch=? ORDER BY commit_seq",
            (source_id, stream_epoch),
        ).fetchall()
        if not rows:
            raise CollectorRetentionError(
                "collector retention target has no retained delta history"
            )
        decoded = [self.collector._row_delta(row) for row in rows]
        checkpoint = self.collector._verified_stream_checkpoint(
            connection, source_id, stream_epoch
        )
        if checkpoint is None:
            raise CollectorRetentionError(
                "collector retention target has no verified stream checkpoint"
            )

        pin_rows = connection.execute(
            f"SELECT p.delta_id, p.canonical_event_digest "
            f"FROM {self._PIN_TABLE} p "
            "JOIN collector_deltas d ON d.delta_id=p.delta_id "
            "WHERE d.source_id=? AND d.stream_epoch=? ORDER BY d.commit_seq, p.pin_kind, p.owner_id",
            (source_id, stream_epoch),
        ).fetchall()
        pinned = {row["delta_id"] for row in pin_rows}
        delta_by_id = {delta.delta_id: delta for delta in decoded}

        acked_ids: list[str] = []
        unacknowledged: set[str] = set()
        for delta in decoded:
            if not desktop_checkpoint.has_ack(delta.delta_id):
                unacknowledged.add(delta.delta_id)
                continue
            receipt = desktop_checkpoint.application_receipt(delta)
            if receipt is None:
                unacknowledged.add(delta.delta_id)
                continue
            if receipt.canonical_event_digest != delta.canonical_event_digest:
                raise CollectorRetentionError(
                    "desktop acknowledgement digest conflicts with collector evidence"
                )
            acked_ids.append(delta.delta_id)

        latest_acked = None
        if acked_ids:
            acked = set(acked_ids)
            for row in reversed(rows):
                if row["delta_id"] in acked:
                    latest_acked = row["delta_id"]
                    break

        protected = set(pinned)
        protected.update(unacknowledged)
        protected.add(checkpoint.last_delta_id)
        if latest_acked is not None:
            protected.add(latest_acked)

        # A retained correction/recovery marker is not independently meaningful
        # without its immutable predecessor. Preserve the complete retained ancestor
        # chain even when the predecessor itself has already been acknowledged.
        changed = True
        while changed:
            changed = False
            for delta_id in tuple(protected):
                delta = delta_by_id.get(delta_id)
                if (
                    delta is not None
                    and delta.revision_of is not None
                    and delta.revision_of in delta_by_id
                    and delta.revision_of not in protected
                ):
                    protected.add(delta.revision_of)
                    changed = True

        delete_ids = tuple(
            row["delta_id"] for row in rows if row["delta_id"] not in protected
        )
        retained_ids = tuple(
            row["delta_id"] for row in rows if row["delta_id"] in protected
        )
        snapshot = {
            "source_id": source_id,
            "stream_epoch": stream_epoch,
            "current_stream_epoch": current_stream_epoch,
            "rows": [
                [row["commit_seq"], row["delta_id"], row["payload_sha256"]]
                for row in rows
            ],
            "delete_delta_ids": list(delete_ids),
            "retained_delta_ids": list(retained_ids),
            "pinned_delta_ids": sorted(pinned),
            "unacknowledged_delta_ids": sorted(unacknowledged),
            "terminal_checkpoint_delta_id": checkpoint.last_delta_id,
            "desktop_transport_anchor_delta_id": latest_acked,
        }
        raw = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        plan_id = hashlib.sha256(raw).hexdigest()
        return CollectorRetentionPlan(
            source_id=source_id,
            stream_epoch=stream_epoch,
            current_stream_epoch=current_stream_epoch,
            plan_id=plan_id,
            delete_delta_ids=delete_ids,
            retained_delta_ids=retained_ids,
            pinned_delta_ids=tuple(sorted(pinned)),
            unacknowledged_delta_ids=tuple(sorted(unacknowledged)),
            terminal_checkpoint_delta_id=checkpoint.last_delta_id,
            desktop_transport_anchor_delta_id=latest_acked,
            max_commit_seq=int(rows[-1]["commit_seq"]),
        )

    def preview(
        self,
        *,
        source_id: str,
        stream_epoch: str,
        current_stream_epoch: str,
        desktop_checkpoint: DesktopDeltaCheckpointStore,
    ) -> CollectorRetentionPlan:
        source_id = _text(source_id, "source_id")
        stream_epoch = _text(stream_epoch, "stream_epoch")
        current_stream_epoch = _text(
            current_stream_epoch, "current_stream_epoch"
        )
        checkpoint = self._require_desktop_checkpoint(desktop_checkpoint)
        connection = self.collector._connect()
        try:
            return self._build_plan(
                connection,
                source_id=source_id,
                stream_epoch=stream_epoch,
                current_stream_epoch=current_stream_epoch,
                desktop_checkpoint=checkpoint,
            )
        except sqlite3.DatabaseError as exc:
            raise CollectorRetentionError(
                "cannot preview collector retention compaction"
            ) from exc
        finally:
            connection.close()

    def compact(
        self,
        plan: CollectorRetentionPlan,
        *,
        desktop_checkpoint: DesktopDeltaCheckpointStore,
        compacted_at: str,
    ) -> CollectorCompactionResult:
        if not isinstance(plan, CollectorRetentionPlan):
            raise TypeError("plan must be CollectorRetentionPlan")
        checkpoint = self._require_desktop_checkpoint(desktop_checkpoint)
        _instant(compacted_at, "compacted_at")
        try:
            bytes_before = self.collector.path.stat().st_size
        except OSError as exc:
            raise CollectorRetentionError("collector store size is unavailable") from exc

        connection = self.collector._connect()
        deleted: tuple[str, ...] = ()
        try:
            connection.execute("BEGIN IMMEDIATE")
            refreshed = self._build_plan(
                connection,
                source_id=plan.source_id,
                stream_epoch=plan.stream_epoch,
                current_stream_epoch=plan.current_stream_epoch,
                desktop_checkpoint=checkpoint,
            )
            if refreshed != plan:
                raise CollectorRetentionPlanStaleError(
                    "collector retention plan is stale; preview again before compaction"
                )
            deleted = plan.delete_delta_ids
            if deleted:
                placeholders = ",".join("?" for _ in deleted)
                cursor = connection.execute(
                    f"DELETE FROM collector_deltas WHERE delta_id IN ({placeholders})",
                    deleted,
                )
                if cursor.rowcount != len(deleted):
                    raise CollectorRetentionPlanStaleError(
                        "collector retention rows changed during compaction"
                    )
            connection.execute(
                f"INSERT INTO {self._JOURNAL_TABLE}("
                "plan_id, source_id, stream_epoch, compacted_at, deleted_count, "
                "deleted_delta_ids_json, retained_delta_ids_json"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    plan.plan_id,
                    plan.source_id,
                    plan.stream_epoch,
                    compacted_at,
                    len(deleted),
                    json.dumps(list(deleted), separators=(",", ":")),
                    json.dumps(list(plan.retained_delta_ids), separators=(",", ":")),
                ),
            )
            connection.commit()
            if deleted:
                connection.execute("VACUUM")
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise CollectorRetentionError(
                "collector compaction failed without a valid terminal result"
            ) from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

        # Reopen through the canonical verification paths after page reclamation.
        self.collector._verify_sqlite_schema()
        self.collector._ensure_projection_integrity_guard()
        verified = self.collector.stream_checkpoint(
            plan.source_id, plan.stream_epoch
        )
        if (
            verified is None
            or verified.last_delta_id != plan.terminal_checkpoint_delta_id
        ):
            raise CollectorRetentionError(
                "collector checkpoint changed across compaction"
            )
        try:
            bytes_after = self.collector.path.stat().st_size
        except OSError as exc:
            raise CollectorRetentionError("collector store size is unavailable") from exc
        return CollectorCompactionResult(
            plan_id=plan.plan_id,
            source_id=plan.source_id,
            stream_epoch=plan.stream_epoch,
            deleted_delta_ids=deleted,
            retained_delta_ids=plan.retained_delta_ids,
            bytes_before=bytes_before,
            bytes_after=bytes_after,
            compacted_at=compacted_at,
        )

    def compaction_journal(self) -> tuple[dict[str, object], ...]:
        connection = self.collector._connect()
        try:
            rows = connection.execute(
                f"SELECT plan_id, source_id, stream_epoch, compacted_at, "
                f"deleted_count, deleted_delta_ids_json, retained_delta_ids_json "
                f"FROM {self._JOURNAL_TABLE} ORDER BY rowid"
            ).fetchall()
            return tuple(
                {
                    "plan_id": row["plan_id"],
                    "source_id": row["source_id"],
                    "stream_epoch": row["stream_epoch"],
                    "compacted_at": row["compacted_at"],
                    "deleted_count": row["deleted_count"],
                    "deleted_delta_ids": tuple(
                        json.loads(row["deleted_delta_ids_json"])
                    ),
                    "retained_delta_ids": tuple(
                        json.loads(row["retained_delta_ids_json"])
                    ),
                }
                for row in rows
            )
        except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
            raise CollectorRetentionError(
                "collector compaction journal is invalid"
            ) from exc
        finally:
            connection.close()
