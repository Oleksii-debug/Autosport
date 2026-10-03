from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.paper as paper_module
from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    SettlementResolution,
)
from autosport.domain import TicketLeg, TicketStatus
from autosport.event_lifecycle import EventPhase
from autosport.paper import PaperBook


class _Lifecycle:
    def __init__(self, record) -> None:
        self.record = record

    def records(self):
        return (self.record,)


def _fixture(tmp_path: Path):
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
    return paper_book_path, resolution


def _coordinator(paper_book_path: Path, resolution, outcome_authority):
    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"
    coordinator.outcome_authority = outcome_authority
    coordinator.lifecycle = _Lifecycle(
        SimpleNamespace(
            phase=EventPhase.COMPLETED,
            settlement_ref=resolution.settlement_ref,
            identity=resolution.event_identity,
        )
    )
    return coordinator


def test_outcome_callback_cannot_replace_ticketstatus_enum_call_during_load(
    tmp_path: Path,
) -> None:
    paper_book_path, resolution = _fixture(tmp_path)
    canonical_status = paper_module.TicketStatus
    status_metaclass = type(canonical_status)
    status_metaclass_dict = type.__getattribute__(status_metaclass, "__dict__")
    canonical_call = status_metaclass_dict["__call__"]
    hostile_calls: list[object] = []

    def hostile_call(cls, value, *args, **kwargs):
        hostile_calls.append(value)
        type.__setattr__(status_metaclass, "__call__", canonical_call)
        if cls is canonical_status and value == TicketStatus.OPEN.value:
            return TicketStatus.LOST
        return canonical_call(cls, value, *args, **kwargs)

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            del record, as_of
            type.__setattr__(status_metaclass, "__call__", hostile_call)
            return resolution

    coordinator = _coordinator(paper_book_path, resolution, OutcomeAuthority())
    try:
        with pytest.raises(
            ContinuousSessionError,
            match="TicketStatus.*conversion.*changed|materialization.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        type.__setattr__(status_metaclass, "__call__", canonical_call)

    assert hostile_calls == []
    assert type.__getattribute__(status_metaclass, "__dict__")["__call__"] is canonical_call


def test_outcome_callback_cannot_retarget_ticketstatus_value_member_map(
    tmp_path: Path,
) -> None:
    paper_book_path, resolution = _fixture(tmp_path)
    canonical_status = paper_module.TicketStatus
    status_dict = type.__getattribute__(canonical_status, "__dict__")
    value_map = status_dict["_value2member_map_"]
    original_items = tuple(value_map.items())

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            del record, as_of
            value_map[TicketStatus.OPEN.value] = TicketStatus.LOST
            return resolution

    coordinator = _coordinator(paper_book_path, resolution, OutcomeAuthority())
    try:
        with pytest.raises(
            ContinuousSessionError,
            match="TicketStatus.*value/member.*changed|materialization.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        value_map.clear()
        value_map.update(original_items)

    assert value_map[TicketStatus.OPEN.value] is TicketStatus.OPEN
    assert value_map[TicketStatus.LOST.value] is TicketStatus.LOST
