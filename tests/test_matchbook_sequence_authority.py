from __future__ import annotations

from decimal import Decimal

from autosport.matchbook_provider import MatchbookHttpJsonResponse, MatchbookReadOnlyProvider


BODY_SHA = "a" * 64


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


def _provider(*, transport, clock) -> MatchbookReadOnlyProvider:
    return MatchbookReadOnlyProvider(
        "test-session-token",
        sport_key="soccer",
        currency="EUR",
        sport_ids=(15,),
        transport=transport,
        clock=clock,
    )


def test_same_receive_timestamp_cannot_reuse_durable_sequence() -> None:
    """Two successful observations at one wall-clock instant remain distinct snapshots."""

    responses = iter((_response("2.10"), _response("2.20")))
    client = _provider(
        transport=lambda *_: next(responses),
        clock=lambda: "2026-09-22T03:40:00+00:00",
    )

    first = client.read_batch().quotes[0]
    second = client.read_batch().quotes[0]

    assert first.observed_ts == second.observed_ts
    assert first.decimal_odds != second.decimal_odds
    assert second.sequence > first.sequence, (
        "successful Matchbook snapshots must have a strictly advancing durable "
        "ordering identity even when product receive timestamps are equal"
    )


def test_wall_clock_rollback_cannot_regress_successful_snapshot_sequence() -> None:
    """A later successful provider read cannot become older ordering truth."""

    responses = iter((_response("2.10"), _response("2.20")))
    times = iter(
        (
            "2026-09-22T03:40:01+00:00",
            "2026-09-22T03:40:00+00:00",
        )
    )
    client = _provider(
        transport=lambda *_: next(responses),
        clock=lambda: next(times),
    )

    first = client.read_batch().quotes[0]
    second = client.read_batch().quotes[0]

    assert second.observed_ts < first.observed_ts
    assert second.sequence > first.sequence, (
        "a later successful acquisition must not regress durable ordering when "
        "the local wall clock moves backward"
    )


def test_restart_does_not_reset_sequence_authority_to_wall_clock() -> None:
    """A fresh adapter instance must not mint older sequence authority after restart."""

    first_client = _provider(
        transport=lambda *_: _response("2.10"),
        clock=lambda: "2026-09-22T03:40:01+00:00",
    )
    before_restart = first_client.read_batch().quotes[0]

    reopened_client = _provider(
        transport=lambda *_: _response("2.20"),
        clock=lambda: "2026-09-22T03:39:59+00:00",
    )
    after_restart = reopened_client.read_batch().quotes[0]

    assert after_restart.decimal_odds != before_restart.decimal_odds
    assert after_restart.sequence > before_restart.sequence, (
        "a successful post-restart acquisition needs durable monotonic ordering; "
        "a fresh process-local/wall-clock sequence is insufficient"
    )
