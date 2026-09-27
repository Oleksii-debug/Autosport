"""Seal owner-facing PaperRiskPolicy entrypoints against class replacement/deletion.

All risk logic remains on the existing canonical PaperRiskPolicy implementation.  This
final composition step publishes one zero-state subclass facade with a metaclass that
rejects mutation of only the owner-facing decision/sizing roots.  Internal helpers stay
mutable so the existing adversarial helper-rebind tests can continue proving that the
underlying generation/helper/private-authority seals fail closed.

The module is installed during the early PAPER composition before later product modules
import PaperRiskPolicy, so one canonical exported class identity is used downstream.
"""

from __future__ import annotations

from types import FunctionType

from . import risk as _risk


_PROTECTED_ROOTS = (
    "derive_goal_stake",
    "derive_goal_stake_vector",
    "evaluate",
)


def _make_sealed_meta(base_meta: type, protected_names: frozenset[str]) -> type:
    class _SealedPaperRiskPolicyMeta(base_meta):
        def __setattr__(cls, name: str, value: object) -> None:
            if name in protected_names:
                raise TypeError(f"canonical PaperRiskPolicy root is sealed: {name}")
            super().__setattr__(name, value)

        def __delattr__(cls, name: str) -> None:
            if name in protected_names:
                raise TypeError(f"canonical PaperRiskPolicy root is sealed: {name}")
            super().__delattr__(name)

    return _SealedPaperRiskPolicyMeta


def _install() -> None:
    canonical = _risk.PaperRiskPolicy
    canonical_namespace = vars(canonical)
    root_descriptors: dict[str, object] = {}
    for name in _PROTECTED_ROOTS:
        descriptor = canonical_namespace.get(name)
        if type(descriptor) is not FunctionType:
            raise RuntimeError(f"canonical PaperRiskPolicy root changed before sealing: {name}")
        root_descriptors[name] = descriptor

    protected_names = frozenset(root_descriptors)
    sealed_meta = _make_sealed_meta(type(canonical), protected_names)
    namespace: dict[str, object] = {
        "__module__": canonical.__module__,
        "__qualname__": canonical.__qualname__,
        "__doc__": canonical.__doc__,
        "__slots__": (),
        **root_descriptors,
    }
    sealed = sealed_meta(canonical.__name__, (canonical,), namespace)

    # Preserve one downstream module export.  The original class remains only as the
    # implementation base of this zero-state facade; no parallel policy state or logic
    # is introduced.
    _risk.PaperRiskPolicy = sealed


_install()
del _install
