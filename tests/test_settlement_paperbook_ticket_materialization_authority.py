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
from autosport.domain import PaperTicket, TicketLeg
from autosport.event_lifecycle import EventPhase
from autosport.paper import PaperBook


class _Lifecycle:
    def __init__(self, record) -> None:
        self.record = record

    def records(self):
        return (self.record,)


def test_outcome_callback_cannot_erase_ticket_source_provenance_during_load(
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
    hostile_calls: list[tuple[str, ...]] = []

    def hostile_paper_ticket(*args, **kwargs):
        hostile_calls.append(tuple(kwargs.get("provider_source_ids", ())))
        paper_module.PaperTicket = canonical_ticket_type
        kwargs["provider_source_ids"] = ()
        kwargs["provider_accounts"] = ()
        return canonical_ticket_type(*args, **kwargs)

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            del record, as_of
            paper_module.PaperTicket = hostile_paper_ticket
            return resolution

    coordinator = object.__new__(ContinuousSessionCoordinator)
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
            match="PaperBook|paper book|PaperTicket|ticket.*authority|materialization.*changed|dependency.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        paper_module.PaperTicket = canonical_ticket_type

    assert hostile_calls == []
    assert paper_module.PaperTicket is PaperTicket


def test_outcome_callback_cannot_rewrite_ticket_event_during_load(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    durable_leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [durable_leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)

    forged_leg = TicketLeg("event-2", "winner", "alice", Decimal("2"))
    resolution = SettlementResolution(
        event_identity="provider-a:event-2",
        settlement_ref="provider-result:2",
        quote_outcomes={forged_leg.quote_key: "win"},
        evidence_id="provider-a-outcome-2",
        evidence_sha256="1" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )
    canonical_leg_type = paper_module.TicketLeg
    hostile_calls: list[str] = []

    def hostile_ticket_leg(event_id, market_id, selection_id, locked_odds, **kwargs):
        hostile_calls.append(event_id)
        paper_module.TicketLeg = canonical_leg_type
        return canonical_leg_type(
            "event-2",
            market_id,
            selection_id,
            locked_odds,
            **kwargs,
        )

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            del record, as_of
            paper_module.TicketLeg = hostile_ticket_leg
            return resolution

    coordinator = object.__new__(ContinuousSessionCoordinator)
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
            match="PaperBook|paper book|TicketLeg|ticket.*authority|materialization.*changed|dependency.*changed",
        ):
            coordinator._settlement_resolutions(
                as_of="2026-09-22T07:02:00+00:00",
            )
    finally:
        paper_module.TicketLeg = canonical_leg_type

    assert hostile_calls == []
    assert paper_module.TicketLeg is TicketLeg
