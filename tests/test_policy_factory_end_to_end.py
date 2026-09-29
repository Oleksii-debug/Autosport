from datetime import datetime, timezone

import hashlib
from autosport.integrity import atomic_write_json
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.external_validity_baseline import (
    BaselineDefinition,
    BaselineKind,
    EvaluationContractFamily,
    FrozenBaselineProtocol,
    FrozenEvidenceScope,
    PolicyEvaluation as ExternalPolicyEvaluation,
    canonical_evaluation_contract,
)
from autosport.external_validity_policy_issuance import (
    IssuedPolicyEvaluationRef,
    ProductPolicyEvaluationIssuanceError,
    ProductPolicyEvaluationWorkspace,
    canonical_product_policy_evaluation_bundle_sha256,
    issue_product_policy_evaluation,
    resolve_product_policy_evaluation,
    verify_product_policy_evaluation,
)
from autosport.monotonic_workspace_binding import WorkspaceIdentityBinding
from autosport.opportunity import StrategyClass

from autosport.champion_policy import persist_policy_state
from autosport.experiential_learning import PolicyRetestSpec
from autosport.learning_environment import Action, EvidenceTruth, RewardEvidence, Transition
from autosport.policy_evaluation import (
    PolicyEvaluationCase,
    PolicyEvaluationConfig,
    QualifiedCounterfactualAuthority,
    PolicyRewardMode,
    evaluate_policy_pair,
    policy_evaluation_cases_manifest_sha256,
)
from autosport.scientific_registry import (
    CounterfactualQualification,
    CounterfactualSourceEvidence,
    DatasetSnapshot,
    EvaluationBundleRef,
    ExperimentRecord,
    FeatureSet,
    Hypothesis,
    ModelVersion,
    PromotionAction,
    PromotionDecision,
    ResearchOutcome,
    ResearchProtocol,
    ResearchQuestion,
    ScientificRegistry,
    StrategyVersion,
)
from autosport.strategy_experiment import ScientificProtocolBinding
from autosport.strategy_model_factory import (
    ExperimentRunner,
    FactoryArtifactStore,
    PromotionRule,
    PromotionVerdict,
    _StagedFactoryArtifactStore,
)
from autosport.transparent_bandit_policy import BanditPolicyState


ENVIRONMENT = "a" * 64
CONFIG = "b" * 64
SOURCE = "c" * 64
EVALUATOR_SOURCE = "d" * 64
FEATURE_DEFINITION = "e" * 64
FEATURE_SOURCE = "f" * 64
REWARD_DEFINITION = "1" * 64
COST_DEFINITION = "2" * 64
CANONICAL_STRATEGY = "canonical-policy-e2e"
PROTOCOL_ID = "protocol-policy-e2e-v1"
DATASET_ID = "dataset-policy-e2e-v1"
FEATURE_SET_ID = "features-policy-e2e-v1"
FEATURE_VERSION = "policy-features-v1"

QUESTION_AT = "2026-09-19T09:00:00Z"
HYPOTHESIS_AT = "2026-09-19T09:02:00Z"
FEATURE_AT = "2026-09-19T09:03:00Z"
QUALIFICATION_AT = "2026-09-19T09:04:00Z"
FROZEN_AT = "2026-09-19T09:10:00Z"
PROTOCOL_AT = "2026-09-19T09:11:00Z"
DATASET_AT = "2026-09-19T09:12:00Z"
CAUSAL_CUTOFF = "2026-09-19T09:09:00Z"
HOLDOUT_REVEAL_AT = "2026-09-19T09:20:00Z"
BOOTSTRAP_CREATED = "2026-09-19T09:25:00Z"
BOOTSTRAP_COMPLETED = "2026-09-19T09:30:00Z"
BOOTSTRAP_DECIDED = "2026-09-19T09:31:00Z"
CHALLENGER_CREATED = "2026-09-19T09:40:00Z"
CHALLENGER_COMPLETED = "2026-09-19T10:00:00Z"
CHALLENGER_DECIDED = "2026-09-19T10:01:00Z"
SECOND_CREATED = "2026-09-19T10:10:00Z"
SECOND_COMPLETED = "2026-09-19T10:20:00Z"
SECOND_DECIDED = "2026-09-19T10:21:00Z"


class _FixtureClock:
    def __init__(self):
        self._times = iter(
            (
                datetime(2026, 9, 19, 9, 4, tzinfo=timezone.utc),
                datetime(2026, 9, 19, 9, 20, tzinfo=timezone.utc),
                datetime(2026, 9, 19, 9, 20, tzinfo=timezone.utc),
            )
        )

    def __call__(self):
        return next(
            self._times,
            datetime(2026, 9, 19, 9, 40, tzinfo=timezone.utc),
        )


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _cases(store: FactoryArtifactStore) -> tuple[PolicyEvaluationCase, ...]:
    authority_id="mechanical-paper-settlement:v1"
    def build(sample_id: str, observed_at: str) -> PolicyEvaluationCase:
        bound_case={"sample_id":sample_id,"observed_at":observed_at,"reward_available_at":HOLDOUT_REVEAL_AT,"admissible_actions":["BET","HEDGE","WAIT"],"action_rewards":[["BET","0"],["HEDGE","2"],["WAIT","1"]],"action_costs":[["BET","0"],["HEDGE","0"],["WAIT","0"]],"behavior_propensities":[["BET","0.34"],["HEDGE","0.33"],["WAIT","0.33"]],"reward_truth":EvidenceTruth.OBSERVED.value,"reward_mode":PolicyRewardMode.MECHANICAL_PAPER.value,"regime_id":"table-tennis:pre-match","historical_action":None,"counterfactual_source_id":authority_id}
        source_sha=_digest({"schema_version":1,"kind":"autosport-counterfactual-source-evidence-v1","authority_id":authority_id,"authority_version":"1","case":bound_case})
        return PolicyEvaluationCase(sample_id,observed_at,HOLDOUT_REVEAL_AT,("BET","HEDGE","WAIT"),(("BET",Decimal("0")),("HEDGE",Decimal("2")),("WAIT",Decimal("1"))),(("BET",Decimal("0")),("HEDGE",Decimal("0")),("WAIT",Decimal("0"))),(("BET",Decimal("0.34")),("HEDGE",Decimal("0.33")),("WAIT",Decimal("0.33"))),EvidenceTruth.OBSERVED,PolicyRewardMode.MECHANICAL_PAPER,source_sha,"table-tennis:pre-match",None,authority_id)
    return (build("holdout-1","2026-09-19T09:05:00Z"),build("holdout-2","2026-09-19T09:06:00Z"))


