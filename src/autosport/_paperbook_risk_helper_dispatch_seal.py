"""Seal PaperRiskPolicy helper dispatch used by generation-stable read roots.

The generation guard stores pre-guard risk roots as inert executable specs and
reconstructs them only inside the PaperBook publication-lock critical section.  Those
original classmethod bodies still accept a ``cls`` argument, however, and can therefore
resolve later replacements of helpers such as ``_exact_positive_sum`` through the live
PaperRiskPolicy class.

Post-compose only those inert specs with a tiny frozen helper surface captured from the
canonical policy.  This retains the existing risk implementation and generation guard;
it adds no state, estimator, policy, journal, parser, or separate authority.
"""

from __future__ import annotations

from types import FunctionType

from . import risk as _risk


def _descriptor_function(descriptor: object, expected_type: type) -> FunctionType:
    if type(descriptor) is not expected_type:
        raise RuntimeError("canonical PaperRiskPolicy helper descriptor changed")
    function = descriptor.__func__
    if type(function) is not FunctionType:
        raise RuntimeError("canonical PaperRiskPolicy helper is not a Python function")
    return function


def _from_spec(spec: object) -> FunctionType:
    if type(spec) is not tuple or len(spec) != 6:
        raise RuntimeError("canonical PaperRiskPolicy inert delegate spec changed")
    code, name, defaults, kwdefaults, closure, global_bindings = spec
    if not isinstance(code, type((lambda: None).__code__)) or type(name) is not str:
        raise RuntimeError("canonical PaperRiskPolicy inert delegate spec is invalid")
    if type(global_bindings) is not tuple:
        raise RuntimeError("canonical PaperRiskPolicy inert delegate globals changed")
    function = FunctionType(
        code,
        dict(global_bindings),
        name=name,
        argdefs=defaults,
        closure=closure,
    )
    if kwdefaults is not None:
        if type(kwdefaults) is not dict:
            raise RuntimeError("canonical PaperRiskPolicy inert delegate kwdefaults changed")
        function.__kwdefaults__ = dict(kwdefaults)
    return function


def _spec(function: FunctionType) -> tuple[object, ...]:
    return (
        function.__code__,
        function.__name__,
        function.__defaults__,
        None if function.__kwdefaults__ is None else dict(function.__kwdefaults__),
        function.__closure__,
        tuple(function.__globals__.items()),
    )


def _install() -> None:
    policy = _risk.PaperRiskPolicy
    namespace = vars(policy)
    guarded_book_state = _descriptor_function(namespace.get("_book_state"), classmethod)
    private_globals = guarded_book_state.__globals__

    spec_names = (
        "_BOOK_STATE_SPEC",
        "_PORTFOLIO_HASH_SPEC",
        "_HISTORICAL_METRICS_SPEC",
        "_IDENTITY_CONCENTRATION_SPEC",
    )
    if any(name not in private_globals for name in spec_names):
        raise RuntimeError("canonical generation-stable risk read specs are unavailable")

    original_book_state = _from_spec(private_globals["_BOOK_STATE_SPEC"])
    original_portfolio_hash = _from_spec(private_globals["_PORTFOLIO_HASH_SPEC"])
    original_historical_metrics = _from_spec(private_globals["_HISTORICAL_METRICS_SPEC"])
    original_identity_concentration = _from_spec(
        private_globals["_IDENTITY_CONCENTRATION_SPEC"]
    )

    exact_positive_sum = _descriptor_function(
        namespace.get("_exact_positive_sum"), staticmethod
    )
    decimal_context = _descriptor_function(namespace.get("_decimal_context"), staticmethod)
    fraction_exceeds = _descriptor_function(
        namespace.get("_fraction_exceeds"), staticmethod
    )

    class FrozenPolicyReadHelpers:
        _exact_positive_sum = staticmethod(exact_positive_sum)
        _decimal_context = staticmethod(decimal_context)
        _fraction_exceeds = staticmethod(fraction_exceeds)

    def frozen_book_state(book):
        return original_book_state(FrozenPolicyReadHelpers, book)

    FrozenPolicyReadHelpers._book_state = staticmethod(frozen_book_state)

    def sealed_book_state(_cls, book):
        return original_book_state(FrozenPolicyReadHelpers, book)

    def sealed_portfolio_hash(_cls, book):
        return original_portfolio_hash(FrozenPolicyReadHelpers, book)

    def sealed_historical_metrics(
        _cls,
        book,
        *,
        realized_loss_window=None,
        causal_cutoff=None,
    ):
        return original_historical_metrics(
            FrozenPolicyReadHelpers,
            book,
            realized_loss_window=realized_loss_window,
            causal_cutoff=causal_cutoff,
        )

    def sealed_identity_concentration(
        _cls,
        book,
        amount,
        context,
        *,
        dimension,
        limit,
    ):
        return original_identity_concentration(
            FrozenPolicyReadHelpers,
            book,
            amount,
            context,
            dimension=dimension,
            limit=limit,
        )

    private_globals["_BOOK_STATE_SPEC"] = _spec(sealed_book_state)
    private_globals["_PORTFOLIO_HASH_SPEC"] = _spec(sealed_portfolio_hash)
    private_globals["_HISTORICAL_METRICS_SPEC"] = _spec(sealed_historical_metrics)
    private_globals["_IDENTITY_CONCENTRATION_SPEC"] = _spec(
        sealed_identity_concentration
    )


_install()
del _install
