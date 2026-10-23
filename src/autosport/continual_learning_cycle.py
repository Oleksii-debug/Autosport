"""Bounded provider-free continual-learning cycle for transparent policies.

The cycle composes Autosport's existing causal policy update and paired-policy
evaluation authorities.  It does not create a second trainer, scientific
registry, promotion authority, risk authority, or execution authority.

The design deliberately keeps three populations separate:

* new causal experiences, used for the current update;
* replay experiences, retained from earlier cycles to reduce forgetting;
* frozen retention and holdout suites, used only for evaluation.

A successful result is still only a candidate.  Promotion and deployment must
continue through the canonical Strategy/Model Factory and ScientificRegistry.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from .learning_environment import Action, EvidenceTruth, RewardEvidence, Transition
from .policy_evaluation import (
    PolicyEvaluationCase,
    PolicyPairEvaluation,
    QualifiedCounterfactualAuthority,
    evaluate_policy_pair,
    policy_evaluation_cases_manifest_sha256,
)
from .transparent_bandit_policy import BanditPolicyState, PolicyUpdateEvidence


_HEX: Final = frozenset("0123456789abcdef")
_ROLES: Final = frozenset({"retention", "holdout"})


class ContinualLearningCycleError(ValueError):
    """A continual-learning input or result violates the frozen cycle contract."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ContinualLearningCycleError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ContinualLearningCycleError(f"{name} must be valid UTF-8") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ContinualLearningCycleError(f"{name} must be lowercase SHA-256")
    return text


def _decimal(value: object, name: str, *, nonnegative: bool = False) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ContinualLearningCycleError(f"{name} must be a finite exact Decimal")
    if nonnegative and value < 0:
        raise ContinualLearningCycleError(f"{name} must be non-negative")
    return value


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ContinualLearningCycleError("cycle evidence is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class VerifiedLearningExperience:
    """One independently sourced, already-resolved causal training experience."""

    sample_id: str
    source_evidence_sha256: str
    action: Action
    reward: RewardEvidence
    transition: Transition

    def __post_init__(self) -> None:
        _text(self.sample_id, "sample_id")
        _sha256(self.source_evidence_sha256, "source_evidence_sha256")
        if type(self.action) is not Action:
            raise ContinualLearningCycleError("action must be exact Action")
        if type(self.reward) is not RewardEvidence:
            raise ContinualLearningCycleError("reward must be exact RewardEvidence")
        if type(self.transition) is not Transition:
            raise ContinualLearningCycleError("transition must be exact Transition")
        if not (
            self.action.environment_id
            == self.reward.environment_id
            == self.transition.environment_id
        ):
            raise ContinualLearningCycleError("experience environment identity mismatch")
        if self.reward.action_id != self.action.action_id:
            raise ContinualLearningCycleError("experience reward does not bind exact action")
        if self.transition.action_id != self.action.action_id:
            raise ContinualLearningCycleError("experience transition does not bind exact action")
        if self.transition.observation_id != self.action.observation_id:
            raise ContinualLearningCycleError(
                "experience transition does not bind exact observation"
            )
        if self.transition.outcome_id != self.reward.outcome_id:
            raise ContinualLearningCycleError("experience transition outcome mismatch")
        if self.transition.reward_id != self.reward.reward_id:
            raise ContinualLearningCycleError("experience transition reward mismatch")

    @property
    def experience_id(self) -> str:
        return _digest(
            {
                "schema": "autosport.verified_learning_experience",
                "schema_version": 1,
                "sample_id": self.sample_id,
                "source_evidence_sha256": self.source_evidence_sha256,
                "action_id": self.action.action_id,
                "reward_id": self.reward.reward_id,
                "transition_id": self.transition.transition_id,
                "reward_truth": self.reward.truth.value,
                "simulation_model_id": self.reward.simulation_model_id,
            }
        )


@dataclass(frozen=True, slots=True)
class FrozenPolicySuite:
    """Immutable retention or untouched-holdout paired-policy population."""

    suite_id: str
    role: str
    cases: tuple[PolicyEvaluationCase, ...]

    def __post_init__(self) -> None:
        _text(self.suite_id, "suite_id")
        if self.role not in _ROLES:
            raise ContinualLearningCycleError("suite role must be retention or holdout")
        if type(self.cases) is not tuple or not self.cases:
            raise ContinualLearningCycleError("suite cases must be a non-empty tuple")
        if len(self.cases) > 1024:
            raise ContinualLearningCycleError("suite exceeds bounded evaluation population")
        if any(type(case) is not PolicyEvaluationCase for case in self.cases):
            raise ContinualLearningCycleError(
                "suite cases must be exact PolicyEvaluationCase values"
            )
        sample_ids = tuple(case.sample_id for case in self.cases)
        if len(sample_ids) != len(set(sample_ids)):
            raise ContinualLearningCycleError("suite sample identities must be unique")

    @property
    def suite_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.frozen_policy_suite",
                "schema_version": 1,
                "suite_id": self.suite_id,
                "role": self.role,
                "cases_manifest_sha256": policy_evaluation_cases_manifest_sha256(
                    self.cases
                ),
                "source_evidence_sha256s": [
                    case.source_evidence_sha256 for case in self.cases
                ],
            }
        )


