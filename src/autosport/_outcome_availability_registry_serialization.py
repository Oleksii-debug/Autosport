"""Serialize RunRegistry read/modify/write authority transitions.

Outcome first-availability is authority-bearing state.  Atomic replacement keeps a
single JSON image intact, but it cannot prevent a writer that read an older image
from replacing a newer one later.  Reuse the canonical durable per-path lock over
the *entire* RunRegistry mutation so every writer reads the winner's current image
before deriving its successor.

This guard intentionally does not claim to solve the separate causal-publication
question of when a newly observed outcome may receive a positive
``first_available_at`` timestamp.  It only closes stale-snapshot/lost-update races.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable, TypeVar, cast

from .integrity import durable_path_lock
from . import run_registry as _run_registry

_F = TypeVar("_F", bound=Callable[..., object])
_MUTATION_METHODS = (
    "begin",
    "complete",
    "abort_uncommitted",
    "reconcile_completed_summary",
)


def _serialized(method: _F) -> _F:
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with durable_path_lock(self.path):
            return method(self, *args, **kwargs)

    return cast(_F, guarded)


for _method_name in _MUTATION_METHODS:
    _method = getattr(_run_registry.RunRegistry, _method_name)
    if not getattr(_method, "_autosport_registry_rmw_serialized", False):
        _guarded = _serialized(_method)
        setattr(_guarded, "_autosport_registry_rmw_serialized", True)
        setattr(_run_registry.RunRegistry, _method_name, _guarded)
