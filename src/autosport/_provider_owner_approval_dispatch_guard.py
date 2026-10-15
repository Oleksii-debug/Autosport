"""Seal provider owner-approval positive authority against mutable dispatch.

The owning store persists a causal, monotonic approval/revocation history.  Its
public methods are ordinary Python methods, however, and internally dispatch to
other class methods plus module helpers.  A later class/module rebind must not let
an exact canonical store instance mint or resolve positive approval without the
same durable validation path.

The existing product-clock and atomic-publication seams remain intentionally
patchable for deterministic fake-clock/crash tests; the guard seals the authority
logic around them, not those test injection points.

This guard does not interpret provider terms, authenticate a signer, or authorize
provider writes, execution, settlement, or real money.
"""

from __future__ import annotations

from . import provider_owner_approval as _approval


def _install_guard() -> None:
    store_type = _approval.OwnerApprovalStore
    resolution_type = _approval.OwnerApprovalResolution
    reason_type = _approval.OwnerApprovalResolutionReason

    raw_approve = store_type.approve
    raw_revoke = store_type.revoke
    raw_resolve = store_type.resolve
    raw_approve_code = getattr(raw_approve, "__code__", None)
    raw_revoke_code = getattr(raw_revoke, "__code__", None)
    raw_resolve_code = getattr(raw_resolve, "__code__", None)

    method_names = (
        "__init__",
        "_read_locked",
        "_recover_locked",
        "_resolve_state",
        "_publish_locked",
    )
    method_dispatch = tuple(
        (name, getattr(store_type, name), getattr(getattr(store_type, name), "__code__", None))
        for name in method_names
    )

    static_names = (
        "_inputs",
        "_event_now_locked",
        "_previous",
        "_for_authority",
        "_validate_state",
    )
    static_dispatch = []
    for name in static_names:
        descriptor = store_type.__dict__.get(name)
        if type(descriptor) is not staticmethod:
            raise RuntimeError(f"OwnerApprovalStore.{name} must remain staticmethod")
        function = descriptor.__func__
        static_dispatch.append((name, descriptor, function, getattr(function, "__code__", None)))
    static_dispatch = tuple(static_dispatch)

    resolution_init = resolution_type.__init__
    resolution_init_code = getattr(resolution_init, "__code__", None)
    resolution_fields = tuple(
        (name, getattr(resolution_type, name))
        for name in resolution_type.__dataclass_fields__
    )

    identity_names = (
        "OwnerApprovalResolution",
        "OwnerApprovalResolutionReason",
        "durable_path_lock",
        "strict_json_loads",
        "MonotonicWorkspaceAuthority",
        "AuthorityPhase",
        "Path",
        "hashlib",
        "json",
        "datetime",
        "timezone",
        "urlsplit",
        "_text",
        "_sha",
        "_dt",
        "_instant",
        "_now",
        "_reference",
        "_json_bytes",
        "_hash",
        "_event",
        "_resolution",
    )
    canonical_identities = tuple(
        (name, getattr(_approval, name)) for name in identity_names
    )

    value_names = (
        "_SCHEMA",
        "_DOMAIN",
        "_STATE_KEYS",
        "_EVENT_KEYS",
    )
    canonical_values = tuple(
        (name, getattr(_approval, name)) for name in value_names
    )

    hashlib_sha256 = _approval.hashlib.sha256
    json_dumps = _approval.json.dumps
    json_load_error = _approval.json.JSONDecodeError

    def require_dispatch() -> None:
        if (
            _approval.OwnerApprovalStore is not store_type
            or _approval.OwnerApprovalResolution is not resolution_type
            or _approval.OwnerApprovalResolutionReason is not reason_type
            or getattr(raw_approve, "__code__", None) is not raw_approve_code
            or getattr(raw_revoke, "__code__", None) is not raw_revoke_code
            or getattr(raw_resolve, "__code__", None) is not raw_resolve_code
        ):
            raise RuntimeError("provider owner-approval public authority changed")

        for name, expected, expected_code in method_dispatch:
            current = getattr(store_type, name, None)
            if current is not expected or getattr(current, "__code__", None) is not expected_code:
                raise RuntimeError(
                    f"provider owner-approval store dispatch {name!r} changed"
                )

        for name, descriptor, function, code in static_dispatch:
            current_descriptor = store_type.__dict__.get(name)
            if (
                current_descriptor is not descriptor
                or getattr(current_descriptor, "__func__", None) is not function
                or getattr(function, "__code__", None) is not code
            ):
                raise RuntimeError(
                    f"provider owner-approval store dispatch {name!r} changed"
                )

        if (
            resolution_type.__init__ is not resolution_init
            or getattr(resolution_init, "__code__", None) is not resolution_init_code
            or any(
                getattr(resolution_type, name, None) is not descriptor
                for name, descriptor in resolution_fields
            )
        ):
            raise RuntimeError("provider owner-approval resolution type changed")

        for name, expected in canonical_identities:
            if getattr(_approval, name, None) is not expected:
                raise RuntimeError(
                    f"provider owner-approval dependency {name!r} changed"
                )
        for name, expected in canonical_values:
            current = getattr(_approval, name, None)
            if type(current) is not type(expected) or current != expected:
                raise RuntimeError(
                    f"provider owner-approval contract {name!r} changed"
                )
        if (
            _approval.hashlib.sha256 is not hashlib_sha256
            or _approval.json.dumps is not json_dumps
            or _approval.json.JSONDecodeError is not json_load_error
        ):
            raise RuntimeError("provider owner-approval primitive dispatch changed")

    def _require_result(result):
        require_dispatch()
        if type(result) is not resolution_type:
            raise RuntimeError(
                "provider owner-approval returned non-canonical resolution"
            )
        if type(result.reason) is not reason_type or type(result.approved) is not bool:
            raise RuntimeError(
                "provider owner-approval returned malformed resolution authority"
            )
        return result

    def guarded_approve(self, *args, **kwargs):
        if store_type.approve is not guarded_approve:
            raise RuntimeError("provider owner-approval approve entrypoint changed")
        require_dispatch()
        return _require_result(raw_approve(self, *args, **kwargs))

    def guarded_revoke(self, *args, **kwargs):
        if store_type.revoke is not guarded_revoke:
            raise RuntimeError("provider owner-approval revoke entrypoint changed")
        require_dispatch()
        return _require_result(raw_revoke(self, *args, **kwargs))

    def guarded_resolve(self, *args, **kwargs):
        if store_type.resolve is not guarded_resolve:
            raise RuntimeError("provider owner-approval resolve entrypoint changed")
        require_dispatch()
        return _require_result(raw_resolve(self, *args, **kwargs))

    for guarded, raw in (
        (guarded_approve, raw_approve),
        (guarded_revoke, raw_revoke),
        (guarded_resolve, raw_resolve),
    ):
        guarded.__name__ = raw.__name__
        guarded.__qualname__ = raw.__qualname__
        guarded.__doc__ = raw.__doc__
        guarded.__module__ = raw.__module__

    store_type.approve = guarded_approve
    store_type.revoke = guarded_revoke
    store_type.resolve = guarded_resolve


_install_guard()
del _install_guard
