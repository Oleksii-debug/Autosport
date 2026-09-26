from decimal import Decimal

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror, MirrorUpdate


_TS = "2026-09-21T20:00:00+00:00"


def _event(*, exchange_side: str | None, sequence: int, sport: str | None = None) -> MarketEvent:
    return MarketEvent(
        event_id="matchbook:event-1",
        market_id="matchbook:market-1",
        selection_id="matchbook:runner-1",
        decimal_odds=Decimal("2.5"),
        observed_ts=_TS,
        source_id="matchbook",
        sequence=sequence,
        ingest_ts=_TS,
        sport=sport,
        exchange_side=exchange_side,
    )


def test_exact_get_can_address_back_and_lay_without_guessing() -> None:
    mirror = MarketMirror()
    back = _event(exchange_side="back", sequence=1)
    lay = _event(exchange_side="lay", sequence=2)

    assert mirror.apply(back).status is MirrorUpdate.APPLIED
    assert mirror.apply(lay).status is MirrorUpdate.APPLIED

    assert (
        mirror.get(
            "matchbook",
            back.event_id,
            back.market_id,
            back.selection_id,
            exchange_side="back",
        )
        == back
    )
    assert (
        mirror.get(
            "matchbook",
            lay.event_id,
            lay.market_id,
            lay.selection_id,
            exchange_side="lay",
        )
        == lay
    )

    # Legacy lookup must remain exact and must not guess one exchange side.
    assert (
        mirror.get(
            "matchbook",
            back.event_id,
            back.market_id,
            back.selection_id,
        )
        is None
    )


def test_exact_get_composes_sport_and_exchange_side_identity() -> None:
    mirror = MarketMirror()
    back = _event(exchange_side="back", sequence=3, sport="soccer")
    lay = _event(exchange_side="lay", sequence=4, sport="soccer")

    mirror.apply(back)
    mirror.apply(lay)

    assert (
        mirror.get(
            "matchbook",
            back.event_id,
            back.market_id,
            back.selection_id,
            sport="soccer",
            exchange_side="back",
        )
        == back
    )
    assert (
        mirror.get(
            "matchbook",
            lay.event_id,
            lay.market_id,
            lay.selection_id,
            sport="soccer",
            exchange_side="lay",
        )
        == lay
    )

    # Omitting either identity dimension must not alias a side-bearing quote.
    assert (
        mirror.get(
            "matchbook",
            back.event_id,
            back.market_id,
            back.selection_id,
            sport="soccer",
        )
        is None
    )
