from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autosport.causal_features import (
    AvailabilityCache,
    CausalFeatureError,
    DatasetPartition,
    FeatureLeakageError,
    FeatureLineage,
    FeaturePoint,
    TrainOnlyStandardizer,
    causal_shift,
    fill_missing,
    trailing_mean,
)


BASE = datetime(2026, 1, 1, tzinfo=UTC)


def points() -> tuple[FeaturePoint, ...]:
    return (
        FeaturePoint(Decimal("1"), BASE),
        FeaturePoint(None, BASE + timedelta(minutes=1)),
        FeaturePoint(Decimal("3"), BASE + timedelta(minutes=2)),
    )


def out_of_order_points() -> tuple[FeaturePoint, ...]:
    return (
        FeaturePoint(Decimal("1"), BASE + timedelta(minutes=10)),
        FeaturePoint(None, BASE + timedelta(minutes=5)),
    )


def test_negative_shift_fails_closed() -> None:
    with pytest.raises(FeatureLeakageError, match="negative shift"):
        causal_shift(points(), -1)


def test_positive_shift_preserves_decision_time_and_only_uses_past_values() -> None:
    shifted = causal_shift(points(), 1)
    assert shifted[0].value is None
    assert shifted[1].value == Decimal("1")
    assert shifted[2].value is None
    assert tuple(item.available_at for item in shifted) == tuple(
        item.available_at for item in points()
    )


def test_shift_rejects_out_of_order_availability() -> None:
    with pytest.raises(FeatureLeakageError, match="nondecreasing available_at"):
        causal_shift(out_of_order_points(), 1)


def test_centered_window_fails_closed() -> None:
    with pytest.raises(FeatureLeakageError, match="centered"):
        trailing_mean(points(), 3, centered=True)


def test_trailing_mean_uses_only_current_and_past_values() -> None:
    result = trailing_mean(points(), 2)
    assert result[0].value == Decimal("1")
    assert result[1].value == Decimal("1")
    assert result[2].value == Decimal("3")


def test_trailing_mean_rejects_out_of_order_availability() -> None:
    with pytest.raises(FeatureLeakageError, match="nondecreasing available_at"):
        trailing_mean(out_of_order_points(), 2)


def test_backward_fill_fails_closed() -> None:
    with pytest.raises(FeatureLeakageError, match="forward fill"):
        fill_missing(points(), method="backward")


def test_forward_fill_never_reads_a_later_value() -> None:
    filled = fill_missing(points())
    assert [item.value for item in filled] == [
        Decimal("1"),
        Decimal("1"),
        Decimal("3"),
    ]


def test_forward_fill_rejects_out_of_order_availability() -> None:
    with pytest.raises(FeatureLeakageError, match="nondecreasing available_at"):
        fill_missing(out_of_order_points())


def test_equal_availability_is_valid_for_index_ordered_transforms() -> None:
    equal_time = (
        FeaturePoint(Decimal("1"), BASE),
        FeaturePoint(None, BASE),
    )
    assert causal_shift(equal_time, 1)[1].value == Decimal("1")
    assert trailing_mean(equal_time, 2)[1].value == Decimal("1")
    assert fill_missing(equal_time)[1].value == Decimal("1")


def test_train_only_standardizer_rejects_validation_and_test_fit() -> None:
    scaler = TrainOnlyStandardizer()
    for partition in (DatasetPartition.VALIDATION, DatasetPartition.TEST):
        with pytest.raises(FeatureLeakageError, match="train"):
            scaler.fit(
                [Decimal("1"), Decimal("2")],
                partition=partition,
            )

    scaler.fit(
        [Decimal("1"), Decimal("2")],
        partition=DatasetPartition.TRAIN,
    )
    assert scaler.fitted is True
    assert len(scaler.transform([Decimal("3")])) == 1


def test_standardizer_must_be_fit_before_transform() -> None:
    with pytest.raises(FeatureLeakageError, match="must be fit"):
        TrainOnlyStandardizer().transform([Decimal("1")])


def test_feature_lineage_cannot_precede_latest_input() -> None:
    with pytest.raises(FeatureLeakageError, match="latest input"):
        FeatureLineage(
            "bad",
            ("x", "y"),
            (BASE, BASE + timedelta(minutes=2)),
            BASE + timedelta(minutes=1),
        )


def test_feature_lineage_requires_complete_input_identity() -> None:
    with pytest.raises(CausalFeatureError, match="equal length"):
        FeatureLineage(
            "bad",
            ("x",),
            (BASE, BASE + timedelta(minutes=1)),
            BASE + timedelta(minutes=2),
        )


def test_future_cache_entry_is_not_visible_at_decision_time() -> None:
    cache = AvailabilityCache()
    cache.put(
        "candidate.feature",
        Decimal("7"),
        available_at=BASE + timedelta(minutes=2),
    )
    with pytest.raises(FeatureLeakageError, match="future cache"):
        cache.get("candidate.feature", at=BASE + timedelta(minutes=1))
    assert cache.get("candidate.feature", at=BASE + timedelta(minutes=2)) == Decimal("7")


def test_naive_timestamps_fail_closed() -> None:
    with pytest.raises(CausalFeatureError, match="timezone-aware"):
        FeaturePoint(Decimal("1"), datetime(2026, 1, 1))
