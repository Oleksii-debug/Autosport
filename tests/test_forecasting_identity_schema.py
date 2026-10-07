from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.forecasting import (
    ForecastOutcomeFact,
    ForecastRecord,
    JsonlForecastLedger,
    TemporalEvaluationWindow,
    evaluate_forecast_window,
    evaluate_walk_forward,
)


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


class _TrapEvidenceList(list):
    def __iter__(self):
        raise AssertionError("evidence list subclass iteration must not execute")


class _TrapEvidenceTuple(tuple):
    def __iter__(self):
        raise AssertionError("evidence tuple subclass iteration must not execute")


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


def test_forecast_evidence_hashes_reject_container_subclass_before_dispatch() -> None:
    digest = "a" * 64
    for evidence_hashes in (
        _TrapEvidenceList([digest]),
        _TrapEvidenceTuple((digest,)),
    ):
        with pytest.raises(ValueError, match="exact list or tuple"):
            _record(evidence_hashes=evidence_hashes)


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


@pytest.mark.parametrize("value", ("", " padded", "padded ", "line\nbreak", "\x7f", "\ud800"))
def test_forecast_outcome_identity_rejects_noncanonical_spelling(value: str) -> None:
    with pytest.raises(ValueError, match="forecast_id"):
        ForecastOutcomeFact(forecast_id=value, outcome=1, revealed_at=_TS)


def test_forecast_outcome_identity_rejects_string_subclass_before_dispatch() -> None:
    with pytest.raises(ValueError, match="forecast_id"):
        ForecastOutcomeFact(
            forecast_id=_ExplosiveString("forecast-1"),
            outcome=1,
            revealed_at=_TS,
        )


def test_forecast_outcome_requires_exact_integer_binary_value_before_alias_dispatch() -> None:
    class HostileOutcome(int):
        def __eq__(self, other):
            raise AssertionError("outcome alias comparison must not execute")

    for value in (True, False, HostileOutcome(1), HostileOutcome(0), 2, -1):
        with pytest.raises(ValueError, match="exact integer 0 or 1"):
            ForecastOutcomeFact(
                forecast_id="forecast-1",
                outcome=value,
                revealed_at=_TS,
            )


def test_evaluation_revalidates_post_init_outcome_value_type() -> None:
    record = _record(generated_at="2026-10-07T00:00:00+00:00")
    fact = ForecastOutcomeFact(
        forecast_id="forecast-1",
        outcome=1,
        revealed_at="2026-10-07T00:10:00+00:00",
    )
    object.__setattr__(fact, "outcome", True)

    with pytest.raises(ValueError, match="exact integer 0 or 1"):
        evaluate_forecast_window((record,), (fact,), _evaluation_window())


def _evaluation_window() -> TemporalEvaluationWindow:
    return TemporalEvaluationWindow(
        window_id="window-1",
        training_end_ts="2026-10-06T21:00:00+00:00",
        evaluation_start_ts="2026-10-06T23:30:00+00:00",
        evaluation_end_ts="2026-10-07T00:30:00+00:00",
    )


def test_evaluation_revalidates_post_init_forecast_identity() -> None:
    record = _record(generated_at="2026-10-07T00:00:00+00:00")
    object.__setattr__(record, "forecast_id", " forecast-1")
    fact = ForecastOutcomeFact(
        forecast_id="forecast-1",
        outcome=1,
        revealed_at="2026-10-07T00:10:00+00:00",
    )

    with pytest.raises(ValueError, match="forecast_id"):
        evaluate_forecast_window((record,), (fact,), _evaluation_window())


def test_evaluation_revalidates_post_init_outcome_identity() -> None:
    record = _record(generated_at="2026-10-07T00:00:00+00:00")
    fact = ForecastOutcomeFact(
        forecast_id="forecast-1",
        outcome=1,
        revealed_at="2026-10-07T00:10:00+00:00",
    )
    object.__setattr__(fact, "forecast_id", " forecast-1")

    with pytest.raises(ValueError, match="forecast_id"):
        evaluate_forecast_window((record,), (fact,), _evaluation_window())


def test_evaluation_rejects_forecast_and_outcome_subclasses_before_virtual_dispatch() -> None:
    record = _record(generated_at="2026-10-07T00:00:00+00:00")
    subclass_record = _ForecastRecordSubclass(**record.to_dict())
    fact = ForecastOutcomeFact(
        forecast_id="forecast-1",
        outcome=1,
        revealed_at="2026-10-07T00:10:00+00:00",
    )

    with pytest.raises(ValueError, match="exact ForecastRecord"):
        evaluate_forecast_window((subclass_record,), (fact,), _evaluation_window())