def _policy_successor(
    predecessor: BanditPolicyState,
    *,
    observation_id: str,
    outcome_id: str,
    episode_id: str,
    decided_at: str,
    available_at: str,
    reward_value: str,
    action_type: str = "WAIT",
):
    action = Action(
        environment_id=ENVIRONMENT,
        observation_id=observation_id,
        action_type=action_type,
        decided_at=decided_at,
    )
    reward = RewardEvidence(
        environment_id=ENVIRONMENT,
        action_id=action.action_id,
        outcome_id=outcome_id,
        reward=Decimal(reward_value),
        available_at=available_at,
        truth=EvidenceTruth.OBSERVED,
    )
    transition = Transition(
        environment_id=ENVIRONMENT,
        episode_id=episode_id,
        step_index=predecessor.generation + 1,
        observation_id=action.observation_id,
        action_id=action.action_id,
        outcome_id=reward.outcome_id,
        reward_id=reward.reward_id,
        decision_at=action.decided_at,
        resolved_at=reward.available_at,
    )
    return predecessor.update(action=action, reward=reward, transition=transition)


def _foundation(tmp_path, *, artifact_directory: str = "artifacts"):
    artifact_root = tmp_path / artifact_directory
    clock = _FixtureClock()
    store = FactoryArtifactStore(artifact_root, clock=clock)
    cases = _cases(store)
    dataset_manifest = policy_evaluation_cases_manifest_sha256(cases)
    qualification_evidence_sha256 = store.materialize(
        "counterfactual-qualification",
        "mechanical-paper-settlement:v1@1",
        {
            "schema_version": 1,
            "kind": "autosport-counterfactual-qualification-v1",
            "authority_id": "mechanical-paper-settlement:v1",
            "authority_version": "1",
            "evaluator_source_sha256": EVALUATOR_SOURCE,
            "reward_definition_sha256": REWARD_DEFINITION,
            "reward_mode": PolicyRewardMode.MECHANICAL_PAPER.value,
            "scope": "table-tennis:pre-match",
            "qualification_status": "QUALIFIED",
            "qualified_at": QUALIFICATION_AT,
        },
    )
    counterfactual_authority = QualifiedCounterfactualAuthority(
        authority_id="mechanical-paper-settlement:v1",
        authority_version="1",
        evaluator_source_sha256=EVALUATOR_SOURCE,
        qualification_evidence_sha256=qualification_evidence_sha256,
        reward_definition_sha256=REWARD_DEFINITION,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        scope="table-tennis:pre-match",
        qualification_status="QUALIFIED",
    )
    evaluator_config = PolicyEvaluationConfig(
        FEATURE_SET_ID,
        FEATURE_DEFINITION,
        FEATURE_SOURCE,
        REWARD_DEFINITION,
        COST_DEFINITION,
        "WAIT",
        counterfactual_authority,
    )
    rule = PromotionRule(
        "policy_loss",
        1.0,
        (("downside_loss", 0.0), ("max_drawdown", 0.0)),
        minimum_effective_sample_size=2,
    )
    question = ResearchQuestion(
        "question-policy-e2e",
        "Does the learned action policy improve unseen net reward?",
        SOURCE,
        QUESTION_AT,
    )
    hypothesis = Hypothesis(
        "hypothesis-policy-e2e",
        question.question_id,
        "The challenger improves unseen paired policy reward.",
        "Paired policy_loss improves by at least one unit.",
        "Reject or retain if the threshold or protective metrics fail.",
        "policy_loss",
        ("downside_loss", "max_drawdown"),
        HYPOTHESIS_AT,
    )
    binding = ScientificProtocolBinding(
        research_protocol_id=PROTOCOL_ID,
        research_question_id=question.question_id,
        research_question_sha256=_digest(question.to_payload()),
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=_digest(hypothesis.to_payload()),
        inclusion_criteria="Frozen mechanically reconstructable paper holdout cases only.",
        exclusion_criteria="Exclude unsupported counterfactual or unavailable reward evidence.",
        lawful_source_requirements="Autosport-owned paper evidence with explicit provenance.",
        causal_cutoff=CAUSAL_CUTOFF,
        evaluation_design=evaluator_config.frozen_text,
        feature_set_version=FEATURE_VERSION,
        uncertainty_method="paired min/max interval",
        multiple_comparison_control="single frozen challenger family",
        robustness_checks=("paired support", "regime attribution"),
        random_seed_policy="fixed seed 7",
        stopping_rule="evaluate the frozen two-case holdout exactly once",
        promotion_rule=rule.frozen_text,
        expected_artifacts=("policy-evaluation", "promotion-evidence", "reproducibility"),
        code_config_sha256=CONFIG,
        frozen_at_utc=FROZEN_AT,
    )
    protocol = ResearchProtocol(
        binding,
        SOURCE,
        ENVIRONMENT,
        dataset_manifest,
        PROTOCOL_AT,
    )
    dataset = DatasetSnapshot(
        DATASET_ID,
        dataset_manifest,
        "autosport-policy-e2e-fixture",
        "test-fixture",
        CAUSAL_CUTOFF,
        DATASET_AT,
        outcome_reveal_after=HOLDOUT_REVEAL_AT,
    )
    feature = FeatureSet(
        FEATURE_SET_ID,
        FEATURE_VERSION,
        FEATURE_DEFINITION,
        FEATURE_SOURCE,
        FEATURE_AT,
    )

    registry_path = tmp_path / "scientific_registry.json"
    artifact_root = tmp_path / artifact_directory
    registry = ScientificRegistry.initialize_pristine(registry_path)
    for record in (question, hypothesis, feature):
        registry.append(record)
    registry.append(CounterfactualQualification("mechanical-paper-settlement:v1","1",EVALUATOR_SOURCE,REWARD_DEFINITION,PolicyRewardMode.MECHANICAL_PAPER.value,"table-tennis:pre-match","QUALIFIED",QUALIFICATION_AT,QUALIFICATION_AT,qualification_evidence_sha256))
    registry.append(protocol)
    registry.append(dataset)
    for case in cases:
        bound_case=dict(case.canonical_payload()); source_sha=bound_case.pop("source_evidence_sha256")
        identity=f"mechanical-paper-settlement:v1@1:{case.sample_id}"
        store.materialize("counterfactual-source-evidence",identity,{"schema_version":1,"kind":"autosport-counterfactual-source-evidence-v1","authority_id":"mechanical-paper-settlement:v1","authority_version":"1","case":bound_case})
        registry.append(CounterfactualSourceEvidence("mechanical-paper-settlement:v1","1",case.sample_id,source_sha,case.observed_at,case.reward_available_at,HOLDOUT_REVEAL_AT,DATASET_ID))
    store = FactoryArtifactStore(artifact_root, clock=clock)

    predecessor = BanditPolicyState.initial(
        environment_id=ENVIRONMENT,
        protocol_id=PROTOCOL_ID,
        config_sha256=CONFIG,
        seed=7,
        action_types=frozenset({"BET", "HEDGE", "WAIT"}),
    )
    predecessor_policy_sha = persist_policy_state(store, predecessor)
    predecessor_model_id = "model-policy-bootstrap"
    predecessor_model_sha = store.write(
        "model",
        predecessor_model_id,
        {
            "schema_version": 1,
            "kind": "autosport-transparent-bandit-policy-model-v1",
            "model_version_id": predecessor_model_id,
            "policy_id": predecessor.policy_id,
            "policy_artifact_sha256": predecessor_policy_sha,
            "research_protocol_id": PROTOCOL_ID,
            "dataset_snapshot_id": DATASET_ID,
            "feature_set_id": FEATURE_SET_ID,
            "config_sha256": CONFIG,
            "seed": 7,
            "bootstrap_fixture": True,
        },
    )
    predecessor_evaluation_id = "evaluation-policy-bootstrap"
    predecessor_evaluation_sha = store.write(
        "evaluation",
        predecessor_evaluation_id,
        {
            "schema_version": 1,
            "kind": "bootstrap-champion-fixture",
            "policy_id": predecessor.policy_id,
        },
    )
    registry.append(
        ModelVersion(
            predecessor_model_id,
            "transparent-bandit-policy-v1",
            predecessor_model_sha,
            SOURCE,
            ENVIRONMENT,
            DATASET_ID,
            FEATURE_SET_ID,
            PROTOCOL_ID,
            7,
            CONFIG,
            BOOTSTRAP_CREATED,
        )
    )
    registry.append(
        StrategyVersion(
            predecessor.policy_id,
            CANONICAL_STRATEGY,
            SOURCE,
            ENVIRONMENT,
            CONFIG,
            BOOTSTRAP_CREATED,
            model_version_id=predecessor_model_id,
        )
    )
    registry.append(
        EvaluationBundleRef(
            predecessor_evaluation_id,
            predecessor_evaluation_sha,
            EVALUATOR_SOURCE,
            DATASET_ID,
            protocol.protocol_sha256,
            (predecessor_model_sha, predecessor_policy_sha),
            BOOTSTRAP_COMPLETED,
            evaluated_strategy_version_id=predecessor.policy_id,
            evaluated_model_version_id=predecessor_model_id,
        )
    )
    registry.append(
        ExperimentRecord(
            "experiment-policy-bootstrap",
            PROTOCOL_ID,
            DATASET_ID,
            FEATURE_SET_ID,
            predecessor.policy_id,
            predecessor_evaluation_id,
            7,
            CONFIG,
            ResearchOutcome.POSITIVE,
            BOOTSTRAP_CREATED,
            model_version_id=predecessor_model_id,
            completed_at=BOOTSTRAP_COMPLETED,
            notes="test fixture represents an already-qualified durable champion",
        )
    )
    # This fixture models a champion that predates the holdout trial exercised by
    # this test.  It is appended through the private immutable-record primitive so
    # the test does not consume the very holdout whose single-use behavior it tests.
    registry._append(
        PromotionDecision(
            "promotion-policy-bootstrap",
            PromotionAction.PROMOTE,
            predecessor.policy_id,
            PROTOCOL_ID,
            protocol.protocol_sha256,
            predecessor_evaluation_id,
            predecessor_evaluation_sha,
            BOOTSTRAP_DECIDED,
            predecessor_strategy_version_id=None,
            candidate_model_version_id=predecessor_model_id,
            promotion_evidence_id=None,
            reason="pre-existing champion fixture",
        )
    )
    return (
        registry,
        registry_path,
        artifact_root,
        store,
        predecessor,
        predecessor_model_id,
        cases,
        rule,
    )


