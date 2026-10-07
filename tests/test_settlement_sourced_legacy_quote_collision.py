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


def test_scoped_outcome_cannot_settle_exact_source_and_legacy_same_quote(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    legacy_ticket = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
    )
    provider_ticket = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    book.save(tmp_path / "paper_book.json")
    balance_before = book.balance
    lifecycle_before = list(book._lifecycle)

    resolution = SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="provider-result:1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-1",
        evidence_sha256="0" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )

    with pytest.raises(
        ContinuousSessionError,
        match="provider source scope is ambiguous",
    ):
        _coordinator(tmp_path)._settle(resolutions=(resolution,))

    reloaded = PaperBook.load(tmp_path / "paper_book.json")
    assert reloaded.balance == balance_before
    assert reloaded.tickets[legacy_ticket.ticket_id].status is TicketStatus.OPEN
    assert reloaded.tickets[provider_ticket.ticket_id].status is TicketStatus.OPEN
    assert reloaded._lifecycle == lifecycle_before
