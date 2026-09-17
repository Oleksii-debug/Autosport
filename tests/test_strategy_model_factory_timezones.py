import pytest

from autosport.strategy_model_factory import MeanBaselineModel, TrainingPoint, WalkForwardRunner


def test_baseline_cutoff_compares_mixed_offsets_as_instants():
    points = (
        TrainingPoint(
            "2025-12-31T23:00:00+00:00",
            1.0,
            0.0,
            "2025-12-31T23:00:00+00:00",
        ),
        # Lexically this looks like Dec 31, but it is 2026-01-01T00:30:00Z.
        TrainingPoint(
            "2025-12-31T23:30:00-01:00",
            2.0,
            1.0,
            "2025-12-31T23:30:00-01:00",
        ),
    )

    model = MeanBaselineModel.fit(
        "mixed-offset-cutoff",
        points,
        training_cutoff="2026-01-01T00:00:00+00:00",
    )

    assert model.training_count == 1
    assert model.mean_target == 0.0


def test_prediction_future_guard_compares_mixed_offsets_as_instants():
    model = MeanBaselineModel.fit(
        "mixed-offset-predict",
        (
            TrainingPoint(
                "2025-12-30T00:00:00+00:00",
                1.0,
                0.0,
                "2025-12-30T00:00:00+00:00",
            ),
            TrainingPoint(
                "2025-12-31T00:00:00+00:00",
                2.0,
                1.0,
                "2025-12-31T00:00:00+00:00",
            ),
        ),
        training_cutoff="2025-12-31T00:00:00+00:00",
    )

    with pytest.raises(ValueError, match="not available"):
        model.predict(
            # This is 2026-01-01T00:30:00Z, 30 minutes after decision_at.
            TrainingPoint(
                "2025-12-31T23:30:00-01:00",
                3.0,
                1.0,
                "2025-12-31T23:30:00-01:00",
            ),
            decision_at="2026-01-01T00:00:00+00:00",
        )


def test_walk_forward_duplicate_timestamp_identity_normalizes_offsets():
    points = (
        TrainingPoint(
            "2026-01-01T00:00:00+00:00",
            1.0,
            0.0,
            "2026-01-01T00:00:00+00:00",
        ),
        TrainingPoint(
            "2026-01-02T00:00:00+00:00",
            2.0,
            1.0,
            "2026-01-02T00:00:00+00:00",
        ),
        # Same instant as the previous observation under a different offset.
        TrainingPoint(
            "2026-01-01T19:00:00-05:00",
            3.0,
            0.0,
            "2026-01-01T19:00:00-05:00",
        ),
    )

    with pytest.raises(ValueError, match="unique timestamps"):
        WalkForwardRunner.run(points, minimum_train_size=1)


@pytest.mark.parametrize(
    ("timestamp", "message"),
    (
        ("2026-01-01T00:00:00", "timezone"),
        ("not-an-iso-timestamp", "ISO-8601"),
    ),
)
def test_training_point_rejects_naive_or_malformed_timestamps(timestamp, message):
    with pytest.raises(ValueError, match=message):
        TrainingPoint(timestamp, 1.0, 0.0, timestamp)
