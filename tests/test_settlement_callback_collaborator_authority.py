from __future__ import annotations

import builtins as _builtins
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    EventPhase,
    SettlementResolution,
)
from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_AT = "2026-09-22T07:02:00+00:00"
_HOSTILE_OUTCOME_CALLS: list[str] = []
_HOSTILE_PREPARE_CALLS: list[str] = []
_HOSTILE_RECONCILE_CALLS: list[str] = []


class _State:
    session_id = "session-callback-witness"

    def snapshot(self):
        return SimpleNamespace(cycles_completed=0, last_success_at=None)

    def validate_settlement_evidence(self, *, settlement_evidence) -> None:
        tuple(settlement_evidence)

    def record_success(self, **_kwargs) -> None:
        return None

    def record_failure(self, **_kwargs) -> None:
        return None


class _Lifecycle:
    def __init__(self, resolution: SettlementResolution) -> None:
        self._resolution = resolution

    def records(self):
        return (
            SimpleNamespace(
                phase=EventPhase.COMPLETED,
                settlement_ref=self._resolution.settlement_ref,
                identity=self._resolution.event_identity,
            ),
        )

    def register_eligible(self, *_args, **_kwargs):
        return ()


class _Collector:
    source_id = "provider-a"

    def __init__(self, callback=None) -> None:
        self._callback = callback

    def run_cycle(self):
        if self._callback is not None:
            self._callback()
        return SimpleNamespace(
            provider_unavailable=False,
            source_id=self.source_id,
            committed_delta_ids=(),
        )


class _DesktopConsumer:
    def drain(self, **_kwargs):
        return ()


class _OutcomeAuthority:
    def __init__(self, resolution: SettlementResolution, callback=None) -> None:
        self._resolution = resolution
        self._callback = callback

    def resolve(self, _record, *, as_of: str):
        del as_of
        if self._callback is not None:
            self._callback()
        return self._resolution


def _hostile_outcome_resolve(self, _record, *, as_of: str):
    del self, _record
    _HOSTILE_OUTCOME_CALLS.append(as_of)
    raise AssertionError("hostile outcome resolve executed")


class _LearningHandoff:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.reconcile_calls = 0

    def prepare_settlement(self, **_kwargs) -> None:
        self.prepare_calls += 1

    def reconcile_after_settlement(self, **_kwargs) -> None:
        self.reconcile_calls += 1


def _hostile_prepare(self, **_kwargs) -> None:
    del self
    _HOSTILE_PREPARE_CALLS.append("called")
    raise AssertionError("hostile learning prepare executed")


def _hostile_reconcile(self, **_kwargs) -> None:
    del self
    _HOSTILE_RECONCILE_CALLS.append("called")
    raise AssertionError("hostile learning reconcile executed")


class _ReconcileMutatingHandoff(_LearningHandoff):
    def prepare_settlement(self, **_kwargs) -> None:
        self.prepare_calls += 1
        type(self).reconcile_after_settlement.__code__ = _hostile_reconcile.__code__


def _build_resolution_and_book(tmp_path: Path) -> tuple[SettlementResolution, Path]:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    path = tmp_path / "paper_book.json"
    book.save(path)
    return (
        SettlementResolution(
            event_identity="provider-a:event-1",
            settlement_ref="provider-result:1",
            quote_outcomes={leg.quote_key: "win"},
            evidence_id="provider-a-outcome-1",
            evidence_sha256="6" * 64,
            available_at="2026-09-22T07:01:00+00:00",
        ),
        path,
    )


