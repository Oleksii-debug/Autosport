"""Compatibility reload guard for intrinsic point-in-time dispatch checks.

The authoritative point-in-time class-dispatch protection now lives directly in
``_point_in_time_authority_runtime_repair`` and revalidates the concrete authority
implementation against its canonical source before authority-bearing dispatch.
This module deliberately keeps no pristine delegate, class-namespace snapshot, or
other authority-bearing value in Python closure cells.

The two reload finders are retained only for compatibility: when present they refresh
these public test aliases after an explicit repair-module reload.  Removing either or
both finders cannot weaken the product authority because repair re-execution itself
restores the intrinsic source-backed checks.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
from typing import Callable

from . import _point_in_time_authority_runtime_repair as repair


_sealed_require_exact_lineage_authority = repair._require_exact_lineage_authority
_sealed_resolve_canonical_snapshot = repair._resolve_canonical_snapshot


def _install_seals() -> None:
    """Refresh compatibility aliases without patching authority-bearing dispatch."""

    global _sealed_require_exact_lineage_authority
    global _sealed_resolve_canonical_snapshot

    require = repair._require_exact_lineage_authority
    resolve = repair._resolve_canonical_snapshot
    if getattr(require, "__closure__", None) is not None:
        raise RuntimeError(
            "intrinsic point-in-time lineage guard unexpectedly captured closure state"
        )
    if getattr(resolve, "__closure__", None) is not None:
        raise RuntimeError(
            "intrinsic point-in-time snapshot resolver unexpectedly captured closure state"
        )
    _sealed_require_exact_lineage_authority = require
    _sealed_resolve_canonical_snapshot = resolve


def _make_reload_finder_installer(
    module_name: str,
    target_module: object,
    reinstall: Callable[[], None],
) -> tuple[importlib.abc.MetaPathFinder, Callable[[], None]]:
    """Create canonical+backup compatibility reload hooks.

    These hooks are not a trust root.  Their only responsibility is keeping the
    compatibility aliases above synchronized after a normal explicit reload.
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
