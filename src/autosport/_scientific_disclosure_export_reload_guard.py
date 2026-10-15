"""Preserve the scientific outcome-export fence across explicit registry reloads.

The disclosure exporter deliberately patches only the legacy outward file-export
entrypoint.  An explicit ``importlib.reload(scientific_registry)`` must not restore
that unfenced method and reopen outcome disclosure without holdout consumption.
This compatibility finder has no positive scientific authority: it only reapplies
the fail-closed direct-export stub after the canonical source module executes.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

from . import scientific_disclosure_export as _export
from . import scientific_registry as _registry


_MARKER = "_autosport_scientific_disclosure_export_reload_guard_v1"


class _RegistryReloadLoader(importlib.abc.Loader):
    def __init__(self, wrapped: importlib.abc.Loader) -> None:
        self._wrapped = wrapped

    def create_module(self, spec):
        create = getattr(self._wrapped, "create_module", None)
        if create is None:
            return None
        return create(spec)

    def exec_module(self, module) -> None:
        self._wrapped.exec_module(module)
        registry_type = getattr(module, "ScientificRegistry", None)
        if not isinstance(registry_type, type):
            raise RuntimeError(
                "scientific registry reload did not restore ScientificRegistry"
            )
        registry_type.export_reproducibility_bundle = _export._blocked_direct_export


class _RegistryReloadFinder(importlib.abc.MetaPathFinder):
    _autosport_scientific_disclosure_export_reload_guard_v1 = True

    def find_spec(self, fullname, path, target=None):
        if fullname != _registry.__name__ or target is not _registry:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _RegistryReloadLoader(spec.loader)
        return spec


def _install() -> None:
    if any(getattr(finder, _MARKER, False) for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _RegistryReloadFinder())


_install()

__all__: list[str] = []
