from __future__ import annotations

import hashlib
import inspect
import json
from decimal import Decimal
from urllib.parse import parse_qs, parse_qsl, urlparse

import pytest

from autosport.matchbook_provider import (
    MatchbookHttpJsonResponse,
    MatchbookPayloadError,
    MatchbookReadOnlyProvider,
)


OBSERVED = "2026-09-22T00:30:00+00:00"
BODY_SHA = "b" * 64


def _payload(*, currency: str = "EUR") -> dict:
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
                                        "available-amount": Decimal("12.34"),
                                        "currency": currency,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _response(*, currency: str = "EUR") -> MatchbookHttpJsonResponse:
    return MatchbookHttpJsonResponse(
        payload=_payload(currency=currency),
        status_code=200,
        headers={},
        body_sha256=BODY_SHA,
    )


def _client(
    transport,
    *,
    currency: str = "EUR",
    minimum_liquidity: Decimal = Decimal("0"),
) -> MatchbookReadOnlyProvider:
    sequence = 0

    def allocate_sequence(_source_id: str) -> int:
        nonlocal sequence
        sequence += 1
        return sequence

    return MatchbookReadOnlyProvider(
        "test-session-token",
        sport_key="soccer",
        sport_ids=(15,),
        currency=currency,
        minimum_liquidity=minimum_liquidity,
        transport=transport,
        clock=lambda: OBSERVED,
        sequence_allocator=allocate_sequence,
    )


def test_market_view_requires_explicit_currency_contract() -> None:
    parameter = inspect.signature(MatchbookReadOnlyProvider).parameters.get("currency")
    assert parameter is not None, (
        "Matchbook market reads must expose an explicit currency input; "
        "provider regional defaults cannot own liquidity units"
    )
    assert parameter.default is inspect.Parameter.empty, (
        "currency must be explicit rather than silently defaulting to a region-dependent value"
    )


