from __future__ import annotations

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
