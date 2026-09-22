from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from autosport.domain import MarketType
from autosport.providers import CanonicalNormalizer, ProviderUnavailableError
from autosport.the_odds_api_reference import (
    TheOddsApiHttpJsonResponse,
    TheOddsApiPayloadError,
    TheOddsApiReferenceProvider,
    TheOddsApiTransportError,
    _decode_provider_json,
)


ACQUIRED = "2026-09-22T02:00:00Z"
BODY_SHA = "a" * 64
SECRET = "super-secret-api-key"


def event_payload(*, sport_key: str = "basketball_nba") -> list[dict]:
    return [
        {
            "id": "event-123",
            "sport_key": sport_key,
            "sport_title": "NBA",
            "commence_time": "2026-09-22T03:00:00Z",
            "home_team": "Home Team",
            "away_team": "Away Team",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "title": "DraftKings",
                    "last_update": "2026-09-22T01:58:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "last_update": "2026-09-22T01:58:30Z",
                            "outcomes": [
                                {"name": "Home Team", "price": Decimal("1.90")},
                                {"name": "Away Team", "price": Decimal("2.05")},
                            ],
                        },
                        {
                            "key": "spreads",
                            "last_update": "2026-09-22T01:58:40Z",
                            "outcomes": [
                                {
                                    "name": "Home Team",
                                    "price": Decimal("1.95"),
                                    "point": Decimal("-2.5"),
                                },
                                {
                                    "name": "Away Team",
                                    "price": Decimal("1.91"),
                                    "point": Decimal("2.5"),
                                },
                            ],
                        },
                    ],
                }
            ],
        }
    ]


def response(payload=None, *, headers=None) -> TheOddsApiHttpJsonResponse:
    return TheOddsApiHttpJsonResponse(
        event_payload() if payload is None else payload,
        200,
        headers or {},
        BODY_SHA,
    )


def current_provider(transport, **kwargs) -> TheOddsApiReferenceProvider:
    return TheOddsApiReferenceProvider(
        SECRET,
        sport_key="basketball_nba",
        markets=("h2h", "spreads"),
        bookmakers=("draftkings",),
        transport=transport,
        clock=lambda: ACQUIRED,
        **kwargs,
    )


def test_current_request_keeps_api_key_out_of_path_and_parameter_evidence() -> None:
    calls = []

    def transport(path, params, api_key, timeout):
        calls.append((path, dict(params), api_key, timeout))
        return response()

    batch = current_provider(transport).read_batch()
    assert len(batch.quotes) == 4
    path, params, api_key, timeout = calls[0]
    assert path == "/v4/sports/basketball_nba/odds"
    assert params == {
        "markets": "h2h,spreads",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
        "bookmakers": "draftkings",
    }
    assert SECRET not in path
    assert SECRET not in repr(params)
    assert api_key == SECRET
    assert timeout == 10.0


def test_reference_quotes_preserve_acquisition_and_provider_publish_times() -> None:
    batch = current_provider(lambda *_: response()).read_batch()
    h2h, _, spread, _ = batch.quotes

    assert batch.source_id == "the-odds-api:basketball_nba:current"
    assert h2h.provider_event_id == "event-123"
    assert h2h.provider_market_id == "draftkings:h2h"
    assert h2h.provider_selection_id == "Home Team"
    assert h2h.decimal_odds == Decimal("1.90")
    assert h2h.observed_ts == ACQUIRED
    assert h2h.source_ts == "2026-09-22T01:58:30Z"
    assert h2h.status == "reference"
    assert h2h.market_type is MarketType.WINNER
    assert h2h.sport == "basketball_nba"
    assert spread.provider_market_id == "draftkings:spreads"
    assert spread.provider_selection_id == "Home Team@point=-2.5"
    assert spread.market_type is MarketType.HANDICAP
    assert spread.metadata["point"] == "-2.5"


def test_reference_truth_never_upgrades_to_execution_fair_or_accepted_price() -> None:
    batch = current_provider(lambda *_: response()).read_batch()
    assert "REFERENCE_ONLY" in batch.quality_flags
    assert "NON_EXECUTABLE_REFERENCE" in batch.quality_flags
    for quote in batch.quotes:
        assert quote.metadata["reference_only"] is True
        assert quote.metadata["execution_authorized"] is False
        assert quote.metadata["accepted_price_authorized"] is False
        assert quote.metadata["fair_value_authorized"] is False
        assert quote.metadata["raw_redistribution_authorized"] is False
        assert quote.metadata["terms_version"] == "2026-08-31"


