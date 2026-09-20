from __future__ import annotations

"""Fail-closed exact-type admission at the canonical policy utility store.

``PolicyUtilityStore`` is the durable mutation authority for schema-v1 utility
records. A subclass must never reach semantic-key evaluation or serialization:
its overridable properties/``to_dict`` methods could otherwise influence the
successor image and only be rejected after that image was durably published.

The package also treats a public-module reload as an ordinary supported Python
operation. ``importlib.reload(autosport.policy_utility_evidence)`` replaces the
class objects defined by that module, so a one-shot monkeypatch is insufficient.
A narrow reload finder wraps the canonical source loader and reinstalls the same
fail-before-mutation fence after each reload.
"""

from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import PathFinder
import sys
from types import ModuleType
from typing import Any

from . import policy_utility_evidence as _utility


_MODULE_NAME = _utility.__name__
_FENCE_MARKER = "__autosport_policy_utility_exact_type_fence__"


def _install_exact_type_fence(module: ModuleType) -> None:
    original_append = module.PolicyUtilityStore.append
    if getattr(original_append, _FENCE_MARKER, False):
        return

    def append_exact_policy_utility(self: Any, evidence: Any) -> bool:
        if type(evidence) is not module.PolicyUtilityEvidence:
            raise module.PolicyUtilityError(
                "append requires exact PolicyUtilityEvidence"
            )
        return original_append(self, evidence)

    setattr(append_exact_policy_utility, _FENCE_MARKER, True)
    module.PolicyUtilityStore.append = append_exact_policy_utility


class _PolicyUtilityReloadLoader(Loader):
    def __init__(self, delegate: Loader) -> None:
        self._delegate = delegate

    def create_module(self, spec):
        create_module = getattr(self._delegate, "create_module", None)
        if create_module is None:
            return None
        return create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        exec_module = getattr(self._delegate, "exec_module", None)
        if exec_module is None:
            raise ImportError("policy utility source loader cannot execute module")
        exec_module(module)
        _install_exact_type_fence(module)


class _PolicyUtilityReloadFinder(MetaPathFinder):
    def find_spec(self, fullname: str, path, target=None):
        if fullname != _MODULE_NAME or target is None:
            return None
        spec = PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        if not isinstance(spec.loader, _PolicyUtilityReloadLoader):
            spec.loader = _PolicyUtilityReloadLoader(spec.loader)
        return spec


def _install_reload_finder() -> None:
    # Never trust a caller-defined marker or duck type here. A process may populate
    # sys.meta_path before importing autosport, so only this exact private class can
    # satisfy the installed-finder check. The canonical instance is inserted first;
    # unrelated/fake finders remain ordinary import machinery behind it.
    if any(type(finder) is _PolicyUtilityReloadFinder for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _PolicyUtilityReloadFinder())


_install_exact_type_fence(_utility)
_install_reload_finder()


__all__: list[str] = []
