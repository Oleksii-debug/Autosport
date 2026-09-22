from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport.betdaq_market_projection import (
    BetdaqMarketContext,
    BetdaqProjectionError,
    project_betdaq_get_prices,
)
from autosport.betdaq_readonly_market_wire import (
    BetdaqGetPricesWireResponse,
    BetdaqMarketPrices,
    BetdaqPriceLevel,
    BetdaqSelectionPrices,
    BetdaqUnavailableMarket,
)
from autosport.domain import MarketType
from autosport.providers import CanonicalNormalizer


OBSERVED_TS = "2026-09-23T00:00:01+00:00"


def _level(side: str, price: str, stake: str) -> BetdaqPriceLevel:
    return BetdaqPriceLevel(
        provider_side=side,
        price=Decimal(price),
        stake=Decimal(stake),
    )


def _selection(
    *,
    for_levels: tuple[BetdaqPriceLevel, ...] | None = None,
    against_levels: tuple[BetdaqPriceLevel, ...] | None = None,
    selection_id: int = 7001,
) -> BetdaqSelectionPrices:
    return BetdaqSelectionPrices(
        selection_id=selection_id,
        name="Home",
        status_code=2,
        reset_count=7,
        deduction_factor=Decimal("0.125"),
        for_side_prices=(
            (
                _level("FOR", "3.00", "20.50"),
                _level("FOR", "2.50", "40.25"),
            )
            if for_levels is None
            else for_levels
        ),
        against_side_prices=(
            (
                _level("AGAINST", "2.10", "30.75"),
                _level("AGAINST", "2.20", "50.00"),
            )
            if against_levels is None
            else against_levels
        ),
    )


def _market(
    *,
    selections: tuple[BetdaqSelectionPrices, ...] | None = None,
    market_id: int = 9001,
    status_code: int = 42,
) -> BetdaqMarketPrices:
    return BetdaqMarketPrices(
        market_id=market_id,
        name="Match Odds",
        market_type_code=1,
        status_code=status_code,
        start_time=datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc),
        start_time_text="2026-09-23T18:00:00+00:00",
        withdrawal_sequence_number=11,
        is_play_market=False,
        is_in_running_allowed=True,
        is_managed_when_in_running=True,
        is_currently_in_running=False,
        in_running_delay_seconds=5,
        selections=(_selection(),) if selections is None else selections,
    )


def _response(
    *,
    markets: tuple[BetdaqMarketPrices, ...] | None = None,
    unavailable: tuple[BetdaqUnavailableMarket, ...] = (),
    source_timestamp: bool = False,
) -> BetdaqGetPricesWireResponse:
    created = (
        datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
        if source_timestamp
        else None
    )
    created_text = "2026-09-23T00:00:00Z" if source_timestamp else None
    return BetdaqGetPricesWireResponse(
        return_code=0,
        return_description="Success",
        call_id="call-1",
        provider_created_at=created,
        provider_created_at_text=created_text,
        markets=(_market(),) if markets is None else markets,
        unavailable_markets=unavailable,
    )


def _context(
    market_id: int = 9001,
) -> dict[int, BetdaqMarketContext]:
    return {
        market_id: BetdaqMarketContext(
            provider_event_id="event-501",
            market_type=MarketType.WINNER,
            sport="football",
        )
    }


def test_projects_distinct_back_and_lay_without_mutating_native_selection_id() -> None:
    batch = project_betdaq_get_prices(
        response=_response(),
        market_context=_context(),
        observed_ts=OBSERVED_TS,
        sequence=101,
    )

    assert batch.source_id == "betdaq"
    assert len(batch.quotes) == 2

    back, lay = batch.quotes
    assert back.provider_event_id == "event-501"
    assert back.provider_market_id == "9001"
    assert back.provider_selection_id == "7001"
    assert lay.provider_selection_id == "7001"
    assert back.exchange_side == "back"
    assert lay.exchange_side == "lay"
    assert back.decimal_odds == Decimal("3.00")
    assert lay.decimal_odds == Decimal("2.10")

    normalizer = CanonicalNormalizer()
    back_event = normalizer.normalize(batch.source_id, back)
    lay_event = normalizer.normalize(batch.source_id, lay)
    assert back_event.quote_key != lay_event.quote_key
    assert back_event.selection_id == lay_event.selection_id


