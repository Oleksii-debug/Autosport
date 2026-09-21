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


def test_adapter_derives_exact_indices_with_delayed_label_gap():
    points = (
        Point(T0, T0),
        Point(T1, T4),
        Point(T2, T2),
        Point(T3, T3),
    )
    folds = (
        Fold("fold-2", T1, T2, 1),
        Fold("fold-3", T2, T3, 2),
    )
    splits = derive_walk_forward_splits(points, folds)
    assert splits[0].training_indices == (0,)
    assert splits[0].evaluation_index == 2
    assert splits[1].training_indices == (0, 2)
    assert splits[1].evaluation_index == 3


def test_adapter_is_input_order_independent_but_indexed_to_canonical_order():
    points = (
        Point(T2, T2),
        Point(T0, T0),
        Point(T1, T1),
    )
    split = derive_walk_forward_splits(
        points,
        (Fold("fold-2", T1, T2, 2),),
    )[0]
    assert split.training_indices == (0, 1)
    assert split.evaluation_index == 2


def test_adapter_rejects_forged_causal_training_count():
    points = (
        Point(T0, T0),
        Point(T1, T1),
        Point(T2, T2),
    )
    with pytest.raises(
        ReproducibilityManifestError,
        match="causal_training_count does not match governed input lineage",
    ):
        derive_walk_forward_splits(
            points,
            (Fold("fold-2", T1, T2, 1),),
        )


def test_adapter_rejects_non_factory_cutoff_and_unknown_evaluation():
    points = (
        Point(T0, T0),
        Point(T1, T1),
        Point(T2, T2),
    )
    with pytest.raises(
        ReproducibilityManifestError,
        match="training_cutoff does not match preceding governed input",
    ):
        derive_walk_forward_splits(
            points,
            (Fold("fold-2", T0, T2, 1),),
        )
    with pytest.raises(
        ReproducibilityManifestError,
        match="evaluation_at does not resolve",
    ):
        derive_walk_forward_splits(
            points,
            (Fold("fold-x", T2, T3, 3),),
        )


def test_adapter_rejects_duplicate_observation_instants():
    points = (
        Point(T0, T0),
        Point(T0, T1),
        Point(T2, T2),
    )
    with pytest.raises(
        ReproducibilityManifestError,
        match="unique observed_at instants",
    ):
        derive_walk_forward_splits(
            points,
            (Fold("fold-2", T0, T2, 1),),
        )
