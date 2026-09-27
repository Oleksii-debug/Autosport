"""Bind PaperRiskPolicy derivations to PaperBook's private economic authority.

Structural lifecycle replay can prove internal coherence but cannot prove that caller-
visible ticket economics are still the product-issued opening/causal facts. Reuse the
existing opening and causal registries for every risk derivation, inside the same
publication-lock/read critical section installed by the generation-risk guard.

This module creates no risk, persistence, registry, journal, or generation authority.
It only composes the already-canonical private PaperBook checks around the already-
guarded PaperRiskPolicy surface.
"""

from __future__ import annotations

from types import FunctionType

from . import _paperbook_preload_authority_guard as _authority
from . import paper as _paper
from . import risk as _risk


def _function_for_descriptor(owner: type, name: str, descriptor_type: type) -> FunctionType:
    descriptor = vars(owner).get(name)
    if type(descriptor) is not descriptor_type:
        raise RuntimeError(f"canonical risk descriptor changed: {name}")
    function = descriptor.__func__
    if type(function) is not FunctionType:
        raise RuntimeError(f"canonical risk executable changed: {name}")
    return function


def _plain_function(owner: type, name: str) -> FunctionType:
    function = vars(owner).get(name)
    if type(function) is not FunctionType:
        raise RuntimeError(f"canonical risk method changed: {name}")
    return function


def _delegate_spec(function: FunctionType) -> tuple[object, ...]:
    return (
        function.__code__,
        function.__name__,
        function.__defaults__,
        None if function.__kwdefaults__ is None else dict(function.__kwdefaults__),
        function.__closure__,
        tuple(function.__globals__.items()),
    )


def _call_spec(spec: tuple[object, ...], args: tuple[object, ...], kwargs: dict[str, object]):
    code, name, defaults, kwdefaults, closure, global_bindings = spec
    delegate = _FUNCTION_TYPE(
        code,
        dict(global_bindings),
        name=name,
        argdefs=defaults,
        closure=closure,
    )
    if kwdefaults is not None:
        delegate.__kwdefaults__ = dict(kwdefaults)
    return delegate(*args, **kwargs)


def _require_private_authority(book: object) -> None:
    _FROZEN_CALL_WITNESSED(
        _REQUIRE_OPENING,
        _REQUIRE_OPENING_WITNESS,
        "opening-authority risk read",
        book,
    )
    _FROZEN_CALL_WITNESSED(
        _REQUIRE_CAUSAL,
        _REQUIRE_CAUSAL_WITNESS,
        "causal-authority risk read",
        book,
    )


def _guarded_private_call(
    book: object,
    delegate_spec: tuple[object, ...],
    args: tuple[object, ...],
    kwargs: dict[str, object],
    failure_result=None,
):
    snapshot_path = None
    held_reads = None
    try:
        snapshot_path = _FROZEN_BOUND_SNAPSHOT_PATH(book)
        if snapshot_path is not None:
            held_reads = getattr(_RISK_READ_LOCAL, "held", None)
            if held_reads is None:
                held_reads = {}
                _RISK_READ_LOCAL.held = held_reads
            entry = held_reads.get(snapshot_path)
            if entry is None:
                publication_lock = _FROZEN_ACQUIRE_LOCK(
                    _FROZEN_WITNESS_PATH(snapshot_path)
                )
                held_reads[snapshot_path] = [1, publication_lock]
            else:
                entry[0] += 1

        _require_private_authority(book)
        result = _call_spec(delegate_spec, args, kwargs)
        _require_private_authority(book)
        return result
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return failure_result
    finally:
        if snapshot_path is not None and held_reads is not None:
            entry = held_reads.get(snapshot_path)
            if entry is not None:
                entry[0] -= 1
                if entry[0] == 0:
                    _count, publication_lock = held_reads.pop(snapshot_path)
                    _FROZEN_RELEASE_LOCK(publication_lock)


def _book_state_template(cls, book):
    return _guarded_private_call(book, _BOOK_STATE_SPEC, (cls, book), {})


def _portfolio_hash_template(cls, book):
    return _guarded_private_call(book, _PORTFOLIO_HASH_SPEC, (cls, book), {})


def _historical_metrics_template(cls, book, *, realized_loss_window=None, causal_cutoff=None):
    return _guarded_private_call(
        book,
        _HISTORICAL_METRICS_SPEC,
        (cls, book),
        {
            "realized_loss_window": realized_loss_window,
            "causal_cutoff": causal_cutoff,
        },
    )


def _shadow_book_template(book):
    return _guarded_private_call(book, _SHADOW_BOOK_SPEC, (book,), {})