def test_projects_only_top_of_book_and_preserves_exact_bounded_depth_as_metadata() -> None:
    batch = project_betdaq_get_prices(
        response=_response(),
        market_context=_context(),
        observed_ts=OBSERVED_TS,
        sequence=102,
    )
    back, lay = batch.quotes

    assert back.metadata["betdaq_depth_projection"] == "top-of-book-only"
    assert back.metadata["betdaq_top_available_stake"] == "20.50"
    assert back.metadata["betdaq_available_depth"] == [
        {"price": "3.00", "stake": "20.50"},
        {"price": "2.50", "stake": "40.25"},
    ]
    assert lay.metadata["betdaq_available_depth"] == [
        {"price": "2.10", "stake": "30.75"},
        {"price": "2.20", "stake": "50.00"},
    ]
    assert all(
        not isinstance(item["stake"], Decimal)
        for quote in batch.quotes
        for item in quote.metadata["betdaq_available_depth"]
    )


def test_preserves_reset_withdrawal_in_running_and_unknown_status_without_open_guess() -> None:
    batch = project_betdaq_get_prices(
        response=_response(),
        market_context=_context(),
        observed_ts=OBSERVED_TS,
        sequence=103,
    )

    for quote in batch.quotes:
        assert quote.status == "betdaq-status:42"
        assert quote.status != "open"
        assert quote.metadata["betdaq_market_status_code"] == 42
        assert quote.metadata["betdaq_withdrawal_sequence_number"] == 11
        assert quote.metadata["betdaq_selection_status_code"] == 2
        assert quote.metadata["betdaq_selection_reset_count"] == 7
        assert quote.metadata["betdaq_selection_deduction_factor"] == "0.125"
        assert quote.metadata["betdaq_is_in_running_allowed"] is True
        assert quote.metadata["betdaq_is_currently_in_running"] is False
        assert quote.metadata["betdaq_in_running_delay_seconds"] == 5


def test_local_observation_time_never_fabricates_provider_source_time() -> None:
    batch = project_betdaq_get_prices(
        response=_response(source_timestamp=False),
        market_context=_context(),
        observed_ts=OBSERVED_TS,
        sequence=104,
    )

    assert all(quote.observed_ts == OBSERVED_TS for quote in batch.quotes)
    assert all(quote.source_ts is None for quote in batch.quotes)


def test_exact_provider_created_time_is_preserved_separately_when_present() -> None:
    batch = project_betdaq_get_prices(
        response=_response(source_timestamp=True),
        market_context=_context(),
        observed_ts=OBSERVED_TS,
        sequence=105,
    )

    assert all(
        quote.source_ts == "2026-09-23T00:00:00Z"
        for quote in batch.quotes
    )
    assert all(quote.observed_ts == OBSERVED_TS for quote in batch.quotes)


def test_partial_unavailable_scope_cannot_publish_successful_canonical_batch() -> None:
    response = _response(
        unavailable=(
            BetdaqUnavailableMarket(
                market_id=9002,
                return_code=16,
            ),
        )
    )

    with pytest.raises(
        BetdaqProjectionError,
        match="partial/unavailable",
    ):
        project_betdaq_get_prices(
            response=response,
            market_context=_context(),
            observed_ts=OBSERVED_TS,
            sequence=106,
        )


@pytest.mark.parametrize(
    "market_context",
    [
        {},
        {
            9001: BetdaqMarketContext(
                provider_event_id="event-501",
                market_type=MarketType.WINNER,
            ),
            9002: BetdaqMarketContext(
                provider_event_id="event-502",
                market_type=MarketType.WINNER,
            ),
        },
    ],
)
def test_context_must_exactly_cover_response_market_scope(
    market_context: dict[int, BetdaqMarketContext],
) -> None:
    with pytest.raises(BetdaqProjectionError, match="exactly cover"):
        project_betdaq_get_prices(
            response=_response(),
            market_context=market_context,
            observed_ts=OBSERVED_TS,
            sequence=107,
        )


