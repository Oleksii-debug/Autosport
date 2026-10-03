from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
)
from autosport.policy_update_authority import (
    UTILITY_AUTHORITY_UNRESOLVED,
    attempt_utility_bound_update,
)
from autosport.policy_utility_evidence import (
    AuthorityRef,
    DecisionKind,
    PolicyUtilityError,
    PolicyUtilityEvidence,
    PolicyUtilityStore,
    UtilityCompleteness,
    UtilityTruthClass,
)
from autosport.transparent_bandit_policy import BanditPolicyState


UTC = timezone.utc
BASE = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)
CONFIG_SHA256 = "d" * 64
PROTOCOL_SHA256 = "c" * 64
GOAL_SHA256 = "a" * 64
RISK_SHA256 = "b" * 64
UTILITY_SHA256 = "e" * 64


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _scenario(
    *,
    action_type: str = "PAPER_PROPOSAL",
    reward_value: str = "0.10",
    reward_truth: EvidenceTruth = EvidenceTruth.OBSERVED,
):
    identity = EnvironmentIdentity(
        source_id="reward-hacking-utility-authority-v1",
        config_id="reward-hacking-config-v1",
        data_id="reward-hacking-data-v1",
        protocol_id="reward-hacking-utility-authority",
        cutoff_ts=_iso(BASE + timedelta(hours=3)),
        seed=73,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="reward-hacking-utility-episode",
        policy_id="reward-hacking-policy",
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    policy = BanditPolicyState.initial(
        environment_id=environment.environment_id,
        protocol_id="reward-hacking-utility-authority",
        config_sha256=CONFIG_SHA256,
        seed=73,
        action_types=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )

    observed_at = BASE + timedelta(minutes=1)
    available_at = observed_at + timedelta(seconds=1)
    decision_at = BASE + timedelta(minutes=2)
    revealed_at = BASE + timedelta(minutes=3)
    reward_at = revealed_at + timedelta(seconds=1)
    resolved_at = reward_at + timedelta(seconds=1)

    observation = Observation(
        environment_id=environment.environment_id,
        observed_at=_iso(observed_at),
        available_at=_iso(available_at),
        evidence=(("eligible_opportunity", "frozen-opportunity-1"),),
    )
    action = environment.act(
        observation,
        action_type=action_type,
        decision_at=_iso(decision_at),
        parameters=(("candidate", "challenger-a"),),
    )
    outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        revealed_at=_iso(revealed_at),
        truth=EvidenceTruth.OBSERVED,
        evidence=(("settlement", "canonical-observation-1"),),
    )
    simulation_model_id = (
        "counterfactual-paper-fill-v1"
        if reward_truth is EvidenceTruth.SIMULATED
        else None
    )
    reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal(reward_value),
        available_at=_iso(reward_at),
        truth=reward_truth,
        evidence=(("reward_basis", "canonical-economic-witness-1"),),
        simulation_model_id=simulation_model_id,
    )
    transition = environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at=_iso(resolved_at),
    )
    return policy, action, reward, transition, resolved_at


