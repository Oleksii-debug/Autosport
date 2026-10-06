from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.domain import TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.economic_session import ProductEconomicSessionStore
from autosport.paper import PaperBook
from autosport.risk_day_window import ProductDayRiskWindowStore
from autosport.risk_session_turnover_evidence import (
    PaperSessionTurnoverEvidenceError,
    PaperSessionTurnoverEvidenceIncompleteError,
    PaperSessionTurnoverEvidenceMismatchError,
    PaperSessionTurnoverResolver,
)


def _epoch_ns(value: str) -> int:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(instant.timestamp()) * 1_000_000_000 + instant.microsecond * 1000


class _Clock:
    def __init__(self, value: str) -> None:
        self.value = _epoch_ns(value)

    def __call__(self) -> int:
        return self.value

    def set(self, value: str) -> None:
        self.value = _epoch_ns(value)


def _goal(*, revision: int = 1, max_turnover: str = "1") -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="owner-goal",
        revision=revision,
        bankroll_id="paper-bankroll",
        currency="EUR",
        max_turnover_fraction=Decimal(max_turnover),
    )


def _leg(suffix: str, odds: str = "2") -> TicketLeg:
    return TicketLeg(
        event_id=f"event-{suffix}",
        market_id=f"market-{suffix}",
        selection_id=f"selection-{suffix}",
        locked_odds=Decimal(odds),
        sport="tennis",
    )


def _open(
    book: PaperBook,
    *,
    stake: str,
    suffix: str,
    placed_at: str,
    currency: str = "EUR",
    bankroll_id: str = "paper-bankroll",
    legs: tuple[TicketLeg, ...] | None = None,
):
    return book.open_ticket(
        legs or (_leg(suffix),),
        Decimal(stake),
        placed_at=placed_at,
        bankroll_id=bankroll_id,
        currency=currency,
        reason=f"test-{suffix}",
    )


def _fixture_causal_advance():
    record = PaperBook.__dict__["_record_product_day_admission"]
    closure = record.__closure__
    assert closure is not None
    freevars = record.__code__.co_freevars
    assert "causal_advance" in freevars
    return closure[freevars.index("causal_advance")].cell_contents


def _save_authoritative(workspace: Path, book: PaperBook) -> None:
    for ticket_id, ticket in book.tickets.items():
        if ticket_id in book._product_day_admissions:
            continue
        epoch_ns = _epoch_ns(ticket.placed_at)
        day_store = ProductDayRiskWindowStore(
            workspace,
            authority_root=workspace.parent / "authority",
            _test_clock=lambda value=epoch_ns: value,
        )
        window = day_store.current()
        witness = book._validate_product_day_admission_witness(
            (
                ticket.placed_at,
                window.day_key,
                window.window_start,
                window.window_end_exclusive,
                window.state_sha256,
                window.authority_generation,
            ),
            ticket_id=ticket_id,
        )
        book._product_day_admissions[ticket_id] = witness
        _fixture_causal_advance()(book, ticket_id, witness)
    book.save(workspace / "paper_book.json")


def _setup(tmp_path, *, max_turnover: str = "1"):
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "authority"
    workspace.mkdir(parents=True)
    EconomicGoalStore(workspace).initialize_owner(
        _goal(max_turnover=max_turnover)
    )
    book = PaperBook("100")
    book.save(workspace / "paper_book.json")
    clock = _Clock("2026-10-05T12:00:00Z")
    store = ProductEconomicSessionStore(
        workspace,
        authority_root=authority_root,
        _test_clock=clock,
    )
    session = store.current()
    return workspace, authority_root, clock, store, session


def _resolve(store: ProductEconomicSessionStore, session):
    return PaperSessionTurnoverResolver.resolve(
        session_store=store,
        session=session,
    )


def test_session_turnover_counts_parlay_stake_once(tmp_path):
    workspace, _, _, store, session = _setup(tmp_path)
    book = PaperBook.load(workspace / "paper_book.json")
    _open(
        book,
        stake="25",
        suffix="parlay",
        placed_at="2026-10-05T12:30:00Z",
        legs=(_leg("a"), _leg("b", "1.5")),
    )
    _save_authoritative(workspace, book)

    evidence = _resolve(store, session)

    assert evidence.confirmed_turnover == Decimal("25")
    assert evidence.constituent_count == 1
    assert evidence.turnover_cap == Decimal("100")
    assert evidence.residual_headroom == Decimal("75")
    assert not evidence.breached
    assert evidence.scope_class == "ECONOMIC_SESSION"
    assert evidence.metric_class == "PAPER_ACCEPTED_TURNOVER"
    assert not evidence.atomic_admission_authority
    assert not evidence.account_wide_provider_turnover_complete
    assert not evidence.real_money_execution_authorized


def test_restart_and_midnight_do_not_reset_session_turnover(tmp_path):
    workspace, authority_root, clock, store, session = _setup(tmp_path)
    book = PaperBook.load(workspace / "paper_book.json")
    _open(
        book,
        stake="30",
        suffix="before-midnight",
        placed_at="2026-10-05T23:59:59Z",
    )
    _save_authoritative(workspace, book)
    first = _resolve(store, session)

    clock.set("2026-10-06T12:00:00Z")
    restarted = ProductEconomicSessionStore(
        workspace,
        authority_root=authority_root,
        _test_clock=clock,
    )
    current = restarted.current()
    second = _resolve(restarted, current)

    assert current.session_id == session.session_id
    assert second == first
    assert second.confirmed_turnover == Decimal("30")


