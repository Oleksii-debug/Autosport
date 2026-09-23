from __future__ import annotations

"""Fail-closed public authority guard for durable sport-memory state.

``sport_memory_runtime`` intentionally contains the low-level deterministic state
machine used by the canonical checkpoint composition. This module narrows the
supported public durable write path to the bound product runtime and adds a
load-time generation fence for every persisted artifact. The base runtime remains
an implementation detail: even an exact bound instance may publish or consume only
while its canonical ``BoundSportMemoryRuntime`` override is the immediate caller,
so explicit base dispatch cannot skip fresh checkpoint/source-root verification.
"""

import inspect

from . import sport_memory_runtime as _impl


_ORIGINAL_MATERIALIZE = _impl.SportMemoryRuntime.materialize
_ORIGINAL_RECORD_CONSUMPTION = _impl.SportMemoryRuntime.record_consumption
_ORIGINAL_LOAD = _impl.SportMemoryRuntime._load


def _is_bound_product_runtime(runtime: object) -> bool:
    # Resolve lazily so importing this guard from autosport.__init__ does not
    # create a cycle while sport_memory_checkpoint is still being defined. Exact
    # type identity is required: module/name strings are mutable and a subclass
    # must not be able to spoof product authority.
    from .sport_memory_checkpoint import BoundSportMemoryRuntime

    return type(runtime) is BoundSportMemoryRuntime


def _called_from_bound_method(runtime: object, method_name: str) -> bool:
    frame = inspect.currentframe()
    caller = frame.f_back.f_back if frame is not None and frame.f_back is not None else None
    try:
        if caller is None or caller.f_locals.get("self") is not runtime:
            return False
        # Resolve lazily so importing this guard from autosport.__init__ does not
        # create a cycle while sport_memory_checkpoint is still being defined.
        from .sport_memory_checkpoint import BoundSportMemoryRuntime

        method = getattr(BoundSportMemoryRuntime, method_name)
        return caller.f_code is method.__code__
    finally:
        # Frame objects participate in reference cycles; drop local references at
        # this durable boundary instead of retaining product/runtime state.
        del caller
        del frame


def _guarded_materialize(self, *args, **kwargs):
    if not _is_bound_product_runtime(self) or not _called_from_bound_method(
        self, "materialize"
    ):
        raise _impl.SportMemoryError(
            "durable sport-memory materialization requires canonical bound authority"
        )
    return _ORIGINAL_MATERIALIZE(self, *args, **kwargs)


def _guarded_record_consumption(self, *args, **kwargs):
    # Durable decision-use evidence is product authority just like materialized
    # memory. Low-level runtimes remain available only through explicit test seams;
    # the public/base/subclass path must never persist consumption without the
    # canonical bound wrapper revalidating the current checkpoint/source roots.
    if not _is_bound_product_runtime(self) or not _called_from_bound_method(
        self, "record_consumption"
    ):
        raise _impl.SportMemoryError(
            "bound sport-memory consumption requires canonical generation verification"
        )
    return _ORIGINAL_RECORD_CONSUMPTION(self, *args, **kwargs)


def _guarded_load(self) -> None:
    _ORIGINAL_LOAD(self)
    for artifact in self._artifacts.values():
        if artifact.authority_generation_sha256 != self.authority_generation_sha256:
            raise _impl.SportMemoryError(
                "artifact authority generation does not match runtime authority generation"
            )


_impl.SportMemoryRuntime.materialize = _guarded_materialize
_impl.SportMemoryRuntime.record_consumption = _guarded_record_consumption
_impl.SportMemoryRuntime._load = _guarded_load
