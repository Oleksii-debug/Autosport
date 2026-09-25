from __future__ import annotations

"""Serialize canonical collector-service state mutations across processes.

The durable headless collector state is a read/modify/replace journal. Atomic file
replacement prevents torn bytes but, by itself, does not serialize two independent
state objects (or processes) that read the same predecessor. Reuse the repository's
hardened WorkspaceEconomicLock pathname/handle validation and crash-release semantics,
changing only acquisition to blocking for this state transaction. This preserves the
single existing _CollectorServiceState authority while fencing lost updates and STOP
rollback races.
"""

import os
from typing import BinaryIO

from . import collector_service as _collector_service
from .workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


_State = _collector_service._CollectorServiceState
_ORIGINAL_UPDATE_ANCHOR = "_collector_service_state_original_update_v1"
_SERIALIZED_UPDATE_ANCHOR = "_collector_service_state_serialized_update_v1"


def _load_original_update():
    existing = getattr(_collector_service, _ORIGINAL_UPDATE_ANCHOR, None)
    if existing is None:
        existing = _State._update
        setattr(_collector_service, _ORIGINAL_UPDATE_ANCHOR, existing)
    if not callable(existing):
        raise RuntimeError("collector service state update authority anchor is invalid")
    return existing


_ORIGINAL_UPDATE = _load_original_update()


class _CollectorServiceStateMutationLock(WorkspaceEconomicLock):
    """Blocking cross-process lock for one collector-state workspace.

    WorkspaceEconomicLock is deliberately nonblocking for economic writers. Collector
    state updates instead need wait-and-serialize semantics: dropping the losing update
    is not an acceptable outcome. All of its hardened lock-file creation, no-follow
    identity checks, hard-link rejection, crash release and unlock logic are reused.
    """

    FILE_NAME = ".collector-service-state.lock"

    @staticmethod
    def _lock_handle(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                # LK_LOCK waits/retries for the byte-range lock instead of returning
                # the nonblocking busy outcome used by WorkspaceEconomicLock.
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise WorkspaceEconomicLockError(
                    "cannot acquire blocking collector-service state lock"
                ) from exc
            return

        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise WorkspaceEconomicLockError(
                "cannot acquire blocking collector-service state lock"
            ) from exc


def _serialized_update(self, mutate) -> None:
    try:
        with _CollectorServiceStateMutationLock(self.path.parent):
            _ORIGINAL_UPDATE(self, mutate)
    except WorkspaceEconomicLockError as exc:
        raise _collector_service.CollectorServiceError(
            "cannot serialize collector service durable state mutation"
        ) from exc


def _install() -> None:
    installed = getattr(_collector_service, _SERIALIZED_UPDATE_ANCHOR, None)
    if installed is None:
        installed = _serialized_update
        setattr(_collector_service, _SERIALIZED_UPDATE_ANCHOR, installed)
    if not callable(installed):
        raise RuntimeError("collector service state serialization anchor is invalid")
    _State._update = installed


_install()

__all__: list[str] = []
