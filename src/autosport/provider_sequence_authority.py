from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path


_SCHEMA_VERSION = 1
_SQLITE_SEQUENCE_MAX = (1 << 63) - 1


class ProviderSequenceAuthorityError(RuntimeError):
    """Durable provider acquisition ordering cannot be proven safely."""


def _canonical_identity(value: object, *, field: str, forbid_pipe: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    if "\x00" in value or any(ord(character) < 32 for character in value):
        raise ValueError(f"{field} must not contain control characters")
    if forbid_pipe and "|" in value:
        raise ValueError(f"{field} must not contain reserved delimiter '|'")
    return value


def _busy_timeout_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("busy_timeout_seconds must be a finite positive number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("busy_timeout_seconds must be a finite positive number")
    return parsed


class SQLiteProviderSequenceAuthority:
    """Restart-safe, cross-connection allocator for provider acquisition sequence.

    The authority is deliberately separate from provider adapters. Callers must
    bind an explicit stable authority_id and explicitly choose first creation
    versus reopening. Reopen never creates a missing database, preventing a
    missing state file from silently resetting provider sequence to one.

    Each allocation uses BEGIN IMMEDIATE against one SQLite row per canonical
    provider source_id. SQLite therefore serializes competing writers across
    threads/processes/connections before the previous value is read and advanced.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        authority_id: str,
        create: bool,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        if type(create) is not bool:
            raise TypeError("create must be bool")
        if isinstance(path, str) and path == ":memory:":
            raise ValueError("provider sequence authority must use a durable filesystem path")
        self.path = Path(path).expanduser().resolve(strict=False)
        self.authority_id = _canonical_identity(authority_id, field="authority_id")
        self.busy_timeout_seconds = _busy_timeout_seconds(busy_timeout_seconds)
        self._busy_timeout_ms = max(1, int(self.busy_timeout_seconds * 1000))

        parent = self.path.parent
        if not parent.exists() or not parent.is_dir():
            raise ValueError("provider sequence authority parent directory must exist")
        if self.path.exists() and self.path.is_dir():
            raise ValueError("provider sequence authority path must be a file")

        existed_before = self.path.exists()
        if create and not existed_before:
            self._initialize_new()
        else:
            self._validate_existing()

    def __call__(self, source_id: str) -> int:
        return self.allocate(source_id)

    def allocate(self, source_id: str) -> int:
        canonical_source = _canonical_identity(
            source_id,
            field="source_id",
            forbid_pipe=True,
        )
        connection = self._connect_existing()
        try:
            self._configure_connection(connection)
            connection.execute("BEGIN IMMEDIATE")
            self._require_wal_mode(connection)
            self._validate_schema_and_authority(connection)
            row = connection.execute(
                "SELECT last_sequence FROM provider_sequences_v1 WHERE source_id=?",
                (canonical_source,),
            ).fetchone()

            if row is None:
                next_sequence = 1
                connection.execute(
                    """INSERT INTO provider_sequences_v1(source_id,last_sequence)
                       VALUES (?,?)""",
                    (canonical_source, next_sequence),
                )
            else:
                if len(row) != 1 or type(row[0]) is not int:
                    raise ProviderSequenceAuthorityError(
                        "provider sequence row contains non-canonical integer state"
                    )
                previous = row[0]
                if previous <= 0 or previous > _SQLITE_SEQUENCE_MAX:
                    raise ProviderSequenceAuthorityError(
                        "provider sequence row is outside positive signed-64 range"
                    )
                if previous == _SQLITE_SEQUENCE_MAX:
                    raise ProviderSequenceAuthorityError(
                        "provider sequence authority exhausted signed-64 range"
                    )
                next_sequence = previous + 1
                cursor = connection.execute(
                    """UPDATE provider_sequences_v1
                       SET last_sequence=?
                       WHERE source_id=? AND last_sequence=?""",
                    (next_sequence, canonical_source, previous),
                )
                if cursor.rowcount != 1:
                    raise ProviderSequenceAuthorityError(
                        "provider sequence compare-and-advance failed"
                    )

            connection.commit()
            return next_sequence
        except ProviderSequenceAuthorityError:
            self._rollback_quietly(connection)
            raise
        except sqlite3.Error as exc:
            self._rollback_quietly(connection)
            raise ProviderSequenceAuthorityError(
                "provider sequence SQLite authority unavailable"
            ) from exc
        except Exception:
            self._rollback_quietly(connection)
            raise
        finally:
            connection.close()

    def _initialize_new(self) -> None:
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR,
                0o600,
            )
        except FileExistsError as exc:
            raise ProviderSequenceAuthorityError(
                "provider sequence authority appeared during exclusive creation"
            ) from exc
        except OSError as exc:
            raise ProviderSequenceAuthorityError(
                "provider sequence authority file cannot be created"
            ) from exc
        else:
            os.close(descriptor)

        try:
            initial_stat = os.stat(self.path)
            initial_file_identity = (int(initial_stat.st_dev), int(initial_stat.st_ino))
        except OSError as exc:
            raise ProviderSequenceAuthorityError(
                "new provider sequence authority file cannot be stat-bound"
            ) from exc

        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_seconds,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise ProviderSequenceAuthorityError(
                "provider sequence SQLite authority cannot be initialized"
            ) from exc

        try:
            try:
                opened_stat = os.stat(self.path)
            except OSError as exc:
                raise ProviderSequenceAuthorityError(
                    "new provider sequence authority file disappeared during initialization"
                ) from exc
            opened_file_identity = (int(opened_stat.st_dev), int(opened_stat.st_ino))
            if opened_file_identity != initial_file_identity:
                raise ProviderSequenceAuthorityError(
                    "provider sequence authority file changed during initialization"
                )

            self._configure_connection(connection)
            journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if (
                journal_mode is None
                or len(journal_mode) != 1
                or str(journal_mode[0]).lower() != "wal"
            ):
                raise ProviderSequenceAuthorityError(
                    "provider sequence authority requires SQLite WAL mode"
                )
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS provider_sequence_meta_v1 (
                       singleton INTEGER NOT NULL PRIMARY KEY,
                       schema_version INTEGER NOT NULL,
                       authority_id TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS provider_sequences_v1 (
                       source_id TEXT NOT NULL PRIMARY KEY,
                       last_sequence INTEGER NOT NULL
                   )"""
            )

            self._validate_table_info(
                connection,
                "provider_sequence_meta_v1",
                (
                    (0, "singleton", "INTEGER", 1, None, 1),
                    (1, "schema_version", "INTEGER", 1, None, 0),
                    (2, "authority_id", "TEXT", 1, None, 0),
                ),
            )
            self._validate_table_info(
                connection,
                "provider_sequences_v1",
                (
                    (0, "source_id", "TEXT", 1, None, 1),
                    (1, "last_sequence", "INTEGER", 1, None, 0),
                ),
            )
            meta_rows = connection.execute(
                """SELECT singleton,schema_version,authority_id
                   FROM provider_sequence_meta_v1
                   ORDER BY singleton"""
            ).fetchall()
            if meta_rows:
                raise ProviderSequenceAuthorityError(
                    "new provider sequence authority unexpectedly contains metadata"
                )
            connection.execute(
                """INSERT INTO provider_sequence_meta_v1
                   (singleton,schema_version,authority_id)
                   VALUES (1,?,?)""",
                (_SCHEMA_VERSION, self.authority_id),
            )
            connection.commit()
        except ProviderSequenceAuthorityError:
            self._rollback_quietly(connection)
            raise
        except sqlite3.Error as exc:
            self._rollback_quietly(connection)
            raise ProviderSequenceAuthorityError(
                "provider sequence SQLite initialization failed"
            ) from exc
        finally:
            connection.close()

        self._validate_existing()

    def _validate_existing(self) -> None:
        connection = self._connect_existing()
        try:
            self._configure_connection(connection)
            self._require_wal_mode(connection)
            self._validate_schema_and_authority(connection)
        except sqlite3.Error as exc:
            raise ProviderSequenceAuthorityError(
                "provider sequence SQLite authority validation failed"
            ) from exc
        finally:
            connection.close()

    def _connect_existing(self) -> sqlite3.Connection:
        if not self.path.exists() or not self.path.is_file():
            raise ProviderSequenceAuthorityError(
                "provider sequence authority database is missing"
            )
        uri = self.path.as_uri() + "?mode=rw"
        try:
            return sqlite3.connect(
                uri,
                uri=True,
                timeout=self.busy_timeout_seconds,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise ProviderSequenceAuthorityError(
                "provider sequence authority database cannot be opened"
            ) from exc

    def _configure_connection(self, connection: sqlite3.Connection) -> None:
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")

    @staticmethod
    def _require_wal_mode(connection: sqlite3.Connection) -> None:
        row = connection.execute("PRAGMA journal_mode").fetchone()
        if row is None or len(row) != 1 or str(row[0]).lower() != "wal":
            raise ProviderSequenceAuthorityError(
                "provider sequence authority requires SQLite WAL mode"
            )

    def _validate_schema_and_authority(self, connection: sqlite3.Connection) -> None:
        expected_meta = (
            (0, "singleton", "INTEGER", 1, None, 1),
            (1, "schema_version", "INTEGER", 1, None, 0),
            (2, "authority_id", "TEXT", 1, None, 0),
        )
        expected_sequences = (
            (0, "source_id", "TEXT", 1, None, 1),
            (1, "last_sequence", "INTEGER", 1, None, 0),
        )
        self._validate_table_info(
            connection,
            "provider_sequence_meta_v1",
            expected_meta,
        )
        self._validate_table_info(
            connection,
            "provider_sequences_v1",
            expected_sequences,
        )

        triggers = connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='trigger'
                 AND tbl_name IN ('provider_sequence_meta_v1','provider_sequences_v1')
               ORDER BY name"""
        ).fetchall()
        if triggers:
            raise ProviderSequenceAuthorityError(
                "provider sequence authority tables must not have triggers"
            )

        meta_rows = connection.execute(
            """SELECT singleton,schema_version,authority_id
               FROM provider_sequence_meta_v1
               ORDER BY singleton"""
        ).fetchall()
        if meta_rows != [(1, _SCHEMA_VERSION, self.authority_id)]:
            raise ProviderSequenceAuthorityError(
                "provider sequence authority identity/schema binding mismatch"
            )

    @staticmethod
    def _validate_table_info(
        connection: sqlite3.Connection,
        table_name: str,
        expected: tuple[tuple[object, ...], ...],
    ) -> None:
        rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        normalized = tuple(
            (
                int(row[0]),
                row[1],
                str(row[2]).upper(),
                int(row[3]),
                row[4],
                int(row[5]),
            )
            for row in rows
        )
        if normalized != expected:
            raise ProviderSequenceAuthorityError(
                f"{table_name} schema is not canonical"
            )

    @staticmethod
    def _rollback_quietly(connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.rollback()
        except sqlite3.Error:
            pass
