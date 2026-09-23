from __future__ import annotations

import heapq
import sqlite3
from pathlib import Path
from typing import Any

from .causal_collector_legacy import (
    CollectorDelta,
    CursorRegressionError,
    DeltaConflictError,
    GapState,
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
_COMMIT_ORDER_INTEGRITY_META_KEY = "commit_order_integrity_v1"
_COMMIT_ORDER_UNVERIFIED_SOURCE_PREFIX = "commit_order_unverified_source_v1:"
_PROJECTION_IMMUTABILITY_TRIGGER = "collector_deltas_projection_immutable_v1"
_COMMIT_ORDER_UNVERIFIED_ERROR = (
    "collector durable commit order is not independently verified for this source"
)
_PROJECTION_IMMUTABILITY_ERROR = "collector delta indexed projections are immutable"
_INDEXED_PROJECTION_FIELDS = (
    "delta_id",
    "source_id",
    "stream_epoch",
    "cursor_position",
    "revision_number",
    "desktop_available_at",
    "collector_committed_at",
)
_IMMUTABLE_INDEX_FIELDS = ("commit_seq", *_INDEXED_PROJECTION_FIELDS)
_PREDECESSOR_PROJECTION_IMMUTABILITY_TRIGGER_SQL = (
    f"CREATE TRIGGER {_PROJECTION_IMMUTABILITY_TRIGGER} BEFORE UPDATE OF "
    + ", ".join(_INDEXED_PROJECTION_FIELDS)
    + " ON collector_deltas BEGIN "
    + f"SELECT RAISE(ABORT, '{_PROJECTION_IMMUTABILITY_ERROR}'); END"
)
_PROJECTION_IMMUTABILITY_TRIGGER_SQL = (
    f"CREATE TRIGGER {_PROJECTION_IMMUTABILITY_TRIGGER} BEFORE UPDATE OF "
    + ", ".join(_IMMUTABLE_INDEX_FIELDS)
    + " ON collector_deltas BEGIN "
    + f"SELECT RAISE(ABORT, '{_PROJECTION_IMMUTABILITY_ERROR}'); END"
)
_DELTA_SELECT_COLUMNS = ", ".join(
    ("commit_seq", *_INDEXED_PROJECTION_FIELDS, "payload_sha256", "payload_json")
)


def _normalized_trigger_sql(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.strip().rstrip(";").split())


def _matches_projection_trigger_sql(value: object, expected_sql: str) -> bool:
    normalized = _normalized_trigger_sql(value)
    expected = _normalized_trigger_sql(expected_sql)
    assert expected is not None
    legacy_expected = expected.replace(
        "CREATE TRIGGER ",
        "CREATE TRIGGER IF NOT EXISTS ",
        1,
    )
    return normalized in {expected, legacy_expected}


def _is_canonical_projection_trigger(value: object) -> bool:
    return _matches_projection_trigger_sql(
        value,
        _PROJECTION_IMMUTABILITY_TRIGGER_SQL,
    )


def _is_predecessor_projection_trigger(value: object) -> bool:
    return _matches_projection_trigger_sql(
        value,
        _PREDECESSOR_PROJECTION_IMMUTABILITY_TRIGGER_SQL,
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

    @classmethod
    def _reconcile_predecessor_commit_order(
        cls,
        rows: list[sqlite3.Row],
    ) -> set[str]:
        """Return sources whose legacy commit order cannot be uniquely reconstructed.

        The predecessor guard authenticated payload-derived projections but did not
        protect commit_seq. A legacy sequence is promoted only when retained payload
        relationships force one per-source append order and the observed sequence is
        that order. Ambiguity remains explicit instead of becoming authority.
        """

        by_source: dict[str, list[tuple[int, CollectorDelta]]] = {}
        for row in rows:
            commit_seq = row["commit_seq"]
            if (
                isinstance(commit_seq, bool)
                or not isinstance(commit_seq, int)
                or commit_seq <= 0
            ):
                raise ValueError("collector commit order contains an invalid sequence")
            delta = cls._row_delta(row)
            by_source.setdefault(delta.source_id, []).append((commit_seq, delta))

        unverified_sources: set[str] = set()
        for source_id, entries in by_source.items():
            entries.sort(key=lambda item: item[0])
            observed_ids = [delta.delta_id for _, delta in entries]
            positions = {
                delta_id: index for index, delta_id in enumerate(observed_ids)
            }
            deltas = {delta.delta_id: delta for _, delta in entries}
            if len(deltas) != len(entries):
                raise ValueError("collector commit order contains duplicate delta identity")

            adjacency: dict[str, set[str]] = {
                delta_id: set() for delta_id in observed_ids
            }
            indegree = {delta_id: 0 for delta_id in observed_ids}

            def add_edge(before: str, after: str) -> None:
                if after not in adjacency[before]:
                    adjacency[before].add(after)
                    indegree[after] += 1

            by_epoch: dict[str, list[CollectorDelta]] = {}
            for delta in deltas.values():
                by_epoch.setdefault(delta.stream_epoch, []).append(delta)

            for epoch_deltas in by_epoch.values():
                bases = sorted(
                    (
                        delta
                        for delta in epoch_deltas
                        if delta.revision_of is None
                    ),
                    key=lambda delta: delta.cursor_position,
                )
                base_positions = [delta.cursor_position for delta in bases]
                if len(base_positions) != len(set(base_positions)):
                    raise ValueError(
                        "collector commit order conflicts with immutable causal history"
                    )
                for before, after in zip(bases, bases[1:]):
                    add_edge(before.delta_id, after.delta_id)

            for delta in deltas.values():
                if delta.revision_of is None:
                    continue
                predecessor = deltas.get(delta.revision_of)
                if predecessor is None:
                    # A retained correction can outlive a compacted predecessor.
                    # Without that predecessor's order evidence this source remains
                    # usable for payload/causal reads but not delivery-order cursors.
                    unverified_sources.add(source_id)
                    continue
                if (
                    predecessor.source_id != delta.source_id
                    or predecessor.stream_epoch != delta.stream_epoch
                    or predecessor.event_dedupe_key != delta.event_dedupe_key
                    or predecessor.event_id != delta.event_id
                    or predecessor.cursor_position != delta.cursor_position
                    or predecessor.source_cursor != delta.source_cursor
                    or delta.revision_number != predecessor.revision_number + 1
                    or delta.gap_from_cursor != predecessor.gap_from_cursor
                    or delta.gap_to_cursor != predecessor.gap_to_cursor
                ):
                    raise ValueError(
                        "collector commit order conflicts with immutable causal history"
                    )
                if predecessor.gap_state is GapState.DETECTED:
                    if delta.gap_state is not GapState.RECOVERED:
                        raise ValueError(
                            "collector commit order conflicts with immutable causal history"
                        )
                elif delta.gap_state is GapState.RECOVERED:
                    raise ValueError(
                        "collector commit order conflicts with immutable causal history"
                    )
                add_edge(predecessor.delta_id, delta.delta_id)

            for before, successors in adjacency.items():
                for after in successors:
                    if positions[before] >= positions[after]:
                        raise ValueError(
                            "collector commit order conflicts with immutable causal history"
                        )

            ready = [
                (positions[delta_id], delta_id)
                for delta_id, degree in indegree.items()
                if degree == 0
            ]
            heapq.heapify(ready)
            processed = 0
            unique = source_id not in unverified_sources
            while ready:
                if len(ready) != 1:
                    unique = False
                _, current = heapq.heappop(ready)
                processed += 1
                for successor in adjacency[current]:
                    indegree[successor] -= 1
                    if indegree[successor] == 0:
                        heapq.heappush(
                            ready,
                            (positions[successor], successor),
                        )

            if processed != len(entries):
                raise ValueError(
                    "collector commit order conflicts with immutable causal history"
                )
            if not unique:
                unverified_sources.add(source_id)

        return unverified_sources

    @staticmethod
    def _require_verified_commit_order(
        connection: sqlite3.Connection,
        source_id: str,
    ) -> None:
        marker = connection.execute(
            "SELECT value FROM collector_meta WHERE key=?",
            (_COMMIT_ORDER_INTEGRITY_META_KEY,),
        ).fetchone()
        if marker is None or marker[0] != "1":
            raise ValueError(_COMMIT_ORDER_UNVERIFIED_ERROR)
        unverified = connection.execute(
            "SELECT value FROM collector_meta WHERE key=?",
            (_COMMIT_ORDER_UNVERIFIED_SOURCE_PREFIX + source_id,),
        ).fetchone()
        if unverified is not None:
            if unverified[0] != "1":
                raise ValueError("invalid collector commit-order integrity metadata")
            raise ValueError(_COMMIT_ORDER_UNVERIFIED_ERROR)

    def _ensure_projection_integrity_guard(self) -> None:
        """Reconcile projections and establish explicit commit-order authority."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
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
            order_marker = connection.execute(
                "SELECT value FROM collector_meta WHERE key=?",
                (_COMMIT_ORDER_INTEGRITY_META_KEY,),
            ).fetchone()
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (_PROJECTION_IMMUTABILITY_TRIGGER,),
            ).fetchone()

            if order_marker is not None and order_marker[0] != "1":
                raise ValueError("invalid collector commit-order integrity metadata")

            needs_order_reconciliation = order_marker is None
            if marker is None:
                if order_marker is not None:
                    raise ValueError(
                        "collector commit-order integrity metadata lacks projection authority"
                    )
                rows = connection.execute(
                    f"SELECT {_DELTA_SELECT_COLUMNS} "
                    "FROM collector_deltas ORDER BY commit_seq"
                ).fetchall()
                for row in rows:
                    self._row_delta(row)
                unverified_sources = self._reconcile_predecessor_commit_order(rows)

                # Without the projection marker, a same-name trigger has no product
                # authority. Reconcile first, then replace it and publish both
                # projection and commit-order migration metadata atomically.
                if trigger is not None:
                    connection.execute(
                        f"DROP TRIGGER {_PROJECTION_IMMUTABILITY_TRIGGER}"
                    )
                connection.execute(_PROJECTION_IMMUTABILITY_TRIGGER_SQL)
                connection.execute(
                    "INSERT INTO collector_meta(key, value) VALUES(?, '1')",
                    (_PROJECTION_INTEGRITY_META_KEY,),
                )
                for source_id in sorted(unverified_sources):
                    connection.execute(
                        "INSERT INTO collector_meta(key, value) VALUES(?, '1')",
                        (_COMMIT_ORDER_UNVERIFIED_SOURCE_PREFIX + source_id,),
                    )
                connection.execute(
                    "INSERT INTO collector_meta(key, value) VALUES(?, '1')",
                    (_COMMIT_ORDER_INTEGRITY_META_KEY,),
                )
            elif marker[0] != "1" or trigger is None:
                raise ValueError(
                    "collector indexed projection integrity guard is missing or noncanonical"
                )
            elif not _is_canonical_projection_trigger(trigger[0]):
                if not _is_predecessor_projection_trigger(trigger[0]):
                    raise ValueError(
                        "collector indexed projection integrity guard is missing or noncanonical"
                    )
                if order_marker is not None:
                    raise ValueError(
                        "collector commit-order metadata conflicts with predecessor guard"
                    )

                rows = connection.execute(
                    f"SELECT {_DELTA_SELECT_COLUMNS} "
                    "FROM collector_deltas ORDER BY commit_seq"
                ).fetchall()
                for row in rows:
                    self._row_delta(row)
                unverified_sources = self._reconcile_predecessor_commit_order(rows)

                # The predecessor marker proves only payload-derived projections.
                # Reconstruct what can be proven about per-source append order before
                # installing the stronger commit_seq guard. Ambiguous sources stay
                # explicitly unverified rather than inheriting false authority.
                connection.execute(
                    f"DROP TRIGGER {_PROJECTION_IMMUTABILITY_TRIGGER}"
                )
                connection.execute(_PROJECTION_IMMUTABILITY_TRIGGER_SQL)
                for source_id in sorted(unverified_sources):
                    connection.execute(
                        "INSERT INTO collector_meta(key, value) VALUES(?, '1')",
                        (_COMMIT_ORDER_UNVERIFIED_SOURCE_PREFIX + source_id,),
                    )
                connection.execute(
                    "INSERT INTO collector_meta(key, value) VALUES(?, '1')",
                    (_COMMIT_ORDER_INTEGRITY_META_KEY,),
                )
            elif needs_order_reconciliation:
                # A branch predecessor may already have installed the stronger
                # trigger without recording whether legacy commit order was provable.
                # The trigger freezes the current rows, so classify exactly once.
                rows = connection.execute(
                    f"SELECT {_DELTA_SELECT_COLUMNS} "
                    "FROM collector_deltas ORDER BY commit_seq"
                ).fetchall()
                for row in rows:
                    self._row_delta(row)
                unverified_sources = self._reconcile_predecessor_commit_order(rows)
                for source_id in sorted(unverified_sources):
                    connection.execute(
                        "INSERT INTO collector_meta(key, value) VALUES(?, '1')",
                        (_COMMIT_ORDER_UNVERIFIED_SOURCE_PREFIX + source_id,),
                    )
                connection.execute(
                    "INSERT INTO collector_meta(key, value) VALUES(?, '1')",
                    (_COMMIT_ORDER_INTEGRITY_META_KEY,),
                )
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

            self._require_verified_commit_order(connection, source_id)
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
                self._require_verified_commit_order(connection, delta.source_id)
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
            self._require_verified_commit_order(connection, source_id)
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
