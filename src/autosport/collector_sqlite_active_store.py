from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .causal_collector_legacy import DeltaConflictError
from .collector_sqlite_store import CollectorDeltaStore as _SQLiteCollectorDeltaStore


_SQLITE_HEADER = b"SQLite format 3\x00"


class CollectorDeltaStore(_SQLiteCollectorDeltaStore):
    """Canonical SQLite store plus compatibility fault-injection boundary."""

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
