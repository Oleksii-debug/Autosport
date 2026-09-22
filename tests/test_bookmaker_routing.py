from decimal import Decimal, localcontext

import pytest

from autosport.bookmaker_routing import (
    ExternalEffect,
    RoutingContractError,
    RoutingState,
    VenueObservation,
    VenueQuote,
    route_residual,
)
from autosport.opportunity import QuoteRef


_REQUEST_ID = "route-request-1"


def _quote(source: str, selection: str = "home") -> QuoteRef:
    return QuoteRef(
        event_id="event-1",
        market_id="winner",
        selection_id=selection,
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-16T19:00:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-16T19:00:00+00:00",
        market_event_hash="a" * 64,
    )


def _venue(source: str, account: str, ceiling: str = "100") -> VenueQuote:
    return VenueQuote(source, account, _quote(source), Decimal(ceiling))


def _observation(
    venue: VenueQuote,
    effect: ExternalEffect,
    accepted: str = "0",
    observation_id: str | None = None,
    *,
    request_id: str = _REQUEST_ID,
    quote: QuoteRef | None = None,
) -> VenueObservation:
    return VenueObservation(
        venue.venue_id,
        venue.account_id,
        effect,
        request_id,
        venue.quote if quote is None else quote,
        Decimal(accepted),
        observation_id,
    )


def _route(
    requested: str,
    venues: tuple[VenueQuote, ...],
    observations: tuple[VenueObservation, ...] = (),
    *,
    request_id: str = _REQUEST_ID,
):
    return route_residual(
        Decimal(requested),
        venues,
        observations,
        routing_request_id=request_id,
    )


def test_confirmed_first_portion_routes_only_residual_to_selected_second_venue() -> None:
    a = _venue("book-a", "acct-a", "40")
    b = _venue("book-b", "acct-b")
    decision = _route(
        "100",
        (a, b),
        (_observation(a, ExternalEffect.ACCEPTED, "40"),),
    )
    assert decision.state is RoutingState.ROUTE
    assert decision.residual == Decimal("60")
    assert decision.next_venue == b
    assert decision.proposed_stake == Decimal("60")


def test_market_refusal_does_not_become_account_ban_and_allows_fallback() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    decision = _route(
        "100",
        (a, b),
        (_observation(a, ExternalEffect.MARKET_REFUSED),),
    )
    assert decision.state is RoutingState.ROUTE
    assert decision.next_venue == b
    assert decision.proposed_stake == Decimal("100")
    assert a.account_enabled is True


def test_accepted_then_refused_on_same_venue_routes_residual_to_fallback() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    decision = _route(
        "100",
        (a, b),
        (
            _observation(
                a,
                ExternalEffect.ACCEPTED,
                "40",
                "ack-a-1",
            ),
            _observation(
                a,
                ExternalEffect.MARKET_REFUSED,
                observation_id="ack-a-2",
            ),
        ),
    )
    assert decision.state is RoutingState.ROUTE
    assert decision.confirmed_total == Decimal("40")
    assert decision.residual == Decimal("60")
    assert decision.next_venue == b
    assert decision.proposed_stake == Decimal("60")


def test_remaining_same_venue_capacity_is_reused_before_fallback() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    decision = _route(
        "100",
        (a, b),
        (_observation(a, ExternalEffect.ACCEPTED, "40"),),
    )
    assert decision.state is RoutingState.ROUTE
    assert decision.next_venue == a
    assert decision.proposed_stake == Decimal("60")


def test_acceptance_ceiling_caps_proposed_stake() -> None:
    a, b = _venue("book-a", "acct-a", "30"), _venue("book-b", "acct-b")
    decision = _route("100", (a, b))
    assert decision.state is RoutingState.ROUTE
    assert decision.next_venue == a
    assert decision.residual == Decimal("100")
    assert decision.proposed_stake == Decimal("30")


