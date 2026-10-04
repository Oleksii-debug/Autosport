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


def _resolution(leg: TicketLeg, *, event_identity: str) -> SettlementResolution:
    return SettlementResolution(
        event_identity=event_identity,
        settlement_ref="provider-result:1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id=f"outcome:{event_identity}",
        evidence_sha256="0" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )


def test_source_scoped_outcome_cannot_cross_settle_same_quote_from_other_provider(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    provider_a = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    provider_b = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-b",),
    )
    book.save(tmp_path / "paper_book.json")
    lifecycle_before = list(book._lifecycle)

    resolution = _resolution(leg, event_identity="provider-a:event-1")

    with pytest.raises(
        ContinuousSessionError,
        match="provider|source|scope|ambiguous",
    ):
        _coordinator(tmp_path)._settle(resolutions=(resolution,))

    reloaded = PaperBook.load(tmp_path / "paper_book.json")
    assert reloaded.balance == Decimal("80")
    assert reloaded.tickets[provider_a.ticket_id].status is TicketStatus.OPEN
    assert reloaded.tickets[provider_b.ticket_id].status is TicketStatus.OPEN
    assert reloaded._lifecycle == lifecycle_before


def test_bare_legacy_outcome_does_not_erase_provider_scope(tmp_path: Path) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    provider_ticket = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    book.save(tmp_path / "paper_book.json")
    lifecycle_before = list(book._lifecycle)

    settled, evidence_ids = _coordinator(tmp_path)._settle(
        resolutions=(_resolution(leg, event_identity="event-1"),),
    )

    assert settled == ()
    assert evidence_ids == ("outcome:event-1",)
    reloaded = PaperBook.load(tmp_path / "paper_book.json")
    assert reloaded.balance == Decimal("90")
    assert reloaded.tickets[provider_ticket.ticket_id].status is TicketStatus.OPEN
    assert reloaded._lifecycle == lifecycle_before


def test_bare_outcome_cannot_cross_settle_two_sourced_tickets_with_same_quote(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    provider_a = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a",),
    )
    provider_b = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-b",),
    )
    book.save(tmp_path / "paper_book.json")
    lifecycle_before = list(book._lifecycle)

    settled, evidence_ids = _coordinator(tmp_path)._settle(
        resolutions=(_resolution(leg, event_identity="event-1"),),
    )

    assert settled == ()
    assert evidence_ids == ("outcome:event-1",)
    reloaded = PaperBook.load(tmp_path / "paper_book.json")
    assert reloaded.balance == Decimal("80")
    assert reloaded.tickets[provider_a.ticket_id].status is TicketStatus.OPEN
    assert reloaded.tickets[provider_b.ticket_id].status is TicketStatus.OPEN
    assert reloaded._lifecycle == lifecycle_before


def test_bare_legacy_outcome_fails_closed_when_quote_key_spans_source_scopes(
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
    lifecycle_before = list(book._lifecycle)

    with pytest.raises(
        ContinuousSessionError,
        match="provider|source|scope|ambiguous",
    ):
        _coordinator(tmp_path)._settle(
            resolutions=(_resolution(leg, event_identity="event-1"),),
        )

    reloaded = PaperBook.load(tmp_path / "paper_book.json")
    assert reloaded.balance == Decimal("80")
    assert reloaded.tickets[legacy_ticket.ticket_id].status is TicketStatus.OPEN
    assert reloaded.tickets[provider_ticket.ticket_id].status is TicketStatus.OPEN
    assert reloaded._lifecycle == lifecycle_before


def test_bare_legacy_outcome_still_settles_unambiguous_source_less_ticket(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    legacy_ticket = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")

    settled, evidence_ids = _coordinator(tmp_path)._settle(
        resolutions=(_resolution(leg, event_identity="event-1"),),
    )

    assert settled == (legacy_ticket.ticket_id,)
    assert evidence_ids == ("outcome:event-1",)
    reloaded = PaperBook.load(tmp_path / "paper_book.json")
    assert reloaded.balance == Decimal("110")
    assert reloaded.tickets[legacy_ticket.ticket_id].status is TicketStatus.WON


def test_single_prefix_outcome_still_settles_source_less_legacy_ticket(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    legacy_ticket = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")

    settled, evidence_ids = _coordinator(tmp_path)._settle(
        resolutions=(_resolution(leg, event_identity="provider-a:event-1"),),
    )

    assert settled == (legacy_ticket.ticket_id,)
    assert evidence_ids == ("outcome:provider-a:event-1",)
    reloaded = PaperBook.load(tmp_path / "paper_book.json")
    assert reloaded.balance == Decimal("110")
    assert reloaded.tickets[legacy_ticket.ticket_id].status is TicketStatus.WON


def test_multi_segment_scope_does_not_expand_source_less_legacy_ticket(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    legacy_ticket = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")
    lifecycle_before = list(book._lifecycle)

    settled, evidence_ids = _coordinator(tmp_path)._settle(
        resolutions=(_resolution(leg, event_identity="feed:book:event-1"),),
    )

    assert settled == ()
    assert evidence_ids == ("outcome:feed:book:event-1",)
    reloaded = PaperBook.load(tmp_path / "paper_book.json")
    assert reloaded.balance == Decimal("90")
    assert reloaded.tickets[legacy_ticket.ticket_id].status is TicketStatus.OPEN
    assert reloaded._lifecycle == lifecycle_before


def test_legacy_first_prefix_rule_preserves_colon_bearing_event_id(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("book:event-1", "winner", "alice", Decimal("2"))
    legacy_ticket = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")

    settled, _evidence_ids = _coordinator(tmp_path)._settle(
        resolutions=(_resolution(leg, event_identity="feed:book:event-1"),),
    )

    assert settled == (legacy_ticket.ticket_id,)
    reloaded = PaperBook.load(tmp_path / "paper_book.json")
    assert reloaded.balance == Decimal("110")
    assert reloaded.tickets[legacy_ticket.ticket_id].status is TicketStatus.WON
