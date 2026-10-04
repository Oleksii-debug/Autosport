from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    SettlementResolution,
)
from autosport.domain import TicketLeg
from autosport.event_lifecycle import EventPhase
from autosport.paper import PaperBook


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


class _Lifecycle:
    def __init__(self, record) -> None:
        self.record = record

    def records(self):
        return (self.record,)


def test_outcome_callback_cannot_retarget_source_scope_helper_before_prelearning(
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

    coordinator_type = ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator_type, "__dict__")
    descriptor = coordinator_dict["_settlement_resolutions"]
    descriptor_get = type(descriptor).__dict__["__get__"]
    target = descriptor_get(descriptor, None, coordinator_type)
    scope_cell = _reachable_closure_cell(target, "scoped_open_quote_keys")
    canonical_scope = scope_cell.cell_contents
    hostile_calls: list[str] = []

    def hostile_scope(_book, event_identity, *, candidate_quote_keys=None):
        hostile_calls.append(event_identity)
        return set(candidate_quote_keys or ())

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            scope_cell.cell_contents = hostile_scope
            return resolution

    coordinator = object.__new__(coordinator_type)
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"
    coordinator.outcome_authority = OutcomeAuthority()
    coordinator.lifecycle = _Lifecycle(
        SimpleNamespace(
            phase=EventPhase.COMPLETED,
            settlement_ref=resolution.settlement_ref,
            identity=resolution.event_identity,
        )
    )

    try:
        with pytest.raises(
            ContinuousSessionError,
            match="source-scope helper authority changed|settlement coordinator dispatch changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
        assert hostile_calls == []
    finally:
        scope_cell.cell_contents = canonical_scope
