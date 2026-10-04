from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.continuous_session as session
from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    EventPhase,
    SettlementResolution,
)


_AT = "2026-09-27T03:00:00+00:00"
_FUTURE = "2026-09-27T04:00:00+00:00"


class _State:
    session_id = "session-1"

    def __init__(self) -> None:
        self.failures: list[str] = []

    def snapshot(self):
        return SimpleNamespace(cycles_completed=0, last_success_at=None)

    def validate_settlement_evidence(self, *, settlement_evidence) -> None:
        tuple(settlement_evidence)

    def record_success(self, **_kwargs) -> None:
        return None

    def record_failure(self, *, code: str) -> None:
        self.failures.append(code)


class _Lifecycle:
    def records(self):
        return (
            SimpleNamespace(
                phase=EventPhase.COMPLETED,
                settlement_ref="settlement-1",
                identity="event-1",
            ),
        )

    def register_eligible(self, *_args, **_kwargs):
        return ()


class _OutcomeAuthority:
    def __init__(self, resolution: SettlementResolution) -> None:
        self.resolution = resolution

    def resolve(self, _record, *, as_of: str):
        del as_of
        return self.resolution


class _MutatingOutcomeAuthority(_OutcomeAuthority):
    def __init__(self, resolution: SettlementResolution, mutate) -> None:
        super().__init__(resolution)
        self.mutate = mutate

    def resolve(self, _record, *, as_of: str):
        del as_of
        self.mutate()
        return self.resolution


class _Collector:
    source_id = "provider-a"

    def run_cycle(self):
        return SimpleNamespace(
            provider_unavailable=False,
            source_id=self.source_id,
            committed_delta_ids=(),
        )


class _DesktopConsumer:
    def drain(self, **_kwargs):
        return ()


class _LearningHandoff:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.reconcile_calls = 0

    def prepare_settlement(self, **_kwargs) -> None:
        self.prepare_calls += 1

    def reconcile_after_settlement(self, **_kwargs) -> None:
        self.reconcile_calls += 1


def _resolution(*, available_at: str = _AT) -> SettlementResolution:
    return SettlementResolution(
        event_identity="event-1",
        settlement_ref="settlement-1",
        quote_outcomes={"quote-1": "win"},
        evidence_id="evidence-1",
        evidence_sha256="a" * 64,
        available_at=available_at,
    )


def _coordinator(
    resolution: SettlementResolution,
    *,
    handoff: _LearningHandoff | None = None,
) -> ContinuousSessionCoordinator:
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = _Collector()
    coordinator._refresh_source_state_projection = lambda: SimpleNamespace(
        source_gap_state=None,
        source_sync_state=None,
    )
    coordinator.desktop_consumer = _DesktopConsumer()
    coordinator.causal_view = object()
    coordinator._drain_invalidations = lambda: ((), False, False)
    coordinator.dependency_index = SimpleNamespace(input_ids=set())
    coordinator.lifecycle = _Lifecycle()
    coordinator.market_store = object()
    coordinator.required_history = timedelta(0)
    coordinator.outcome_authority = _OutcomeAuthority(resolution)
    coordinator.settlement_learning_handoff = handoff
    coordinator.workspace = Path(".")
    coordinator.paper_book_path = Path("unused-paper-book.json")
    coordinator.initial_bankroll = "100"
    return coordinator


def _closure_cell(function, name: str):
    freevars = function.__code__.co_freevars
    closure = function.__closure__ or ()
    if name not in freevars:
        raise AssertionError(f"{function.__qualname__} has no {name!r} closure cell")
    return closure[freevars.index(name)]


def test_tick_fails_closed_before_direct_base_type_settle_replacement_dispatch() -> None:
    hostile_calls = 0

    def hostile_settle(self, *, resolutions):
        nonlocal hostile_calls
        del self, resolutions
        hostile_calls += 1
        return (), ()

    # The composed Wave M class guard now rejects this mutation before a hostile slot
    # can be installed. This is strictly earlier than the tick-entry preflight that this
    # regression originally exercised and preserves the same zero-dispatch invariant.
    with pytest.raises(
        TypeError,
        match="canonical settlement consumer entry binding is immutable|canonical settlement coordinator dispatch is immutable",
    ):
        type.__setattr__(ContinuousSessionCoordinator, "_settle", hostile_settle)
    assert hostile_calls == 0


