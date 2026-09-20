from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .causal_collector_legacy import (
    CollectorDelta,
    CursorRegressionError,
    DeltaConflictError,
    StreamCheckpoint,
    _text,
)
from .collector_sqlite_store import (
    CollectorDeltaStore as _SQLiteCollectorDeltaStore,
    _canonical_delta_json,
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


class CollectorDeltaStore(_SQLiteCollectorDeltaStore):
    """Canonical indexed SQLite store with bounded product-compatible durability."""

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
            # New-epoch admission must prove coverage for every retained epoch,
            # not merely validate whichever checkpoint rows survived corruption.
            # These indexed probes stop on the first missing/orphaned epoch.
            history_without_checkpoint = connection.execute(
                "SELECT 1 FROM collector_deltas AS d "
                "LEFT JOIN collector_streams AS s "
                "ON s.source_id=d.source_id AND s.stream_epoch=d.stream_epoch "
                "WHERE d.source_id=? AND s.source_id IS NULL LIMIT 1",
                (source_id,),
            ).fetchone()
            checkpoint_without_history = connection.execute(
                "SELECT 1 FROM collector_streams AS s "
                "WHERE s.source_id=? AND NOT EXISTS ("
                "SELECT 1 FROM collector_deltas AS d "
                "WHERE d.source_id=s.source_id AND d.stream_epoch=s.stream_epoch "
                "LIMIT 1) LIMIT 1",
                (source_id,),
            ).fetchone()
            if (
                history_without_checkpoint is not None
                or checkpoint_without_history is not None
            ):
                raise ValueError(
                    "collector stream checkpoint conflicts with immutable delta history"
                )

            source_checkpoints = connection.execute(
                "SELECT stream_epoch FROM collector_streams "
                "WHERE source_id=? ORDER BY stream_epoch",
                (source_id,),
            ).fetchall()
            # Set coverage above proves one row exists for each retained historical
            # epoch (the table PK proves at most one). Validate every terminal
            # cursor/delta binding before a fresh epoch may be admitted.
            for checkpoint_row in source_checkpoints:
                cls._verified_stream_checkpoint(
                    connection,
                    source_id,
                    checkpoint_row["stream_epoch"],
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
        existing = connection.execute(
            f"SELECT {_DELTA_SELECT_COLUMNS} FROM collector_deltas WHERE delta_id=?",
            (delta.delta_id,),
        ).fetchone()
        if existing is not None:
            existing_delta = cls._row_delta(existing)
            if _canonical_delta_json(existing_delta) != _canonical_delta_json(delta):
                raise DeltaConflictError(
                    f"delta {delta.delta_id} conflicts with immutable evidence"
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
