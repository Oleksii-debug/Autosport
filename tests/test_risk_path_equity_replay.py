from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.risk_path_equity_replay import (
    RiskPathEquityReplayError,
    replay_paper_book_equity_path,
)


def _leg(*, selection_id: str = "home", odds: str = "1.5") -> TicketLeg:
    return TicketLeg(
        event_id="event-1",
        market_id="market-1",
        selection_id=selection_id,
        locked_odds=Decimal(odds),
        sport="soccer",
    )


def _snapshot(book: PaperBook, path) -> bytes:
    book.save(path)
    return path.read_bytes()


def test_replay_preserves_transient_minimum_after_later_win(tmp_path) -> None:
    book = PaperBook("100")
    base = _snapshot(book, tmp_path / "base.json")

    ticket = book.open_ticket(
        [_leg()],
        Decimal("40"),
        reason="risk-path-test",
        placed_at="2026-01-01T10:00:00+00:00",
        bankroll_id="paper-main",
        currency="EUR",
    )
    assert book.balance == Decimal("60")
    book.settle(
        ticket.ticket_id,
        {ticket.legs[0].quote_key},
        settled_at="2026-01-01T11:00:00+00:00",
    )
    assert book.balance == Decimal("120")
    final = _snapshot(book, tmp_path / "final.json")

    replay = replay_paper_book_equity_path(
        base,
        final,
        expected_changed_ticket_ids=frozenset({ticket.ticket_id}),
        require_causal_timestamps=True,
    )

    assert replay.start_balance == Decimal("100")
    assert replay.minimum_equity == Decimal("60")
    assert replay.final_balance == Decimal("120")
    assert [item.balance for item in replay.transitions] == [
        Decimal("60"),
        Decimal("120"),
    ]
    assert replay.causal_available_at == "2026-01-01T11:00:00+00:00"
    assert replay.causal_complete is True


def test_replay_uses_materialized_partial_stake_not_requested_alias(tmp_path) -> None:
    book = PaperBook("100")
    base = _snapshot(book, tmp_path / "base.json")

    ticket = book.open_ticket(
        [_leg(selection_id="away", odds="2.0")],
        Decimal("25"),
        reason="materialized partial stake",
        placed_at="2026-01-01T10:00:00+00:00",
        bankroll_id="paper-main",
        currency="EUR",
    )
    final = _snapshot(book, tmp_path / "final.json")

    replay = replay_paper_book_equity_path(
        base,
        final,
        expected_changed_ticket_ids=frozenset({ticket.ticket_id}),
    )

    assert replay.minimum_equity == Decimal("75")
    assert replay.final_balance == Decimal("75")


def test_foreign_interleaved_ticket_fails_closed(tmp_path) -> None:
    book = PaperBook("100")
    base = _snapshot(book, tmp_path / "base.json")

    owned = book.open_ticket(
        [_leg(selection_id="owned", odds="2.0")],
        Decimal("10"),
        placed_at="2026-01-01T10:00:00+00:00",
    )
    book.open_ticket(
        [_leg(selection_id="foreign", odds="2.0")],
        Decimal("10"),
        placed_at="2026-01-01T10:01:00+00:00",
    )
    final = _snapshot(book, tmp_path / "final.json")

    with pytest.raises(RiskPathEquityReplayError, match="foreign PaperBook mutation"):
        replay_paper_book_equity_path(
            base,
            final,
            expected_changed_ticket_ids=frozenset({owned.ticket_id}),
        )


def test_expected_ticket_alias_cannot_inflate_occurrence(tmp_path) -> None:
    book = PaperBook("100")
    base = _snapshot(book, tmp_path / "base.json")
    ticket = book.open_ticket(
        [_leg()],
        Decimal("10"),
        placed_at="2026-01-01T10:00:00+00:00",
    )
    final = _snapshot(book, tmp_path / "final.json")

    with pytest.raises(
        RiskPathEquityReplayError,
        match="expected changed ticket set does not match lifecycle suffix",
    ):
        replay_paper_book_equity_path(
            base,
            final,
            expected_changed_ticket_ids=frozenset(
                {ticket.ticket_id, "synthetic-alias-ticket"}
            ),
        )