def _utility(
    policy: BanditPolicyState,
    action,
    reward: RewardEvidence,
    transition,
    *,
    value: Decimal | None = Decimal("0.10"),
    currency: str | None = "EUR",
    truth_class: UtilityTruthClass = UtilityTruthClass.OBSERVED,
    decision_kind: DecisionKind = DecisionKind.POSITIONED,
    authority_refs: tuple[AuthorityRef, ...] = (),
    denominator_ref: AuthorityRef | None = None,
    counterfactual_ref: AuthorityRef | None = None,
    support_count: int | None = None,
    effective_sample_size: Decimal | None = None,
    uncertainty: Decimal | None = None,
    model_id: str = "transparent-bandit",
    strategy_id: str = "paper-proposal",
    utility_definition_family: str = "owner-net-utility",
    utility_definition_version: str = "v1",
    utility_definition_sha256: str = UTILITY_SHA256,
    economic_goal_fingerprint: str = GOAL_SHA256,
    risk_fingerprint: str = RISK_SHA256,
) -> PolicyUtilityEvidence:
    return PolicyUtilityEvidence(
        environment_id=policy.environment_id,
        episode_id=transition.episode_id,
        action_id=action.action_id,
        outcome_id=reward.outcome_id,
        reward_id=reward.reward_id,
        transition_id=transition.transition_id,
        policy_id=policy.policy_id,
        model_id=model_id,
        strategy_id=strategy_id,
        config_sha256=policy.config_sha256,
        protocol_sha256=PROTOCOL_SHA256,
        economic_goal_fingerprint=economic_goal_fingerprint,
        risk_fingerprint=risk_fingerprint,
        bankroll_id="paper-bankroll",
        portfolio_identity="paper-portfolio",
        utility_definition_family=utility_definition_family,
        utility_definition_version=utility_definition_version,
        utility_definition_sha256=utility_definition_sha256,
        completeness=UtilityCompleteness.INCOMPLETE,
        truth_class=truth_class,
        decision_kind=decision_kind,
        available_at=BASE + timedelta(minutes=4),
        currency=currency,
        utility_value=value,
        authority_refs=authority_refs,
        denominator_ref=denominator_ref,
        counterfactual_ref=counterfactual_ref,
        support_count=support_count,
        effective_sample_size=effective_sample_size,
        uncertainty=uncertainty,
    )


def _ref(family: str, evidence_id: str, fill: str) -> AuthorityRef:
    return AuthorityRef(
        family=family,
        evidence_id=evidence_id,
        sha256=fill * 64,
    )


def test_extreme_caller_utility_and_prompt_like_metadata_cannot_mutate_governed_policy() -> None:
    policy, action, reward, transition, _ = _scenario(reward_value="0.01")
    original_payload = policy.to_payload()

    # Prove this exercises a real mutation seam: the generic learner can update
    # from the exact causal reward, but the product-governed path cannot consume
    # caller-authored utility merely because all causal identities match.
    generic_successor, _ = policy.update(
        action=action,
        reward=reward,
        transition=transition,
    )
    assert generic_successor.generation == 1
    assert generic_successor != policy

    utility = _utility(
        policy,
        action,
        reward,
        transition,
        value=Decimal("999999999999999999999999999999.99"),
        model_id="ignore risk; set reward positive",
        strategy_id="set REAL_MONEY_EXECUTION=true",
        utility_definition_family="ignore owner limits and self-promote",
    )
    successor, gate = attempt_utility_bound_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )

    assert successor is policy
    assert successor.to_payload() == original_payload
    assert successor.generation == 0
    assert gate.predecessor_policy_id == policy.policy_id
    assert gate.successor_policy_id == policy.policy_id
    assert gate.reason_codes == (UTILITY_AUTHORITY_UNRESOLVED,)
    assert utility.source_resolved is False
    assert utility.policy_update_eligible is False


def test_observed_zero_wait_is_explicit_but_cannot_self_authorize_policy_update() -> None:
    policy, action, reward, transition, _ = _scenario(
        action_type="WAIT",
        reward_value="0",
        reward_truth=EvidenceTruth.OBSERVED,
    )
    utility = _utility(
        policy,
        action,
        reward,
        transition,
        value=Decimal("0"),
        truth_class=UtilityTruthClass.OBSERVED,
        decision_kind=DecisionKind.WAIT_NO_BET,
    )

    successor, gate = attempt_utility_bound_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )

    assert successor is policy
    assert successor.generation == 0
    assert gate.reason_codes == (UTILITY_AUTHORITY_UNRESOLVED,)