@pytest.mark.parametrize("value", ("", " padded", "padded ", "line\nbreak", "\x7f", "\ud800"))
def test_evaluation_window_identity_rejects_noncanonical_spelling(value: str) -> None:
    with pytest.raises(ValueError, match="window_id"):
        TemporalEvaluationWindow(
            window_id=value,
            training_end_ts="2026-10-06T21:00:00+00:00",
            evaluation_start_ts="2026-10-06T23:30:00+00:00",
            evaluation_end_ts="2026-10-07T00:30:00+00:00",
        )


def test_evaluation_window_identity_rejects_string_subclass_before_dispatch() -> None:
    with pytest.raises(ValueError, match="window_id"):
        TemporalEvaluationWindow(
            window_id=_ExplosiveString("window-1"),
            training_end_ts="2026-10-06T21:00:00+00:00",
            evaluation_start_ts="2026-10-06T23:30:00+00:00",
            evaluation_end_ts="2026-10-07T00:30:00+00:00",
        )


def test_evaluation_revalidates_post_init_window_identity() -> None:
    record = _record(generated_at="2026-10-07T00:00:00+00:00")
    fact = ForecastOutcomeFact(
        forecast_id="forecast-1",
        outcome=1,
        revealed_at="2026-10-07T00:10:00+00:00",
    )
    window = _evaluation_window()
    object.__setattr__(window, "window_id", " window-1")

    with pytest.raises(ValueError, match="window_id"):
        evaluate_forecast_window((record,), (fact,), window)


def test_forecast_canonical_hash_rejects_subclass_before_virtual_serialization() -> None:
    record = _ForecastRecordSubclass(
        quote_key="event-1|market-1|selection-1",
        probability=Decimal("0.5"),
        model_id="model-1",
        model_version="model-version-1",
        strategy_version="strategy-version-1",
        model_training_cutoff_ts="2026-10-06T22:00:00+00:00",
        input_cutoff_ts="2026-10-06T23:00:00+00:00",
        generated_at=_TS,
        forecast_id="forecast-subclass-hash",
    )

    with pytest.raises(ValueError, match="exact ForecastRecord"):
        _ = record.canonical_hash

def test_walk_forward_rejects_window_subclass_before_attribute_dispatch() -> None:
    class HostileWindow(TemporalEvaluationWindow):
        __slots__ = ()

        def __getattribute__(self, name: str):
            if name == "evaluation_start_ts":
                raise AssertionError("window subclass attribute dispatch must not execute")
            return super().__getattribute__(name)

    window = HostileWindow(
        window_id="window-hostile",
        training_end_ts="2026-10-06T21:00:00+00:00",
        evaluation_start_ts="2026-10-06T23:30:00+00:00",
        evaluation_end_ts="2026-10-07T00:30:00+00:00",
    )

    with pytest.raises(ValueError, match="exact TemporalEvaluationWindow"):
        evaluate_walk_forward((), (), (window,))

def test_forecast_use_boundary_revalidates_evidence_reference_identities() -> None:
    digest = "a" * 64
    record = _record(evidence_hashes=(digest,), market_snapshot_hash="b" * 64)

    object.__setattr__(record, "evidence_hashes", [digest])
    with pytest.raises(ValueError, match="exact tuple"):
        record.to_dict()

    object.__setattr__(record, "evidence_hashes", ("A" * 64,))
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        record.to_dict()

    object.__setattr__(record, "evidence_hashes", (digest, digest))
    with pytest.raises(ValueError, match="duplicate evidence hashes"):
        record.to_dict()

    object.__setattr__(record, "evidence_hashes", (digest,))
    object.__setattr__(record, "market_snapshot_hash", _ExplosiveString("b" * 64))
    with pytest.raises(ValueError, match="market_snapshot_hash"):
        record.to_dict()


def test_forecast_ledger_rejects_mutated_evidence_identity_before_append(tmp_path) -> None:
    record = _record(evidence_hashes=("a" * 64,))
    object.__setattr__(record, "evidence_hashes", ("A" * 64,))
    ledger = JsonlForecastLedger(tmp_path / "forecasts.jsonl")

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        ledger.append(record)

    assert not (tmp_path / "forecasts.jsonl").exists()


def test_evaluation_window_split_rejects_string_subclass_before_hash_dispatch() -> None:
    class HostileSplit(str):
        def __hash__(self):
            raise AssertionError("split alias hash must not execute")

    with pytest.raises(ValueError, match="exact validation or holdout"):
        TemporalEvaluationWindow(
            window_id="window-1",
            training_end_ts="2026-10-06T21:00:00+00:00",
            evaluation_start_ts="2026-10-06T23:30:00+00:00",
            evaluation_end_ts="2026-10-07T00:30:00+00:00",
            split=HostileSplit("holdout"),
        )

