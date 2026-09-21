from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.learning_environment import EvidenceTruth
from autosport.policy_evaluation import (
    PolicyEvaluationCase,
    PolicyRewardMode,
    QualifiedCounterfactualAuthority,
    evaluate_policy_pair,
)
from autosport.transparent_bandit_policy import ActionEstimate, BanditPolicyState


ENVIRONMENT = "a" * 64
CONFIG = "b" * 64
REWARD_DEF = "c" * 64
EVALUATOR_SOURCE = "d" * 64
QUALIFICATION = "e" * 64
EVIDENCE = "f" * 64
T0 = "2026-09-21T10:00:00Z"
T1 = "2026-09-21T11:00:00Z"
T2 = "2026-09-21T12:00:00Z"


def _policy(preferred: str, actions: tuple[str, ...]) -> BanditPolicyState:
    return BanditPolicyState(
        environment_id=ENVIRONMENT,
        protocol_id="protocol-abstention-metrics-v1",
        config_sha256=CONFIG,
        seed=17,
        generation=0,
        estimates=tuple(
            ActionEstimate(
                action_type,
                1,
                Decimal("2") if action_type == preferred else Decimal("0"),
            )
            for action_type in sorted(actions)
        ),
    )


def _authority() -> QualifiedCounterfactualAuthority:
    return QualifiedCounterfactualAuthority(
        authority_id="paper-settlement-engine:v1",
        authority_version="1",
        evaluator_source_sha256=EVALUATOR_SOURCE,
        qualification_evidence_sha256=QUALIFICATION,
        reward_definition_sha256=REWARD_DEF,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        scope="table-tennis:pre-match",
    )


def _case(actions: tuple[str, ...]) -> PolicyEvaluationCase:
    ordered = tuple(sorted(actions))
    return PolicyEvaluationCase(
        sample_id="abstention-taxonomy",
        observed_at=T0,
        reward_available_at=T1,
        admissible_actions=ordered,
        action_rewards=tuple((action, Decimal("0")) for action in ordered),
        action_costs=tuple((action, Decimal("0")) for action in ordered),
        behavior_propensities=tuple(
            (action, Decimal("0.34") if index == 0 else Decimal("0.33"))
            for index, action in enumerate(ordered)
        ),
        reward_truth=EvidenceTruth.OBSERVED,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        source_evidence_sha256=EVIDENCE,
        regime_id="table-tennis:pre-match",
        counterfactual_source_id="paper-settlement-engine:v1",
    )


def _metrics(result, *, challenger: bool) -> dict[str, Decimal]:
    values = result.challenger_metrics if challenger else result.predecessor_metrics
    return dict(values)


def test_wait_and_no_bet_are_both_classified_as_canonical_abstentions():
    actions = ("BET", "NO_BET", "WAIT")
    predecessor = _policy("NO_BET", actions)
    challenger = _policy("WAIT", actions)

    result = evaluate_policy_pair(
        predecessor,
        challenger,
        (_case(actions),),
        completed_at=T2,
        counterfactual_authority=_authority(),
    )

    predecessor_metrics = _metrics(result, challenger=False)
    challenger_metrics = _metrics(result, challenger=True)

    assert result.samples[0]["predecessor_action"] == "NO_BET"
    assert result.samples[0]["challenger_action"] == "WAIT"
    assert predecessor_metrics["abstention_rate"] == Decimal("1")
    assert predecessor_metrics["action_rate"] == Decimal("0")
    assert challenger_metrics["abstention_rate"] == Decimal("1")
    assert challenger_metrics["action_rate"] == Decimal("0")
    assert result.practical_improvement == Decimal("0")


def test_explicit_custom_abstain_action_extends_canonical_taxonomy():
    actions = ("BET", "NO_BET", "SKIP")
    predecessor = _policy("NO_BET", actions)
    challenger = _policy("SKIP", actions)

    result = evaluate_policy_pair(
        predecessor,
        challenger,
        (_case(actions),),
        completed_at=T2,
        abstain_action="SKIP",
        counterfactual_authority=_authority(),
    )

    assert _metrics(result, challenger=False)["action_rate"] == Decimal("0")
    assert _metrics(result, challenger=True)["action_rate"] == Decimal("0")


def test_material_bet_remains_an_active_action():
    actions = ("BET", "NO_BET", "WAIT")
    predecessor = _policy("BET", actions)
    challenger = _policy("NO_BET", actions)

    result = evaluate_policy_pair(
        predecessor,
        challenger,
        (_case(actions),),
        completed_at=T2,
        counterfactual_authority=_authority(),
    )

    assert _metrics(result, challenger=False)["action_rate"] == Decimal("1")
    assert _metrics(result, challenger=False)["abstention_rate"] == Decimal("0")
    assert _metrics(result, challenger=True)["action_rate"] == Decimal("0")
    assert _metrics(result, challenger=True)["abstention_rate"] == Decimal("1")

def test_material_bet_cannot_be_configured_as_custom_abstention():
    actions = ("BET", "NO_BET", "WAIT")
    predecessor = _policy("BET", actions)
    challenger = _policy("NO_BET", actions)

    with pytest.raises(ValueError, match="material PAPER action"):
        evaluate_policy_pair(
            predecessor,
            challenger,
            (_case(actions),),
            completed_at=T2,
            abstain_action="BET",
            counterfactual_authority=_authority(),
        )

def test_sample_id_alias_cannot_duplicate_identical_causal_case():
    actions = ("BET", "NO_BET", "WAIT")
    predecessor = _policy("NO_BET", actions)
    challenger = _policy("WAIT", actions)
    original = _case(actions)
    aliased_duplicate = replace(
        original,
        sample_id="abstention-taxonomy-fragment-b",
    )

    with pytest.raises(ValueError, match="duplicate causal evidence|sample_id aliases"):
        evaluate_policy_pair(
            predecessor,
            challenger,
            (original, aliased_duplicate),
            completed_at=T2,
            counterfactual_authority=_authority(),
        )


@pytest.mark.parametrize(
    "propensities",
    (
        (
            ("BET", Decimal("0.75")),
            ("NO_BET", Decimal("0.50")),
            ("WAIT", Decimal("0.25")),
        ),
        (
            ("BET", Decimal("0.20")),
            ("NO_BET", Decimal("0.20")),
            ("WAIT", Decimal("0.20")),
        ),
    ),
)
def test_behavior_propensity_mass_must_be_exactly_one(propensities):
    actions = ("BET", "NO_BET", "WAIT")

    with pytest.raises(ValueError, match="sum to exactly 1"):
        replace(
            _case(actions),
            behavior_propensities=propensities,
        )


def test_distinct_source_evidence_remains_distinct_causal_support():
    actions = ("BET", "NO_BET", "WAIT")
    predecessor = _policy("NO_BET", actions)
    challenger = _policy("WAIT", actions)
    original = _case(actions)
    distinct = replace(
        original,
        sample_id="abstention-taxonomy-distinct-b",
        source_evidence_sha256="1" * 64,
    )

    result = evaluate_policy_pair(
        predecessor,
        challenger,
        (original, distinct),
        completed_at=T2,
        counterfactual_authority=_authority(),
    )

    assert len(result.samples) == 2

