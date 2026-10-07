from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.forecasting import ForecastRecord, JsonlForecastLedger


TRAINING = "2026-09-13T09:00:00+00:00"
INPUT = "2026-09-13T10:00:01+00:00"
GENERATED = "2026-09-13T10:00:02+00:00"


def _forecast() -> ForecastRecord:
    return ForecastRecord(
        quote_key="match-1|winner|B",
        probability=Decimal("0.50"),
        model_id="model-1",
        model_version="1.0.0",
        strategy_version="research-v1",
        model_training_cutoff_ts=TRAINING,
        input_cutoff_ts=INPUT,
        generated_at=GENERATED,
        uncertainty=Decimal("0.10"),
        evidence_hashes=("a" * 64,),
        market_snapshot_hash="b" * 64,
        provenance={"source": "chronology-use-boundary-test"},
        forecast_id="forecast-1",
    )


@pytest.mark.parametrize("operation", ("to_dict", "canonical_hash"))
def test_forecast_use_boundary_rejects_mutated_training_after_input(
    operation: str,
) -> None:
    record = _forecast()
    object.__setattr__(
        record,
        "model_training_cutoff_ts",
        "2026-09-13T10:00:01.500000+00:00",
    )

    with pytest.raises(
        ValueError,
        match="model training cutoff cannot be after forecast input cutoff",
    ):
        if operation == "to_dict":
            record.to_dict()
        else:
            _ = record.canonical_hash


@pytest.mark.parametrize("operation", ("to_dict", "canonical_hash"))
def test_forecast_use_boundary_rejects_mutated_input_after_generation(
    operation: str,
) -> None:
    record = _forecast()
    object.__setattr__(
        record,
        "input_cutoff_ts",
        "2026-09-13T10:00:03+00:00",
    )

    with pytest.raises(
        ValueError,
        match="input cutoff cannot be after forecast generation",
    ):
        if operation == "to_dict":
            record.to_dict()
        else:
            _ = record.canonical_hash


def test_forecast_ledger_rejects_mutated_naive_cutoff_before_write() -> None:
    record = _forecast()
    object.__setattr__(
        record,
        "input_cutoff_ts",
        "2026-09-13T10:00:01",
    )

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "forecasts.jsonl"
        with pytest.raises(ValueError, match="timezone-aware"):
            JsonlForecastLedger(path).append(record)
        assert not path.exists()
