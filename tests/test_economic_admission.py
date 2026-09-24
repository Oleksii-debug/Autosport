from decimal import Decimal

import pytest

from autosport.domain import TicketLeg
from autosport.economic_admission import admit_paper_ticket
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy
from autosport.workspace_lock import WorkspaceEconomicLockBusyError


def _leg() -> TicketLeg:
    return TicketLeg(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        locked_odds=Decimal("2.00"),
    )


def test_allowed_admission_evaluates_and_opens_inside_one_workspace_lock(tmp_path):
    book = PaperBook("1000")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.10"),
        max_committed_fraction=Decimal("0.20"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )

    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("50"),
        legs=(_leg(),),
        reason="atomic admission regression",
        placed_at="2026-09-24T16:00:00Z",
    )

    assert result.admitted is True
    assert result.risk.allowed is True
    assert result.ticket is not None
    assert result.ticket.stake == Decimal("50")
    assert book.balance == Decimal("950")
    assert len(book.tickets) == 1


def test_denied_admission_does_not_mutate_paper_book(tmp_path):
    book = PaperBook("1000")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.01"),
        max_committed_fraction=Decimal("0.20"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )

    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("50"),
        legs=(_leg(),),
        reason="must remain rejected",
        placed_at="2026-09-24T16:00:00Z",
    )

    assert result.admitted is False
    assert result.risk.allowed is False
    assert result.ticket is None
    assert book.balance == Decimal("1000")
    assert book.tickets == {}
    assert book._lifecycle == []


def test_existing_workspace_writer_blocks_stale_parallel_admission(tmp_path):
    book = PaperBook("1000")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.10"),
        max_committed_fraction=Decimal("0.20"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )

    from autosport.workspace_lock import WorkspaceEconomicLock

    lock = WorkspaceEconomicLock(tmp_path)
    lock.acquire()
    try:
        with pytest.raises(WorkspaceEconomicLockBusyError):
            admit_paper_ticket(
                workspace=tmp_path,
                book=book,
                risk_policy=policy,
                stake=Decimal("50"),
                legs=(_leg(),),
                reason="must not evaluate stale state",
                placed_at="2026-09-24T16:00:00Z",
            )
    finally:
        lock.release()

    assert book.balance == Decimal("1000")
    assert book.tickets == {}


@pytest.mark.parametrize("stake", ["0", "-1", "NaN", "Infinity"])
def test_invalid_stake_fails_before_economic_mutation(tmp_path, stake):
    book = PaperBook("1000")

    with pytest.raises(ValueError, match="finite positive decimal"):
        admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=PaperRiskPolicy(),
            stake=stake,
            legs=(_leg(),),
            reason="invalid",
            placed_at="2026-09-24T16:00:00Z",
        )

    assert book.balance == Decimal("1000")
    assert book.tickets == {}
