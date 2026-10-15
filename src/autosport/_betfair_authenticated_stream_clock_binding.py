"""Hard-seal authenticated Stream freshness timing to the product clock.

The owning composition intentionally accepts no caller-supplied receive, ingest, or
as-of timestamp. Positive freshness therefore cannot trust a wall-clock callable that an
ordinary caller can replace before runtime construction. This guard captures the exact
product ``time.time_ns`` callable once when package composition is installed, keeps that
callable closure-owned, and fails closed if public clock or guarded dispatch changes
before or after runtime admission.
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


def _install_clock_binding() -> None:
    canonical_time_ns = _stream.time.time_ns
    original_init = _Runtime.__init__
    original_read_and_ingest = _Runtime.read_and_ingest
    original_evaluate = _Runtime.evaluate
    original_decision_eligible = _Decision.decision_eligible.fget
    if original_decision_eligible is None:  # pragma: no cover - structural import guard
        raise RuntimeError("authenticated freshness decision property has no getter")
    if not callable(canonical_time_ns):  # pragma: no cover - stdlib structural guard
        raise RuntimeError("canonical product wall clock callable is unavailable")

    clock_lock = RLock()
    runtime_clocks: WeakKeyDictionary[object, Callable[[], int]] = WeakKeyDictionary()
    active_clock: ContextVar[Callable[[], int] | None] = ContextVar(
        "autosport_betfair_authenticated_stream_wall_clock",
        default=None,
    )
    installed_dispatch: tuple[object, object, object, object] = ()

    def require_public_clock_dispatch() -> None:
        if _stream.time.time_ns is not canonical_time_ns:
            raise _stream.BetfairAuthenticatedStreamError(
                "authenticated freshness product wall-clock dispatch changed"
            )

    def require_guard_dispatch() -> None:
        if len(installed_dispatch) != 4:  # pragma: no cover - import ordering guard
            raise _stream.BetfairAuthenticatedStreamError(
                "authenticated freshness clock guard is not installed"
            )
        init_guard, read_guard, evaluate_guard, decision_guard = installed_dispatch
        current_decision_getter = _Decision.decision_eligible.fget
        if (
            _Runtime.__init__ is not init_guard
            or _Runtime.read_and_ingest is not read_guard
            or _Runtime.evaluate is not evaluate_guard
            or current_decision_getter is not decision_guard
        ):
            raise _stream.BetfairAuthenticatedStreamError(
                "authenticated freshness guarded dispatch changed"
            )

    def bound_wall_time_ms() -> int:
        clock = active_clock.get()
        if clock is None or clock is not canonical_time_ns:
            raise _stream.BetfairAuthenticatedStreamError(
                "authenticated freshness runtime-bound product wall clock is unavailable"
            )
        require_public_clock_dispatch()
        require_guard_dispatch()
        value = canonical_time_ns() // 1_000_000
        if type(value) is not int or value <= 0:
            raise _stream.BetfairAuthenticatedStreamError(
                "product wall clock is unavailable"
            )
        return value

    def clock_for(runtime: object) -> Callable[[], int]:
        require_public_clock_dispatch()
        require_guard_dispatch()
        if _stream._wall_time_ms is not bound_wall_time_ms:
            raise _stream.BetfairAuthenticatedStreamError(
                "authenticated freshness product wall-clock binding changed"
            )
        with clock_lock:
            clock = runtime_clocks.get(runtime)
        if clock is None or clock is not canonical_time_ns:
            raise _stream.BetfairAuthenticatedStreamError(
                "authenticated freshness runtime has no canonical product wall clock"
            )
        return canonical_time_ns

    def call_with_runtime_clock(runtime: object, function, *args, **kwargs):
        token = active_clock.set(clock_for(runtime))
        try:
            return function(runtime, *args, **kwargs)
        finally:
            active_clock.reset(token)

    @wraps(original_init)
    def init_with_bound_clock(self, *args, **kwargs):
        # Admission is fail-closed both before and after the owning initializer. A
        # caller cannot replace time.time_ns before construction and have that forged
        # callable become the runtime's product clock.
        require_public_clock_dispatch()
        require_guard_dispatch()
        original_init(self, *args, **kwargs)
        require_public_clock_dispatch()
        require_guard_dispatch()
        with clock_lock:
            runtime_clocks[self] = canonical_time_ns

    @wraps(original_read_and_ingest)
    def read_and_ingest_with_bound_clock(self, *args, **kwargs):
        return call_with_runtime_clock(
            self,
            original_read_and_ingest,
            *args,
            **kwargs,
        )

    @wraps(original_evaluate)
    def evaluate_with_bound_clock(self, *args, **kwargs):
        return call_with_runtime_clock(
            self,
            original_evaluate,
            *args,
            **kwargs,
        )

    def decision_eligible_with_bound_clock(self) -> bool:
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
            token = active_clock.set(clock_for(runtime))
        except _stream.BetfairAuthenticatedStreamError:
            return False
        try:
            return bool(original_decision_eligible(self))
        except _stream.BetfairAuthenticatedStreamError:
            return False
        finally:
            active_clock.reset(token)

    installed_dispatch = (
        init_with_bound_clock,
        read_and_ingest_with_bound_clock,
        evaluate_with_bound_clock,
        decision_eligible_with_bound_clock,
    )
    _Runtime.__init__ = init_with_bound_clock
    _Runtime.read_and_ingest = read_and_ingest_with_bound_clock
    _Runtime.evaluate = evaluate_with_bound_clock
    _Decision.decision_eligible = property(
        decision_eligible_with_bound_clock,
        doc=_Decision.decision_eligible.__doc__,
    )
    _stream._wall_time_ms = bound_wall_time_ms


_install_clock_binding()
del _install_clock_binding
