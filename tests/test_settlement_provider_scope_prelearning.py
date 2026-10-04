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


class _OutcomeAuthority:
    def __init__(self, resolution: SettlementResolution) -> None:
        self.resolution = resolution
        self.calls = 0

    def resolve(self, record, *, as_of: str):
        self.calls += 1
        return self.resolution


class _Lifecycle:
    def __init__(self, record) -> None:
        self.record = record

    def records(self):
        return (self.record,)


def _coordinator(
    *,
    paper_book_path: Path,
    resolution: SettlementResolution,
) -> tuple[ContinuousSessionCoordinator, _OutcomeAuthority]:
    authority = _OutcomeAuthority(resolution)
    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.paper_book_path = paper_book_path
    coordinator.initial_bankroll = "100"
    coordinator.outcome_authority = authority
    coordinator.lifecycle = _Lifecycle(
        SimpleNamespace(
            phase=EventPhase.COMPLETED,
            settlement_ref=resolution.settlement_ref,
            identity=resolution.event_identity,
        )
    )
    return coordinator, authority


def test_provider_scope_ambiguity_fails_before_resolution_leaves_outcome_boundary(
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
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-b",),
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
    coordinator, authority = _coordinator(
        paper_book_path=paper_book_path,
        resolution=resolution,
    )

    with pytest.raises(
        ContinuousSessionError,
        match="provider source scope is ambiguous",
    ):
        coordinator._settlement_resolutions(
            as_of="2026-09-22T07:02:00+00:00",
        )

    assert authority.calls == 1


def test_multi_source_ticket_scope_fails_closed_before_prelearning(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    ticket = book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("provider-a", "provider-b"),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)
    before = paper_book_path.read_bytes()

    resolution = SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="provider-result:multi-source",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-multi-source",
        evidence_sha256="2" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )
    coordinator, authority = _coordinator(
        paper_book_path=paper_book_path,
        resolution=resolution,
    )

    with pytest.raises(
        ContinuousSessionError,
        match="multi-source|provider source scope is ambiguous",
    ):
        coordinator._settlement_resolutions(
            as_of="2026-09-22T07:02:00+00:00",
        )

    assert authority.calls == 1
    assert paper_book_path.read_bytes() == before
    reloaded = PaperBook.load(paper_book_path)
    assert reloaded.tickets[ticket.ticket_id].status.value == "open"
    assert reloaded.balance == Decimal("90")


def test_colon_bearing_provider_scope_passes_prelearning_when_unambiguous(
    tmp_path: Path,
) -> None:
    book = PaperBook("100")
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    book.open_ticket(
        [leg],
        "10",
        placed_at="2026-09-22T07:00:00+00:00",
        provider_source_ids=("feed:book",),
    )
    paper_book_path = tmp_path / "paper_book.json"
    book.save(paper_book_path)

    resolution = SettlementResolution(
        event_identity="feed:book:event-1",
        settlement_ref="provider-result:1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="feed-book-outcome-1",
        evidence_sha256="1" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )
    coordinator, authority = _coordinator(
        paper_book_path=paper_book_path,
        resolution=resolution,
    )

    assert coordinator._settlement_resolutions(
        as_of="2026-09-22T07:02:00+00:00",
    ) == (resolution,)
    assert authority.calls == 1
