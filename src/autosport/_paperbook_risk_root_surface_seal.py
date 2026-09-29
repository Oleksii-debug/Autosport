"""Seal finalized PaperRiskPolicy dispatch roots without replacing class identity.

``risk.PaperRiskPolicy`` is created at its source definition with a dedicated metaclass.
Generation/private-authority composition is intentionally allowed to replace the
owner-facing functions and selected transitive helper roots first. This final step then
installs metaclass data descriptors over those exact finalized class-dict descriptors.

The seal rejects descriptor replacement/deletion and also witnesses the exact Python
function/code behind each protected descriptor on class dispatch. Reconstructible raw
risk specs therefore cannot retarget a finalized generation/private helper through the
class while preserving an outer helper's identity. The canonical PaperRiskPolicy class
object and ``__bases__ == (object,)`` are preserved; no policy facade, sibling engine,
state, store or second risk authority is created.
"""

from __future__ import annotations

from types import FunctionType

from . import risk as _risk


_PROTECTED_ROOT_TYPES = (
    ("_book_state", classmethod),
    ("_historical_risk_metrics", classmethod),
    ("_shadow_book_for_allocation", staticmethod),
    ("_identity_concentration_decision", classmethod),
    ("risk_of_ruin_portfolio_sha256", classmethod),
    ("risk_of_ruin_candidate_sha256", staticmethod),
    ("_effective_fraction_limits", FunctionType),
    ("_decimal_context", staticmethod),
    ("_exact_positive_sum", staticmethod),
    ("_fraction_exceeds", staticmethod),
    ("derive_goal_stake", FunctionType),
    ("derive_goal_stake_vector", FunctionType),
    ("evaluate", FunctionType),
)


class _SealedClassRoot:
    __slots__ = ("_name", "_descriptor", "_owner", "_function", "_code")

    def __init__(self, name: str, descriptor: object, owner: type) -> None:
        self._name = name
        self._descriptor = descriptor
        self._owner = owner
        if type(descriptor) is FunctionType:
            function = descriptor
        elif type(descriptor) in (classmethod, staticmethod):
            function = descriptor.__func__
        else:
            raise TypeError(f"unsupported PaperRiskPolicy sealed root: {name}")
        if type(function) is not FunctionType:
            raise TypeError(f"PaperRiskPolicy sealed root is not a Python function: {name}")
        self._function = function
        self._code = function.__code__

    def __get__(self, instance: object, owner: type | None = None):
        del owner
        if instance is None:
            return self
        namespace = type.__getattribute__(self._owner, "__dict__")
        if namespace.get(self._name) is not self._descriptor:
            raise TypeError(f"canonical PaperRiskPolicy root changed: {self._name}")
        descriptor = namespace[self._name]
        if type(descriptor) is FunctionType:
            function = descriptor
        elif type(descriptor) in (classmethod, staticmethod):
            function = descriptor.__func__
        else:
            raise TypeError(f"canonical PaperRiskPolicy root changed: {self._name}")
        if function is not self._function or function.__code__ is not self._code:
            raise TypeError(f"canonical PaperRiskPolicy executable root changed: {self._name}")
        getter = getattr(descriptor, "__get__", None)
        if getter is None:
            return descriptor
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
    root_types = dict(_PROTECTED_ROOT_TYPES)
    for name, expected_type in _PROTECTED_ROOT_TYPES:
        descriptor = namespace.get(name)
        if expected_type is FunctionType:
            if type(descriptor) is not FunctionType:
                raise RuntimeError(
                    f"canonical PaperRiskPolicy root changed before sealing: {name}"
                )
        elif (
            type(descriptor) is not expected_type
            or type(descriptor.__func__) is not FunctionType
        ):
            raise RuntimeError(
                f"canonical PaperRiskPolicy root changed before sealing: {name}"
            )
        root_descriptors[name] = descriptor

    def reject_subclass(cls, **kwargs) -> None:
        del cls, kwargs
        raise TypeError("canonical PaperRiskPolicy is not extensible")

    # Install the subclass fence while root assignment is still intentionally open,
    # then protect that exact classmethod through the same metaclass data-descriptor
    # mechanism as the owner-facing/transitive roots.
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
    # lookup while vars(policy) proves the finalized class-dict descriptor identity
    # stayed untouched. The _SealedClassRoot lookup also validates exact function/code.
    current_namespace = type.__getattribute__(policy, "__dict__")
    for name, expected in root_descriptors.items():
        if current_namespace.get(name) is not expected:
            raise RuntimeError(f"canonical PaperRiskPolicy root moved during sealing: {name}")
        resolved = getattr(policy, name)
        expected_type = root_types[name]
        if expected_type is classmethod:
            if (
                getattr(resolved, "__func__", None) is not expected.__func__
                or getattr(resolved, "__self__", None) is not policy
            ):
                raise RuntimeError(
                    f"canonical PaperRiskPolicy root lookup changed during sealing: {name}"
                )
        elif expected_type is staticmethod:
            if resolved is not expected.__func__:
                raise RuntimeError(
                    f"canonical PaperRiskPolicy root lookup changed during sealing: {name}"
                )
        elif resolved is not expected:
            raise RuntimeError(
                f"canonical PaperRiskPolicy root lookup changed during sealing: {name}"
            )


_install()
del _install
