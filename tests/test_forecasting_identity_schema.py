from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.forecasting import ForecastRecord, JsonlForecastLedger


_TS = "2026-10-07T00:00:00+00:00"


class _ExplosiveString(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("str subclass strip() must not execute")

    def encode(self, *args: object, **kwargs: object) -> bytes:
        raise AssertionError("str subclass encode() must not execute")


class _ForecastRecordSubclass(ForecastRecord):
    __slots__ = ()

    def to_dict(self) -> dict[str, object]:
        raise AssertionError("ForecastRecord subclass serialization must not execute")


def _record(**overrides: object) -> ForecastRecord:
    payload: dict[str, object] = {
        "quote_key": "event-1|market-1|selection-1",
        "probability": Decimal("0.5"),
        "model_id": "model-1",
        "model_version": "model-version-1",
        "strategy_version": "strategy-version-1",
        "model_training_cutoff_ts": "2026-10-06T22:00:00+00:00",
        "input_cutoff_ts": "2026-10-06T23:00:00+00:00",
        "generated_at": _TS,
        "forecast_id": "forecast-1",
    }
    payload.update(overrides)
    return ForecastRecord(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("forecast_id", "forecast-1"),
        ("quote_key", "event-1|market-1|selection-1"),
        ("model_id", "model-1"),
        ("model_version", "model-version-1"),
        ("strategy_version", "strategy-version-1"),
    ),
)
def test_forecast_identity_rejects_string_subclass_before_dispatch(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        _record(**{field_name: _ExplosiveString(value)})


@pytest.mark.parametrize(
    "field_name",
    ("forecast_id", "quote_key", "model_id", "model_version", "strategy_version"),
)
@pytest.mark.parametrize(
    "value",
    ("", " padded", "padded ", "line\nbreak", "\x7f", "\ud800"),
)
def test_forecast_identity_rejects_noncanonical_spelling(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        _record(**{field_name: value})


@pytest.mark.parametrize(
    "field_name",
    ("forecast_id", "quote_key", "model_id", "model_version", "strategy_version"),
)
def test_forecast_identity_is_revalidated_at_hash_and_serialization_boundary(
    field_name: str,
) -> None:
    record = _record()
    object.__setattr__(record, field_name, " padded")

    with pytest.raises(ValueError, match=field_name):
        record.to_dict()
    with pytest.raises(ValueError, match=field_name):
        _ = record.canonical_hash


def test_forecast_ledger_revalidates_mutated_identity_before_durable_append(
    tmp_path,
) -> None:
    record = _record()
    object.__setattr__(record, "model_id", "model-1\nforged")
    ledger = JsonlForecastLedger(tmp_path / "forecasts.jsonl")

    with pytest.raises(ValueError, match="model_id"):
        ledger.append(record)

    assert not (tmp_path / "forecasts.jsonl").exists()


def test_forecast_ledger_rejects_subclass_before_virtual_serialization(
    tmp_path,
) -> None:
    record = _ForecastRecordSubclass(
        quote_key="event-1|market-1|selection-1",
        probability=Decimal("0.5"),
        model_id="model-1",
        model_version="model-version-1",
        strategy_version="strategy-version-1",
        model_training_cutoff_ts="2026-10-06T22:00:00+00:00",
        input_cutoff_ts="2026-10-06T23:00:00+00:00",
        generated_at=_TS,
        forecast_id="forecast-subclass",
    )
    ledger = JsonlForecastLedger(tmp_path / "forecasts.jsonl")

    with pytest.raises(ValueError, match="exact ForecastRecord"):
        ledger.append(record)

    assert not (tmp_path / "forecasts.jsonl").exists()


def test_valid_forecast_identity_spelling_and_hash_remain_stable() -> None:
    record = _record()
    payload = record.to_dict()

    assert payload["forecast_id"] == "forecast-1"
    assert payload["quote_key"] == "event-1|market-1|selection-1"
    assert payload["model_id"] == "model-1"
    assert payload["model_version"] == "model-version-1"
    assert payload["strategy_version"] == "strategy-version-1"
    assert len(record.canonical_hash) == 64