def test_tick_fails_closed_before_rebound_resolution_validator_reaches_learning_handoff() -> None:
    resolution = _resolution(available_at=_FUTURE)
    resolution_type = SettlementResolution
    resolution_dict = type.__getattribute__(resolution_type, "__dict__")
    original_validate = resolution_dict["validate"]
    validated_outcomes_type = session._ValidatedQuoteOutcomes
    handoff = _LearningHandoff()

    def hostile_validate(self, *, as_of: str) -> None:
        del as_of
        # Preserve the post-validation container shape/digest while deliberately
        # skipping chronology and the rest of the canonical validation contract.
        if type(self.quote_outcomes) is not validated_outcomes_type:
            object.__setattr__(
                self,
                "quote_outcomes",
                validated_outcomes_type(dict(self.quote_outcomes)),
            )

    coordinator = _coordinator(resolution, handoff=handoff)
    try:
        # `_settle` currently witnesses this slot, but learning prepare occurs before
        # `_settle`; product-entry preflight must reject the drift before that side effect.
        type.__setattr__(resolution_type, "validate", hostile_validate)
        with pytest.raises(ContinuousSessionError):
            coordinator.tick()
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        type.__setattr__(resolution_type, "validate", original_validate)


def test_tick_rechecks_resolution_validator_after_authority_callback_before_learning_handoff() -> None:
    resolution = _resolution(available_at=_FUTURE)
    resolution_type = SettlementResolution
    resolution_dict = type.__getattribute__(resolution_type, "__dict__")
    original_validate = resolution_dict["validate"]
    validated_outcomes_type = session._ValidatedQuoteOutcomes
    handoff = _LearningHandoff()

    def hostile_validate(self, *, as_of: str) -> None:
        del as_of
        if type(self.quote_outcomes) is not validated_outcomes_type:
            object.__setattr__(
                self,
                "quote_outcomes",
                validated_outcomes_type(dict(self.quote_outcomes)),
            )

    def mutate_validator() -> None:
        type.__setattr__(resolution_type, "validate", hostile_validate)

    coordinator = _coordinator(resolution, handoff=handoff)
    coordinator.outcome_authority = _MutatingOutcomeAuthority(
        resolution,
        mutate_validator,
    )
    try:
        # Initial tick preflight sees the canonical validator. The authority callback
        # then mutates the class slot before the resolution and learning handoff are
        # consumed. The product must recheck authority before any durable handoff side
        # effect rather than relying only on tick entry/exit checks.
        with pytest.raises(ContinuousSessionError):
            coordinator.tick()
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        type.__setattr__(resolution_type, "validate", original_validate)


def test_tick_rejects_retargeted_post_callback_checker_before_learning_handoff() -> None:
    """A reachable closure cell must not be able to erase the callback authority fence."""

    resolution = _resolution(available_at=_FUTURE)
    resolution_type = SettlementResolution
    resolution_dict = type.__getattribute__(resolution_type, "__dict__")
    original_validate = resolution_dict["validate"]
    validated_outcomes_type = session._ValidatedQuoteOutcomes
    handoff = _LearningHandoff()

    coordinator_type = ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator_type, "__dict__")
    descriptor = coordinator_dict["_settlement_resolutions"]
    descriptor_get = type(descriptor).__dict__["__get__"]
    after_call = _closure_cell(descriptor_get, "after_call").cell_contents
    checker_cell = _closure_cell(after_call, "product_authority_recheck")
    original_checker = checker_cell.cell_contents
    hostile_checker_calls = 0

    def hostile_validate(self, *, as_of: str) -> None:
        del as_of
        if type(self.quote_outcomes) is not validated_outcomes_type:
            object.__setattr__(
                self,
                "quote_outcomes",
                validated_outcomes_type(dict(self.quote_outcomes)),
            )

    def hostile_checker() -> None:
        nonlocal hostile_checker_calls
        hostile_checker_calls += 1

    def mutate_validator() -> None:
        type.__setattr__(resolution_type, "validate", hostile_validate)

    coordinator = _coordinator(resolution, handoff=handoff)
    coordinator.outcome_authority = _MutatingOutcomeAuthority(
        resolution,
        mutate_validator,
    )
    try:
        # Current preflight must witness this long-lived closure binding itself. On the
        # vulnerable head, replacing only this cell leaves every descriptor/function
        # identity intact, neutralizes the post-callback check, and lets learning prepare
        # run before the later economic-entry witness rejects the validator drift.
        checker_cell.cell_contents = hostile_checker
        with pytest.raises(
            ContinuousSessionError,
            match="post-callback authority changed",
        ):
            coordinator.tick()
        assert hostile_checker_calls == 0
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        checker_cell.cell_contents = original_checker
        type.__setattr__(resolution_type, "validate", original_validate)