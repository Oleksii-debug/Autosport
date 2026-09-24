from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from .learning_environment import EvidenceTruth


SCHEMA = "autosport.learning_reward_truth"
SCHEMA_VERSION = 1
_RECORD_FIELDS = {
    "schema",
    "schema_version",
    "outcome_truth",
    "reward_truth",
    "composed_truth",
    "composition_sha256",
}
_HEX = frozenset("0123456789abcdef")


class LearningRewardTruthError(ValueError):
    """Invalid or relabelled learning truth composition evidence."""


class ComposedLearningTruth(StrEnum):
    """Derived truth class for one outcome/reward pair."""

    OBSERVED = "observed"
    SIMULATED = "simulated"
    MIXED = "mixed"
    UNKNOWN = "unknown"


def _component(value: object, field_name: str) -> EvidenceTruth | None:
    if value is None:
        return None
    if type(value) is not EvidenceTruth:
        raise LearningRewardTruthError(
            f"{field_name} must be canonical EvidenceTruth or None"
        )
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha(value: object, field_name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(character not in _HEX for character in value)
    ):
        raise LearningRewardTruthError(
            f"{field_name} must be lowercase SHA-256 hex"
        )
    return value


def _parse_component(value: object, field_name: str) -> EvidenceTruth | None:
    if value is None:
        return None
    if type(value) is not str:
        raise LearningRewardTruthError(
            f"{field_name} must serialize as canonical text or null"
        )
    try:
        return EvidenceTruth(value)
    except ValueError as exc:
        raise LearningRewardTruthError(
            f"{field_name} is not a canonical EvidenceTruth value"
        ) from exc


@dataclass(frozen=True, slots=True)
class LearningTruthComposition:
    """Truth projection only; never reward, promotion, or execution authority.

    Component truth stays canonical to ``learning_environment.EvidenceTruth``.
    MIXED and UNKNOWN are deliberately derived-only states and therefore cannot
    be supplied as primitive outcome/reward truth labels through this type.
    """

    outcome_truth: EvidenceTruth | None
    reward_truth: EvidenceTruth | None

    def __post_init__(self) -> None:
        _component(self.outcome_truth, "outcome_truth")
        _component(self.reward_truth, "reward_truth")

    @property
    def composed_truth(self) -> ComposedLearningTruth:
        outcome = self.outcome_truth
        reward = self.reward_truth
        if outcome is None or reward is None:
            return ComposedLearningTruth.UNKNOWN
        if outcome is EvidenceTruth.OBSERVED and reward is EvidenceTruth.OBSERVED:
            return ComposedLearningTruth.OBSERVED
        if outcome is EvidenceTruth.SIMULATED and reward is EvidenceTruth.SIMULATED:
            return ComposedLearningTruth.SIMULATED
        return ComposedLearningTruth.MIXED

    @property
    def fully_observed(self) -> bool:
        return self.composed_truth is ComposedLearningTruth.OBSERVED

    def payload(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "outcome_truth": (
                None if self.outcome_truth is None else self.outcome_truth.value
            ),
            "reward_truth": (
                None if self.reward_truth is None else self.reward_truth.value
            ),
            "composed_truth": self.composed_truth.value,
        }

    @property
    def composition_sha256(self) -> str:
        return _digest(self.payload())

    def to_record(self) -> dict[str, object]:
        return {
            **self.payload(),
            "composition_sha256": self.composition_sha256,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "LearningTruthComposition":
        if type(record) is not dict:
            raise LearningRewardTruthError(
                "learning truth record must be an exact JSON object"
            )
        if set(record) != _RECORD_FIELDS:
            raise LearningRewardTruthError(
                "learning truth record fields do not match schema"
            )
        if record.get("schema") != SCHEMA:
            raise LearningRewardTruthError("learning truth schema mismatch")
        version = record.get("schema_version")
        if type(version) is not int or version != SCHEMA_VERSION:
            raise LearningRewardTruthError(
                "learning truth schema version mismatch"
            )

        composition = cls(
            outcome_truth=_parse_component(
                record["outcome_truth"],
                "outcome_truth",
            ),
            reward_truth=_parse_component(
                record["reward_truth"],
                "reward_truth",
            ),
        )
        raw_composed = record["composed_truth"]
        if type(raw_composed) is not str:
            raise LearningRewardTruthError(
                "composed_truth must serialize as canonical text"
            )
        try:
            claimed_composed = ComposedLearningTruth(raw_composed)
        except ValueError as exc:
            raise LearningRewardTruthError(
                "composed_truth is not canonical"
            ) from exc
        if claimed_composed is not composition.composed_truth:
            raise LearningRewardTruthError(
                "serialized composed truth conflicts with canonical derivation"
            )
        claimed_digest = _sha(
            record["composition_sha256"],
            "composition_sha256",
        )
        if claimed_digest != composition.composition_sha256:
            raise LearningRewardTruthError(
                "learning truth composition digest mismatch"
            )
        return composition
