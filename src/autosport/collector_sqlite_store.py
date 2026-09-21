from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from .causal_collector_legacy import (
    CausalView,
    CollectorDelta,
    CursorRegressionError,
    DeltaConflictError,
    GapState,
    GapStateError,
    StreamCheckpoint,
    SyncState,
    _instant,
    _text,
)
from .workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockBusyError


_SQLITE_HEADER = b"SQLite format 3\x00"
_DB_SCHEMA_VERSION = 1


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _canonical_delta_json(delta: CollectorDelta) -> str:
    return json.dumps(
        delta.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _payload_digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decode_delta(payload: str, expected_digest: str) -> CollectorDelta:
    if _payload_digest(payload) != expected_digest:
        raise ValueError("collector delta payload integrity check failed")
    try:
        raw = json.loads(
            payload,
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid durable collector delta payload") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("invalid durable collector delta payload")
    return CollectorDelta.from_dict(raw)


def _fsync_file(path: Path) -> None:
    # Windows rejects fsync() on a CRT descriptor opened read-only. The migration
    # candidate is our own writable temporary SQLite file, so use read/write here.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_parent(path: Path) -> None:
    try:
        descriptor = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class CollectorDeltaStore:
    """Indexed durable collector archive preserving the historical public contract.

    The path remains the sole canonical authority.  A pre-existing schema-v1 JSON
    store is validated and migrated once, atomically, to SQLite at that exact path;
    an immutable ``.legacy-v1.json`` copy is retained as migration evidence.  Normal
    startup verifies only schema metadata, so restart cost is independent of retained
    delta count.  Append admission and stream-checkpoint mutation share one
    ``BEGIN IMMEDIATE`` transaction, while a monotonic SQLite row id independently
    preserves desktop delivery/commit order for late corrections.
    """

    schema_version = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                prefix = self.path.read_bytes()[: len(_SQLITE_HEADER)]
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

    @staticmethod
    def _connect_path(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(
            path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _connect(self) -> sqlite3.Connection:
        return self._connect_path(self.path)

    @classmethod
    def _initialize_sqlite(cls, path: Path, *, wal: bool) -> None:
        connection = cls._connect_path(path)
        try:
            mode = "WAL" if wal else "DELETE"
            selected = connection.execute(f"PRAGMA journal_mode={mode}").fetchone()[0]
            if str(selected).lower() != mode.lower():
                raise ValueError("collector SQLite journal mode could not be established")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS collector_meta ("
                "key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS collector_deltas ("
                "commit_seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                "delta_id TEXT NOT NULL UNIQUE,"
                "source_id TEXT NOT NULL,"
                "stream_epoch TEXT NOT NULL,"
                "cursor_position INTEGER NOT NULL CHECK(cursor_position >= 0),"
                "revision_number INTEGER NOT NULL CHECK(revision_number >= 0),"
                "desktop_available_at TEXT NOT NULL,"
                "collector_committed_at TEXT NOT NULL,"
                "payload_sha256 TEXT NOT NULL,"
                "payload_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS collector_deltas_source_commit "
                "ON collector_deltas(source_id, commit_seq)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS collector_deltas_causal "
                "ON collector_deltas(source_id, stream_epoch, cursor_position, revision_number, commit_seq)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS collector_streams ("
                "source_id TEXT NOT NULL,"
                "stream_epoch TEXT NOT NULL,"
                "last_cursor TEXT NOT NULL,"
                "last_position INTEGER NOT NULL CHECK(last_position >= 0),"
                "last_delta_id TEXT NOT NULL,"
                "PRIMARY KEY(source_id, stream_epoch))"
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
            connection.execute(
                "INSERT OR REPLACE INTO collector_meta(key, value) VALUES('schema_version', ?)",
                (str(_DB_SCHEMA_VERSION),),
            )
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _activate_wal(self) -> None:
        connection = self._connect()
        try:
            selected = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(selected).lower() != "wal":
                raise ValueError("collector SQLite WAL mode could not be established")
            connection.execute("PRAGMA synchronous=FULL")
        except sqlite3.DatabaseError as exc:
            raise ValueError("invalid causal collector store") from exc
        finally:
            connection.close()

    def _verify_sqlite_schema(self) -> None:
        connection = self._connect()
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != _DB_SCHEMA_VERSION:
                raise ValueError("unsupported causal collector store schema")
            meta = connection.execute(
                "SELECT value FROM collector_meta WHERE key='schema_version'"
            ).fetchone()
            if meta is None or meta[0] != str(_DB_SCHEMA_VERSION):
                raise ValueError("unsupported causal collector store schema")
            expected_tables = {"collector_meta", "collector_deltas", "collector_streams"}
            observed = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not expected_tables.issubset(observed):
                raise ValueError("unsupported causal collector store schema")
            delta_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(collector_deltas)")
            }
            required_delta_columns = {
                "commit_seq",
                "delta_id",
                "source_id",
                "stream_epoch",
                "cursor_position",
                "revision_number",
                "desktop_available_at",
                "collector_committed_at",
                "payload_sha256",
                "payload_json",
            }
            if delta_columns != required_delta_columns:
                raise ValueError("unsupported causal collector store schema")
        except sqlite3.DatabaseError as exc:
            raise ValueError("invalid causal collector store") from exc
        finally:
            connection.close()

    def _load_legacy(self) -> tuple[bytes, dict[str, Any]]:
        try:
            source_bytes = self.path.read_bytes()
            raw = json.loads(
                source_bytes.decode("utf-8"),
                object_pairs_hook=_pairs,
                parse_constant=_constant,
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid causal collector store") from exc
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != 1
            or not isinstance(raw.get("deltas"), list)
            or not isinstance(raw.get("streams"), dict)
        ):
            raise ValueError("unsupported causal collector store schema")
        return source_bytes, raw

    def _migration_candidate_ready(self) -> None:
        """Private pre-switch hook used by deterministic crash/race falsifiers."""

    def _migrate_legacy_json(self) -> None:
        source_bytes, legacy = self._load_legacy()
        temp = self.path.with_name(
            f".{self.path.name}.sqlite-migrate-{os.getpid()}-{uuid.uuid4().hex}.tmp"
        )
        backup = self.path.with_name(f"{self.path.name}.legacy-v1.json")
        lock_workspace = self.path.parent / f".{self.path.name}.migration-lock"
        try:
            self._initialize_sqlite(temp, wal=False)
            connection = self._connect_path(temp)
            try:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                for item in legacy["deltas"]:
                    if not isinstance(item, Mapping):
                        raise ValueError("invalid causal collector store")
                    self._append_connection(
                        connection,
                        CollectorDelta.from_dict(item),
                    )
                derived = self._stream_dict(connection)
                expected = self._validated_legacy_streams(legacy["streams"])
                if derived != expected:
                    raise ValueError(
                        "legacy collector stream checkpoints conflict with replayed deltas"
                    )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()

            _fsync_file(temp)
            self._migration_candidate_ready()

            # Only the authority switch is serialized. Candidate construction is kept
            # outside this crash-releasing lock so normal first-open latency does not
            # hold a workspace-wide writer boundary.
            migration_lock = WorkspaceEconomicLock(lock_workspace)
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    migration_lock.acquire()
                    break
                except WorkspaceEconomicLockBusyError as exc:
                    if time.monotonic() >= deadline:
                        raise ValueError(
                            "collector legacy migration is busy in another process"
                        ) from exc
                    time.sleep(0.01)
            try:
                # Re-read the active path only after winning the lock. A delayed
                # first-opener must never replace a winner's migrated-and-appended
                # SQLite authority with its stale candidate.
                try:
                    with self.path.open("rb") as handle:
                        prefix = handle.read(len(_SQLITE_HEADER))
                except OSError as exc:
                    raise ValueError("invalid causal collector store") from exc

                if prefix == _SQLITE_HEADER:
                    try:
                        preserved = backup.read_bytes()
                    except OSError as exc:
                        raise ValueError(
                            "legacy collector migration evidence is unavailable"
                        ) from exc
                    if preserved != source_bytes:
                        raise ValueError(
                            "legacy collector migration source identity conflicts"
                        )
                    self._verify_sqlite_schema()
                    return

                active_source_bytes, _ = self._load_legacy()
                if active_source_bytes != source_bytes:
                    raise ValueError(
                        "legacy collector source changed during migration"
                    )

                if backup.exists():
                    try:
                        if backup.read_bytes() != source_bytes:
                            raise ValueError(
                                "legacy collector migration backup conflicts"
                            )
                    except OSError as exc:
                        raise ValueError(
                            "legacy collector migration backup is unreadable"
                        ) from exc
                else:
                    try:
                        with backup.open("xb") as handle:
                            handle.write(source_bytes)
                            handle.flush()
                            os.fsync(handle.fileno())
                    except OSError as exc:
                        raise ValueError(
                            "cannot preserve legacy collector migration evidence"
                        ) from exc
                    _fsync_parent(backup)

                os.replace(temp, self.path)
                _fsync_parent(self.path)
            finally:
                migration_lock.release()
        finally:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass

    @staticmethod
    def _validated_legacy_streams(raw_streams: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for key, raw in raw_streams.items():
            if not isinstance(key, str) or not isinstance(raw, Mapping):
                raise ValueError("invalid causal collector stream checkpoint")
            checkpoint = StreamCheckpoint(**dict(raw))
            checkpoint.validate()
            expected_key = f"{checkpoint.source_id}|{checkpoint.stream_epoch}"
            if key != expected_key:
                raise ValueError("legacy collector stream checkpoint key conflicts")
            result[key] = asdict(checkpoint)
        return result

    @staticmethod
    def _stream_dict(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        rows = connection.execute(
            "SELECT source_id, stream_epoch, last_cursor, last_position, last_delta_id "
            "FROM collector_streams"
        ).fetchall()
        for row in rows:
            checkpoint = StreamCheckpoint(
                source_id=row["source_id"],
                stream_epoch=row["stream_epoch"],
                last_cursor=row["last_cursor"],
                last_position=row["last_position"],
                last_delta_id=row["last_delta_id"],
            )
            checkpoint.validate()
            result[f"{checkpoint.source_id}|{checkpoint.stream_epoch}"] = asdict(checkpoint)
        return result

    @staticmethod
    def _row_delta(row: sqlite3.Row) -> CollectorDelta:
        return _decode_delta(row["payload_json"], row["payload_sha256"])

    @classmethod
    def _delta_by_id(
        cls,
        connection: sqlite3.Connection,
        delta_id: str,
    ) -> CollectorDelta | None:
        row = connection.execute(
            "SELECT payload_sha256, payload_json FROM collector_deltas WHERE delta_id=?",
            (delta_id,),
        ).fetchone()
        return None if row is None else cls._row_delta(row)

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
            "SELECT payload_sha256, payload_json FROM collector_deltas WHERE delta_id=?",
            (delta.delta_id,),
        ).fetchone()
        if existing is not None:
            existing_delta = cls._row_delta(existing)
            if _canonical_delta_json(existing_delta) != encoded:
                raise DeltaConflictError(
                    f"delta {delta.delta_id} conflicts with immutable evidence"
                )
            return False

        prior_epochs = {
            row[0]
            for row in connection.execute(
                "SELECT stream_epoch FROM collector_streams WHERE source_id=?",
                (delta.source_id,),
            ).fetchall()
        }
        if prior_epochs and delta.stream_epoch not in prior_epochs and delta.sync_state not in {
            SyncState.EPOCH_CHANGED,
            SyncState.CURSOR_RESET,
        }:
            raise CursorRegressionError(
                "new stream epoch requires explicit epoch-change/reset state"
            )

        previous_row = connection.execute(
            "SELECT last_cursor, last_position, last_delta_id "
            "FROM collector_streams WHERE source_id=? AND stream_epoch=?",
            (delta.source_id, delta.stream_epoch),
        ).fetchone()
        previous = None
        if previous_row is not None:
            previous = StreamCheckpoint(
                source_id=delta.source_id,
                stream_epoch=delta.stream_epoch,
                last_cursor=previous_row["last_cursor"],
                last_position=previous_row["last_position"],
                last_delta_id=previous_row["last_delta_id"],
            )
            previous.validate()

        if delta.revision_of is not None:
            predecessor = cls._delta_by_id(connection, delta.revision_of)
            if predecessor is None:
                raise CursorRegressionError(
                    "revision must target an existing predecessor"
                )
            if predecessor.source_id != delta.source_id:
                raise CursorRegressionError(
                    "revision source_id does not match predecessor"
                )
            if predecessor.stream_epoch != delta.stream_epoch:
                raise CursorRegressionError(
                    "revision stream_epoch does not match predecessor"
                )
            if predecessor.event_dedupe_key != delta.event_dedupe_key:
                raise CursorRegressionError(
                    "revision event_dedupe_key does not match predecessor"
                )
            if predecessor.event_id != delta.event_id:
                raise CursorRegressionError(
                    "revision event_id does not match predecessor"
                )
            if predecessor.cursor_position != delta.cursor_position:
                raise CursorRegressionError(
                    "revision cursor_position does not match predecessor"
                )
            if predecessor.source_cursor != delta.source_cursor:
                raise CursorRegressionError(
                    "revision source_cursor does not match predecessor"
                )
            if delta.revision_number != predecessor.revision_number + 1:
                raise CursorRegressionError(
                    "revision_number must advance exactly one step"
                )
            if (
                delta.gap_from_cursor != predecessor.gap_from_cursor
                or delta.gap_to_cursor != predecessor.gap_to_cursor
            ):
                raise GapStateError("revision gap bounds must match predecessor")
            if predecessor.gap_state is GapState.DETECTED:
                if delta.gap_state is not GapState.RECOVERED:
                    raise GapStateError(
                        "detected gap can only be revised by a recovered marker"
                    )
            elif delta.gap_state is GapState.RECOVERED:
                raise GapStateError(
                    "recovered marker must revise a detected gap"
                )

        if previous is not None:
            if delta.cursor_position < previous.last_position and delta.revision_of is None:
                raise CursorRegressionError(
                    "source cursor moved backwards within one epoch"
                )
            if delta.cursor_position == previous.last_position and delta.revision_of is None:
                raise CursorRegressionError(
                    "equal cursor position requires an explicit revision relationship"
                )

        connection.execute(
            "INSERT INTO collector_deltas("
            "delta_id, source_id, stream_epoch, cursor_position, revision_number, "
            "desktop_available_at, collector_committed_at, payload_sha256, payload_json"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                delta.delta_id,
                delta.source_id,
                delta.stream_epoch,
                delta.cursor_position,
                delta.revision_number,
                delta.desktop_available_at,
                delta.collector_committed_at,
                digest,
                encoded,
            ),
        )
        if previous is None or delta.cursor_position > previous.last_position:
            connection.execute(
                "INSERT INTO collector_streams("
                "source_id, stream_epoch, last_cursor, last_position, last_delta_id"
                ") VALUES(?,?,?,?,?) "
                "ON CONFLICT(source_id, stream_epoch) DO UPDATE SET "
                "last_cursor=excluded.last_cursor, "
                "last_position=excluded.last_position, "
                "last_delta_id=excluded.last_delta_id",
                (
                    delta.source_id,
                    delta.stream_epoch,
                    delta.source_cursor,
                    delta.cursor_position,
                    delta.delta_id,
                ),
            )
        return True

    def get(self, delta_id: str) -> CollectorDelta | None:
        _text(delta_id, "delta_id")
        connection = self._connect()
        try:
            return self._delta_by_id(connection, delta_id)
        except sqlite3.DatabaseError as exc:
            raise ValueError("invalid causal collector store") from exc
        finally:
            connection.close()

    def _all(self) -> list[CollectorDelta]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT payload_sha256, payload_json FROM collector_deltas ORDER BY commit_seq"
            ).fetchall()
            return [self._row_delta(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            raise ValueError("invalid causal collector store") from exc
        finally:
            connection.close()

    def append(self, delta: CollectorDelta) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = self._append_connection(connection, delta)
            connection.commit()
            return changed
        except sqlite3.IntegrityError as exc:
            if connection.in_transaction:
                connection.rollback()
            raise DeltaConflictError("collector delta violates durable identity constraints") from exc
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

    def deltas_available_through(
        self,
        *,
        as_of: str,
        view: CausalView = CausalView.AS_KNOWN_AT_DECISION,
    ) -> tuple[CollectorDelta, ...]:
        boundary = _instant(as_of, "as_of")
        items = [
            delta
            for delta in self._all()
            if _instant(delta.desktop_available_at, "desktop_available_at") <= boundary
        ]
        try:
            normalized_view = CausalView(view)
        except ValueError as exc:
            raise ValueError("unsupported causal view") from exc
        ordered = tuple(
            sorted(
                items,
                key=lambda item: (
                    item.source_id,
                    item.stream_epoch,
                    item.cursor_position,
                    item.revision_number,
                    item.collector_committed_at,
                    item.delta_id,
                ),
            )
        )
        if normalized_view is CausalView.AS_KNOWN_AT_DECISION:
            return ordered
        superseded = {
            item.revision_of for item in ordered if item.revision_of is not None
        }
        return tuple(item for item in ordered if item.delta_id not in superseded)

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
                    "SELECT source_id, commit_seq FROM collector_deltas WHERE delta_id=?",
                    (after_delta_id,),
                ).fetchone()
                if anchor is None or anchor["source_id"] != source_id:
                    raise CursorRegressionError(
                        "delivery cursor delta is not present for this source"
                    )
                after_seq = anchor["commit_seq"]
            rows = connection.execute(
                "SELECT payload_sha256, payload_json FROM collector_deltas "
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
            row = connection.execute(
                "SELECT last_cursor, last_position, last_delta_id "
                "FROM collector_streams WHERE source_id=? AND stream_epoch=?",
                (source_id, stream_epoch),
            ).fetchone()
            if row is None:
                return None
            checkpoint = StreamCheckpoint(
                source_id=source_id,
                stream_epoch=stream_epoch,
                last_cursor=row["last_cursor"],
                last_position=row["last_position"],
                last_delta_id=row["last_delta_id"],
            )
            checkpoint.validate()
            return checkpoint
        except sqlite3.DatabaseError as exc:
            raise ValueError("invalid causal collector store") from exc
        finally:
            connection.close()