@dataclass(frozen=True, slots=True)
class ContinualLearningRecipe:
    """Frozen bounds and acceptance thresholds for one candidate-only cycle."""

    champion_policy_id: str
    retention_suite_sha256: str
    holdout_suite_sha256: str
    max_updates: int = 64
    min_holdout_improvement: Decimal = Decimal("0")
    max_retention_drop: Decimal = Decimal("0")
    min_effective_sample_size: Decimal = Decimal("1")
    require_observed_rewards: bool = True

    def __post_init__(self) -> None:
        _sha256(self.champion_policy_id, "champion_policy_id")
        _sha256(self.retention_suite_sha256, "retention_suite_sha256")
        _sha256(self.holdout_suite_sha256, "holdout_suite_sha256")
        if type(self.max_updates) is not int or not 2 <= self.max_updates <= 256:
            raise ContinualLearningCycleError("max_updates must be in [2,256]")
        _decimal(
            self.min_holdout_improvement,
            "min_holdout_improvement",
            nonnegative=True,
        )
        _decimal(self.max_retention_drop, "max_retention_drop", nonnegative=True)
        if _decimal(
            self.min_effective_sample_size,
            "min_effective_sample_size",
            nonnegative=True,
        ) <= 0:
            raise ContinualLearningCycleError(
                "min_effective_sample_size must be positive"
            )
        if type(self.require_observed_rewards) is not bool:
            raise ContinualLearningCycleError(
                "require_observed_rewards must be exact bool"
            )

    @property
    def recipe_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.continual_learning_recipe",
                "schema_version": 1,
                "champion_policy_id": self.champion_policy_id,
                "retention_suite_sha256": self.retention_suite_sha256,
                "holdout_suite_sha256": self.holdout_suite_sha256,
                "max_updates": self.max_updates,
                "min_holdout_improvement": str(self.min_holdout_improvement),
                "max_retention_drop": str(self.max_retention_drop),
                "min_effective_sample_size": str(self.min_effective_sample_size),
                "require_observed_rewards": self.require_observed_rewards,
            }
        )


@dataclass(frozen=True, slots=True)
class ContinualUpdateRecord:
    index: int
    population: str
    sample_id: str
    experience_id: str
    update_id: str
    predecessor_policy_id: str
    successor_policy_id: str


@dataclass(frozen=True, slots=True)
class ContinualLearningResult:
    """Replay-verifiable candidate or fail-closed rollback outcome."""

    state: str
    champion_policy_id: str
    candidate_policy_id: str | None
    candidate: BanditPolicyState | None
    recipe_sha256: str
    new_population_sha256: str
    replay_population_sha256: str
    updates: tuple[ContinualUpdateRecord, ...]
    retention_evaluation: PolicyPairEvaluation
    holdout_evaluation: PolicyPairEvaluation
    rollback_to_policy_id: str
    qualification_reasons: tuple[str, ...]
    evidence_sha256: str
    promotion_authorized: bool = False


def _population(
    experiences: object,
    *,
    name: str,
    champion: BanditPolicyState,
    require_observed: bool,
) -> tuple[VerifiedLearningExperience, ...]:
    if type(experiences) is not tuple or not experiences:
        raise ContinualLearningCycleError(f"{name} experiences must be a non-empty tuple")
    if any(type(item) is not VerifiedLearningExperience for item in experiences):
        raise ContinualLearningCycleError(
            f"{name} experiences must be exact VerifiedLearningExperience values"
        )
    ordered = tuple(sorted(experiences, key=lambda item: item.sample_id))
    sample_ids = tuple(item.sample_id for item in ordered)
    experience_ids = tuple(item.experience_id for item in ordered)
    if len(sample_ids) != len(set(sample_ids)):
        raise ContinualLearningCycleError(f"{name} sample identities must be unique")
    if len(experience_ids) != len(set(experience_ids)):
        raise ContinualLearningCycleError(f"{name} experience identities must be unique")
    for item in ordered:
        if item.action.environment_id != champion.environment_id:
            raise ContinualLearningCycleError(
                f"{name} experience belongs to another policy environment"
            )
        if require_observed and item.reward.truth is not EvidenceTruth.OBSERVED:
            raise ContinualLearningCycleError(
                f"{name} experience requires OBSERVED reward truth"
            )
    return ordered


