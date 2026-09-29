"""Seal Wave M settlement composition against caller-controlled dispatch/state drift.

The owning continuous-session implementation remains canonical. This package guard only
closes Python mutation seams around its already-defined settlement authorities: helper
dispatch through ``self`` and the post-validation quote-outcome fingerprint. It creates
no second settlement engine, store, scheduler, or outcome authority.
"""

from __future__ import annotations

import weakref
from types import MethodType

from . import continuous_session as _session


def _build_validated_outcomes_type(digest_fn, digest_code, sha256_validator, sha256_code):
    """Keep a validated mapping's original digest outside caller-writable object state."""

    # These names are authority-bearing inside the returned type long after installation.
    # Capture them in closure cells so a later module-global shadow cannot forge the
    # witnesses or redirect state construction while preserving function identity.
    session_module = _session
    exact_getattr = getattr
    exact_id = id
    exact_dict_init = dict.__init__
    exact_weakref_ref = weakref.ref
    exact_type_error = TypeError
    exact_value_error = ValueError

    issued: dict[int, tuple[weakref.ReferenceType[dict[str, str]], str]] = {}

    def require_digest_authority() -> None:
        if exact_getattr(digest_fn, "__code__", None) is not digest_code:
            raise session_module.ContinuousSessionError(
                "settlement outcome digest executable changed"
            )
        if exact_getattr(sha256_validator, "__code__", None) is not sha256_code:
            raise session_module.ContinuousSessionError(
                "settlement sha256 validator executable changed"
            )

    class ValidatedQuoteOutcomes(dict[str, str]):
        __slots__ = ("__weakref__",)

        def __init__(
            self,
            values=(),
            *,
            validated_sha256: str | None = None,
        ) -> None:
            identity = exact_id(self)
            previous = issued.get(identity)
            if previous is not None and previous[0]() is self:
                raise exact_type_error("validated settlement outcomes cannot be reinitialized")

            require_digest_authority()
            exact_dict_init(self, values)
            digest = digest_fn(self)
            require_digest_authority()
            if validated_sha256 is not None:
                expected = sha256_validator(
                    validated_sha256,
                    "validated_sha256",
                )
                require_digest_authority()
                if expected != digest:
                    raise exact_value_error(
                        "validated settlement outcome digest does not match contents"
                    )

            def remove(reference, *, identity=identity) -> None:
                current = issued.get(identity)
                if current is not None and current[0] is reference:
                    issued.pop(identity, None)

            issued[identity] = (exact_weakref_ref(self, remove), digest)

        @property
        def validated_sha256(self) -> str:
            current = issued.get(exact_id(self))
            if current is None or current[0]() is not self:
                raise session_module.ContinuousSessionError(
                    "validated settlement outcome authority is unavailable"
                )
            return current[1]

    ValidatedQuoteOutcomes.__name__ = "_ValidatedQuoteOutcomes"
    ValidatedQuoteOutcomes.__qualname__ = "_ValidatedQuoteOutcomes"
    return ValidatedQuoteOutcomes


def _validated_resolution_with_authority_witness(
    validate,
    *,
    validated_type,
    helper_witnesses: tuple[tuple[str, object, object], ...],
):
    """Reject mutable module dispatch before canonical settlement validation uses it."""

    session_module = _session
    exact_getattr = getattr
    validate_code = validate.__code__

    def require_authority(*, during: bool = False) -> None:
        if exact_getattr(validate, "__code__", None) is not validate_code:
            suffix = " during validation" if during else ""
            raise session_module.ContinuousSessionError(
                "canonical settlement resolution validator executable changed" + suffix
            )
        if session_module._ValidatedQuoteOutcomes is not validated_type:
            suffix = " during validation" if during else ""
            raise session_module.ContinuousSessionError(
                "canonical validated settlement outcome type changed" + suffix
            )
        for name, expected, expected_code in helper_witnesses:
            current = exact_getattr(session_module, name, None)
            if current is not expected or exact_getattr(expected, "__code__", None) is not expected_code:
                suffix = " during validation" if during else ""
                if name == "_settlement_quote_outcomes_sha256":
                    raise session_module.ContinuousSessionError(
                        "canonical settlement outcome digest authority changed" + suffix
                    )
                raise session_module.ContinuousSessionError(
                    "canonical settlement resolution helper authority changed: "
                    + name
                    + suffix
                )

    def guarded(self, *args, **kwargs):
        require_authority()
        result = validate(self, *args, **kwargs)
        require_authority(during=True)
        return result

    guarded.__name__ = validate.__name__
    guarded.__qualname__ = validate.__qualname__
    guarded.__doc__ = validate.__doc__
    guarded.__annotations__ = validate.__annotations__
    return guarded


