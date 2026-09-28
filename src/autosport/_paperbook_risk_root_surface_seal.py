"""Seal owner-facing PaperRiskPolicy roots without replacing canonical class identity.

``risk.PaperRiskPolicy`` is created at its source definition with a dedicated metaclass.
Generation/private-authority composition is intentionally allowed to replace the three
owner-facing Python functions and the private classmethod book-state root before this final step. We then install data descriptors
on that *existing* metaclass. Python's class-assignment path, including an explicit
``type.__setattr__`` / ``type.__delattr__`` call, must pass through those descriptors,
so the finalized class-dict functions cannot be replaced or deleted afterward.

The canonical PaperRiskPolicy class object and ``__bases__ == (object,)`` are preserved;
no policy facade, sibling engine, state, store or second risk authority is created.
"""

from __future__ import annotations

from types import FunctionType

from . import risk as _risk


_PROTECTED_ROOTS = (
    "_book_state",
    "derive_goal_stake",
    "derive_goal_stake_vector",
    "evaluate",
)


class _SealedClassRoot:
    __slots__ = ("_name", "_descriptor", "_owner")

    def __init__(self, name: str, descriptor: object, owner: type) -> None:
        self._name = name
        self._descriptor = descriptor
        self._owner = owner

    def __get__(self, instance: object, owner: type | None = None):
        del owner
        if instance is None:
            return self
        namespace = type.__getattribute__(self._owner, "__dict__")
        if namespace.get(self._name) is not self._descriptor:
            raise TypeError(f"canonical PaperRiskPolicy root changed: {self._name}")
        getter = getattr(self._descriptor, "__get__", None)
        if getter is None:
            return self._descriptor
        return getter(None, self._owner)

    def __set__(self, instance: object, value: object) -> None:
        del instance, value
        raise TypeError(f"canonical PaperRiskPolicy root is sealed: {self._name}")

    def __delete__(self, instance: object) -> None:
        del instance
        raise TypeError(f"canonical PaperRiskPolicy root is sealed: {self._name}")


def _install() -> None:
    policy = _risk.PaperRiskPolicy
    if policy.__bases__ != (object,):
        raise RuntimeError("canonical PaperRiskPolicy identity changed before root sealing")
    meta = type(policy)
    if meta is type or meta.__module__ != _risk.__name__:
        raise RuntimeError("canonical PaperRiskPolicy source metaclass is unavailable")

    namespace = type.__getattribute__(policy, "__dict__")
    root_descriptors: dict[str, object] = {}
    for name in _PROTECTED_ROOTS:
        descriptor = namespace.get(name)
        if name == "_book_state":
            if (
                type(descriptor) is not classmethod
                or type(descriptor.__func__) is not FunctionType
            ):
                raise RuntimeError(
                    f"canonical PaperRiskPolicy root changed before sealing: {name}"
                )
        elif type(descriptor) is not FunctionType:
            raise RuntimeError(f"canonical PaperRiskPolicy root changed before sealing: {name}")
        root_descriptors[name] = descriptor

    def reject_subclass(cls, **kwargs) -> None:
        del cls, kwargs
        raise TypeError("canonical PaperRiskPolicy is not extensible")

    # Install the subclass fence while root assignment is still intentionally open,
    # then protect that exact classmethod through the same metaclass data-descriptor
    # mechanism as the owner-facing roots.
    type.__setattr__(policy, "__init_subclass__", classmethod(reject_subclass))
    subclass_descriptor = type.__getattribute__(policy, "__dict__").get("__init_subclass__")
    if type(subclass_descriptor) is not classmethod:
        raise RuntimeError("canonical PaperRiskPolicy subclass fence was not installed")

    sealed = {
        name: _SealedClassRoot(name, descriptor, policy)
        for name, descriptor in root_descriptors.items()
    }
    sealed["__init_subclass__"] = _SealedClassRoot(
        "__init_subclass__",
        subclass_descriptor,
        policy,
    )
    for name, descriptor in sealed.items():
        type.__setattr__(meta, name, descriptor)

    # Refuse a partially installed seal. Class access exercises metaclass descriptor
    # lookup while vars(policy) proves the finalized class-dict function identity stayed
    # untouched.
    current_namespace = type.__getattribute__(policy, "__dict__")
    for name, expected in root_descriptors.items():
        if current_namespace.get(name) is not expected:
            raise RuntimeError(f"canonical PaperRiskPolicy root moved during sealing: {name}")
        resolved = getattr(policy, name)
        if type(expected) is classmethod:
            if (
                getattr(resolved, "__func__", None) is not expected.__func__
                or getattr(resolved, "__self__", None) is not policy
            ):
                raise RuntimeError(
                    f"canonical PaperRiskPolicy root lookup changed during sealing: {name}"
                )
        elif resolved is not expected:
            raise RuntimeError(f"canonical PaperRiskPolicy root lookup changed during sealing: {name}")


_install()
del _install
