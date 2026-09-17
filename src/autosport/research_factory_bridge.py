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


def stage_baseline_candidate(
    runner: _factory.ExperimentRunner,
    spec: _impl.FactoryCandidateSpec,
    points: Sequence[_impl.TrainingPoint],
    *,
    rule: _impl.PromotionRule,
    minimum_train_size: int | None = None,
) -> StagedFactoryEvaluation:
    """Publish canonical candidate/evaluation evidence without publishing a decision.

    The canonical implementation still performs the complete causal evaluation in an
    isolated staged workspace. Before publication this bridge removes only the
    candidate's staged PromotionDecision and negative-result Postmortem. Model,
    strategy, evaluation bundle, and Experiment remain canonical immutable evidence.
    Re-delivery before finalization is idempotent because the canonical factory
    re-validates those immutable identities against the staged copy.
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
            # FactoryRunResult exposes `registry_action`, which here is only the
            # frozen factory proposal. A negative candidate creates one staged
            # postmortem; a positive candidate creates none. Neither may be published
            # before robustness and forward paper/shadow complete.
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
    experiment = reopened.get("Experiment", spec.experiment_id)
    bundle = reopened.get("EvaluationBundle", spec.evaluation_bundle_id)
    if experiment is None or bundle is None:
        raise ResearchFactoryBridgeError(
            "staged candidate/evaluation evidence is missing after publication"
        )
    if reopened.get("PromotionDecision", spec.promotion_decision_id) is not None:
        raise ResearchFactoryBridgeError(
            "predecision stage unexpectedly published a PromotionDecision"
        )
    reproducibility = reopened.reproducibility_bundle(spec.experiment_id)
    return StagedFactoryEvaluation(
        experiment_id=spec.experiment_id,
        model_version_id=spec.model_version_id,
        strategy_version_id=spec.strategy_version_id,
        evaluation_bundle_id=spec.evaluation_bundle_id,
        promotion_decision_id=spec.promotion_decision_id,
        evaluation_bundle_sha256=bundle.payload["bundle_sha256"],
        reproducibility_bundle_sha256=reproducibility["bundle_sha256"],
        proposed_verdict=result.verdict,
        proposed_action=result.registry_action,
        candidate_metrics=tuple(sorted(result.candidate_metrics.items())),
    )


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
            if experiment_outcome is not ResearchOutcome.POSITIVE:
                postmortem_id = f"{spec.experiment_id}:postmortem"
                staged_registry.append(
                    Postmortem(
                        postmortem_id,
                        spec.experiment_id,
                        experiment_outcome,
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