def _identity_concentration_template(
    cls,
    book,
    amount,
    context,
    *,
    dimension,
    limit,
):
    failure = _RISK_DECISION(
        False,
        f"owner {dimension} concentration evidence is invalid",
    )
    return _guarded_private_call(
        book,
        _IDENTITY_CONCENTRATION_SPEC,
        (cls, book, amount, context),
        {"dimension": dimension, "limit": limit},
        failure,
    )


def _derive_goal_stake_template(self, book, signal_strength, *, context=None):
    return _guarded_private_call(
        book,
        _DERIVE_GOAL_STAKE_SPEC,
        (self, book, signal_strength),
        {"context": context},
    )


def _evaluate_template(self, book, stake, *, context=None):
    failure = _RISK_DECISION(
        False,
        "virtual bankroll private economic authority is invalid",
    )
    return _guarded_private_call(
        book,
        _EVALUATE_SPEC,
        (self, book, stake),
        {"context": context},
        failure,
    )


def _derive_goal_stake_vector_template(
    self,
    book,
    signal_strengths,
    *,
    contexts,
    risk_of_ruin_vector_evidence=None,
):
    count = len(contexts) if type(contexts) is tuple else 0
    failure = _STAKE_VECTOR_DECISION(
        "WAIT",
        (_ZERO_DECIMAL,) * count,
        "virtual bankroll private economic authority is invalid",
    )
    return _guarded_private_call(
        book,
        _DERIVE_GOAL_STAKE_VECTOR_SPEC,
        (self, book, signal_strengths),
        {
            "contexts": contexts,
            "risk_of_ruin_vector_evidence": risk_of_ruin_vector_evidence,
        },
        failure,
    )


def _clone_template(template: FunctionType, globals_mapping: dict[str, object]) -> FunctionType:
    clone = FunctionType(
        template.__code__,
        globals_mapping,
        name=template.__name__,
        argdefs=template.__defaults__,
        closure=template.__closure__,
    )
    if template.__kwdefaults__ is not None:
        clone.__kwdefaults__ = dict(template.__kwdefaults__)
    clone.__annotations__ = dict(template.__annotations__)
    clone.__doc__ = template.__doc__
    return clone


