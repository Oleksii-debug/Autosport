"""Seal product outcome-availability clock dispatch against function-metadata rebinding.

The two-phase RunRegistry outcome publication is causal only if its post-publication
clock sampler cannot be replaced.  Python function defaults, globals and closure-
reachable predecessor functions are mutable authority surfaces.  This composition
therefore keeps the already-canonical begin/clock implementations behind checked
callable objects and exposes only one tiny public method wrapper whose function
metadata contains no authority-bearing predecessor ``FunctionType``.

The checked objects validate the exact import-time public clock identity, the exact
installed sampler/begin identities and every directly-read global binding of the
private clones before delegating.  No second registry or availability authority is
introduced.
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

    class CheckedClock:
        """Callable clock boundary with no raw clock function in public metadata."""

        __slots__ = (
            "_function",
            "_snapshot",
            "_run_registry_module",
            "_canonical_public_clock",
            "_error_type",
        )

        def __init__(
            self,
            function: FunctionType,
            snapshot: tuple[tuple[str, object], ...],
            run_registry_module,
            canonical_clock: FunctionType,
            error,
        ) -> None:
            self._function = function
            self._snapshot = snapshot
            self._run_registry_module = run_registry_module
            self._canonical_public_clock = canonical_clock
            self._error_type = error

        def __call__(self) -> str:
            if self._run_registry_module._utc_now is not self._canonical_public_clock:
                raise self._error_type("product UTC clock authority was rebound")
            globals_dict = self._function.__globals__
            for name, expected in self._snapshot:
                if globals_dict.get(name, self) is not expected:
                    raise self._error_type(
                        f"frozen product UTC clock global {name!r} was rebound"
                    )
            return self._function()

    # Freeze the canonical formatter over its import-time datetime/timezone objects.
    # The raw clone is retained only inside CheckedClock, not in any installed
    # FunctionType closure/default/wrapped chain.
    clock_clone = clone_function(canonical_public_clock)
    checked_clock = CheckedClock(
        clock_clone,
        snapshot_globals(clock_clone),
        _run_registry,
        canonical_public_clock,
        error_type,
    )
    del clock_clone

    # Clone the already-canonical two-phase begin and bind it to the checked clock.
    # The raw begin clone will likewise live only inside CheckedBegin below.
    begin_clone = clone_function(
        canonical_begin,
        globals_overrides={"_sealed_product_utc_now": checked_clock},
    )
    begin_snapshot = snapshot_globals(begin_clone)

    class CheckedBegin:
        """Self-checking causal begin implementation hidden behind one public wrapper."""

        __slots__ = (
            "_function",
            "_snapshot",
            "_registry_type",
            "_run_registry_module",
            "_availability_module",
            "_canonical_public_clock",
            "_clock",
            "_error_type",
            "_public_wrapper",
        )

        def __init__(
            self,
            function: FunctionType,
            snapshot: tuple[tuple[str, object], ...],
            registry,
            run_registry_module,
            availability_module,
            canonical_clock: FunctionType,
            clock,
            error,
        ) -> None:
            self._function = function
            self._snapshot = snapshot
            self._registry_type = registry
            self._run_registry_module = run_registry_module
            self._availability_module = availability_module
            self._canonical_public_clock = canonical_clock
            self._clock = clock
            self._error_type = error
            self._public_wrapper = None

        def bind_public_wrapper(self, wrapper: FunctionType) -> None:
            if self._public_wrapper is not None:
                raise RuntimeError("causal RunRegistry begin wrapper was already bound")
            self._public_wrapper = wrapper

        def __call__(self, registry, *args, **kwargs):
            if self._registry_type.begin is not self._public_wrapper:
                raise self._error_type("causal RunRegistry begin dispatch was rebound")
            if self._run_registry_module._utc_now is not self._canonical_public_clock:
                raise self._error_type("product UTC clock authority was rebound")
            if self._availability_module._sealed_product_utc_now is not self._clock:
                raise self._error_type(
                    "outcome availability clock sampler dispatch was rebound"
                )
            globals_dict = self._function.__globals__
            for name, expected in self._snapshot:
                if globals_dict.get(name, self) is not expected:
                    raise self._error_type(
                        f"frozen causal RunRegistry begin global {name!r} was rebound"
                    )
            return self._function(registry, *args, **kwargs)

    checked_begin = CheckedBegin(
        begin_clone,
        begin_snapshot,
        registry_type,
        _run_registry,
        _availability,
        canonical_public_clock,
        checked_clock,
        error_type,
    )
    del begin_clone
    del begin_snapshot

    def guarded_begin(self, *args, **kwargs):
        # Deliberately retain only a non-FunctionType checked boundary in this
        # closure.  Recursive ordinary function-metadata traversal cannot recover
        # and invoke the authority-bearing predecessor/clone directly.
        return checked_begin(self, *args, **kwargs)

    checked_begin.bind_public_wrapper(guarded_begin)
    guarded_begin._autosport_registry_rmw_serialized = True
    guarded_begin._autosport_outcome_two_phase = True
    guarded_begin._autosport_product_clock_sealed = True
    guarded_begin._autosport_clock_metadata_sealed = True
    guarded_begin._autosport_predecessor_unreachable = True

    _availability._sealed_product_utc_now = checked_clock
    registry_type.begin = guarded_begin


_install_guard()
del _install_guard