def _sealed_dispatch(target, *, bind: bool, after_call=None):
    """Return a descriptor bound to one exact hidden function and executable."""

    session_module = _session
    exact_getattr = getattr
    exact_method_type = MethodType
    exact_type_error = TypeError
    target_code = exact_getattr(target, "__code__", None)
    if target_code is None:
        raise exact_type_error("canonical settlement helper must expose executable code")
    target_closure = exact_getattr(target, "__closure__", None) or ()
    if len(target_closure) != len(target_code.co_freevars):
        raise exact_type_error("canonical settlement helper closure is malformed")
    target_cell_witnesses: tuple[tuple[object, object], ...] = tuple(
        (cell, cell.cell_contents) for cell in target_closure
    )

    def require_target_authority() -> None:
        if exact_getattr(target, "__code__", None) is not target_code:
            raise session_module.ContinuousSessionError(
                "canonical settlement coordinator dispatch changed"
            )
        current_closure = exact_getattr(target, "__closure__", None) or ()
        if len(current_closure) != len(target_cell_witnesses):
            raise session_module.ContinuousSessionError(
                "canonical settlement coordinator dispatch changed"
            )
        for current_cell, (expected_cell, expected_value) in zip(
            current_closure,
            target_cell_witnesses,
        ):
            if current_cell is not expected_cell:
                raise session_module.ContinuousSessionError(
                    "canonical settlement coordinator dispatch changed"
                )
            try:
                current_value = current_cell.cell_contents
            except ValueError as exc:
                raise session_module.ContinuousSessionError(
                    "canonical settlement coordinator dispatch changed"
                ) from exc
            if current_value is not expected_value:
                raise session_module.ContinuousSessionError(
                    "canonical settlement coordinator dispatch changed"
                )

    class SealedDispatch:
        __slots__ = ()

        def __get__(self, instance, owner=None):
            require_target_authority()
            if instance is None:
                return target
            if bind:
                canonical = exact_method_type(target, instance)
                if after_call is None:
                    def guarded_target(*args, **kwargs):
                        require_target_authority()
                        result = canonical(*args, **kwargs)
                        require_target_authority()
                        return result

                    return guarded_target

                # Freeze the direct product checker into this invocation before any
                # callback-capable canonical work runs. Long-lived closure cells remain
                # witnessed by the product entry, but a callback cannot retarget this
                # invocation-local checker after the trusted interval has begun.
                expected_after_call = after_call
                post_call = expected_after_call(snapshot=True)
                if post_call is None:
                    raise session_module.ContinuousSessionError(
                        "canonical settlement post-callback authority is unavailable"
                    )

                def guarded(*args, **kwargs):
                    if after_call is not expected_after_call:
                        raise session_module.ContinuousSessionError(
                            "canonical settlement post-callback authority changed"
                        )
                    require_target_authority()
                    result = canonical(*args, **kwargs)
                    # Tick has callback-capable work before this descriptor is acquired.
                    # Re-resolve the independently witnessed economic entry here, after
                    # outcome callbacks but before resolutions can reach learning prepare.
                    # This catches validator/dispatch drift even if an earlier callback
                    # retargeted the advisory post-call checker before method acquisition.
                    exact_getattr(instance, "_settle")
                    if after_call is not expected_after_call:
                        raise session_module.ContinuousSessionError(
                            "canonical settlement post-callback authority changed"
                        )
                    if expected_after_call(snapshot=True) is not post_call:
                        raise session_module.ContinuousSessionError(
                            "canonical settlement post-callback authority changed"
                        )
                    post_call()
                    if (
                        after_call is not expected_after_call
                        or expected_after_call(snapshot=True) is not post_call
                    ):
                        raise session_module.ContinuousSessionError(
                            "canonical settlement post-callback authority changed"
                        )
                    require_target_authority()
                    return result

                return guarded
            return target

        def __set__(self, _instance, _value) -> None:
            raise exact_type_error(
                "canonical settlement consumer entry binding is immutable; "
                "canonical settlement coordinator dispatch is immutable"
            )

        def __delete__(self, _instance) -> None:
            raise exact_type_error(
                "canonical settlement consumer entry binding is immutable; "
                "canonical settlement coordinator dispatch is immutable"
            )

    return SealedDispatch()


