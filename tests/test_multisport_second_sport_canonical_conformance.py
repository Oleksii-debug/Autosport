from __future__ import annotations

from decimal import Decimal

from autosport.domain import MarketEvent, PaperTicket, TicketLeg
from autosport.market_mirror import MarketMirror, MirrorUpdate
from autosport.portfolio import PortfolioEngine
from autosport.replay import ReplayEngine
from autosport.scenario_search import PortfolioDependencyIndex


T0 = "2026-09-22T12:00:00+00:00"


def _event(
    *,
    sport: str,
    selection_id: str = "same-selection",
    sequence: int = 1,
) -> MarketEvent:
    """Deterministic engineering fixture only; it is not external/provider evidence."""
    return MarketEvent(
        event_id="same-provider-event",
        market_id="same-provider-market",
        selection_id=selection_id,
        decimal_odds=Decimal("2.00"),
        observed_ts=T0,
        source_id="fixture:multisport",
        sequence=sequence,
        sport=sport,
        competition_id="fixture-competition",
        market_semantics_id="fixture-regulation-result",
        provider_source_class="fixture-only",
    )


def _football_legs() -> tuple[TicketLeg, ...]:
    return tuple(
        TicketLeg(
            event_id="football-fixture-event",
            market_id="football-fixture-regulation-result",
            selection_id=selection_id,
            locked_odds=Decimal(odds),
            sport="association_football",
        )
        for selection_id, odds in (
            ("home", "2.10"),
            ("draw", "3.20"),
            ("away", "3.40"),
        )
    )


def test_sport_identity_survives_canonical_market_event_roundtrip() -> None:
    table_tennis = _event(sport="table_tennis")
    football = _event(sport="association_football")

    assert table_tennis.quote_key != football.quote_key
    assert table_tennis.dedupe_key != football.dedupe_key

    restored_table_tennis = MarketEvent.from_dict(table_tennis.to_dict())
    restored_football = MarketEvent.from_dict(football.to_dict())

    assert restored_table_tennis == table_tennis
    assert restored_football == football
    assert restored_table_tennis.sport == "table_tennis"
    assert restored_football.sport == "association_football"
    assert restored_table_tennis.quote_key != restored_football.quote_key


def test_market_mirror_does_not_collapse_same_provider_local_ids_across_sports() -> None:
    mirror = MarketMirror()

    first = mirror.apply(_event(sport="table_tennis"))
    second = mirror.apply(_event(sport="association_football"))

    assert first.status is MirrorUpdate.APPLIED
    assert second.status is MirrorUpdate.APPLIED
    assert first.previous_sequence is None
    assert second.previous_sequence is None
    assert first.quote_key != second.quote_key


def test_replay_dataset_identity_and_callbacks_remain_sport_bound() -> None:
    table_tennis = _event(sport="table_tennis")
    football = _event(sport="association_football")

    assert (
        ReplayEngine([table_tennis]).dataset_hash
        != ReplayEngine([football]).dataset_hash
    )

    observed: list[tuple[str | None, str]] = []
    replay = ReplayEngine([table_tennis, football])
    run = replay.run(lambda event: observed.append((event.sport, event.quote_key)))

    assert run.event_count == 2
    assert {sport for sport, _quote_key in observed} == {
        "table_tennis",
        "association_football",
    }
    assert len({quote_key for _sport, quote_key in observed}) == 2


def test_three_way_second_sport_fixture_has_three_distinct_canonical_leg_identities() -> None:
    # Engineering conformance only: no provider settlement/economic claim.
    legs = _football_legs()

    assert len({leg.quote_key for leg in legs}) == 3
    assert all(leg.sport == "association_football" for leg in legs)

    table_tennis_alias_attempt = TicketLeg(
        event_id="football-fixture-event",
        market_id="football-fixture-regulation-result",
        selection_id="home",
        locked_odds=Decimal("2.10"),
        sport="table_tennis",
    )
    assert table_tennis_alias_attempt.quote_key != legs[0].quote_key


def test_three_way_second_sport_routes_through_canonical_portfolio_math() -> None:
    legs = _football_legs()
    tickets = [
        PaperTicket(
            ticket_id=f"football:{leg.selection_id}",
            stake=Decimal("10"),
            legs=(leg,),
            placed_at=T0,
        )
        for leg in legs
    ]

    dependency_index = PortfolioDependencyIndex(tickets)
    for ticket, leg in zip(tickets, legs):
        assert dependency_index.affected_by({leg.quote_key}) == {ticket.ticket_id}

    profits = {
        leg.selection_id: PortfolioEngine.scenario_profit(tickets, {leg.quote_key})
        for leg in legs
    }
    assert profits == {
        "home": Decimal("-9.00"),
        "draw": Decimal("2.00"),
        "away": Decimal("4.00"),
    }

    table_tennis_alias = TicketLeg(
        event_id=legs[0].event_id,
        market_id=legs[0].market_id,
        selection_id=legs[0].selection_id,
        locked_odds=legs[0].locked_odds,
        sport="table_tennis",
    )
    assert dependency_index.affected_by({table_tennis_alias.quote_key}) == set()