def test_zero_top_liquidity_cannot_be_published_as_available_quote() -> None:
    selection = _selection(
        for_levels=(
            _level("FOR", "3.00", "0"),
            _level("FOR", "2.50", "40"),
        )
    )
    response = _response(markets=(_market(selections=(selection,)),))

    with pytest.raises(BetdaqProjectionError, match="zero available liquidity"):
        project_betdaq_get_prices(
            response=response,
            market_context=_context(),
            observed_ts=OBSERVED_TS,
            sequence=108,
        )


@pytest.mark.parametrize(
    ("provider_side", "levels"),
    [
        (
            "FOR",
            (
                _level("FOR", "2.00", "10"),
                _level("FOR", "2.50", "20"),
            ),
        ),
        (
            "AGAINST",
            (
                _level("AGAINST", "2.20", "10"),
                _level("AGAINST", "2.10", "20"),
            ),
        ),
    ],
)
def test_projection_rechecks_provider_competitiveness_order(
    provider_side: str,
    levels: tuple[BetdaqPriceLevel, ...],
) -> None:
    if provider_side == "FOR":
        selection = _selection(for_levels=levels)
    else:
        selection = _selection(against_levels=levels)
    response = _response(markets=(_market(selections=(selection,)),))

    with pytest.raises(BetdaqProjectionError, match="competitiveness order"):
        project_betdaq_get_prices(
            response=response,
            market_context=_context(),
            observed_ts=OBSERVED_TS,
            sequence=109,
        )


def test_noncanonical_odds_are_rejected_before_provider_batch_publication() -> None:
    selection = _selection(
        for_levels=(_level("FOR", "1.00", "10"),),
        against_levels=(),
    )

    with pytest.raises(BetdaqProjectionError, match="greater than one"):
        project_betdaq_get_prices(
            response=_response(
                markets=(_market(selections=(selection,)),)
            ),
            market_context=_context(),
            observed_ts=OBSERVED_TS,
            sequence=110,
        )


@pytest.mark.parametrize(
    "observed_ts",
    [
        "2026-09-23T00:00:01",
        "not-a-time",
        " 2026-09-23T00:00:01+00:00",
    ],
)
def test_observed_time_must_be_explicit_timezone_aware_causal_time(
    observed_ts: str,
) -> None:
    with pytest.raises(BetdaqProjectionError):
        project_betdaq_get_prices(
            response=_response(),
            market_context=_context(),
            observed_ts=observed_ts,
            sequence=111,
        )


@pytest.mark.parametrize("sequence", [True, 1 << 63, -(1 << 63) - 1])
def test_sequence_must_round_trip_through_canonical_sqlite_identity(
    sequence: object,
) -> None:
    with pytest.raises(BetdaqProjectionError):
        project_betdaq_get_prices(
            response=_response(),
            market_context=_context(),
            observed_ts=OBSERVED_TS,
            sequence=sequence,  # type: ignore[arg-type]
        )


def test_empty_provider_side_does_not_fabricate_quote_or_liquidity() -> None:
    selection = _selection(against_levels=())
    batch = project_betdaq_get_prices(
        response=_response(
            markets=(_market(selections=(selection,)),)
        ),
        market_context=_context(),
        observed_ts=OBSERVED_TS,
        sequence=112,
    )

    assert len(batch.quotes) == 1
    assert batch.quotes[0].exchange_side == "back"


def test_cursor_is_only_caller_supplied_opaque_evidence_and_not_synthesized() -> None:
    without_cursor = project_betdaq_get_prices(
        response=_response(),
        market_context=_context(),
        observed_ts=OBSERVED_TS,
        sequence=113,
    )
    with_cursor = project_betdaq_get_prices(
        response=_response(),
        market_context=_context(),
        observed_ts=OBSERVED_TS,
        sequence=113,
        cursor="selection-frontier:88",
    )

    assert without_cursor.cursor is None
    assert with_cursor.cursor == "selection-frontier:88"