def test_historical_snapshot_is_at_or_before_requested_but_acquisition_stays_later() -> None:
    historical_payload = {
        "timestamp": "2026-09-22T00:55:00Z",
        "previous_timestamp": "2026-09-22T00:50:00Z",
        "next_timestamp": "2026-09-22T01:00:00Z",
        "data": event_payload(),
    }

    calls = []

    def transport(path, params, api_key, timeout):
        calls.append((path, dict(params), api_key, timeout))
        return response(historical_payload)

    provider = TheOddsApiReferenceProvider(
        SECRET,
        sport_key="basketball_nba",
        markets=("h2h", "spreads"),
        bookmakers=("draftkings",),
        historical_at="2026-09-22T01:00:00Z",
        transport=transport,
        clock=lambda: ACQUIRED,
    )
    batch = provider.read_batch()
    quote = batch.quotes[0]

    assert calls[0][0] == "/v4/historical/sports/basketball_nba/odds"
    assert calls[0][1]["date"] == "2026-09-22T01:00:00Z"
    assert quote.observed_ts == ACQUIRED
    assert quote.metadata["historical_requested_at"] == "2026-09-22T01:00:00Z"
    assert quote.metadata["historical_returned_at"] == "2026-09-22T00:55:00Z"
    assert quote.metadata["historical_previous_at"] == "2026-09-22T00:50:00Z"
    assert quote.metadata["historical_next_at"] == "2026-09-22T01:00:00Z"
    assert "HISTORICAL_SNAPSHOT" in batch.quality_flags


def test_historical_snapshot_after_requested_cutoff_fails_closed() -> None:
    historical_payload = {
        "timestamp": "2026-09-22T01:00:01Z",
        "previous_timestamp": "2026-09-22T00:55:00Z",
        "next_timestamp": "2026-09-22T01:05:00Z",
        "data": event_payload(),
    }
    provider = TheOddsApiReferenceProvider(
        SECRET,
        sport_key="basketball_nba",
        markets=("h2h",),
        bookmakers=("draftkings",),
        historical_at="2026-09-22T01:00:00Z",
        transport=lambda *_: response(historical_payload),
        clock=lambda: ACQUIRED,
    )

    with pytest.raises(TheOddsApiPayloadError, match="after requested cutoff"):
        provider.read_batch()


def test_next_timestamp_is_navigation_metadata_not_acquisition_time() -> None:
    historical_payload = {
        "timestamp": "2026-09-22T00:55:00Z",
        "previous_timestamp": "2026-09-22T00:50:00Z",
        "next_timestamp": "2026-09-22T01:05:00Z",
        "data": event_payload(),
    }
    provider = TheOddsApiReferenceProvider(
        SECRET,
        sport_key="basketball_nba",
        markets=("h2h",),
        bookmakers=("draftkings",),
        historical_at="2026-09-22T01:00:00Z",
        transport=lambda *_: response(historical_payload),
        clock=lambda: ACQUIRED,
    )
    quote = provider.read_batch().quotes[0]
    assert quote.observed_ts == ACQUIRED
    assert quote.source_ts == "2026-09-22T01:58:30Z"
    assert quote.metadata["historical_next_at"] == "2026-09-22T01:05:00Z"


def test_two_sport_keys_remain_distinct_reference_sources() -> None:
    basketball = current_provider(lambda *_: response()).read_batch()

    soccer_payload = event_payload(sport_key="soccer_epl")
    soccer = TheOddsApiReferenceProvider(
        SECRET,
        sport_key="soccer_epl",
        markets=("h2h",),
        regions=("uk",),
        transport=lambda *_: response(soccer_payload),
        clock=lambda: ACQUIRED,
    ).read_batch()

    assert basketball.source_id == "the-odds-api:basketball_nba:current"
    assert soccer.source_id == "the-odds-api:soccer_epl:current"
    assert basketball.source_id != soccer.source_id
    assert soccer.quotes[0].sport == "soccer_epl"


def test_cross_sport_payload_cannot_relabel_configured_source() -> None:
    payload = event_payload(sport_key="soccer_epl")
    with pytest.raises(TheOddsApiPayloadError, match="configured sport"):
        current_provider(lambda *_: response(payload)).read_batch()


