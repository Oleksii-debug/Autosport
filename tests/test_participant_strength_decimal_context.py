from __future__ import annotations

from decimal import Decimal, localcontext

from autosport.opponent_intelligence import RatingSnapshot, SnapshotState
from autosport.participant_identity import IdentityView
from autosport.participant_strength import (
    HistogramCalibratedStrengthFactory,
    HistogramCalibratedStrengthModel,
    RatingDifferenceBaselineModel,
    StrengthSnapshotPair,
)
from autosport.strategy_model_factory import TrainingPoint


DECISION_AT = "2026-01-02T00:00:00Z"
SNAPSHOT_AT = "2026-01-01T00:00:00Z"


def _snapshot(
    participant_id: str,
    *,
    snapshot_sha256: str,
    rating: str,
) -> RatingSnapshot:
    return RatingSnapshot(
        snapshot_id=snapshot_sha256,
        participant_entity_id=participant_id,
        sport_id="tennis",
        league_id="atp",
        market_context_id="match-winner",
        view=IdentityView.AS_KNOWN_AT_DECISION,
        causal_cutoff=SNAPSHOT_AT,
        published_at=SNAPSHOT_AT,
        algorithm_family="test-rating",
        algorithm_version="1",
        config_sha256="c" * 64,
        min_support=1,
        max_age_seconds=3600,
        code_sha256="d" * 64,
        dependency_sha256="e" * 64,
        predecessor_snapshot_ids=(),
        input_performance_ids=(),
        input_digest="f" * 64,
        support=1,
        effective_sample=1,
        opponent_count=1,
        rating=rating,
        uncertainty="0.1",
        state=SnapshotState.SUPPORTED,
    )


def test_strength_snapshot_feature_is_independent_of_ambient_decimal_context() -> None:
    pair = StrengthSnapshotPair(
        _snapshot(
            "participant-a",
            snapshot_sha256="a" * 64,
            rating="0.70000000000000000000000000009",
        ),
        _snapshot(
            "participant-b",
            snapshot_sha256="b" * 64,
            rating="0.40000000000000000000000000001",
        ),
        DECISION_AT,
    )

    with localcontext() as context:
        context.prec = 5
        low_precision = pair.feature

    with localcontext() as context:
        context.prec = 50
        high_precision = pair.feature

    assert low_precision == high_precision


def test_rating_difference_prediction_is_independent_of_ambient_decimal_context() -> None:
    model = RatingDifferenceBaselineModel(
        model_id="baseline-decimal-context",
        training_cutoff=SNAPSHOT_AT,
        training_count=1,
        training_manifest_sha256="a" * 64,
    )
    feature = Decimal("0.12345678901234567890123456789")

    with localcontext() as context:
        context.prec = 5
        low_precision = model.predict_feature(feature, decision_at=DECISION_AT)

    with localcontext() as context:
        context.prec = 50
        high_precision = model.predict_feature(feature, decision_at=DECISION_AT)

    assert low_precision == high_precision


def test_histogram_prediction_bin_is_independent_of_ambient_decimal_context() -> None:
    model = HistogramCalibratedStrengthModel(
        model_id="histogram-decimal-context",
        training_cutoff=SNAPSHOT_AT,
        training_count=2,
        training_manifest_sha256="b" * 64,
        bin_count=10,
        prior_weight="2",
        bin_counts=(0, 0, 0, 0, 0, 1, 1, 0, 0, 0),
        bin_probabilities=(
            None,
            None,
            None,
            None,
            None,
            "0.55",
            "0.65",
            None,
            None,
            None,
        ),
    )
    feature = Decimal("0.1999999999999999999999999999")

    with localcontext() as context:
        context.prec = 5
        low_precision = model.predict_feature(feature, decision_at=DECISION_AT)

    with localcontext() as context:
        context.prec = 50
        high_precision = model.predict_feature(feature, decision_at=DECISION_AT)

    assert low_precision == high_precision


def test_histogram_training_is_independent_of_ambient_decimal_context() -> None:
    points = (
        TrainingPoint(
            observed_at="2026-01-01T00:00:01Z",
            feature=0.12345678901234568,
            target=1.0,
            target_available_at="2026-01-01T00:01:01Z",
            evidence_sha256s=("1" * 64,),
        ),
        TrainingPoint(
            observed_at="2026-01-01T00:00:02Z",
            feature=0.22345678901234567,
            target=0.0,
            target_available_at="2026-01-01T00:01:02Z",
            evidence_sha256s=("2" * 64,),
        ),
        TrainingPoint(
            observed_at="2026-01-01T00:00:03Z",
            feature=0.3234567890123457,
            target=1.0,
            target_available_at="2026-01-01T00:01:03Z",
            evidence_sha256s=("3" * 64,),
        ),
    )
    factory = HistogramCalibratedStrengthFactory(bin_count=2, prior_weight="2")
    cutoff = "2026-01-01T00:02:00Z"

    with localcontext() as context:
        context.prec = 5
        low_precision = factory.fit(
            "histogram-training-decimal-context",
            points,
            training_cutoff=cutoff,
        )

    with localcontext() as context:
        context.prec = 50
        high_precision = factory.fit(
            "histogram-training-decimal-context",
            points,
            training_cutoff=cutoff,
        )

    assert low_precision == high_precision
    assert low_precision.identity_sha256 == high_precision.identity_sha256