def _population_sha256(
    population: tuple[VerifiedLearningExperience, ...], *, name: str
) -> str:
    return _digest(
        {
            "schema": "autosport.continual_learning_population",
            "schema_version": 1,
            "name": name,
            "experience_ids": [item.experience_id for item in population],
        }
    )


def run_continual_learning_cycle(
    champion: BanditPolicyState,
    *,
    new_experiences: tuple[VerifiedLearningExperience, ...],
    replay_experiences: tuple[VerifiedLearningExperience, ...],
    retention_suite: FrozenPolicySuite,
    holdout_suite: FrozenPolicySuite,
    recipe: ContinualLearningRecipe,
    completed_at: str,
    counterfactual_authority: QualifiedCounterfactualAuthority | None = None,
) -> ContinualLearningResult:
    """Run one deterministic candidate-only continual-learning cycle."""

    if type(champion) is not BanditPolicyState:
        raise TypeError("champion must be exact BanditPolicyState")
    if type(retention_suite) is not FrozenPolicySuite:
        raise TypeError("retention_suite must be exact FrozenPolicySuite")
    if type(holdout_suite) is not FrozenPolicySuite:
        raise TypeError("holdout_suite must be exact FrozenPolicySuite")
    if type(recipe) is not ContinualLearningRecipe:
        raise TypeError("recipe must be exact ContinualLearningRecipe")
    if recipe.champion_policy_id != champion.policy_id:
        raise ContinualLearningCycleError("recipe is bound to a stale champion")
    if retention_suite.role != "retention" or holdout_suite.role != "holdout":
        raise ContinualLearningCycleError("frozen suite roles are reversed or invalid")
    if retention_suite.suite_sha256 != recipe.retention_suite_sha256:
        raise ContinualLearningCycleError("retention suite changed after recipe freeze")
    if holdout_suite.suite_sha256 != recipe.holdout_suite_sha256:
        raise ContinualLearningCycleError("holdout suite changed after recipe freeze")
    if retention_suite.suite_id == holdout_suite.suite_id:
        raise ContinualLearningCycleError("retention and holdout suite identities overlap")

    retention_ids = {case.sample_id for case in retention_suite.cases}
    holdout_ids = {case.sample_id for case in holdout_suite.cases}
    if retention_ids.intersection(holdout_ids):
        raise ContinualLearningCycleError(
            "retention and untouched holdout samples must be disjoint"
        )
    retention_evidence = {
        case.source_evidence_sha256 for case in retention_suite.cases
    }
    holdout_evidence = {case.source_evidence_sha256 for case in holdout_suite.cases}
    if retention_evidence.intersection(holdout_evidence):
        raise ContinualLearningCycleError(
            "retention and holdout source evidence must be independent"
        )

    new = _population(
        new_experiences,
        name="new",
        champion=champion,
        require_observed=recipe.require_observed_rewards,
    )
    replay = _population(
        replay_experiences,
        name="replay",
        champion=champion,
        require_observed=recipe.require_observed_rewards,
    )
    if len(new) + len(replay) > recipe.max_updates:
        raise ContinualLearningCycleError("training populations exceed frozen update budget")
    train_ids = {item.sample_id for item in (*new, *replay)}
    if len(train_ids) != len(new) + len(replay):
        raise ContinualLearningCycleError("new and replay sample identities overlap")
    if train_ids.intersection(retention_ids | holdout_ids):
        raise ContinualLearningCycleError(
            "training population contaminates retention or untouched holdout"
        )
    train_experience_ids = [item.experience_id for item in (*new, *replay)]
    if len(train_experience_ids) != len(set(train_experience_ids)):
        raise ContinualLearningCycleError("new and replay experiences overlap")

    candidate = champion
    records: list[ContinualUpdateRecord] = []
    for population_name, population in (("new", new), ("replay", replay)):
        for item in population:
            predecessor = candidate
            candidate, evidence = candidate.update(
                action=item.action,
                reward=item.reward,
                transition=item.transition,
            )
            if type(evidence) is not PolicyUpdateEvidence:
                raise ContinualLearningCycleError(
                    "canonical policy update returned untyped evidence"
                )
            records.append(
                ContinualUpdateRecord(
                    index=len(records) + 1,
                    population=population_name,
                    sample_id=item.sample_id,
                    experience_id=item.experience_id,
                    update_id=evidence.update_id,
                    predecessor_policy_id=predecessor.policy_id,
                    successor_policy_id=candidate.policy_id,
                )
            )

    retention_evaluation = evaluate_policy_pair(
        champion,
        candidate,
        retention_suite.cases,
        completed_at=completed_at,
        counterfactual_authority=counterfactual_authority,
    )
    holdout_evaluation = evaluate_policy_pair(
        champion,
        candidate,
        holdout_suite.cases,
        completed_at=completed_at,
        counterfactual_authority=counterfactual_authority,
    )
    reasons: list[str] = []
    if (
        retention_evaluation.practical_improvement
        < -recipe.max_retention_drop
    ):
        reasons.append("aggregate retention degradation exceeds tolerance")
    degraded_regimes = tuple(
        regime
        for regime, improvement in retention_evaluation.regime_attribution
        if improvement < -recipe.max_retention_drop
    )
    if degraded_regimes:
        reasons.append(
            "retention degradation exceeds tolerance in regimes: "
            + ", ".join(degraded_regimes)
        )
    if (
        holdout_evaluation.practical_improvement
        < recipe.min_holdout_improvement
    ):
        reasons.append("untouched holdout improvement threshold not met")
    if (
        holdout_evaluation.effective_sample_size
        < recipe.min_effective_sample_size
    ):
        reasons.append("untouched holdout effective sample size is insufficient")
    qualified = not reasons
    state = "CANDIDATE_READY" if qualified else "ROLLED_BACK"
    accepted_candidate = candidate if qualified else None
    candidate_policy_id = candidate.policy_id if qualified else None
    new_sha = _population_sha256(new, name="new")
    replay_sha = _population_sha256(replay, name="replay")
    evidence_sha = _digest(
        {
            "schema": "autosport.continual_learning_result",
            "schema_version": 1,
            "state": state,
            "champion_policy_id": champion.policy_id,
            "candidate_policy_id": candidate_policy_id,
            "evaluated_candidate_policy_id": candidate.policy_id,
            "recipe_sha256": recipe.recipe_sha256,
            "new_population_sha256": new_sha,
            "replay_population_sha256": replay_sha,
            "updates": [
                {
                    "index": record.index,
                    "population": record.population,
                    "sample_id": record.sample_id,
                    "experience_id": record.experience_id,
                    "update_id": record.update_id,
                    "predecessor_policy_id": record.predecessor_policy_id,
                    "successor_policy_id": record.successor_policy_id,
                }
                for record in records
            ],
            "retention_evaluation_sha256": retention_evaluation.evaluation_sha256,
            "holdout_evaluation_sha256": holdout_evaluation.evaluation_sha256,
            "rollback_to_policy_id": champion.policy_id,
            "qualification_reasons": reasons,
            "promotion_authorized": False,
        }
    )
    return ContinualLearningResult(
        state=state,
        champion_policy_id=champion.policy_id,
        candidate_policy_id=candidate_policy_id,
        candidate=accepted_candidate,
        recipe_sha256=recipe.recipe_sha256,
        new_population_sha256=new_sha,
        replay_population_sha256=replay_sha,
        updates=tuple(records),
        retention_evaluation=retention_evaluation,
        holdout_evaluation=holdout_evaluation,
        rollback_to_policy_id=champion.policy_id,
        qualification_reasons=tuple(reasons),
        evidence_sha256=evidence_sha,
    )


