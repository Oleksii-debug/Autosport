from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from autosport.domain import TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.economic_session import ProductEconomicSessionStore
from autosport.paper import PaperBook
from autosport.risk_day_window import ProductDayRiskWindowStore
from autosport.risk_turnover_evidence import PaperSessionTurnoverResolver


def _timestamp_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _goal(revision: int = 1) -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-paper-session-turnover",
        revision=revision,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("1"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("0.10"),
        max_risk_of_ruin=Decimal("1"),
        max_concurrent_positions=10,
    )


def _leg(suffix: str) -> TicketLeg:
    return TicketLeg(
        event_id=f"event-{suffix}",
        market_id=f"market-{suffix}",
        selection_id=f"selection-{suffix}",
        locked_odds=Decimal("2"),
    )


def _fixture_causal_advance():
    record = PaperBook.__dict__["_record_product_day_admission"]
    closure = record.__closure__
    assert closure is not None
    freevars = record.__code__.co_freevars
    assert "causal_advance" in freevars
    return closure[freevars.index("causal_advance")].cell_contents


def _add_current_admission(
    workspace: Path,
    book: PaperBook,
    *,
    stake: str,
    suffix: str,
    placed_at: str | None = None,
) -> str:
    placed_at = _timestamp_now() if placed_at is None else placed_at
    ticket = book.open_ticket(
        [_leg(suffix)],
        Decimal(stake),
        placed_at=placed_at,
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    window = ProductDayRiskWindowStore(workspace).current()
    witness = book._validate_product_day_admission_witness(
        (
            placed_at,
            window.day_key,
            window.window_start,
            window.window_end_exclusive,
            window.state_sha256,
            window.authority_generation,
        ),
        ticket_id=ticket.ticket_id,
    )
    book._product_day_admissions[ticket.ticket_id] = witness
    _fixture_causal_advance()(book, ticket.ticket_id, witness)
    return ticket.ticket_id


def _resolve(workspace: Path, book: PaperBook, session_store: ProductEconomicSessionStore):
    session = session_store.current()
    return PaperSessionTurnoverResolver.resolve(
        book=book,
        goal_store=EconomicGoalStore(workspace),
        session_store=session_store,
        session_evidence=session,
    )


def test_current_session_turnover_counts_product_admissions_after_session_start(tmp_path):
    EconomicGoalStore(tmp_path).initialize_owner(_goal())
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    session_store = ProductEconomicSessionStore(tmp_path)
    session = session_store.current()

    book = PaperBook.load(tmp_path / "paper_book.json")
    ticket_id = _add_current_admission(
        tmp_path,
        book,
        stake="4",
        suffix="current",
        placed_at=session.started_at,
    )
    book.save(tmp_path / "paper_book.json")
    current = PaperBook.load(tmp_path / "paper_book.json")

    evidence = _resolve(tmp_path, current, session_store)

    assert evidence.scope_class == "ECONOMIC_SESSION"
    assert evidence.metric_class == "PAPER_ACCEPTED_TURNOVER"
    assert evidence.confirmed_turnover == Decimal("4")
    assert evidence.turnover_cap == Decimal("10")
    assert evidence.residual_headroom == Decimal("6")
    assert evidence.constituent_count == 1
    assert ticket_id in current.tickets
    assert not evidence.atomic_admission_authority
    assert not evidence.real_money_execution_authorized


def test_session_turnover_restarts_to_same_identity_and_digest(tmp_path):
    EconomicGoalStore(tmp_path).initialize_owner(_goal())
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    initial_session = ProductEconomicSessionStore(tmp_path).current()

    book = PaperBook.load(tmp_path / "paper_book.json")
    _add_current_admission(
        tmp_path,
        book,
        stake="3",
        suffix="restart",
        placed_at=initial_session.started_at,
    )
    book.save(tmp_path / "paper_book.json")
    current = PaperBook.load(tmp_path / "paper_book.json")

    first_store = ProductEconomicSessionStore(tmp_path)
    first = _resolve(tmp_path, current, first_store)
    second = _resolve(tmp_path, current, ProductEconomicSessionStore(tmp_path))

    assert second == first
    assert second.evidence_sha256 == first.evidence_sha256
    assert second.confirmed_turnover == Decimal("3")


def test_explicit_successor_resets_session_scope_without_erasing_paper_history(tmp_path):
    goal_store = EconomicGoalStore(tmp_path)
    goal_store.initialize_owner(_goal())
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    session_store = ProductEconomicSessionStore(tmp_path)
    predecessor = session_store.current()

    book = PaperBook.load(tmp_path / "paper_book.json")
    ticket_id = _add_current_admission(
        tmp_path,
        book,
        stake="4",
        suffix="predecessor",
        placed_at=predecessor.started_at,
    )
    book.save(tmp_path / "paper_book.json")
    current = PaperBook.load(tmp_path / "paper_book.json")
    before = _resolve(tmp_path, current, session_store)
    assert before.confirmed_turnover == Decimal("4")

    goal_store.persist_automatic_successor(_goal(revision=2))
    successor = session_store.transition_to_current_goal(predecessor)
    refreshed = PaperBook.load(tmp_path / "paper_book.json")
    after = PaperSessionTurnoverResolver.resolve(
        book=refreshed,
        goal_store=EconomicGoalStore(tmp_path),
        session_store=session_store,
        session_evidence=successor,
    )

    assert ticket_id in refreshed.tickets
    assert after.session_id == successor.session_id
    assert after.confirmed_turnover == Decimal("0")
    assert after.residual_headroom == Decimal("10")
    assert after.constituent_count == 0



def test_successor_boundary_equal_admission_fails_closed_without_session_binding(tmp_path):
    goal_store = EconomicGoalStore(tmp_path)
    goal_store.initialize_owner(_goal())
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    session_store = ProductEconomicSessionStore(tmp_path)
    predecessor = session_store.current()

    goal_store.persist_automatic_successor(_goal(revision=2))
    successor = session_store.transition_to_current_goal(predecessor)

    book = PaperBook.load(tmp_path / "paper_book.json")
    _add_current_admission(
        tmp_path,
        book,
        stake="1",
        suffix="boundary-equal",
        placed_at=successor.started_at,
    )
    book.save(tmp_path / "paper_book.json")
    current = PaperBook.load(tmp_path / "paper_book.json")

    import pytest
    from autosport.risk_turnover_evidence import (
        PaperSessionTurnoverEvidenceIncompleteError,
    )

    with pytest.raises(
        PaperSessionTurnoverEvidenceIncompleteError,
        match="boundary-equal",
    ):
        PaperSessionTurnoverResolver.resolve(
            book=current,
            goal_store=EconomicGoalStore(tmp_path),
            session_store=session_store,
            session_evidence=successor,
        )


def test_session_turnover_require_current_rejects_stale_book_projection(tmp_path):
    EconomicGoalStore(tmp_path).initialize_owner(_goal())
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    session_store = ProductEconomicSessionStore(tmp_path)
    session = session_store.current()
    current = PaperBook.load(tmp_path / "paper_book.json")
    first = PaperSessionTurnoverResolver.resolve(
        book=current,
        goal_store=EconomicGoalStore(tmp_path),
        session_store=session_store,
        session_evidence=session,
    )

    changed = PaperBook.load(tmp_path / "paper_book.json")
    _add_current_admission(
        tmp_path,
        changed,
        stake="2",
        suffix="stale-projection",
        placed_at=session.started_at,
    )
    changed.save(tmp_path / "paper_book.json")
    durable = PaperBook.load(tmp_path / "paper_book.json")

    import pytest
    from autosport.risk_turnover_evidence import (
        PaperSessionTurnoverEvidenceMismatchError,
    )

    with pytest.raises(PaperSessionTurnoverEvidenceMismatchError):
        PaperSessionTurnoverResolver.require_current(
            first,
            book=durable,
            goal_store=EconomicGoalStore(tmp_path),
            session_store=ProductEconomicSessionStore(tmp_path),
            session_evidence=ProductEconomicSessionStore(tmp_path).current(),
        )


def test_session_turnover_require_current_rejects_predecessor_after_goal_transition(tmp_path):
    goal_store = EconomicGoalStore(tmp_path)
    goal_store.initialize_owner(_goal())
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    session_store = ProductEconomicSessionStore(tmp_path)
    predecessor = session_store.current()
    evidence = PaperSessionTurnoverResolver.resolve(
        book=PaperBook.load(tmp_path / "paper_book.json"),
        goal_store=goal_store,
        session_store=session_store,
        session_evidence=predecessor,
    )
    goal_store.persist_automatic_successor(_goal(revision=2))
    successor = session_store.transition_to_current_goal(predecessor)

    import pytest
    from autosport.risk_turnover_evidence import (
        PaperSessionTurnoverEvidenceMismatchError,
    )

    with pytest.raises(PaperSessionTurnoverEvidenceMismatchError):
        PaperSessionTurnoverResolver.require_current(
            evidence,
            book=PaperBook.load(tmp_path / "paper_book.json"),
            goal_store=EconomicGoalStore(tmp_path),
            session_store=session_store,
            session_evidence=successor,
        )


def test_session_turnover_counts_parlay_ticket_stake_once(tmp_path):
    EconomicGoalStore(tmp_path).initialize_owner(_goal())
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    session_store = ProductEconomicSessionStore(tmp_path)
    session = session_store.current()

    current = PaperBook.load(tmp_path / "paper_book.json")
    legs = [_leg("parlay-a"), _leg("parlay-b")]
    ticket = current.open_ticket(
        legs,
        Decimal("4"),
        placed_at=session.started_at,
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    window = ProductDayRiskWindowStore(tmp_path).current()
    witness = current._validate_product_day_admission_witness(
        (
            session.started_at,
            window.day_key,
            window.window_start,
            window.window_end_exclusive,
            window.state_sha256,
            window.authority_generation,
        ),
        ticket_id=ticket.ticket_id,
    )
    current._product_day_admissions[ticket.ticket_id] = witness
    _fixture_causal_advance()(current, ticket.ticket_id, witness)
    current.save(tmp_path / "paper_book.json")

    evidence = _resolve(
        tmp_path,
        PaperBook.load(tmp_path / "paper_book.json"),
        session_store,
    )

    assert evidence.confirmed_turnover == Decimal("4")
    assert evidence.constituent_count == 1


def test_session_turnover_is_invariant_to_later_settlement(tmp_path):
    EconomicGoalStore(tmp_path).initialize_owner(_goal())
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    session_store = ProductEconomicSessionStore(tmp_path)
    session = session_store.current()

    current = PaperBook.load(tmp_path / "paper_book.json")
    ticket_id = _add_current_admission(
        tmp_path,
        current,
        stake="4",
        suffix="settlement",
        placed_at=session.started_at,
    )
    current.save(tmp_path / "paper_book.json")
    before_book = PaperBook.load(tmp_path / "paper_book.json")
    before = _resolve(tmp_path, before_book, session_store)

    settled = PaperBook.load(tmp_path / "paper_book.json")
    ticket = settled.tickets[ticket_id]
    settled.settle(ticket_id, set(), {ticket.legs[0].quote_key})
    settled.save(tmp_path / "paper_book.json")
    after = _resolve(
        tmp_path,
        PaperBook.load(tmp_path / "paper_book.json"),
        session_store,
    )

    assert after.confirmed_turnover == before.confirmed_turnover == Decimal("4")
    assert after.constituent_sha256 == before.constituent_sha256
    assert after.evidence_sha256 == before.evidence_sha256


def test_unwitnessed_ticket_timestamp_cannot_mint_session_turnover_scope(tmp_path):
    EconomicGoalStore(tmp_path).initialize_owner(_goal())
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    session_store = ProductEconomicSessionStore(tmp_path)
    session = session_store.current()

    current = PaperBook.load(tmp_path / "paper_book.json")
    current.open_ticket(
        [_leg("unwitnessed")],
        Decimal("2"),
        placed_at=session.started_at,
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    current.save(tmp_path / "paper_book.json")

    import pytest
    from autosport.risk_turnover_evidence import (
        PaperSessionTurnoverEvidenceIncompleteError,
    )

    with pytest.raises(PaperSessionTurnoverEvidenceIncompleteError):
        PaperSessionTurnoverResolver.resolve(
            book=PaperBook.load(tmp_path / "paper_book.json"),
            goal_store=EconomicGoalStore(tmp_path),
            session_store=session_store,
            session_evidence=session,
        )
