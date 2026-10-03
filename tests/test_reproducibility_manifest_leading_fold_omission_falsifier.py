import pytest

from autosport.reproducibility_manifest import (
    ReproducibilityManifestError,
    derive_walk_forward_splits,
)
from autosport.strategy_model_factory import TrainingPoint, WalkForwardRunner


T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"
T4 = "2026-01-05T00:00:00+00:00"


def test_adapter_rejects_omitted_leading_canonical_folds() -> None:
    """A contiguous suffix is not a complete canonical walk-forward history."""

    points = (
        TrainingPoint(T0, 1.0, 1.0, T0),
        TrainingPoint(T1, 2.0, 2.0, T1),
        TrainingPoint(T2, 3.0, 3.0, T2),
        TrainingPoint(T3, 4.0, 4.0, T3),
        TrainingPoint(T4, 5.0, 5.0, T4),
    )
    canonical = WalkForwardRunner.run(points)
    assert tuple(fold.fold_id for fold in canonical.folds) == (
        "fold-2",
        "fold-3",
        "fold-4",
    )

    omitted_leading_fold = canonical.folds[1:]

    with pytest.raises(
        ReproducibilityManifestError,
        match="complete canonical evaluation coverage",
    ):
        derive_walk_forward_splits(
            points,
            omitted_leading_fold,
            minimum_train_size=2,
        )
