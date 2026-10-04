from __future__ import annotations

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


class _State:
    session_id = "session-paperbook-frozen-private-globals"

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


def _paperbook_wrapper_authority_globals(*, operation: str) -> dict[str, object]:
    descriptor = vars(PaperBook)[operation]
    if operation == "load":
        descriptor = descriptor.__func__
    closure = descriptor.__closure__
    if closure is None:
        raise AssertionError(f"guarded PaperBook.{operation} closure is unavailable")
    frozen_name = f"_FROZEN_{operation.upper()}"
    for freevar, cell in zip(descriptor.__code__.co_freevars, closure):
        if freevar != "trusted_globals":
            continue
        mapping = cell.cell_contents
        if type(mapping) is dict and frozen_name in mapping:
            return mapping
    raise AssertionError(
        f"guarded PaperBook.{operation} trusted globals are unavailable"
    )


def _coordinator(
    *,
    tmp_path: Path,
    paper_book_path: Path,
    resolution: SettlementResolution,
    outcome_authority,
    handoff: _LearningHandoff,
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
    coordinator.lifecycle = _Lifecycle(resolution)
    coordinator.market_store = object()
    coordinator.required_history = timedelta(0)
    coordinator.outcome_authority = outcome_authority
    coordinator.settlement_learning_handoff = handoff
    coordinator.workspace = tmp_path
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"
    return coordinator


def _book_and_resolution(tmp_path: Path, *, suffix: str):
    book = PaperBook("100")
    leg = TicketLeg(f"event-{suffix}", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / f"paper_book_{suffix}.json"
    book.save(paper_book_path)
    resolution = SettlementResolution(
        event_identity=f"provider-a:event-{suffix}",
        settlement_ref=f"provider-result:{suffix}",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id=f"provider-a-outcome-{suffix}",
        evidence_sha256="b" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )
    return paper_book_path, resolution


def test_tick_rejects_frozen_save_private_global_retarget_before_learning(
    tmp_path: Path,
) -> None:
    paper_book_path, resolution = _book_and_resolution(
        tmp_path,
        suffix="frozen-save-private-global",
    )
    durable_before = paper_book_path.read_bytes()

    wrapper_globals = _paperbook_wrapper_authority_globals(operation="save")
    frozen_save = wrapper_globals["_FROZEN_SAVE"]
    private_globals = frozen_save.__globals__
    original_save_name = "_GENERATION_ORIGINAL_TRUSTED_SAVE"
    advance_name = "_advance_book_binding"
    canonical_original_save = private_globals[original_save_name]
    canonical_advance = private_globals[advance_name]
    hostile_save_calls: list[Path] = []
    hostile_advance_calls: list[Path] = []

    def hostile_original_save(_book, path) -> None:
        hostile_save_calls.append(Path(path))

    def hostile_advance(_book, path) -> None:
        hostile_advance_calls.append(Path(path))

    class OutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            del as_of
            private_globals[original_save_name] = hostile_original_save
            private_globals[advance_name] = hostile_advance
            return resolution

    handoff = _LearningHandoff()
    coordinator = _coordinator(
        tmp_path=tmp_path,
        paper_book_path=paper_book_path,
        resolution=resolution,
        outcome_authority=OutcomeAuthority(),
        handoff=handoff,
    )

    try:
        with pytest.raises(
            ContinuousSessionError,
            match=(
                "PaperBook|paper book|frozen save|pre-learning settlement authority changed"
                "|settlement coordinator dispatch changed"
            ),
        ):
            coordinator.tick()
        assert hostile_save_calls == []
        assert hostile_advance_calls == []
        assert paper_book_path.read_bytes() == durable_before
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        private_globals[original_save_name] = canonical_original_save
        private_globals[advance_name] = canonical_advance


def test_tick_rejects_frozen_load_private_global_retarget_before_dispatch(
    tmp_path: Path,
) -> None:
    paper_book_path, resolution = _book_and_resolution(
        tmp_path,
        suffix="frozen-load-private-global",
    )

    wrapper_globals = _paperbook_wrapper_authority_globals(operation="load")
    frozen_load = wrapper_globals["_FROZEN_LOAD"]
    private_globals = frozen_load.__globals__
    original_load_name = "_GENERATION_ORIGINAL_TRUSTED_LOAD"
    require_name = "_require_bound_book"
    canonical_original_load = private_globals[original_load_name]
    canonical_require = private_globals[require_name]
    hostile_load_calls: list[Path] = []
    hostile_require_calls: list[Path] = []

    def hostile_original_load(_cls, path):
        hostile_load_calls.append(Path(path))
        return PaperBook("100")

    def hostile_require(_book, path) -> None:
        hostile_require_calls.append(Path(path))

    class OutcomeAuthority:
        def resolve(self, _record, *, as_of: str):
            del as_of
            private_globals[original_load_name] = hostile_original_load
            private_globals[require_name] = hostile_require
            return resolution

    handoff = _LearningHandoff()
    coordinator = _coordinator(
        tmp_path=tmp_path,
        paper_book_path=paper_book_path,
        resolution=resolution,
        outcome_authority=OutcomeAuthority(),
        handoff=handoff,
    )

    try:
        with pytest.raises(
            ContinuousSessionError,
            match=(
                "PaperBook|paper book|frozen load|pre-learning settlement authority changed"
                "|settlement coordinator dispatch changed"
            ),
        ):
            coordinator.tick()
        assert hostile_load_calls == []
        assert hostile_require_calls == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        private_globals[original_load_name] = canonical_original_load
        private_globals[require_name] = canonical_require
