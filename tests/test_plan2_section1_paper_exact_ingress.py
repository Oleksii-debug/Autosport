"""Plan 2 Section 1: exact PAPER bankroll/stake ingress and durable settlement."""
from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook


class _HostileText(str):
    def __str__(self) -> str:
        raise AssertionError("caller-controlled string coercion must not run")


class _HostileDecimal(Decimal):
    def __str__(self) -> str:
        raise AssertionError("caller-controlled decimal coercion must not run")


@pytest.mark.parametrize(
    "bad",
    [100.0, 0.1, 100, True, _HostileText("100"), _HostileDecimal("100")],
)
def test_virtual_bankroll_rejects_inexact_or_hostile_transport(bad: object) -> None:
    with pytest.raises(ValueError, match="initial_bankroll must be a Decimal or decimal string"):
        PaperBook(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [10.0, 0.1, 10, False, _HostileText("10"), _HostileDecimal("10")],
)
def test_open_ticket_rejects_inexact_stake_without_financial_effect(bad: object) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2.5"))
    with pytest.raises(ValueError, match="stake must be a Decimal or decimal string"):
        book.open_ticket([leg], bad)  # type: ignore[arg-type]
    assert book.balance == Decimal("100")
    assert book.tickets == {}
    assert book.committed_stake == Decimal("0")


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity", "broken"])
def test_nonfinite_and_malformed_financial_transport_stays_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        PaperBook(bad)
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    with pytest.raises(ValueError):
        book.open_ticket([leg], bad)
    assert book.balance == Decimal("100")
    assert not book.tickets


def test_exact_paper_stake_settlement_restart_and_no_double_effect(tmp_path) -> None:
    book = PaperBook(Decimal("100.00"))
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2.5"))
    ticket = book.open_ticket(
        [leg], Decimal("0.10"), placed_at="2026-10-08T00:00:00+00:00"
    )
    assert book.balance == Decimal("99.90")
    assert book.committed_stake == Decimal("0.10")

    snapshot = tmp_path / "paper.json"
    book.save(snapshot)
    resumed = PaperBook.load(snapshot)
    assert resumed.balance == Decimal("99.90")
    assert resumed.committed_stake == Decimal("0.10")
    assert resumed.tickets[ticket.ticket_id].status is TicketStatus.OPEN

    settled = resumed.settle(
        ticket.ticket_id,
        {leg.quote_key},
        settled_at="2026-10-08T01:00:00+00:00",
    )
    assert settled.payout == Decimal("0.25")
    assert resumed.balance == Decimal("100.15")
    assert resumed.committed_stake == Decimal("0")

    resumed.save(snapshot)
    restored = PaperBook.load(snapshot)
    assert restored.balance == Decimal("100.15")
    assert restored.tickets[ticket.ticket_id].status is TicketStatus.WON
    with pytest.raises(ValueError, match="already settled"):
        restored.settle(ticket.ticket_id, {leg.quote_key})
    assert restored.balance == Decimal("100.15")
