"""Runtime composition repair for source-owned feature publication authority.

The source publication ledger deliberately uses the existing crash-releasing
``WorkspaceEconomicLock`` for cross-process exclusion.  That lock is fail-fast by
contract, so two source materializers in the *same* Python process can otherwise
race and make an idempotent first publication spuriously fail with ``BusyError``.
This module adds only an in-process ``RLock`` around the source-authority use of the
existing OS lock; cross-process semantics and the canonical lock file are unchanged.

Some integrity tests explicitly reload the legacy point-in-time provenance guard.
That module historically reinstalled its own ``PointInTimeFeatureAuthority.bind``
on reload.  Once source-owned first-publication evidence is part of the product,
such a reload must not silently downgrade the public bind surface.  A narrow reload
hook therefore reapplies the source-aware binding after the legacy guard executes.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import threading

from . import _point_in_time_feature_provenance_guard as provenance_guard
from . import point_in_time_evidence as evidence
from . import source_feature_artifact_authority as source_authority
from .workspace_lock import WorkspaceEconomicLock as _WorkspaceEconomicLock


_PROCESS_SOURCE_PUBLICATION_LOCK = threading.RLock()


def _source_feature_materializer(
    self,
    *,
    lineage_authority,
):
    """Return a source-feature writer capability owned by this collector service."""

    return source_authority._materializer_from_headless_collector_service(
        service=self,
        lineage_authority=lineage_authority,
    )


class _SerializedSourceFeatureWorkspaceLock(_WorkspaceEconomicLock):
    """Serialize same-process source writers before taking the canonical OS lock."""

    def __init__(self, workspace) -> None:
        super().__init__(workspace)
        self._process_lock_owned = False

    def acquire(self) -> None:
        _PROCESS_SOURCE_PUBLICATION_LOCK.acquire()
        self._process_lock_owned = True
        try:
            super().acquire()
        except BaseException:
            self._process_lock_owned = False
            _PROCESS_SOURCE_PUBLICATION_LOCK.release()
            raise

    def release(self) -> None:
        process_lock_owned = self._process_lock_owned
        try:
            super().release()
        finally:
            if process_lock_owned:
                self._process_lock_owned = False
                _PROCESS_SOURCE_PUBLICATION_LOCK.release()


def _install_source_runtime_surface() -> None:
    from . import collector_service as collector_service_module

    # The writer capability is issued only by the canonical source runtime.  Keep
    # collector_service.py itself untouched so unrelated collector repair lineages
    # can reconverge independently.
    collector_service_module.HeadlessCollectorService.source_feature_materializer = (
        _source_feature_materializer
    )

    # SourceFeatureArtifactAuthority resolves this module global at lock use time.
    # Keep the durable cross-process lock implementation itself unchanged.
    source_authority.WorkspaceEconomicLock = _SerializedSourceFeatureWorkspaceLock
    evidence.PointInTimeFeatureAuthority.bind = staticmethod(
        source_authority._bind_with_source_authority
    )


class _ProvenanceReloadLoader(importlib.abc.Loader):
    def __init__(self, wrapped: importlib.abc.Loader) -> None:
        self._wrapped = wrapped

    def create_module(self, spec):
        create = getattr(self._wrapped, "create_module", None)
        if create is None:
            return None
        return create(spec)

    def exec_module(self, module) -> None:
        self._wrapped.exec_module(module)
        _install_source_runtime_surface()


class _ProvenanceReloadFinder(importlib.abc.MetaPathFinder):
    _autosport_source_feature_runtime_repair_v1 = True

    def find_spec(self, fullname, path, target=None):
        if fullname != provenance_guard.__name__ or target is not provenance_guard:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _ProvenanceReloadLoader(spec.loader)
        return spec


_RELOAD_FINDER = _ProvenanceReloadFinder()
_install_source_runtime_surface()
if not any(
    getattr(finder, "_autosport_source_feature_runtime_repair_v1", False)
    for finder in sys.meta_path
):
    sys.meta_path.insert(0, _RELOAD_FINDER)


__all__: list[str] = []
