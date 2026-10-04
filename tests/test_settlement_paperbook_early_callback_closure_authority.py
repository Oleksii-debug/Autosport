from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
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
from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_AT = "2026-09-22T07:02:00+00:00"


class _State:
    session_id = "session-paperbook-early-closure"

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


def _reachable_closure_cell(function, name: str):
    function_type = type(function)
    pending = [function]
    seen: set[int] = set()
    while pending:
        target = pending.pop()
        identity = id(target)
        if identity in seen:
            continue
        seen.add(identity)
        closure = target.__closure__ or ()
        for freevar, cell in zip(target.__code__.co_freevars, closure):
            if freevar == name:
                return cell
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if type(value) is function_type:
                pending.append(value)
    raise AssertionError(f"reachable closure graph has no {name!r} cell")


def _hidden_load_book_target():
    coordinator_type = ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator_type, "__dict__")
    descriptor = coordinator_dict["_load_book"]
    descriptor_get = type(descriptor).__dict__["__get__"]
    return descriptor_get(descriptor, None, coordinator_type)


def test_early_collector_callback_cannot_preseed_hostile_paperbook_closure(
    tmp_path: Path,
) -> None:
    canonical_paper_book = session.PaperBook
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="provider-result:1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-1",
        evidence_sha256="0" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    hidden_load_book = _hidden_load_book_target()
    canonical_class_cell = _reachable_closure_cell(
        hidden_load_book,
        "canonical_paper_book",
    )
    assert canonical_class_cell.cell_contents is canonical_paper_book
    hostile_load_calls: list[Path] = []
    outcome_calls = 0

    class HostilePaperBook:
        @classmethod
        def load(cls, path):
            hostile_load_calls.append(Path(path))
            # Self-restore so post-load/post-tick witnesses alone cannot detect that a
            # hostile positive book acquisition already executed.
            session.PaperBook = canonical_paper_book
            canonical_class_cell.cell_contents = canonical_paper_book
            return canonical_paper_book("100")

    class EarlyMutatingCollector:
        source_id = "provider-a"

        def run_cycle(self):
            # Product-entry preflight has already passed. The repair must compare the
            # later settlement crossing with install-time authority, not snapshot this
            # already-hostile state as the invocation baseline.
            canonical_class_cell.cell_contents = HostilePaperBook
            session.PaperBook = HostilePaperBook
            return SimpleNamespace(
                provider_unavailable=False,
                source_id=self.source_id,
                committed_delta_ids=(),
            )

    class OutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            nonlocal outcome_calls
            del as_of
            outcome_calls += 1
            return resolution

    handoff = _LearningHandoff()
    coordinator = ContinuousSessionCoordinator.__new__(ContinuousSessionCoordinator)
    coordinator._state = _State()
    coordinator._require_running = lambda: None
    coordinator.clock = lambda: _AT
    coordinator.collector = EarlyMutatingCollector()
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
    coordinator.outcome_authority = OutcomeAuthority()
    coordinator.settlement_learning_handoff = handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="pre-learning settlement authority changed|settlement coordinator dispatch changed",
        ):
            coordinator.tick()
        assert outcome_calls == 0
        assert hostile_load_calls == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        session.PaperBook = canonical_paper_book
        canonical_class_cell.cell_contents = canonical_paper_book
