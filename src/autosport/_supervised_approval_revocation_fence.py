"""Linearize supervised-approval revocation with canonical economic writes.

Betfair supervised execution already holds ``WorkspaceEconomicLock`` from current
admission through the irreversible provider call.  A durable approval revocation
must participate in that same existing serialization authority; otherwise a
separate ledger writer can append SUPERVISED_APPROVAL_REVOKED after admission but
before placeOrders transport.

This module does not create another execution or revocation authority.  It wraps
the existing ``RealExecutionLedger.revoke_supervised_approval`` transition with
the canonical workspace economic lock.  Therefore exactly one ordering wins:

* revocation commits first -> later supervised admission sees it and fails closed;
* the provider-write critical section wins first -> revocation cannot interleave
  with that already-authorized effect and may be retried after the critical
  section exits.

The latter is intentionally not retroactive cancellation.  Emergency in-flight
stopping remains the responsibility of the existing ExecutionStopAuthority.
"""

from __future__ import annotations

from pathlib import Path

from .real_execution_ledger import (
    ExecutionLedgerBusyError,
    ExecutionLedgerIntegrityError,
    RealExecutionLedger,
)
from .workspace_lock import (
    WorkspaceEconomicLock,
    WorkspaceEconomicLockBusyError,
    WorkspaceEconomicLockError,
)


_LEDGER_TYPE = RealExecutionLedger
_LOCK_TYPE = WorkspaceEconomicLock
_LOCK_NEW = WorkspaceEconomicLock.__new__
_LOCK_INIT = WorkspaceEconomicLock.__init__
_LOCK_INIT_CODE = _LOCK_INIT.__code__
_LOCK_ACQUIRE = WorkspaceEconomicLock.acquire
_LOCK_ACQUIRE_CODE = _LOCK_ACQUIRE.__code__
_LOCK_RELEASE = WorkspaceEconomicLock.release
_LOCK_RELEASE_CODE = _LOCK_RELEASE.__code__
_LOCK_METHOD_GRAPH = tuple(
    (
        name,
        getattr(_LOCK_TYPE, name),
        getattr(getattr(_LOCK_TYPE, name), "__code__", None),
    )
    for name in (
        "__init__",
        "acquire",
        "release",
        "_open_lock_handle",
        "_open_new_lock_handle",
        "_validate_existing_lock_path",
        "_validate_open_handle_identity",
        "_require_regular_file",
        "_require_single_link",
        "_lock_handle",
        "_unlock_handle",
    )
)


def _build_fenced_revoke(raw_revoke, raw_revoke_code):
    lock_type = _LOCK_TYPE
    lock_method_graph = _LOCK_METHOD_GRAPH

    def lock_method_graph_unchanged() -> bool:
        return all(
            getattr(lock_type, name, None) is expected
            and (
                expected_code is None
                or getattr(expected, "__code__", None) is expected_code
            )
            for name, expected, expected_code in lock_method_graph
        )
    def revoke_supervised_approval_with_workspace_fence(
        self: RealExecutionLedger,
        *,
        plan_id: str,
        approval_id: str,
        approval_fingerprint: str,
        revoked_at: str,
        revocation_evidence_sha256: str,
    ) -> None:
        if type(self) is not _LEDGER_TYPE:
            raise ExecutionLedgerIntegrityError(
                "supervised approval revocation requires canonical execution ledger"
            )
        if (
            getattr(raw_revoke, "__code__", None) is not raw_revoke_code
            or WorkspaceEconomicLock is not _LOCK_TYPE
            or _LOCK_TYPE.__new__ is not _LOCK_NEW
            or _LOCK_TYPE.__init__ is not _LOCK_INIT
            or getattr(_LOCK_INIT, "__code__", None) is not _LOCK_INIT_CODE
            or _LOCK_TYPE.acquire is not _LOCK_ACQUIRE
            or getattr(_LOCK_ACQUIRE, "__code__", None) is not _LOCK_ACQUIRE_CODE
            or _LOCK_TYPE.release is not _LOCK_RELEASE
            or getattr(_LOCK_RELEASE, "__code__", None) is not _LOCK_RELEASE_CODE
            or not lock_method_graph_unchanged()
        ):
            raise ExecutionLedgerIntegrityError(
                "supervised approval revocation serialization authority changed"
            )

        try:
            workspace = Path(self.path).parent.resolve()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ExecutionLedgerIntegrityError(
                "supervised approval revocation workspace is not canonical"
            ) from exc

        # Do not delegate authority to mutable __enter__/__exit__ dispatch.
        # Allocate and initialize the canonical lock through captured implementations,
        # then invoke captured acquire/release non-virtually.
        try:
            lock = _LOCK_NEW(_LOCK_TYPE)
            _LOCK_INIT(lock, workspace)
            _LOCK_ACQUIRE(lock)
        except WorkspaceEconomicLockBusyError as exc:
            raise ExecutionLedgerBusyError(
                "supervised approval revocation is fenced by active economic execution"
            ) from exc
        except WorkspaceEconomicLockError as exc:
            raise ExecutionLedgerIntegrityError(
                "supervised approval revocation economic fence failed"
            ) from exc

        try:
            if (
                getattr(raw_revoke, "__code__", None) is not raw_revoke_code
                or _LOCK_TYPE.__init__ is not _LOCK_INIT
                or getattr(_LOCK_INIT, "__code__", None) is not _LOCK_INIT_CODE
                or _LOCK_TYPE.acquire is not _LOCK_ACQUIRE
                or _LOCK_TYPE.release is not _LOCK_RELEASE
                or not lock_method_graph_unchanged()
            ):
                raise ExecutionLedgerIntegrityError(
                    "supervised approval revocation authority changed while fenced"
                )
            raw_revoke(
                self,
                plan_id=plan_id,
                approval_id=approval_id,
                approval_fingerprint=approval_fingerprint,
                revoked_at=revoked_at,
                revocation_evidence_sha256=revocation_evidence_sha256,
            )
            if getattr(raw_revoke, "__code__", None) is not raw_revoke_code:
                raise ExecutionLedgerIntegrityError(
                    "supervised approval revocation implementation changed while fenced"
                )
        except BaseException as primary_error:
            try:
                _LOCK_RELEASE(lock)
            except BaseException as release_error:
                try:
                    primary_error.add_note(
                        "supervised approval revocation economic fence release also failed: "
                        f"{type(release_error).__name__}: {release_error}"
                    )
                except BaseException:
                    pass
            raise

        try:
            _LOCK_RELEASE(lock)
        except WorkspaceEconomicLockError as exc:
            raise ExecutionLedgerIntegrityError(
                "supervised approval revocation economic fence failed"
            ) from exc

    return revoke_supervised_approval_with_workspace_fence


_RAW_REVOKE = _LEDGER_TYPE.__dict__.get("revoke_supervised_approval")
_RAW_REVOKE_CODE = getattr(_RAW_REVOKE, "__code__", None)
if not callable(_RAW_REVOKE) or _RAW_REVOKE_CODE is None:
    raise RuntimeError("canonical supervised approval revocation is unavailable")

_FENCED_REVOKE = _build_fenced_revoke(_RAW_REVOKE, _RAW_REVOKE_CODE)
_FENCED_REVOKE.__name__ = "revoke_supervised_approval"
_FENCED_REVOKE.__qualname__ = f"{_LEDGER_TYPE.__name__}.revoke_supervised_approval"
_FENCED_REVOKE.__module__ = _LEDGER_TYPE.__module__
_LEDGER_TYPE.revoke_supervised_approval = _FENCED_REVOKE

del _RAW_REVOKE, _RAW_REVOKE_CODE
