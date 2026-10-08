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
        Fold("fold-1", T0, T1, 1),
        Fold("fold-2", T1, T2, 1),
        Fold("fold-3", T2, T3, 2),
    )
    splits = derive_walk_forward_splits(
        points,
        folds,
        minimum_train_size=1,
    )
    assert tuple(split.training_indices for split in splits) == (
        (0,),
        (0,),
        (0, 2),
    )
    assert tuple(split.evaluation_index for split in splits) == (1, 2, 3)


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


def test_adapter_rejects_impossible_label_reveal_order():
    points = (
        Point(T0, T0),
        Point(T1, T0),
        Point(T2, T2),
    )
    with pytest.raises(
        ReproducibilityManifestError,
        match="target_reveal_at must not precede observed_at",
    ):
        derive_walk_forward_splits(
            points,
            (Fold("fold-2", T1, T2, 2),),
        )


def test_adapter_rejects_partial_fold_coverage_of_governed_population():
    points = (
        Point(T0, T0),
        Point(T1, T1),
        Point(T2, T2),
        Point(T3, T3),
    )
    with pytest.raises(
        ReproducibilityManifestError,
        match="cover the final governed input",
    ):
        derive_walk_forward_splits(
            points,
            (Fold("fold-2", T1, T2, 2),),
        )

def test_adapter_honors_explicit_minimum_train_size():
    points = (
        Point(T0, T0),
        Point(T1, T1),
        Point(T2, T2),
        Point(T3, T3),
        Point(T4, T4),
    )
    folds = (
        Fold("fold-3", T2, T3, 3),
        Fold("fold-4", T3, T4, 4),
    )

    splits = derive_walk_forward_splits(
        points,
        folds,
        minimum_train_size=3,
    )

    assert tuple(split.evaluation_index for split in splits) == (3, 4)


@pytest.mark.parametrize("bad_minimum_train_size", [0, True, 1.0])
def test_adapter_rejects_noncanonical_minimum_train_size(bad_minimum_train_size):
    points = (
        Point(T0, T0),
        Point(T1, T1),
        Point(T2, T2),
    )
    with pytest.raises(
        ReproducibilityManifestError,
        match="minimum_train_size must be a positive integer",
    ):
        derive_walk_forward_splits(
            points,
            (Fold("fold-2", T1, T2, 2),),
            minimum_train_size=bad_minimum_train_size,
        )