def _install() -> None:
    policy = _risk.PaperRiskPolicy
    book_state = _function_for_descriptor(policy, "_book_state", classmethod)
    portfolio_hash = _function_for_descriptor(
        policy, "risk_of_ruin_portfolio_sha256", classmethod
    )
    historical_metrics = _function_for_descriptor(
        policy, "_historical_risk_metrics", classmethod
    )
    shadow_book = _function_for_descriptor(
        policy, "_shadow_book_for_allocation", staticmethod
    )
    identity_concentration = _function_for_descriptor(
        policy, "_identity_concentration_decision", classmethod
    )
    derive_goal_stake = _plain_function(policy, "derive_goal_stake")
    derive_goal_stake_vector = _plain_function(policy, "derive_goal_stake_vector")
    evaluate = _plain_function(policy, "evaluate")

    generation_globals = book_state.__globals__
    helper_names = (
        "_FROZEN_BOUND_SNAPSHOT_PATH",
        "_FROZEN_ACQUIRE_LOCK",
        "_FROZEN_RELEASE_LOCK",
        "_FROZEN_WITNESS_PATH",
        "_FROZEN_CALL_WITNESSED",
    )
    helpers = tuple(generation_globals.get(name) for name in helper_names)
    if any(type(value) is not FunctionType for value in helpers):
        raise RuntimeError("canonical generation-stable risk helper graph is unavailable")
    (
        bound_snapshot_path,
        acquire_lock,
        release_lock,
        witness_path,
        call_witnessed,
    ) = helpers
    risk_read_local = generation_globals.get("_RISK_READ_LOCAL")
    if risk_read_local is None:
        raise RuntimeError("canonical generation-stable risk critical section is unavailable")

    require_opening = _paper._require_ticket_opening_authority
    require_causal = _paper._require_paperbook_causal_history_authority
    if type(require_opening) is not FunctionType or type(require_causal) is not FunctionType:
        raise RuntimeError("canonical PaperBook private economic authority is unavailable")
    opening_witness = _authority._capture_delegate_witness(
        require_opening,
        "opening-authority risk read",
    )
    causal_witness = _authority._capture_delegate_witness(
        require_causal,
        "causal-authority risk read",
    )

    private_globals: dict[str, object] = dict(globals())
    private_globals.update(
        {
            "_FUNCTION_TYPE": FunctionType,
            "_FROZEN_BOUND_SNAPSHOT_PATH": bound_snapshot_path,
            "_FROZEN_ACQUIRE_LOCK": acquire_lock,
            "_FROZEN_RELEASE_LOCK": release_lock,
            "_FROZEN_WITNESS_PATH": witness_path,
            "_FROZEN_CALL_WITNESSED": call_witnessed,
            "_RISK_READ_LOCAL": risk_read_local,
            "_REQUIRE_OPENING": require_opening,
            "_REQUIRE_OPENING_WITNESS": opening_witness,
            "_REQUIRE_CAUSAL": require_causal,
            "_REQUIRE_CAUSAL_WITNESS": causal_witness,
            "_RISK_DECISION": _risk.RiskDecision,
            "_STAKE_VECTOR_DECISION": _risk.StakeVectorDecision,
            "_ZERO_DECIMAL": _risk.Decimal("0"),
            "_BOOK_STATE_SPEC": _delegate_spec(book_state),
            "_PORTFOLIO_HASH_SPEC": _delegate_spec(portfolio_hash),
            "_HISTORICAL_METRICS_SPEC": _delegate_spec(historical_metrics),
            "_SHADOW_BOOK_SPEC": _delegate_spec(shadow_book),
            "_IDENTITY_CONCENTRATION_SPEC": _delegate_spec(identity_concentration),
            "_DERIVE_GOAL_STAKE_SPEC": _delegate_spec(derive_goal_stake),
            "_DERIVE_GOAL_STAKE_VECTOR_SPEC": _delegate_spec(derive_goal_stake_vector),
            "_EVALUATE_SPEC": _delegate_spec(evaluate),
        }
    )
    private_globals["_call_spec"] = _clone_template(_call_spec, private_globals)
    private_globals["_require_private_authority"] = _clone_template(
        _require_private_authority, private_globals
    )
    private_globals["_guarded_private_call"] = _clone_template(
        _guarded_private_call, private_globals
    )

    guarded_book_state = _clone_template(_book_state_template, private_globals)
    guarded_portfolio_hash = _clone_template(_portfolio_hash_template, private_globals)
    guarded_historical_metrics = _clone_template(
        _historical_metrics_template, private_globals
    )
    guarded_shadow_book = _clone_template(_shadow_book_template, private_globals)
    guarded_identity_concentration = _clone_template(
        _identity_concentration_template, private_globals
    )
    guarded_derive_goal_stake = _clone_template(
        _derive_goal_stake_template, private_globals
    )
    guarded_derive_goal_stake_vector = _clone_template(
        _derive_goal_stake_vector_template, private_globals
    )
    guarded_evaluate = _clone_template(_evaluate_template, private_globals)

    guarded_book_state.__name__ = "_book_state"
    guarded_book_state.__qualname__ = "PaperRiskPolicy._book_state"
    guarded_portfolio_hash.__name__ = "risk_of_ruin_portfolio_sha256"
    guarded_portfolio_hash.__qualname__ = "PaperRiskPolicy.risk_of_ruin_portfolio_sha256"
    guarded_historical_metrics.__name__ = "_historical_risk_metrics"
    guarded_historical_metrics.__qualname__ = "PaperRiskPolicy._historical_risk_metrics"
    guarded_shadow_book.__name__ = "_shadow_book_for_allocation"
    guarded_shadow_book.__qualname__ = "PaperRiskPolicy._shadow_book_for_allocation"
    guarded_identity_concentration.__name__ = "_identity_concentration_decision"
    guarded_identity_concentration.__qualname__ = (
        "PaperRiskPolicy._identity_concentration_decision"
    )
    guarded_derive_goal_stake.__name__ = "derive_goal_stake"
    guarded_derive_goal_stake.__qualname__ = "PaperRiskPolicy.derive_goal_stake"
    guarded_derive_goal_stake_vector.__name__ = "derive_goal_stake_vector"
    guarded_derive_goal_stake_vector.__qualname__ = "PaperRiskPolicy.derive_goal_stake_vector"
    guarded_evaluate.__name__ = "evaluate"
    guarded_evaluate.__qualname__ = "PaperRiskPolicy.evaluate"

    policy._book_state = classmethod(guarded_book_state)
    policy.risk_of_ruin_portfolio_sha256 = classmethod(guarded_portfolio_hash)
    policy._historical_risk_metrics = classmethod(guarded_historical_metrics)
    policy._shadow_book_for_allocation = staticmethod(guarded_shadow_book)
    policy._identity_concentration_decision = classmethod(guarded_identity_concentration)
    policy.derive_goal_stake = guarded_derive_goal_stake
    policy.derive_goal_stake_vector = guarded_derive_goal_stake_vector
    policy.evaluate = guarded_evaluate


_install()
del _install