def test_final_lifecycle_must_extend_base_exactly(tmp_path) -> None:
    book = PaperBook("100")
    ticket = book.open_ticket(
        [_leg()],
        Decimal("10"),
        placed_at="2026-01-01T10:00:00+00:00",
    )
    later = _snapshot(book, tmp_path / "later.json")

    empty = PaperBook("100")
    earlier = _snapshot(empty, tmp_path / "earlier.json")

    with pytest.raises(RiskPathEquityReplayError, match="rolls back BASE history"):
        replay_paper_book_equity_path(
            later,
            earlier,
            expected_changed_ticket_ids=frozenset({ticket.ticket_id}),
        )


def test_immutable_base_ticket_cannot_be_rewritten(tmp_path) -> None:
    book = PaperBook("100")
    ticket = book.open_ticket(
        [_leg()],
        Decimal("10"),
        placed_at="2026-01-01T10:00:00+00:00",
    )
    base = _snapshot(book, tmp_path / "base.json")

    mutated = deepcopy(book)
    mutated.tickets[ticket.ticket_id].strategy_reason = "rewritten"
    final = _snapshot(mutated, tmp_path / "final.json")

    with pytest.raises(
        RiskPathEquityReplayError,
        match="mutated immutable BASE ticket",
    ):
        replay_paper_book_equity_path(
            base,
            final,
            expected_changed_ticket_ids=frozenset(),
        )


def test_missing_settlement_timestamp_is_noncausal_and_strict_mode_rejects(
    tmp_path,
) -> None:
    book = PaperBook("100")
    base = _snapshot(book, tmp_path / "base.json")
    ticket = book.open_ticket(
        [_leg()],
        Decimal("10"),
        placed_at="2026-01-01T10:00:00+00:00",
    )
    book.settle(ticket.ticket_id, {ticket.legs[0].quote_key})
    final = _snapshot(book, tmp_path / "final.json")

    relaxed = replay_paper_book_equity_path(
        base,
        final,
        expected_changed_ticket_ids=frozenset({ticket.ticket_id}),
    )
    assert relaxed.causal_complete is False
    assert relaxed.causal_available_at == "2026-01-01T10:00:00+00:00"

    with pytest.raises(RiskPathEquityReplayError, match="lacks causal timestamp"):
        replay_paper_book_equity_path(
            base,
            final,
            expected_changed_ticket_ids=frozenset({ticket.ticket_id}),
            require_causal_timestamps=True,
        )


def test_same_exact_snapshots_rederive_same_occurrence_digest(tmp_path) -> None:
    book = PaperBook("100")
    base = _snapshot(book, tmp_path / "base.json")
    ticket = book.open_ticket(
        [_leg()],
        Decimal("10"),
        placed_at="2026-01-01T10:00:00+00:00",
    )
    final = _snapshot(book, tmp_path / "final.json")

    first = replay_paper_book_equity_path(
        base,
        final,
        expected_changed_ticket_ids=frozenset({ticket.ticket_id}),
    )
    second = replay_paper_book_equity_path(
        base,
        final,
        expected_changed_ticket_ids=frozenset({ticket.ticket_id}),
    )

    assert first == second
    assert first.source_evidence_sha256 == second.source_evidence_sha256
    assert first.source_evidence_sha256 != first.base_snapshot_sha256
    assert first.source_evidence_sha256 != first.final_snapshot_sha256


def test_replay_is_independent_of_ambient_decimal_context(tmp_path) -> None:
    book = PaperBook("100")
    base = _snapshot(book, tmp_path / "base.json")
    ticket = book.open_ticket(
        [_leg()],
        Decimal("12.345678"),
        placed_at="2026-01-01T10:00:00+00:00",
    )
    final = _snapshot(book, tmp_path / "final.json")
    expected_final = book.balance

    with localcontext() as context:
        context.prec = 3
        replay = replay_paper_book_equity_path(
            base,
            final,
            expected_changed_ticket_ids=frozenset({ticket.ticket_id}),
        )

    assert replay.minimum_equity == expected_final
    assert replay.final_balance == expected_final
