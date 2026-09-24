from decimal import Decimal

import pytest

from autosport.domain import TicketLeg
from autosport.economic_admission import admit_paper_ticket
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


def _leg() -> TicketLeg:
    return TicketLeg(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        locked_odds=Decimal("2.00"),
    )


def test_post_save_readback_must_match_exact_admitted_semantic_state(
    tmp_path,
    monkeypatch,
) -> None:
    book_path = tmp_path / "paper_book.json"
    caller_view = PaperBook("1000")
    caller_view.save(book_path)
    real_load = PaperBook.load
    load_count = 0

    def substituted_readback(path):
        nonlocal load_count
        loaded = real_load(path)
        load_count += 1
        if load_count == 2:
            assert len(loaded.tickets) == 1
            persisted_ticket = next(iter(loaded.tickets.values()))
            # Keep the forged readback semantically valid so this regression proves
            # that schema/lifecycle validation alone is insufficient after commit.
            persisted_ticket.strategy_reason = "substituted-after-save"
            PaperBook._validate_loaded_state(loaded)
        return loaded

    monkeypatch.setattr(PaperBook, "load", staticmethod(substituted_readback))

    with pytest.raises(
        RuntimeError,
        match="persisted PaperBook state does not match the admitted mutation",
    ):
        admit_paper_ticket(
            workspace=tmp_path,
            book=caller_view,
            risk_policy=PaperRiskPolicy(
                max_ticket_fraction=Decimal("0.10"),
                max_committed_fraction=Decimal("0.20"),
                minimum_cash_reserve_fraction=Decimal("0"),
            ),
            stake=Decimal("50"),
            legs=(_leg(),),
            reason="risk-reviewed-admission",
            placed_at="2026-09-25T00:00:00Z",
        )

    # The caller must not accept the substituted readback as its canonical view.
    assert caller_view.balance == Decimal("1000")
    assert caller_view.tickets == {}

    # The actual durable write remains the exact admitted state. The injected
    # substitution existed only in the post-save readback boundary.
    durable = real_load(book_path)
    assert durable.balance == Decimal("950")
    assert len(durable.tickets) == 1
    durable_ticket = next(iter(durable.tickets.values()))
    assert durable_ticket.strategy_reason == "risk-reviewed-admission"
    assert durable_ticket.stake == Decimal("50")
