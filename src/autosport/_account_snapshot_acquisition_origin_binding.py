"""Bind live account-snapshot authority to the authenticated credential origin.

The durable acquisition receipt intentionally does not persist provider credentials and
cannot recreate remote-provider authority. Reusing an existing durable acquisition never
reissues provider-origin authority. This guard binds each initialized acquirer to its exact
database/credential origin so caller-visible state cannot silently redirect a fresh read.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from threading import RLock
from weakref import WeakKeyDictionary

from .account_snapshot_acquisition import (
    AccountSnapshotAcquisitionError,
    AuthoritativeAccountSnapshot,
    BetfairAccountSnapshotAcquirer,
)
from .betfair_account_readonly import BetfairSessionCredentials


@dataclass(frozen=True, slots=True)
class _AcquirerOrigin:
    database_path: str
    credentials: BetfairSessionCredentials


_LOCK = RLock()
_ORIGINS: WeakKeyDictionary = WeakKeyDictionary()


def _database_identity(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def _install_origin_binding() -> None:
    raw_init = BetfairAccountSnapshotAcquirer.__init__
    raw_acquire = BetfairAccountSnapshotAcquirer.acquire
    if getattr(raw_acquire, "_autosport_account_snapshot_origin_binding_guard", False):
        return

    def guarded_init(
        self: BetfairAccountSnapshotAcquirer,
        database_path: str | Path,
        credentials: BetfairSessionCredentials,
        *,
        account_id: str = "default-account",
        timeout_seconds: float = 10.0,
    ) -> None:
        raw_init(
            self,
            database_path,
            credentials,
            account_id=account_id,
            timeout_seconds=timeout_seconds,
        )
        with _LOCK:
            _ORIGINS[self] = _AcquirerOrigin(
                database_path=_database_identity(database_path),
                credentials=credentials,
            )

    def guarded_acquire(
        self: BetfairAccountSnapshotAcquirer,
        requested_capabilities,
        *,
        acquisition_id: str,
    ) -> AuthoritativeAccountSnapshot:
        # Preserve the owning method's validation/error contract for malformed ids.
        if (
            type(acquisition_id) is not str
            or not acquisition_id
            or acquisition_id != acquisition_id.strip()
        ):
            return raw_acquire(
                self,
                requested_capabilities,
                acquisition_id=acquisition_id,
            )

        with _LOCK:
            origin = _ORIGINS.get(self)
            if origin is None:
                raise AccountSnapshotAcquisitionError(
                    "account snapshot acquirer lacks canonical credential-origin binding"
                )
            acquired = raw_acquire(
                self,
                requested_capabilities,
                acquisition_id=acquisition_id,
            )
            if type(acquired) is not AuthoritativeAccountSnapshot:
                raise AccountSnapshotAcquisitionError(
                    "canonical account acquisition returned non-canonical authority"
                )

            return acquired

    guarded_acquire._autosport_account_snapshot_origin_binding_guard = True  # type: ignore[attr-defined]
    BetfairAccountSnapshotAcquirer.__init__ = guarded_init
    BetfairAccountSnapshotAcquirer.acquire = guarded_acquire


_install_origin_binding()
del _install_origin_binding