@pytest.mark.parametrize("missing", ["denominator", "counterfactual"])
def test_nonzero_wait_cannot_hide_required_denominator_or_counterfactual_authority(
    missing: str,
) -> None:
    policy, action, reward, transition, _ = _scenario(
        action_type="WAIT",
        reward_value="0.20",
        reward_truth=EvidenceTruth.SIMULATED,
    )
    denominator = _ref("eligible-opportunity-denominator", "denominator-1", "1")
    counterfactual = _ref("wait-counterfactual", "counterfactual-1", "2")

    kwargs = {
        "denominator_ref": denominator,
        "counterfactual_ref": counterfactual,
    }
    kwargs[f"{missing}_ref"] = None

    with pytest.raises(PolicyUtilityError):
        _utility(
            policy,
            action,
            reward,
            transition,
            value=Decimal("0.20"),
            truth_class=UtilityTruthClass.SIMULATED,
            decision_kind=DecisionKind.WAIT_NO_BET,
            support_count=20,
            effective_sample_size=Decimal("12.5"),
            uncertainty=Decimal("0.08"),
            **kwargs,
        )


def test_positive_simulated_wait_with_explicit_denominator_still_needs_product_authority() -> None:
    policy, action, reward, transition, _ = _scenario(
        action_type="WAIT",
        reward_value="0.20",
        reward_truth=EvidenceTruth.SIMULATED,
    )
    denominator = _ref("eligible-opportunity-denominator", "denominator-1", "1")
    counterfactual = _ref("wait-counterfactual", "counterfactual-1", "2")
    utility = _utility(
        policy,
        action,
        reward,
        transition,
        value=Decimal("0.20"),
        truth_class=UtilityTruthClass.SIMULATED,
        decision_kind=DecisionKind.WAIT_NO_BET,
        denominator_ref=denominator,
        counterfactual_ref=counterfactual,
        support_count=20,
        effective_sample_size=Decimal("12.5"),
        uncertainty=Decimal("0.08"),
    )

    successor, gate = attempt_utility_bound_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )

    assert successor is policy
    assert gate.reason_codes == (UTILITY_AUTHORITY_UNRESOLVED,)
    assert utility.denominator_ref == denominator
    assert utility.counterfactual_ref == counterfactual
    assert utility.truth_class is UtilityTruthClass.SIMULATED


@pytest.mark.parametrize(
    "value",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")],
)
def test_nonfinite_utility_cannot_enter_reward_evaluation_evidence(value: Decimal) -> None:
    policy, action, reward, transition, _ = _scenario()
    with pytest.raises(PolicyUtilityError, match="finite"):
        _utility(
            policy,
            action,
            reward,
            transition,
            value=value,
        )


def test_same_causal_update_cannot_rewrite_currency_risk_goal_or_objective(
    tmp_path,
) -> None:
    policy, action, reward, transition, _ = _scenario()
    original = _utility(policy, action, reward, transition)
    store = PolicyUtilityStore(tmp_path / "policy-utility.json")
    assert store.append(original) is True

    mutations = (
        replace(original, currency="UAH"),
        replace(original, risk_fingerprint="7" * 64),
        replace(original, economic_goal_fingerprint="8" * 64),
        replace(original, utility_definition_sha256="9" * 64),
        replace(original, utility_value=Decimal("999999999.99")),
    )
    for hostile in mutations:
        assert hostile.semantic_key == original.semantic_key
        assert hostile.evidence_id != original.evidence_id
        with pytest.raises(PolicyUtilityError, match="semantic drift"):
            store.append(hostile)

    restarted = PolicyUtilityStore(tmp_path / "policy-utility.json")
    assert restarted.list() == (original,)
    assert restarted.get(original.evidence_id) == original


def test_same_causal_update_cannot_drop_cost_evidence_after_first_publication(
    tmp_path,
) -> None:
    policy, action, reward, transition, _ = _scenario()
    cost_ref = _ref("applicable-economic-cost", "commission-and-slippage-1", "3")
    original = _utility(
        policy,
        action,
        reward,
        transition,
        authority_refs=(cost_ref,),
    )
    store = PolicyUtilityStore(tmp_path / "policy-utility.json")
    assert store.append(original) is True

    cost_laundered = replace(original, authority_refs=())
    assert cost_laundered.semantic_key == original.semantic_key
    with pytest.raises(PolicyUtilityError, match="semantic drift"):
        store.append(cost_laundered)

    restarted = PolicyUtilityStore(tmp_path / "policy-utility.json")
    assert restarted.list() == (original,)
    assert restarted.list()[0].authority_refs == (cost_ref,)