def _coordinator(
    tmp_path: Path,
    *,
    resolution: SettlementResolution,
    paper_book_path: Path,
    outcome_authority,
    learning_handoff,
    collector: _Collector | None = None,
) -> ContinuousSessionCoordinator:
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = collector or _Collector()
    coordinator._refresh_source_state_projection = lambda: SimpleNamespace(
        source_gap_state=None,
        source_sync_state=None,
    )
    coordinator.desktop_consumer = _DesktopConsumer()
    coordinator.causal_view = object()
    coordinator._drain_invalidations = lambda: ((), False, False)
    coordinator.dependency_index = SimpleNamespace(input_ids=set())
    coordinator.lifecycle = _Lifecycle(resolution)
    coordinator.market_store = object()
    coordinator.required_history = timedelta(0)
    coordinator.outcome_authority = outcome_authority
    coordinator.settlement_learning_handoff = learning_handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"
    return coordinator


def test_tick_rejects_collector_outcome_resolve_code_retarget_before_dispatch(
    tmp_path: Path,
) -> None:
    resolution, paper_book_path = _build_resolution_and_book(tmp_path)
    authority = _OutcomeAuthority(resolution)
    handoff = _LearningHandoff()
    canonical_code = type(authority).resolve.__code__
    assert canonical_code.co_freevars == _hostile_outcome_resolve.__code__.co_freevars

    def mutate() -> None:
        type(authority).resolve.__code__ = _hostile_outcome_resolve.__code__

    coordinator = _coordinator(
        tmp_path,
        resolution=resolution,
        paper_book_path=paper_book_path,
        outcome_authority=authority,
        learning_handoff=handoff,
        collector=_Collector(mutate),
    )
    _HOSTILE_OUTCOME_CALLS.clear()
    try:
        with pytest.raises(
            ContinuousSessionError,
            match="settlement outcome authority resolve (executable|dispatch) changed during tick",
        ):
            coordinator.tick()
        assert _HOSTILE_OUTCOME_CALLS == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        type(authority).resolve.__code__ = canonical_code


def test_tick_rejects_outcome_callback_prepare_code_retarget_before_prepare(
    tmp_path: Path,
) -> None:
    resolution, paper_book_path = _build_resolution_and_book(tmp_path)
    handoff = _LearningHandoff()
    canonical_code = type(handoff).prepare_settlement.__code__
    assert canonical_code.co_freevars == _hostile_prepare.__code__.co_freevars

    def mutate() -> None:
        type(handoff).prepare_settlement.__code__ = _hostile_prepare.__code__

    authority = _OutcomeAuthority(resolution, mutate)
    coordinator = _coordinator(
        tmp_path,
        resolution=resolution,
        paper_book_path=paper_book_path,
        outcome_authority=authority,
        learning_handoff=handoff,
    )
    _HOSTILE_PREPARE_CALLS.clear()
    try:
        with pytest.raises(
            ContinuousSessionError,
            match="settlement learning prepare (executable|dispatch) changed during tick",
        ):
            coordinator.tick()
        assert _HOSTILE_PREPARE_CALLS == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        type(handoff).prepare_settlement.__code__ = canonical_code


def test_tick_rejects_prepare_reconcile_code_retarget_before_reconcile(
    tmp_path: Path,
) -> None:
    resolution, paper_book_path = _build_resolution_and_book(tmp_path)
    handoff = _ReconcileMutatingHandoff()
    canonical_code = type(handoff).reconcile_after_settlement.__code__
    assert canonical_code.co_freevars == _hostile_reconcile.__code__.co_freevars
    authority = _OutcomeAuthority(resolution)
    coordinator = _coordinator(
        tmp_path,
        resolution=resolution,
        paper_book_path=paper_book_path,
        outcome_authority=authority,
        learning_handoff=handoff,
    )
    _HOSTILE_RECONCILE_CALLS.clear()
    try:
        with pytest.raises(
            ContinuousSessionError,
            match="settlement learning reconcile (executable|dispatch) changed during tick",
        ):
            coordinator.tick()
        assert handoff.prepare_calls == 1
        assert handoff.reconcile_calls == 0
        assert _HOSTILE_RECONCILE_CALLS == []
    finally:
        type(handoff).reconcile_after_settlement.__code__ = canonical_code


