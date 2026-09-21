"""Fail closed if point-in-time authority implementations are replaced in-process.

The point-in-time holdout path requires exact concrete authority objects, but exact
``type(...)`` checks are not sufficient when a caller can replace methods on those
classes.  The authority-bearing identities in this module therefore live in closure
cells captured at import time rather than in caller-rebindable module globals.

Runtime-repair reload is guarded by two independent finder instances.  Only the
canonical finder is intentionally exposed for deterministic compatibility tests; the
backup finder, its class, and its loader class are retained solely by ``sys.meta_path``
and the installer closure.  Rebinding module aliases or replacing/removing the public
finder therefore cannot silently drop the seal.
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


def _make_reload_finder_installer(
    module_name: str,
    target_module: object,
    reinstall: Callable[[], None],
) -> tuple[importlib.abc.MetaPathFinder, Callable[[], None]]:
    """Create closure-owned canonical+backup reload guards.

    The backup implementation types deliberately never become module attributes.
    This makes assignment to the old/public finder, loader, repair-module, class, or
    ``importlib`` aliases irrelevant to the live backup path.
    """

    loader_base = importlib.abc.Loader
    finder_base = importlib.abc.MetaPathFinder
    path_finder = importlib.machinery.PathFinder
    system_module = sys

    class RepairReloadLoader(loader_base):
        def __init__(self, wrapped: importlib.abc.Loader) -> None:
            self._wrapped = wrapped

        def create_module(self, spec):
            create = getattr(self._wrapped, "create_module", None)
            if create is None:
                return None
            return create(spec)

        def exec_module(self, module) -> None:
            self._wrapped.exec_module(module)
            reinstall()

    class CanonicalRepairReloadFinder(finder_base):
        _autosport_point_in_time_class_dispatch_seal_v1 = True

        def find_spec(self, fullname, path, target=None):
            if fullname != module_name or target is not target_module:
                return None
            spec = path_finder.find_spec(fullname, path)
            if spec is None or spec.loader is None:
                return spec
            spec.loader = RepairReloadLoader(spec.loader)
            return spec

    class BackupRepairReloadFinder(finder_base):
        def find_spec(self, fullname, path, target=None):
            if fullname != module_name or target is not target_module:
                return None
            spec = path_finder.find_spec(fullname, path)
            if spec is None or spec.loader is None:
                return spec
            spec.loader = RepairReloadLoader(spec.loader)
            return spec

    canonical = CanonicalRepairReloadFinder()
    backup = BackupRepairReloadFinder()

    def install() -> None:
        meta_path = system_module.meta_path
        if not any(finder is backup for finder in meta_path):
            meta_path.insert(0, backup)
        if not any(finder is canonical for finder in meta_path):
            meta_path.insert(0, canonical)

    return canonical, install


_CANONICAL_REPAIR_RELOAD_FINDER, _install_reload_finders = _make_reload_finder_installer(
    repair.__name__,
    repair,
    _install_seals,
)

_install_seals()
_install_reload_finders()


__all__: list[str] = []
