from __future__ import annotations

"""Fail-closed public authority guard for durable sport-memory state.

``sport_memory_runtime`` intentionally contains the low-level deterministic state
machine used by the canonical checkpoint composition.  This module narrows the
supported public durable write path to the bound product runtime and adds a
load-time generation fence for every persisted artifact.  It mirrors the small
import-time guard pattern already used by other Autosport durability authorities.
"""

from . import sport_memory_runtime as _impl


_ORIGINAL_MATERIALIZE = _impl.SportMemoryRuntime.materialize
_ORIGINAL_LOAD = _impl.SportMemoryRuntime._load
_BOUND_RUNTIME_MODULE = "autosport.sport_memory_checkpoint"
_BOUND_RUNTIME_NAME = "BoundSportMemoryRuntime"


def _is_bound_product_runtime(runtime: object) -> bool:
    runtime_type = type(runtime)
    return (
        runtime_type.__module__ == _BOUND_RUNTIME_MODULE
        and runtime_type.__name__ == _BOUND_RUNTIME_NAME
    )


def _guarded_materialize(self, *args, **kwargs):
    if not _is_bound_product_runtime(self):
        raise _impl.SportMemoryError(
            "durable sport-memory materialization requires canonical bound authority"
        )
    return _ORIGINAL_MATERIALIZE(self, *args, **kwargs)


def _guarded_load(self) -> None:
    _ORIGINAL_LOAD(self)
    for artifact in self._artifacts.values():
        if artifact.authority_generation_sha256 != self.authority_generation_sha256:
            raise _impl.SportMemoryError(
                "artifact authority generation does not match runtime authority generation"
            )


_impl.SportMemoryRuntime.materialize = _guarded_materialize
_impl.SportMemoryRuntime._load = _guarded_load
