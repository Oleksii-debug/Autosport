from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import autosport.portfolio_live_product_universe as product_universe
from autosport.domain import MarketEvent, TicketLeg
from autosport.market_mirror import MarketMirror
from autosport.paper import PaperBook
from autosport.portfolio_live_product_universe import (
    PaperLivePortfolioUniverseError,
    resolve_paper_live_portfolio_universe,
    verify_paper_live_portfolio_universe,
)
from autosport.risk import PaperRiskPolicy


AS_OF = datetime(2026, 9, 22, 0, 2, tzinfo=timezone.utc)
MAX_AGE = timedelta(minutes=10)
SKEW_US = 120_000_000


def _leg(*, sport: str | None = "soccer", selection_id: str = "s1") -> TicketLeg:
    return TicketLeg(
        event_id="e1",
        market_id="m1",
        selection_id=selection_id,
        locked_odds=Decimal("2.00"),
        sport=sport,
    )


def _open(
    book: PaperBook,
    *,
    leg: TicketLeg | None = None,
    legs: tuple[TicketLeg, ...] | None = None,
    source_id: str = "betfair",
    account_id: str = "acct-1",
    stake: str = "10",
):
    chosen = legs if legs is not None else (leg or _leg(),)
    return book.open_ticket(
        chosen,
        stake,
        placed_at="2026-09-22T00:00:00Z",
        provider_source_ids=(source_id,),
        provider_accounts=((source_id, account_id),),
        bankroll_id="paper-bankroll",
        currency="EUR",
    )


def _event(
    *,
    leg: TicketLeg | None = None,
    source_id: str = "betfair",
    sequence: int = 1,
    source_ts: str = "2026-09-22T00:01:00Z",
    observed_ts: str = "2026-09-22T00:01:01Z",
    ingest_ts: str = "2026-09-22T00:01:02Z",
) -> MarketEvent:
    chosen = leg or _leg()
    return MarketEvent(
        event_id=chosen.event_id,
        market_id=chosen.market_id,
        selection_id=chosen.selection_id,
        sport=chosen.sport,
        decimal_odds=Decimal("2.10"),
        observed_ts=observed_ts,
        source_ts=source_ts,
        ingest_ts=ingest_ts,
        source_id=source_id,
        sequence=sequence,
    )


def _resolve(book: PaperBook, mirror: MarketMirror):
    return resolve_paper_live_portfolio_universe(
        book,
        mirror,
        as_of=AS_OF,
        max_age=MAX_AGE,
        max_observation_skew_microseconds=SKEW_US,
    )


def _verify(evidence, book: PaperBook, mirror: MarketMirror) -> bool:
    return verify_paper_live_portfolio_universe(
        evidence,
        book,
        mirror,
        as_of=AS_OF,
        max_age=MAX_AGE,
        max_observation_skew_microseconds=SKEW_US,
    )


def test_empty_validated_book_reverifies_as_exact_empty_product_universe() -> None:
    book = PaperBook("1000")
    mirror = MarketMirror()

    evidence = _resolve(book, mirror)

    assert evidence.position_ids == ()
    assert evidence.structural_evidence.position_count == 0
    assert evidence.structural_evidence.total_capital_at_risk == Decimal("0")
    assert evidence.structural_evidence.source_authority_proven is False
    assert evidence.structural_evidence.portfolio_universe_authoritative is False
    assert _verify(evidence, book, mirror)


def test_open_ticket_membership_and_capital_are_derived_not_caller_declared() -> None:
    book = PaperBook("1000")
    ticket = _open(book)
    mirror = MarketMirror()
    mirror.apply(_event())

    evidence = _resolve(book, mirror)

    assert evidence.position_ids == (ticket.ticket_id,)
    assert evidence.structural_evidence.position_count == 1
    assert evidence.structural_evidence.total_capital_at_risk == Decimal("10")
    assert (
        evidence.structural_evidence.conservative_componentwise_loss_upper_bound
        == Decimal("10")
    )
    assert (
        evidence.structural_evidence.portfolio_state_sha256
        == evidence.product_portfolio_state_sha256
    )
    assert _verify(evidence, book, mirror)


def test_two_tickets_on_same_quote_remain_two_product_positions() -> None:
    book = PaperBook("1000")
    first = _open(book, stake="10")
    second = _open(book, stake="15")
    mirror = MarketMirror()
    mirror.apply(_event())

    evidence = _resolve(book, mirror)

    assert evidence.position_ids == tuple(sorted((first.ticket_id, second.ticket_id)))
    assert evidence.structural_evidence.position_count == 2
    assert evidence.structural_evidence.total_capital_at_risk == Decimal("25")


