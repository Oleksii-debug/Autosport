"""Bind live account-snapshot authority to the authenticated credential origin.

The durable acquisition receipt intentionally does not persist provider credentials and
cannot recreate remote-provider authority.  The owning acquisition module keeps a
same-process exact-object capability so an idempotent retry can avoid provider I/O.
This guard closes the remaining composition boundary: that live capability may be
reused by another canonical acquirer only when it carries the exact same in-memory
Betfair session credentials.  A caller-local account label is never sufficient.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from threading import RLock
from weakref import WeakKeyDictionary, ref

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


@dataclass(frozen=True, slots=True)
class _LiveRequestBinding:
    credentials: BetfairSessionCredentials
    acquired_ref: object


_LOCK = RLock()
_ORIGINS: WeakKeyDictionary = WeakKeyDictionary()
_LIVE_REQUEST_BINDINGS: dict[tuple[str, str], _LiveRequestBinding] = {}


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
            key = (origin.database_path, acquisition_id)
            current = _LIVE_REQUEST_BINDINGS.get(key)
            if current is not None:
                live = current.acquired_ref()
                if live is None:
                    _LIVE_REQUEST_BINDINGS.pop(key, None)
                elif current.credentials != origin.credentials:
                    raise AccountSnapshotAcquisitionError(
                        "live account snapshot acquisition is bound to a different "
                        "authenticated credential origin"
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

            def forget_live(reference: object, *, binding_key=key) -> None:
                with _LOCK:
                    bound = _LIVE_REQUEST_BINDINGS.get(binding_key)
                    if bound is not None and bound.acquired_ref is reference:
                        _LIVE_REQUEST_BINDINGS.pop(binding_key, None)

            acquired_ref = ref(acquired, forget_live)
            _LIVE_REQUEST_BINDINGS[key] = _LiveRequestBinding(
                credentials=origin.credentials,
                acquired_ref=acquired_ref,
            )
            return acquired

    guarded_acquire._autosport_account_snapshot_origin_binding_guard = True  # type: ignore[attr-defined]
    BetfairAccountSnapshotAcquirer.__init__ = guarded_init
    BetfairAccountSnapshotAcquirer.acquire = guarded_acquire


_install_origin_binding()
del _install_origin_binding