def _settlement_entry_with_dispatch_witness(
    entry: property,
    coordinator: type,
    coordinator_dict,
    expected_dispatch: dict[str, object],
    expected_targets: dict[
        str,
        tuple[object, object, tuple[tuple[object, object], ...]],
    ],
    *,
    resolution_dict,
    resolution_validate: object,
) -> property:
    """Fail before economic entry when protected helper descriptors/executables drift.

    The real class mapping proxies are captured during installation through
    ``type.__getattribute__``. The witness therefore never asks the mutable custom
    metaclass to resolve ``coordinator.__dict__`` at authority-crossing time.
    """

    session_module = _session
    exact_getattr = getattr
    exact_type_error = TypeError

    def verify_target_graph() -> None:
        for target, expected_code, expected_cells in expected_targets.values():
            if exact_getattr(target, "__code__", None) is not expected_code:
                raise session_module.ContinuousSessionError(
                    "canonical settlement coordinator dispatch changed"
                )
            current_closure = exact_getattr(target, "__closure__", None) or ()
            if len(current_closure) != len(expected_cells):
                raise session_module.ContinuousSessionError(
                    "canonical settlement coordinator dispatch changed"
                )
            for current_cell, (expected_cell, expected_value) in zip(
                current_closure,
                expected_cells,
            ):
                if current_cell is not expected_cell:
                    raise session_module.ContinuousSessionError(
                        "canonical settlement coordinator dispatch changed"
                    )
                try:
                    current_value = current_cell.cell_contents
                except ValueError as exc:
                    raise session_module.ContinuousSessionError(
                        "canonical settlement coordinator dispatch changed"
                    ) from exc
                if current_value is not expected_value:
                    raise session_module.ContinuousSessionError(
                        "canonical settlement coordinator dispatch changed"
                    )

    def verify() -> None:
        for name, expected in expected_dispatch.items():
            if coordinator_dict.get(name) is not expected:
                raise session_module.ContinuousSessionError(
                    "canonical settlement coordinator dispatch changed"
                )
        verify_target_graph()
        if resolution_dict.get("validate") is not resolution_validate:
            raise session_module.ContinuousSessionError(
                "canonical settlement resolution validation dispatch changed"
            )

    def resolve(instance):
        verify()
        canonical = entry.__get__(instance, coordinator)

        def guarded(*args, **kwargs):
            verify()
            result = canonical(*args, **kwargs)
            verify()
            return result

        return guarded

    def reject_set(_instance, _value) -> None:
        raise exact_type_error("canonical settlement consumer entry binding is immutable")

    def reject_delete(_instance) -> None:
        raise exact_type_error("canonical settlement consumer entry binding is immutable")

    return property(resolve, reject_set, reject_delete, entry.__doc__)


