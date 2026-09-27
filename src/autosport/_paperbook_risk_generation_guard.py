"""Bind canonical PaperRiskPolicy reads to one current PaperBook generation.

PaperBook generation CAS prevents stale mutation/persistence, but risk policy derives
portfolio/equity evidence from several direct PaperBook fields after validation. A
writer could otherwise publish a newer durable generation between validation and those
reads, or a later class-descriptor rebind could bypass the live validator dispatch.

Reuse the already-sealed persistence graph: acquire the same publication lock for the
entire risk derivation, require the existing frozen PaperBook class-callable graph, and
run the frozen canonical loaded-state validator before and after the original risk
calculation. Nested risk helpers reuse the same thread-local read critical section so
higher-level whole-portfolio checks can remain atomic without making the canonical
writer lock reentrant. This module creates no risk store, parser, serializer, journal,
or generation authority.
"""

from __future__ import annotations

import threading
from types import FunctionType

from . import paper as _paper
from . import risk as _risk


class _InvalidRiskSurface(RuntimeError):
    pass


def _descriptor_function(descriptor: object, expected_type: type) -> FunctionType:
    if type(descriptor) is not expected_type:
        raise _InvalidRiskSurface("canonical PaperRiskPolicy descriptor type changed")
    function = descriptor.__func__
    if type(function) is not FunctionType:
        raise _InvalidRiskSurface("canonical PaperRiskPolicy descriptor is not a Python function")
    return function


def _plain_function(value: object) -> FunctionType:
    if type(value) is not FunctionType:
        raise _InvalidRiskSurface("canonical PaperRiskPolicy method is not a Python function")
    return value


def _clone_module_function_graph(function: FunctionType) -> FunctionType:
    """Detach one verifier and its same-module Python helper graph."""

    module_globals = function.__globals__
    trusted_globals: dict[str, object] = dict(module_globals)
    clones: dict[str, FunctionType] = {}
    for name, value in tuple(module_globals.items()):
        if type(value) is FunctionType and value.__globals__ is module_globals:
            clone = FunctionType(
                value.__code__,
                trusted_globals,
                name=value.__name__,
                argdefs=value.__defaults__,
                closure=value.__closure__,
            )
            if value.__kwdefaults__ is not None:
                clone.__kwdefaults__ = dict(value.__kwdefaults__)
            clone.__annotations__ = dict(value.__annotations__)
            clone.__doc__ = value.__doc__
            clone.__qualname__ = value.__qualname__
            clones[name] = clone
    trusted_globals.update(clones)
    frozen = clones.get(function.__name__)
    if frozen is None:
        raise RuntimeError("canonical PaperBook class-graph verifier clone is unavailable")
    return frozen


def _sealed_load_trusted_globals(load_wrapper: FunctionType) -> dict[str, object]:
    """Recover the closure-private globals snapshot created by the final wrapper seal."""

    closure = load_wrapper.__closure__
    if closure is None:
        raise RuntimeError("canonical sealed PaperBook load wrapper has no closure authority")
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if (
            type(value) is dict
            and type(value.get("_FROZEN_LOAD")) is FunctionType
            and "_LOAD_CLASS_CALLABLE_GRAPH_WITNESSES" in value
            and type(value.get("_require_class_callable_graph_witnesses")) is FunctionType
        ):
            return value
    raise RuntimeError("canonical sealed PaperBook load trusted globals are unavailable")


def _guarded_risk_call(book, delegate, args, kwargs, failure_result=None):
    """Run one risk derivation against a generation-stable canonical PaperBook."""

    snapshot_path = None
    held_reads = None
    try:
        _FROZEN_REQUIRE_CLASS_GRAPH(
            _CANONICAL_PAPER_BOOK,
            _PAPERBOOK_CLASS_WITNESSES,
        )
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
        _FROZEN_VALIDATE_LOADED_STATE(_CANONICAL_PAPER_BOOK, book)
        result = delegate(*args, **kwargs)
        _FROZEN_VALIDATE_LOADED_STATE(_CANONICAL_PAPER_BOOK, book)
        _FROZEN_REQUIRE_CLASS_GRAPH(
            _CANONICAL_PAPER_BOOK,
            _PAPERBOOK_CLASS_WITNESSES,
        )
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
    return _guarded_risk_call(book, _ORIGINAL_BOOK_STATE, (cls, book), {})


def _portfolio_hash_template(cls, book):
    return _guarded_risk_call(book, _ORIGINAL_PORTFOLIO_HASH, (cls, book), {})


def _historical_metrics_template(cls, book, *, realized_loss_window=None, causal_cutoff=None):
    return _guarded_risk_call(
        book,
        _ORIGINAL_HISTORICAL_METRICS,
        (cls, book),
        {
            "realized_loss_window": realized_loss_window,
            "causal_cutoff": causal_cutoff,
        },
    )


def _shadow_book_template(book):
    return _guarded_risk_call(book, _ORIGINAL_SHADOW_BOOK, (book,), {})


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
    return _guarded_risk_call(
        book,
        _ORIGINAL_IDENTITY_CONCENTRATION,
        (cls, book, amount, context),
        {"dimension": dimension, "limit": limit},
        failure,
    )


def _derive_goal_stake_template(self, book, signal_strength, *, context=None):
    return _guarded_risk_call(
        book,
        _ORIGINAL_DERIVE_GOAL_STAKE,
        (self, book, signal_strength),
        {"context": context},
    )


