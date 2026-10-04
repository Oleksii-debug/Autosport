"""Seal ProphetX lifecycle scope/path authority against instance rebinding.

The lifecycle deliberately copies a caller-owned frozen scope into private primitive
fields.  Those fields, plus the derived pool directory and state path, are still
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
    records_lock = RLock()

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

        instance_ref = ref(instance, _discard)
        with records_lock:
            records[instance_id] = (instance_ref, *expected)

    def _require_current(instance) -> None:
        with records_lock:
            record = records.get(id(instance))
        if record is None:
            # Canonical __init__ legitimately accesses these attributes before its
            # post-construction anchor has been registered by guarded_init.
            return
        if record[0]() is not instance:
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
