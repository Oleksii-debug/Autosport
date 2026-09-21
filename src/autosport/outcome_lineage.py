"""Deterministic, fail-closed classification of outcome lineage revisions.

Outcome feeds can replay old observations, revise a previously published result, or
contradict themselves at the same revision. This module intentionally does not pick
a winner and does not mutate settlement/execution state. It provides a small pure
model callers can use to decide whether an observation is a safe continuation of one
already accepted into a lineage.

Revision is the ordering authority. Timestamps are causal guards: an accepted newer
revision may not move either the outcome's effective time or the authority's observed
time backwards. Same-revision payload changes are conflicts rather than revisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Final


class OutcomeLineageClassification(str, Enum):
    """Closed set of transition outcomes for one source-owned outcome lineage."""

    FIRST_SEEN = "first_seen"
    DUPLICATE = "duplicate"
    REVISION = "revision"
    STALE = "stale"
    LATE = "late"
    CONFLICT = "conflict"
    MISMATCH = "mismatch"


_ACCEPTED: Final = frozenset(
    {
        OutcomeLineageClassification.FIRST_SEEN,
        OutcomeLineageClassification.DUPLICATE,
        OutcomeLineageClassification.REVISION,
    }
)


def _canonical_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be canonical non-empty text")
    value.encode("utf-8", errors="strict")
    return value


def _aware_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class OutcomeObservation:
    """One source-owned observation of a versioned event outcome."""

    event_id: str
    source: str
    source_outcome_id: str
    value: str
    revision: int
    effective_at: datetime
    observed_at: datetime

    def __post_init__(self) -> None:
        _canonical_text(self.event_id, "event_id")
        _canonical_text(self.source, "source")
        _canonical_text(self.source_outcome_id, "source_outcome_id")
        _canonical_text(self.value, "value")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        object.__setattr__(self, "effective_at", _aware_utc(self.effective_at, "effective_at"))
        object.__setattr__(self, "observed_at", _aware_utc(self.observed_at, "observed_at"))

    @property
    def lineage_key(self) -> tuple[str, str, str]:
        """Stable identity of the source-owned outcome lineage."""

        return (self.event_id, self.source, self.source_outcome_id)

    @property
    def semantic_revision(self) -> tuple[int, datetime, str]:
        """Fields whose same-revision disagreement must fail closed."""

        return (self.revision, self.effective_at, self.value)


@dataclass(frozen=True, slots=True)
class OutcomeLineageDecision:
    """Pure classification result; it carries no mutation authority."""

    classification: OutcomeLineageClassification
    reason: str

    @property
    def accepted(self) -> bool:
        return self.classification in _ACCEPTED


def _decision(
    classification: OutcomeLineageClassification,
    reason: str,
) -> OutcomeLineageDecision:
    return OutcomeLineageDecision(classification=classification, reason=reason)


def classify_outcome_transition(
    previous: OutcomeObservation | None,
    incoming: OutcomeObservation,
) -> OutcomeLineageDecision:
    """Classify ``incoming`` relative to the last accepted observation.

    The function is deliberately conservative:

    * lineage identity must never change;
    * lower revisions and causal observation-time regressions are stale;
    * same-revision semantic changes are conflicts;
    * newer revisions whose effective time moves backwards are late and rejected;
    * a semantically identical replay at the same revision is an idempotent duplicate;
    * only a causally monotonic higher revision is an accepted revision.

    No wall clock is read and no threshold is guessed, so repeated evaluation of the
    same inputs is deterministic.
    """

    if not isinstance(incoming, OutcomeObservation):
        raise TypeError("incoming must be OutcomeObservation")
    if previous is None:
        return _decision(
            OutcomeLineageClassification.FIRST_SEEN,
            "no prior accepted observation exists for this lineage",
        )
    if not isinstance(previous, OutcomeObservation):
        raise TypeError("previous must be OutcomeObservation or None")

    if incoming.lineage_key != previous.lineage_key:
        return _decision(
            OutcomeLineageClassification.MISMATCH,
            "incoming observation belongs to a different event/source outcome lineage",
        )

    if incoming.revision < previous.revision:
        return _decision(
            OutcomeLineageClassification.STALE,
            "incoming revision is older than the accepted revision",
        )

    if incoming.revision == previous.revision:
        if incoming.effective_at != previous.effective_at or incoming.value != previous.value:
            return _decision(
                OutcomeLineageClassification.CONFLICT,
                "same revision disagrees with the accepted semantic outcome",
            )
        if incoming.observed_at < previous.observed_at:
            return _decision(
                OutcomeLineageClassification.STALE,
                "same-revision replay predates the accepted observation time",
            )
        return _decision(
            OutcomeLineageClassification.DUPLICATE,
            "same revision and semantic outcome already accepted",
        )

    if incoming.effective_at < previous.effective_at:
        return _decision(
            OutcomeLineageClassification.LATE,
            "newer revision moves the outcome effective time backwards",
        )
    if incoming.observed_at < previous.observed_at:
        return _decision(
            OutcomeLineageClassification.STALE,
            "newer revision was observed before the accepted observation",
        )

    return _decision(
        OutcomeLineageClassification.REVISION,
        "newer revision preserves causal effective and observation time",
    )
