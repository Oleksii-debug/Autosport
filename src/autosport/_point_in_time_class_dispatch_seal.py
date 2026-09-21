"""Fail closed if point-in-time authority classes are replaced in-process.

The point-in-time holdout path deliberately requires exact concrete authority
objects.  Exact ``type(...)`` and instance-shadow checks are insufficient when a
caller can replace a method on the concrete class itself after a valid authority
object has been constructed.  This guard freezes the concrete class namespaces
used by canonical lineage resolution and re-applies the seal after the runtime
repair module is explicitly reloaded.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
from types import MappingProxyType
from typing import Any

from . import _point_in_time_authority_runtime_repair as repair
from .dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from .scientific_registry import ScientificRegistry

_REPAIR_MODULE_NAME = repair.__name__


def _snapshot_class_namespace(concrete_type: type[Any]) -> dict[str, object]:
    """Capture exact class-owned attributes without invoking descriptors."""

    namespace = concrete_type.__dict__
    if not isinstance(namespace, MappingProxyType):
        raise RuntimeError("concrete authority class namespace is not immutable-view backed")
    return dict(namespace)


if "_TRUSTED_LINEAGE_NAMESPACE" not in globals():
    _TRUSTED_LINEAGE_NAMESPACE = _snapshot_class_namespace(
        DatasetSnapshotLineageAuthority
    )
if "_TRUSTED_REGISTRY_NAMESPACE" not in globals():
    _TRUSTED_REGISTRY_NAMESPACE = _snapshot_class_namespace(ScientificRegistry)


def _capture_pristine_repair_delegates() -> None:
    """Capture delegates immediately after a pristine repair-module execution."""

    global _PRISTINE_REQUIRE_EXACT_LINEAGE_AUTHORITY
    global _PRISTINE_RESOLVE_CANONICAL_SNAPSHOT
    _PRISTINE_REQUIRE_EXACT_LINEAGE_AUTHORITY = repair._require_exact_lineage_authority
    _PRISTINE_RESOLVE_CANONICAL_SNAPSHOT = repair._resolve_canonical_snapshot


if "_PRISTINE_REQUIRE_EXACT_LINEAGE_AUTHORITY" not in globals():
    _capture_pristine_repair_delegates()


def _require_unchanged_class_namespace(
    concrete_type: type[Any],
    trusted: dict[str, object],
    *,
    label: str,
) -> None:
    """Reject class-level replacement/addition/removal before authority dispatch."""

    current = dict(concrete_type.__dict__)
    if set(current) != set(trusted):
        changed = sorted(set(current) ^ set(trusted))
        detail = changed[0] if changed else "namespace"
        raise repair.evidence.PointInTimeEvidenceError(
            f"trusted {label} class implementation changed: {detail}"
        )
    for name, expected in trusted.items():
        if current[name] is not expected:
            raise repair.evidence.PointInTimeEvidenceError(
                f"trusted {label} class implementation changed: {name}"
            )


def _require_trusted_class_dispatch() -> None:
    _require_unchanged_class_namespace(
        DatasetSnapshotLineageAuthority,
        _TRUSTED_LINEAGE_NAMESPACE,
        label="DatasetSnapshotLineageAuthority",
    )
    _require_unchanged_class_namespace(
        ScientificRegistry,
        _TRUSTED_REGISTRY_NAMESPACE,
        label="ScientificRegistry",
    )


def _sealed_require_exact_lineage_authority(lineage: object, *, holdout: bool = False):
    _require_trusted_class_dispatch()
    return _PRISTINE_REQUIRE_EXACT_LINEAGE_AUTHORITY(lineage, holdout=holdout)


def _sealed_resolve_canonical_snapshot(ledger, dataset_snapshot):
    _require_trusted_class_dispatch()
    return _PRISTINE_RESOLVE_CANONICAL_SNAPSHOT(ledger, dataset_snapshot)


def _install_seals() -> None:
    repair._require_exact_lineage_authority = _sealed_require_exact_lineage_authority
    repair._resolve_canonical_snapshot = _sealed_resolve_canonical_snapshot


class _RepairReloadLoader(importlib.abc.Loader):
    """Re-seal the authority functions after explicit runtime-repair reload."""

    def __init__(self, wrapped: importlib.abc.Loader) -> None:
        self._wrapped = wrapped

    def create_module(self, spec):
        create = getattr(self._wrapped, "create_module", None)
        if create is None:
            return None
        return create(spec)

    def exec_module(self, module) -> None:
        self._wrapped.exec_module(module)
        _capture_pristine_repair_delegates()
        _install_seals()


class _RepairReloadFinder(importlib.abc.MetaPathFinder):
    """Intercept only explicit reload of the already-loaded runtime repair."""

    _autosport_point_in_time_class_dispatch_seal_v1 = True

    def find_spec(self, fullname, path, target=None):
        if fullname != _REPAIR_MODULE_NAME or target is not repair:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _RepairReloadLoader(spec.loader)
        return spec


if "_CANONICAL_REPAIR_RELOAD_FINDER" not in globals():
    _CANONICAL_REPAIR_RELOAD_FINDER = _RepairReloadFinder()


def _install_reload_finder() -> None:
    if any(finder is _CANONICAL_REPAIR_RELOAD_FINDER for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _CANONICAL_REPAIR_RELOAD_FINDER)


_install_seals()
_install_reload_finder()


__all__: list[str] = []