_GLOBAL_LEARNING_HELPER_CALLS: list[str] = []
_GLOBAL_OUTCOME_HELPER_CALLS: list[str] = []


def _canonical_learning_global_helper(handoff) -> None:
    handoff.prepare_calls += 1


def _hostile_learning_global_helper(_handoff) -> None:
    _GLOBAL_LEARNING_HELPER_CALLS.append("called")
    raise AssertionError("hostile learning global helper executed")


_LEARNING_GLOBAL_HELPER = _canonical_learning_global_helper


class _GlobalHelperLearningHandoff(_LearningHandoff):
    def prepare_settlement(self, **_kwargs) -> None:
        _LEARNING_GLOBAL_HELPER(self)


def _canonical_outcome_global_helper(authority, _record, *, as_of: str):
    del as_of
    return authority._resolution


def _hostile_outcome_global_helper(_authority, _record, *, as_of: str):
    del as_of
    _GLOBAL_OUTCOME_HELPER_CALLS.append("called")
    raise AssertionError("hostile outcome global helper executed")


_OUTCOME_GLOBAL_HELPER = _canonical_outcome_global_helper


class _GlobalHelperOutcomeAuthority(_OutcomeAuthority):
    def resolve(self, record, *, as_of: str):
        return _OUTCOME_GLOBAL_HELPER(self, record, as_of=as_of)


def test_tick_rejects_outcome_callback_learning_global_helper_retarget_before_prepare(
    tmp_path: Path,
) -> None:
    global _LEARNING_GLOBAL_HELPER

    resolution, paper_book_path = _build_resolution_and_book(tmp_path)
    handoff = _GlobalHelperLearningHandoff()
    canonical_helper = _LEARNING_GLOBAL_HELPER

    def mutate() -> None:
        global _LEARNING_GLOBAL_HELPER
        _LEARNING_GLOBAL_HELPER = _hostile_learning_global_helper

    authority = _OutcomeAuthority(resolution, mutate)
    coordinator = _coordinator(
        tmp_path,
        resolution=resolution,
        paper_book_path=paper_book_path,
        outcome_authority=authority,
        learning_handoff=handoff,
    )
    _GLOBAL_LEARNING_HELPER_CALLS.clear()
    try:
        with pytest.raises(
            ContinuousSessionError,
            match="settlement learning prepare global binding changed during tick",
        ):
            coordinator.tick()
        assert _GLOBAL_LEARNING_HELPER_CALLS == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        _LEARNING_GLOBAL_HELPER = canonical_helper


def test_tick_rejects_collector_outcome_global_helper_retarget_before_resolve(
    tmp_path: Path,
) -> None:
    global _OUTCOME_GLOBAL_HELPER

    resolution, paper_book_path = _build_resolution_and_book(tmp_path)
    authority = _GlobalHelperOutcomeAuthority(resolution)
    handoff = _LearningHandoff()
    canonical_helper = _OUTCOME_GLOBAL_HELPER

    def mutate() -> None:
        global _OUTCOME_GLOBAL_HELPER
        _OUTCOME_GLOBAL_HELPER = _hostile_outcome_global_helper

    coordinator = _coordinator(
        tmp_path,
        resolution=resolution,
        paper_book_path=paper_book_path,
        outcome_authority=authority,
        learning_handoff=handoff,
        collector=_Collector(mutate),
    )
    _GLOBAL_OUTCOME_HELPER_CALLS.clear()
    try:
        with pytest.raises(
            ContinuousSessionError,
            match="settlement outcome authority resolve global binding changed during tick",
        ):
            coordinator.tick()
        assert _GLOBAL_OUTCOME_HELPER_CALLS == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        _OUTCOME_GLOBAL_HELPER = canonical_helper


_BUILTIN_SHADOW_CALLS: list[str] = []


def _hostile_len(value) -> int:
    del value
    globals().pop("len", None)
    _BUILTIN_SHADOW_CALLS.append("called")
    return 1