def verify_continual_learning_replay(
    champion: BanditPolicyState,
    *,
    new_experiences: tuple[VerifiedLearningExperience, ...],
    replay_experiences: tuple[VerifiedLearningExperience, ...],
    retention_suite: FrozenPolicySuite,
    holdout_suite: FrozenPolicySuite,
    recipe: ContinualLearningRecipe,
    completed_at: str,
    result: ContinualLearningResult,
    counterfactual_authority: QualifiedCounterfactualAuthority | None = None,
) -> bool:
    """Recompute an exact cycle and reject altered or promotion-inflated receipts."""

    if type(result) is not ContinualLearningResult:
        raise TypeError("result must be exact ContinualLearningResult")
    if result.promotion_authorized:
        raise ContinualLearningCycleError(
            "continual cycle cannot authorize its own promotion"
        )
    recomputed = run_continual_learning_cycle(
        champion,
        new_experiences=new_experiences,
        replay_experiences=replay_experiences,
        retention_suite=retention_suite,
        holdout_suite=holdout_suite,
        recipe=recipe,
        completed_at=completed_at,
        counterfactual_authority=counterfactual_authority,
    )
    if recomputed != result:
        raise ContinualLearningCycleError("continual cycle restart/replay mismatch")
    return True


__all__ = [
    "ContinualLearningCycleError",
    "ContinualLearningRecipe",
    "ContinualLearningResult",
    "ContinualUpdateRecord",
    "FrozenPolicySuite",
    "VerifiedLearningExperience",
    "run_continual_learning_cycle",
    "verify_continual_learning_replay",
]
