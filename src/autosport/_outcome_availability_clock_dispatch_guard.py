"""Seal product outcome-availability clock dispatch against function-metadata rebinding.

The two-phase RunRegistry outcome publication is causal only if its post-publication
clock sampler cannot be replaced.  Python function defaults and module globals are
mutable authority surfaces, so this final composition guard removes both from the
positive path: it clones the canonical product clock and the already-composed begin
implementation, snapshots every directly-read global, and installs a wrapper that
validates those exact snapshots before an outcome-qualified run may begin.

No second registry or availability authority is introduced.
"""
from __future__ import annotations

from types import FunctionType

from . import _outcome_availability_registry_serialization as _availability
from . import run_registry as _run_registry


def _install_guard() -> None:
    registry_type = _run_registry.RunRegistry
    error_type = _availability._outcome_trust.OutcomeLineageTrustError
    canonical_public_clock = _run_registry._utc_now
    canonical_begin = registry_type.begin

    if not getattr(canonical_begin, "_autosport_outcome_two_phase", False):
        raise RuntimeError("causal outcome publication guard is not installed")
    if type(canonical_public_clock) is not FunctionType:
        raise RuntimeError("canonical RunRegistry product clock is not a plain function")
    if type(canonical_begin) is not FunctionType:
        raise RuntimeError("canonical causal RunRegistry begin is not a plain function")

    missing = object()

    def clone_function(
        function: FunctionType,
        *,
        globals_overrides: dict[str, object] | None = None,
    ) -> FunctionType:
        globals_copy = dict(function.__globals__)
        if globals_overrides:
            globals_copy.update(globals_overrides)
        cloned = FunctionType(
            function.__code__,
            globals_copy,
            function.__name__,
            function.__defaults__,
            function.__closure__,
        )
        cloned.__kwdefaults__ = (
            None if function.__kwdefaults__ is None else dict(function.__kwdefaults__)
        )
        cloned.__annotations__ = dict(function.__annotations__)
        return cloned

    def snapshot_globals(function: FunctionType) -> tuple[tuple[str, object], ...]:
        names = sorted(
            name for name in set(function.__code__.co_names) if name in function.__globals__
        )
        if "__builtins__" in function.__globals__:
            names.append("__builtins__")
        return tuple((name, function.__globals__[name]) for name in names)

    def require_snapshot(
        function: FunctionType,
        snapshot: tuple[tuple[str, object], ...],
        label: str,
    ) -> None:
        for name, expected in snapshot:
            if function.__globals__.get(name, missing) is not expected:
                raise error_type(f"frozen {label} global {name!r} was rebound")

    # Freeze the canonical formatter over its import-time datetime/timezone objects.
    # The clone itself remains inspectable, so its directly-read globals are checked
    # before each sample rather than treated as hidden capability state.
    clock_clone = clone_function(canonical_public_clock)
    clock_snapshot = snapshot_globals(clock_clone)

    def sealed_product_utc_now() -> str:
        if _run_registry._utc_now is not canonical_public_clock:
            raise error_type("product UTC clock authority was rebound")
        require_snapshot(clock_clone, clock_snapshot, "product UTC clock")
        return clock_clone()

    # No authority is stored in mutable function defaults.
    if sealed_product_utc_now.__defaults__ is not None:
        raise RuntimeError("sealed product clock unexpectedly exposes defaults")

    # Clone the already-canonical two-phase begin, but replace its late global lookup
    # with the exact sampler above.  A direct module-global sampler rebind can no
    # longer steer this implementation.
    begin_clone = clone_function(
        canonical_begin,
        globals_overrides={"_sealed_product_utc_now": sealed_product_utc_now},
    )
    begin_snapshot = snapshot_globals(begin_clone)

    def guarded_begin(self, *args, **kwargs):
        if registry_type.begin is not guarded_begin:
            raise error_type("causal RunRegistry begin dispatch was rebound")
        if _run_registry._utc_now is not canonical_public_clock:
            raise error_type("product UTC clock authority was rebound")
        if _availability._sealed_product_utc_now is not sealed_product_utc_now:
            raise error_type("outcome availability clock sampler dispatch was rebound")
        require_snapshot(clock_clone, clock_snapshot, "product UTC clock")
        require_snapshot(begin_clone, begin_snapshot, "causal RunRegistry begin")
        return begin_clone(self, *args, **kwargs)

    guarded_begin._autosport_registry_rmw_serialized = True
    guarded_begin._autosport_outcome_two_phase = True
    guarded_begin._autosport_product_clock_sealed = True
    guarded_begin._autosport_clock_metadata_sealed = True

    _availability._sealed_product_utc_now = sealed_product_utc_now
    registry_type.begin = guarded_begin


_install_guard()
del _install_guard