def _spec(
    *,
    experiment_id: str,
    model_id: str,
    evaluation_id: str,
    promotion_id: str,
    predecessor_policy_id: str,
    predecessor_model_id: str,
    created_at: str,
    completed_at: str,
    decided_at: str,
) -> PolicyRetestSpec:
    return PolicyRetestSpec(
        experiment_id=experiment_id,
        model_version_id=model_id,
        evaluation_bundle_id=evaluation_id,
        promotion_decision_id=promotion_id,
        canonical_strategy_id=CANONICAL_STRATEGY,
        dataset_snapshot_id=DATASET_ID,
        feature_set_id=FEATURE_SET_ID,
        source_sha256=SOURCE,
        evaluator_source_sha256=EVALUATOR_SOURCE,
        created_at=created_at,
        completed_at=completed_at,
        decided_at=decided_at,
        predecessor_strategy_version_id=predecessor_policy_id,
        predecessor_model_version_id=predecessor_model_id,
    )


def _run_nongoverned_factory_retest(
    runner: ExperimentRunner,
    *,
    predecessor_policy: BanditPolicyState,
    challenger_policy: BanditPolicyState,
    update_evidence,
    spec: PolicyRetestSpec,
    points=(),
    rule: PromotionRule,
    evaluation_cases=None,
):
    """Exercise the lower-level factory/evaluation seam without product utility authority.

    These legacy scientific/factory regressions intentionally test qualification,
    materialization, promotion and restart invariants below the governed product update
    boundary. They must not mint a fake positive UtilityBoundUpdateEvidence merely to
    reach those lower-level invariants.
    """
    if points:
        raise ValueError("baseline TrainingPoint evidence cannot authorize a learned policy retest")
    if update_evidence.predecessor_policy_id != predecessor_policy.policy_id:
        raise ValueError("policy update predecessor identity mismatch")
    if update_evidence.successor_policy_id != challenger_policy.policy_id:
        raise ValueError("policy update successor identity mismatch")

    protocol = runner.registry.get("ResearchProtocol", challenger_policy.protocol_id)
    if protocol is None:
        raise ValueError("challenger policy research protocol is missing from ScientificRegistry")
    binding = protocol.payload.get("binding")
    if type(binding) is not dict:
        raise ValueError("challenger policy research protocol lacks frozen binding")
    if evaluation_cases is None:
        raise ValueError("policy-specific causal evaluation cases are required for learned policy retest")

    factory_spec = spec.factory_spec(challenger_policy)
    evaluation_config = PolicyEvaluationConfig.from_frozen_text(
        binding.get("evaluation_design")
    )
    authority = evaluation_config.counterfactual_authority
    if (
        authority is not None
        and authority.evaluator_source_sha256 != spec.evaluator_source_sha256.lower()
    ):
        raise ValueError(
            "counterfactual authority evaluator identity does not match factory spec"
        )
    evaluation = evaluate_policy_pair(
        predecessor_policy,
        challenger_policy,
        evaluation_cases,
        completed_at=spec.completed_at,
        abstain_action=evaluation_config.abstain_action,
        counterfactual_authority=authority,
    )
    if evaluation.dataset_manifest_sha256 != protocol.payload.get(
        "dataset_manifest_sha256"
    ):
        raise ValueError(
            "policy evaluation cohort does not match frozen research protocol dataset"
        )
    if spec.predecessor_strategy_version_id != predecessor_policy.policy_id:
        raise ValueError(
            "policy retest rollback strategy must be the exact evaluated predecessor policy"
        )

    persist_policy_state(runner.artifact_store, predecessor_policy)
    challenger_artifact_sha256 = persist_policy_state(
        runner.artifact_store, challenger_policy
    )
    return runner.run_policy_candidate(
        factory_spec,
        evaluation,
        rule=rule,
        policy_artifact_sha256=challenger_artifact_sha256,
    )


