from decimal import Decimal

import pytest

from autosport.domain import TicketLeg
from autosport.economic_admission import admit_paper_ticket
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext
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
    book.save(tmp_path / "paper_book.json")
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
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert persisted.balance == Decimal("950")
    assert result.ticket.ticket_id in persisted.tickets


def test_missing_canonical_book_fails_closed_without_bootstrap(tmp_path):
    book = PaperBook("1000")

    with pytest.raises(
        FileNotFoundError,
        match="canonical paper_book.json must already exist",
    ):
        admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=PaperRiskPolicy(),
            stake=Decimal("10"),
            legs=(_leg(),),
            reason="must not create a second bootstrap authority",
            placed_at="2026-09-24T16:00:00Z",
        )

    assert not (tmp_path / "paper_book.json").exists()
    assert book.balance == Decimal("1000")
    assert book.tickets == {}


def test_denied_admission_does_not_mutate_paper_book(tmp_path):
    book = PaperBook("1000")
    book.save(tmp_path / "paper_book.json")
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


def _other_leg() -> TicketLeg:
    return TicketLeg(
        event_id="event-2",
        market_id="market-2",
        selection_id="selection-2",
        locked_odds=Decimal("1.80"),
    )


def test_risk_context_legs_must_match_ticket_that_is_opened(tmp_path):
    book = PaperBook("1000")
    context = ProposedTicketRiskContext(legs=(_leg(),))

    with pytest.raises(ValueError, match="risk context legs must match"):
        admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=PaperRiskPolicy(),
            stake=Decimal("10"),
            legs=(_other_leg(),),
            reason="must not swap ticket after risk review",
            placed_at="2026-09-24T16:00:00Z",
            context=context,
        )

    assert book.balance == Decimal("1000")
    assert book.tickets == {}


def test_risk_context_proposal_time_must_match_ticket_placed_at(tmp_path):
    book = PaperBook("1000")
    context = ProposedTicketRiskContext(
        legs=(_leg(),),
        proposal_ts="2026-09-24T16:00:00Z",
    )

    with pytest.raises(ValueError, match="proposal_ts must match admitted ticket placed_at"):
        admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=PaperRiskPolicy(),
            stake=Decimal("10"),
            legs=(_leg(),),
            reason="must not substitute a later ticket time after risk review",
            placed_at="2026-09-24T17:00:00Z",
            context=context,
        )

    assert book.balance == Decimal("1000")
    assert book.tickets == {}
    assert not (tmp_path / "paper_book.json").exists()


def test_matching_risk_context_proposal_time_is_persisted_exactly(tmp_path):
    book = PaperBook("1000")
    book.save(tmp_path / "paper_book.json")
    context = ProposedTicketRiskContext(
        legs=(_leg(),),
        proposal_ts="2026-09-24T16:00:00Z",
    )

    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=PaperRiskPolicy(),
        stake=Decimal("10"),
        legs=(_leg(),),
        reason="preserve reviewed proposal time",
        placed_at=context.proposal_ts,
        context=context,
    )

    assert result.admitted is True
    assert result.ticket is not None
    assert result.ticket.placed_at == context.proposal_ts


def test_risk_context_bankroll_identity_must_match_persisted_ticket(tmp_path):
    book = PaperBook("1000")
    context = ProposedTicketRiskContext(
        legs=(_leg(),),
        bankroll_id="bankroll-reviewed",
        currency="EUR",
    )

    with pytest.raises(ValueError, match="bankroll and currency must match"):
        admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=PaperRiskPolicy(),
            stake=Decimal("10"),
            legs=(_leg(),),
            reason="must preserve reviewed bankroll identity",
            placed_at="2026-09-24T16:00:00Z",
            context=context,
            bankroll_id="bankroll-opened",
            currency="EUR",
        )

    assert book.balance == Decimal("1000")
    assert book.tickets == {}


def test_matching_risk_context_remains_admissible(tmp_path):
    book = PaperBook("1000")
    book.save(tmp_path / "paper_book.json")
    context = ProposedTicketRiskContext(
        legs=(_leg(),),
        bankroll_id="bankroll-1",
        currency="EUR",
    )

    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=PaperRiskPolicy(),
        stake=Decimal("10"),
        legs=(_leg(),),
        reason="matching reviewed ticket",
        placed_at="2026-09-24T16:00:00Z",
        context=context,
        bankroll_id="bankroll-1",
        currency="EUR",
    )

    assert result.admitted is True
    assert result.ticket is not None
    assert result.ticket.legs == context.legs
    assert result.ticket.bankroll_id == context.bankroll_id
    assert result.ticket.currency == context.currency


def test_second_stale_book_refreshes_under_lock_before_risk_admission(tmp_path):
    book_path = tmp_path / "paper_book.json"
    PaperBook("1000").save(book_path)
    first_view = PaperBook.load(book_path)
    stale_second_view = PaperBook.load(book_path)
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.20"),
        max_committed_fraction=Decimal("0.15"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )

    first = admit_paper_ticket(
        workspace=tmp_path,
        book=first_view,
        risk_policy=policy,
        stake=Decimal("100"),
        legs=(_leg(),),
        reason="first serialized admission",
        placed_at="2026-09-24T16:00:00Z",
    )
    second = admit_paper_ticket(
        workspace=tmp_path,
        book=stale_second_view,
        risk_policy=policy,
        stake=Decimal("100"),
        legs=(_other_leg(),),
        reason="must see first durable commitment",
        placed_at="2026-09-24T16:00:01Z",
    )

    assert first.admitted is True
    assert second.admitted is False
    assert stale_second_view.balance == Decimal("900")
    assert len(stale_second_view.tickets) == 1
    persisted = PaperBook.load(book_path)
    assert persisted.balance == Decimal("900")
    assert len(persisted.tickets) == 1
    assert first.ticket is not None
    assert first.ticket.ticket_id in persisted.tickets


class _PaperBookSubclass(PaperBook):
    pass


class _AdversarialStake(Decimal):
    def is_finite(self):
        raise AssertionError("subclass virtual method must not execute")


def test_admission_rejects_paperbook_subclass_before_polymorphic_authority(tmp_path):
    with pytest.raises(TypeError, match="exact PaperBook"):
        admit_paper_ticket(
            workspace=tmp_path,
            book=_PaperBookSubclass("1000"),
            risk_policy=PaperRiskPolicy(),
            stake=Decimal("10"),
            legs=(_leg(),),
            reason="subclass must not become authority",
            placed_at="2026-09-24T16:00:00Z",
        )


def test_admission_rejects_decimal_subclass_before_virtual_dispatch(tmp_path):
    book = PaperBook("1000")
    with pytest.raises(ValueError, match="finite positive decimal"):
        admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=PaperRiskPolicy(),
            stake=_AdversarialStake("10"),
            legs=(_leg(),),
            reason="stake subclass must not become authority",
            placed_at="2026-09-24T16:00:00Z",
        )
    assert book.balance == Decimal("1000")
    assert not (tmp_path / "paper_book.json").exists()
