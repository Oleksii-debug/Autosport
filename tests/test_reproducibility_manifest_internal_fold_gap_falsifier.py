"""Dependent falsifier for PR #968 incomplete walk-forward fold coverage."""

from dataclasses import dataclass

import pytest

from autosport.reproducibility_manifest import (
    ReproducibilityManifestError,
    derive_walk_forward_splits,
)


T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"
T4 = "2026-01-05T00:00:00+00:00"


@dataclass(frozen=True)
class Point:
    observed_at: str
    target_reveal_at: str


@dataclass(frozen=True)
class Fold:
    fold_id: str
    training_cutoff: str
    evaluation_at: str
    causal_training_count: int


def test_adapter_rejects_internal_fold_gap_even_when_final_input_is_covered() -> None:
    points = (
        Point(T0, T0),
        Point(T1, T1),
        Point(T2, T2),
        Point(T3, T3),
        Point(T4, T4),
    )

    # With minimum_train_size=1, the canonical WalkForwardRunner has enough
    # causally available training data from evaluation index 1 onward. Once that
    # first fold exists, every later governed evaluation index is part of the
    # canonical fold history. This witness omits indices 2 and 3 but still includes
    # the final index, so checking only "last == final" is insufficient.
    incomplete_folds = (
        Fold("fold-1", T0, T1, 1),
        Fold("fold-4", T3, T4, 4),
    )

    with pytest.raises(
        ReproducibilityManifestError,
        match="cover|coverage|contiguous|complete",
    ):
        derive_walk_forward_splits(points, incomplete_folds)
