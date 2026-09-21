"""Preserve physical holdout semantics when the #716 runtime repair is reloaded.

``_point_in_time_class_dispatch_seal`` already owns a compatibility finder for an
explicit reload of ``_point_in_time_authority_runtime_repair``.  Compose that finder
rather than bypassing it: its loader first restores the class-dispatch seal, then this
loader rebinds the physical-content holdout guard to the freshly executed runtime
module.
"""

from __future__ import annotations

import importlib.abc
import sys

from . import _holdout_physical_content_guard as _physical
from . import _point_in_time_authority_runtime_repair as _runtime
from . import _point_in_time_class_dispatch_seal as _seal


_MARKER = "_autosport_holdout_physical_runtime_reload_v1"


def _rebind_after_runtime_reload() -> None:
    """Capture the fresh #716 runtime primitives, then reinstall physical semantics."""

    _physical._ORIGINAL_RUNTIME_INSTALL = _runtime._install_runtime_guards
    _physical._ORIGINAL_LEDGER_LOAD = _physical._evidence.HoldoutConsumptionLedger._load
    _runtime._install_runtime_guards = _physical._runtime_install_with_physical_guard
    _physical._install_physical_guards()


class _PhysicalRuntimeReloadLoader(importlib.abc.Loader):
    def __init__(self, wrapped: importlib.abc.Loader) -> None:
        self._wrapped = wrapped

    def create_module(self, spec):
        create = getattr(self._wrapped, "create_module", None)
        if create is None:
            return None
        return create(spec)

    def exec_module(self, module) -> None:
        # The wrapped loader is the existing class-dispatch-seal loader, so its
        # reinstall runs before physical holdout semantics are rebound here.
        self._wrapped.exec_module(module)
        _rebind_after_runtime_reload()


class _PhysicalRuntimeReloadFinder(importlib.abc.MetaPathFinder):
    _autosport_holdout_physical_runtime_reload_v1 = True

    def find_spec(self, fullname, path, target=None):
        if fullname != _runtime.__name__ or target is not _runtime:
            return None
        canonical_finder = _seal._CANONICAL_REPAIR_RELOAD_FINDER
        spec = canonical_finder.find_spec(fullname, path, target)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _PhysicalRuntimeReloadLoader(spec.loader)
        return spec


def _install() -> None:
    if any(getattr(finder, _MARKER, False) for finder in sys.meta_path):
        return
    # Run before the canonical seal finder, but delegate to that exact finder so
    # both compatibility layers execute in the established order.
    sys.meta_path.insert(0, _PhysicalRuntimeReloadFinder())


_install()

__all__: list[str] = []
