"""Bind authenticated Stream freshness timing to each runtime instance.

The owning composition intentionally accepts no caller-supplied receive, ingest, or
as-of timestamp.  Its public methods therefore must not re-resolve the mutable
``time.time_ns`` module attribute after a runtime has been admitted.  This guard keeps
the existing composition authoritative: it captures the product clock callable once at
runtime construction, activates it only while the owning methods execute, and fails
closed if the clock dispatch binding itself is replaced.
"""

from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from threading import RLock
from typing import Callable
from weakref import WeakKeyDictionary

from . import betfair_authenticated_stream as _stream

_Runtime = _stream.BetfairAuthenticatedStreamFreshnessRuntime
_Decision = _stream.BetfairAuthenticatedFreshnessDecision
_ORIGINAL_INIT = _Runtime.__init__
_ORIGINAL_READ_AND_INGEST = _Runtime.read_and_ingest
_ORIGINAL_EVALUATE = _Runtime.evaluate
_ORIGINAL_DECISION_ELIGIBLE = _Decision.decision_eligible.fget
if _ORIGINAL_DECISION_ELIGIBLE is None:  # pragma: no cover - structural import guard
    raise RuntimeError("authenticated freshness decision property has no getter")

_CLOCK_LOCK = RLock()
_RUNTIME_CLOCKS: WeakKeyDictionary[object, Callable[[], int]] = WeakKeyDictionary()
_ACTIVE_CLOCK: ContextVar[Callable[[], int] | None] = ContextVar(
    "autosport_betfair_authenticated_stream_wall_clock",
    default=None,
)


def _bound_wall_time_ms() -> int:
    clock = _ACTIVE_CLOCK.get()
    if clock is None:
        raise _stream.BetfairAuthenticatedStreamError(
            "authenticated freshness runtime-bound product wall clock is unavailable"
        )
    value = clock() // 1_000_000
    if type(value) is not int or value <= 0:
        raise _stream.BetfairAuthenticatedStreamError("product wall clock is unavailable")
    return value


def _clock_for(runtime: object) -> Callable[[], int]:
    with _CLOCK_LOCK:
        clock = _RUNTIME_CLOCKS.get(runtime)
    if clock is None:
        raise _stream.BetfairAuthenticatedStreamError(
            "authenticated freshness runtime has no bound product wall clock"
        )
    if _stream._wall_time_ms is not _bound_wall_time_ms:
        raise _stream.BetfairAuthenticatedStreamError(
            "authenticated freshness product wall-clock binding changed"
        )
    return clock


def _call_with_runtime_clock(runtime: object, function, *args, **kwargs):
    token = _ACTIVE_CLOCK.set(_clock_for(runtime))
    try:
        return function(runtime, *args, **kwargs)
    finally:
        _ACTIVE_CLOCK.reset(token)


@wraps(_ORIGINAL_INIT)
def _init_with_bound_clock(self, *args, **kwargs):
    # Capture the exact callable before the owning initializer exposes a usable
    # runtime. Tests may install a deterministic clock before construction; a later
    # transient module rebind cannot alter this runtime's authority.
    product_time_ns = _stream.time.time_ns
    if not callable(product_time_ns):
        raise _stream.BetfairAuthenticatedStreamError(
            "product wall clock callable is unavailable"
        )
    _ORIGINAL_INIT(self, *args, **kwargs)
    with _CLOCK_LOCK:
        _RUNTIME_CLOCKS[self] = product_time_ns


@wraps(_ORIGINAL_READ_AND_INGEST)
def _read_and_ingest_with_bound_clock(self, *args, **kwargs):
    return _call_with_runtime_clock(
        self,
        _ORIGINAL_READ_AND_INGEST,
        *args,
        **kwargs,
    )


@wraps(_ORIGINAL_EVALUATE)
def _evaluate_with_bound_clock(self, *args, **kwargs):
    return _call_with_runtime_clock(
        self,
        _ORIGINAL_EVALUATE,
        *args,
        **kwargs,
    )


def _decision_eligible_with_bound_clock(self) -> bool:
    if (
        self.verdict
        is not _stream.BetfairAuthenticatedFreshnessVerdict.FRESH_AUTHENTICATED_PROVIDER_PUBLISH
    ):
        return False
    with _stream._AUTHORITY_LOCK:
        authority = _stream._ISSUED_DECISIONS.get(self)
    if authority is None:
        return False
    runtime = authority.runtime_ref()
    if runtime is None:
        return False
    try:
        token = _ACTIVE_CLOCK.set(_clock_for(runtime))
    except _stream.BetfairAuthenticatedStreamError:
        return False
    try:
        return bool(_ORIGINAL_DECISION_ELIGIBLE(self))
    except _stream.BetfairAuthenticatedStreamError:
        return False
    finally:
        _ACTIVE_CLOCK.reset(token)


_Runtime.__init__ = _init_with_bound_clock
_Runtime.read_and_ingest = _read_and_ingest_with_bound_clock
_Runtime.evaluate = _evaluate_with_bound_clock
_Decision.decision_eligible = property(
    _decision_eligible_with_bound_clock,
    doc=_Decision.decision_eligible.__doc__,
)
_stream._wall_time_ms = _bound_wall_time_ms
