from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    SettlementResolution,
)
from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook


def _coordinator(root: Path) -> ContinuousSessionCoordinator:
    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.workspace = root
    coordinator.paper_book_path = root / "paper_book.json"
    coordinator.initial_bankroll = "100"
    return coordinator


def _resolution(leg: TicketLeg, *, available_at: str) -> SettlementResolution:
    return SettlementResolution(
        event_identity="feed:book:event-1",
        settlement_ref="provider-result:1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="outcome-colon-source-1",
        evidence_sha256="0" * 64,
        available_at=available_at,
    )


def test_colon_bearing_source_identity_settles_legacy_event_leg(tmp_path: Path) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    ticket = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("feed:book",),
    )
    book.save(tmp_path / "paper_book.json")
    coordinator = _coordinator(tmp_path)

    settled, evidence_ids = coordinator._settle(
        resolutions=(
            _resolution(
                leg,
                available_at="2026-09-22T07:01:00+00:00",
            ),
        )
    )

    assert settled == (ticket.ticket_id,)
    assert evidence_ids == ("outcome-colon-source-1",)
    reloaded = PaperBook.load(tmp_path / "paper_book.json")
    assert reloaded.tickets[ticket.ticket_id].status is TicketStatus.WON
    assert reloaded.balance == Decimal("110")


def test_colon_bearing_source_identity_keeps_late_ticket_causality(tmp_path: Path) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    ticket = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:01:00+00:00",
        provider_source_ids=("feed:book",),
    )
    book.save(tmp_path / "paper_book.json")
    coordinator = _coordinator(tmp_path)

    with pytest.raises(
        ContinuousSessionError,
        match="predates matching open ticket placement",
    ):
        coordinator._settle(
            resolutions=(
                _resolution(
                    leg,
                    available_at="2026-09-22T07:00:00+00:00",
                ),
            )
        )

    reloaded = PaperBook.load(tmp_path / "paper_book.json")
    assert reloaded.tickets[ticket.ticket_id].status is TicketStatus.OPEN
    assert reloaded.balance == Decimal("90")
