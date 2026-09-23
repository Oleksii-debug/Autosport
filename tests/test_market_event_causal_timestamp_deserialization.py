from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import MarketEvent


def _serialized_event() -> dict[str, object]:
    return {
        "event_id": "event-1",
        "market_id": "market-1",
        "selection_id": "selection-1",
        "decimal_odds": "2.50",
        "observed_ts": "2026-09-23T10:00:00+00:00",
        "source_id": "source-1",
        "sequence": 1,
    }


def _direct_event(**overrides: object) -> MarketEvent:
    values: dict[str, object] = {
        "event_id": "event-1",
        "market_id": "market-1",
        "selection_id": "selection-1",
        "decimal_odds": Decimal("2.50"),
        "observed_ts": "2026-09-23T10:00:00+00:00",
        "source_id": "source-1",
        "sequence": 1,
        "source_ts": "2026-09-23T09:59:59Z",
        "ingest_ts": "2026-09-23T12:00:01+02:00",
    }
    values.update(overrides)
    return MarketEvent(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("observed_ts", "not-a-timestamp"),
        ("observed_ts", "2026-09-23T10:00:00"),
        ("ingest_ts", "not-a-timestamp"),
        ("ingest_ts", "2026-09-23T10:00:01"),
    ],
)
def test_from_dict_rejects_invalid_or_timezone_naive_causal_timestamp(
    field_name: str,
    invalid_value: str,
) -> None:
    payload = _serialized_event()
    payload[field_name] = invalid_value

    with pytest.raises(ValueError, match=rf"{field_name} must be"):
        MarketEvent.from_dict(payload)


def test_from_dict_legacy_missing_ingest_timestamp_inherits_valid_observed_timestamp() -> None:
    payload = _serialized_event()

    event = MarketEvent.from_dict(payload)

    assert event.observed_ts == "2026-09-23T10:00:00+00:00"
    assert event.ingest_ts == event.observed_ts


def test_from_dict_preserves_valid_timezone_aware_timestamp_lexemes() -> None:
    payload = _serialized_event()
    payload["observed_ts"] = "2026-09-23T12:00:00+02:00"
    payload["ingest_ts"] = "2026-09-23T10:00:01Z"

    event = MarketEvent.from_dict(payload)

    assert event.observed_ts == "2026-09-23T12:00:00+02:00"
    assert event.ingest_ts == "2026-09-23T10:00:01Z"


def test_from_dict_missing_observed_timestamp_fails_closed() -> None:
    payload = _serialized_event()
    del payload["observed_ts"]

    with pytest.raises(ValueError, match="observed_ts must be"):
        MarketEvent.from_dict(payload)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("observed_ts", "not-a-timestamp"),
        ("observed_ts", "2026-09-23T10:00:00"),
        ("ingest_ts", "not-a-timestamp"),
        ("ingest_ts", "2026-09-23T10:00:01"),
        ("source_ts", "not-a-timestamp"),
        ("source_ts", "2026-09-23T09:59:59"),
    ],
)
def test_direct_construction_rejects_non_persistable_causal_timestamp(
    field_name: str,
    invalid_value: str,
) -> None:
    with pytest.raises(ValueError, match=rf"{field_name} must be"):
        _direct_event(**{field_name: invalid_value})


def test_direct_construction_accepts_missing_optional_source_timestamp() -> None:
    event = _direct_event(source_ts=None)

    assert event.source_ts is None
    assert MarketEvent.from_dict(event.to_dict()) == event


def test_direct_construction_round_trip_preserves_valid_timestamp_lexemes() -> None:
    event = _direct_event(
        observed_ts="2026-09-23T12:00:00+02:00",
        source_ts="2026-09-23T09:59:59Z",
        ingest_ts="2026-09-23T10:00:01Z",
    )

    restored = MarketEvent.from_dict(event.to_dict())

    assert restored == event
    assert restored.observed_ts == "2026-09-23T12:00:00+02:00"
    assert restored.source_ts == "2026-09-23T09:59:59Z"
    assert restored.ingest_ts == "2026-09-23T10:00:01Z"
