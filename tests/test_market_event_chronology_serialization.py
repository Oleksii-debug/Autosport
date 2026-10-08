from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import MarketEvent


_TS = "2026-10-07T00:00:00+00:00"


class _ExplosiveTimestamp(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("timestamp subclass dispatch must not run")

    def replace(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("timestamp subclass dispatch must not run")

    def encode(self, *args: object, **kwargs: object) -> bytes:
        raise AssertionError("timestamp subclass dispatch must not run")


def _event() -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal("2.5"),
        observed_ts=_TS,
        source_id="provider-1",
        sequence=1,
        source_ts=_TS,
        ingest_ts=_TS,
    )


@pytest.mark.parametrize("field_name", ("observed_ts", "ingest_ts", "source_ts"))
def test_to_dict_rejects_post_construction_timestamp_subclass_without_dispatch(
    field_name: str,
) -> None:
    event = _event()
    object.__setattr__(event, field_name, _ExplosiveTimestamp(_TS))

    with pytest.raises(ValueError, match=rf"{field_name} must be a non-empty trimmed string"):
        event.to_dict()


@pytest.mark.parametrize("field_name", ("observed_ts", "ingest_ts", "source_ts"))
def test_to_dict_rejects_post_construction_naive_timestamp(field_name: str) -> None:
    event = _event()
    object.__setattr__(event, field_name, "2026-10-07T00:00:00")

    with pytest.raises(
        ValueError,
        match=rf"{field_name} must be timezone-aware ISO-8601",
    ):
        event.to_dict()


def test_to_dict_publishes_revalidated_canonical_chronology() -> None:
    event = _event()

    payload = event.to_dict()

    assert payload["observed_ts"] == _TS
    assert payload["ingest_ts"] == _TS
    assert payload["source_ts"] == _TS


def test_to_dict_preserves_absent_source_time() -> None:
    event = _event()
    object.__setattr__(event, "source_ts", None)

    payload = event.to_dict()

    assert payload["source_ts"] is None


def test_constructor_rejects_ingest_before_observation() -> None:
    with pytest.raises(ValueError, match="ingest_ts cannot be before observed_ts"):
        MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            decimal_odds=Decimal("2.5"),
            observed_ts="2026-10-07T00:00:02+00:00",
            source_id="provider-1",
            sequence=1,
            source_ts="2026-10-07T00:00:03+00:00",
            ingest_ts="2026-10-07T00:00:01+00:00",
        )


def test_from_dict_rejects_ingest_before_observation() -> None:
    raw = _event().to_dict()
    raw["observed_ts"] = "2026-10-07T00:00:02+00:00"
    raw["ingest_ts"] = "2026-10-07T00:00:01+00:00"

    with pytest.raises(ValueError, match="ingest_ts cannot be before observed_ts"):
        MarketEvent.from_dict(raw)


def test_to_dict_rejects_post_construction_chronology_inversion() -> None:
    event = _event()
    object.__setattr__(event, "observed_ts", "2026-10-07T00:00:02+00:00")
    object.__setattr__(event, "ingest_ts", "2026-10-07T00:00:01+00:00")

    with pytest.raises(ValueError, match="ingest_ts cannot be before observed_ts"):
        event.to_dict()


def test_market_event_chronology_compares_absolute_instants() -> None:
    event = _event()
    object.__setattr__(event, "observed_ts", "2026-10-07T02:00:00+02:00")
    object.__setattr__(event, "ingest_ts", "2026-10-07T00:00:00+00:00")

    payload = event.to_dict()

    assert payload["observed_ts"] == "2026-10-07T02:00:00+02:00"
    assert payload["ingest_ts"] == "2026-10-07T00:00:00+00:00"