def test_bookmaker_market_outcome_duplicate_identity_fails_closed() -> None:
    payload = event_payload()
    duplicate = deepcopy(
        payload[0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    )
    payload[0]["bookmakers"][0]["markets"][0]["outcomes"].append(duplicate)
    with pytest.raises(TheOddsApiPayloadError, match="duplicate"):
        current_provider(lambda *_: response(payload)).read_batch()


def test_float_wire_price_fails_closed_instead_of_losing_decimal_identity() -> None:
    payload = event_payload()
    payload[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 1.9
    with pytest.raises(TheOddsApiPayloadError, match="exact JSON"):
        current_provider(lambda *_: response(payload)).read_batch()


def test_json_decoder_preserves_decimal_and_rejects_duplicate_nonfinite_values() -> None:
    assert _decode_provider_json(b'{"price":1.90,"n":2}') == {
        "price": Decimal("1.90"),
        "n": 2,
    }
    with pytest.raises(TheOddsApiPayloadError, match="duplicate JSON key"):
        _decode_provider_json(b'{"x":1,"x":2}')
    with pytest.raises(TheOddsApiPayloadError, match="non-standard JSON constant"):
        _decode_provider_json(b'{"x":NaN}')


def test_quota_response_digest_and_request_scope_are_auditable_without_secret() -> None:
    batch = current_provider(
        lambda *_: response(
            headers={
                "X-Requests-Remaining": "499",
                "X-Requests-Used": "1",
                "X-Requests-Last": "1",
            }
        )
    ).read_batch()
    metadata = batch.quotes[0].metadata
    assert metadata["response_sha256"] == BODY_SHA
    assert metadata["x-requests-remaining"] == "499"
    assert metadata["x-requests-used"] == "1"
    assert metadata["x-requests-last"] == "1"
    assert metadata["endpoint"] == "/v4/sports/basketball_nba/odds"
    assert metadata["requested_markets"] == ["h2h", "spreads"]
    assert metadata["requested_bookmakers"] == ["draftkings"]
    assert SECRET not in repr(metadata)


def test_missing_requested_market_or_bookmaker_is_partial_not_synthesized_zero() -> None:
    payload = event_payload()
    payload[0]["bookmakers"][0]["markets"] = [
        payload[0]["bookmakers"][0]["markets"][0]
    ]
    provider = TheOddsApiReferenceProvider(
        SECRET,
        sport_key="basketball_nba",
        markets=("h2h", "spreads"),
        bookmakers=("draftkings", "fanduel"),
        transport=lambda *_: response(payload),
        clock=lambda: ACQUIRED,
    )
    batch = provider.read_batch()
    assert len(batch.quotes) == 2
    assert "PARTIAL_REFERENCE_SCOPE" in batch.quality_flags
    assert all(quote.decimal_odds > 1 for quote in batch.quotes)


def test_empty_response_is_explicit_reference_coverage_not_numeric_zero() -> None:
    batch = current_provider(lambda *_: response([])).read_batch()
    assert batch.quotes == ()
    assert "EMPTY_REFERENCE_SCOPE" in batch.quality_flags
    assert "REFERENCE_ONLY" in batch.quality_flags


def test_materialized_snapshot_chunking_does_not_refetch_mid_snapshot() -> None:
    calls = 0

    def transport(*_):
        nonlocal calls
        calls += 1
        return response()

    provider = current_provider(transport)
    first = provider.read_batch(max_items=1)
    second = provider.read_batch(max_items=3)
    assert calls == 1
    assert len(first.quotes) == 1
    assert len(second.quotes) == 3
    assert first.cursor == second.cursor == BODY_SHA
    assert "TRUNCATED_BATCH" in first.quality_flags
    assert "TRUNCATED_BATCH" not in second.quality_flags


def test_secret_is_absent_from_repr_metadata_and_typed_http_error() -> None:
    provider = current_provider(
        lambda *_: TheOddsApiHttpJsonResponse({}, 401, {})
    )
    assert SECRET not in repr(provider)
    with pytest.raises(TheOddsApiTransportError) as excinfo:
        provider.read_batch()
    assert isinstance(excinfo.value, ProviderUnavailableError)
    assert SECRET not in str(excinfo.value)

    batch = current_provider(lambda *_: response()).read_batch()
    assert all(SECRET not in repr(quote.metadata) for quote in batch.quotes)


def test_generic_normalizer_preserves_reference_identity_without_execution_upgrade() -> None:
    quote = current_provider(lambda *_: response()).read_batch().quotes[0]
    normalized = CanonicalNormalizer().normalize(
        "the-odds-api:basketball_nba:current",
        quote,
    )
    assert normalized.status == "reference"
    assert normalized.source_ts == "2026-09-22T01:58:30Z"
    assert normalized.metadata["reference_only"] is True
    assert normalized.metadata["execution_authorized"] is False


def test_constructor_requires_one_explicit_provider_scope_and_canonical_components() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        TheOddsApiReferenceProvider(
            SECRET,
            sport_key="basketball_nba",
            markets=("h2h",),
        )
    with pytest.raises(ValueError, match="exactly one"):
        TheOddsApiReferenceProvider(
            SECRET,
            sport_key="basketball_nba",
            markets=("h2h",),
            regions=("us",),
            bookmakers=("draftkings",),
        )
    with pytest.raises(ValueError, match="sport_key"):
        TheOddsApiReferenceProvider(
            SECRET,
            sport_key="Basketball_NBA",
            markets=("h2h",),
            regions=("us",),
        )


def test_only_public_provider_operation_is_read_batch() -> None:
    public = {
        name
        for name in dir(TheOddsApiReferenceProvider)
        if not name.startswith("_")
    }
    assert public == {"read_batch"}
