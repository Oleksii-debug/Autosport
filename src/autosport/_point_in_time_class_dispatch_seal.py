"""Fail closed if point-in-time authority implementations are replaced in-process.

The point-in-time holdout path requires exact concrete authority objects, but exact
``type(...)`` checks are not sufficient when a caller can replace methods on those
classes.  The authority-bearing identities in this module therefore live in closure
cells captured at import time rather than in caller-rebindable module globals.

Runtime-repair reload is guarded by two independent finder instances.  Only the
canonical finder is intentionally exposed for deterministic compatibility tests; the
backup finder is retained solely by ``sys.meta_path`` and the installer closure, so
rewriting/removing the public finder reference cannot silently drop the seal.
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


def _snapshot_class_namespace(concrete_type: type[Any]) -> tuple[tuple[str, object], ...]:
    """Capture exact class-owned attributes without invoking descriptors."""

    namespace = concrete_type.__dict__
    if not isinstance(namespace, MappingProxyType):
        raise RuntimeError("concrete authority class namespace is not immutable-view backed")
    return tuple(namespace.items())


def _build_seal(
    repair_module,
    lineage_type: type[Any],
    registry_type: type[Any],
) -> tuple[Callable[..., object], Callable[..., object], Callable[[], None]]:
    """Build one closure-owned seal with no mutable expected-value globals."""

    trusted_lineage = _snapshot_class_namespace(lineage_type)
    trusted_registry = _snapshot_class_namespace(registry_type)
    pristine_require = repair_module._require_exact_lineage_authority
    pristine_resolve = repair_module._resolve_canonical_snapshot
    error_type = repair_module.evidence.PointInTimeEvidenceError

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
            raise error_type(f"trusted {label} class implementation changed: {detail}")
        for name, expected in trusted:
            if current[name] is not expected:
                raise error_type(f"trusted {label} class implementation changed: {name}")

    def require_trusted_class_dispatch() -> None:
        require_unchanged_class_namespace(
            lineage_type,
            trusted_lineage,
            label="DatasetSnapshotLineageAuthority",
        )
        require_unchanged_class_namespace(
            registry_type,
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
        repair_module._require_exact_lineage_authority = sealed_require_exact_lineage_authority
        repair_module._resolve_canonical_snapshot = sealed_resolve_canonical_snapshot

    return sealed_require_exact_lineage_authority, sealed_resolve_canonical_snapshot, install


_sealed_require_exact_lineage_authority, _sealed_resolve_canonical_snapshot, _install_seals = (
    _build_seal(repair, DatasetSnapshotLineageAuthority, ScientificRegistry)
)


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
        # Use the loader-owned closure, never a mutable module-global installer lookup.
        self._reinstall()


class _RepairReloadFinder(importlib.abc.MetaPathFinder):
    """Canonical public reload interceptor for the runtime repair module."""

    _autosport_point_in_time_class_dispatch_seal_v1 = True

    def __init__(
        self,
        module_name: str,
        target_module: object,
        reinstall: Callable[[], None],
    ) -> None:
        self._module_name = module_name
        self._target_module = target_module
        self._reinstall = reinstall

    def find_spec(self, fullname, path, target=None):
        if fullname != self._module_name or target is not self._target_module:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _RepairReloadLoader(spec.loader, self._reinstall)
        return spec


class _BackupRepairReloadFinder(importlib.abc.MetaPathFinder):
    """Independent fallback if the canonical public finder is removed/replaced."""

    def __init__(
        self,
        module_name: str,
        target_module: object,
        reinstall: Callable[[], None],
    ) -> None:
        self._module_name = module_name
        self._target_module = target_module
        self._reinstall = reinstall

    def find_spec(self, fullname, path, target=None):
        if fullname != self._module_name or target is not self._target_module:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _RepairReloadLoader(spec.loader, self._reinstall)
        return spec


def _make_reload_finder_installer(
    module_name: str,
    target_module: object,
    reinstall: Callable[[], None],
) -> tuple[_RepairReloadFinder, Callable[[], None]]:
    canonical = _RepairReloadFinder(module_name, target_module, reinstall)
    backup = _BackupRepairReloadFinder(module_name, target_module, reinstall)

    def install() -> None:
        # ``backup`` is deliberately closure-owned: it cannot be replaced by assigning
        # a similarly named attribute on this module.
        if not any(finder is backup for finder in sys.meta_path):
            sys.meta_path.insert(0, backup)
        if not any(finder is canonical for finder in sys.meta_path):
            sys.meta_path.insert(0, canonical)

    return canonical, install


_CANONICAL_REPAIR_RELOAD_FINDER, _install_reload_finders = _make_reload_finder_installer(
    repair.__name__,
    repair,
    _install_seals,
)

_install_seals()
_install_reload_finders()


__all__: list[str] = []