def test_policy_factory_promotes_evaluated_policy_and_verifies_restart(tmp_path):
    (
        registry,
        registry_path,
        artifact_root,
        store,
        predecessor,
        predecessor_model_id,
        cases,
        rule,
    ) = _foundation(tmp_path)
    challenger, update = _policy_successor(
        predecessor,
        observation_id="5" * 64,
        outcome_id="6" * 64,
        episode_id="7" * 64,
        decided_at="2026-09-19T09:33:00Z",
        available_at="2026-09-19T09:34:00Z",
        reward_value="2",
    )
    runner = ExperimentRunner(registry, store)
    spec = _spec(
        experiment_id="experiment-policy-v2",
        model_id="model-policy-v2",
        evaluation_id="evaluation-policy-v2",
        promotion_id="promotion-policy-v2",
        predecessor_policy_id=predecessor.policy_id,
        predecessor_model_id=predecessor_model_id,
        created_at=CHALLENGER_CREATED,
        completed_at=CHALLENGER_COMPLETED,
        decided_at=CHALLENGER_DECIDED,
    )

    result = _run_nongoverned_factory_retest(
        runner,
        predecessor_policy=predecessor,
        challenger_policy=challenger,
        update_evidence=update,
        spec=spec,
        points=(),
        evaluation_cases=cases,
        rule=rule,
    )

    assert result.verdict is PromotionVerdict.PROMOTE
    assert result.registry_action is PromotionAction.PROMOTE
    assert result.strategy_version_id == challenger.policy_id
    assert result.candidate_metrics["policy_loss"] == -1.0

    decision = registry.get("PromotionDecision", spec.promotion_decision_id)
    assert decision is not None
    evidence_id = decision.payload["promotion_evidence_id"]
    evidence = registry.get("PromotionEvidence", evidence_id)
    assert evidence is not None
    assert evidence.payload["candidate_strategy_version_id"] == challenger.policy_id
    assert evidence.payload["candidate_model_version_id"] == spec.model_version_id
    assert evidence.payload["holdout_consumed"] is False
    assert registry.champion_strategy(
        as_of=CHALLENGER_DECIDED,
        canonical_strategy_id=CANONICAL_STRATEGY,
    ) == challenger.policy_id

    restart = ExperimentRunner.verify_policy_restart(
        registry_path,
        artifact_root,
        spec.experiment_id,
        as_of=CHALLENGER_DECIDED,
    )
    assert restart.experiment_id == spec.experiment_id
    assert restart.champion_strategy_version_id == challenger.policy_id
    assert restart.outcome is ResearchOutcome.POSITIVE
    assert restart.reproducibility_bundle_sha256 == result.reproducibility_bundle_sha256


def test_policy_retest_rejects_mismatched_counterfactual_evaluator_before_publish(
    tmp_path,
):
    (
        registry,
        _,
        _,
        store,
        predecessor,
        predecessor_model_id,
        cases,
        rule,
    ) = _foundation(tmp_path)
    challenger, update = _policy_successor(
        predecessor,
        observation_id="5" * 64,
        outcome_id="6" * 64,
        episode_id="7" * 64,
        decided_at="2026-09-19T09:33:00Z",
        available_at="2026-09-19T09:34:00Z",
        reward_value="2",
    )
    runner = ExperimentRunner(registry, store)
    spec = _spec(
        experiment_id="experiment-policy-authority-mismatch",
        model_id="model-policy-authority-mismatch",
        evaluation_id="evaluation-policy-authority-mismatch",
        promotion_id="promotion-policy-authority-mismatch",
        predecessor_policy_id=predecessor.policy_id,
        predecessor_model_id=predecessor_model_id,
        created_at=CHALLENGER_CREATED,
        completed_at=CHALLENGER_COMPLETED,
        decided_at=CHALLENGER_DECIDED,
    )
    bad_spec = replace(spec, evaluator_source_sha256="0" * 64)

    import pytest

    with pytest.raises(ValueError, match="authority evaluator identity"):
        _run_nongoverned_factory_retest(
            runner,
            predecessor_policy=predecessor,
            challenger_policy=challenger,
            update_evidence=update,
            spec=bad_spec,
            evaluation_cases=cases,
            rule=rule,
        )

    assert registry.get("EvaluationBundle", bad_spec.evaluation_bundle_id) is None
    assert registry.get("PromotionDecision", bad_spec.promotion_decision_id) is None
    assert registry.get("Experiment", bad_spec.experiment_id) is None


