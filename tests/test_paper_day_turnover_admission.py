from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import autosport.economic_admission as economic_admission
import autosport.risk as risk_module

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_admission import admit_paper_ticket
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext
from autosport.workspace_lock import WorkspaceEconomicLock


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


def test_admission_rejects_workspace_lock_rebinding_before_mutation(tmp_path):
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    candidate = _leg("lock-dispatch")
    original_acquire = WorkspaceEconomicLock.acquire

    def hostile_acquire(_self) -> None:
        raise AssertionError("mutated admission lock acquire executed")

    try:
        WorkspaceEconomicLock.acquire = hostile_acquire
        with pytest.raises(
            RuntimeError,
            match="workspace economic lock executable authority changed",
        ):
            admit_paper_ticket(
                workspace=tmp_path,
                book=book,
                risk_policy=policy,
                stake=Decimal("1"),
                legs=(candidate,),
                reason="lock dispatch must remain canonical",
                placed_at="2026-10-03T06:30:00Z",
            )
    finally:
        WorkspaceEconomicLock.acquire = original_acquire

    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert persisted.tickets == {}


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


def test_day_turnover_override_does_not_bypass_local_ticket_limit(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("candidate")
    context = _context(candidate, _timestamp(now))
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.00001"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )

    baseline = policy.evaluate(book, Decimal("0.01"), context=context)
    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("0.01"),
        legs=(candidate,),
        reason="post-turnover local risk gate",
        placed_at=_timestamp(now),
        context=context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is False
    assert result.risk.reason == "ticket exceeds configured bankroll fraction"


def test_day_turnover_override_does_not_bypass_quote_freshness(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("candidate")
    stale_quote = MarketEvent(
        event_id=candidate.event_id,
        market_id=candidate.market_id,
        selection_id=candidate.selection_id,
        decimal_odds=candidate.locked_odds,
        observed_ts=_timestamp(now - timedelta(hours=1)),
        source_id="provider-1",
        sequence=1,
        source_ts=_timestamp(now - timedelta(hours=1, seconds=1)),
        ingest_ts=_timestamp(now - timedelta(hours=1) + timedelta(seconds=1)),
    )
    context = ProposedTicketRiskContext(
        legs=(candidate,),
        quotes=(stale_quote,),
        bankroll_id="paper-bankroll",
        currency="USD",
        proposal_ts=_timestamp(now),
    )
    policy = _policy(goal)

    baseline = policy.evaluate(book, Decimal("0.01"), context=context)
    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("0.01"),
        legs=(candidate,),
        reason="post-turnover quote gate",
        placed_at=_timestamp(now),
        context=context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is False
    assert "quote" in result.risk.reason



def test_risk_module_timestamp_rebind_cannot_bypass_post_turnover_quote_gate(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("candidate-risk-global-rebind")
    stale_quote = MarketEvent(
        event_id=candidate.event_id,
        market_id=candidate.market_id,
        selection_id=candidate.selection_id,
        decimal_odds=candidate.locked_odds,
        observed_ts=_timestamp(now - timedelta(hours=1)),
        source_id="provider-1",
        sequence=1,
        source_ts=_timestamp(now - timedelta(hours=1, seconds=1)),
        ingest_ts=_timestamp(now - timedelta(hours=1) + timedelta(seconds=1)),
    )
    context = ProposedTicketRiskContext(
        legs=(candidate,),
        quotes=(stale_quote,),
        bankroll_id="paper-bankroll",
        currency="USD",
        proposal_ts=_timestamp(now),
    )
    policy = _policy(goal)
    baseline = policy.evaluate(book, Decimal("0.01"), context=context)
    assert baseline.reason == "economic goal turnover limit exceeded"

    original = risk_module._canonical_context_timestamp
    try:
        risk_module._canonical_context_timestamp = (
            lambda label, value: (_timestamp(now), now)
        )
        result = admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=policy,
            stake=Decimal("0.01"),
            legs=(candidate,),
            reason="risk module timestamp rebind must not widen admission",
            placed_at=_timestamp(now),
            context=context,
            provider_source_ids=("provider-1",),
            bankroll_id="paper-bankroll",
            currency="USD",
        )
    finally:
        risk_module._canonical_context_timestamp = original

    assert result.admitted is False
    assert "quote" in result.risk.reason


def test_serial_admissions_cannot_double_spend_current_day_headroom(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    canonical = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    canonical.save(tmp_path / "paper_book.json")
    first_view = PaperBook.load(tmp_path / "paper_book.json")
    stale_second_view = PaperBook.load(tmp_path / "paper_book.json")
    policy = _policy(goal)

    first_leg = _leg("first-current-day")
    first_context = _context(first_leg, _timestamp(now))
    first = admit_paper_ticket(
        workspace=tmp_path,
        book=first_view,
        risk_policy=policy,
        stake=Decimal("3"),
        legs=(first_leg,),
        reason="consume first current-day turnover room",
        placed_at=_timestamp(now),
        context=first_context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    second_leg = _leg("second-current-day")
    second_context = _context(second_leg, _timestamp(now))
    second = admit_paper_ticket(
        workspace=tmp_path,
        book=stale_second_view,
        risk_policy=policy,
        stake=Decimal("3"),
        legs=(second_leg,),
        reason="must not double-spend current-day turnover room",
        placed_at=_timestamp(now),
        context=second_context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    assert first.admitted is True
    assert second.admitted is False
    assert second.risk.reason == "economic goal turnover limit exceeded"
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert len(persisted.tickets) == 2


def test_restart_re_resolves_remaining_current_day_headroom(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    policy = _policy(goal)

    first_leg = _leg("restart-first")
    first_context = _context(first_leg, _timestamp(now))
    first = admit_paper_ticket(
        workspace=tmp_path,
        book=PaperBook.load(tmp_path / "paper_book.json"),
        risk_policy=policy,
        stake=Decimal("3"),
        legs=(first_leg,),
        reason="current-day turnover before restart",
        placed_at=_timestamp(now),
        context=first_context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    assert first.admitted is True

    second_leg = _leg("restart-second")
    second_context = _context(second_leg, _timestamp(now))
    second = admit_paper_ticket(
        workspace=tmp_path,
        book=PaperBook.load(tmp_path / "paper_book.json"),
        risk_policy=policy,
        stake=Decimal("2"),
        legs=(second_leg,),
        reason="consume exact residual after restart",
        placed_at=_timestamp(now),
        context=second_context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    assert second.admitted is True

    third_leg = _leg("restart-third")
    third_context = _context(third_leg, _timestamp(now))
    third = admit_paper_ticket(
        workspace=tmp_path,
        book=PaperBook.load(tmp_path / "paper_book.json"),
        risk_policy=policy,
        stake=Decimal("0.01"),
        legs=(third_leg,),
        reason="cap exhausted after restart",
        placed_at=_timestamp(now),
        context=third_context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    assert third.admitted is False
    assert third.risk.reason == "economic goal turnover limit exceeded"


def test_live_module_rebind_cannot_mint_turnover_headroom(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(now), stake="5")

    original = economic_admission._revalidated_product_day_turnover_room
    try:
        economic_admission._revalidated_product_day_turnover_room = (
            lambda **kwargs: Decimal("999999")
        )
        baseline, result = _admit(tmp_path, book, goal, _timestamp(now))
    finally:
        economic_admission._revalidated_product_day_turnover_room = original

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is False
    assert result.risk.reason == "economic goal turnover limit exceeded"


def test_live_resume_helper_rebind_cannot_replace_positive_risk_suffix(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")

    original = economic_admission._resume_after_product_day_turnover
    try:
        def hostile_resume(**kwargs):
            del kwargs
            raise AssertionError("mutable admission resume helper executed")

        economic_admission._resume_after_product_day_turnover = hostile_resume
        baseline, result = _admit(tmp_path, book, goal, _timestamp(now))
    finally:
        economic_admission._resume_after_product_day_turnover = original

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is True
    assert result.risk.allowed is True
