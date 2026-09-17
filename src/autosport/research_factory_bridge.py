"""Research-supervisor bridge to the canonical Strategy/Model Factory.

This module does not implement a second evaluator, model factory, artifact store, or
promotion authority. It reuses the canonical factory's existing isolated staging
transaction, but deliberately publishes the scientific candidate/evaluation before
publishing a PromotionDecision. The supervisor can therefore perform robustness
and forward paper/shadow phases before the canonical registry is asked to change
champion history.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import _strategy_model_factory_impl as _impl
from . import strategy_model_factory as _factory
from .integrity import atomic_write_json
from .scientific_registry import (
    Postmortem,
    PromotionAction,
    PromotionDecision,
    ResearchOutcome,
    ScientificRegistry,
)
from .workspace_lock import WorkspaceEconomicLock


class ResearchFactoryBridgeError(RuntimeError):
    """A staged research/factory boundary could not be proven safe."""


class ResearchFactoryAlreadyFinalized(ResearchFactoryBridgeError):
    """The candidate already has a durable canonical promotion decision."""


class BlindResearchRerunBlocked(ResearchFactoryBridgeError):
    """Known failed/null/harmful work must not be rediscovered blindly."""

    def __init__(self, prior_experiment_ids: tuple[str, ...]) -> None:
        self.prior_experiment_ids = prior_experiment_ids
        joined = ", ".join(prior_experiment_ids)
        super().__init__(
            "known non-positive research memory blocks blind rerun under the same "
            f"frozen protocol; reuse the conclusion or create a justified new protocol: {joined}"
        )


@dataclass(frozen=True, slots=True)
class StagedFactoryEvaluation:
    experiment_id: str
    model_version_id: str
    strategy_version_id: str
    evaluation_bundle_id: str
    promotion_decision_id: str
    evaluation_bundle_sha256: str
    reproducibility_bundle_sha256: str
    proposed_verdict: _impl.PromotionVerdict
    proposed_action: PromotionAction
    candidate_metrics: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class FinalizedFactoryDecision:
    promotion_decision_id: str
    action: PromotionAction
    record_sha256: str
    postmortem_id: str | None


def _canonical_reason(
    *,
    proposed_action: PromotionAction,
    final_action: PromotionAction,
    robustness_evidence_sha256: str,
    forward_evidence_sha256: str,
    reason: str,
) -> str:
    if type(reason) is not str or not reason or reason != reason.strip():
        raise ValueError("reason must be a non-empty canonical string")
    payload = {
        "kind": "autosport-research-supervisor-final-decision-v1",
        "factory_proposed_action": proposed_action.value,
        "final_action": final_action.value,
        "robustness_evidence_sha256": _impl._sha256(
            robustness_evidence_sha256, "robustness_evidence_sha256"
        ),
        "forward_evidence_sha256": _impl._sha256(
            forward_evidence_sha256, "forward_evidence_sha256"
        ),
        "reason": reason,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _publish_staged_state(
    *,
    real_registry: ScientificRegistry,
    real_store: _factory.FactoryArtifactStore,
    original_state: dict[str, object],
    final_state: dict[str, object],
    staged_store: _factory._StagedFactoryArtifactStore,
) -> None:
    """Publish one already-validated registry/artifact snapshot with crash recovery."""
    transaction_path = _factory._publish_transaction_path(real_registry)
    transaction = {
        "schema_version": 1,
        "phase": "prepared",
        "original_registry_sha256": _factory._registry_state_sha256(original_state),
        "final_registry_sha256": _factory._registry_state_sha256(final_state),
        "artifacts": list(staged_store.transaction_artifacts()),
    }
    atomic_write_json(transaction_path, transaction)
    try:
        created_artifacts = staged_store.publish()
    except Exception as publish_error:
        _factory._unlink_transaction_manifest(transaction_path, publish_error)
        raise
    try:
        atomic_write_json(real_registry.path, final_state)
        real_registry._read()
    except Exception as publish_error:
        try:
            atomic_write_json(real_registry.path, original_state)
            real_registry._read()
        except BaseException as restore_error:
            try:
                publish_error.add_note(
                    "research factory bridge registry rollback also failed: "
                    f"{type(restore_error).__name__}: {restore_error}"
                )
            except BaseException:
                pass
        staged_store._rollback(created_artifacts, publish_error)
        _factory._unlink_transaction_manifest(transaction_path, publish_error)
        raise
    _factory._unlink_transaction_manifest(transaction_path)


def _strip_predecision_records(
    final_state: dict[str, object],
    *,
    promotion_decision_id: str,
    postmortem_id: str,
) -> tuple[dict[str, object], int, int]:
    records = final_state.get("records")
    if type(records) is not list:
        raise ResearchFactoryBridgeError("staged scientific registry records are invalid")
    kept: list[object] = []
    removed_decisions = 0
    removed_postmortems = 0
    for raw in records:
        if type(raw) is not dict:
            raise ResearchFactoryBridgeError("staged scientific registry entry is invalid")
        record_type = raw.get("record_type")
        record_id = raw.get("record_id")
        if record_type == "PromotionDecision" and record_id == promotion_decision_id:
            removed_decisions += 1
            continue
        if record_type == "Postmortem" and record_id == postmortem_id:
            removed_postmortems += 1
            continue
        kept.append(raw)
    stripped = dict(final_state)
    stripped["records"] = kept
    return stripped, removed_decisions, removed_postmortems


def _existing_staged_evaluation(
    registry: ScientificRegistry,
    store: _factory.FactoryArtifactStore,
    spec: _impl.FactoryCandidateSpec,
) -> StagedFactoryEvaluation | None:
    """Reconstruct an already-published predecision stage after restart/redelivery."""
    experiment = registry.get("Experiment", spec.experiment_id)
    if experiment is None:
        return None
    if registry.get("PromotionDecision", spec.promotion_decision_id) is not None:
        raise ResearchFactoryAlreadyFinalized(
            f"candidate already finalized: {spec.promotion_decision_id}"
        )
    if registry.get("Postmortem", f"{spec.experiment_id}:postmortem") is not None:
        raise ResearchFactoryBridgeError(
            "candidate has a postmortem without its canonical final decision"
        )

    model = registry.get("ModelVersion", spec.model_version_id)
    strategy = registry.get("StrategyVersion", spec.strategy_version_id)
    bundle = registry.get("EvaluationBundle", spec.evaluation_bundle_id)
    if any(value is None for value in (model, strategy, bundle)):
        raise ResearchFactoryBridgeError(
            "existing staged experiment has incomplete factory lineage"
        )
    assert model is not None
    assert strategy is not None
    assert bundle is not None

    expected_experiment = {
        "research_protocol_id": spec.research_protocol_id,
        "dataset_snapshot_id": spec.dataset_snapshot_id,
        "feature_set_id": spec.feature_set_id,
        "model_version_id": spec.model_version_id,
        "strategy_version_id": spec.strategy_version_id,
        "evaluation_bundle_id": spec.evaluation_bundle_id,
        "seed": spec.seed,
    }
    for key, expected in expected_experiment.items():
        if experiment.payload.get(key) != expected:
            raise ResearchFactoryBridgeError(
                f"existing staged experiment identity mismatch: {key}"
            )
    if strategy.payload.get("canonical_strategy_id") != spec.canonical_strategy_id:
        raise ResearchFactoryBridgeError("existing staged canonical strategy mismatch")
    if strategy.payload.get("source_sha256") != spec.source_sha256.lower():
        raise ResearchFactoryBridgeError("existing staged strategy source mismatch")
    if strategy.payload.get("environment_sha256") != spec.environment_sha256.lower():
        raise ResearchFactoryBridgeError("existing staged strategy environment mismatch")
    if model.payload.get("research_protocol_id") != spec.research_protocol_id:
        raise ResearchFactoryBridgeError("existing staged model protocol mismatch")
    if model.payload.get("dataset_snapshot_id") != spec.dataset_snapshot_id:
        raise ResearchFactoryBridgeError("existing staged model dataset mismatch")
    if model.payload.get("feature_set_id") != spec.feature_set_id:
        raise ResearchFactoryBridgeError("existing staged model feature mismatch")

    evaluation = store.read(
        "evaluation",
        spec.evaluation_bundle_id,
        expected_sha256=bundle.payload.get("bundle_sha256"),
    )
    if evaluation.get("experiment_id") != spec.experiment_id:
        raise ResearchFactoryBridgeError("existing staged evaluation experiment mismatch")
    if evaluation.get("model_version_id") != spec.model_version_id:
        raise ResearchFactoryBridgeError("existing staged evaluation model mismatch")
    if evaluation.get("strategy_version_id") != spec.strategy_version_id:
        raise ResearchFactoryBridgeError("existing staged evaluation strategy mismatch")
    verdict_raw = evaluation.get("promotion_verdict")
    try:
        verdict = _impl.PromotionVerdict(verdict_raw)
    except (TypeError, ValueError) as exc:
        raise ResearchFactoryBridgeError(
            "existing staged evaluation lacks canonical promotion verdict"
        ) from exc
    candidate_metrics = _impl._metric_map(
        evaluation.get("candidate_metrics"),
        "existing staged candidate metrics",
    )
    reproducibility = registry.reproducibility_bundle(spec.experiment_id)
    return StagedFactoryEvaluation(
        experiment_id=spec.experiment_id,
        model_version_id=spec.model_version_id,
        strategy_version_id=spec.strategy_version_id,
        evaluation_bundle_id=spec.evaluation_bundle_id,
        promotion_decision_id=spec.promotion_decision_id,
        evaluation_bundle_sha256=bundle.payload["bundle_sha256"],
        reproducibility_bundle_sha256=reproducibility["bundle_sha256"],
        proposed_verdict=verdict,
        proposed_action=(
            PromotionAction.PROMOTE
            if verdict is _impl.PromotionVerdict.PROMOTE
            else PromotionAction.REJECT
        ),
        candidate_metrics=tuple(sorted(candidate_metrics.items())),
    )


def _known_nonpositive_equivalents(
    registry: ScientificRegistry,
    spec: _impl.FactoryCandidateSpec,
    *,
    config_sha256: str,
) -> tuple[str, ...]:
    """Find semantically equivalent negative memory even when version IDs differ."""
    experiments = registry.causal_records("Experiment", as_of=spec.created_at)
    postmortems = {
        entry.payload.get("experiment_id"): entry
        for entry in registry.causal_records("Postmortem", as_of=spec.created_at)
        if isinstance(entry.payload.get("experiment_id"), str)
    }
    decisions = registry.causal_records("PromotionDecision", as_of=spec.created_at)
    blocked: list[str] = []
    for experiment in experiments:
        payload = experiment.payload
        if payload.get("research_protocol_id") != spec.research_protocol_id:
            continue
        if payload.get("dataset_snapshot_id") != spec.dataset_snapshot_id:
            continue
        if payload.get("feature_set_id") != spec.feature_set_id:
            continue
        if payload.get("seed") != spec.seed:
            continue
        if payload.get("config_sha256") != config_sha256:
            continue
        strategy_id = payload.get("strategy_version_id")
        if type(strategy_id) is not str:
            continue
        strategy = registry.get("StrategyVersion", strategy_id)
        if strategy is None:
            raise ResearchFactoryBridgeError(
                f"historical experiment references missing StrategyVersion:{strategy_id}"
            )
        if strategy.payload.get("canonical_strategy_id") != spec.canonical_strategy_id:
            continue
        if strategy.payload.get("source_sha256") != spec.source_sha256.lower():
            continue
        if strategy.payload.get("environment_sha256") != spec.environment_sha256.lower():
            continue

        outcome = ResearchOutcome(payload["outcome"])
        has_postmortem = experiment.record_id in postmortems
        has_rejecting_decision = any(
            decision.payload.get("candidate_strategy_version_id") == strategy_id
            and decision.payload.get("action") != PromotionAction.PROMOTE.value
            for decision in decisions
        )
        if outcome is not ResearchOutcome.POSITIVE or has_postmortem or has_rejecting_decision:
            blocked.append(experiment.record_id)
    return tuple(sorted(set(blocked)))


def stage_baseline_candidate(
    runner: _factory.ExperimentRunner,
    spec: _impl.FactoryCandidateSpec,
    points: Sequence[_impl.TrainingPoint],
    *,
    rule: _impl.PromotionRule,
    minimum_train_size: int | None = None,
) -> StagedFactoryEvaluation:
    """Publish canonical candidate/evaluation evidence without publishing a decision.

    Exact redelivery of an already-published predecision stage is reconstructed
    idempotently. New semantically equivalent work is checked against causal durable
    negative/null/harmful memory before the canonical factory is allowed to evaluate
    anything. A justified retest therefore needs a new frozen protocol version.
    """
    if not isinstance(runner, _factory.ExperimentRunner):
        raise TypeError("runner must be the canonical strategy_model_factory.ExperimentRunner")
    if not isinstance(spec, _impl.FactoryCandidateSpec):
        raise TypeError("spec must be FactoryCandidateSpec")

    real_registry = runner.registry
    real_store = runner.artifact_store
    postmortem_id = f"{spec.experiment_id}:postmortem"

    with WorkspaceEconomicLock(real_registry.path.parent):
        _factory._recover_interrupted_factory_publish(real_registry, real_store)
        existing = _existing_staged_evaluation(real_registry, real_store, spec)
        if existing is not None:
            return existing
        if real_registry.get("PromotionDecision", spec.promotion_decision_id) is not None:
            raise ResearchFactoryAlreadyFinalized(
                f"candidate already finalized: {spec.promotion_decision_id}"
            )
        if real_registry.get("Postmortem", postmortem_id) is not None:
            raise ResearchFactoryBridgeError(
                "candidate has a postmortem without its canonical final decision"
            )

        original_state = real_registry._read()
        protocol = real_registry.get("ResearchProtocol", spec.research_protocol_id)
        if protocol is None:
            raise ResearchFactoryBridgeError("factory research protocol is missing")
        binding = protocol.payload.get("binding")
        if type(binding) is not dict:
            raise ResearchFactoryBridgeError(
                "factory research protocol lacks frozen binding"
            )
        config_sha256 = _impl._sha256(
            binding.get("code_config_sha256"), "code_config_sha256"
        )
        blocked = _known_nonpositive_equivalents(
            real_registry,
            spec,
            config_sha256=config_sha256,
        )
        if blocked:
            raise BlindResearchRerunBlocked(blocked)

        evaluation_config = _impl.WalkForwardEvaluationConfig.from_frozen_text(
            binding.get("evaluation_design")
        )
        causal_factory = _factory._CausalFinalFitFactory(
            runner.baseline_model_factory,
            final_model_id=spec.model_version_id,
            minimum_causal_train_size=evaluation_config.minimum_causal_train_size,
        )

        with tempfile.TemporaryDirectory(
            prefix="autosport-research-factory-stage-"
        ) as temporary_directory:
            temporary_root = Path(temporary_directory)
            staged_registry_path = temporary_root / "scientific_registry.json"
            atomic_write_json(staged_registry_path, original_state)
            staged_registry = ScientificRegistry(staged_registry_path)
            staged_store = _factory._StagedFactoryArtifactStore(
                real_store,
                temporary_root / "factory-artifacts",
            )
            staged_runner = _impl.ExperimentRunner(
                staged_registry,
                staged_store,
                baseline_model_factory=causal_factory,
            )
            result = staged_runner.run_baseline_candidate(
                spec,
                points,
                rule=rule,
                minimum_train_size=minimum_train_size,
            )
            full_final_state = staged_registry._read()
            final_state, removed_decisions, removed_postmortems = _strip_predecision_records(
                full_final_state,
                promotion_decision_id=spec.promotion_decision_id,
                postmortem_id=postmortem_id,
            )
            if removed_decisions != 1:
                raise ResearchFactoryBridgeError(
                    "canonical staged factory did not produce exactly one decision candidate"
                )
            if result.registry_action is PromotionAction.PROMOTE:
                if removed_postmortems != 0:
                    raise ResearchFactoryBridgeError(
                        "positive staged factory result unexpectedly created a postmortem"
                    )
            elif removed_postmortems != 1:
                raise ResearchFactoryBridgeError(
                    "negative staged factory result lacks exactly one staged postmortem"
                )

            _publish_staged_state(
                real_registry=real_registry,
                real_store=real_store,
                original_state=original_state,
                final_state=final_state,
                staged_store=staged_store,
            )

    reopened = ScientificRegistry(real_registry.path)
    existing = _existing_staged_evaluation(reopened, real_store, spec)
    if existing is None:
        raise ResearchFactoryBridgeError(
            "staged candidate/evaluation evidence is missing after publication"
        )
    return existing


def finalize_staged_candidate(
    runner: _factory.ExperimentRunner,
    spec: _impl.FactoryCandidateSpec,
    staged: StagedFactoryEvaluation,
    *,
    final_action: PromotionAction,
    decided_at: str,
    robustness_evidence_sha256: str,
    forward_evidence_sha256: str,
    reason: str,
    retest_conditions: tuple[str, ...] = (
        "new protocol version or explicitly authorized retest",
    ),
) -> FinalizedFactoryDecision:
    """Publish the canonical decision only after later supervisor evidence exists."""
    if not isinstance(runner, _factory.ExperimentRunner):
        raise TypeError("runner must be the canonical strategy_model_factory.ExperimentRunner")
    if not isinstance(spec, _impl.FactoryCandidateSpec):
        raise TypeError("spec must be FactoryCandidateSpec")
    if not isinstance(staged, StagedFactoryEvaluation):
        raise TypeError("staged must be StagedFactoryEvaluation")
    if final_action not in (PromotionAction.PROMOTE, PromotionAction.REJECT):
        raise ValueError("research final_action must be PROMOTE or REJECT")
    if staged.experiment_id != spec.experiment_id:
        raise ResearchFactoryBridgeError("staged experiment identity mismatch")
    if staged.strategy_version_id != spec.strategy_version_id:
        raise ResearchFactoryBridgeError("staged strategy identity mismatch")
    if staged.model_version_id != spec.model_version_id:
        raise ResearchFactoryBridgeError("staged model identity mismatch")
    if staged.evaluation_bundle_id != spec.evaluation_bundle_id:
        raise ResearchFactoryBridgeError("staged evaluation identity mismatch")
    if staged.promotion_decision_id != spec.promotion_decision_id:
        raise ResearchFactoryBridgeError("staged promotion identity mismatch")
    if (
        final_action is PromotionAction.PROMOTE
        and staged.proposed_action is not PromotionAction.PROMOTE
    ):
        raise ResearchFactoryBridgeError(
            "later evidence cannot upgrade a factory-rejected candidate to PROMOTE"
        )

    canonical_reason = _canonical_reason(
        proposed_action=staged.proposed_action,
        final_action=final_action,
        robustness_evidence_sha256=robustness_evidence_sha256,
        forward_evidence_sha256=forward_evidence_sha256,
        reason=reason,
    )
    real_registry = runner.registry
    real_store = runner.artifact_store

    with WorkspaceEconomicLock(real_registry.path.parent):
        _factory._recover_interrupted_factory_publish(real_registry, real_store)
        original_state = real_registry._read()
        with tempfile.TemporaryDirectory(
            prefix="autosport-research-factory-finalize-"
        ) as temporary_directory:
            temporary_root = Path(temporary_directory)
            staged_registry_path = temporary_root / "scientific_registry.json"
            atomic_write_json(staged_registry_path, original_state)
            staged_registry = ScientificRegistry(staged_registry_path)

            experiment = staged_registry.get("Experiment", spec.experiment_id)
            protocol = staged_registry.get("ResearchProtocol", spec.research_protocol_id)
            bundle = staged_registry.get("EvaluationBundle", spec.evaluation_bundle_id)
            strategy = staged_registry.get("StrategyVersion", spec.strategy_version_id)
            model = staged_registry.get("ModelVersion", spec.model_version_id)
            if any(
                value is None
                for value in (experiment, protocol, bundle, strategy, model)
            ):
                raise ResearchFactoryBridgeError(
                    "staged factory lineage is incomplete before final decision"
                )
            assert experiment is not None
            assert protocol is not None
            assert bundle is not None
            if bundle.payload.get("bundle_sha256") != staged.evaluation_bundle_sha256:
                raise ResearchFactoryBridgeError(
                    "staged evaluation bundle changed before final decision"
                )
            reproducibility = staged_registry.reproducibility_bundle(spec.experiment_id)
            if reproducibility["bundle_sha256"] != staged.reproducibility_bundle_sha256:
                raise ResearchFactoryBridgeError(
                    "staged reproducibility lineage changed before final decision"
                )
            protocol_sha256 = protocol.payload.get("protocol_sha256")
            if type(protocol_sha256) is not str:
                raise ResearchFactoryBridgeError("durable protocol hash is missing")

            decision = PromotionDecision(
                spec.promotion_decision_id,
                final_action,
                spec.strategy_version_id,
                spec.research_protocol_id,
                protocol_sha256,
                spec.evaluation_bundle_id,
                staged.evaluation_bundle_sha256,
                decided_at,
                predecessor_strategy_version_id=spec.predecessor_strategy_version_id,
                candidate_model_version_id=spec.model_version_id,
                reason=canonical_reason,
            )
            decision_sha256 = staged_registry.record_promotion(decision)

            postmortem_id: str | None = None
            experiment_outcome = ResearchOutcome(experiment.payload["outcome"])
            if final_action is PromotionAction.REJECT:
                postmortem_id = f"{spec.experiment_id}:postmortem"
                memory_classification = (
                    experiment_outcome
                    if experiment_outcome is not ResearchOutcome.POSITIVE
                    else ResearchOutcome.NEGATIVE
                )
                staged_registry.append(
                    Postmortem(
                        postmortem_id,
                        spec.experiment_id,
                        memory_classification,
                        canonical_reason,
                        retest_conditions,
                        decided_at,
                    )
                )

            final_state = staged_registry._read()
            empty_staged_store = _factory._StagedFactoryArtifactStore(
                real_store,
                temporary_root / "no-new-artifacts",
            )
            _publish_staged_state(
                real_registry=real_registry,
                real_store=real_store,
                original_state=original_state,
                final_state=final_state,
                staged_store=empty_staged_store,
            )

    reopened = ScientificRegistry(real_registry.path)
    durable = reopened.get("PromotionDecision", spec.promotion_decision_id)
    if durable is None or durable.record_sha256 != decision_sha256:
        raise ResearchFactoryBridgeError(
            "final PromotionDecision is missing after canonical publication"
        )
    return FinalizedFactoryDecision(
        promotion_decision_id=spec.promotion_decision_id,
        action=final_action,
        record_sha256=decision_sha256,
        postmortem_id=postmortem_id,
    )
