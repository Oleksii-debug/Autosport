"""Causal paired evaluator for learned transparent policies.

This module owns only evaluation arithmetic/evidence.  It never grants promotion,
risk, execution, or money authority.  ScientificRegistry remains the promotion
authority and callers must bind this evidence through the canonical factory.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Sequence

from .learning_environment import EvidenceTruth
from .transparent_bandit_policy import BanditPolicyState


_HEX = frozenset("0123456789abcdef")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be canonical non-empty text")
    value.encode("utf-8", errors="strict")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, name: str, *, nonnegative: bool = False) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if nonnegative and value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _decimal_text(value: Decimal) -> str:
    value = _decimal(value, "decimal")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PolicyRewardMode(StrEnum):
    """What makes rewards for actions causally admissible in one paired case."""

    OBSERVED_ACTION = "OBSERVED_ACTION"
    QUALIFIED_SIMULATOR = "QUALIFIED_SIMULATOR"
    MECHANICAL_PAPER = "MECHANICAL_PAPER"


@dataclass(frozen=True, slots=True)
class PolicyEvaluationConfig:
    """Frozen degrees of freedom for policy-specific paired evaluation."""

    feature_set_id: str
    feature_definition_sha256: str
    feature_source_sha256: str
    reward_definition_sha256: str
    cost_definition_sha256: str
    abstain_action: str = "WAIT"

    def __post_init__(self) -> None:
        _text(self.feature_set_id, "feature_set_id")
        _sha256(self.feature_definition_sha256, "feature_definition_sha256")
        _sha256(self.feature_source_sha256, "feature_source_sha256")
        _sha256(self.reward_definition_sha256, "reward_definition_sha256")
        _sha256(self.cost_definition_sha256, "cost_definition_sha256")
        _text(self.abstain_action, "abstain_action")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "kind": "autosport-policy-paired-evaluation-v1",
            "feature_set_id": self.feature_set_id,
            "feature_definition_sha256": self.feature_definition_sha256,
            "feature_source_sha256": self.feature_source_sha256,
            "reward_definition_sha256": self.reward_definition_sha256,
            "cost_definition_sha256": self.cost_definition_sha256,
            "abstain_action": self.abstain_action,
        }

    @property
    def frozen_text(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @property
    def config_sha256(self) -> str:
        return _digest(self.canonical_payload())

    @classmethod
    def from_frozen_text(cls, value: object) -> "PolicyEvaluationConfig":
        text = _text(value, "evaluation_design")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("policy evaluation_design must be canonical JSON") from exc
        expected = {
            "kind",
            "feature_set_id",
            "feature_definition_sha256",
            "feature_source_sha256",
            "reward_definition_sha256",
            "cost_definition_sha256",
            "abstain_action",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise ValueError("policy evaluation_design fields mismatch")
        if payload.get("kind") != "autosport-policy-paired-evaluation-v1":
            raise ValueError("policy evaluation_design kind is unsupported")
        result = cls(
            payload["feature_set_id"],
            payload["feature_definition_sha256"],
            payload["feature_source_sha256"],
            payload["reward_definition_sha256"],
            payload["cost_definition_sha256"],
            payload["abstain_action"],
        )
        if text != result.frozen_text:
            raise ValueError("policy evaluation_design must use canonical encoding")
        return result


@dataclass(frozen=True, slots=True)
class PolicyEvaluationCase:
    """One frozen unseen causal case with explicit reward/support evidence."""

    sample_id: str
    observed_at: str
    reward_available_at: str
    admissible_actions: tuple[str, ...]
    action_rewards: tuple[tuple[str, Decimal], ...]
    action_costs: tuple[tuple[str, Decimal], ...]
    behavior_propensities: tuple[tuple[str, Decimal], ...]
    reward_truth: EvidenceTruth
    reward_mode: PolicyRewardMode
    source_evidence_sha256: str
    regime_id: str
    historical_action: str | None = None
    counterfactual_source_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.sample_id, "sample_id")
        observed = _instant(self.observed_at, "observed_at")
        revealed = _instant(self.reward_available_at, "reward_available_at")
        if revealed < observed:
            raise ValueError("reward_available_at must not precede observed_at")
        _sha256(self.source_evidence_sha256, "source_evidence_sha256")
        _text(self.regime_id, "regime_id")
        if not isinstance(self.reward_truth, EvidenceTruth):
            raise ValueError("reward_truth must be EvidenceTruth")
        if not isinstance(self.reward_mode, PolicyRewardMode):
            raise ValueError("reward_mode must be PolicyRewardMode")
        if type(self.admissible_actions) is not tuple or not self.admissible_actions:
            raise ValueError("admissible_actions must be a non-empty tuple")
        actions = tuple(_text(value, "admissible action") for value in self.admissible_actions)
        if actions != tuple(sorted(actions)) or len(actions) != len(set(actions)):
            raise ValueError("admissible_actions must be sorted and unique")

        def pairs(
            raw: object,
            name: str,
            *,
            nonnegative: bool = False,
        ) -> dict[str, Decimal]:
            if type(raw) is not tuple or not raw:
                raise ValueError(f"{name} must be a non-empty tuple")
            result: dict[str, Decimal] = {}
            previous: str | None = None
            for item in raw:
                if type(item) is not tuple or len(item) != 2:
                    raise ValueError(f"{name} entries must be (action, Decimal)")
                action = _text(item[0], f"{name} action")
                value = _decimal(item[1], f"{name}.{action}", nonnegative=nonnegative)
                if action not in actions:
                    raise ValueError(f"{name} references action outside admissible set")
                if action in result:
                    raise ValueError(f"{name} action identities must be unique")
                if previous is not None and action <= previous:
                    raise ValueError(f"{name} must be sorted by action identity")
                result[action] = value
                previous = action
            return result

        rewards = pairs(self.action_rewards, "action_rewards")
        costs = pairs(self.action_costs, "action_costs", nonnegative=True)
        propensities = pairs(
            self.behavior_propensities,
            "behavior_propensities",
            nonnegative=True,
        )
        if any(value <= 0 or value > 1 for value in propensities.values()):
            raise ValueError("behavior propensity must be in (0,1]")
        if not set(costs).issubset(rewards):
            raise ValueError("cost evidence exists for action without reward evidence")

        if self.reward_mode is PolicyRewardMode.OBSERVED_ACTION:
            historical = _text(self.historical_action, "historical_action")
            if self.reward_truth is not EvidenceTruth.OBSERVED:
                raise ValueError("observed-action reward must have OBSERVED truth")
            if set(rewards) != {historical}:
                raise ValueError("observed-action case may expose only historical reward")
            if self.counterfactual_source_id is not None:
                raise ValueError("observed-action case cannot claim counterfactual source")
        else:
            if self.historical_action is not None:
                _text(self.historical_action, "historical_action")
            source_id = _text(self.counterfactual_source_id, "counterfactual_source_id")
            if set(rewards) != set(actions):
                raise ValueError("qualified full-information case requires every action reward")
            if self.reward_mode is PolicyRewardMode.QUALIFIED_SIMULATOR:
                if self.reward_truth is not EvidenceTruth.SIMULATED:
                    raise ValueError("simulator case must have SIMULATED truth")
            elif self.reward_truth is not EvidenceTruth.OBSERVED:
                raise ValueError("mechanical paper case must have OBSERVED truth")
            _text(source_id, "counterfactual_source_id")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "observed_at": self.observed_at,
            "reward_available_at": self.reward_available_at,
            "admissible_actions": list(self.admissible_actions),
            "action_rewards": [[k, _decimal_text(v)] for k, v in self.action_rewards],
            "action_costs": [[k, _decimal_text(v)] for k, v in self.action_costs],
            "behavior_propensities": [
                [k, _decimal_text(v)] for k, v in self.behavior_propensities
            ],
            "reward_truth": self.reward_truth.value,
            "reward_mode": self.reward_mode.value,
            "source_evidence_sha256": self.source_evidence_sha256,
            "regime_id": self.regime_id,
            "historical_action": self.historical_action,
            "counterfactual_source_id": self.counterfactual_source_id,
        }


def policy_evaluation_cases_manifest_sha256(
    cases: Sequence[PolicyEvaluationCase],
) -> str:
    ordered = _ordered_cases(cases)
    return _digest(
        {
            "schema_version": 1,
            "kind": "autosport-policy-evaluation-cases-v1",
            "cases": [case.canonical_payload() for case in ordered],
        }
    )


@dataclass(frozen=True, slots=True)
class PolicyPairEvaluation:
    predecessor_policy_id: str
    challenger_policy_id: str
    environment_id: str
    protocol_id: str
    predecessor_action_types: tuple[str, ...]
    challenger_action_types: tuple[str, ...]
    dataset_manifest_sha256: str
    completed_at: str
    samples: tuple[dict[str, object], ...]
    predecessor_metrics: tuple[tuple[str, Decimal], ...]
    challenger_metrics: tuple[tuple[str, Decimal], ...]
    paired_improvements: tuple[Decimal, ...]
    effective_sample_size: Decimal
    regime_attribution: tuple[tuple[str, Decimal], ...]

    @property
    def practical_improvement(self) -> Decimal:
        with localcontext() as context:
            context.prec = 50
            return sum(self.paired_improvements, Decimal(0)) / Decimal(
                len(self.paired_improvements)
            )

    @property
    def effect_interval_low(self) -> Decimal:
        return min(self.paired_improvements)

    @property
    def effect_interval_high(self) -> Decimal:
        return max(self.paired_improvements)

    @property
    def evaluation_sha256(self) -> str:
        return _digest(self.canonical_payload())

    def metrics_as_float(self, *, challenger: bool) -> dict[str, float]:
        source = self.challenger_metrics if challenger else self.predecessor_metrics
        return {name: float(value) for name, value in source}

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "autosport-policy-paired-causal-evaluation-v1",
            "predecessor_policy_id": self.predecessor_policy_id,
            "challenger_policy_id": self.challenger_policy_id,
            "environment_id": self.environment_id,
            "protocol_id": self.protocol_id,
            "predecessor_action_types": list(self.predecessor_action_types),
            "challenger_action_types": list(self.challenger_action_types),
            "dataset_manifest_sha256": self.dataset_manifest_sha256,
            "completed_at": self.completed_at,
            "samples": list(self.samples),
            "predecessor_metrics": {
                key: _decimal_text(value) for key, value in self.predecessor_metrics
            },
            "challenger_metrics": {
                key: _decimal_text(value) for key, value in self.challenger_metrics
            },
            "paired_improvements": [
                _decimal_text(value) for value in self.paired_improvements
            ],
            "effective_sample_size": _decimal_text(self.effective_sample_size),
            "regime_attribution": {
                key: _decimal_text(value) for key, value in self.regime_attribution
            },
            "evaluation_sha256": _digest(
                {
                    "predecessor_policy_id": self.predecessor_policy_id,
                    "challenger_policy_id": self.challenger_policy_id,
                    "dataset_manifest_sha256": self.dataset_manifest_sha256,
                    "samples": list(self.samples),
                }
            ),
            "truth": {
                "real_money_execution": False,
                "counterfactual_rewards_invented": False,
                "llm_arithmetic_authority": False,
            },
        }


def _ordered_cases(cases: Sequence[PolicyEvaluationCase]) -> tuple[PolicyEvaluationCase, ...]:
    if not cases:
        raise ValueError("policy evaluation requires at least one case")
    if any(not isinstance(case, PolicyEvaluationCase) for case in cases):
        raise ValueError("policy evaluation cases must be PolicyEvaluationCase")
    ordered = tuple(
        sorted(
            cases,
            key=lambda case: (
                _instant(case.observed_at, "observed_at"),
                case.sample_id,
            ),
        )
    )
    identities = tuple(case.sample_id for case in ordered)
    if len(identities) != len(set(identities)):
        raise ValueError("policy evaluation sample_id values must be unique")
    return ordered


def _max_drawdown(rewards: Sequence[Decimal]) -> Decimal:
    cumulative = Decimal(0)
    peak = Decimal(0)
    worst = Decimal(0)
    for reward in rewards:
        cumulative += reward
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > worst:
            worst = drawdown
    return worst


def _policy_metrics(
    rewards: Sequence[Decimal],
    actions: Sequence[str],
    *,
    abstain_action: str,
) -> tuple[tuple[str, Decimal], ...]:
    with localcontext() as context:
        context.prec = 50
        count = Decimal(len(rewards))
        mean_reward = sum(rewards, Decimal(0)) / count
        worst_reward = min(rewards)
        downside_loss = max(Decimal(0), -worst_reward)
        max_drawdown = _max_drawdown(rewards)
        abstentions = Decimal(sum(action == abstain_action for action in actions))
        abstention_rate = abstentions / count
        action_rate = Decimal(1) - abstention_rate
    return tuple(
        sorted(
            (
                ("policy_loss", -mean_reward),
                ("downside_loss", downside_loss),
                ("max_drawdown", max_drawdown),
                ("abstention_rate", abstention_rate),
                ("action_rate", action_rate),
            )
        )
    )


def evaluate_policy_pair(
    predecessor: BanditPolicyState,
    challenger: BanditPolicyState,
    cases: Sequence[PolicyEvaluationCase],
    *,
    completed_at: str,
    abstain_action: str = "WAIT",
) -> PolicyPairEvaluation:
    """Execute both exact policies over one frozen paired causal population."""

    if not isinstance(predecessor, BanditPolicyState):
        raise TypeError("predecessor must be BanditPolicyState")
    if not isinstance(challenger, BanditPolicyState):
        raise TypeError("challenger must be BanditPolicyState")
    completed = _instant(completed_at, "completed_at")
    if predecessor.environment_id != challenger.environment_id:
        raise ValueError("policy evaluation environment mismatch")
    if predecessor.protocol_id != challenger.protocol_id:
        raise ValueError("policy evaluation protocol mismatch")
    predecessor_actions = tuple(item.action_type for item in predecessor.estimates)
    challenger_actions = tuple(item.action_type for item in challenger.estimates)
    if predecessor_actions != challenger_actions:
        raise ValueError("paired policy evaluation requires identical action universe")
    _text(abstain_action, "abstain_action")

    ordered = _ordered_cases(cases)
    predecessor_rewards: list[Decimal] = []
    challenger_rewards: list[Decimal] = []
    predecessor_choices: list[str] = []
    challenger_choices: list[str] = []
    paired_improvements: list[Decimal] = []
    importance_weights: list[Decimal] = []
    regime_deltas: dict[str, list[Decimal]] = {}
    sample_payloads: list[dict[str, object]] = []

    for case in ordered:
        if _instant(case.reward_available_at, "reward_available_at") > completed:
            raise ValueError("policy reward was not causally available by completion")
        admitted = frozenset(case.admissible_actions)
        predecessor_choice = predecessor.choose(admissible_actions=admitted)
        challenger_choice = challenger.choose(admissible_actions=admitted)
        rewards = dict(case.action_rewards)
        costs = dict(case.action_costs)
        propensities = dict(case.behavior_propensities)

        if predecessor_choice not in rewards or challenger_choice not in rewards:
            raise ValueError(
                "unsupported counterfactual reward: chosen policy action lacks qualified reward"
            )
        if (
            case.reward_mode is PolicyRewardMode.OBSERVED_ACTION
            and (
                predecessor_choice != case.historical_action
                or challenger_choice != case.historical_action
            )
        ):
            raise ValueError(
                "observed chosen-action evidence cannot invent unchosen counterfactual reward"
            )
        predecessor_propensity = propensities.get(predecessor_choice)
        challenger_propensity = propensities.get(challenger_choice)
        if predecessor_propensity is None or challenger_propensity is None:
            raise ValueError("policy evaluation is missing behavior propensity/support")
        if predecessor_propensity <= 0 or challenger_propensity <= 0:
            raise ValueError("policy evaluation chosen action is outside supported behavior")

        predecessor_net = rewards[predecessor_choice] - costs.get(
            predecessor_choice, Decimal(0)
        )
        challenger_net = rewards[challenger_choice] - costs.get(
            challenger_choice, Decimal(0)
        )
        delta = challenger_net - predecessor_net
        predecessor_rewards.append(predecessor_net)
        challenger_rewards.append(challenger_net)
        predecessor_choices.append(predecessor_choice)
        challenger_choices.append(challenger_choice)
        paired_improvements.append(delta)
        with localcontext() as context:
            context.prec = 50
            importance_weights.append(Decimal(1) / challenger_propensity)
        regime_deltas.setdefault(case.regime_id, []).append(delta)
        sample_payloads.append(
            {
                "sample_id": case.sample_id,
                "observed_at": case.observed_at,
                "reward_available_at": case.reward_available_at,
                "reward_truth": case.reward_truth.value,
                "reward_mode": case.reward_mode.value,
                "source_evidence_sha256": case.source_evidence_sha256,
                "regime_id": case.regime_id,
                "predecessor_action": predecessor_choice,
                "challenger_action": challenger_choice,
                "predecessor_net_reward": _decimal_text(predecessor_net),
                "challenger_net_reward": _decimal_text(challenger_net),
                "paired_improvement": _decimal_text(delta),
                "predecessor_behavior_propensity": _decimal_text(
                    predecessor_propensity
                ),
                "challenger_behavior_propensity": _decimal_text(
                    challenger_propensity
                ),
                "counterfactual_source_id": case.counterfactual_source_id,
            }
        )

    with localcontext() as context:
        context.prec = 50
        weight_sum = sum(importance_weights, Decimal(0))
        weight_square_sum = sum(
            (weight * weight for weight in importance_weights), Decimal(0)
        )
        if weight_square_sum <= 0:
            raise ValueError("policy evaluation effective support is zero")
        effective_sample_size = (weight_sum * weight_sum) / weight_square_sum
        regime_attribution = tuple(
            sorted(
                (
                    regime,
                    sum(values, Decimal(0)) / Decimal(len(values)),
                )
                for regime, values in regime_deltas.items()
            )
        )

    return PolicyPairEvaluation(
        predecessor_policy_id=predecessor.policy_id,
        challenger_policy_id=challenger.policy_id,
        environment_id=challenger.environment_id,
        protocol_id=challenger.protocol_id,
        predecessor_action_types=predecessor_actions,
        challenger_action_types=challenger_actions,
        dataset_manifest_sha256=policy_evaluation_cases_manifest_sha256(ordered),
        completed_at=completed_at,
        samples=tuple(sample_payloads),
        predecessor_metrics=_policy_metrics(
            predecessor_rewards,
            predecessor_choices,
            abstain_action=abstain_action,
        ),
        challenger_metrics=_policy_metrics(
            challenger_rewards,
            challenger_choices,
            abstain_action=abstain_action,
        ),
        paired_improvements=tuple(paired_improvements),
        effective_sample_size=effective_sample_size,
        regime_attribution=regime_attribution,
    )


__all__ = [
    "PolicyEvaluationCase",
    "PolicyEvaluationConfig",
    "PolicyPairEvaluation",
    "PolicyRewardMode",
    "evaluate_policy_pair",
    "policy_evaluation_cases_manifest_sha256",
]