def test_ambiguous_acknowledgement_blocks_blind_reroute() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    decision = _route(
        "40",
        (a, b),
        (_observation(a, ExternalEffect.UNKNOWN),),
    )
    assert decision.state is RoutingState.BLOCKED_UNKNOWN
    assert decision.next_venue is None
    assert decision.residual == Decimal("40")
    assert decision.proposed_stake == Decimal("0")


def test_unknown_blocks_complete_even_after_confirmed_total_reaches_request() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    decision = _route(
        "40",
        (a, b),
        (
            _observation(a, ExternalEffect.ACCEPTED, "40"),
            _observation(b, ExternalEffect.UNKNOWN),
        ),
    )
    assert decision.state is RoutingState.BLOCKED_UNKNOWN
    assert decision.residual == Decimal("0")
    assert decision.next_venue is None


def test_repeated_venue_observations_require_explicit_evidence_identity() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    with pytest.raises(RoutingContractError, match="observation_id"):
        _route(
            "100",
            (a, b),
            (
                _observation(a, ExternalEffect.ACCEPTED, "40"),
                _observation(a, ExternalEffect.MARKET_REFUSED),
            ),
        )


def test_duplicate_observation_identity_is_rejected() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    with pytest.raises(RoutingContractError, match="duplicate observation_id"):
        _route(
            "100",
            (a, b),
            (
                _observation(
                    a,
                    ExternalEffect.ACCEPTED,
                    "40",
                    "ack-1",
                ),
                _observation(
                    a,
                    ExternalEffect.MARKET_REFUSED,
                    observation_id="ack-1",
                ),
            ),
        )


def test_unselected_observation_cannot_enlarge_authority() -> None:
    selected = _venue("book-a", "acct-a")
    unselected = _venue("book-b", "acct-b")
    with pytest.raises(RoutingContractError, match="unselected"):
        _route(
            "10",
            (selected,),
            (_observation(unselected, ExternalEffect.MARKET_REFUSED),),
        )


@pytest.mark.parametrize(
    ("effect", "accepted"),
    (
        (ExternalEffect.ACCEPTED, "10"),
        (ExternalEffect.MARKET_REFUSED, "0"),
        (ExternalEffect.UNKNOWN, "0"),
    ),
)
def test_cross_market_observation_cannot_change_this_request(
    effect: ExternalEffect,
    accepted: str,
) -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    other_market_quote = _quote("book-a", "away")
    cross_market = _observation(
        a,
        effect,
        accepted,
        quote=other_market_quote,
    )
    with pytest.raises(RoutingContractError, match="selected venue quote"):
        _route("100", (a, b), (cross_market,))


def test_cross_request_observation_cannot_change_this_request() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    other_request = _observation(
        a,
        ExternalEffect.ACCEPTED,
        "40",
        request_id="route-request-other",
    )
    with pytest.raises(RoutingContractError, match="routing_request_id"):
        _route("100", (a, b), (other_request,))


def test_quote_must_be_bound_to_venue_provider() -> None:
    with pytest.raises(RoutingContractError, match="source_id"):
        VenueQuote("book-b", "acct-b", _quote("book-a"), Decimal("10"))


def test_observation_quote_must_be_bound_to_observation_provider() -> None:
    with pytest.raises(RoutingContractError, match="source_id"):
        VenueObservation(
            "book-a",
            "acct-a",
            ExternalEffect.MARKET_REFUSED,
            _REQUEST_ID,
            _quote("book-b"),
        )


def test_partial_when_confirmed_stake_exists_but_no_selected_capacity_remains() -> None:
    a = _venue("book-a", "acct-a", "40")
    decision = _route(
        "100",
        (a,),
        (_observation(a, ExternalEffect.ACCEPTED, "40"),),
    )
    assert decision.state is RoutingState.PARTIAL
    assert decision.residual == Decimal("60")
    assert decision.proposed_stake == Decimal("0")


def test_complete_uses_only_externally_confirmed_accepted_stake() -> None:
    a = _venue("book-a", "acct-a")
    decision = _route(
        "40",
        (a,),
        (_observation(a, ExternalEffect.ACCEPTED, "40"),),
    )
    assert decision.state is RoutingState.COMPLETE
    assert decision.residual == Decimal("0")
    assert decision.proposed_stake == Decimal("0")