class _BuiltinShadowOutcomeAuthority(_OutcomeAuthority):
    def resolve(self, record, *, as_of: str):
        del as_of
        if len((record,)) != 1:
            raise AssertionError("unexpected record cardinality")
        return self._resolution


def test_tick_rejects_collector_transient_builtin_shadow_before_resolve(
    tmp_path: Path,
) -> None:
    resolution, paper_book_path = _build_resolution_and_book(tmp_path)
    authority = _BuiltinShadowOutcomeAuthority(resolution)
    handoff = _LearningHandoff()

    def mutate() -> None:
        globals()["len"] = _hostile_len

    coordinator = _coordinator(
        tmp_path,
        resolution=resolution,
        paper_book_path=paper_book_path,
        outcome_authority=authority,
        learning_handoff=handoff,
        collector=_Collector(mutate),
    )
    _BUILTIN_SHADOW_CALLS.clear()
    try:
        with pytest.raises(
            ContinuousSessionError,
            match="settlement outcome authority resolve global binding changed during tick",
        ):
            coordinator.tick()
        assert _BUILTIN_SHADOW_CALLS == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        globals().pop("len", None)


_BUILTIN_NAMESPACE_CALLS: list[str] = []
_CANONICAL_ABS = _builtins.abs


def _hostile_abs(value):
    _builtins.abs = _CANONICAL_ABS
    _BUILTIN_NAMESPACE_CALLS.append("called")
    return _CANONICAL_ABS(value)


class _BuiltinNamespaceOutcomeAuthority(_OutcomeAuthority):
    def resolve(self, record, *, as_of: str):
        del as_of
        if abs(1) != 1:
            raise AssertionError("unexpected builtin abs semantics")
        return self._resolution


def test_tick_rejects_collector_builtin_namespace_retarget_before_resolve(
    tmp_path: Path,
) -> None:
    resolution, paper_book_path = _build_resolution_and_book(tmp_path)
    authority = _BuiltinNamespaceOutcomeAuthority(resolution)
    handoff = _LearningHandoff()

    def mutate() -> None:
        _builtins.abs = _hostile_abs

    coordinator = _coordinator(
        tmp_path,
        resolution=resolution,
        paper_book_path=paper_book_path,
        outcome_authority=authority,
        learning_handoff=handoff,
        collector=_Collector(mutate),
    )
    _BUILTIN_NAMESPACE_CALLS.clear()
    try:
        with pytest.raises(
            ContinuousSessionError,
            match="settlement outcome authority resolve builtin binding changed during tick",
        ):
            coordinator.tick()
        assert _BUILTIN_NAMESPACE_CALLS == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        _builtins.abs = _CANONICAL_ABS


_DYNAMIC_LOOKUP_HOSTILE_CALLS: list[str] = []


def _hostile_dynamic_lookup_resolve(self, _record, *, as_of: str):
    del self, _record
    _DYNAMIC_LOOKUP_HOSTILE_CALLS.append(as_of)
    raise AssertionError("hostile dynamic outcome resolve executed")


class _DynamicLookupOutcomeAuthority:
    def __init__(self) -> None:
        self._armed = False
        self._armed_resolve_lookups = 0

    def arm(self) -> None:
        self._armed = True

    def resolve(self, _record, *, as_of: str):
        del _record, as_of
        raise ContinuousSessionError("canonical outcome resolve reached")

    def __getattribute__(self, name: str):
        if name == "resolve" and object.__getattribute__(self, "_armed"):
            count = object.__getattribute__(self, "_armed_resolve_lookups") + 1
            object.__setattr__(self, "_armed_resolve_lookups", count)
            # Canonical tick currently validates two post-collector lookups before
            # _settlement_resolutions performs a fresh third lookup for dispatch.
            if count == 3:
                return _hostile_dynamic_lookup_resolve.__get__(self, type(self))
        return object.__getattribute__(self, name)