def _evaluate_template(self, book, stake, *, context=None):
    failure = _RISK_DECISION(
        False,
        "virtual bankroll generation authority is invalid",
    )
    return _guarded_risk_call(
        book,
        _ORIGINAL_EVALUATE,
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
    context_count = len(contexts) if type(contexts) is tuple else 0
    failure = _STAKE_VECTOR_DECISION(
        "WAIT",
        (_ZERO_DECIMAL,) * context_count,
        "virtual bankroll generation authority is invalid",
    )
    return _guarded_risk_call(
        book,
        _ORIGINAL_DERIVE_GOAL_STAKE_VECTOR,
        (self, book, signal_strengths),
        {
            "contexts": contexts,
            "risk_of_ruin_vector_evidence": risk_of_ruin_vector_evidence,
        },
        failure,
    )


def _clone_template(template: FunctionType, private_globals: dict[str, object]) -> FunctionType:
    clone = FunctionType(
        template.__code__,
        private_globals,
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
    paper_book = _paper.PaperBook
    policy = _risk.PaperRiskPolicy

    load_descriptor = vars(paper_book).get("load")
    if type(load_descriptor) is not classmethod or type(load_descriptor.__func__) is not FunctionType:
        raise RuntimeError("canonical sealed PaperBook load wrapper is unavailable")
    load_trusted_globals = _sealed_load_trusted_globals(load_descriptor.__func__)
    frozen_load = load_trusted_globals.get("_FROZEN_LOAD")
    if type(frozen_load) is not FunctionType:
        raise RuntimeError("canonical frozen PaperBook load graph is unavailable")
    sealed_globals = frozen_load.__globals__

    helper_names = (
        "_bound_snapshot_path",
        "_acquire_snapshot_publication_lock",
        "_release_snapshot_publication_lock",
        "_witness_path",
        "_generation_guarded_validate_loaded_state",
    )
    helpers = tuple(sealed_globals.get(name) for name in helper_names)
    if any(type(value) is not FunctionType for value in helpers):
        raise RuntimeError("canonical sealed PaperBook generation helper graph is unavailable")
    (
        bound_snapshot_path,
        acquire_lock,
        release_lock,
        witness_path,
        validate_loaded_state,
    ) = helpers

    class_graph_verifier = load_trusted_globals.get(
        "_require_class_callable_graph_witnesses"
    )
    class_witnesses = load_trusted_globals.get(
        "_LOAD_CLASS_CALLABLE_GRAPH_WITNESSES"
    )
    if type(class_graph_verifier) is not FunctionType or type(class_witnesses) is not tuple:
        raise RuntimeError("canonical PaperBook class-callable witness is unavailable")
    frozen_class_graph_verifier = _clone_module_function_graph(class_graph_verifier)

    namespace = vars(policy)
    book_state = _descriptor_function(namespace.get("_book_state"), classmethod)
    portfolio_hash = _descriptor_function(
        namespace.get("risk_of_ruin_portfolio_sha256"), classmethod
    )
    historical_metrics = _descriptor_function(
        namespace.get("_historical_risk_metrics"), classmethod
    )
    shadow_book = _descriptor_function(
        namespace.get("_shadow_book_for_allocation"), staticmethod
    )
    identity_concentration = _descriptor_function(
        namespace.get("_identity_concentration_decision"), classmethod
    )
    derive_goal_stake = _plain_function(namespace.get("derive_goal_stake"))
    derive_goal_stake_vector = _plain_function(namespace.get("derive_goal_stake_vector"))
    evaluate = _plain_function(namespace.get("evaluate"))

    private_globals: dict[str, object] = dict(globals())
    private_globals.update(
        {
            "_CANONICAL_PAPER_BOOK": paper_book,
            "_PAPERBOOK_CLASS_WITNESSES": class_witnesses,
            "_FROZEN_REQUIRE_CLASS_GRAPH": frozen_class_graph_verifier,
            "_FROZEN_BOUND_SNAPSHOT_PATH": bound_snapshot_path,
            "_FROZEN_ACQUIRE_LOCK": acquire_lock,
            "_FROZEN_RELEASE_LOCK": release_lock,
            "_FROZEN_WITNESS_PATH": witness_path,
            "_FROZEN_VALIDATE_LOADED_STATE": validate_loaded_state,
            "_RISK_READ_LOCAL": threading.local(),
            "_RISK_DECISION": _risk.RiskDecision,
            "_STAKE_VECTOR_DECISION": _risk.StakeVectorDecision,
            "_ZERO_DECIMAL": _risk.Decimal("0"),
            "_ORIGINAL_BOOK_STATE": book_state,
            "_ORIGINAL_PORTFOLIO_HASH": portfolio_hash,
            "_ORIGINAL_HISTORICAL_METRICS": historical_metrics,
            "_ORIGINAL_SHADOW_BOOK": shadow_book,
            "_ORIGINAL_IDENTITY_CONCENTRATION": identity_concentration,
            "_ORIGINAL_DERIVE_GOAL_STAKE": derive_goal_stake,
            "_ORIGINAL_DERIVE_GOAL_STAKE_VECTOR": derive_goal_stake_vector,
            "_ORIGINAL_EVALUATE": evaluate,
        }
    )
    private_globals["_guarded_risk_call"] = _clone_template(
        _guarded_risk_call, private_globals
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
