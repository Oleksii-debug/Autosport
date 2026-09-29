from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.paper as paper_module
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
    session_id = "session-paperbook-materialization-closure"

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


def _hidden_load_book_target():
    coordinator_type = ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator_type, "__dict__")
    descriptor = coordinator_dict["_load_book"]
    descriptor_get = type(descriptor).__dict__["__get__"]
    return descriptor_get(descriptor, None, coordinator_type)


def _direct_closure_cell(function, name: str):
    closure = function.__closure__ or ()
    freevars = function.__code__.co_freevars
    if name not in freevars:
        raise AssertionError(f"{function.__qualname__} has no direct {name!r} closure cell")
    return closure[freevars.index(name)]


def test_early_callback_cannot_pair_rebind_materialization_global_and_witness(
    tmp_path: Path,
) -> None:
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

    canonical_ticket_type = paper_module.PaperTicket
    hidden_load_book = _hidden_load_book_target()
    # Attack the outer materialization guard specifically. The source-scope loader one
    # level below has an independent witness with the same name; a recursive search
    # could hit that older guard and make this regression vacuous for the TOCTOU fixed
    # by the materialization preflight wrapper.
    preflight_cell = _direct_closure_cell(hidden_load_book, "materialization_preflight")
    materialization_preflight = preflight_cell.cell_contents
    witness_cell = _direct_closure_cell(
        materialization_preflight,
        "frozen_materialization_global_witnesses",
    )
    canonical_witnesses = witness_cell.cell_contents
    assert dict(canonical_witnesses)["PaperTicket"] is canonical_ticket_type

    hostile_calls: list[tuple[str, ...]] = []
    outcome_calls = 0

    def hostile_paper_ticket(*args, **kwargs):
        hostile_calls.append(tuple(kwargs.get("provider_source_ids", ())))
        # Self-restore so a late post-load witness would see pristine authority even
        # though provenance had already been erased during positive materialization.
        paper_module.PaperTicket = canonical_ticket_type
        witness_cell.cell_contents = canonical_witnesses
        kwargs["provider_source_ids"] = ()
        kwargs["provider_accounts"] = ()
        return canonical_ticket_type(*args, **kwargs)

    forged_witnesses = tuple(
        (name, hostile_paper_ticket if name == "PaperTicket" else expected)
        for name, expected in canonical_witnesses
    )

    class EarlyMutatingCollector:
        source_id = "provider-a"

        def run_cycle(self):
            # Tick-entry authority has already passed. Pair-rebind the parser global and
            # the OUTER materialization guard's expected value before settlement method
            # acquisition. The new direct preflight-cell witness must fail before the
            # callback-capable outcome authority runs.
            paper_module.PaperTicket = hostile_paper_ticket
            witness_cell.cell_contents = forged_witnesses
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
            match="pre-learning settlement authority changed|settlement coordinator dispatch changed|materialization.*changed",
        ):
            coordinator.tick()
        assert outcome_calls == 0
        assert hostile_calls == []
        assert handoff.prepare_calls == 0
        assert handoff.reconcile_calls == 0
    finally:
        paper_module.PaperTicket = canonical_ticket_type
        witness_cell.cell_contents = canonical_witnesses
