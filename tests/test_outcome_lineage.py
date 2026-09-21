from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from autosport.outcome_lineage import (
    OutcomeLineageClassification,
    OutcomeObservation,
    classify_outcome_transition,
)


T0 = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)


def _observation(**changes: object) -> OutcomeObservation:
    values: dict[str, object] = {
        "event_id": "event:match-42",
        "source": "provider-a",
        "source_outcome_id": "outcome:42",
        "value": "HOME_WIN",
        "revision": 3,
        "effective_at": T0,
        "observed_at": T0 + timedelta(seconds=1),
    }
    values.update(changes)
    return OutcomeObservation(**values)  # type: ignore[arg-type]


def test_first_seen_is_accepted_without_reading_wall_clock() -> None:
    incoming = _observation()

    first = classify_outcome_transition(None, incoming)
    repeated = classify_outcome_transition(None, incoming)

    assert first == repeated
    assert first.classification is OutcomeLineageClassification.FIRST_SEEN
    assert first.accepted is True


def test_same_semantic_revision_reobserved_later_is_idempotent_duplicate() -> None:
    previous = _observation()
    incoming = replace(previous, observed_at=previous.observed_at + timedelta(hours=2))

    decision = classify_outcome_transition(previous, incoming)

    assert decision.classification is OutcomeLineageClassification.DUPLICATE
    assert decision.accepted is True


def test_same_revision_with_changed_value_fails_closed_as_conflict() -> None:
    previous = _observation()
    incoming = replace(previous, value="AWAY_WIN", observed_at=previous.observed_at + timedelta(seconds=1))

    decision = classify_outcome_transition(previous, incoming)

    assert decision.classification is OutcomeLineageClassification.CONFLICT
    assert decision.accepted is False


def test_same_revision_with_changed_effective_time_is_conflict_not_revision() -> None:
    previous = _observation()
    incoming = replace(
        previous,
        effective_at=previous.effective_at + timedelta(seconds=1),
        observed_at=previous.observed_at + timedelta(seconds=1),
    )

    decision = classify_outcome_transition(previous, incoming)

    assert decision.classification is OutcomeLineageClassification.CONFLICT
    assert decision.accepted is False


def test_lower_revision_is_stale_even_when_observed_later() -> None:
    previous = _observation()
    incoming = replace(
        previous,
        revision=previous.revision - 1,
        observed_at=previous.observed_at + timedelta(days=1),
    )

    decision = classify_outcome_transition(previous, incoming)

    assert decision.classification is OutcomeLineageClassification.STALE
    assert decision.accepted is False


def test_newer_revision_with_monotonic_causal_times_is_accepted() -> None:
    previous = _observation()
    incoming = replace(
        previous,
        value="DRAW",
        revision=previous.revision + 1,
        effective_at=previous.effective_at + timedelta(minutes=2),
        observed_at=previous.observed_at + timedelta(minutes=3),
    )

    decision = classify_outcome_transition(previous, incoming)

    assert decision.classification is OutcomeLineageClassification.REVISION
    assert decision.accepted is True


def test_newer_revision_may_keep_same_effective_time() -> None:
    previous = _observation()
    incoming = replace(
        previous,
        value="DRAW",
        revision=previous.revision + 1,
        observed_at=previous.observed_at + timedelta(seconds=1),
    )

    decision = classify_outcome_transition(previous, incoming)

    assert decision.classification is OutcomeLineageClassification.REVISION
    assert decision.accepted is True


def test_newer_revision_with_effective_time_regression_is_late() -> None:
    previous = _observation()
    incoming = replace(
        previous,
        value="DRAW",
        revision=previous.revision + 1,
        effective_at=previous.effective_at - timedelta(seconds=1),
        observed_at=previous.observed_at + timedelta(seconds=1),
    )

    decision = classify_outcome_transition(previous, incoming)

    assert decision.classification is OutcomeLineageClassification.LATE
    assert decision.accepted is False


def test_observation_time_regression_is_stale() -> None:
    previous = _observation()
    incoming = replace(previous, observed_at=previous.observed_at - timedelta(microseconds=1))

    decision = classify_outcome_transition(previous, incoming)

    assert decision.classification is OutcomeLineageClassification.STALE
    assert decision.accepted is False


def test_newer_revision_observed_before_previous_is_stale() -> None:
    previous = _observation()
    incoming = replace(
        previous,
        value="DRAW",
        revision=previous.revision + 1,
        observed_at=previous.observed_at - timedelta(microseconds=1),
    )

    decision = classify_outcome_transition(previous, incoming)

    assert decision.classification is OutcomeLineageClassification.STALE
    assert decision.accepted is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", "event:other"),
        ("source", "provider-b"),
        ("source_outcome_id", "outcome:other"),
    ],
)
def test_lineage_identity_mismatch_is_rejected(field: str, value: str) -> None:
    previous = _observation()
    incoming = replace(previous, **{field: value})

    decision = classify_outcome_transition(previous, incoming)

    assert decision.classification is OutcomeLineageClassification.MISMATCH
    assert decision.accepted is False


def test_lineage_key_is_stable_tuple() -> None:
    observation = _observation()

    assert observation.lineage_key == (
        "event:match-42",
        "provider-a",
        "outcome:42",
    )


def test_timestamps_are_normalized_to_utc_without_changing_instant() -> None:
    plus_two = timezone(timedelta(hours=2))
    observation = _observation(
        effective_at=datetime(2026, 9, 20, 20, 0, tzinfo=plus_two),
        observed_at=datetime(2026, 9, 20, 20, 0, 1, tzinfo=plus_two),
    )

    assert observation.effective_at == T0
    assert observation.observed_at == T0 + timedelta(seconds=1)
    assert observation.effective_at.tzinfo is timezone.utc
    assert observation.observed_at.tzinfo is timezone.utc


@pytest.mark.parametrize("field", ["effective_at", "observed_at"])
def test_naive_timestamp_is_rejected(field: str) -> None:
    with pytest.raises(ValueError, match="timezone"):
        _observation(**{field: datetime(2026, 9, 20, 18, 0)})


@pytest.mark.parametrize("revision", [-1, True, 1.5])
def test_invalid_revision_is_rejected(revision: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        _observation(revision=revision)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", ""),
        ("source", " provider-a"),
        ("source_outcome_id", "outcome:42\x00bad"),
        ("value", " "),
    ],
)
def test_noncanonical_identity_or_value_text_is_rejected(field: str, value: str) -> None:
    with pytest.raises(ValueError, match="canonical non-empty text"):
        _observation(**{field: value})


def test_wrong_input_types_fail_loudly_instead_of_becoming_accepted() -> None:
    incoming = _observation()

    with pytest.raises(TypeError, match="incoming"):
        classify_outcome_transition(None, object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="previous"):
        classify_outcome_transition(object(), incoming)  # type: ignore[arg-type]
