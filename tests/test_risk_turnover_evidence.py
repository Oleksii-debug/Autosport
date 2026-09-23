from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from autosport.domain import TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.paper import PaperBook
from autosport.risk_day_window import ProductDayRiskWindowStore
from autosport.risk_turnover_evidence import (
    PaperDayTurnoverEvidenceIncompleteError,
    PaperDayTurnoverEvidenceMismatchError,
    PaperDayTurnoverResolver,
)


def _store(tmp_path):
    return ProductDayRiskWindowStore(
        tmp_path / "workspace",
        authority_root=tmp_path / "authority",
    )


def _goal(
    *,
    currency: str = "EUR",
    max_turnover: str = "1",
    revision: int = 1,
) -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="owner-goal",
        revision=revision,
        bankroll_id="paper-bankroll",
        currency=currency,
        max_turnover_fraction=Decimal(max_turnover),
    )


def _goal_store(
    day_store: ProductDayRiskWindowStore,
    *,
    currency: str = "EUR",
    max_turnover: str = "1",
) -> EconomicGoalStore:
    expected = _goal(currency=currency, max_turnover=max_turnover)
    store = EconomicGoalStore(day_store.workspace)
    if store.path.exists():
        assert store.load() == expected
    else:
        store.initialize_owner(expected)
    return store


def _leg(suffix: str, odds: str = "2") -> TicketLeg:
    return TicketLeg(
        event_id=f"event-{suffix}",
        market_id=f"market-{suffix}",
        selection_id=f"selection-{suffix}",
        locked_odds=Decimal(odds),
        sport="tennis",
    )


def _inside(window, *, hours: int = 1) -> str:
    start = datetime.fromisoformat(window.window_start.replace("Z", "+00:00"))
    return (start + timedelta(hours=hours)).isoformat()


def _before(window) -> str:
    start = datetime.fromisoformat(window.window_start.replace("Z", "+00:00"))
    return (start - timedelta(seconds=1)).isoformat()


def _book(initial: str = "100") -> PaperBook:
    return PaperBook(Decimal(initial))


def _open(
    book: PaperBook,
    window,
    *,
    stake: str,
    suffix: str,
    placed_at: str | None = None,
    currency: str | None = "EUR",
    bankroll_id: str | None = "paper-bankroll",
    legs: tuple[TicketLeg, ...] | None = None,
):
    return book.open_ticket(
        legs or (_leg(suffix),),
        Decimal(stake),
        placed_at=placed_at or _inside(window),
        bankroll_id=bankroll_id,
        currency=currency,
        reason=f"test-{suffix}",
    )


