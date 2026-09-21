from __future__ import annotations

from dataclasses import dataclass

from autosport.strategy_model_factory import TrainingPoint, WalkForwardRunner


T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"
T4 = "2026-01-05T00:00:00+00:00"
T5 = "2026-01-06T00:00:00+00:00"
T6 = "2026-01-07T00:00:00+00:00"


@dataclass(frozen=True, slots=True)
class _FitCall:
    model_id: str
    training_cutoff: str
    observed_at: tuple[str, ...]
    target_reveal_at: tuple[str, ...]
    targets: tuple[float, ...]


class _RecordingMeanModel:
    def __init__(
        self,
        model_id: str,
        training_cutoff: str,
        prediction: float,
    ) -> None:
        self.model_id = model_id
        self.training_cutoff = training_cutoff
        self.prediction = prediction

    def predict(self, point, *, decision_at: str) -> float:
        # Deliberately use only a field available before the decision. This keeps
        # WP-S04 window tests disjoint from #797 prediction-label redaction.
        assert point.observed_at == decision_at
        return self.prediction


class _RecordingMeanFactory:
    model_family = "wp-s04-recording-mean-v1"

    def __init__(self) -> None:
        self.fit_calls: list[_FitCall] = []

    def fit(self, model_id, points, *, training_cutoff):
        snapshot = tuple(points)
        call = _FitCall(
            model_id=model_id,
            training_cutoff=training_cutoff,
            observed_at=tuple(point.observed_at for point in snapshot),
            target_reveal_at=tuple(point.target_reveal_at for point in snapshot),
            targets=tuple(point.target for point in snapshot),
        )
        self.fit_calls.append(call)
        mean_target = sum(call.targets) / len(call.targets)
        return _RecordingMeanModel(model_id, training_cutoff, mean_target)


def _prefix() -> tuple[TrainingPoint, ...]:
    return (
        TrainingPoint(T0, 10.0, 0.0, T0),
        TrainingPoint(T1, 11.0, 1.0, T1),
        TrainingPoint(T2, 12.0, 0.0, T2),
        TrainingPoint(T3, 13.0, 1.0, T3),
        TrainingPoint(T4, 14.0, 0.0, T4),
    )


def _run(points: tuple[TrainingPoint, ...]):
    factory = _RecordingMeanFactory()
    result = WalkForwardRunner.run(
        points,
        minimum_train_size=2,
        model_factory=factory,
    )
    return result, factory


def test_closed_prefix_folds_are_unchanged_by_future_tail_extension() -> None:
    prefix = _prefix()
    prefix_result, prefix_factory = _run(prefix)

    extended = prefix + (
        TrainingPoint(T5, 1_000_000.0, -1000.0, T5),
        TrainingPoint(T6, -1_000_000.0, 1000.0, T6),
    )
    extended_result, extended_factory = _run(extended)

    prefix_fold_count = len(prefix_result.folds)
    assert tuple(extended_result.folds[:prefix_fold_count]) == prefix_result.folds
    assert (
        tuple(extended_factory.fit_calls[: len(prefix_factory.fit_calls)])
        == tuple(prefix_factory.fit_calls)
    )
    assert [fold.evaluation_at for fold in prefix_result.folds] == [T2, T3, T4]


def test_mutating_only_unseen_future_tail_cannot_rewrite_closed_prefix() -> None:
    prefix = _prefix()
    first_result, first_factory = _run(
        prefix
        + (
            TrainingPoint(T5, 15.0, 0.0, T5),
            TrainingPoint(T6, 16.0, 1.0, T6),
        )
    )
    second_result, second_factory = _run(
        prefix
        + (
            TrainingPoint(T5, -9_999_999.0, 9999.0, T5),
            TrainingPoint(T6, 9_999_999.0, -9999.0, T6),
        )
    )

    closed_count = len(_run(prefix)[0].folds)
    assert tuple(first_result.folds[:closed_count]) == tuple(
        second_result.folds[:closed_count]
    )
    assert tuple(first_factory.fit_calls[:closed_count]) == tuple(
        second_factory.fit_calls[:closed_count]
    )
    assert first_result.folds[closed_count:] != second_result.folds[closed_count:]


def test_every_fit_uses_only_pre_evaluation_observations_with_revealed_labels() -> None:
    result, factory = _run(
        (
            TrainingPoint(T0, 1.0, 0.0, T0),
            TrainingPoint(T1, 2.0, 1.0, T1),
            TrainingPoint(T2, 3.0, 0.0, T2),
            TrainingPoint(T3, 4.0, 1.0, T3),
            TrainingPoint(T4, 5.0, 0.0, T4),
            TrainingPoint(T5, 6.0, 1.0, T5),
        )
    )

    assert len(factory.fit_calls) == len(result.folds)
    for call, fold in zip(factory.fit_calls, result.folds, strict=True):
        assert call.training_cutoff == fold.training_cutoff
        assert fold.evaluation_at not in call.observed_at
        assert all(
            observed_at <= call.training_cutoff for observed_at in call.observed_at
        )
        assert all(
            target_reveal_at <= call.training_cutoff
            for target_reveal_at in call.target_reveal_at
        )
        assert len(call.observed_at) == fold.causal_training_count


def test_delayed_label_enters_fit_history_only_after_its_reveal_cutoff() -> None:
    points = (
        TrainingPoint(T0, 1.0, 0.0, T0),
        TrainingPoint(T1, 2.0, 1.0, T4),
        TrainingPoint(T2, 3.0, 0.0, T2),
        TrainingPoint(T3, 4.0, 1.0, T3),
        TrainingPoint(T4, 5.0, 0.0, T4),
        TrainingPoint(T5, 6.0, 1.0, T5),
    )
    result, factory = _run(points)

    assert [fold.evaluation_at for fold in result.folds] == [T3, T4, T5]
    assert [fold.causal_training_count for fold in result.folds] == [2, 3, 5]
    assert [call.observed_at for call in factory.fit_calls] == [
        (T0, T2),
        (T0, T2, T3),
        (T0, T1, T2, T3, T4),
    ]
    assert T1 not in factory.fit_calls[0].observed_at
    assert T1 not in factory.fit_calls[1].observed_at
    assert T1 in factory.fit_calls[2].observed_at
    assert factory.fit_calls[2].training_cutoff == T4


def test_reordered_input_cannot_change_fit_history_or_closed_window_results() -> None:
    points = _prefix() + (
        TrainingPoint(T5, 15.0, 1.0, T5),
        TrainingPoint(T6, 16.0, 0.0, T6),
    )
    ordered_result, ordered_factory = _run(points)
    reordered_result, reordered_factory = _run(
        (points[4], points[1], points[6], points[0], points[5], points[3], points[2])
    )

    assert reordered_result == ordered_result
    assert reordered_result.result_sha256 == ordered_result.result_sha256
    assert reordered_factory.fit_calls == ordered_factory.fit_calls
