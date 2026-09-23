from __future__ import annotations

"""Causal collector public surface with durable indexed delta persistence.

The original module body is retained verbatim in ``causal_collector_legacy`` so the
large established collector/desktop API remains byte-for-byte stable. This module
re-exports that surface and replaces only ``CollectorDeltaStore`` with the bounded
SQLite-backed implementation.
"""

from . import causal_collector_legacy as _legacy

for _name, _value in vars(_legacy).items():
    if not _name.startswith("__"):
        globals()[_name] = _value
    if getattr(_value, "__module__", None) == _legacy.__name__:
        try:
            _value.__module__ = __name__
        except (AttributeError, TypeError):
            pass

from .collector_sqlite_active_store import CollectorDeltaStore as CollectorDeltaStore
from .collector_sqlite_bounded_storage import (
    CollectorStorageBackpressureError,
    CollectorStorageBudgetError,
    install_collector_storage_budget,
)

# Keep the exact canonical class identity required by retention/desktop guards while
# installing the optional native SQLite allocation ceiling in place.
install_collector_storage_budget(CollectorDeltaStore)

from .collector_retention import (
    CollectorCompactionResult,
    CollectorRetentionError,
    CollectorRetentionManager,
    CollectorRetentionPlan,
    CollectorRetentionPlanStaleError,
    RetentionPinKind,
)

# Preserve the established public import/pickle identity for the replacement class.
CollectorDeltaStore.__module__ = __name__
# Legacy-defined callables resolve postponed annotations and any module-global store
# reference through their defining module. Point that global at the same canonical
# class exported here so reflection/runtime lookup cannot resurrect the JSON store.
_legacy.CollectorDeltaStore = CollectorDeltaStore

del _name, _value