def test_policy_retest_rejects_self_asserted_authority_without_qualification_artifact(
    tmp_path,
):
    (
        registry,
        _,
        _,
        store,
        predecessor,
        predecessor_model_id,
        cases,
        rule,
    ) = _foundation(tmp_path)
    store.path_for_testing(
        "counterfactual-qualification",
        "mechanical-paper-settlement:v1@1",
    ).unlink()
    challenger, update = _policy_successor(
        predecessor,
        observation_id="5" * 64,
        outcome_id="6" * 64,
        episode_id="7" * 64,
        decided_at="2026-09-19T09:33:00Z",
        available_at="2026-09-19T09:34:00Z",
        reward_value="2",
    )
    spec = _spec(
        experiment_id="experiment-policy-missing-qualification",
        model_id="model-policy-missing-qualification",
        evaluation_id="evaluation-policy-missing-qualification",
        promotion_id="promotion-policy-missing-qualification",
        predecessor_policy_id=predecessor.policy_id,
        predecessor_model_id=predecessor_model_id,
        created_at=CHALLENGER_CREATED,
        completed_at=CHALLENGER_COMPLETED,
        decided_at=CHALLENGER_DECIDED,
    )

    import pytest

    with pytest.raises(
        ValueError,
        match="counterfactual-qualification",
    ):
        _run_nongoverned_factory_retest(
            ExperimentRunner(registry, store),
            predecessor_policy=predecessor,
            challenger_policy=challenger,
            update_evidence=update,
            spec=spec,
            evaluation_cases=cases,
            rule=rule,
        )

    assert registry.get("EvaluationBundle", spec.evaluation_bundle_id) is None
    assert registry.get("PromotionDecision", spec.promotion_decision_id) is None
    assert registry.get("Experiment", spec.experiment_id) is None


def test_policy_retest_rejects_unqualified_case_digest_before_publish(tmp_path):
    (
        registry,
        _,
        _,
        store,
        predecessor,
        predecessor_model_id,
        cases,
        rule,
    ) = _foundation(tmp_path)
    challenger, update = _policy_successor(
        predecessor,
        observation_id="5" * 64,
        outcome_id="6" * 64,
        episode_id="7" * 64,
        decided_at="2026-09-19T09:33:00Z",
        available_at="2026-09-19T09:34:00Z",
        reward_value="2",
    )
    spec = _spec(
        experiment_id="experiment-policy-evidence-mismatch",
        model_id="model-policy-evidence-mismatch",
        evaluation_id="evaluation-policy-evidence-mismatch",
        promotion_id="promotion-policy-evidence-mismatch",
        predecessor_policy_id=predecessor.policy_id,
        predecessor_model_id=predecessor_model_id,
        created_at=CHALLENGER_CREATED,
        completed_at=CHALLENGER_COMPLETED,
        decided_at=CHALLENGER_DECIDED,
    )
    tampered_cases = (
        replace(cases[0], source_evidence_sha256="8" * 64),
        cases[1],
    )

    import pytest

    with pytest.raises(ValueError, match="receipt does not match evaluated case"):
        _run_nongoverned_factory_retest(
            ExperimentRunner(registry, store),
            predecessor_policy=predecessor,
            challenger_policy=challenger,
            update_evidence=update,
            spec=spec,
            evaluation_cases=tampered_cases,
            rule=rule,
        )

    assert registry.get("EvaluationBundle", spec.evaluation_bundle_id) is None
    assert registry.get("PromotionDecision", spec.promotion_decision_id) is None
    assert registry.get("Experiment", spec.experiment_id) is None


def test_policy_retest_rejects_backdated_qualification_appended_after_freeze(tmp_path):
    registry, _, _, store, predecessor, predecessor_model_id, cases, rule = _foundation(tmp_path)
    state=registry._read(); state["records"]=[x for x in state["records"] if not (x["record_type"]=="CounterfactualQualification" and x["record_id"]=="mechanical-paper-settlement:v1@1")]
    atomic_write_json(registry.path,state)
    qsha=store.sha256("counterfactual-qualification","mechanical-paper-settlement:v1@1")
    registry.append(CounterfactualQualification("mechanical-paper-settlement:v1","1",EVALUATOR_SOURCE,REWARD_DEFINITION,PolicyRewardMode.MECHANICAL_PAPER.value,"table-tennis:pre-match","QUALIFIED",QUALIFICATION_AT,CHALLENGER_CREATED,qsha))
    challenger, update=_policy_successor(predecessor,observation_id="5"*64,outcome_id="6"*64,episode_id="7"*64,decided_at="2026-09-19T09:33:00Z",available_at="2026-09-19T09:34:00Z",reward_value="2")
    spec=_spec(experiment_id="experiment-backdated-qualification",model_id="model-backdated-qualification",evaluation_id="evaluation-backdated-qualification",promotion_id="promotion-backdated-qualification",predecessor_policy_id=predecessor.policy_id,predecessor_model_id=predecessor_model_id,created_at=CHALLENGER_CREATED,completed_at=CHALLENGER_COMPLETED,decided_at=CHALLENGER_DECIDED)
    import pytest
    with pytest.raises(ValueError,match="registered after protocol freeze"):
        _run_nongoverned_factory_retest(ExperimentRunner(registry,store),predecessor_policy=predecessor,challenger_policy=challenger,update_evidence=update,spec=spec,evaluation_cases=cases,rule=rule)


