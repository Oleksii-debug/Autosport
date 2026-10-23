"""Bind forward-campaign populations to canonical learning/evaluation inputs.

This module is a fail-closed evidence adapter.  It neither trains a policy nor
evaluates one.  Instead, it proves that the four populations admitted by
``forward_campaign_population`` are exactly the inputs supplied to the existing
``continual_learning_cycle`` authority.  Non-actions in NEW/REPLAY remain
denominator evidence and can never be converted into synthetic rewards.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final, Mapping

from .continual_learning_cycle import (
    ContinualLearningRecipe,
    FrozenPolicySuite,
    VerifiedLearningExperience,
)
from .forward_campaign_population import (
    ForwardCampaignPopulationManifest,
    ForwardPopulationSample,
    PopulationRole,
)
from .forward_evidence_completeness import DecisionState
from .learning_environment import EvidenceTruth
from .policy_evaluation import PolicyEvaluationCase


SCHEMA: Final = "autosport.forward_learning_population_binding"
SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")


class ForwardLearningPopulationBindingError(ValueError):
    """Campaign populations cannot be proven identical to learning inputs."""


class PopulationConsumer(StrEnum):
    LEARNING_EXPERIENCE = "LEARNING_EXPERIENCE"
    POLICY_EVALUATION_CASE = "POLICY_EVALUATION_CASE"
    NEGATIVE_EVIDENCE = "NEGATIVE_EVIDENCE"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ForwardLearningPopulationBindingError(
            f"{name} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ForwardLearningPopulationBindingError(
            f"{name} must be valid UTF-8"
        ) from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ForwardLearningPopulationBindingError(
            f"{name} must be lowercase SHA-256"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForwardLearningPopulationBindingError(
            f"{name} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardLearningPopulationBindingError(
            f"{name} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ForwardLearningPopulationBindingError(
            "population binding evidence is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _strict_mapping(value: object, fields: set[str], name: str) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ForwardLearningPopulationBindingError(f"{name} fields mismatch")
    return value


@dataclass(frozen=True, slots=True)
class ForwardLearningMemberBinding:
    """One exact campaign-sample to canonical-consumer binding."""

    role: PopulationRole
    sample_id: str
    campaign_sample_sha256: str
    source_evidence_sha256: str
    consumer: PopulationConsumer
    consumer_evidence_sha256: str

    def __post_init__(self) -> None:
        if type(self.role) is not PopulationRole:
            raise ForwardLearningPopulationBindingError(
                "member role must be exact PopulationRole"
            )
        _text(self.sample_id, "sample_id")
        _sha256(self.campaign_sample_sha256, "campaign_sample_sha256")
        _sha256(self.source_evidence_sha256, "source_evidence_sha256")
        if type(self.consumer) is not PopulationConsumer:
            raise ForwardLearningPopulationBindingError(
                "member consumer must be exact PopulationConsumer"
            )
        _sha256(self.consumer_evidence_sha256, "consumer_evidence_sha256")
        if self.role in {PopulationRole.NEW, PopulationRole.REPLAY}:
            if self.consumer not in {
                PopulationConsumer.LEARNING_EXPERIENCE,
                PopulationConsumer.NEGATIVE_EVIDENCE,
            }:
                raise ForwardLearningPopulationBindingError(
                    "training roles require learning or negative-evidence consumer"
                )
        elif self.consumer is not PopulationConsumer.POLICY_EVALUATION_CASE:
            raise ForwardLearningPopulationBindingError(
                "evaluation roles require policy-evaluation consumer"
            )

    def canonical_payload(self) -> dict[str, str]:
        return {
            "role": self.role.value,
            "sample_id": self.sample_id,
            "campaign_sample_sha256": self.campaign_sample_sha256,
            "source_evidence_sha256": self.source_evidence_sha256,
            "consumer": self.consumer.value,
            "consumer_evidence_sha256": self.consumer_evidence_sha256,
        }

    @property
    def member_sha256(self) -> str:
        return _digest(self.canonical_payload())

    @classmethod
    def from_payload(cls, payload: object) -> "ForwardLearningMemberBinding":
        raw = _strict_mapping(
            payload,
            {
                "role",
                "sample_id",
                "campaign_sample_sha256",
                "source_evidence_sha256",
                "consumer",
                "consumer_evidence_sha256",
            },
            "population member binding",
        )
        try:
            role = PopulationRole(raw["role"])
            consumer = PopulationConsumer(raw["consumer"])
        except (TypeError, ValueError) as exc:
            raise ForwardLearningPopulationBindingError(
                "population member enum value is invalid"
            ) from exc
        return cls(
            role=role,
            sample_id=raw["sample_id"],
            campaign_sample_sha256=raw["campaign_sample_sha256"],
            source_evidence_sha256=raw["source_evidence_sha256"],
            consumer=consumer,
            consumer_evidence_sha256=raw["consumer_evidence_sha256"],
        )


@dataclass(frozen=True, slots=True)
class ForwardLearningPopulationBinding:
    """Restart-verifiable receipt; never a promotion or execution authority."""

    campaign_manifest_sha256: str
    scientific_protocol_sha256: str
    recipe_sha256: str
    retention_suite_sha256: str
    holdout_suite_sha256: str
    bound_at: str
    members: tuple[ForwardLearningMemberBinding, ...]
    role_binding_sha256s: tuple[tuple[str, str], ...]
    negative_evidence_sha256: str
    profitability_claim: bool = False
    promotion_authorized: bool = False
    real_money_execution: bool = False

    def __post_init__(self) -> None:
        for name in (
            "campaign_manifest_sha256",
            "scientific_protocol_sha256",
            "recipe_sha256",
            "retention_suite_sha256",
            "holdout_suite_sha256",
            "negative_evidence_sha256",
        ):
            _sha256(getattr(self, name), name)
        _instant(self.bound_at, "bound_at")
        if type(self.members) is not tuple or not self.members:
            raise ForwardLearningPopulationBindingError(
                "binding members must be a non-empty tuple"
            )
        if any(type(member) is not ForwardLearningMemberBinding for member in self.members):
            raise ForwardLearningPopulationBindingError(
                "binding members must be exact ForwardLearningMemberBinding values"
            )
        if self.members != tuple(sorted(self.members, key=lambda value: value.sample_id)):
            raise ForwardLearningPopulationBindingError(
                "binding members must be sample-id ordered"
            )
        sample_ids = tuple(member.sample_id for member in self.members)
        if len(sample_ids) != len(set(sample_ids)):
            raise ForwardLearningPopulationBindingError(
                "binding member sample identities must be unique"
            )
        expected_roles = tuple(
            (
                role.value,
                _digest(
                    {
                        "role": role.value,
                        "member_sha256s": [
                            member.member_sha256
                            for member in self.members
                            if member.role is role
                        ],
                    }
                ),
            )
            for role in PopulationRole
        )
        if self.role_binding_sha256s != expected_roles:
            raise ForwardLearningPopulationBindingError(
                "role binding digest mismatch"
            )
        expected_negative = _digest(
            {
                "negative_member_sha256s": [
                    member.member_sha256
                    for member in self.members
                    if member.consumer is PopulationConsumer.NEGATIVE_EVIDENCE
                ]
            }
        )
        if self.negative_evidence_sha256 != expected_negative:
            raise ForwardLearningPopulationBindingError(
                "negative-evidence digest mismatch"
            )
        for name in (
            "profitability_claim",
            "promotion_authorized",
            "real_money_execution",
        ):
            if getattr(self, name) is not False:
                raise ForwardLearningPopulationBindingError(
                    f"{name} must remain false for population binding evidence"
                )

    def payload_without_binding_sha(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "campaign_manifest_sha256": self.campaign_manifest_sha256,
            "scientific_protocol_sha256": self.scientific_protocol_sha256,
            "recipe_sha256": self.recipe_sha256,
            "retention_suite_sha256": self.retention_suite_sha256,
            "holdout_suite_sha256": self.holdout_suite_sha256,
            "bound_at": _timestamp(self.bound_at, "bound_at"),
            "members": [member.canonical_payload() for member in self.members],
            "role_binding_sha256s": [list(value) for value in self.role_binding_sha256s],
            "negative_evidence_sha256": self.negative_evidence_sha256,
            "profitability_claim": self.profitability_claim,
            "promotion_authorized": self.promotion_authorized,
            "real_money_execution": self.real_money_execution,
        }

    @property
    def binding_sha256(self) -> str:
        return _digest(self.payload_without_binding_sha())

    def canonical_payload(self) -> dict[str, object]:
        return {
            **self.payload_without_binding_sha(),
            "binding_sha256": self.binding_sha256,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "ForwardLearningPopulationBinding":
        raw = _strict_mapping(
            payload,
            {
                "schema",
                "schema_version",
                "campaign_manifest_sha256",
                "scientific_protocol_sha256",
                "recipe_sha256",
                "retention_suite_sha256",
                "holdout_suite_sha256",
                "bound_at",
                "members",
                "role_binding_sha256s",
                "negative_evidence_sha256",
                "profitability_claim",
                "promotion_authorized",
                "real_money_execution",
                "binding_sha256",
            },
            "forward learning population binding",
        )
        if raw["schema"] != SCHEMA or raw["schema_version"] != SCHEMA_VERSION:
            raise ForwardLearningPopulationBindingError(
                "population binding schema is unsupported"
            )
        if type(raw["members"]) is not list:
            raise ForwardLearningPopulationBindingError(
                "population binding members are malformed"
            )
        role_pairs = raw["role_binding_sha256s"]
        if type(role_pairs) is not list or any(
            type(value) is not list or len(value) != 2 for value in role_pairs
        ):
            raise ForwardLearningPopulationBindingError(
                "role binding digests are malformed"
            )
        value = cls(
            campaign_manifest_sha256=raw["campaign_manifest_sha256"],
            scientific_protocol_sha256=raw["scientific_protocol_sha256"],
            recipe_sha256=raw["recipe_sha256"],
            retention_suite_sha256=raw["retention_suite_sha256"],
            holdout_suite_sha256=raw["holdout_suite_sha256"],
            bound_at=raw["bound_at"],
            members=tuple(
                ForwardLearningMemberBinding.from_payload(item)
                for item in raw["members"]
            ),
            role_binding_sha256s=tuple((item[0], item[1]) for item in role_pairs),
            negative_evidence_sha256=raw["negative_evidence_sha256"],
            profitability_claim=raw["profitability_claim"],
            promotion_authorized=raw["promotion_authorized"],
            real_money_execution=raw["real_money_execution"],
        )
        if raw["binding_sha256"] != value.binding_sha256:
            raise ForwardLearningPopulationBindingError(
                "population binding digest mismatch"
            )
        return value


def _by_sample_id(values: tuple[object, ...], *, name: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for value in values:
        sample_id = value.sample_id
        if sample_id in result:
            raise ForwardLearningPopulationBindingError(
                f"{name} sample identities must be unique"
            )
        result[sample_id] = value
    return result


def _same_instant(left: str, right: str) -> bool:
    return _instant(left, "left timestamp") == _instant(right, "right timestamp")


def _bind_experience(
    sample: ForwardPopulationSample,
    experience: VerifiedLearningExperience,
) -> ForwardLearningMemberBinding:
    if experience.source_evidence_sha256 != sample.source_evidence_sha256:
        raise ForwardLearningPopulationBindingError(
            f"training source evidence mismatch for {sample.sample_id}"
        )
    if experience.reward.truth is not EvidenceTruth.OBSERVED:
        raise ForwardLearningPopulationBindingError(
            f"training reward must be OBSERVED for {sample.sample_id}"
        )
    if not _same_instant(experience.action.decided_at, sample.decided_at):
        raise ForwardLearningPopulationBindingError(
            f"training decision timestamp mismatch for {sample.sample_id}"
        )
    if not _same_instant(experience.transition.decision_at, sample.decided_at):
        raise ForwardLearningPopulationBindingError(
            f"training transition decision timestamp mismatch for {sample.sample_id}"
        )
    if not _same_instant(experience.reward.available_at, sample.outcome_available_at):
        raise ForwardLearningPopulationBindingError(
            f"training outcome availability mismatch for {sample.sample_id}"
        )
    if not _same_instant(experience.transition.resolved_at, sample.settled_at):
        raise ForwardLearningPopulationBindingError(
            f"training settlement timestamp mismatch for {sample.sample_id}"
        )
    if experience.reward.reward != sample.observed_reward:
        raise ForwardLearningPopulationBindingError(
            f"training observed reward mismatch for {sample.sample_id}"
        )
    return ForwardLearningMemberBinding(
        role=sample.role,
        sample_id=sample.sample_id,
        campaign_sample_sha256=sample.sample_sha256,
        source_evidence_sha256=sample.source_evidence_sha256,
        consumer=PopulationConsumer.LEARNING_EXPERIENCE,
        consumer_evidence_sha256=experience.experience_id,
    )


def _bind_case(
    sample: ForwardPopulationSample,
    case: PolicyEvaluationCase,
) -> ForwardLearningMemberBinding:
    if case.source_evidence_sha256 != sample.source_evidence_sha256:
        raise ForwardLearningPopulationBindingError(
            f"evaluation source evidence mismatch for {sample.sample_id}"
        )
    if case.reward_truth is not EvidenceTruth.OBSERVED:
        raise ForwardLearningPopulationBindingError(
            f"forward evaluation reward must be OBSERVED for {sample.sample_id}"
        )
    if case.regime_id != sample.regime_id:
        raise ForwardLearningPopulationBindingError(
            f"evaluation regime mismatch for {sample.sample_id}"
        )
    if not _same_instant(case.observed_at, sample.observed_at):
        raise ForwardLearningPopulationBindingError(
            f"evaluation observation timestamp mismatch for {sample.sample_id}"
        )
    if not _same_instant(case.reward_available_at, sample.outcome_available_at):
        raise ForwardLearningPopulationBindingError(
            f"evaluation outcome availability mismatch for {sample.sample_id}"
        )
    return ForwardLearningMemberBinding(
        role=sample.role,
        sample_id=sample.sample_id,
        campaign_sample_sha256=sample.sample_sha256,
        source_evidence_sha256=sample.source_evidence_sha256,
        consumer=PopulationConsumer.POLICY_EVALUATION_CASE,
        consumer_evidence_sha256=_digest(case.canonical_payload()),
    )


def bind_forward_learning_populations(
    manifest: ForwardCampaignPopulationManifest,
    *,
    new_experiences: tuple[VerifiedLearningExperience, ...],
    replay_experiences: tuple[VerifiedLearningExperience, ...],
    retention_suite: FrozenPolicySuite,
    holdout_suite: FrozenPolicySuite,
    recipe: ContinualLearningRecipe,
    bound_at: str,
) -> ForwardLearningPopulationBinding:
    """Prove exact four-way population use before a continual-learning cycle."""

    if type(manifest) is not ForwardCampaignPopulationManifest:
        raise TypeError("manifest must be exact ForwardCampaignPopulationManifest")
    if type(new_experiences) is not tuple or any(
        type(value) is not VerifiedLearningExperience for value in new_experiences
    ):
        raise TypeError("new_experiences must contain exact VerifiedLearningExperience values")
    if type(replay_experiences) is not tuple or any(
        type(value) is not VerifiedLearningExperience for value in replay_experiences
    ):
        raise TypeError("replay_experiences must contain exact VerifiedLearningExperience values")
    if type(retention_suite) is not FrozenPolicySuite:
        raise TypeError("retention_suite must be exact FrozenPolicySuite")
    if type(holdout_suite) is not FrozenPolicySuite:
        raise TypeError("holdout_suite must be exact FrozenPolicySuite")
    if type(recipe) is not ContinualLearningRecipe:
        raise TypeError("recipe must be exact ContinualLearningRecipe")
    if _instant(bound_at, "bound_at") < _instant(manifest.created_at, "manifest.created_at"):
        raise ForwardLearningPopulationBindingError(
            "population binding cannot predate campaign manifest"
        )
    if retention_suite.role != "retention" or holdout_suite.role != "holdout":
        raise ForwardLearningPopulationBindingError(
            "frozen evaluation suite roles are reversed or invalid"
        )
    if recipe.retention_suite_sha256 != retention_suite.suite_sha256:
        raise ForwardLearningPopulationBindingError(
            "recipe retention suite binding mismatch"
        )
    if recipe.holdout_suite_sha256 != holdout_suite.suite_sha256:
        raise ForwardLearningPopulationBindingError(
            "recipe holdout suite binding mismatch"
        )

    new_by_id = _by_sample_id(new_experiences, name="new experience")
    replay_by_id = _by_sample_id(replay_experiences, name="replay experience")
    retention_by_id = _by_sample_id(retention_suite.cases, name="retention case")
    holdout_by_id = _by_sample_id(holdout_suite.cases, name="holdout case")
    samples_by_role = {
        role: tuple(sample for sample in manifest.samples if sample.role is role)
        for role in PopulationRole
    }
    expected_new = {
        sample.sample_id
        for sample in samples_by_role[PopulationRole.NEW]
        if sample.decision_state is DecisionState.ACTION
    }
    expected_replay = {
        sample.sample_id
        for sample in samples_by_role[PopulationRole.REPLAY]
        if sample.decision_state is DecisionState.ACTION
    }
    expected_retention = {
        sample.sample_id for sample in samples_by_role[PopulationRole.RETENTION]
    }
    expected_holdout = {
        sample.sample_id for sample in samples_by_role[PopulationRole.HOLDOUT]
    }
    if not expected_new or not expected_replay:
        raise ForwardLearningPopulationBindingError(
            "NEW and REPLAY each require at least one observed ACTION for the canonical cycle"
        )
    if len(expected_new) + len(expected_replay) > recipe.max_updates:
        raise ForwardLearningPopulationBindingError(
            "bound training populations exceed the frozen recipe update budget"
        )
    for actual, expected, name in (
        (set(new_by_id), expected_new, "new learning"),
        (set(replay_by_id), expected_replay, "replay learning"),
        (set(retention_by_id), expected_retention, "retention evaluation"),
        (set(holdout_by_id), expected_holdout, "holdout evaluation"),
    ):
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ForwardLearningPopulationBindingError(
                f"{name} population mismatch; missing={missing!r}; extra={extra!r}"
            )

    members: list[ForwardLearningMemberBinding] = []
    for sample in manifest.samples:
        if sample.role is PopulationRole.NEW:
            if sample.decision_state is DecisionState.ACTION:
                members.append(_bind_experience(sample, new_by_id[sample.sample_id]))
            else:
                members.append(_negative_binding(sample))
        elif sample.role is PopulationRole.REPLAY:
            if sample.decision_state is DecisionState.ACTION:
                members.append(_bind_experience(sample, replay_by_id[sample.sample_id]))
            else:
                members.append(_negative_binding(sample))
        elif sample.role is PopulationRole.RETENTION:
            members.append(_bind_case(sample, retention_by_id[sample.sample_id]))
        else:
            members.append(_bind_case(sample, holdout_by_id[sample.sample_id]))

    ordered = tuple(sorted(members, key=lambda value: value.sample_id))
    role_digests = tuple(
        (
            role.value,
            _digest(
                {
                    "role": role.value,
                    "member_sha256s": [
                        member.member_sha256 for member in ordered if member.role is role
                    ],
                }
            ),
        )
        for role in PopulationRole
    )
    negative_digest = _digest(
        {
            "negative_member_sha256s": [
                member.member_sha256
                for member in ordered
                if member.consumer is PopulationConsumer.NEGATIVE_EVIDENCE
            ]
        }
    )
    return ForwardLearningPopulationBinding(
        campaign_manifest_sha256=manifest.manifest_sha256,
        scientific_protocol_sha256=manifest.scientific_protocol_sha256,
        recipe_sha256=recipe.recipe_sha256,
        retention_suite_sha256=retention_suite.suite_sha256,
        holdout_suite_sha256=holdout_suite.suite_sha256,
        bound_at=_timestamp(bound_at, "bound_at"),
        members=ordered,
        role_binding_sha256s=role_digests,
        negative_evidence_sha256=negative_digest,
    )


def _negative_binding(sample: ForwardPopulationSample) -> ForwardLearningMemberBinding:
    if sample.decision_state is DecisionState.ACTION:
        raise ForwardLearningPopulationBindingError(
            "ACTION sample cannot be bound as negative evidence"
        )
    return ForwardLearningMemberBinding(
        role=sample.role,
        sample_id=sample.sample_id,
        campaign_sample_sha256=sample.sample_sha256,
        source_evidence_sha256=sample.source_evidence_sha256,
        consumer=PopulationConsumer.NEGATIVE_EVIDENCE,
        consumer_evidence_sha256=sample.terminal_evidence_sha256,
    )


def verify_forward_learning_population_binding(
    binding: ForwardLearningPopulationBinding,
    manifest: ForwardCampaignPopulationManifest,
    *,
    new_experiences: tuple[VerifiedLearningExperience, ...],
    replay_experiences: tuple[VerifiedLearningExperience, ...],
    retention_suite: FrozenPolicySuite,
    holdout_suite: FrozenPolicySuite,
    recipe: ContinualLearningRecipe,
) -> bool:
    """Rebuild the receipt from canonical authorities and require exact identity."""

    if type(binding) is not ForwardLearningPopulationBinding:
        raise TypeError("binding must be exact ForwardLearningPopulationBinding")
    restored = ForwardLearningPopulationBinding.from_payload(
        json.loads(_canonical_bytes(binding.canonical_payload()).decode("utf-8"))
    )
    rebuilt = bind_forward_learning_populations(
        manifest,
        new_experiences=new_experiences,
        replay_experiences=replay_experiences,
        retention_suite=retention_suite,
        holdout_suite=holdout_suite,
        recipe=recipe,
        bound_at=binding.bound_at,
    )
    if restored != binding or rebuilt != binding:
        raise ForwardLearningPopulationBindingError(
            "population binding restart or authority replay mismatch"
        )
    return True


__all__ = [
    "ForwardLearningMemberBinding",
    "ForwardLearningPopulationBinding",
    "ForwardLearningPopulationBindingError",
    "PopulationConsumer",
    "bind_forward_learning_populations",
    "verify_forward_learning_population_binding",
]
