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
    session_id = "session-early-callback"

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


class _MutatingCollector:
    source_id = "provider-a"

    def __init__(self, mutate) -> None:
        self._mutate = mutate

    def run_cycle(self):
        self._mutate()
        return SimpleNamespace(
            provider_unavailable=False,
            source_id=self.source_id,
            committed_delta_ids=(),
        )


class _MutatingOutcomeAuthority:
    def __init__(self, resolution: SettlementResolution, mutate) -> None:
        self._resolution = resolution
        self._mutate = mutate

    def resolve(self, _record, *, as_of: str):
        del as_of
        self._mutate()
        return self._resolution


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


def _closure_cell(function, name: str):
    freevars = function.__code__.co_freevars
    closure = function.__closure__ or ()
    if name not in freevars:
        raise AssertionError(f"{function.__qualname__} has no {name!r} closure cell")
    return closure[freevars.index(name)]


def _resolution() -> SettlementResolution:
    return SettlementResolution(
        event_identity="event-1",
        settlement_ref="settlement-1",
        quote_outcomes={"quote-1": "win"},
        evidence_id="evidence-1",
        evidence_sha256="a" * 64,
        available_at=_FUTURE,
    )


def _coordinator(
    resolution: SettlementResolution,
    *,
    collector,
    handoff: _LearningHandoff,
    mutate_validator,
) -> ContinuousSessionCoordinator:
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = collector
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
    coordinator.outcome_authority = _MutatingOutcomeAuthority(
        resolution,
        mutate_validator,
    )
    coordinator.settlement_learning_handoff = handoff
    coordinator.workspace = Path(".")
    coordinator.paper_book_path = Path("unused-paper-book.json")
    coordinator.initial_bankroll = "100"
    return coordinator


def test_early_callback_cannot_retarget_recheck_before_outcome_validator_drift() -> None:
    resolution = _resolution()
    resolution_type = SettlementResolution
    resolution_dict = type.__getattribute__(resolution_type, "__dict__")
    original_validate = resolution_dict["validate"]
    validated_outcomes_type = session._ValidatedQuoteOutcomes

    coordinator_type = ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator_type, "__dict__")
    resolution_descriptor = coordinator_dict["_settlement_resolutions"]
    descriptor_get = type(resolution_descriptor).__dict__["__get__"]
    after_call = _closure_cell(descriptor_get, "after_call").cell_contents
    checker_cell = _closure_cell(after_call, "product_authority_recheck")
    original_checker = checker_cell.cell_contents

    hostile_checker_calls = 0
    handoff = _LearningHandoff()

    def hostile_checker() -> None:
        nonlocal hostile_checker_calls
        hostile_checker_calls += 1

    def mutate_checker() -> None:
        # This callback runs only after guarded_tick's entry preflight has passed.
        checker_cell.cell_contents = hostile_checker

    def hostile_validate(self, *, as_of: str) -> None:
        del as_of
        # Preserve the container shape expected by the canonical snapshot while
        # deliberately bypassing the future-availability chronology check.
        if type(self.quote_outcomes) is not validated_outcomes_type:
            object.__setattr__(
                self,
                "quote_outcomes",
                validated_outcomes_type(dict(self.quote_outcomes)),
            )

    def mutate_validator() -> None:
        type.__setattr__(resolution_type, "validate", hostile_validate)

    coordinator = _coordinator(
        resolution,
        collector=_MutatingCollector(mutate_checker),
        handoff=handoff,
        mutate_validator=mutate_validator,
    )

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="canonical settlement resolution validation dispatch changed",
        ):
            coordinator.tick()

        # The independent sealed economic-entry witness must reject the validator
        # drift before the forged advisory checker or either learning side effect runs.
        assert hostile_checker_calls == 0
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        checker_cell.cell_contents = original_checker
        type.__setattr__(resolution_type, "validate", original_validate)
