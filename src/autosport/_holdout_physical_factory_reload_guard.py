"""Reinstall physical holdout consumption semantics after factory-impl reload.

The public strategy-model factory facade keeps a module reference to
``_strategy_model_factory_impl`` and constructs its staged ExperimentRunner from that
module at execution time.  Therefore an explicit reload of the implementation must
not restore the legacy access-id-only consumption comparison.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

from . import _holdout_physical_content_guard as _physical
from . import _strategy_model_factory_impl as _factory


_MARKER = "_autosport_holdout_physical_factory_reload_v1"


def _rebind_after_factory_reload() -> None:
    """Capture freshly executed factory originals before reinstalling the guard."""

    _physical._ORIGINAL_FACTORY_HELPER = _factory._holdout_consumed_by_other_evidence
    _physical._ORIGINAL_FACTORY_RUN = _factory.ExperimentRunner.run_baseline_candidate
    _physical._install_physical_guards()


class _PhysicalFactoryReloadLoader(importlib.abc.Loader):
    def __init__(self, wrapped: importlib.abc.Loader) -> None:
        self._wrapped = wrapped

    def create_module(self, spec):
        create = getattr(self._wrapped, "create_module", None)
        if create is None:
            return None
        return create(spec)

    def exec_module(self, module) -> None:
        self._wrapped.exec_module(module)
        _rebind_after_factory_reload()


class _PhysicalFactoryReloadFinder(importlib.abc.MetaPathFinder):
    _autosport_holdout_physical_factory_reload_v1 = True

    def find_spec(self, fullname, path, target=None):
        if fullname != _factory.__name__ or target is not _factory:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _PhysicalFactoryReloadLoader(spec.loader)
        return spec


def _install() -> None:
    if any(getattr(finder, _MARKER, False) for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _PhysicalFactoryReloadFinder())


_install()

__all__: list[str] = []