def test_policy_retest_rejects_revoked_counterfactual_authority(tmp_path):
    registry, _, _, store, predecessor, predecessor_model_id, cases, rule = _foundation(tmp_path)
    state=registry._read(); state["records"]=[x for x in state["records"] if not (x["record_type"]=="CounterfactualQualification" and x["record_id"]=="mechanical-paper-settlement:v1@1")]
    atomic_write_json(registry.path,state)
    qsha=store.sha256("counterfactual-qualification","mechanical-paper-settlement:v1@1")
    registry.append(CounterfactualQualification("mechanical-paper-settlement:v1","1",EVALUATOR_SOURCE,REWARD_DEFINITION,PolicyRewardMode.MECHANICAL_PAPER.value,"table-tennis:pre-match","REVOKED",QUALIFICATION_AT,QUALIFICATION_AT,qsha))
    challenger, update=_policy_successor(predecessor,observation_id="5"*64,outcome_id="6"*64,episode_id="7"*64,decided_at="2026-09-19T09:33:00Z",available_at="2026-09-19T09:34:00Z",reward_value="2")
    spec=_spec(experiment_id="experiment-revoked-authority",model_id="model-revoked-authority",evaluation_id="evaluation-revoked-authority",promotion_id="promotion-revoked-authority",predecessor_policy_id=predecessor.policy_id,predecessor_model_id=predecessor_model_id,created_at=CHALLENGER_CREATED,completed_at=CHALLENGER_COMPLETED,decided_at=CHALLENGER_DECIDED)
    import pytest
    with pytest.raises(ValueError,match="does not match frozen authority"):
        _run_nongoverned_factory_retest(ExperimentRunner(registry,store),predecessor_policy=predecessor,challenger_policy=challenger,update_evidence=update,spec=spec,evaluation_cases=cases,rule=rule)


def test_policy_retest_rejects_missing_post_reveal_materialization_receipt(tmp_path):
    registry, _, _, store, predecessor, predecessor_model_id, cases, rule = _foundation(tmp_path)
    state=registry._read(); state["records"]=[x for x in state["records"] if not (x["record_type"]=="CounterfactualSourceEvidence" and x["record_id"].endswith(":holdout-1"))]
    atomic_write_json(registry.path,state)
    challenger, update=_policy_successor(predecessor,observation_id="5"*64,outcome_id="6"*64,episode_id="7"*64,decided_at="2026-09-19T09:33:00Z",available_at="2026-09-19T09:34:00Z",reward_value="2")
    spec=_spec(experiment_id="experiment-missing-receipt",model_id="model-missing-receipt",evaluation_id="evaluation-missing-receipt",promotion_id="promotion-missing-receipt",predecessor_policy_id=predecessor.policy_id,predecessor_model_id=predecessor_model_id,created_at=CHALLENGER_CREATED,completed_at=CHALLENGER_COMPLETED,decided_at=CHALLENGER_DECIDED)
    import pytest
    with pytest.raises(ValueError,match="materialization receipt"):
        _run_nongoverned_factory_retest(ExperimentRunner(registry,store),predecessor_policy=predecessor,challenger_policy=challenger,update_evidence=update,spec=spec,evaluation_cases=cases,rule=rule)




def test_staged_factory_artifact_cannot_mint_materialization_receipt(tmp_path):
    real_store = FactoryArtifactStore(tmp_path / "real-artifacts")
    staged_store = _StagedFactoryArtifactStore(
        real_store,
        tmp_path / "staged-artifacts",
    )
    payload = {
        "schema_version": 1,
        "kind": "staged-only-evidence",
    }
    digest = staged_store.write(
        "counterfactual-source-evidence",
        "staged-only",
        payload,
    )

    import pytest

    with pytest.raises(ValueError, match="no durable materialization receipt"):
        staged_store.materialization_receipt(
            "counterfactual-source-evidence",
            "staged-only",
            expected_sha256=digest,
        )


def test_policy_retest_rejects_tampered_materialization_ledger(tmp_path):
    (
        registry,
        _,
        artifact_root,
        store,
        predecessor,
        predecessor_model_id,
        cases,
        rule,
    ) = _foundation(tmp_path)
    ledger = store._materialization_ledger_path()
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    payload["records"][0]["materialized_at"] = FROZEN_AT
    atomic_write_json(ledger, payload)
    challenger, update = _policy_successor(
        predecessor,
        observation_id="5" * 64,
        outcome_id="6" * 64,
        episode_id="7" * 64,
        decided_at="2026-09-19T09:33:00Z",
        available_at="2026-09-19T09:34:00Z",
        reward_value="2",
    )
    spec = _spec(
        experiment_id="experiment-tampered-materialization-ledger",
        model_id="model-tampered-materialization-ledger",
        evaluation_id="evaluation-tampered-materialization-ledger",
        promotion_id="promotion-tampered-materialization-ledger",
        predecessor_policy_id=predecessor.policy_id,
        predecessor_model_id=predecessor_model_id,
        created_at=CHALLENGER_CREATED,
        completed_at=CHALLENGER_COMPLETED,
        decided_at=CHALLENGER_DECIDED,
    )
    import pytest

    with pytest.raises(ValueError, match="materialization ledger record digest mismatch"):
        _run_nongoverned_factory_retest(
            ExperimentRunner(registry, store),
            predecessor_policy=predecessor,
            challenger_policy=challenger,
            update_evidence=update,
            spec=spec,
            points=(),
            evaluation_cases=cases,
            rule=rule,
        )
    assert registry.get("EvaluationBundle", spec.evaluation_bundle_id) is None
    assert registry.get("PromotionDecision", spec.promotion_decision_id) is None


def test_policy_retest_rejects_store_materialization_after_evaluation_completion(tmp_path):
    (
        registry,
        _,
        artifact_root,
        store,
        predecessor,
        predecessor_model_id,
        cases,
        rule,
    ) = _foundation(tmp_path)
    ledger = store._materialization_ledger_path()
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    for record in payload["records"]:
        if record["kind"] == "counterfactual-source-evidence" and record["identity"].endswith(":holdout-2"):
            record["materialized_at"] = "2026-09-19T10:30:00Z"
            break
    # Recompute the chain as a controlled fixture mutation so the validator
    # reaches the temporal rule rather than the ledger-integrity rule.
    previous = "0" * 64
    for record in payload["records"]:
        record["predecessor_record_sha256"] = previous
        record["record_sha256"] = store._materialization_digest(record)
        previous = record["record_sha256"]
    atomic_write_json(ledger, payload)
    challenger, update = _policy_successor(
        predecessor,
        observation_id="5" * 64,
        outcome_id="6" * 64,
        episode_id="7" * 64,
        decided_at="2026-09-19T09:33:00Z",
        available_at="2026-09-19T09:34:00Z",
        reward_value="2",
    )
    spec = _spec(
        experiment_id="experiment-late-materialization",
        model_id="model-late-materialization",
        evaluation_id="evaluation-late-materialization",
        promotion_id="promotion-late-materialization",
        predecessor_policy_id=predecessor.policy_id,
        predecessor_model_id=predecessor_model_id,
        created_at=CHALLENGER_CREATED,
        completed_at=CHALLENGER_COMPLETED,
        decided_at=CHALLENGER_DECIDED,
    )
    import pytest

    with pytest.raises(ValueError, match="materialized after evaluation completion"):
        _run_nongoverned_factory_retest(
            ExperimentRunner(registry, store),
            predecessor_policy=predecessor,
            challenger_policy=challenger,
            update_evidence=update,
            spec=spec,
            points=(),
            evaluation_cases=cases,
            rule=rule,
        )
    assert registry.get("EvaluationBundle", spec.evaluation_bundle_id) is None
    assert registry.get("PromotionDecision", spec.promotion_decision_id) is None

