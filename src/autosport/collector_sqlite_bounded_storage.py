from __future__ import annotations

"""SQLite-native allocation ceiling for the canonical collector delta store.

This module augments the existing canonical ``CollectorDeltaStore`` in place.  It
never creates a second store or retention authority: every connection to the same
canonical SQLite file receives the configured ``max_page_count`` ceiling, while
existing retention/compaction policy remains solely responsible for deciding what
may be removed.
"""

import sqlite3
from pathlib import Path
from typing import Any


class CollectorStorageBudgetError(ValueError):
    """Raised when a configured hard SQLite allocation budget is invalid."""


class CollectorStorageBackpressureError(RuntimeError):
    """Recoverable fail-closed signal emitted when SQLite reaches its page ceiling."""

    code = "RETENTION_REQUIRED"


def _sqlite_full_in_chain(error: BaseException) -> bool:
    """Return whether ``error`` was caused by SQLite's FULL primary result code."""

    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, sqlite3.DatabaseError):
            code = getattr(current, "sqlite_errorcode", None)
            if isinstance(code, int) and (code & 0xFF) == sqlite3.SQLITE_FULL:
                return True
            # Python implementations without sqlite_errorcode still expose the
            # canonical SQLite diagnostic. Keep this fallback deliberately exact.
            if str(current).strip().lower() == "database or disk is full":
                return True
        current = current.__cause__ or current.__context__
    return False


def _validated_max_bytes(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CollectorStorageBudgetError("max_bytes must be a positive integer or None")
    return value


def install_collector_storage_budget(store_cls: type[Any]) -> None:
    """Install the bounded-storage contract on the canonical store class once.

    The class identity is intentionally preserved because retention and desktop
    authority guards require the exact canonical ``CollectorDeltaStore`` type.
    """

    if getattr(store_cls, "_collector_storage_budget_v1_installed", False):
        return

    original_init = store_cls.__init__
    original_connect = store_cls._connect
    original_append = store_cls.append
    original_runtime_append = getattr(store_cls, "_append_with_runtime_stream_epoch", None)

    def bounded_init(
        self: Any,
        path: str | Path,
        *,
        max_bytes: int | None = None,
    ) -> None:
        self._collector_max_bytes_v1 = _validated_max_bytes(max_bytes)
        original_init(self, path)

    def bounded_connect(self: Any) -> sqlite3.Connection:
        connection = original_connect(self)
        budget = getattr(self, "_collector_max_bytes_v1", None)
        if budget is None:
            return connection
        try:
            page_size_row = connection.execute("PRAGMA page_size").fetchone()
            page_count_row = connection.execute("PRAGMA page_count").fetchone()
            if page_size_row is None or page_count_row is None:
                raise CollectorStorageBudgetError(
                    "cannot resolve SQLite page geometry for collector byte budget"
                )
            page_size = int(page_size_row[0])
            page_count = int(page_count_row[0])
            if page_size <= 0 or page_count < 0:
                raise CollectorStorageBudgetError(
                    "invalid SQLite page geometry for collector byte budget"
                )
            requested_pages = budget // page_size
            if requested_pages < 1:
                raise CollectorStorageBudgetError(
                    "max_bytes is smaller than one SQLite page"
                )
            if page_count > requested_pages:
                raise CollectorStorageBudgetError(
                    "existing collector SQLite store exceeds configured max_bytes"
                )

            applied_row = connection.execute(
                f"PRAGMA max_page_count={requested_pages}"
            ).fetchone()
            if applied_row is None:
                raise CollectorStorageBudgetError(
                    "cannot establish collector SQLite max_page_count"
                )
            applied_pages = int(applied_row[0])
            # SQLite may clamp an extremely large request to its own hard maximum.
            # A lower clamp is still safe; a larger result would violate our budget.
            if applied_pages < page_count or applied_pages > requested_pages:
                raise CollectorStorageBudgetError(
                    "collector SQLite max_page_count conflicts with configured max_bytes"
                )
            return connection
        except Exception:
            connection.close()
            raise

    def bounded_append(self: Any, *args: Any, **kwargs: Any) -> bool:
        try:
            return original_append(self, *args, **kwargs)
        except Exception as exc:
            if _sqlite_full_in_chain(exc):
                raise CollectorStorageBackpressureError(
                    "RETENTION_REQUIRED: collector SQLite page budget is exhausted; "
                    "run explicit pin-aware compaction or enlarge max_bytes, then retry"
                ) from exc
            raise

    def bounded_runtime_append(self: Any, *args: Any, **kwargs: Any) -> bool:
        assert original_runtime_append is not None
        try:
            return original_runtime_append(self, *args, **kwargs)
        except Exception as exc:
            if _sqlite_full_in_chain(exc):
                raise CollectorStorageBackpressureError(
                    "RETENTION_REQUIRED: collector SQLite page budget is exhausted; "
                    "run explicit pin-aware compaction or enlarge max_bytes, then retry"
                ) from exc
            raise

    def configured_max_bytes(self: Any) -> int | None:
        return getattr(self, "_collector_max_bytes_v1", None)

    store_cls.__init__ = bounded_init
    store_cls._connect = bounded_connect
    store_cls.append = bounded_append
    if original_runtime_append is not None:
        store_cls._append_with_runtime_stream_epoch = bounded_runtime_append
    store_cls.configured_max_bytes = property(configured_max_bytes)
    store_cls._collector_storage_budget_v1_installed = True
