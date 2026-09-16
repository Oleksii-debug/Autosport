from decimal import Decimal

import pytest

from autosport.bookmaker_routing import (
    ExternalEffect, RoutingContractError, RoutingState, VenueObservation, VenueQuote,
    route_residual,
)
from autosport.opportunity import QuoteRef


def _quote(source: str, selection: str = "home") -> QuoteRef:
    return QuoteRef(
        event_id="event-1", market_id="winner", selection_id=selection,
        source_id=source, sequence=1, decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-16T19:00:00+00:00", source_ts=None,
        ingest_ts="2026-09-16T19:00:00+00:00", market_event_hash="a" * 64,
    )


def _venue(source: str, account: str, ceiling: str = "100") -> VenueQuote:
    return VenueQuote(source, account, _quote(source), Decimal(ceiling))


def test_confirmed_first_portion_routes_only_residual_to_selected_second_venue() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    decision = route_residual(
        Decimal("100"), (a, b),
        (VenueObservation("book-a", "acct-a", ExternalEffect.ACCEPTED, Decimal("40")),),
    )
    assert decision.state is RoutingState.ROUTE
    assert decision.residual == Decimal("60")
    assert decision.next_venue == b


def test_market_refusal_does_not_become_account_ban_and_allows_fallback() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    decision = route_residual(
        Decimal("100"), (a, b),
        (VenueObservation("book-a", "acct-a", ExternalEffect.MARKET_REFUSED),),
    )
    assert decision.state is RoutingState.ROUTE
    assert decision.next_venue == b
    assert a.account_enabled is True


def test_ambiguous_acknowledgement_blocks_blind_reroute() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    decision = route_residual(
        Decimal("40"), (a, b),
        (VenueObservation("book-a", "acct-a", ExternalEffect.UNKNOWN),),
    )
    assert decision.state is RoutingState.BLOCKED_UNKNOWN
    assert decision.next_venue is None
    assert decision.residual == Decimal("40")


def test_unselected_observation_cannot_enlarge_authority() -> None:
    with pytest.raises(RoutingContractError, match="unselected"):
        route_residual(
            Decimal("10"), (_venue("book-a", "acct-a"),),
            (VenueObservation("book-b", "acct-b", ExternalEffect.MARKET_REFUSED),),
        )


def test_quote_must_be_bound_to_venue_provider() -> None:
    with pytest.raises(RoutingContractError, match="source_id"):
        VenueQuote("book-b", "acct-b", _quote("book-a"), Decimal("10"))


def test_partial_when_confirmed_stake_exists_but_no_selected_capacity_remains() -> None:
    a = _venue("book-a", "acct-a")
    decision = route_residual(
        Decimal("100"), (a,),
        (VenueObservation("book-a", "acct-a", ExternalEffect.ACCEPTED, Decimal("40")),),
    )
    assert decision.state is RoutingState.PARTIAL
    assert decision.residual == Decimal("60")


def test_complete_uses_only_externally_confirmed_accepted_stake() -> None:
    a = _venue("book-a", "acct-a")
    decision = route_residual(
        Decimal("40"), (a,),
        (VenueObservation("book-a", "acct-a", ExternalEffect.ACCEPTED, Decimal("40")),),
    )
    assert decision.state is RoutingState.COMPLETE
    assert decision.residual == Decimal("0")


def test_selected_quotes_must_share_market_selection_identity() -> None:
    a = _venue("book-a", "acct-a")
    b = VenueQuote("book-b", "acct-b", _quote("book-b", "away"), Decimal("100"))
    with pytest.raises(RoutingContractError, match="same market selection"):
        route_residual(Decimal("10"), (a, b))
