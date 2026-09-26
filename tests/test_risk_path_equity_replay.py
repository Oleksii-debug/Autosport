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


def _leg(
    *,
    selection_id: str = "home",
    odds: str = "1.5",
    exchange_side: str | None = None,
) -> TicketLeg:
    return TicketLeg(
        event_id="event-1",
        market_id="market-1",
        selection_id=selection_id,
        locked_odds=Decimal(odds),
        sport="soccer",
        exchange_side=exchange_side,
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
        require_complete_observed_timestamps=True,
    )

    assert replay.start_balance == Decimal("100")
    assert replay.minimum_equity == Decimal("60")
    assert replay.final_balance == Decimal("120")
    assert [item.balance for item in replay.transitions] == [
        Decimal("60"),
        Decimal("120"),
    ]
    assert replay.latest_observed_at == "2026-01-01T11:00:00+00:00"
    assert replay.observed_timestamps_complete is True


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
    assert relaxed.observed_timestamps_complete is False
    assert relaxed.latest_observed_at == "2026-01-01T10:00:00+00:00"

    with pytest.raises(RiskPathEquityReplayError, match="lacks observed timestamp"):
        replay_paper_book_equity_path(
            base,
            final,
            expected_changed_ticket_ids=frozenset({ticket.ticket_id}),
            require_complete_observed_timestamps=True,
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


def test_base_open_ticket_settlement_does_not_double_debit_stake(tmp_path) -> None:
    book = PaperBook("100")
    ticket = book.open_ticket(
        [_leg()],
        Decimal("40"),
        placed_at="2026-01-01T10:00:00+00:00",
    )
    assert book.balance == Decimal("60")
    base = _snapshot(book, tmp_path / "base-open.json")

    book.settle(
        ticket.ticket_id,
        {ticket.legs[0].quote_key},
        settled_at="2026-01-01T11:00:00+00:00",
    )
    final = _snapshot(book, tmp_path / "final-settled.json")

    replay = replay_paper_book_equity_path(
        base,
        final,
        expected_changed_ticket_ids=frozenset({ticket.ticket_id}),
        require_complete_observed_timestamps=True,
    )

    assert replay.start_balance == Decimal("60")
    assert replay.minimum_equity == Decimal("60")
    assert replay.final_balance == Decimal("120")
    assert len(replay.transitions) == 1
    assert replay.transitions[0].action == "settle"


@pytest.mark.parametrize(
    "bankroll",
    ("1E+100000000", "1E-100000000"),
)
def test_replay_rejects_exponent_sized_fixed_point_evidence(
    tmp_path,
    bankroll: str,
) -> None:
    book = PaperBook(bankroll)
    snapshot = _snapshot(book, tmp_path / "huge-exponent.json")

    with pytest.raises(
        RiskPathEquityReplayError,
        match="fixed-point representation exceeds supported evidence size",
    ):
        replay_paper_book_equity_path(
            snapshot,
            snapshot,
            expected_changed_ticket_ids=frozenset(),
        )


def test_replay_rejects_huge_scale_zero_before_fixed_point_materialization(
    tmp_path,
) -> None:
    book = PaperBook("1")
    book.open_ticket(
        [_leg()],
        Decimal("1"),
        placed_at="2026-01-01T10:00:00+00:00",
    )
    snapshot = _snapshot(book, tmp_path / "zero-balance.json")
    assert b'"balance": "0"' in snapshot
    scaled_zero_snapshot = snapshot.replace(
        b'"balance": "0"',
        b'"balance": "0E-100000000"',
        1,
    )

    # Decimal equality keeps this a semantically valid PaperBook snapshot:
    # lifecycle replay reaches numeric zero. The risk evidence renderer must
    # nevertheless reject its enormous fixed-point scale before allocation.
    PaperBook.load_bytes(scaled_zero_snapshot)
    with pytest.raises(
        RiskPathEquityReplayError,
        match="fixed-point representation exceeds supported evidence size",
    ):
        replay_paper_book_equity_path(
            scaled_zero_snapshot,
            scaled_zero_snapshot,
            expected_changed_ticket_ids=frozenset(),
        )

def test_replay_rejects_lay_ticket_already_present_in_base(tmp_path) -> None:
    book = PaperBook("100")
    ticket = book.open_ticket(
        [_leg(odds="3", exchange_side="lay")],
        Decimal("10"),
        placed_at="2026-01-01T10:00:00+00:00",
    )
    base = _snapshot(book, tmp_path / "base-lay.json")

    with pytest.raises(
        RiskPathEquityReplayError,
        match="LAY ticket",
    ):
        replay_paper_book_equity_path(
            base,
            base,
            expected_changed_ticket_ids=frozenset(),
        )

    assert ticket.legs[0].exchange_side == "lay"


def test_replay_rejects_lay_ticket_opened_in_suffix(tmp_path) -> None:
    book = PaperBook("100")
    base = _snapshot(book, tmp_path / "base-before-lay.json")
    ticket = book.open_ticket(
        [_leg(odds="3", exchange_side="lay")],
        Decimal("10"),
        placed_at="2026-01-01T10:00:00+00:00",
    )
    final = _snapshot(book, tmp_path / "final-with-lay.json")

    with pytest.raises(
        RiskPathEquityReplayError,
        match="LAY ticket",
    ):
        replay_paper_book_equity_path(
            base,
            final,
            expected_changed_ticket_ids=frozenset({ticket.ticket_id}),
        )


def test_replay_preserves_explicit_back_exchange_side(tmp_path) -> None:
    book = PaperBook("100")
    base = _snapshot(book, tmp_path / "base-before-back.json")
    ticket = book.open_ticket(
        [_leg(odds="3", exchange_side="back")],
        Decimal("10"),
        placed_at="2026-01-01T10:00:00+00:00",
    )
    final = _snapshot(book, tmp_path / "final-with-back.json")

    replay = replay_paper_book_equity_path(
        base,
        final,
        expected_changed_ticket_ids=frozenset({ticket.ticket_id}),
    )

    assert replay.start_balance == Decimal("100")
    assert replay.final_balance == Decimal("90")
    assert replay.minimum_equity == Decimal("90")