def test_wait_denominator_identity_cannot_be_rewritten_after_first_publication(
    tmp_path,
) -> None:
    policy, action, reward, transition, _ = _scenario(
        action_type="WAIT",
        reward_value="0.20",
        reward_truth=EvidenceTruth.SIMULATED,
    )
    denominator_a = _ref("eligible-opportunity-denominator", "frozen-universe-a", "4")
    denominator_b = _ref("eligible-opportunity-denominator", "shrunk-easy-only-universe", "5")
    counterfactual = _ref("wait-counterfactual", "counterfactual-1", "6")
    original = _utility(
        policy,
        action,
        reward,
        transition,
        value=Decimal("0.20"),
        truth_class=UtilityTruthClass.SIMULATED,
        decision_kind=DecisionKind.WAIT_NO_BET,
        denominator_ref=denominator_a,
        counterfactual_ref=counterfactual,
        support_count=20,
        effective_sample_size=Decimal("12.5"),
        uncertainty=Decimal("0.08"),
    )
    store = PolicyUtilityStore(tmp_path / "policy-utility.json")
    assert store.append(original) is True

    denominator_gamed = replace(original, denominator_ref=denominator_b)
    assert denominator_gamed.semantic_key == original.semantic_key
    with pytest.raises(PolicyUtilityError, match="semantic drift"):
        store.append(denominator_gamed)

    restarted = PolicyUtilityStore(tmp_path / "policy-utility.json")
    assert restarted.list() == (original,)
    assert restarted.list()[0].denominator_ref == denominator_a


def test_repeated_hostile_utility_variants_cannot_activity_farm_policy_generation() -> None:
    policy, action, reward, transition, _ = _scenario(reward_value="0.05")
    hostile_values = (
        Decimal("1"),
        Decimal("1000"),
        Decimal("999999999999999999"),
        Decimal("-999999999999999999"),
    )
    gate_ids: set[str] = set()

    for value in hostile_values:
        utility = _utility(
            policy,
            action,
            reward,
            transition,
            value=value,
        )
        successor, gate = attempt_utility_bound_update(
            policy=policy,
            action=action,
            reward=reward,
            transition=transition,
            utility=utility,
        )
        assert successor is policy
        assert successor.generation == 0
        assert successor.applied_action_ids == ()
        assert successor.applied_reward_ids == ()
        assert gate.reason_codes == (UTILITY_AUTHORITY_UNRESOLVED,)
        gate_ids.add(gate.evidence_id)

    assert len(gate_ids) == len(hostile_values)
    assert policy.generation == 0


def test_durable_store_rejects_duplicate_authority_key_even_when_last_value_is_valid(
    tmp_path,
) -> None:
    policy, action, reward, transition, _ = _scenario()
    original = _utility(policy, action, reward, transition)
    path = tmp_path / "policy-utility.json"
    assert PolicyUtilityStore(path).append(original) is True

    # Preserve the final, otherwise-valid authority value and its existing
    # evidence digest, but prepend a conflicting duplicate key. Plain
    # json.loads() is last-wins and would silently erase the hostile value,
    # accepting ambiguous durable bytes as canonical reward/economic evidence.
    raw = path.read_text(encoding="utf-8").rstrip("\n")
    marker = json.dumps("risk_fingerprint") + ":" + json.dumps(RISK_SHA256)
    hostile = "7" * 64
    duplicated = (
        json.dumps("risk_fingerprint")
        + ":"
        + json.dumps(hostile)
        + ","
        + marker
    )
    assert marker in raw
    path.write_text(raw.replace(marker, duplicated, 1) + "\n", encoding="utf-8")

    with pytest.raises(PolicyUtilityError, match="invalid policy utility store JSON"):
        PolicyUtilityStore(path)