def test_second_policy_attempt_cannot_reuse_same_confirmation_holdout(tmp_path):
    (
        registry,
        _,
        _,
        store,
        predecessor,
        predecessor_model_id,
        cases,
        rule,
    ) = _foundation(tmp_path)
    challenger, update = _policy_successor(
        predecessor,
        observation_id="5" * 64,
        outcome_id="6" * 64,
        episode_id="7" * 64,
        decided_at="2026-09-19T09:33:00Z",
        available_at="2026-09-19T09:34:00Z",
        reward_value="2",
    )
    runner = ExperimentRunner(registry, store)
    first_spec = _spec(
        experiment_id="experiment-policy-v2",
        model_id="model-policy-v2",
        evaluation_id="evaluation-policy-v2",
        promotion_id="promotion-policy-v2",
        predecessor_policy_id=predecessor.policy_id,
        predecessor_model_id=predecessor_model_id,
        created_at=CHALLENGER_CREATED,
        completed_at=CHALLENGER_COMPLETED,
        decided_at=CHALLENGER_DECIDED,
    )
    first = _run_nongoverned_factory_retest(
        runner,
        predecessor_policy=predecessor,
        challenger_policy=challenger,
        update_evidence=update,
        spec=first_spec,
        evaluation_cases=cases,
        rule=rule,
    )
    assert first.verdict is PromotionVerdict.PROMOTE

    second, second_update = _policy_successor(
        challenger,
        observation_id="8" * 64,
        outcome_id="9" * 64,
        episode_id="a" * 64,
        decided_at="2026-09-19T10:05:00Z",
        available_at="2026-09-19T10:06:00Z",
        reward_value="2",
        action_type="HEDGE",
    )
    second_spec = _spec(
        experiment_id="experiment-policy-v3",
        model_id="model-policy-v3",
        evaluation_id="evaluation-policy-v3",
        promotion_id="promotion-policy-v3",
        predecessor_policy_id=challenger.policy_id,
        predecessor_model_id=first_spec.model_version_id,
        created_at=SECOND_CREATED,
        completed_at=SECOND_COMPLETED,
        decided_at=SECOND_DECIDED,
    )
    repeated = _run_nongoverned_factory_retest(
        runner,
        predecessor_policy=challenger,
        challenger_policy=second,
        update_evidence=second_update,
        spec=second_spec,
        evaluation_cases=cases,
        rule=rule,
    )

    assert repeated.verdict is PromotionVerdict.INCONCLUSIVE
    assert registry.champion_strategy(
        as_of=SECOND_DECIDED,
        canonical_strategy_id=CANONICAL_STRATEGY,
    ) == challenger.policy_id
    decision = registry.get("PromotionDecision", second_spec.promotion_decision_id)
    assert decision is not None
    evidence = registry.get("PromotionEvidence", decision.payload["promotion_evidence_id"])
    assert evidence is not None
    assert evidence.payload["validity"] == "ELIGIBLE"
    assert evidence.payload["practical_improvement"] == "1"
    assert evidence.payload["effect_interval_low"] == "1"
    assert evidence.payload["guardrails_passed"] is True
    assert evidence.payload["holdout_consumed"] is True


def _external_validity_protocol_for_policy_factory(
    *,
    candidate_id: str,
    candidate_artifact_sha256: str,
    dataset_manifest_sha256: str,
    cases: tuple[PolicyEvaluationCase, ...],
) -> FrozenBaselineProtocol:
    contract = canonical_evaluation_contract(
        EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE
    )
    scope = FrozenEvidenceScope(
        dataset_sha256=dataset_manifest_sha256,
        dataset_cutoff=CAUSAL_CUTOFF,
        cohort_keys=tuple(sorted(case.sample_id for case in cases)),
        market_evidence_sha256=_digest({"external-scope": "market-evidence"}),
        outcome_evidence_sha256=_digest({"external-scope": "outcome-evidence"}),
        cost_model_sha256=_digest({"external-scope": "cost-model"}),
        execution_model_sha256=_digest({"external-scope": "execution-model"}),
    )
    baselines = tuple(
        BaselineDefinition(
            kind=kind,
            baseline_id=f"fixture-baseline:{kind.value}",
            implementation_sha256=_digest(
                {"fixture-baseline": kind.value, "artifact": "implementation"}
            ),
            config_sha256=_digest(
                {"fixture-baseline": kind.value, "artifact": "config"}
            ),
            supported=False,
            unsupported_reason="candidate-only product-issuance acceptance fixture",
        )
        for kind in BaselineKind
    )
    return FrozenBaselineProtocol(
        protocol_id="external-validity-policy-e2e-v1",
        frozen_at=CHALLENGER_CREATED,
        evidence_scope=scope,
        candidate_id=candidate_id,
        candidate_artifact_sha256=candidate_artifact_sha256,
        strategy_class=StrategyClass.PREDICTIVE_EDGE,
        evaluation_contract_family=EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE,
        evaluation_semantics=str(contract["evaluation_semantics"]),
        evaluation_contract_sha256=str(contract["evaluation_contract_sha256"]),
        primary_metric=str(contract["primary_metric"]),
        uncertainty_method=str(contract["uncertainty_method"]),
        baselines=baselines,
    )


