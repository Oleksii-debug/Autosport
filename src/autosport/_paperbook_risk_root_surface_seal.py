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

from types import FunctionType, MethodType

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


_INSTANCE_CALL_ROOT_NAMES = (
    "derive_goal_stake",
    "derive_goal_stake_vector",
    "evaluate",
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


def _make_instance_root_getattribute(
    policy: type,
    root_descriptors: dict[str, object],
) -> FunctionType:
    """Guard normal instance dispatch against in-place root executable mutation.

    Metaclass data descriptors protect class-level root replacement/deletion, but
    Python instance lookup reads plain functions directly from the class dictionary.
    A caller retaining a bound evaluate call could otherwise bypass the metaclass
    seal if that exact function object's code were changed.

    Return a narrow instance attribute gate that wraps only positive owner-facing
    risk calls. Each returned bound call revalidates identity/code/default/closure
    state before and after execution and executes a fresh FunctionType from the
    witnessed code object. Ordinary dataclass/field access continues through the
    builtin object attribute path unchanged.
    """

    exact_type = type
    function_type = FunctionType
    method_type = MethodType
    object_getattribute = object.__getattribute__
    type_getattribute = type.__getattribute__
    empty_cell = object()

    witnesses: list[tuple[object, ...]] = []
    for name in _INSTANCE_CALL_ROOT_NAMES:
        function = root_descriptors.get(name)
        if type(function) is not FunctionType:
            raise RuntimeError(
                f"canonical PaperRiskPolicy instance root is unavailable: {name}"
            )
        closure = function.__closure__
        closure_values: tuple[object, ...] | None
        if closure is None:
            closure_values = None
        else:
            values: list[object] = []
            for cell in closure:
                try:
                    values.append(cell.cell_contents)
                except ValueError:
                    values.append(empty_cell)
            closure_values = tuple(values)
        kwdefaults = (
            None
            if function.__kwdefaults__ is None
            else tuple(function.__kwdefaults__.items())
        )
        witnesses.append(
            (
                name,
                function,
                function.__code__,
                function.__globals__,
                function.__defaults__,
                kwdefaults,
                closure,
                closure_values,
            )
        )
    frozen_witnesses = tuple(witnesses)

    def require_instance_root(name: str):
        if exact_type(name) is not str:
            return None
        expected = None
        for witness in frozen_witnesses:
            if witness[0] == name:
                expected = witness
                break
        if expected is None:
            return None

        (
            _root_name,
            function,
            code,
            globals_mapping,
            defaults,
            kwdefaults,
            closure,
            closure_values,
        ) = expected
        namespace = type_getattribute(policy, "__dict__")
        current = namespace.get(name, empty_cell)
        if (
            exact_type(current) is not function_type
            or current is not function
            or current.__code__ is not code
            or current.__globals__ is not globals_mapping
            or current.__defaults__ is not defaults
            or current.__closure__ is not closure
        ):
            raise TypeError(
                f"canonical PaperRiskPolicy executable root changed: {name}"
            )

        current_kwdefaults = current.__kwdefaults__
        if kwdefaults is None:
            if current_kwdefaults is not None:
                raise TypeError(
                    f"canonical PaperRiskPolicy executable root changed: {name}"
                )
        else:
            if (
                exact_type(current_kwdefaults) is not dict
                or len(current_kwdefaults) != len(kwdefaults)
                or any(
                    key not in current_kwdefaults
                    or current_kwdefaults[key] is not expected_value
                    for key, expected_value in kwdefaults
                )
            ):
                raise TypeError(
                    f"canonical PaperRiskPolicy executable root changed: {name}"
                )

        if closure_values is None:
            if closure is not None:
                raise TypeError(
                    f"canonical PaperRiskPolicy executable root changed: {name}"
                )
        else:
            if closure is None or len(closure) != len(closure_values):
                raise TypeError(
                    f"canonical PaperRiskPolicy executable root changed: {name}"
                )
            for cell, expected_value in zip(closure, closure_values):
                try:
                    current_value = cell.cell_contents
                except ValueError:
                    current_value = empty_cell
                if current_value is not expected_value:
                    raise TypeError(
                        f"canonical PaperRiskPolicy executable root changed: {name}"
                    )
        return expected

    require_instance_root_code = require_instance_root.__code__

    def sealed_instance_getattribute(self, name: str):
        if (
            exact_type(require_instance_root) is not function_type
            or require_instance_root.__code__ is not require_instance_root_code
        ):
            raise TypeError(
                "canonical PaperRiskPolicy instance-root verifier changed"
            )
        witness = require_instance_root(name)
        if witness is None:
            return object_getattribute(self, name)
        if exact_type(self) is not policy:
            raise TypeError("canonical PaperRiskPolicy instance identity changed")

        (
            _root_name,
            function,
            code,
            globals_mapping,
            defaults,
            kwdefaults,
            closure,
            _closure_values,
        ) = witness

        def guarded_root(bound_self, *args, **kwargs):
            if (
                exact_type(require_instance_root) is not function_type
                or require_instance_root.__code__ is not require_instance_root_code
            ):
                raise TypeError(
                    "canonical PaperRiskPolicy instance-root verifier changed"
                )
            current = require_instance_root(name)
            if current is not witness:
                raise TypeError(
                    f"canonical PaperRiskPolicy executable root changed: {name}"
                )
            delegate = function_type(
                code,
                globals_mapping,
                name=function.__name__,
                argdefs=defaults,
                closure=closure,
            )
            if kwdefaults is not None:
                delegate.__kwdefaults__ = dict(kwdefaults)
            try:
                return delegate(bound_self, *args, **kwargs)
            finally:
                if (
                    exact_type(require_instance_root) is not function_type
                    or require_instance_root.__code__ is not require_instance_root_code
                ):
                    raise TypeError(
                        "canonical PaperRiskPolicy instance-root verifier changed"
                    )
                current_after = require_instance_root(name)
                if current_after is not witness:
                    raise TypeError(
                        f"canonical PaperRiskPolicy executable root changed: {name}"
                    )

        guarded_root.__name__ = function.__name__
        guarded_root.__qualname__ = function.__qualname__
        guarded_root.__doc__ = function.__doc__
        guarded_root.__annotations__ = dict(function.__annotations__)
        return method_type(guarded_root, self)

    return sealed_instance_getattribute


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

    instance_getattribute = _make_instance_root_getattribute(
        policy,
        root_descriptors,
    )
    type.__setattr__(policy, "__getattribute__", instance_getattribute)
    if (
        type.__getattribute__(policy, "__dict__").get("__getattribute__")
        is not instance_getattribute
    ):
        raise RuntimeError(
            "canonical PaperRiskPolicy instance dispatch fence was not installed"
        )
    root_descriptors["__getattribute__"] = instance_getattribute
    root_types["__getattribute__"] = FunctionType

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
