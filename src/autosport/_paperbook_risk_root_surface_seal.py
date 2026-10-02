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

from dis import get_instructions
from functools import partial
import sys
from types import CodeType, FunctionType

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


class _SealedClassGetattributeRoot:
    """Protect the instance __getattribute__ class slot without corrupting metaclass lookup.

    A generic _SealedClassRoot cannot be installed on the metaclass under the
    special name __getattribute__: CPython then uses that descriptor as the
    metaclass\'s own attribute-dispatch implementation. Keep the data-descriptor
    replacement/deletion fence, but have special-method lookup resolve to the
    canonical C-level type.__getattribute__ dispatcher instead.
    """

    __slots__ = ("_owner", "_descriptor", "_code")

    def __init__(self, owner: type, descriptor: FunctionType) -> None:
        self._owner = owner
        self._descriptor = descriptor
        self._code = descriptor.__code__

    def __get__(self, instance: object, owner: type | None = None):
        del owner
        if instance is None:
            return self
        if instance is not self._owner:
            raise TypeError("canonical PaperRiskPolicy metaclass dispatch owner changed")
        namespace = type.__getattribute__(self._owner, "__dict__")
        current = namespace.get("__getattribute__")
        if (
            current is not self._descriptor
            or type(current) is not FunctionType
            or current.__code__ is not self._code
        ):
            raise TypeError("canonical PaperRiskPolicy root changed: __getattribute__")
        return type.__getattribute__.__get__(instance, type(instance))

    def __set__(self, instance: object, value: object) -> None:
        del instance, value
        raise TypeError("canonical PaperRiskPolicy root is sealed: __getattribute__")

    def __delete__(self, instance: object) -> None:
        del instance
        raise TypeError("canonical PaperRiskPolicy root is sealed: __getattribute__")


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

    def referenced_global_names(code):
        names: set[str] = set()
        for instruction in get_instructions(code):
            if (
                instruction.opname
                in ("LOAD_GLOBAL", "LOAD_NAME", "LOAD_FROM_DICT_OR_GLOBALS")
                and type(instruction.argval) is str
            ):
                names.add(instruction.argval)
        for constant in code.co_consts:
            if type(constant) is CodeType:
                names.update(referenced_global_names(constant))
        return tuple(sorted(names))

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
        globals_mapping = function.__globals__
        builtins_mapping = function.__builtins__
        if type(globals_mapping) is not dict or type(builtins_mapping) is not dict:
            raise RuntimeError(
                f"canonical PaperRiskPolicy executable namespace is invalid: {name}"
            )
        referenced_names = referenced_global_names(function.__code__)
        global_bindings = tuple(
            (binding_name, globals_mapping[binding_name])
            for binding_name in referenced_names
            if binding_name in globals_mapping
        )
        builtin_bindings = tuple(
            (binding_name, builtins_mapping[binding_name])
            for binding_name in referenced_names
            if binding_name not in globals_mapping and binding_name in builtins_mapping
        )
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
                globals_mapping,
                global_bindings,
                builtins_mapping,
                builtin_bindings,
                function.__defaults__,
                kwdefaults,
                closure,
                closure_witnesses,
            )
        )
    frozen_witnesses = tuple(witnesses)

    descriptor_witnesses: list[tuple[object, ...]] = []
    for descriptor_name, descriptor in root_descriptors.items():
        if type(descriptor) is FunctionType:
            descriptor_type = FunctionType
            descriptor_kind = "function"
            function = descriptor
        elif type(descriptor) is classmethod:
            descriptor_type = classmethod
            descriptor_kind = "classmethod"
            function = descriptor.__func__
        elif type(descriptor) is staticmethod:
            descriptor_type = staticmethod
            descriptor_kind = "staticmethod"
            function = descriptor.__func__
        else:
            raise RuntimeError(
                f"canonical PaperRiskPolicy protected descriptor is invalid: {descriptor_name}"
            )
        closure = function.__closure__
        if closure is None:
            closure_witnesses: tuple[tuple[object, object], ...] = ()
        else:
            values: list[tuple[object, object]] = []
            for cell in closure:
                try:
                    value = cell.cell_contents
                except ValueError:
                    value = empty_cell
                values.append((cell, value))
            closure_witnesses = tuple(values)
        globals_mapping = function.__globals__
        builtins_mapping = function.__builtins__
        if type(globals_mapping) is not dict or type(builtins_mapping) is not dict:
            raise RuntimeError(
                f"canonical PaperRiskPolicy protected namespace is invalid: {descriptor_name}"
            )
        referenced_names = referenced_global_names(function.__code__)
        global_bindings = tuple(
            (binding_name, globals_mapping[binding_name])
            for binding_name in referenced_names
            if binding_name in globals_mapping
        )
        builtin_bindings = tuple(
            (binding_name, builtins_mapping[binding_name])
            for binding_name in referenced_names
            if binding_name not in globals_mapping and binding_name in builtins_mapping
        )
        missing_bindings = tuple(
            binding_name
            for binding_name in referenced_names
            if binding_name not in globals_mapping and binding_name not in builtins_mapping
        )
        kwdefaults = (
            None
            if function.__kwdefaults__ is None
            else tuple(function.__kwdefaults__.items())
        )
        descriptor_witnesses.append(
            (
                descriptor_name,
                descriptor,
                descriptor_type,
                descriptor_kind,
                function,
                function.__code__,
                globals_mapping,
                global_bindings,
                builtins_mapping,
                builtin_bindings,
                missing_bindings,
                function.__defaults__,
                kwdefaults,
                closure,
                closure_witnesses,
            )
        )
    frozen_descriptor_witnesses = tuple(descriptor_witnesses)

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
            global_bindings,
            builtins_mapping,
            builtin_bindings,
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
            or current.__builtins__ is not builtins_mapping
            or current.__defaults__ is not defaults
            or current.__closure__ is not closure
        ):
            raise failure_type_arg(
                f"canonical PaperRiskPolicy executable root changed: {name}"
            )

        for binding_name, expected_value in global_bindings:
            if (
                binding_name not in globals_mapping
                or globals_mapping[binding_name] is not expected_value
            ):
                raise failure_type_arg(
                    f"canonical PaperRiskPolicy executable global changed: "
                    f"{name}:{binding_name}"
                )
        for binding_name, expected_value in builtin_bindings:
            if (
                binding_name not in builtins_mapping
                or builtins_mapping[binding_name] is not expected_value
            ):
                raise failure_type_arg(
                    f"canonical PaperRiskPolicy executable builtin changed: "
                    f"{name}:{binding_name}"
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

    def validate_instance_descriptor_root(
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
            descriptor,
            descriptor_type,
            _descriptor_kind,
            function,
            code,
            globals_mapping,
            global_bindings,
            builtins_mapping,
            builtin_bindings,
            missing_bindings,
            defaults,
            kwdefaults,
            closure,
            closure_witnesses,
        ) = witness
        namespace = type_getattribute_arg(owner, "__dict__")
        current = namespace.get(name, empty_cell_arg)
        if current is not descriptor or exact_type_arg(current) is not descriptor_type:
            raise failure_type_arg(f"canonical PaperRiskPolicy root changed: {name}")
        if descriptor_type is function_type_arg:
            current_function = current
        else:
            current_function = current.__func__
        if (
            current_function is not function
            or current_function.__code__ is not code
            or current_function.__globals__ is not globals_mapping
            or current_function.__builtins__ is not builtins_mapping
            or current_function.__defaults__ is not defaults
            or current_function.__closure__ is not closure
        ):
            raise failure_type_arg(
                f"canonical PaperRiskPolicy executable root changed: {name}"
            )

        for binding_name, expected_value in global_bindings:
            if (
                binding_name not in globals_mapping
                or globals_mapping[binding_name] is not expected_value
            ):
                raise failure_type_arg(
                    f"canonical PaperRiskPolicy executable global changed: "
                    f"{name}:{binding_name}"
                )
        for binding_name, expected_value in builtin_bindings:
            if (
                binding_name not in builtins_mapping
                or builtins_mapping[binding_name] is not expected_value
            ):
                raise failure_type_arg(
                    f"canonical PaperRiskPolicy executable builtin changed: "
                    f"{name}:{binding_name}"
                )
        for binding_name in missing_bindings:
            if binding_name in globals_mapping or binding_name in builtins_mapping:
                raise failure_type_arg(
                    f"canonical PaperRiskPolicy executable missing binding changed: "
                    f"{name}:{binding_name}"
                )

        current_kwdefaults = current_function.__kwdefaults__
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

    def guarded_instance_descriptor_call(
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
            _descriptor,
            _descriptor_type,
            descriptor_kind,
            _function,
            code,
            _globals_mapping,
            global_bindings,
            _builtins_mapping,
            builtin_bindings,
            _missing_bindings,
            defaults,
            kwdefaults,
            closure,
            _closure_witnesses,
        ) = witness
        delegate_globals = dict_type_arg(global_bindings)
        delegate_globals["__builtins__"] = dict_type_arg(builtin_bindings)
        delegate = function_type_arg(
            code,
            delegate_globals,
            name=name,
            argdefs=defaults,
            closure=closure,
        )
        if kwdefaults is not None:
            delegate.__kwdefaults__ = dict_type_arg(kwdefaults)
        try:
            if descriptor_kind == "function":
                return delegate(bound_self, *args, **kwargs)
            if descriptor_kind == "classmethod":
                return delegate(owner, *args, **kwargs)
            if descriptor_kind == "staticmethod":
                return delegate(*args, **kwargs)
            raise failure_type_arg(
                f"canonical PaperRiskPolicy protected descriptor is invalid: {name}"
            )
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
            _globals_mapping,
            global_bindings,
            _builtins_mapping,
            builtin_bindings,
            defaults,
            kwdefaults,
            closure,
            _closure_witnesses,
        ) = witness
        delegate_globals = dict_type_arg(global_bindings)
        delegate_globals["__builtins__"] = dict_type_arg(builtin_bindings)
        delegate = function_type_arg(
            code,
            delegate_globals,
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
        descriptor_roots=None,
        descriptor_validator=None,
        descriptor_guarded_call=None,
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
        for descriptor_witness in descriptor_roots:
            if name != descriptor_witness[0]:
                continue
            descriptor_validator(
                descriptor_witness,
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
            return partial_type_arg(
                descriptor_guarded_call,
                descriptor_witness,
                descriptor_validator,
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
        frozen_descriptor_witnesses,
        validate_instance_descriptor_root,
        guarded_instance_descriptor_call,
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
        validate_instance_descriptor_root,
        guarded_instance_root_call,
        guarded_instance_descriptor_call,
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
        if name != "__getattribute__"
    }
    sealed["__init_subclass__"] = _SealedClassRoot(
        "__init_subclass__",
        subclass_descriptor,
        policy,
    )
    for name, descriptor in sealed.items():
        type.__setattr__(meta, name, descriptor)

    # __getattribute__ is itself the metaclass attribute-dispatch special method.
    # Installing the generic owner-root descriptor at that exact name makes CPython
    # call the instance-dispatch function as the metaclass dispatcher and breaks
    # class lookup. The dedicated descriptor remains a data descriptor, so even
    # direct type.__setattr__/__delattr__ cannot replace the policy class slot, while
    # special lookup resolves to the C-level type dispatcher.
    class_getattribute_guard = _SealedClassGetattributeRoot(
        policy,
        instance_getattribute,
    )
    type.__setattr__(meta, "__getattribute__", class_getattribute_guard)

    # Refuse a partially installed seal. Class access exercises metaclass descriptor
    # lookup while vars(policy) proves the finalized class-dict descriptor identity
    # stayed untouched. The _SealedClassRoot lookup also validates exact function/code.
    current_namespace = type.__getattribute__(policy, "__dict__")
    meta_namespace = type.__getattribute__(meta, "__dict__")
    if meta_namespace.get("__getattribute__") is not class_getattribute_guard:
        raise RuntimeError(
            "canonical PaperRiskPolicy metaclass dispatch fence was not installed"
        )
    for name, expected in root_descriptors.items():
        if current_namespace.get(name) is not expected:
            raise RuntimeError(f"canonical PaperRiskPolicy root moved during sealing: {name}")
        if name == "__getattribute__":
            continue
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