def _install() -> None:
    # No validated instances can exist before package import completes. Capture every
    # helper that SettlementResolution.validate late-resolves, then require those exact
    # functions/executables and the installed sealed compatibility type before and after
    # canonical validation. This preserves one validator authority rather than creating
    # a second implementation of its field/chronology rules.
    session_module = _session
    exact_getattr = getattr
    exact_object_getattribute = object.__getattribute__
    exact_id = id
    exact_type = type
    function_type = type(_install)
    canonical_digest = session_module._settlement_quote_outcomes_sha256
    canonical_digest_code = canonical_digest.__code__
    canonical_sha256_validator = session_module._sha256
    canonical_sha256_code = canonical_sha256_validator.__code__
    canonical_text_validator = session_module._text
    canonical_text_code = canonical_text_validator.__code__
    canonical_instant_validator = session_module._instant
    canonical_instant_code = canonical_instant_validator.__code__
    resolution_type = session_module.SettlementResolution
    resolution_dict = type.__getattribute__(resolution_type, "__dict__")
    canonical_resolution_validate = resolution_dict["validate"]

    validated_type = _build_validated_outcomes_type(
        canonical_digest,
        canonical_digest_code,
        canonical_sha256_validator,
        canonical_sha256_code,
    )
    session_module._ValidatedQuoteOutcomes = validated_type
    helper_witnesses = (
        (
            "_settlement_quote_outcomes_sha256",
            canonical_digest,
            canonical_digest_code,
        ),
        ("_sha256", canonical_sha256_validator, canonical_sha256_code),
        ("_text", canonical_text_validator, canonical_text_code),
        ("_instant", canonical_instant_validator, canonical_instant_code),
    )
    witnessed_resolution_validate = _validated_resolution_with_authority_witness(
        canonical_resolution_validate,
        validated_type=validated_type,
        helper_witnesses=helper_witnesses,
    )
    type.__setattr__(
        resolution_type,
        "validate",
        witnessed_resolution_validate,
    )

    coordinator = session_module.ContinuousSessionCoordinator
    metaclass = type(coordinator)
    coordinator_dict = type.__getattribute__(coordinator, "__dict__")
    metaclass_dict = type.__getattribute__(metaclass, "__dict__")
    settlement_entry = coordinator_dict["_settle"]
    if type(settlement_entry) is not property:
        raise TypeError("canonical settlement consumer entry must be a property")
    settlement_class_guard = metaclass_dict.get("_settle")
    if settlement_class_guard is None:
        raise TypeError("canonical settlement consumer class guard is unavailable")

    # Capture the already-composed Wave M implementations before replacing their
    # dispatch slots with data descriptors.
    bound_targets = {
        "_settlement_resolutions": coordinator_dict["_settlement_resolutions"],
        "_load_book": coordinator_dict["_load_book"],
    }
    static_targets = {
        "_require_settlement_causal_for_open_tickets": coordinator_dict[
            "_require_settlement_causal_for_open_tickets"
        ].__func__,
        "_open_quote_keys_for_book": coordinator_dict["_open_quote_keys_for_book"].__func__,
    }
    protected = frozenset(
        (
            *bound_targets,
            *static_targets,
            "_settle",
            "tick",
            "__getattribute__",
            "__init_subclass__",
        )
    )

    original_meta_setattr = metaclass.__setattr__
    original_meta_delattr = metaclass.__delattr__
    exact_issubclass = issubclass
    exact_type_error = TypeError

    def guarded_meta_setattr(cls, name: str, value: object) -> None:
        if exact_issubclass(cls, coordinator) and name in protected:
            raise exact_type_error(
                "canonical settlement consumer entry binding is immutable; "
                "canonical settlement coordinator dispatch is immutable"
            )
        original_meta_setattr(cls, name, value)

    def guarded_meta_delattr(cls, name: str) -> None:
        if exact_issubclass(cls, coordinator) and name in protected:
            raise exact_type_error(
                "canonical settlement consumer entry binding is immutable; "
                "canonical settlement coordinator dispatch is immutable"
            )
        original_meta_delattr(cls, name)

    # `_settlement_resolutions` crosses the callback-capable outcome-authority seam.
    # Reuse the same product-entry witness immediately after that call returns so a
    # callback cannot mutate validator/dispatch authority and reach learning prepare.
    product_authority_recheck = None

    def recheck_product_authority(*, snapshot: bool = False):
        checker = product_authority_recheck
        if checker is None:
            raise session_module.ContinuousSessionError(
                "canonical settlement product authority is unavailable"
            )
        if snapshot:
            return checker
        checker()
        return None

    recheck_product_authority_code = recheck_product_authority.__code__
    recheck_freevars = recheck_product_authority_code.co_freevars
    recheck_closure = recheck_product_authority.__closure__ or ()
    if "product_authority_recheck" not in recheck_freevars:
        raise exact_type_error("canonical settlement recheck closure is unavailable")
    recheck_checker_cell = recheck_closure[
        recheck_freevars.index("product_authority_recheck")
    ]

    # Capture the complete function/closure graph reachable from the exact Wave M
    # helper roots before installing their sealed descriptors. The old witness only
    # remembered each root's code object, so a caller could retarget a nested closure
    # cell or mutate a closure-held helper executable while leaving root code unchanged.
    expected_targets: dict[
        str,
        tuple[object, object, tuple[tuple[object, object], ...]],
    ] = {}
    pending_targets = list((*bound_targets.items(), *static_targets.items()))
    seen_target_ids: set[int] = set()
    while pending_targets:
        name, target = pending_targets.pop()
        if exact_type(target) is not function_type:
            raise exact_type_error("canonical settlement helper must be an exact function")
        target_identity = exact_id(target)
        if target_identity in seen_target_ids:
            continue
        seen_target_ids.add(target_identity)
        target_code = exact_getattr(target, "__code__", None)
        if target_code is None:
            raise exact_type_error("canonical settlement helper must expose executable code")
        target_closure = exact_getattr(target, "__closure__", None) or ()
        if len(target_closure) != len(target_code.co_freevars):
            raise exact_type_error("canonical settlement helper closure is malformed")
        cell_witnesses: list[tuple[object, object]] = []
        for freevar, cell in zip(target_code.co_freevars, target_closure):
            try:
                value = cell.cell_contents
            except ValueError as exc:
                raise exact_type_error(
                    "canonical settlement helper closure contains an empty cell"
                ) from exc
            cell_witnesses.append((cell, value))
            if exact_type(value) is function_type:
                pending_targets.append((f"{name}::{freevar}", value))
        expected_targets[name] = (
            target,
            target_code,
            tuple(cell_witnesses),
        )

    # Install exact captured dispatch first, bypassing the metaclass hook intentionally.
    expected_dispatch: dict[str, object] = {}
    post_callback_descriptor_get = None
    post_callback_descriptor_get_code = None
    post_callback_after_call_cell = None
    for name, target in bound_targets.items():
        descriptor = _sealed_dispatch(
            target,
            bind=True,
            after_call=(
                recheck_product_authority
                if name == "_settlement_resolutions"
                else None
            ),
        )
        type.__setattr__(coordinator, name, descriptor)
        expected_dispatch[name] = descriptor
        if name == "_settlement_resolutions":
            descriptor_get = type(descriptor).__dict__.get("__get__")
            if descriptor_get is None:
                raise exact_type_error(
                    "canonical settlement post-callback descriptor is unavailable"
                )
            descriptor_get_code = exact_getattr(descriptor_get, "__code__", None)
            if descriptor_get_code is None:
                raise exact_type_error(
                    "canonical settlement post-callback descriptor must expose executable code"
                )
            descriptor_freevars = descriptor_get_code.co_freevars
            descriptor_closure = descriptor_get.__closure__ or ()
            if "after_call" not in descriptor_freevars:
                raise exact_type_error(
                    "canonical settlement post-callback closure is unavailable"
                )
            post_callback_descriptor_get = descriptor_get
            post_callback_descriptor_get_code = descriptor_get_code
            post_callback_after_call_cell = descriptor_closure[
                descriptor_freevars.index("after_call")
            ]
    for name, target in static_targets.items():
        descriptor = _sealed_dispatch(target, bind=False)
        type.__setattr__(coordinator, name, descriptor)
        expected_dispatch[name] = descriptor

    # The economic entry witnesses exact objects through the closure-captured mapping
    # proxies and captures the executable code/cell graph of every hidden helper target.
    # A caller therefore cannot preserve a root function while retargeting one of its
    # authority-bearing closure cells or mutating a nested helper executable.
    witnessed_entry = _settlement_entry_with_dispatch_witness(
        settlement_entry,
        coordinator,
        coordinator_dict,
        expected_dispatch,
        expected_targets,
        resolution_dict=resolution_dict,
        resolution_validate=witnessed_resolution_validate,
    )

    # continuous_session seals `_settle` at the metaclass before this package-level
    # guard is imported. Temporarily remove that exact data descriptor only for the
    # trusted installation write, then restore the same object immediately. Without
    # this bounded handoff, type.__setattr__ correctly invokes the existing descriptor's
    # rejecting __set__ and package import fails before qualification can even start.
    type.__delattr__(metaclass, "_settle")
    try:
        type.__setattr__(coordinator, "_settle", witnessed_entry)
    finally:
        type.__setattr__(metaclass, "_settle", settlement_class_guard)

    # Settlement learning preparation occurs before the economic `_settle` entry. A
    # direct `type.__setattr__` can intentionally bypass the custom metaclass hook, so
    # the product tick itself must witness the exact settlement/validator authority
    # before any collector, state, or learning side effect can run.
    canonical_tick = coordinator_dict["tick"]
    canonical_tick_code = exact_getattr(canonical_tick, "__code__", None)
    if canonical_tick_code is None:
        raise exact_type_error("canonical continuous-session tick must expose executable code")

    def require_target_graph_authority() -> None:
        for target, expected_code, expected_cells in expected_targets.values():
            if exact_getattr(target, "__code__", None) is not expected_code:
                raise session_module.ContinuousSessionError(
                    "canonical settlement coordinator dispatch changed"
                )
            current_closure = exact_getattr(target, "__closure__", None) or ()
            if len(current_closure) != len(expected_cells):
                raise session_module.ContinuousSessionError(
                    "canonical settlement coordinator dispatch changed"
                )
            for current_cell, (expected_cell, expected_value) in zip(
                current_closure,
                expected_cells,
            ):
                if current_cell is not expected_cell:
                    raise session_module.ContinuousSessionError(
                        "canonical settlement coordinator dispatch changed"
                    )
                try:
                    current_value = current_cell.cell_contents
                except ValueError as exc:
                    raise session_module.ContinuousSessionError(
                        "canonical settlement coordinator dispatch changed"
                    ) from exc
                if current_value is not expected_value:
                    raise session_module.ContinuousSessionError(
                        "canonical settlement coordinator dispatch changed"
                    )

    def require_product_entry_authority() -> None:
        if coordinator_dict.get("_settle") is not witnessed_entry:
            raise session_module.ContinuousSessionError(
                "canonical settlement product entry dispatch changed"
            )
        if metaclass_dict.get("_settle") is not settlement_class_guard:
            raise session_module.ContinuousSessionError(
                "canonical settlement consumer class guard changed"
            )
        if coordinator_dict.get("tick") is not guarded_tick:
            raise session_module.ContinuousSessionError(
                "canonical continuous-session tick dispatch changed"
            )
        if coordinator_dict.get("__getattribute__") is not guarded_instance_getattribute:
            raise session_module.ContinuousSessionError(
                "canonical continuous-session lookup dispatch changed"
            )
        if resolution_dict.get("validate") is not witnessed_resolution_validate:
            raise session_module.ContinuousSessionError(
                "canonical settlement resolution validation dispatch changed"
            )
        if exact_getattr(canonical_tick, "__code__", None) is not canonical_tick_code:
            raise session_module.ContinuousSessionError(
                "canonical continuous-session tick executable changed"
            )
        if (
            post_callback_descriptor_get is None
            or post_callback_descriptor_get_code is None
            or post_callback_after_call_cell is None
            or exact_getattr(post_callback_descriptor_get, "__code__", None)
            is not post_callback_descriptor_get_code
            or exact_getattr(recheck_product_authority, "__code__", None)
            is not recheck_product_authority_code
        ):
            raise session_module.ContinuousSessionError(
                "canonical settlement post-callback authority changed"
            )
        try:
            current_after_call = post_callback_after_call_cell.cell_contents
            current_checker = recheck_checker_cell.cell_contents
        except ValueError as exc:
            raise session_module.ContinuousSessionError(
                "canonical settlement post-callback authority changed"
            ) from exc
        if (
            current_after_call is not recheck_product_authority
            or current_checker is not require_product_entry_authority
        ):
            raise session_module.ContinuousSessionError(
                "canonical settlement post-callback authority changed"
            )
        for name, expected in expected_dispatch.items():
            if coordinator_dict.get(name) is not expected:
                raise session_module.ContinuousSessionError(
                    "canonical settlement coordinator dispatch changed"
                )
        require_target_graph_authority()

    product_authority_recheck = require_product_entry_authority

    def guarded_tick(self, *args, **kwargs):
        require_product_entry_authority()
        result = canonical_tick(self, *args, **kwargs)
        require_product_entry_authority()
        return result

    guarded_tick.__name__ = canonical_tick.__name__
    guarded_tick.__qualname__ = canonical_tick.__qualname__
    guarded_tick.__doc__ = canonical_tick.__doc__
    guarded_tick.__annotations__ = canonical_tick.__annotations__

    def guarded_instance_getattribute(self, name):
        # Instance attribute lookup runs before a returned class-level method can be
        # dispatched. Witness the exact product-entry slot here so a direct
        # ``type.__setattr__(Coordinator, "tick", hostile)`` cannot bypass the
        # preflight by replacing the preflight wrapper itself.
        if name == "tick" and coordinator_dict.get("tick") is not guarded_tick:
            raise session_module.ContinuousSessionError(
                "canonical continuous-session tick dispatch changed"
            )
        return exact_object_getattribute(self, name)

    type.__setattr__(coordinator, "tick", guarded_tick)
    type.__setattr__(coordinator, "__getattribute__", guarded_instance_getattribute)

    def reject_subclass(cls, **_kwargs) -> None:
        raise exact_type_error("canonical settlement coordinator is not extensible")

    type.__setattr__(coordinator, "__init_subclass__", classmethod(reject_subclass))
    type.__setattr__(metaclass, "__setattr__", guarded_meta_setattr)
    type.__setattr__(metaclass, "__delattr__", guarded_meta_delattr)


_install()
del _install
del _sealed_dispatch
del _settlement_entry_with_dispatch_witness
del _validated_resolution_with_authority_witness
del _build_validated_outcomes_type
