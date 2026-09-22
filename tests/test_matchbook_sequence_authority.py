from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.matchbook_provider import (
    MatchbookHttpJsonResponse,
    MatchbookReadOnlyProvider,
    MatchbookSequenceAuthorityError,
    MatchbookTransportError,
)


BODY_SHA = "a" * 64


class DurableSequenceStub:
    """Test double for a product-owned allocator that survives adapter reopen."""

    def __init__(self, start: int = 0) -> None:
        self.value = start
        self.sources: list[str] = []

    def __call__(self, source_id: str) -> int:
        self.sources.append(source_id)
        self.value += 1
        return self.value


def _payload(back_odds: str) -> dict[str, object]:
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
                                        "decimal-odds": Decimal(back_odds),
                                        "available-amount": Decimal("12.34"),
                                        "currency": "EUR",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _response(back_odds: str) -> MatchbookHttpJsonResponse:
    return MatchbookHttpJsonResponse(
        payload=_payload(back_odds),
        status_code=200,
        headers={},
        body_sha256=BODY_SHA,
    )


def _provider(*, transport, clock, sequence_allocator) -> MatchbookReadOnlyProvider:
    return MatchbookReadOnlyProvider(
        "test-session-token",
        sport_key="soccer",
        currency="EUR",
        sport_ids=(15,),
        transport=transport,
        clock=clock,
        sequence_allocator=sequence_allocator,
    )


def test_missing_product_sequence_authority_fails_closed() -> None:
    with pytest.raises(ValueError, match="sequence_allocator"):
        MatchbookReadOnlyProvider(
            "test-session-token",
            sport_key="soccer",
            currency="EUR",
            sport_ids=(15,),
            transport=lambda *_: _response("2.10"),
            clock=lambda: "2026-09-22T03:40:00+00:00",
        )


def test_same_receive_timestamp_uses_product_sequence_not_wall_clock() -> None:
    responses = iter((_response("2.10"), _response("2.20")))
    authority = DurableSequenceStub(start=100)
    client = _provider(
        transport=lambda *_: next(responses),
        clock=lambda: "2026-09-22T03:40:00+00:00",
        sequence_allocator=authority,
    )

    first = client.read_batch().quotes[0]
    second = client.read_batch().quotes[0]

    assert first.observed_ts == second.observed_ts
    assert first.decimal_odds != second.decimal_odds
    assert (first.sequence, second.sequence) == (101, 102)
    assert second.sequence > first.sequence
    assert authority.sources == [client.source_id, client.source_id]


def test_wall_clock_rollback_cannot_regress_successful_snapshot_sequence() -> None:
    responses = iter((_response("2.10"), _response("2.20")))
    times = iter(
        (
            "2026-09-22T03:40:01+00:00",
            "2026-09-22T03:40:00+00:00",
        )
    )
    authority = DurableSequenceStub(start=200)
    client = _provider(
        transport=lambda *_: next(responses),
        clock=lambda: next(times),
        sequence_allocator=authority,
    )

    first = client.read_batch().quotes[0]
    second = client.read_batch().quotes[0]

    assert second.observed_ts < first.observed_ts
    assert (first.sequence, second.sequence) == (201, 202)
    assert second.sequence > first.sequence


def test_adapter_reopen_reuses_external_durable_sequence_authority() -> None:
    authority = DurableSequenceStub(start=300)
    first_client = _provider(
        transport=lambda *_: _response("2.10"),
        clock=lambda: "2026-09-22T03:40:01+00:00",
        sequence_allocator=authority,
    )
    before_reopen = first_client.read_batch().quotes[0]

    reopened_client = _provider(
        transport=lambda *_: _response("2.20"),
        clock=lambda: "2026-09-22T03:39:59+00:00",
        sequence_allocator=authority,
    )
    after_reopen = reopened_client.read_batch().quotes[0]

    assert after_reopen.decimal_odds != before_reopen.decimal_odds
    assert (before_reopen.sequence, after_reopen.sequence) == (301, 302)
    assert after_reopen.sequence > before_reopen.sequence


@pytest.mark.parametrize("invalid", [True, 0, -1, 1 << 63, "1", Decimal("1")])
def test_invalid_sequence_authority_output_fails_closed(invalid: object) -> None:
    client = _provider(
        transport=lambda *_: _response("2.10"),
        clock=lambda: "2026-09-22T03:40:00+00:00",
        sequence_allocator=lambda _source_id: invalid,
    )
    with pytest.raises(MatchbookSequenceAuthorityError, match="positive signed-64"):
        client.read_batch()


def test_nonadvancing_live_allocator_fails_closed_before_quote_publication() -> None:
    values = iter((41, 41))
    responses = iter((_response("2.10"), _response("2.20")))
    client = _provider(
        transport=lambda *_: next(responses),
        clock=lambda: "2026-09-22T03:40:00+00:00",
        sequence_allocator=lambda _source_id: next(values),
    )

    assert client.read_batch().quotes[0].sequence == 41
    with pytest.raises(MatchbookSequenceAuthorityError, match="strictly advance"):
        client.read_batch()


def test_transport_failure_does_not_consume_product_sequence() -> None:
    calls = 0
    authority = DurableSequenceStub(start=500)

    def transport(*_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MatchbookTransportError("Matchbook HTTP 401", 401)
        return _response("2.10")

    client = _provider(
        transport=transport,
        clock=lambda: "2026-09-22T03:40:00+00:00",
        sequence_allocator=authority,
    )
    with pytest.raises(MatchbookTransportError):
        client.read_batch()
    assert authority.value == 500

    assert client.read_batch().quotes[0].sequence == 501
