from __future__ import annotations

"""SQLite-native allocation ceiling for the canonical collector delta store.

This module augments the existing canonical ``CollectorDeltaStore`` in place. It
never creates a second store or retention authority. Once a byte budget is first
configured for a canonical SQLite path it is durably bound inside that database's
existing ``collector_meta`` authority, and every later canonical handle resolves and
enforces the same effective ceiling before using its connection.
"""

import os
import sqlite3
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any


_BUDGET_META_KEY = "collector_storage_max_bytes_v1"
_ACTIVE_CONSTRUCTION_BUDGET: ContextVar[int | None] = ContextVar(
    "collector_storage_active_construction_budget_v1",
    default=None,
)


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


def _read_durable_budget(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT value FROM collector_meta WHERE key=?",
        (_BUDGET_META_KEY,),
    ).fetchone()
    if row is None:
        return None
    raw = row[0]
    if not isinstance(raw, str):
        raise CollectorStorageBudgetError("invalid durable collector max_bytes authority")
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise CollectorStorageBudgetError(
            "invalid durable collector max_bytes authority"
        ) from exc
    if value <= 0 or str(value) != raw:
        raise CollectorStorageBudgetError("invalid durable collector max_bytes authority")
    return value


def _apply_page_budget(connection: sqlite3.Connection, budget: int) -> None:
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
        raise CollectorStorageBudgetError("max_bytes is smaller than one SQLite page")
    if page_count > requested_pages:
        raise CollectorStorageBudgetError(
            "existing collector SQLite store exceeds configured max_bytes"
        )

    applied_row = connection.execute(f"PRAGMA max_page_count={requested_pages}").fetchone()
    if applied_row is None:
        raise CollectorStorageBudgetError(
            "cannot establish collector SQLite max_page_count"
        )
    applied_pages = int(applied_row[0])
    # SQLite may clamp an extremely large request to its own hard maximum. A lower
    # clamp is still safe; a larger result would violate the durable budget.
    if applied_pages < page_count or applied_pages > requested_pages:
        raise CollectorStorageBudgetError(
            "collector SQLite max_page_count conflicts with configured max_bytes"
        )


def _resolve_effective_budget(
    connection: sqlite3.Connection,
    requested_budget: int | None,
) -> int | None:
    durable_budget = _read_durable_budget(connection)
    if durable_budget is not None:
        if requested_budget is not None and requested_budget != durable_budget:
            raise CollectorStorageBudgetError(
                "configured max_bytes conflicts with durable collector max_bytes"
            )
        return durable_budget
    if requested_budget is None:
        return None

    # Constrain this connection before publishing the durable authority so even the
    # metadata write itself cannot allocate beyond the requested page ceiling.
    _apply_page_budget(connection, requested_budget)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Another canonical process may have established the path authority while
        # this connection was waiting for the SQLite writer lock. Re-read under the
        # lock and never widen or narrow that already-published budget implicitly.
        durable_budget = _read_durable_budget(connection)
        if durable_budget is None:
            # The database may have grown while this connection waited behind a
            # writer that acquired BEGIN IMMEDIATE before budget publication. Rebind
            # and validate the current page geometry while holding the same writer
            # lock that protects the durable authority. If the store is already over
            # budget, fail before publishing a contradictory metadata row.
            _apply_page_budget(connection, requested_budget)
            connection.execute(
                "INSERT INTO collector_meta(key, value) VALUES(?, ?)",
                (_BUDGET_META_KEY, str(requested_budget)),
            )
            durable_budget = requested_budget
        elif durable_budget != requested_budget:
            raise CollectorStorageBudgetError(
                "configured max_bytes conflicts with durable collector max_bytes"
            )
        connection.commit()
    except Exception as exc:
        if connection.in_transaction:
            connection.rollback()
        if _sqlite_full_in_chain(exc):
            raise CollectorStorageBudgetError(
                "configured max_bytes cannot durably record the collector budget "
                "within its page ceiling"
            ) from exc
        raise
    return durable_budget


