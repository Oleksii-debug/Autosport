"""Seal ProphetX lifecycle scope/path authority against instance rebinding.

The lifecycle deliberately copies a caller-owned frozen scope into private primitive
fields. Those fields, plus the derived pool directory and state path, are still
ordinary Python instance attributes and can be reassigned with ``object.__setattr__``.
This composition guard keeps the canonical values in closure-hidden process-local
state and validates the complete scope/path tuple before any authority-bearing field
is returned to the lifecycle implementation.

It does not create provider sessions, credentials, transport, or execution authority.
"""

from __future__ import annotations

from threading import RLock
from weakref import ref

from . import prophetx_session_lifecycle as _lifecycle


_ERROR = _lifecycle.ProphetXSessionLifecycleError
_LIFECYCLE_TYPE = _lifecycle.ProphetXSessionLifecycle
_ORIGINAL_INIT = _LIFECYCLE_TYPE.__init__
_ORIGINAL_GETATTRIBUTE = _LIFECYCLE_TYPE.__getattribute__


def _install_scope_authority_guard() -> None:
    lifecycle_type = _LIFECYCLE_TYPE
    error_type = _ERROR
    original_init = _ORIGINAL_INIT
    original_getattribute = _ORIGINAL_GETATTRIBUTE
    protected_reads = frozenset(
        {
            "workspace",
            "_scope_environment",
            "_scope_access_key_identity_sha256",
            "_scope_credential_revision",
            "_scope_integration_role",
            "_scope_dir",
            "_state_path",
        }
    )
    records: dict[int, tuple[object, ...]] = {}
    # Keep the exact authority tuple itself alive outside the replaceable key->record
    # mapping.  An integer id alone is not enough: Python may reuse the id after a
    # removed tuple is collected.  Exact-object retention makes replacement/re-anchoring
    # fail closed even if an allocator later recycles an address.
    sealed_records: list[tuple[object, ...]] = []
    records_lock = RLock()

    def _forget_sealed(record: tuple[object, ...]) -> None:
        sealed_records[:] = [candidate for candidate in sealed_records if candidate is not record]

    def _snapshot(instance) -> tuple[object, ...]:
        state = original_getattribute(instance, "__dict__")
        if type(state) is not dict:
            raise error_type("session scope authority changed")
        try:
            return (
                state["workspace"],
                state["_scope_environment"],
                state["_scope_access_key_identity_sha256"],
                state["_scope_credential_revision"],
                state["_scope_integration_role"],
                state["_scope_dir"],
                state["_state_path"],
            )
        except KeyError as exc:
            raise error_type("session scope authority changed") from exc

    def _register(instance) -> None:
        instance_id = id(instance)
        expected = _snapshot(instance)

        def _discard(dead_ref) -> None:
            with records_lock:
                current = records.get(instance_id)
                if current is not None and current[0] is dead_ref:
                    records.pop(instance_id, None)
                    _forget_sealed(current)

        instance_ref = ref(instance, _discard)
        record = (instance_ref, *expected)
        with records_lock:
            previous = records.get(instance_id)
            if previous is not None:
                _forget_sealed(previous)
            records[instance_id] = record
            sealed_records.append(record)

    def _require_current(instance) -> None:
        instance_id = id(instance)
        with records_lock:
            record = records.get(instance_id)
            sealed = record is not None and any(
                candidate is record for candidate in sealed_records
            )
        if record is None:
            # Canonical __init__ reads protected scope/path attributes before the
            # post-construction anchor is registered.  Its final initialization
            # marker, _thread_lock, is created only after the last such protected
            # read.  Once that marker exists, a missing record can only mean the
            # authority registry was deleted/corrupted and must fail closed.
            try:
                state = original_getattribute(instance, "__dict__")
            except (AttributeError, TypeError) as exc:
                raise error_type("session scope authority changed") from exc
            if type(state) is dict and "_thread_lock" not in state:
                return
            raise error_type("session scope authority changed")
        if not sealed or record[0]() is not instance:
            raise error_type("session scope authority changed")
        current = _snapshot(instance)
        expected = record[1:]
        if len(current) != len(expected):
            raise error_type("session scope authority changed")
        # Scope text is immutable semantic content; path/workspace objects are also
        # immutable and remain exact construction products. Identity comparison for
        # every slot rejects equal-looking object replacement as well as value drift.
        if any(now is not original for now, original in zip(current, expected)):
            raise error_type("session scope authority changed")
        scope_dir = expected[-2]
        state_path = expected[-1]
        try:
            resolved_scope_dir = scope_dir.resolve(strict=False)
            resolved_state_path = state_path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise error_type("session scope filesystem authority changed") from exc
        if (
            resolved_scope_dir != scope_dir
            or resolved_state_path != state_path
        ):
            raise error_type("session scope filesystem authority changed")

    def guarded_init(self, *args, **kwargs) -> None:
        original_init(self, *args, **kwargs)
        _register(self)

    def guarded_getattribute(self, name):
        if name in protected_reads:
            _require_current(self)
        return original_getattribute(self, name)

    if (
        lifecycle_type.__init__ is not original_init
        or lifecycle_type.__getattribute__ is not original_getattribute
    ):
        raise RuntimeError("ProphetX lifecycle dispatch changed before scope guard")

    lifecycle_type.__init__ = guarded_init
    lifecycle_type.__getattribute__ = guarded_getattribute


_install_scope_authority_guard()
del _install_scope_authority_guard
