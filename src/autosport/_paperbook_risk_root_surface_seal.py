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

from functools import partial
import sys
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
    """Build a fail-closed instance dispatch gate with no mutable closure authority.

    Instance special-method lookup executes __getattribute__ directly from the class
    slot, so a self-check inside that same mutable Python function cannot defend an
    in-place __code__ replacement. Keep the special-method function closureless,
    retain authority inputs in an immutable defaults tuple, and use one narrowly
    scoped process audit hook to reject executable/default metadata replacement on
    the closureless gate/validator/executor before mutation takes effect.

    Owner-facing risk roots remain the canonical Python functions. Their exact
    identity/code/globals/defaults/kwdefaults/closure values are revalidated on each
    lookup and retained call. Calls execute a fresh delegate from that witnessed
    state, so mutation after obtaining a callable also fails closed.
    """

    exact_type = type
    function_type = FunctionType
    dict_type = dict
    failure_type = TypeError
    value_error_type = ValueError
    len_fn = len
    object_getattribute = object.__getattribute__
    type_getattribute = type.__getattribute__
    partial_type = partial
    empty_cell = object()

    witnesses: list[tuple[object, ...]] = []
    for name in _INSTANCE_CALL_ROOT_NAMES:
        function = root_descriptors.get(name)
        if type(function) is not FunctionType:
            raise RuntimeError(
                f"canonical PaperRiskPolicy instance root is unavailable: {name}"
            )
        closure = function.__closure__
        closure_witnesses: tuple[tuple[object, object], ...]
        if closure is None:
            closure_witnesses = ()
        else:
            values: list[tuple[object, object]] = []
            for cell in closure:
                try:
                    value = cell.cell_contents
                except ValueError:
                    value = empty_cell
                values.append((cell, value))
            closure_witnesses = tuple(values)
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
                closure_witnesses,
            )
        )
    frozen_witnesses = tuple(witnesses)

    def validate_instance_root(
        witness,
        owner,
        exact_type_arg,
        function_type_arg,
        dict_type_arg,
        type_getattribute_arg,
        failure_type_arg,
        value_error_type_arg,
        len_fn_arg,
        empty_cell_arg,
    ):
        (
            name,
            function,
            code,
            globals_mapping,
            defaults,
            kwdefaults,
            closure,
            closure_witnesses,
        ) = witness
        namespace = type_getattribute_arg(owner, "__dict__")
        current = namespace.get(name, empty_cell_arg)
        if (
            exact_type_arg(current) is not function_type_arg
            or current is not function
            or current.__code__ is not code
            or current.__globals__ is not globals_mapping
            or current.__defaults__ is not defaults
            or current.__closure__ is not closure
        ):
            raise failure_type_arg(
                f"canonical PaperRiskPolicy executable root changed: {name}"
            )

        current_kwdefaults = current.__kwdefaults__
        if kwdefaults is None:
            if current_kwdefaults is not None:
                raise failure_type_arg(
                    f"canonical PaperRiskPolicy executable root changed: {name}"
                )
        else:
            if (
                exact_type_arg(current_kwdefaults) is not dict_type_arg
                or len_fn_arg(current_kwdefaults) != len_fn_arg(kwdefaults)
            ):
                raise failure_type_arg(
                    f"canonical PaperRiskPolicy executable root changed: {name}"
                )
            for key, expected_value in kwdefaults:
                if (
                    key not in current_kwdefaults
                    or current_kwdefaults[key] is not expected_value
                ):
                    raise failure_type_arg(
                        f"canonical PaperRiskPolicy executable root changed: {name}"
                    )

        if closure is None:
            if closure_witnesses:
                raise failure_type_arg(
                    f"canonical PaperRiskPolicy executable root changed: {name}"
                )
        else:
            if len_fn_arg(closure) != len_fn_arg(closure_witnesses):
                raise failure_type_arg(
                    f"canonical PaperRiskPolicy executable root changed: {name}"
                )
            for cell, expected_value in closure_witnesses:
                try:
                    current_value = cell.cell_contents
                except value_error_type_arg:
                    current_value = empty_cell_arg
                if current_value is not expected_value:
                    raise failure_type_arg(
                        f"canonical PaperRiskPolicy executable root changed: {name}"
                    )
        return witness

    def guarded_instance_root_call(
        witness,
        validator,
        owner,
        exact_type_arg,
        function_type_arg,
        dict_type_arg,
        type_getattribute_arg,
        failure_type_arg,
        value_error_type_arg,
        len_fn_arg,
        empty_cell_arg,
        bound_self,
        *args,
        **kwargs,
    ):
        current = validator(
            witness,
            owner,
            exact_type_arg,
            function_type_arg,
            dict_type_arg,
            type_getattribute_arg,
            failure_type_arg,
            value_error_type_arg,
            len_fn_arg,
            empty_cell_arg,
        )
        if current is not witness or exact_type_arg(bound_self) is not owner:
            raise failure_type_arg("canonical PaperRiskPolicy instance identity changed")
        (
            name,
            _function,
            code,
            globals_mapping,
            defaults,
            kwdefaults,
            closure,
            _closure_witnesses,
        ) = witness
        delegate = function_type_arg(
            code,
            globals_mapping,
            name=name,
            argdefs=defaults,
            closure=closure,
        )
        if kwdefaults is not None:
            delegate.__kwdefaults__ = dict_type_arg(kwdefaults)
        try:
            return delegate(bound_self, *args, **kwargs)
        finally:
            current_after = validator(
                witness,
                owner,
                exact_type_arg,
                function_type_arg,
                dict_type_arg,
                type_getattribute_arg,
                failure_type_arg,
                value_error_type_arg,
                len_fn_arg,
                empty_cell_arg,
            )
            if current_after is not witness:
                raise failure_type_arg(
                    f"canonical PaperRiskPolicy executable root changed: {name}"
                )

    def sealed_instance_getattribute(
        self,
        name,
        roots=None,
        validator=None,
        guarded_call=None,
        owner=None,
        exact_type_arg=None,
        function_type_arg=None,
        dict_type_arg=None,
        type_getattribute_arg=None,
        object_getattribute_arg=None,
        partial_type_arg=None,
        failure_type_arg=None,
        value_error_type_arg=None,
        len_fn_arg=None,
        empty_cell_arg=None,
    ):
        for witness in roots:
            if name != witness[0]:
                continue
            validator(
                witness,
                owner,
                exact_type_arg,
                function_type_arg,
                dict_type_arg,
                type_getattribute_arg,
                failure_type_arg,
                value_error_type_arg,
                len_fn_arg,
                empty_cell_arg,
            )
            if exact_type_arg(self) is not owner:
                raise failure_type_arg(
                    "canonical PaperRiskPolicy instance identity changed"
                )
            return partial_type_arg(
                guarded_call,
                witness,
                validator,
                owner,
                exact_type_arg,
                function_type_arg,
                dict_type_arg,
                type_getattribute_arg,
                failure_type_arg,
                value_error_type_arg,
                len_fn_arg,
                empty_cell_arg,
                self,
            )
        return object_getattribute_arg(self, name)

    sealed_instance_getattribute.__defaults__ = (
        frozen_witnesses,
        validate_instance_root,
        guarded_instance_root_call,
        policy,
        exact_type,
        function_type,
        dict_type,
        type_getattribute,
        object_getattribute,
        partial_type,
        failure_type,
        value_error_type,
        len_fn,
        empty_cell,
    )

    def reject_guard_metadata_mutation(
        event,
        args,
        protected=None,
        failure_type_arg=None,
    ):
        if event != "object.__setattr__":
            return
        target, name, value = args
        if name not in ("__code__", "__defaults__", "__kwdefaults__"):
            return
        for guarded in protected:
            if target is not guarded:
                continue
            if name == "__code__":
                current = guarded.__code__
            elif name == "__defaults__":
                current = guarded.__defaults__
            else:
                current = guarded.__kwdefaults__
            if value is not current:
                raise failure_type_arg(
                    "canonical PaperRiskPolicy instance dispatch executable is sealed"
                )
            return

    protected = (
        validate_instance_root,
        guarded_instance_root_call,
        sealed_instance_getattribute,
        reject_guard_metadata_mutation,
    )
    reject_guard_metadata_mutation.__defaults__ = (protected, failure_type)

    for guarded in protected:
        if guarded.__closure__ is not None:
            raise RuntimeError(
                "canonical PaperRiskPolicy instance dispatch guard retained closure state"
            )

    # CPython emits object.__setattr__ before replacing function executable/default
    # metadata. The hook recognizes only the four exact guard function identities
    # above; unrelated mutation stays outside this authority. The hook also protects
    # its own code/default tuple.
    sys.addaudithook(reject_guard_metadata_mutation)
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
