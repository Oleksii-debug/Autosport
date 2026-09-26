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
from .decision_ledger import JsonlDecisionLedger
from .run_registry import RunRegistry


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
    active_epoch_generation: int
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
    recovered_existing_journal: bool
    reclamation_complete: bool


class CollectorRetentionManager:
    """Explicit safety boundary for collector retention and physical compaction.

    Safety rules:
    - the current collector epoch is resolved from the service-owned durable
      activation journal and is never caller-selectable or compacted;
    - only deltas with a durable canonical desktop application acknowledgement
      may be deleted;
    - the stream terminal checkpoint and latest acknowledged transport anchor are
      retained so restart/delivery cursors remain usable;
    - durable DECISION/REPLAY pins are retained;
    - revision ancestors of every retained row are retained;
    - preview/apply uses a content-bound plan and revalidates under BEGIN IMMEDIATE;
    - deletion and the compaction journal are one SQLite terminal transaction;
      VACUUM is a separately retryable reclamation step and can never erase the
      already-committed terminal result.
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
                "bytes_before INTEGER NOT NULL CHECK(bytes_before >= 0),"
                "deleted_count INTEGER NOT NULL CHECK(deleted_count >= 0),"
                "deleted_delta_ids_json TEXT NOT NULL,"
                "retained_delta_ids_json TEXT NOT NULL,"
                "reclamation_complete INTEGER NOT NULL "
                "CHECK(reclamation_complete IN (0,1)))"
            )
            journal_columns = {
                row["name"]
                for row in connection.execute(
                    f"PRAGMA table_info({self._JOURNAL_TABLE})"
                ).fetchall()
            }
            if "bytes_before" not in journal_columns:
                connection.execute(
                    f"ALTER TABLE {self._JOURNAL_TABLE} "
                    "ADD COLUMN bytes_before INTEGER CHECK(bytes_before >= 0)"
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

    @staticmethod
    def _owner_subject(
        kind: RetentionPinKind,
        owner_id: str,
    ) -> str:
        prefix = f"{kind.value.lower()}:"
        if not owner_id.startswith(prefix):
            raise CollectorRetentionError(
                f"{kind.value} retention pin owner_id must use {prefix!r} namespace"
            )
        subject = owner_id[len(prefix):]
        if not subject or subject.strip() != subject:
            raise CollectorRetentionError(
                "retention pin owner_id has an invalid lifecycle identity"
            )
        return subject

    @staticmethod
    def _terminal_ledger_prefix_count(
        payload: bytes,
        expected_sha256: str,
    ) -> int:
        expected_sha256 = CollectorRetentionManager._digest(
            expected_sha256,
            "terminal_decision_ledger_sha256",
        )
        digest = hashlib.sha256()
        if digest.hexdigest() == expected_sha256:
            return 0
        for line_number, raw_line in enumerate(
            payload.splitlines(keepends=True),
            start=1,
        ):
            digest.update(raw_line)
            if digest.hexdigest() == expected_sha256:
                return line_number
        raise CollectorRetentionError(
            "completed owner Decision Ledger is not an append-prefix of current canonical truth"
        )

    def _require_releasable_owner(
        self,
        kind: RetentionPinKind,
        owner_id: str,
    ) -> None:
        """Re-resolve durable terminal owner truth before releasing retention."""

        subject = self._owner_subject(kind, owner_id)
        workspace = self.collector.path.parent
        try:
            registry = RunRegistry(workspace / "run_registry.json")
            if kind == RetentionPinKind.REPLAY:
                registry.verified_completed_summary_for_run(subject)
                return

            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            snapshot = ledger.verified_snapshot()
            owner_line_number: int | None = None
            replay_run_id: str | None = None
            for line_number, raw_line in enumerate(
                snapshot.payload.splitlines(keepends=True),
                start=1,
            ):
                envelope = json.loads(raw_line)
                record = envelope["record"]
                if record.get("decision_id") == subject:
                    owner_line_number = line_number
                    replay_run_id = record.get("replay_run_id")
                    break
            if (
                owner_line_number is None
                or type(replay_run_id) is not str
                or not replay_run_id
            ):
                raise CollectorRetentionError(
                    "decision retention pin owner is absent from canonical Decision Ledger"
                )

            summary, _summary_sha256 = registry.verified_completed_summary_for_run(
                replay_run_id
            )
            terminal_ledger_sha256 = summary.get("decision_ledger_sha256")
            if type(terminal_ledger_sha256) is not str:
                raise CollectorRetentionError(
                    "completed replay lacks Decision Ledger terminal authority"
                )
            terminal_line_count = self._terminal_ledger_prefix_count(
                snapshot.payload,
                terminal_ledger_sha256,
            )
            if owner_line_number > terminal_line_count:
                raise CollectorRetentionError(
                    "decision retention pin owner was not durable before replay completion"
                )
        except CollectorRetentionError:
            raise
        except Exception as exc:
            raise CollectorRetentionError(
                "retention pin owner lacks verified terminal lifecycle authority"
            ) from exc

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
            # The caller tuple is assertion-only. Re-resolve the canonical owner
            # lifecycle while the collector deletion transaction is still held.
            self._require_releasable_owner(normalized, owner_id)
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

    @staticmethod
    def _resolved_current_stream_epoch(
        connection: sqlite3.Connection,
        source_id: str,
    ) -> tuple[str, int]:
        """Resolve service-owned current epoch plus monotonic activation generation."""

        row = connection.execute(
            "SELECT stream_epoch, generation FROM collector_epoch_activations_v1 "
            "WHERE source_id=? ORDER BY generation DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if row is None:
            raise CollectorRetentionError(
                "collector source has no product-owned active stream epoch"
            )
        generation = row["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise CollectorRetentionError(
                "collector active stream epoch generation is invalid"
            )
        return (
            _text(row["stream_epoch"], "current_stream_epoch"),
            generation,
        )

    def _build_plan(
        self,
        connection: sqlite3.Connection,
        *,
        source_id: str,
        stream_epoch: str,
        desktop_checkpoint: DesktopDeltaCheckpointStore,
    ) -> CollectorRetentionPlan:
        try:
            self.collector._require_verified_commit_order(connection, source_id)
        except ValueError as exc:
            raise CollectorRetentionError(
                "collector retention requires independently verified commit order"
            ) from exc
        current_stream_epoch, active_epoch_generation = (
            self._resolved_current_stream_epoch(connection, source_id)
        )
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
            "active_epoch_generation": active_epoch_generation,
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
            active_epoch_generation=active_epoch_generation,
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
        desktop_checkpoint: DesktopDeltaCheckpointStore,
    ) -> CollectorRetentionPlan:
        source_id = _text(source_id, "source_id")
        stream_epoch = _text(stream_epoch, "stream_epoch")
        checkpoint = self._require_desktop_checkpoint(desktop_checkpoint)
        connection = self.collector._connect()
        try:
            return self._build_plan(
                connection,
                source_id=source_id,
                stream_epoch=stream_epoch,
                desktop_checkpoint=checkpoint,
            )
        except sqlite3.DatabaseError as exc:
            raise CollectorRetentionError(
                "cannot preview collector retention compaction"
            ) from exc
        finally:
            connection.close()

    @staticmethod
    def _decode_journal_ids(value: str, field: str) -> tuple[str, ...]:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CollectorRetentionError(
                f"collector compaction journal {field} is invalid"
            ) from exc
        if (
            not isinstance(decoded, list)
            or any(not isinstance(item, str) or not item for item in decoded)
        ):
            raise CollectorRetentionError(
                f"collector compaction journal {field} is invalid"
            )
        return tuple(decoded)

    @staticmethod
    def _stored_nonnegative_int(value: object, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CollectorRetentionError(
                f"collector compaction journal {field} is invalid"
            )
        return value

    def _existing_terminal(
        self,
        connection: sqlite3.Connection,
        plan: CollectorRetentionPlan,
    ) -> tuple[str, bool, int] | None:
        row = connection.execute(
            f"SELECT source_id, stream_epoch, compacted_at, bytes_before, deleted_count, "
            f"deleted_delta_ids_json, retained_delta_ids_json, reclamation_complete "
            f"FROM {self._JOURNAL_TABLE} WHERE plan_id=?",
            (plan.plan_id,),
        ).fetchone()
        if row is None:
            return None
        deleted = self._decode_journal_ids(
            row["deleted_delta_ids_json"], "deleted_delta_ids"
        )
        retained = self._decode_journal_ids(
            row["retained_delta_ids_json"], "retained_delta_ids"
        )
        journal_bytes_before = self._stored_nonnegative_int(
            row["bytes_before"], "bytes_before"
        )
        if (
            row["source_id"] != plan.source_id
            or row["stream_epoch"] != plan.stream_epoch
            or row["deleted_count"] != len(plan.delete_delta_ids)
            or deleted != plan.delete_delta_ids
            or retained != plan.retained_delta_ids
            or row["reclamation_complete"] not in (0, 1)
        ):
            raise CollectorRetentionError(
                "collector compaction journal conflicts with the requested plan"
            )
        _instant(row["compacted_at"], "compacted_at")
        return (
            row["compacted_at"],
            bool(row["reclamation_complete"]),
            journal_bytes_before,
        )

    def _reclaim_pages(self) -> None:
        connection = self.collector._connect()
        try:
            connection.execute("VACUUM")
        finally:
            connection.close()

    def _mark_reclamation_complete(self, plan_id: str) -> None:
        connection = self.collector._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE {self._JOURNAL_TABLE} SET reclamation_complete=1 "
                "WHERE plan_id=?",
                (plan_id,),
            )
            if cursor.rowcount != 1:
                raise CollectorRetentionError(
                    "collector compaction terminal journal disappeared"
                )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
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
        recovered_existing_journal = False
        terminal_compacted_at = compacted_at
        reclamation_complete = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._existing_terminal(connection, plan)
            if existing is not None:
                (
                    terminal_compacted_at,
                    reclamation_complete,
                    bytes_before,
                ) = existing
                recovered_existing_journal = True
                connection.commit()
            else:
                refreshed = self._build_plan(
                    connection,
                    source_id=plan.source_id,
                    stream_epoch=plan.stream_epoch,
                    desktop_checkpoint=checkpoint,
                )
                if refreshed != plan:
                    raise CollectorRetentionPlanStaleError(
                        "collector retention plan is stale; preview again before compaction"
                    )
                deleted = plan.delete_delta_ids
                if deleted:
                    placeholders = ",".join("?" for _ in deleted)
                    connection.execute(
                        "INSERT INTO collector_delta_tombstones_v1("
                        "delta_id, source_id, stream_epoch, payload_sha256, "
                        "compacted_at, plan_id"
                        ") SELECT delta_id, source_id, stream_epoch, payload_sha256, ?, ? "
                        f"FROM collector_deltas WHERE delta_id IN ({placeholders})",
                        (compacted_at, plan.plan_id, *deleted),
                    )
                    tombstone_count = connection.execute(
                        "SELECT COUNT(*) FROM collector_delta_tombstones_v1 "
                        f"WHERE plan_id=? AND delta_id IN ({placeholders})",
                        (plan.plan_id, *deleted),
                    ).fetchone()[0]
                    if tombstone_count != len(deleted):
                        raise CollectorRetentionPlanStaleError(
                            "collector retention tombstones did not bind every deleted identity"
                        )
                    cursor = connection.execute(
                        f"DELETE FROM collector_deltas WHERE delta_id IN ({placeholders})",
                        deleted,
                    )
                    if cursor.rowcount != len(deleted):
                        raise CollectorRetentionPlanStaleError(
                            "collector retention rows changed during compaction"
                        )
                reclamation_complete = not bool(deleted)
                connection.execute(
                    f"INSERT INTO {self._JOURNAL_TABLE}("
                    "plan_id, source_id, stream_epoch, compacted_at, bytes_before, "
                    "deleted_count, deleted_delta_ids_json, retained_delta_ids_json, "
                    "reclamation_complete"
                    ") VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        plan.plan_id,
                        plan.source_id,
                        plan.stream_epoch,
                        compacted_at,
                        bytes_before,
                        len(deleted),
                        json.dumps(list(deleted), separators=(",", ":")),
                        json.dumps(list(plan.retained_delta_ids), separators=(",", ":")),
                        int(reclamation_complete),
                    ),
                )
                connection.commit()
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise CollectorRetentionError(
                "collector compaction semantic transition did not commit"
            ) from exc
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

        # The journaled delete is the terminal semantic transition. Page reclamation
        # is maintenance: failure leaves a durable retryable journal state instead of
        # converting an already-committed delete into an ambiguous operation failure.
        if plan.delete_delta_ids and not reclamation_complete:
            try:
                self._reclaim_pages()
                self._mark_reclamation_complete(plan.plan_id)
            except sqlite3.DatabaseError:
                reclamation_complete = False
            else:
                reclamation_complete = True

        # Reopen through canonical verification after either terminal path. A retry
        # after process death finds the exact journal first and can finish VACUUM
        # without rebuilding a now-impossible pre-delete plan.
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
            deleted_delta_ids=plan.delete_delta_ids,
            retained_delta_ids=plan.retained_delta_ids,
            bytes_before=bytes_before,
            bytes_after=bytes_after,
            compacted_at=terminal_compacted_at,
            recovered_existing_journal=recovered_existing_journal,
            reclamation_complete=reclamation_complete,
        )

    def compaction_journal(self) -> tuple[dict[str, object], ...]:
        connection = self.collector._connect()
        try:
            rows = connection.execute(
                f"SELECT plan_id, source_id, stream_epoch, compacted_at, bytes_before, "
                f"deleted_count, deleted_delta_ids_json, retained_delta_ids_json, "
                f"reclamation_complete "
                f"FROM {self._JOURNAL_TABLE} ORDER BY rowid"
            ).fetchall()
            return tuple(
                {
                    "plan_id": row["plan_id"],
                    "source_id": row["source_id"],
                    "stream_epoch": row["stream_epoch"],
                    "compacted_at": row["compacted_at"],
                    "bytes_before": self._stored_nonnegative_int(
                        row["bytes_before"], "bytes_before"
                    ),
                    "deleted_count": row["deleted_count"],
                    "deleted_delta_ids": self._decode_journal_ids(
                        row["deleted_delta_ids_json"], "deleted_delta_ids"
                    ),
                    "retained_delta_ids": self._decode_journal_ids(
                        row["retained_delta_ids_json"], "retained_delta_ids"
                    ),
                    "reclamation_complete": bool(row["reclamation_complete"]),
                }
                for row in rows
            )
        except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
            raise CollectorRetentionError(
                "collector compaction journal is invalid"
            ) from exc
        finally:
            connection.close()