def test_missing_or_stale_quote_blocks_positive_reverification() -> None:
    book = PaperBook("1000")
    _open(book)

    with pytest.raises(PaperLivePortfolioUniverseError, match="does not exactly cover"):
        _resolve(book, MarketMirror())

    stale = MarketMirror()
    stale.apply(
        _event(
            source_ts="2026-09-21T20:00:00Z",
            observed_ts="2026-09-21T20:00:01Z",
            ingest_ts="2026-09-21T20:00:02Z",
        )
    )
    with pytest.raises(PaperLivePortfolioUniverseError, match="does not exactly cover"):
        _resolve(book, stale)


def test_multi_leg_ticket_fails_closed_instead_of_duplicating_joint_stake() -> None:
    book = PaperBook("1000")
    _open(
        book,
        legs=(
            _leg(selection_id="s1"),
            TicketLeg(
                event_id="e2",
                market_id="m2",
                selection_id="s2",
                locked_odds=Decimal("3.00"),
                sport="soccer",
            ),
        ),
    )

    with pytest.raises(PaperLivePortfolioUniverseError, match="only single-leg"):
        _resolve(book, MarketMirror())


def test_missing_provider_provenance_cannot_enter_product_universe() -> None:
    book = PaperBook("1000")
    leg = _leg()
    book.open_ticket(
        (leg,),
        "10",
        placed_at="2026-09-22T00:00:00Z",
    )
    mirror = MarketMirror()
    mirror.apply(_event(leg=leg))

    with pytest.raises(PaperLivePortfolioUniverseError, match="provider source"):
        _resolve(book, mirror)


def test_product_commitment_closes_sport_alias_left_by_risk_projection(tmp_path) -> None:
    soccer = PaperBook("1000")
    soccer_leg = _leg(sport="soccer")
    ticket = _open(soccer, leg=soccer_leg)
    path = tmp_path / "paper.json"
    soccer.save(path)

    tennis = PaperBook.load(path)
    tennis_ticket = tennis.tickets[ticket.ticket_id]
    tennis_leg = _leg(sport="tennis")
    tennis_ticket.legs = (tennis_leg,)

    assert (
        PaperRiskPolicy.risk_of_ruin_portfolio_sha256(soccer)
        == PaperRiskPolicy.risk_of_ruin_portfolio_sha256(tennis)
    )

    soccer_mirror = MarketMirror()
    soccer_mirror.apply(_event(leg=soccer_leg))
    tennis_mirror = MarketMirror()
    tennis_mirror.apply(_event(leg=tennis_leg))

    soccer_evidence = _resolve(soccer, soccer_mirror)
    tennis_evidence = _resolve(tennis, tennis_mirror)

    assert soccer_evidence.paper_risk_state_sha256 == tennis_evidence.paper_risk_state_sha256
    assert (
        soccer_evidence.product_portfolio_state_sha256
        != tennis_evidence.product_portfolio_state_sha256
    )


def test_book_mutation_during_mirror_capture_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = PaperBook("1000")
    _open(book)
    mirror = MarketMirror()
    mirror.apply(_event())
    canonical = product_universe._CANONICAL_ACTIVE_VIEW_FOR_KEYS

    def mutate_then_capture(bound_mirror, keys, *, as_of, max_age):
        _open(book, stake="7")
        return canonical(bound_mirror, keys, as_of=as_of, max_age=max_age)

    monkeypatch.setattr(
        product_universe,
        "_CANONICAL_ACTIVE_VIEW_FOR_KEYS",
        mutate_then_capture,
    )

    with pytest.raises(PaperLivePortfolioUniverseError, match="changed during universe"):
        _resolve(book, mirror)


def test_market_ingest_after_decision_boundary_fails_closed() -> None:
    book = PaperBook("1000")
    _open(book)
    mirror = MarketMirror()
    mirror.apply(
        _event(
            source_ts="2026-09-22T00:01:00Z",
            observed_ts="2026-09-22T00:01:01Z",
            ingest_ts="2026-09-22T00:03:00Z",
        )
    )

    with pytest.raises(PaperLivePortfolioUniverseError, match="later than decision"):
        _resolve(book, mirror)


def test_possession_of_modified_evidence_is_not_product_authority() -> None:
    book = PaperBook("1000")
    _open(book)
    mirror = MarketMirror()
    mirror.apply(_event())
    evidence = _resolve(book, mirror)

    forged = replace(evidence, evidence_sha256="0" * 64)

    assert not _verify(forged, book, mirror)


def test_product_state_change_invalidates_prior_evidence() -> None:
    book = PaperBook("1000")
    _open(book)
    mirror = MarketMirror()
    mirror.apply(_event())
    evidence = _resolve(book, mirror)

    _open(book, stake="5")

    assert not _verify(evidence, book, mirror)
