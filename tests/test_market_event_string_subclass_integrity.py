from __future__ import annotations

import pytest

from autosport.domain import MarketEvent


class _ExplosiveString(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("str subclass strip() must not be invoked")

    def replace(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("str subclass replace() must not be invoked")

    def encode(self, *args: object, **kwargs: object) -> bytes:
        raise AssertionError("str subclass encode() must not be invoked")


def _event_payload() -> dict[str, object]:
    return {
        "event_id": "event-1",
        "market_id": "winner",
        "selection_id": "selection-1",
        "decimal_odds": "1.62",
        "observed_ts": "2026-09-12T10:00:00+00:00",
        "source_id": "fixture",
        "sequence": 1,
        "market_type": "winner",
    }


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("event_id", "event-1"),
        ("market_id", "winner"),
        ("selection_id", "selection-1"),
        ("observed_ts", "2026-09-12T10:00:00+00:00"),
        ("source_id", "fixture"),
        ("ingest_ts", "2026-09-12T10:00:01+00:00"),
        ("market_type", "winner"),
        ("status", "open"),
        ("score_state", "2-1"),
        ("source_ts", "2026-09-12T09:59:59Z"),
    ],
)
def test_serialized_string_fields_reject_str_subclasses_before_virtual_methods(
    field_name: str,
    value: str,
) -> None:
    payload = _event_payload()
    payload[field_name] = _ExplosiveString(value)

    with pytest.raises(ValueError):
        MarketEvent.from_dict(payload)


def test_decimal_odds_rejects_str_subclass_before_virtual_methods() -> None:
    payload = _event_payload()
    payload["decimal_odds"] = _ExplosiveString("1.62")

    with pytest.raises(
        ValueError,
        match="decimal_odds must be a finite decimal greater than 1",
    ):
        MarketEvent.from_dict(payload)


def test_metadata_value_rejects_str_subclass_before_virtual_methods() -> None:
    payload = _event_payload()
    payload["metadata"] = {"label": _ExplosiveString("canonical-looking")}

    with pytest.raises(ValueError, match="non-canonical JSON value type"):
        MarketEvent.from_dict(payload)


def test_metadata_key_rejects_str_subclass_before_virtual_methods() -> None:
    payload = _event_payload()
    payload["metadata"] = {_ExplosiveString("label"): "value"}

    with pytest.raises(ValueError, match="non-string JSON object key"):
        MarketEvent.from_dict(payload)