def test_explicit_successor_resets_only_session_scope(tmp_path):
    workspace, authority_root, clock, store, first_session = _setup(tmp_path)
    book = PaperBook.load(workspace / "paper_book.json")
    _open(
        book,
        stake="30",
        suffix="first-session",
        placed_at="2026-10-05T12:30:00Z",
    )
    _save_authoritative(workspace, book)
    assert _resolve(store, first_session).confirmed_turnover == Decimal("30")

    EconomicGoalStore(workspace).persist_automatic_successor(
        _goal(revision=2, max_turnover="0.8")
    )
    clock.set("2026-10-05T14:00:00Z")
    successor = store.transition_to_current_goal(first_session)

    book = PaperBook.load(workspace / "paper_book.json")
    _open(
        book,
        stake="20",
        suffix="second-session",
        placed_at="2026-10-05T14:30:00Z",
    )
    _save_authoritative(workspace, book)

    evidence = _resolve(store, successor)
    assert successor.session_id != first_session.session_id
    assert evidence.confirmed_turnover == Decimal("20")
    assert evidence.constituent_count == 1
    assert evidence.turnover_cap == Decimal("80")


def test_stale_predecessor_session_cannot_resolve_successor_turnover(tmp_path):
    workspace, _, clock, store, first_session = _setup(tmp_path)
    EconomicGoalStore(workspace).persist_automatic_successor(
        _goal(revision=2, max_turnover="0.8")
    )
    clock.set("2026-10-05T14:00:00Z")
    store.transition_to_current_goal(first_session)

    with pytest.raises(
        PaperSessionTurnoverEvidenceIncompleteError,
        match="stale",
    ):
        _resolve(store, first_session)


def test_settlement_does_not_change_accepted_session_turnover(tmp_path):
    workspace, _, _, store, session = _setup(tmp_path)
    book = PaperBook.load(workspace / "paper_book.json")
    ticket = _open(
        book,
        stake="10",
        suffix="settled",
        placed_at="2026-10-05T12:30:00Z",
        legs=(_leg("settled-a"), _leg("settled-b", "1.5")),
    )
    _save_authoritative(workspace, book)
    before = _resolve(store, session)

    book = PaperBook.load(workspace / "paper_book.json")
    book.settle(
        ticket.ticket_id,
        {leg.quote_key for leg in ticket.legs},
        settled_at="2026-10-05T13:00:00Z",
    )
    _save_authoritative(workspace, book)
    after = _resolve(store, session)

    assert after.confirmed_turnover == Decimal("10")
    assert after.constituent_sha256 == before.constituent_sha256
    assert after.evidence_sha256 == before.evidence_sha256


def test_session_turnover_rejects_cross_currency_laundering(tmp_path):
    workspace, _, _, store, session = _setup(tmp_path)
    book = PaperBook.load(workspace / "paper_book.json")
    _open(
        book,
        stake="10",
        suffix="usd",
        placed_at="2026-10-05T12:30:00Z",
        currency="USD",
    )
    _save_authoritative(workspace, book)

    with pytest.raises(
        PaperSessionTurnoverEvidenceIncompleteError,
        match="currency identity",
    ):
        _resolve(store, session)


def test_require_current_rejects_modified_evidence(tmp_path):
    workspace, _, _, store, session = _setup(tmp_path)
    book = PaperBook.load(workspace / "paper_book.json")
    _open(
        book,
        stake="10",
        suffix="candidate",
        placed_at="2026-10-05T12:30:00Z",
    )
    _save_authoritative(workspace, book)
    evidence = _resolve(store, session)

    with pytest.raises(PaperSessionTurnoverEvidenceMismatchError):
        PaperSessionTurnoverResolver.require_current(
            replace(evidence, confirmed_turnover=Decimal("0")),
            session_store=store,
            session=session,
        )


def test_corrupt_paperbook_blocks_positive_session_turnover(tmp_path):
    workspace, _, _, store, session = _setup(tmp_path)
    (workspace / "paper_book.json").write_text(
        '{"schema_version":7,"balance":"tampered"}\n',
        encoding="utf-8",
    )

    with pytest.raises(PaperSessionTurnoverEvidenceIncompleteError):
        _resolve(store, session)


def test_session_evidence_digest_is_input_order_independent(tmp_path):
    workspace, _, _, store, session = _setup(tmp_path)
    book = PaperBook.load(workspace / "paper_book.json")
    _open(
        book,
        stake="10",
        suffix="a",
        placed_at="2026-10-05T12:30:00Z",
    )
    _open(
        book,
        stake="15",
        suffix="b",
        placed_at="2026-10-05T12:40:00Z",
    )
    _save_authoritative(workspace, book)

    evidence = _resolve(store, session)
    restarted = _resolve(store, store.current())

    assert restarted == evidence
    assert evidence.confirmed_turnover == Decimal("25")
    assert evidence.constituent_count == 2
