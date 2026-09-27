"""Bind canonical PaperRiskPolicy reads to one current PaperBook generation.

PaperBook generation CAS prevents stale mutation/persistence, but risk policy derives
portfolio/equity evidence from several direct PaperBook fields after validation.  A
writer could otherwise publish a newer durable generation between validation and those
reads, or a later class-descriptor rebind could bypass the live validator dispatch.

Reuse the already-sealed persistence graph: acquire the same publication lock for the
entire risk derivation and run the frozen canonical loaded-state validator before and
after the original risk calculation.  This module creates no risk store, parser,
serializer, journal, or generation authority.
"""

from __future__ import annotations

from types import FunctionType

from . import _paperbook_preload_authority_guard as _authority_guard
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


def _guarded_risk_call(book, delegate, args, kwargs):
    """Run one risk derivation against a generation-stable canonical PaperBook."""

    snapshot_path = _FROZEN_BOUND_SNAPSHOT_PATH(book)
    publication_lock = None
    try:
        if snapshot_path is not None:
            publication_lock = _FROZEN_ACQUIRE_LOCK(_FROZEN_WITNESS_PATH(snapshot_path))
        _FROZEN_VALIDATE_LOADED_STATE(_CANONICAL_PAPER_BOOK, book)
        result = delegate(*args, **kwargs)
        _FROZEN_VALIDATE_LOADED_STATE(_CANONICAL_PAPER_BOOK, book)
        return result
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        # Canonical risk helpers already use None as their fail-closed invalid-state
        # result. Preserve that contract for generation/lock/validator failures.
        return None
    finally:
        if publication_lock is not None:
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

    # The public load wrapper owns the detached, already-sealed generation helper
    # graph. Reuse those exact helpers instead of reaching back through mutable
    # authority-module globals.
    load_descriptor = vars(paper_book).get("load")
    if type(load_descriptor) is not classmethod or type(load_descriptor.__func__) is not FunctionType:
        raise RuntimeError("canonical sealed PaperBook load wrapper is unavailable")
    load_wrapper_globals = load_descriptor.__func__.__globals__
    frozen_load = load_wrapper_globals.get("_FROZEN_LOAD")
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

    namespace = vars(policy)
    book_state = _descriptor_function(namespace.get("_book_state"), classmethod)
    portfolio_hash = _descriptor_function(
        namespace.get("risk_of_ruin_portfolio_sha256"), classmethod
    )
    historical_metrics = _descriptor_function(
        namespace.get("_historical_risk_metrics"), classmethod
    )
    shadow_descriptor = namespace.get("_shadow_book_for_allocation")
    shadow_book = _descriptor_function(shadow_descriptor, staticmethod)

    private_globals: dict[str, object] = dict(globals())
    private_globals.update(
        {
            "_CANONICAL_PAPER_BOOK": paper_book,
            "_FROZEN_BOUND_SNAPSHOT_PATH": bound_snapshot_path,
            "_FROZEN_ACQUIRE_LOCK": acquire_lock,
            "_FROZEN_RELEASE_LOCK": release_lock,
            "_FROZEN_WITNESS_PATH": witness_path,
            "_FROZEN_VALIDATE_LOADED_STATE": validate_loaded_state,
            "_ORIGINAL_BOOK_STATE": book_state,
            "_ORIGINAL_PORTFOLIO_HASH": portfolio_hash,
            "_ORIGINAL_HISTORICAL_METRICS": historical_metrics,
            "_ORIGINAL_SHADOW_BOOK": shadow_book,
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

    guarded_book_state.__name__ = "_book_state"
    guarded_book_state.__qualname__ = "PaperRiskPolicy._book_state"
    guarded_portfolio_hash.__name__ = "risk_of_ruin_portfolio_sha256"
    guarded_portfolio_hash.__qualname__ = "PaperRiskPolicy.risk_of_ruin_portfolio_sha256"
    guarded_historical_metrics.__name__ = "_historical_risk_metrics"
    guarded_historical_metrics.__qualname__ = "PaperRiskPolicy._historical_risk_metrics"
    guarded_shadow_book.__name__ = "_shadow_book_for_allocation"
    guarded_shadow_book.__qualname__ = "PaperRiskPolicy._shadow_book_for_allocation"

    policy._book_state = classmethod(guarded_book_state)
    policy.risk_of_ruin_portfolio_sha256 = classmethod(guarded_portfolio_hash)
    policy._historical_risk_metrics = classmethod(guarded_historical_metrics)
    policy._shadow_book_for_allocation = staticmethod(guarded_shadow_book)


_install()
del _install
