from datetime import datetime, timedelta, timezone
from decimal import Decimal

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_admission import admit_paper_ticket
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


def _timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _leg(suffix: str) -> TicketLeg:
    return TicketLeg(
        event_id=f"event-{suffix}",
        market_id=f"market-{suffix}",
        selection_id=f"selection-{suffix}",
        locked_odds=Decimal("2"),
    )


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-paper-day-turnover-admission",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("1"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("0.05"),
        max_risk_of_ruin=Decimal("1"),
        max_concurrent_positions=10,
    )


def _policy(goal: EconomicGoalContract) -> PaperRiskPolicy:
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )


def _context(leg: TicketLeg, placed_at: str) -> ProposedTicketRiskContext:
    proposal = datetime.fromisoformat(placed_at.replace("Z", "+00:00"))
    observed = proposal - timedelta(seconds=2)
    quote = MarketEvent(
        event_id=leg.event_id,
        market_id=leg.market_id,
        selection_id=leg.selection_id,
        decimal_odds=leg.locked_odds,
        observed_ts=_timestamp(observed),
        source_id="provider-1",
        sequence=1,
        source_ts=_timestamp(observed - timedelta(seconds=1)),
        ingest_ts=_timestamp(observed + timedelta(seconds=1)),
    )
    return ProposedTicketRiskContext(
        legs=(leg,),
        quotes=(quote,),
        bankroll_id="paper-bankroll",
        currency="USD",
        proposal_ts=placed_at,
    )


def _book_with_settled_turnover(*, placed_at: str, stake: str) -> PaperBook:
    book = PaperBook("100")
    prior = _leg("prior")
    ticket = book.open_ticket(
        [prior],
        Decimal(stake),
        placed_at=placed_at,
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    book.settle(ticket.ticket_id, set(), {ticket.legs[0].quote_key})
    return book


def _admit(tmp_path, book: PaperBook, goal: EconomicGoalContract, placed_at: str):
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("candidate")
    context = _context(candidate, placed_at)
    policy = _policy(goal)
    baseline = policy.evaluate(book, Decimal("0.01"), context=context)
    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("0.01"),
        legs=(candidate,),
        reason="product-issued day turnover regression",
        placed_at=placed_at,
        context=context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    return baseline, result


def test_product_issued_current_utc_day_releases_old_day_turnover(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")

    baseline, result = _admit(tmp_path, book, goal, _timestamp(now))

    assert baseline.allowed is False
    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is True
    assert result.risk.allowed is True
    assert result.ticket is not None
    assert result.ticket.stake == Decimal("0.01")


def test_current_day_turnover_still_consumes_exact_owner_cap(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(now), stake="5")

    baseline, result = _admit(tmp_path, book, goal, _timestamp(now))

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is False
    assert result.risk.reason == "economic goal turnover limit exceeded"
    assert len(result.book.tickets) == 1


def test_missing_durable_goal_never_mints_day_turnover_headroom(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")

    baseline, result = _admit(tmp_path, book, goal, _timestamp(now))

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is False
    assert result.risk.reason == "economic goal turnover limit exceeded"
    assert len(result.book.tickets) == 1


def test_candidate_outside_current_product_day_cannot_spend_current_headroom(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(
        placed_at=_timestamp(old - timedelta(days=1)),
        stake="50",
    )

    baseline, result = _admit(tmp_path, book, goal, _timestamp(old))

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is False
    assert result.risk.reason == "economic goal turnover limit exceeded"
    assert len(result.book.tickets) == 1