def test_product_policy_evaluation_issue_restart_idempotency_and_fresh_mint_rejection(
    tmp_path,
    monkeypatch,
):
    """Exercise the real factory -> issuer -> restart path and the decisive mint attack."""

    workspace = (tmp_path / "product-workspace").resolve()
    authority_root = (tmp_path / "machine-authority").resolve()
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(authority_root))
    binding = WorkspaceIdentityBinding.resolve(
        workspace=workspace,
        authority_root=authority_root,
        requested_workspace_instance_id=None,
    )
    binding.ensure_bound()

    (
        registry,
        registry_path,
        artifact_root,
        store,
        predecessor,
        predecessor_model_id,
        cases,
        rule,
    ) = _foundation(workspace, artifact_directory="factory-artifacts")
    assert registry_path == workspace / "scientific_registry.json"
    assert artifact_root == workspace / "factory-artifacts"

    challenger, update = _policy_successor(
        predecessor,
        observation_id="5" * 64,
        outcome_id="6" * 64,
        episode_id="7" * 64,
        decided_at="2026-09-19T09:33:00Z",
        available_at="2026-09-19T09:34:00Z",
        reward_value="2",
    )
    spec = _spec(
        experiment_id="experiment-policy-issued-v2",
        model_id="model-policy-issued-v2",
        evaluation_id="evaluation-policy-issued-v2",
        promotion_id="promotion-policy-issued-v2",
        predecessor_policy_id=predecessor.policy_id,
        predecessor_model_id=predecessor_model_id,
        created_at=CHALLENGER_CREATED,
        completed_at=CHALLENGER_COMPLETED,
        decided_at=CHALLENGER_DECIDED,
    )
    result = _run_nongoverned_factory_retest(
        ExperimentRunner(registry, store),
        predecessor_policy=predecessor,
        challenger_policy=challenger,
        update_evidence=update,
        spec=spec,
        evaluation_cases=cases,
        rule=rule,
    )
    assert result.strategy_version_id == challenger.policy_id

    model_payload = store.read("model", spec.model_version_id)
    candidate_artifact_sha256 = model_payload["policy_artifact_sha256"]
    dataset_manifest_sha256 = registry.get(
        "DatasetSnapshot", DATASET_ID
    ).payload["manifest_sha256"]
    protocol = _external_validity_protocol_for_policy_factory(
        candidate_id=challenger.policy_id,
        candidate_artifact_sha256=candidate_artifact_sha256,
        dataset_manifest_sha256=dataset_manifest_sha256,
        cases=cases,
    )
    authority = ProductPolicyEvaluationWorkspace.open(
        workspace,
        expected_workspace_instance_id=binding.workspace_instance_id,
    )

    reference = issue_product_policy_evaluation(
        authority,
        protocol,
        source_evaluation_bundle_id=spec.evaluation_bundle_id,
    )
    issued = resolve_product_policy_evaluation(authority, protocol, reference)
    assert issued.policy_id == challenger.policy_id
    assert issued.policy_artifact_sha256 == candidate_artifact_sha256
    assert issued.observed_count == len(cases)
    assert Decimal(issued.metric_value) == Decimal("1")
    assert canonical_product_policy_evaluation_bundle_sha256(issued) == (
        reference.evaluation_bundle_sha256
    )

    # Restart from durable workspace identity + serialized public reference only.
    reopened_authority = ProductPolicyEvaluationWorkspace.open(
        workspace,
        expected_workspace_instance_id=binding.workspace_instance_id,
    )
    restarted_reference = IssuedPolicyEvaluationRef.from_payload(
        reference.to_payload()
    )
    restarted = resolve_product_policy_evaluation(
        reopened_authority,
        protocol,
        restarted_reference,
    )
    assert restarted.to_payload() == issued.to_payload()

    # Exact retry must converge to the same immutable result and registry row.
    before_bundle = ScientificRegistry(registry_path).get(
        "EvaluationBundle", reference.evaluation_bundle_id
    )
    retried_reference = issue_product_policy_evaluation(
        reopened_authority,
        protocol,
        source_evaluation_bundle_id=spec.evaluation_bundle_id,
    )
    after_bundle = ScientificRegistry(registry_path).get(
        "EvaluationBundle", reference.evaluation_bundle_id
    )
    assert retried_reference == reference
    assert before_bundle is not None and after_bundle is not None
    assert after_bundle.record_sha256 == before_bundle.record_sha256

    # Decisive caller-mint attack: choose altered final values, recompute the public
    # commitment, and append a matching public EvaluationBundleRef.  Neither the
    # forged DTO nor the caller-created bundle can substitute for evaluator issuance.
    forged = replace(
        issued,
        metric_value="999",
        uncertainty_low="999",
        uncertainty_high="999",
        evaluation_bundle_sha256="0" * 64,
    )
    forged = replace(
        forged,
        evaluation_bundle_sha256=canonical_product_policy_evaluation_bundle_sha256(
            forged
        ),
    )
    attacker_bundle_id = "caller-minted-external-validity-bundle"
    ScientificRegistry(registry_path).append(
        EvaluationBundleRef(
            attacker_bundle_id,
            forged.evaluation_bundle_sha256,
            EVALUATOR_SOURCE,
            DATASET_ID,
            protocol.identity_sha256,
            (forged.evaluation_bundle_sha256,),
            CHALLENGER_COMPLETED,
            evaluated_strategy_version_id=challenger.policy_id,
            evaluated_model_version_id=spec.model_version_id,
        )
    )
    assert ScientificRegistry(registry_path).get(
        "EvaluationBundle", attacker_bundle_id
    ).payload["bundle_sha256"] == forged.evaluation_bundle_sha256

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="differs from product-issued result",
    ):
        verify_product_policy_evaluation(
            reopened_authority,
            protocol,
            restarted_reference,
            forged,
        )

    forged_reference = replace(
        restarted_reference,
        evaluation_bundle_id=attacker_bundle_id,
        evaluation_bundle_sha256=forged.evaluation_bundle_sha256,
    )
    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="bundle identity mismatch",
    ):
        resolve_product_policy_evaluation(
            reopened_authority,
            protocol,
            forged_reference,
        )

    rebound_protocol = replace(
        protocol,
        protocol_id="external-validity-policy-e2e-rebound",
    )
    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="does not match frozen protocol slot",
    ):
        resolve_product_policy_evaluation(
            reopened_authority,
            rebound_protocol,
            restarted_reference,
        )

    with pytest.raises(ProductPolicyEvaluationIssuanceError):
        issue_product_policy_evaluation(
            reopened_authority,
            protocol,
            source_evaluation_bundle_id="evaluation-policy-bootstrap",
        )