def test_outbound_market_view_and_quote_provenance_bind_currency_and_liquidity() -> None:
    calls: list[str] = []

    def transport(url, headers, timeout):
        calls.append(url)
        return _response(currency="EUR")

    batch = _client(
        transport,
        currency="EUR",
        minimum_liquidity=Decimal("0.50"),
    ).read_batch()

    assert len(calls) == 1
    query = parse_qs(urlparse(calls[0]).query)
    assert query["currency"] == ["EUR"]
    assert query["minimum-liquidity"] == ["0.50"]
    assert query["exchange-type"] == ["back-lay"]
    assert query["odds-type"] == ["DECIMAL"]
    assert query["price-depth"] == ["1"]
    assert query["price-mode"] == ["expanded"]

    quote = batch.quotes[0]
    assert quote.metadata["currency"] == "EUR"
    assert quote.metadata["requested_currency"] == "EUR"
    assert quote.metadata["minimum_liquidity"] == "0.50"
    assert quote.metadata["requested_price_depth"] == 1
    assert quote.metadata["exchange_type"] == "back-lay"
    assert quote.metadata["odds_type"] == "DECIMAL"
    assert quote.metadata["side_filter"] == "both"
    assert quote.metadata["exclude_mirrored_prices"] is False
    assert quote.metadata["market_view_sha256"] == batch.source_id.rsplit(":", 1)[1]
    outbound_query = parse_qsl(urlparse(calls[0]).query, keep_blank_values=True)
    query_bytes = json.dumps(
        outbound_query,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    assert quote.metadata["request_query_sha256"] == hashlib.sha256(query_bytes).hexdigest()


def test_provider_currency_mismatch_fails_closed_before_quote_publication() -> None:
    client = _client(
        lambda *_: _response(currency="GBP"),
        currency="EUR",
        minimum_liquidity=Decimal("0"),
    )

    with pytest.raises(MatchbookPayloadError, match="currency"):
        client.read_batch()


def test_distinct_liquidity_views_retain_distinct_provenance() -> None:
    low = _client(
        lambda *_: _response(currency="EUR"),
        currency="EUR",
        minimum_liquidity=Decimal("0"),
    ).read_batch()
    filtered = _client(
        lambda *_: _response(currency="EUR"),
        currency="EUR",
        minimum_liquidity=Decimal("10"),
    ).read_batch()

    low_meta = low.quotes[0].metadata
    filtered_meta = filtered.quotes[0].metadata
    assert low_meta["minimum_liquidity"] == "0"
    assert filtered_meta["minimum_liquidity"] == "10"
    assert low_meta["minimum_liquidity"] != filtered_meta["minimum_liquidity"]
    assert low.source_id != filtered.source_id
    assert low_meta["market_view_sha256"] != filtered_meta["market_view_sha256"]


def test_equivalent_decimal_spellings_share_semantic_view_identity() -> None:
    first = _client(
        lambda *_: _response(currency="EUR"),
        currency="EUR",
        minimum_liquidity=Decimal("0.50"),
    ).read_batch()
    second = _client(
        lambda *_: _response(currency="EUR"),
        currency="EUR",
        minimum_liquidity=Decimal("0.5"),
    ).read_batch()

    assert first.source_id == second.source_id
    assert first.quotes[0].metadata["market_view_sha256"] == (
        second.quotes[0].metadata["market_view_sha256"]
    )
    assert first.quotes[0].metadata["minimum_liquidity"] == "0.50"
    assert second.quotes[0].metadata["minimum_liquidity"] == "0.5"
    assert first.quotes[0].metadata["request_query_sha256"] != (
        second.quotes[0].metadata["request_query_sha256"]
    )


def test_currency_changes_market_view_identity() -> None:
    eur = _client(
        lambda *_: _response(currency="EUR"),
        currency="EUR",
    ).read_batch()
    gbp = _client(
        lambda *_: _response(currency="GBP"),
        currency="GBP",
    ).read_batch()

    assert eur.source_id != gbp.source_id
    assert eur.quotes[0].metadata["market_view_sha256"] != (
        gbp.quotes[0].metadata["market_view_sha256"]
    )


@pytest.mark.parametrize("currency", ["", "eur", "UAH", " EUR", "EUR "])
def test_noncanonical_or_unsupported_request_currency_is_rejected(currency: str) -> None:
    with pytest.raises((TypeError, ValueError), match="currency"):
        _client(
            lambda *_: _response(currency="EUR"),
            currency=currency,
        )

@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("currency", "GBP"),
        ("event_ids", (999,)),
        ("states", ("graded",)),
        ("price_mode", "aggregated"),
        ("minimum_liquidity", Decimal("10")),
        ("offset", 20),
        ("per_page", 50),
        ("sport_key", "tennis"),
    ],
)
def test_request_semantics_cannot_be_mutated_after_construction(
    field: str,
    replacement: object,
) -> None:
    client = _client(lambda *_: _response(currency="EUR"))

    with pytest.raises(
        AttributeError,
        match="request configuration is immutable after construction",
    ):
        setattr(client, field, replacement)


def test_low_level_request_config_mutation_fails_before_transport() -> None:
    calls: list[str] = []

    def transport(url, headers, timeout):
        calls.append(url)
        return _response(currency="EUR")

    client = _client(transport)
    frozen_query_sha256 = client.request_query_sha256

    object.__setattr__(client, "event_ids", (999,))

    with pytest.raises(
        MatchbookPayloadError,
        match="request configuration changed after construction",
    ):
        client.read_batch()

    assert calls == []
    assert client.request_query_sha256 == frozen_query_sha256


def test_request_config_mutation_during_transport_fails_before_publication() -> None:
    holder: dict[str, MatchbookReadOnlyProvider] = {}
    calls: list[str] = []

    def transport(url, headers, timeout):
        calls.append(url)
        object.__setattr__(holder["client"], "currency", "GBP")
        return _response(currency="GBP")

    client = _client(transport, currency="EUR")
    holder["client"] = client

    with pytest.raises(
        MatchbookPayloadError,
        match="request configuration changed after construction",
    ):
        client.read_batch()

    assert len(calls) == 1

