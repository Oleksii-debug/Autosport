from __future__ import annotations

import pytest

from autosport.learning_environment import EvidenceTruth
from autosport.learning_reward_truth import (
    ComposedLearningTruth,
    LearningRewardTruthError,
    LearningTruthComposition,
)


def compose(
    outcome: EvidenceTruth | None,
    reward: EvidenceTruth | None,
) -> LearningTruthComposition:
    return LearningTruthComposition(outcome_truth=outcome, reward_truth=reward)


def test_observed_plus_observed_is_observed() -> None:
    value = compose(EvidenceTruth.OBSERVED, EvidenceTruth.OBSERVED)
    assert value.composed_truth is ComposedLearningTruth.OBSERVED
    assert value.fully_observed is True


def test_simulated_plus_simulated_is_simulated() -> None:
    value = compose(EvidenceTruth.SIMULATED, EvidenceTruth.SIMULATED)
    assert value.composed_truth is ComposedLearningTruth.SIMULATED
    assert value.fully_observed is False


def test_observed_plus_simulated_is_mixed() -> None:
    value = compose(EvidenceTruth.OBSERVED, EvidenceTruth.SIMULATED)
    assert value.composed_truth is ComposedLearningTruth.MIXED
    assert value.fully_observed is False


def test_simulated_plus_observed_is_mixed() -> None:
    value = compose(EvidenceTruth.SIMULATED, EvidenceTruth.OBSERVED)
    assert value.composed_truth is ComposedLearningTruth.MIXED
    assert value.fully_observed is False


@pytest.mark.parametrize(
    ("outcome", "reward"),
    [
        (None, EvidenceTruth.OBSERVED),
        (EvidenceTruth.OBSERVED, None),
        (None, EvidenceTruth.SIMULATED),
        (EvidenceTruth.SIMULATED, None),
        (None, None),
    ],
)
def test_missing_component_is_unknown(
    outcome: EvidenceTruth | None,
    reward: EvidenceTruth | None,
) -> None:
    value = compose(outcome, reward)
    assert value.composed_truth is ComposedLearningTruth.UNKNOWN
    assert value.fully_observed is False


@pytest.mark.parametrize("bad", ["observed", "simulated", ComposedLearningTruth.MIXED, True])
def test_primitive_components_require_evidence_truth_or_none(bad: object) -> None:
    with pytest.raises(LearningRewardTruthError):
        LearningTruthComposition(
            outcome_truth=bad,  # type: ignore[arg-type]
            reward_truth=EvidenceTruth.OBSERVED,
        )


@pytest.mark.parametrize(
    ("outcome", "reward"),
    [
        (EvidenceTruth.OBSERVED, EvidenceTruth.OBSERVED),
        (EvidenceTruth.SIMULATED, EvidenceTruth.SIMULATED),
        (EvidenceTruth.OBSERVED, EvidenceTruth.SIMULATED),
        (None, EvidenceTruth.OBSERVED),
    ],
)
def test_record_round_trip_preserves_derived_truth(
    outcome: EvidenceTruth | None,
    reward: EvidenceTruth | None,
) -> None:
    original = compose(outcome, reward)
    restored = LearningTruthComposition.from_record(original.to_record())
    assert restored == original
    assert restored.composed_truth is original.composed_truth
    assert restored.composition_sha256 == original.composition_sha256


def test_mixed_record_cannot_be_relabelled_observed() -> None:
    record = compose(EvidenceTruth.OBSERVED, EvidenceTruth.SIMULATED).to_record()
    record["composed_truth"] = "observed"
    with pytest.raises(LearningRewardTruthError, match="conflicts"):
        LearningTruthComposition.from_record(record)


def test_unknown_record_cannot_be_relabelled_observed() -> None:
    record = compose(None, EvidenceTruth.OBSERVED).to_record()
    record["composed_truth"] = "observed"
    with pytest.raises(LearningRewardTruthError, match="conflicts"):
        LearningTruthComposition.from_record(record)


def test_component_tamper_with_old_digest_fails_closed() -> None:
    record = compose(EvidenceTruth.OBSERVED, EvidenceTruth.OBSERVED).to_record()
    record["reward_truth"] = "simulated"
    record["composed_truth"] = "mixed"
    with pytest.raises(LearningRewardTruthError, match="digest mismatch"):
        LearningTruthComposition.from_record(record)


def test_digest_tamper_fails_closed() -> None:
    record = compose(EvidenceTruth.OBSERVED, EvidenceTruth.OBSERVED).to_record()
    record["composition_sha256"] = "a" * 64
    with pytest.raises(LearningRewardTruthError, match="digest mismatch"):
        LearningTruthComposition.from_record(record)


def test_schema_version_bool_alias_fails_closed() -> None:
    record = compose(EvidenceTruth.OBSERVED, EvidenceTruth.OBSERVED).to_record()
    record["schema_version"] = True
    with pytest.raises(LearningRewardTruthError, match="version"):
        LearningTruthComposition.from_record(record)


def test_unknown_record_field_fails_closed() -> None:
    record = compose(EvidenceTruth.OBSERVED, EvidenceTruth.OBSERVED).to_record()
    record["promotion_authorized"] = True
    with pytest.raises(LearningRewardTruthError, match="fields"):
        LearningTruthComposition.from_record(record)


def test_invalid_primitive_mixed_component_fails_closed() -> None:
    record = compose(EvidenceTruth.OBSERVED, EvidenceTruth.SIMULATED).to_record()
    record["outcome_truth"] = "mixed"
    with pytest.raises(LearningRewardTruthError, match="canonical EvidenceTruth"):
        LearningTruthComposition.from_record(record)


def test_digest_is_deterministic_and_component_sensitive() -> None:
    observed = compose(EvidenceTruth.OBSERVED, EvidenceTruth.OBSERVED)
    same = compose(EvidenceTruth.OBSERVED, EvidenceTruth.OBSERVED)
    mixed = compose(EvidenceTruth.OBSERVED, EvidenceTruth.SIMULATED)
    assert observed.composition_sha256 == same.composition_sha256
    assert observed.composition_sha256 != mixed.composition_sha256


def test_record_contains_no_authority_escalation_flags() -> None:
    record = compose(EvidenceTruth.OBSERVED, EvidenceTruth.OBSERVED).to_record()
    forbidden = {
        "promotion_authorized",
        "execution_authorized",
        "real_money_execution",
        "human_tested",
        "nvda_verified",
        "whole_product_complete",
    }
    assert forbidden.isdisjoint(record)