def test_current_day_turnover_counts_ticket_stake_once_for_parlay(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(
        book,
        window,
        stake="25",
        suffix="parlay",
        legs=(_leg("a", "2"), _leg("b", "1.5")),
    )

    evidence = PaperDayTurnoverResolver.resolve(
        book=book,
        goal_store=_goal_store(store, max_turnover="1"),
        window_store=store,
        window_evidence=window,
    )

    assert evidence.confirmed_turnover == Decimal("25")
    assert evidence.constituent_count == 1
    assert evidence.turnover_cap == Decimal("100")
    assert evidence.residual_headroom == Decimal("75")
    assert not evidence.breached
    assert evidence.metric_class == "PAPER_ACCEPTED_TURNOVER"
    assert evidence.scope_class == "UTC_DAY"
    assert not evidence.atomic_admission_authority
    assert not evidence.account_wide_provider_turnover_complete
    assert not evidence.real_money_execution_authorized


def test_previous_day_ticket_does_not_enter_current_utc_day(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(
        book,
        window,
        stake="30",
        suffix="old",
        placed_at=_before(window),
    )
    _open(book, window, stake="20", suffix="current")

    evidence = PaperDayTurnoverResolver.resolve(
        book=book,
        goal_store=_goal_store(store),
        window_store=store,
        window_evidence=window,
    )

    assert evidence.confirmed_turnover == Decimal("20")
    assert evidence.constituent_count == 1


def test_settlement_and_payout_do_not_erase_or_inflate_accepted_turnover(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    ticket = _open(
        book,
        window,
        stake="10",
        suffix="settle",
        legs=(_leg("settle-a", "2"), _leg("settle-b", "1.5")),
    )

    before = PaperDayTurnoverResolver.resolve(
        book=book,
        goal_store=_goal_store(store),
        window_store=store,
        window_evidence=window,
    )
    book.settle(
        ticket.ticket_id,
        {leg.quote_key for leg in ticket.legs},
        settled_at=_inside(window, hours=2),
    )
    after = PaperDayTurnoverResolver.resolve(
        book=book,
        goal_store=_goal_store(store),
        window_store=store,
        window_evidence=window,
    )

    assert after.confirmed_turnover == Decimal("10")
    assert after.constituent_sha256 == before.constituent_sha256
    assert after.evidence_sha256 == before.evidence_sha256


def test_breach_truth_is_reported_not_discarded(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(book, window, stake="60", suffix="breach")

    evidence = PaperDayTurnoverResolver.resolve(
        book=book,
        goal_store=_goal_store(store, max_turnover="0.5"),
        window_store=store,
        window_evidence=window,
    )

    assert evidence.confirmed_turnover == Decimal("60")
    assert evidence.turnover_cap == Decimal("50")
    assert evidence.residual_headroom == Decimal("0")
    assert evidence.breached


def test_current_day_ticket_requires_exact_goal_bankroll_and_currency(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(
        book,
        window,
        stake="10",
        suffix="legacy",
        bankroll_id=None,
        currency=None,
    )

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="bankroll identity",
    ):
        PaperDayTurnoverResolver.resolve(
            book=book,
            goal_store=_goal_store(store),
            window_store=store,
            window_evidence=window,
        )


def test_cross_currency_ticket_cannot_be_silently_aggregated(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(book, window, stake="10", suffix="eur", currency="EUR")

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="currency identity",
    ):
        PaperDayTurnoverResolver.resolve(
            book=book,
            goal_store=_goal_store(store, currency="USD"),
            window_store=store,
            window_evidence=window,
        )


def test_require_current_rejects_caller_forged_turnover_scalar(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(book, window, stake="10", suffix="forgery")

    canonical = PaperDayTurnoverResolver.resolve(
        book=book,
        goal_store=_goal_store(store),
        window_store=store,
        window_evidence=window,
    )
    forged = replace(canonical, evidence_sha256="0" * 64)

    with pytest.raises(PaperDayTurnoverEvidenceMismatchError):
        PaperDayTurnoverResolver.require_current(
            forged,
            book=book,
            goal_store=_goal_store(store),
            window_store=store,
            window_evidence=window,
        )


def test_new_ticket_invalidates_old_evidence_on_reresolution(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(book, window, stake="10", suffix="one")

    first = PaperDayTurnoverResolver.resolve(
        book=book,
        goal_store=_goal_store(store),
        window_store=store,
        window_evidence=window,
    )
    _open(book, window, stake="5", suffix="two", placed_at=_inside(window, hours=2))

    with pytest.raises(PaperDayTurnoverEvidenceMismatchError):
        PaperDayTurnoverResolver.require_current(
            first,
            book=book,
            goal_store=_goal_store(store),
            window_store=store,
            window_evidence=window,
        )

    second = PaperDayTurnoverResolver.resolve(
        book=book,
        goal_store=_goal_store(store),
        window_store=store,
        window_evidence=window,
    )
    assert second.confirmed_turnover == Decimal("15")
    assert second.constituent_count == 2
    assert second.evidence_sha256 != first.evidence_sha256


def test_synthetic_day_clock_cannot_mint_positive_turnover_evidence(tmp_path):
    epoch_ns = 1_800_000_000 * 1_000_000_000
    store = ProductDayRiskWindowStore(
        tmp_path / "workspace",
        authority_root=tmp_path / "authority",
        _test_clock=lambda: epoch_ns,
    )
    window = store.current()
    book = _book()
    _open(book, window, stake="10", suffix="synthetic")

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="not current product authority",
    ):
        PaperDayTurnoverResolver.resolve(
            book=book,
            goal_store=_goal_store(store),
            window_store=store,
            window_evidence=window,
        )


def test_subclassed_book_cannot_override_authority_boundary(tmp_path):
    class DerivedPaperBook(PaperBook):
        pass

    store = _store(tmp_path)
    window = store.current()
    book = DerivedPaperBook(Decimal("100"))

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="canonical PaperBook",
    ):
        PaperDayTurnoverResolver.resolve(
            book=book,
            goal_store=_goal_store(store),
            window_store=store,
            window_evidence=window,
        )


def test_durable_goal_tightening_invalidates_old_turnover_evidence(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store, max_turnover="1")
    book = _book()
    _open(book, window, stake="60", suffix="goal-tighten")

    first = PaperDayTurnoverResolver.resolve(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )
    assert first.turnover_cap == Decimal("100")
    assert not first.breached

    tightened = replace(
        goal_store.load(),
        revision=2,
        max_turnover_fraction=Decimal("0.5"),
    )
    goal_store.persist_automatic_successor(tightened)

    with pytest.raises(PaperDayTurnoverEvidenceMismatchError):
        PaperDayTurnoverResolver.require_current(
            first,
            book=book,
            goal_store=goal_store,
            window_store=store,
            window_evidence=window,
        )

    second = PaperDayTurnoverResolver.resolve(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )
    assert second.goal_revision == 2
    assert second.goal_contract_sha256 != first.goal_contract_sha256
    assert second.turnover_cap == Decimal("50")
    assert second.confirmed_turnover == Decimal("60")
    assert second.breached


def test_goal_and_day_authority_must_share_workspace(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    foreign_goal_store = EconomicGoalStore(tmp_path / "foreign-workspace")
    foreign_goal_store.initialize_owner(_goal())
    book = _book()

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="share one canonical workspace",
    ):
        PaperDayTurnoverResolver.resolve(
            book=book,
            goal_store=foreign_goal_store,
            window_store=store,
            window_evidence=window,
        )