def test_confirmed_stake_cannot_exceed_venue_acceptance_ceiling() -> None:
    a = _venue("book-a", "acct-a", "30")
    with pytest.raises(RoutingContractError, match="acceptance ceiling"):
        _route(
            "100",
            (a,),
            (_observation(a, ExternalEffect.ACCEPTED, "40"),),
        )


def test_selected_quotes_must_share_market_selection_identity() -> None:
    a = _venue("book-a", "acct-a")
    b = VenueQuote(
        "book-b",
        "acct-b",
        _quote("book-b", "away"),
        Decimal("100"),
    )
    with pytest.raises(RoutingContractError, match="same market selection"):
        _route("10", (a, b))


def test_exact_accepted_sum_completes_without_ambient_context_rounding() -> None:
    a = _venue("book-a", "acct-a")
    b = _venue("book-b", "acct-b")
    requested = Decimal("1.0000000000000000000000000001")
    accepted_a = Decimal("0.50000000000000000000000000006")
    accepted_b = Decimal("0.50000000000000000000000000004")

    with localcontext() as context:
        context.prec = 28
        decision = route_residual(
            requested,
            (a, b),
            (
                VenueObservation(
                    a.venue_id,
                    a.account_id,
                    ExternalEffect.ACCEPTED,
                    _REQUEST_ID,
                    a.quote,
                    accepted_a,
                ),
                VenueObservation(
                    b.venue_id,
                    b.account_id,
                    ExternalEffect.ACCEPTED,
                    _REQUEST_ID,
                    b.quote,
                    accepted_b,
                ),
            ),
            routing_request_id=_REQUEST_ID,
        )

    assert decision.state is RoutingState.COMPLETE
    assert decision.confirmed_total == requested
    assert decision.residual == Decimal("0")


def test_exact_accepted_sum_rejects_sub_context_overacceptance() -> None:
    a = _venue("book-a", "acct-a")
    b = _venue("book-b", "acct-b")
    requested = Decimal("1.0000000000000000000000000001")
    accepted = Decimal("0.50000000000000000000000000006")

    with localcontext() as context:
        context.prec = 28
        with pytest.raises(
            RoutingContractError,
            match="confirmed accepted stake exceeds requested stake",
        ):
            route_residual(
                requested,
                (a, b),
                (
                    VenueObservation(
                        a.venue_id,
                        a.account_id,
                        ExternalEffect.ACCEPTED,
                        _REQUEST_ID,
                        a.quote,
                        accepted,
                    ),
                    VenueObservation(
                        b.venue_id,
                        b.account_id,
                        ExternalEffect.ACCEPTED,
                        _REQUEST_ID,
                        b.quote,
                        accepted,
                    ),
                ),
                routing_request_id=_REQUEST_ID,
            )


def test_exact_per_venue_sum_rejects_sub_context_ceiling_overrun() -> None:
    ceiling = "1.0000000000000000000000000001"
    a = _venue("book-a", "acct-a", ceiling)
    accepted = Decimal("0.50000000000000000000000000006")

    with localcontext() as context:
        context.prec = 28
        with pytest.raises(
            RoutingContractError,
            match="confirmed accepted stake exceeds venue acceptance ceiling",
        ):
            route_residual(
                Decimal("2"),
                (a,),
                (
                    VenueObservation(
                        a.venue_id,
                        a.account_id,
                        ExternalEffect.ACCEPTED,
                        _REQUEST_ID,
                        a.quote,
                        accepted,
                        "exact-ceiling-1",
                    ),
                    VenueObservation(
                        a.venue_id,
                        a.account_id,
                        ExternalEffect.ACCEPTED,
                        _REQUEST_ID,
                        a.quote,
                        accepted,
                        "exact-ceiling-2",
                    ),
                ),
                routing_request_id=_REQUEST_ID,
            )
