from __future__ import annotations

"""Restart-safe storage-pressure recovery fence for the canonical collector store.

The guard augments ``CollectorDeltaStore`` in place.  It does not create another
store, scheduler, or retention authority.  Read/replay and explicit retention keep
using the canonical SQLite database directly, while collector intake/bootstrap
writes require a fresh volume observation plus a committed SQLite round-trip on
every process-local store construction and again after any SQLITE_FULL signal.
"""

import shutil
import sqlite3
from typing import Any, Callable

from .collector_sqlite_bounded_storage import (
    CollectorStorageBackpressureError,
    _sqlite_full_in_chain,
)


_RECOVERY_PROBE_META_KEY = "collector_storage_recovery_probe_generation_v1"


def install_collector_storage_recovery_guard(store_cls: type[Any]) -> None:
    """Install a fail-closed first-write/restart recovery fence on ``store_cls``.

    Only write paths owned by the collector runtime are fenced.  Read-only access
    and explicit pin-aware retention/compaction remain available during pressure so
    an operator can inspect evidence and reclaim space.  A successful probe is not
    cached across ``CollectorDeltaStore`` construction: every restart must prove the
    current volume/store can durably commit before intake is reopened.
    """

    if getattr(store_cls, "_collector_storage_recovery_guard_v1_installed", False):
        return

    original_init = store_cls.__init__
    original_append = store_cls.append
    original_runtime_append = getattr(store_cls, "_append_with_runtime_stream_epoch", None)
    original_bootstrap = getattr(store_cls, "_bootstrap_or_recover_runtime_stream_epoch", None)

    def guarded_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        # Process-local truth only.  It intentionally resets on every construction
        # so a probe from a previous process can never reopen writes after restart.
        self._collector_storage_recovery_probe_required_v1 = True
        self._collector_storage_recovery_probe_generation_v1 = None

    def _raise_pressure(message: str, exc: BaseException | None = None) -> None:
        error = CollectorStorageBackpressureError(f"RETENTION_REQUIRED: {message}")
        if exc is None:
            raise error
        raise error from exc

    def recovery_probe(self: Any) -> int:
        """Commit and re-read one volume-bound operational probe generation."""

        connection: sqlite3.Connection | None = None
        next_generation: int | None = None
        try:
            connection = self._connect()
            page_size_row = connection.execute("PRAGMA page_size").fetchone()
            page_count_row = connection.execute("PRAGMA page_count").fetchone()
            max_page_count_row = connection.execute("PRAGMA max_page_count").fetchone()
            if (
                page_size_row is None
                or page_count_row is None
                or max_page_count_row is None
            ):
                raise ValueError("collector SQLite page geometry is unavailable")
            page_size = int(page_size_row[0])
            page_count = int(page_count_row[0])
            max_page_count = int(max_page_count_row[0])
            if page_size <= 0 or page_count < 0 or max_page_count < page_count:
                raise ValueError("collector SQLite page geometry is invalid")

            # Observe the actual volume afresh.  Keep a small journal/write margin;
            # the committed probe below remains the decisive empirical check.
            free_bytes = int(shutil.disk_usage(self.path.parent).free)
            if free_bytes < page_size * 2:
                _raise_pressure(
                    "collector volume lacks the minimum durable write margin; "
                    "READ/retention remain available"
                )

            configured_budget = getattr(self, "configured_max_bytes", None)
            if configured_budget is not None and page_count >= max_page_count:
                _raise_pressure(
                    "collector SQLite page budget has no free page for intake; "
                    "run explicit pin-aware compaction before retry"
                )

            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM collector_meta WHERE key=?",
                (_RECOVERY_PROBE_META_KEY,),
            ).fetchone()
            if row is None:
                current_generation = 0
            else:
                raw_generation = row[0]
                if not isinstance(raw_generation, str):
                    raise ValueError("collector recovery probe generation is invalid")
                try:
                    current_generation = int(raw_generation, 10)
                except ValueError as exc:
                    raise ValueError(
                        "collector recovery probe generation is invalid"
                    ) from exc
                if current_generation < 1 or str(current_generation) != raw_generation:
                    raise ValueError("collector recovery probe generation is invalid")
            next_generation = current_generation + 1
            connection.execute(
                "INSERT INTO collector_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_RECOVERY_PROBE_META_KEY, str(next_generation)),
            )
            connection.commit()
        except CollectorStorageBackpressureError:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        except Exception as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            if _sqlite_full_in_chain(exc) or isinstance(exc, (sqlite3.DatabaseError, OSError)):
                _raise_pressure(
                    "collector durable recovery probe could not commit; "
                    "READ/retention remain available",
                    exc,
                )
            raise
        finally:
            if connection is not None:
                connection.close()

        if next_generation is None:
            raise AssertionError("collector recovery probe generation was not resolved")

        verification: sqlite3.Connection | None = None
        try:
            verification = self._connect()
            row = verification.execute(
                "SELECT value FROM collector_meta WHERE key=?",
                (_RECOVERY_PROBE_META_KEY,),
            ).fetchone()
            if row is None or row[0] != str(next_generation):
                raise ValueError(
                    "collector durable recovery probe did not survive reopen"
                )
        except Exception as exc:
            if _sqlite_full_in_chain(exc) or isinstance(exc, (sqlite3.DatabaseError, OSError)):
                _raise_pressure(
                    "collector durable recovery probe could not be verified after reopen; "
                    "READ/retention remain available",
                    exc,
                )
            raise
        finally:
            if verification is not None:
                verification.close()

        self._collector_storage_recovery_probe_required_v1 = False
        self._collector_storage_recovery_probe_generation_v1 = next_generation
        return next_generation

    def _guarded_write(self: Any, action: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if getattr(self, "_collector_storage_recovery_probe_required_v1", True):
            try:
                recovery_probe(self)
            except Exception:
                self._collector_storage_recovery_probe_required_v1 = True
                raise
        try:
            return action(self, *args, **kwargs)
        except Exception as exc:
            if isinstance(exc, CollectorStorageBackpressureError):
                self._collector_storage_recovery_probe_required_v1 = True
                raise
            if _sqlite_full_in_chain(exc):
                self._collector_storage_recovery_probe_required_v1 = True
                _raise_pressure(
                    "collector write hit SQLite/full-volume pressure; "
                    "a fresh durable recovery probe is required before retry",
                    exc,
                )
            raise

    def guarded_append(self: Any, *args: Any, **kwargs: Any) -> Any:
        return _guarded_write(self, original_append, *args, **kwargs)

    store_cls.__init__ = guarded_init
    store_cls.append = guarded_append
    store_cls._collector_storage_recovery_probe = recovery_probe

    if original_runtime_append is not None:
        def guarded_runtime_append(self: Any, *args: Any, **kwargs: Any) -> Any:
            return _guarded_write(self, original_runtime_append, *args, **kwargs)

        store_cls._append_with_runtime_stream_epoch = guarded_runtime_append

    if original_bootstrap is not None:
        def guarded_bootstrap(self: Any, *args: Any, **kwargs: Any) -> Any:
            return _guarded_write(self, original_bootstrap, *args, **kwargs)

        store_cls._bootstrap_or_recover_runtime_stream_epoch = guarded_bootstrap

    store_cls._collector_storage_recovery_guard_v1_installed = True