def install_collector_storage_budget(store_cls: type[Any]) -> None:
    """Install the bounded-storage contract on the canonical store class once.

    The class identity is intentionally preserved because retention and desktop
    authority guards require the exact canonical ``CollectorDeltaStore`` type.
    """

    if getattr(store_cls, "_collector_storage_budget_v1_installed", False):
        return

    original_init = store_cls.__init__
    original_connect = store_cls._connect
    original_connect_path = store_cls._connect_path
    original_initialize_sqlite = store_cls._initialize_sqlite
    original_append_connection = store_cls._append_connection
    original_append = store_cls.append
    original_runtime_append = getattr(store_cls, "_append_with_runtime_stream_epoch", None)
    original_runtime_batch = getattr(store_cls, "_append_batch_with_runtime_stream_epoch", None)

    def bounded_connect_path(path: Path) -> sqlite3.Connection:
        """Constrain every construction-time SQLite connection before any writes."""

        connection = original_connect_path(path)
        budget = _ACTIVE_CONSTRUCTION_BUDGET.get()
        if budget is None:
            return connection
        try:
            _apply_page_budget(connection, budget)
            return connection
        except Exception:
            connection.close()
            raise

    def bounded_initialize_sqlite(
        cls: type[Any],
        path: Path,
        *,
        wal: bool,
    ) -> None:
        """Initialize under the native ceiling and bind it before authority switch."""

        budget = _ACTIVE_CONSTRUCTION_BUDGET.get()
        try:
            # The original initializer resolves cls._connect_path dynamically, which
            # is the bounded connection factory installed below. Therefore the page
            # ceiling is active before journal/schema/user_version writes begin.
            original_initialize_sqlite(path, wal=wal)
            if budget is None:
                return
            connection = bounded_connect_path(path)
            try:
                effective = _resolve_effective_budget(connection, budget)
                if effective != budget:
                    raise CollectorStorageBudgetError(
                        "initialized collector max_bytes authority conflicts"
                    )
                _apply_page_budget(connection, budget)
            finally:
                connection.close()
        except Exception as exc:
            if budget is not None and _sqlite_full_in_chain(exc):
                raise CollectorStorageBudgetError(
                    "configured max_bytes cannot initialize collector SQLite "
                    "within its page ceiling"
                ) from exc
            raise

    def bounded_init(
        self: Any,
        path: str | Path,
        *,
        max_bytes: int | None = None,
    ) -> None:
        requested_budget = _validated_max_bytes(max_bytes)
        canonical_path = Path(path)
        self._collector_requested_max_bytes_v1 = requested_budget
        self._collector_max_bytes_v1 = None

        token = _ACTIVE_CONSTRUCTION_BUDGET.set(requested_budget)
        staged_path: Path | None = None
        try:
            # A bounded first creation is staged beside the canonical path. This keeps
            # the canonical path absent if schema/projection initialization exhausts
            # the native page ceiling. Existing SQLite and legacy authorities stay at
            # their canonical path; legacy migration already constructs a temp
            # candidate and atomically switches only after successful replay.
            if requested_budget is not None and not canonical_path.exists():
                canonical_path.parent.mkdir(parents=True, exist_ok=True)
                staged_path = canonical_path.with_name(
                    f".{canonical_path.name}.bounded-init-{os.getpid()}-{uuid.uuid4().hex}.tmp"
                )
                original_init(self, staged_path)
                if canonical_path.exists():
                    raise CollectorStorageBudgetError(
                        "collector path appeared during bounded initialization"
                    )
                os.replace(staged_path, canonical_path)
                staged_path = None
                self.path = canonical_path
                # Reopen through the canonical path so the live instance proves that
                # the durable budget survived the authority move.
                verification = self._connect()
                verification.close()
            else:
                original_init(self, canonical_path)
        except Exception as exc:
            if requested_budget is not None and _sqlite_full_in_chain(exc):
                raise CollectorStorageBudgetError(
                    "configured max_bytes cannot initialize collector SQLite "
                    "within its page ceiling"
                ) from exc
            raise
        finally:
            _ACTIVE_CONSTRUCTION_BUDGET.reset(token)
            if staged_path is not None:
                try:
                    staged_path.unlink(missing_ok=True)
                except OSError:
                    pass
                for suffix in ("-journal", "-wal", "-shm"):
                    try:
                        Path(f"{staged_path}{suffix}").unlink(missing_ok=True)
                    except OSError:
                        pass

    def bounded_connect(self: Any) -> sqlite3.Connection:
        connection = original_connect(self)
        requested_budget = getattr(self, "_collector_requested_max_bytes_v1", None)
        try:
            budget = _resolve_effective_budget(connection, requested_budget)
            if budget is not None:
                _apply_page_budget(connection, budget)
            self._collector_max_bytes_v1 = budget
            return connection
        except Exception:
            connection.close()
            raise

    def bounded_append_connection(
        cls: type[Any],
        connection: sqlite3.Connection,
        delta: Any,
    ) -> bool:
        """Rebind durable budget after BEGIN IMMEDIATE and before append writes.

        This closes the first-activation race where an already-open unconfigured
        connection could otherwise acquire the writer lock after another handle had
        durably published a budget. The same SQLite transaction that owns the append
        now observes and installs the canonical ceiling before any page allocation.
        """

        durable_budget = _read_durable_budget(connection)
        if durable_budget is not None:
            _apply_page_budget(connection, durable_budget)
        return original_append_connection(connection, delta)

    def bounded_append(self: Any, *args: Any, **kwargs: Any) -> bool:
        try:
            return original_append(self, *args, **kwargs)
        except Exception as exc:
            if _sqlite_full_in_chain(exc):
                raise CollectorStorageBackpressureError(
                    "RETENTION_REQUIRED: collector SQLite page budget is exhausted; "
                    "run explicit pin-aware compaction within the durable max_bytes, "
                    "then retry"
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
                    "run explicit pin-aware compaction within the durable max_bytes, "
                    "then retry"
                ) from exc
            raise

    def bounded_runtime_batch(
        self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[bool, ...]:
        assert original_runtime_batch is not None
        try:
            return original_runtime_batch(self, *args, **kwargs)
        except Exception as exc:
            if _sqlite_full_in_chain(exc):
                raise CollectorStorageBackpressureError(
                    "RETENTION_REQUIRED: collector SQLite page budget is exhausted; "
                    "run explicit pin-aware compaction within the durable max_bytes, "
                    "then retry"
                ) from exc
            raise

    def configured_max_bytes(self: Any) -> int | None:
        return getattr(self, "_collector_max_bytes_v1", None)

    store_cls._connect_path = staticmethod(bounded_connect_path)
    store_cls._initialize_sqlite = classmethod(bounded_initialize_sqlite)
    store_cls.__init__ = bounded_init
    store_cls._connect = bounded_connect
    store_cls._append_connection = classmethod(bounded_append_connection)
    store_cls.append = bounded_append
    if original_runtime_append is not None:
        store_cls._append_with_runtime_stream_epoch = bounded_runtime_append
    if original_runtime_batch is not None:
        store_cls._append_batch_with_runtime_stream_epoch = bounded_runtime_batch
    store_cls.configured_max_bytes = property(configured_max_bytes)
    store_cls._collector_storage_budget_v1_installed = True