def test_tick_executes_captured_outcome_resolve_not_later_dynamic_lookup(
    tmp_path: Path,
) -> None:
    resolution, paper_book_path = _build_resolution_and_book(tmp_path)
    authority = _DynamicLookupOutcomeAuthority()
    handoff = _LearningHandoff()
    coordinator = _coordinator(
        tmp_path,
        resolution=resolution,
        paper_book_path=paper_book_path,
        outcome_authority=authority,
        learning_handoff=handoff,
        collector=_Collector(authority.arm),
    )
    _DYNAMIC_LOOKUP_HOSTILE_CALLS.clear()

    with pytest.raises(
        ContinuousSessionError,
        match="canonical outcome resolve reached",
    ):
        coordinator.tick()

    assert _DYNAMIC_LOOKUP_HOSTILE_CALLS == []


class _MutatingRecordsLifecycle(_Lifecycle):
    def __init__(self, resolution: SettlementResolution, mutate) -> None:
        super().__init__(resolution)
        self._mutate = mutate

    def records(self):
        self._mutate()
        return super().records()


def test_tick_rejects_lifecycle_records_outcome_code_retarget_before_dispatch(
    tmp_path: Path,
) -> None:
    resolution, paper_book_path = _build_resolution_and_book(tmp_path)
    authority = _OutcomeAuthority(resolution)
    handoff = _LearningHandoff()
    canonical_code = type(authority).resolve.__code__
    assert canonical_code.co_freevars == _hostile_outcome_resolve.__code__.co_freevars

    def mutate() -> None:
        type(authority).resolve.__code__ = _hostile_outcome_resolve.__code__

    coordinator = _coordinator(
        tmp_path,
        resolution=resolution,
        paper_book_path=paper_book_path,
        outcome_authority=authority,
        learning_handoff=handoff,
    )
    coordinator.lifecycle = _MutatingRecordsLifecycle(resolution, mutate)
    _HOSTILE_OUTCOME_CALLS.clear()
    try:
        with pytest.raises(
            ContinuousSessionError,
            match="settlement outcome authority resolve executable changed during tick",
        ):
            coordinator.tick()
        assert _HOSTILE_OUTCOME_CALLS == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        type(authority).resolve.__code__ = canonical_code

_OPAQUE_RESOLVE_CALLS: list[str] = []


class _OpaqueResolve:
    def __init__(self, resolution: SettlementResolution) -> None:
        self._resolution = resolution

    def __call__(self, _record, *, as_of: str):
        del _record, as_of
        return self._resolution


class _OpaqueOutcomeAuthority:
    def __init__(self, resolution: SettlementResolution) -> None:
        self.resolve = _OpaqueResolve(resolution)


def _hostile_opaque_resolve(self, _record, *, as_of: str):
    del self, _record
    _OPAQUE_RESOLVE_CALLS.append(as_of)
    raise AssertionError("hostile opaque outcome resolver executed")


def test_tick_rejects_opaque_callback_call_slot_retarget_before_invocation(
    tmp_path: Path,
) -> None:
    resolution, paper_book_path = _build_resolution_and_book(tmp_path)
    authority = _OpaqueOutcomeAuthority(resolution)
    handoff = _LearningHandoff()
    canonical_call = _OpaqueResolve.__dict__["__call__"]

    def mutate() -> None:
        type.__setattr__(_OpaqueResolve, "__call__", _hostile_opaque_resolve)

    coordinator = _coordinator(
        tmp_path,
        resolution=resolution,
        paper_book_path=paper_book_path,
        outcome_authority=authority,
        learning_handoff=handoff,
        collector=_Collector(mutate),
    )
    _OPAQUE_RESOLVE_CALLS.clear()
    try:
        with pytest.raises(
            ContinuousSessionError,
            match="settlement outcome authority resolve (invocation slot|dispatch|executable) changed during tick",
        ):
            coordinator.tick()
        assert _OPAQUE_RESOLVE_CALLS == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        type.__setattr__(_OpaqueResolve, "__call__", canonical_call)

