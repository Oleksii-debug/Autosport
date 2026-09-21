"""Fail closed if point-in-time authority implementations are replaced in-process.

The point-in-time holdout path requires exact concrete authority objects, but exact
``type(...)`` checks are not sufficient when a caller can replace methods on those
classes.  This module therefore installs wrappers whose trusted implementation
identity lives in private closure cells rather than caller-rewriteable module
snapshots/delegate globals.  Repair-module reload is guarded by two independent
finders; the backup finder is deliberately not published as the canonical finder so
removing/replacing that public hook cannot silently drop the seal.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
from types import MappingProxyType
from typing import Any, Callable

from . import _point_in_time_authority_runtime_repair as repair
from .dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from .scientific_registry import ScientificRegistry

_REPAIR_MODULE_NAME = repair.__name__


def _snapshot_class_namespace(concrete_type: type[Any]) -> tuple[tuple[str, object], ...]:
    """Capture exact class-owned attributes without invoking descriptors."""

    namespace = concrete_type.__dict__
    if not isinstance(namespace, MappingProxyType):
        raise RuntimeError("concrete authority class namespace is not immutable-view backed")
    return tuple(namespace.items())


def _build_seal() -> tuple[
    Callable[..., object],
    Callable[..., object],
    Callable[[], None],
]:
    """Build one closure-owned seal with no mutable expected-value globals."""

    trusted_lineage = _snapshot_class_namespace(DatasetSnapshotLineageAuthority)
    trusted_registry = _snapshot_class_namespace(ScientificRegistry)
    pristine_require = repair._require_exact_lineage_authority
    pristine_resolve = repair._resolve_canonical_snapshot

    def require_unchanged_class_namespace(
        concrete_type: type[Any],
        trusted: tuple[tuple[str, object], ...],
        *,
        label: str,
    ) -> None:
        current = concrete_type.__dict__
        expected_names = tuple(name for name, _ in trusted)
        current_names = tuple(current)
        if set(current_names) != set(expected_names):
            changed = sorted(set(current_names) ^ set(expected_names))
            detail = changed[0] if changed else "namespace"
            raise repair.evidence.PointInTimeEvidenceError(
                f"trusted {label} class implementation changed: {detail}"
            )
        for name, expected in trusted:
            if current[name] is not expected:
                raise repair.evidence.PointInTimeEvidenceError(
                    f"trusted {label} class implementation changed: {name}"
                )

    def require_trusted_class_dispatch() -> None:
        require_unchanged_class_namespace(
            DatasetSnapshotLineageAuthority,
            trusted_lineage,
            label="DatasetSnapshotLineageAuthority",
        )
        require_unchanged_class_namespace(
            ScientificRegistry,
            trusted_registry,
            label="ScientificRegistry",
        )

    def sealed_require_exact_lineage_authority(lineage: object, *, holdout: bool = False):
        require_trusted_class_dispatch()
        return pristine_require(lineage, holdout=holdout)

    def sealed_resolve_canonical_snapshot(ledger, dataset_snapshot):
        require_trusted_class_dispatch()
        return pristine_resolve(ledger, dataset_snapshot)

    def install() -> None:
        repair._require_exact_lineage_authority = sealed_require_exact_lineage_authority
        repair._resolve_canonical_snapshot = sealed_resolve_canonical_snapshot

    return sealed_require_exact_lineage_authority, sealed_resolve_canonical_snapshot, install


_SEALED_REQUIRE, _SEALED_RESOLVE, _INSTALL_SEALS = _build_seal()


class _RepairReloadLoader(importlib.abc.Loader):
    """Re-seal the authority functions after explicit runtime-repair reload."""

    def __init__(self, wrapped: importlib.abc.Loader, reinstall: Callable[[], None]) -> None:
        self._wrapped = wrapped
        self._reinstall = reinstall

    def create_module(self, spec):
        create = getattr(self._wrapped, "create_module", None)
        if create is None:
            return None
        return create(spec)

    def exec_module(self, module) -> None:
        self._wrapped.exec_module(module)
        # Use the loader-owned closure, not a mutable module-global installer.
        self._reinstall()


class _RepairReloadFinder(importlib.abc.MetaPathFinder):
    """Canonical public reload interceptor for the runtime repair module."""

    _autosport_point_in_time_class_dispatch_seal_v1 = True

    def __init__(self, reinstall: Callable[[], None]) -> None:
        self._reinstall = reinstall

    def find_spec(self, fullname, path, target=None):
        if fullname != _REPAIR_MODULE_NAME or target is not repair:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _RepairReloadLoader(spec.loader, self._reinstall)
        return spec


class _BackupRepairReloadFinder(importlib.abc.MetaPathFinder):
    """Independent fallback if the canonical public finder is removed/replaced."""

    def __init__(self, reinstall: Callable[[], None]) -> None:
        self._reinstall = reinstall

    def find_spec(self, fullname, path, target=None):
        if fullname != _REPAIR_MODULE_NAME or target is not repair:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _RepairReloadLoader(spec.loader, self._reinstall)
        return spec


_CANONICAL_REPAIR_RELOAD_FINDER = _RepairReloadFinder(_INSTALL_SEALS)
# Do not export/store the backup under the canonical finder name.  Its live object is
# retained by sys.meta_path itself, so rewriting the public canonical reference does
# not rewrite the fallback's closure-owned reinstall capability.
_BACKUP_REPAIR_RELOAD_FINDER = _BackupRepairReloadFinder(_INSTALL_SEALS)


def _install_reload_finders() -> None:
    if not any(finder is _BACKUP_REPAIR_RELOAD_FINDER for finder in sys.meta_path):
        sys.meta_path.insert(0, _BACKUP_REPAIR_RELOAD_FINDER)
    if not any(finder is _CANONICAL_REPAIR_RELOAD_FINDER for finder in sys.meta_path):
        sys.meta_path.insert(0, _CANONICAL_REPAIR_RELOAD_FINDER)


_INSTALL_SEALS()
_install_reload_finders()


__all__: list[str] = []
