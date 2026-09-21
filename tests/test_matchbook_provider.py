from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from autosport.matchbook_provider import (
    MatchbookHttpJsonResponse,
    MatchbookPayloadError,
    MatchbookReadOnlyProvider,
    MatchbookTransportError,
    _decode_provider_json,
)
from autosport.providers import CanonicalNormalizer, ProviderUnavailableError


OBSERVED = "2026-09-21T20:00:00+00:00"
BODY_SHA = "a" * 64


def sample_payload() -> dict:
    return {
        "events": [
            {
                "id": 101,
                "status": "open",
                "markets": [
                    {
                        "event-id": 101,
                        "id": 202,
                        "status": "open",
                        "market-type": "money_line",
                        "runners": [
                            {
                                "event-id": 101,
                                "market-id": 202,
                                "id": 303,
                                "status": "open",
                                "prices": [
                                    {
                                        "side": "back",
                                        "exchange-type": "back-lay",
                                        "odds-type": "DECIMAL",
                                        "decimal-odds": Decimal("2.10"),
                                        "available-amount": Decimal("12.340"),
                                        "currency": "EUR",
                                    },
                                    {
                                        "side": "lay",
                                        "exchange-type": "back-lay",
                                        "odds-type": "DECIMAL",
                                        "decimal-odds": Decimal("2.12"),
                                        "available-amount": Decimal("8.50"),
                                        "currency": "EUR",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def provider(transport, **kwargs):
    return MatchbookReadOnlyProvider(
        "secret-session-token",
        sport_key="soccer",
        sport_ids=(15,),
        transport=transport,
        clock=lambda: OBSERVED,
        **kwargs,
    )


def response(payload=None):
    return MatchbookHttpJsonResponse(
        sample_payload() if payload is None else payload,
        200,
        {},
        BODY_SHA,
    )


def test_transport_error_is_typed_provider_unavailability():
    error = MatchbookTransportError("Matchbook unavailable", 503)
    assert isinstance(error, ProviderUnavailableError)


def test_authenticated_get_scope_never_places_session_token_in_url():
    calls = []

    def transport(url, headers, timeout):
        calls.append((url, dict(headers), timeout))
        return response()

    batch = provider(transport).read_batch()
    assert len(batch.quotes) == 2
    url, headers, timeout = calls[0]
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "api.matchbook.com"
    assert parsed.path == "/edge/rest/events"
    assert "secret-session-token" not in url
    assert headers["session-token"] == "secret-session-token"
    assert query["exchange-type"] == ["back-lay"]
    assert query["odds-type"] == ["DECIMAL"]
    assert query["include-prices"] == ["true"]
    assert query["price-depth"] == ["1"]
    assert query["price-mode"] == ["expanded"]
    assert query["sport-ids"] == ["15"]
    assert timeout == 10.0


def test_native_ids_back_lay_decimal_liquidity_and_no_fake_source_timestamp():
    batch = provider(lambda *_: response()).read_batch()
    back, lay = batch.quotes
    assert batch.source_id == "matchbook:soccer:expanded"
    assert (back.provider_event_id, back.provider_market_id, back.provider_selection_id) == (
        "101",
        "202",
        "303",
    )
    assert back.exchange_side == "back"
    assert lay.exchange_side == "lay"
    assert back.decimal_odds == Decimal("2.10")
    assert lay.decimal_odds == Decimal("2.12")
    assert back.metadata["available_amount"] == "12.340"
    assert lay.metadata["available_amount"] == "8.50"
    assert back.source_ts is None
    assert lay.source_ts is None
    assert back.metadata["response_sha256"] == BODY_SHA
    assert back.metadata["page_scope_complete"] is False
    assert back.metadata["depth_level"] == 0


def test_generic_normalizer_can_keep_back_and_lay_distinct_after_prerequisite():
    batch = provider(lambda *_: response()).read_batch()
    normalizer = CanonicalNormalizer()
    back = normalizer.normalize(batch.source_id, batch.quotes[0])
    lay = normalizer.normalize(batch.source_id, batch.quotes[1])
    assert back.quote_key != lay.quote_key


def test_float_wire_numbers_fail_closed_and_do_not_leave_partial_snapshot():
    payload = sample_payload()
    payload["events"][0]["markets"][0]["runners"][0]["prices"][1]["decimal-odds"] = 2.12
    calls = 0

    def transport(*_):
        nonlocal calls
        calls += 1
        return response(payload)

    client = provider(transport)
    with pytest.raises(MatchbookPayloadError, match="exact JSON integer/decimal"):
        client.read_batch(max_items=1)
    with pytest.raises(MatchbookPayloadError):
        client.read_batch(max_items=1)
    assert calls == 2


def test_duplicate_native_side_identity_fails_closed():
    payload = sample_payload()
    duplicate = deepcopy(payload["events"][0]["markets"][0]["runners"][0]["prices"][0])
    duplicate["decimal-odds"] = Decimal("2.11")
    payload["events"][0]["markets"][0]["runners"][0]["prices"].append(duplicate)
    with pytest.raises(MatchbookPayloadError, match="duplicate native"):
        provider(lambda *_: response(payload)).read_batch()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("events", 0, "id"), "101"),
        (("events", 0, "id"), True),
        (("events", 0, "markets", 0, "id"), Decimal("202")),
        (("events", 0, "markets", 0, "runners", 0, "id"), "303"),
    ],
)
def test_wrong_typed_native_ids_fail_closed(path, value):
    payload = sample_payload()
    cursor = payload
    for item in path[:-1]:
        cursor = cursor[item]
    cursor[path[-1]] = value
    with pytest.raises(MatchbookPayloadError, match="provider integer"):
        provider(lambda *_: response(payload)).read_batch()


def test_parent_identity_mismatch_fails_closed():
    payload = sample_payload()
    payload["events"][0]["markets"][0]["runners"][0]["market-id"] = 999
    with pytest.raises(MatchbookPayloadError, match="parent identity"):
        provider(lambda *_: response(payload)).read_batch()


def test_suspension_is_preserved_and_unknown_status_rejected():
    payload = sample_payload()
    payload["events"][0]["markets"][0]["status"] = "suspended"
    batch = provider(lambda *_: response(payload)).read_batch()
    assert {quote.status for quote in batch.quotes} == {"suspended"}
    assert batch.quotes[0].metadata["market_status"] == "suspended"

    bad = sample_payload()
    bad["events"][0]["status"] = "mystery"
    with pytest.raises(MatchbookPayloadError, match="unsupported state"):
        provider(lambda *_: response(bad)).read_batch()


def test_expanded_and_aggregated_are_distinct_provider_series():
    expanded = provider(lambda *_: response(), price_mode="expanded").read_batch()
    aggregated = provider(lambda *_: response(), price_mode="aggregated").read_batch()
    assert expanded.source_id == "matchbook:soccer:expanded"
    assert aggregated.source_id == "matchbook:soccer:aggregated"
    assert expanded.source_id != aggregated.source_id
    assert expanded.quotes[0].metadata["price_mode"] == "expanded"
    assert aggregated.quotes[0].metadata["price_mode"] == "aggregated"
    assert "MATCHBOOK_AGGREGATED_PRICE_MODE" in aggregated.quality_flags


def test_rate_limit_retry_is_bounded_and_capped():
    attempts = 0
    sleeps = []

    def transport(*_):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise MatchbookTransportError("Matchbook HTTP 429", 429, 30.0)
        return response()

    client = provider(
        transport,
        max_attempts=2,
        max_backoff_seconds=0.5,
        sleeper=sleeps.append,
    )
    client.read_batch()
    assert attempts == 2
    assert sleeps == [0.5]


def test_auth_failure_is_not_retried():
    attempts = 0

    def transport(*_):
        nonlocal attempts
        attempts += 1
        raise MatchbookTransportError("Matchbook HTTP 401", 401)

    client = provider(transport, max_attempts=5, sleeper=lambda _: pytest.fail("must not sleep"))
    with pytest.raises(MatchbookTransportError):
        client.read_batch()
    assert attempts == 1


def test_batch_chunking_reuses_one_materialized_snapshot_and_marks_scope():
    calls = 0

    def transport(*_):
        nonlocal calls
        calls += 1
        return response()

    client = provider(transport)
    first = client.read_batch(max_items=1)
    second = client.read_batch(max_items=1)
    assert calls == 1
    assert len(first.quotes) == len(second.quotes) == 1
    assert first.cursor == second.cursor == BODY_SHA
    assert "MATCHBOOK_PAGE_SCOPE_ONLY" in first.quality_flags
    assert "TRUNCATED_BATCH" in first.quality_flags
    assert "TRUNCATED_BATCH" not in second.quality_flags


def test_secret_absent_from_repr_metadata_and_transport_error():
    def transport(*_):
        raise MatchbookTransportError("Matchbook HTTP 403", 403)

    client = provider(transport)
    assert "secret-session-token" not in repr(client)
    with pytest.raises(MatchbookTransportError) as excinfo:
        client.read_batch()
    assert "secret-session-token" not in str(excinfo.value)

    batch = provider(lambda *_: response()).read_batch()
    assert all("secret-session-token" not in repr(q.metadata) for q in batch.quotes)


def test_constructor_fences_top_of_book_filters_and_exact_liquidity_type():
    with pytest.raises(ValueError, match="price_depth must be 1"):
        provider(lambda *_: response(), price_depth=2)
    with pytest.raises(ValueError, match="filter is required"):
        MatchbookReadOnlyProvider("token", sport_key="soccer", transport=lambda *_: response())
    with pytest.raises(TypeError, match="minimum_liquidity must be Decimal"):
        provider(lambda *_: response(), minimum_liquidity=0.0)
    with pytest.raises(ValueError, match="session_token"):
        MatchbookReadOnlyProvider(" token ", sport_key="soccer", sport_ids=(15,))


def test_json_decoder_preserves_decimal_exactness_and_rejects_duplicates_nonfinite():
    payload = _decode_provider_json(b'{"x":2.10,"n":2}')
    assert payload == {"x": Decimal("2.10"), "n": 2}
    assert type(payload["n"]) is int
    with pytest.raises(MatchbookPayloadError, match="duplicate JSON key"):
        _decode_provider_json(b'{"x":1,"x":2}')
    with pytest.raises(MatchbookPayloadError, match="non-standard JSON constant"):
        _decode_provider_json(b'{"x":NaN}')


def test_malformed_later_quote_never_returns_earlier_partial_snapshot():
    payload = sample_payload()
    payload["events"][0]["markets"][0]["runners"][0]["prices"][1]["side"] = "buy"
    client = provider(lambda *_: response(payload))
    with pytest.raises(MatchbookPayloadError, match="price.side"):
        client.read_batch(max_items=1)


def test_noncanonical_case_is_rejected_instead_of_silently_normalized():
    payload = sample_payload()
    payload["events"][0]["status"] = "OPEN"
    with pytest.raises(MatchbookPayloadError, match="unsupported state"):
        provider(lambda *_: response(payload)).read_batch()

    payload = sample_payload()
    payload["events"][0]["markets"][0]["runners"][0]["prices"][0]["side"] = "BACK"
    with pytest.raises(MatchbookPayloadError, match="price.side"):
        provider(lambda *_: response(payload)).read_batch()


def test_transport_returning_non_success_status_is_typed_and_retryable_only_when_allowed():
    attempts = 0

    def transport(*_):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return MatchbookHttpJsonResponse({}, 503, {})
        return response()

    batch = provider(transport, max_attempts=2, sleeper=lambda _: None).read_batch()
    assert len(batch.quotes) == 2
    assert attempts == 2


def test_no_betting_write_method_exists():
    public = {name for name in dir(MatchbookReadOnlyProvider) if not name.startswith("_")}
    assert public == {"read_batch"}
