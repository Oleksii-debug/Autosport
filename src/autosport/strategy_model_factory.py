from __future__ import annotations

import hashlib
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from . import _strategy_model_factory_impl as _impl
from ._strategy_model_factory_impl import *  # noqa: F401,F403
from .integrity import atomic_write_json
from .policy_evaluation import PolicyEvaluationConfig, PolicyPairEvaluation
from .run_transaction import RunTransaction, RunTransactionError
from .scientific_registry import ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock


_FACTORY_PUBLISH_TRANSACTION_FILENAME = ".factory-publish-transaction-v1.json"


class FactoryArtifactStore(_impl.FactoryArtifactStore):
    """Immutable factory evidence read from one stable regular filesystem object."""

    def _stable_snapshot(self, kind: str, identity: str):
        path = self._path(kind, identity)
        try:
            return RunTransaction._read_canonical_file_snapshot(
                path,
                f"factory artifact {kind}:{identity}",
            )
        except RunTransactionError as exc:
            raise ValueError(
                f"factory artifact path is not a stable regular object: {kind}:{identity}"
            ) from exc

    @staticmethod
    def _decode_snapshot(snapshot, kind: str, identity: str) -> dict[str, object]:
        try:
            payload = json.loads(snapshot.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("factory artifact is invalid JSON") from exc
        if type(payload) is not dict:
            raise ValueError("factory artifact root must be an object")
        return payload

    def write(self, kind: str, identity: str, payload: dict[str, object]) -> str:
        path = self._path(kind, identity)
        if path.exists():
            snapshot = self._stable_snapshot(kind, identity)
            existing = self._decode_snapshot(snapshot, kind, identity)
            if existing != payload:
                raise ValueError(f"conflicting immutable factory artifact: {kind}:{identity}")
            return snapshot.sha256
        return super().write(kind, identity, payload)

    def read(
        self,
        kind: str,
        identity: str,
        *,
        expected_sha256: str | None = None,
    ) -> dict[str, object]:
        snapshot = self._stable_snapshot(kind, identity)
        if expected_sha256 is not None and snapshot.sha256 != _impl._sha256(
            expected_sha256,
            "expected_sha256",
        ):
            raise ValueError(f"factory artifact hash mismatch: {kind}:{identity}")
        return self._decode_snapshot(snapshot, kind, identity)

    def sha256(self, kind: str, identity: str) -> str:
        return self._stable_snapshot(kind, identity).sha256


class _StagedFactoryArtifactStore:
    """Stage factory evidence until one registry transaction is proven valid."""

    def __init__(self, real_store: _impl.FactoryArtifactStore, root: Path) -> None:
        self._real = real_store
        self._staged = _impl.FactoryArtifactStore(root)
        self._writes: dict[tuple[str, str], dict[str, object]] = {}

    def exists(self, kind: str, identity: str) -> bool:
        return self._staged.exists(kind, identity) or self._real.exists(kind, identity)

    def write(self, kind: str, identity: str, payload: dict[str, object]) -> str:
        if self._real.exists(kind, identity):
            existing = self._real.read(kind, identity)
            if existing != payload:
                raise ValueError(
                    f"conflicting immutable factory artifact: {kind}:{identity}"
                )
            return self._real.sha256(kind, identity)
        digest = self._staged.write(kind, identity, payload)
        self._writes[(kind, identity)] = payload
        return digest

    def read(
        self,
        kind: str,
        identity: str,
        *,
        expected_sha256: str | None = None,
    ) -> dict[str, object]:
        if self._staged.exists(kind, identity):
            return self._staged.read(
                kind,
                identity,
                expected_sha256=expected_sha256,
            )
        return self._real.read(
            kind,
            identity,
            expected_sha256=expected_sha256,
        )

    def sha256(self, kind: str, identity: str) -> str:
        if self._staged.exists(kind, identity):
            return self._staged.sha256(kind, identity)
        return self._real.sha256(kind, identity)

    def path_for_testing(self, kind: str, identity: str) -> Path:
        if self._staged.exists(kind, identity):
            return self._staged.path_for_testing(kind, identity)
        return self._real.path_for_testing(kind, identity)

    def transaction_artifacts(self) -> tuple[dict[str, str], ...]:
        """Hash-bind exactly the new immutable objects this transaction may publish."""
        return tuple(
            {
                "kind": kind,
                "identity": identity,
                "sha256": self._staged.sha256(kind, identity),
            }
            for kind, identity in sorted(self._writes)
        )

    def _rollback(self, created: Sequence[tuple[str, str]], primary: BaseException) -> None:
        for kind, identity in reversed(tuple(created)):
            try:
                self._real.path_for_testing(kind, identity).unlink()
            except FileNotFoundError:
                continue
            except BaseException as cleanup_error:
                try:
                    primary.add_note(
                        "factory transaction artifact rollback also failed for "
                        f"{kind}:{identity}: {type(cleanup_error).__name__}: {cleanup_error}"
                    )
                except BaseException:
                    pass

    def publish(self) -> tuple[tuple[str, str], ...]:
        created: list[tuple[str, str]] = []
        try:
            for (kind, identity), payload in self._writes.items():
                if self._real.exists(kind, identity):
                    existing = self._real.read(kind, identity)
                    if existing != payload:
                        raise ValueError(
                            f"conflicting immutable factory artifact: {kind}:{identity}"
                        )
                    continue
                try:
                    self._real.write(kind, identity, payload)
                except BaseException:
                    path = self._real.path_for_testing(kind, identity)
                    if path.exists():
                        try:
                            path.unlink()
                        except BaseException:
                            pass
                    raise
                created.append((kind, identity))
        except BaseException as publish_error:
            self._rollback(created, publish_error)
            raise
        return tuple(created)


class _CausalFinalFitFactory:
    """Fail-close the final model-adapter boundary to the frozen causal population."""

    def __init__(
        self,
        delegate: _impl.BaselineModelFactory,
        *,
        final_model_id: str,
        minimum_causal_train_size: int,
    ) -> None:
        self._delegate = delegate
        self._final_model_id = final_model_id
        self._minimum_causal_train_size = minimum_causal_train_size
        self.model_family = delegate.model_family

    def fit(
        self,
        model_id: str,
        points: Sequence[_impl.TrainingPoint],
        *,
        training_cutoff: str,
    ) -> _impl.BaselineModel:
        if model_id != self._final_model_id:
            return self._delegate.fit(
                model_id,
                points,
                training_cutoff=training_cutoff,
            )
        cutoff = _impl._instant(training_cutoff, "training_cutoff")
        causal_points = tuple(
            point
            for point in _impl._ordered_training_points(points)
            if _impl._instant(point.observed_at, "observed_at") <= cutoff
            and _impl._instant(point.target_reveal_at, "target_available_at") <= cutoff
        )
        if len(causal_points) < self._minimum_causal_train_size:
            raise ValueError(
                "final model lacks the frozen minimum causally revealed training population"
            )
        return self._delegate.fit(
            model_id,
            causal_points,
            training_cutoff=training_cutoff,
        )


def _publish_transaction_path(registry: ScientificRegistry) -> Path:
    return registry.path.parent / _FACTORY_PUBLISH_TRANSACTION_FILENAME


def _registry_state_sha256(state: dict[str, object]) -> str:
    return _impl._canonical_digest(state)


def _unlink_transaction_manifest(path: Path, primary: BaseException | None = None) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except BaseException as cleanup_error:
        if primary is None:
            raise
        try:
            primary.add_note(
                "factory publish transaction manifest cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        except BaseException:
            pass


def _read_publish_transaction(path: Path) -> dict[str, object]:
    try:
        snapshot = RunTransaction._read_canonical_file_snapshot(
            path,
            "factory publish transaction",
        )
    except RunTransactionError as exc:
        raise ValueError("factory publish transaction is not a stable regular object") from exc
    try:
        payload = json.loads(snapshot.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("factory publish transaction is invalid JSON") from exc
    if type(payload) is not dict or payload.get("schema_version") != 1:
        raise ValueError("factory publish transaction schema is invalid")
    if payload.get("phase") != "prepared":
        raise ValueError("factory publish transaction phase is invalid")
    _impl._sha256(payload.get("original_registry_sha256"), "original_registry_sha256")
    _impl._sha256(payload.get("final_registry_sha256"), "final_registry_sha256")
    artifacts = payload.get("artifacts")
    if type(artifacts) is not list:
        raise ValueError("factory publish transaction artifacts are invalid")
    seen: set[tuple[str, str]] = set()
    for artifact in artifacts:
        if type(artifact) is not dict or set(artifact) != {"kind", "identity", "sha256"}:
            raise ValueError("factory publish transaction artifact entry is invalid")
        kind = _impl._text(artifact.get("kind"), "artifact kind")
        identity = _impl._text(artifact.get("identity"), "artifact identity")
        _impl._sha256(artifact.get("sha256"), "artifact sha256")
        key = (kind, identity)
        if key in seen:
            raise ValueError("factory publish transaction artifact identities must be unique")
        seen.add(key)
    return payload


def _recover_interrupted_factory_publish(
    registry: ScientificRegistry,
    store: FactoryArtifactStore,
) -> None:
    """Resolve the only two safe post-crash states, otherwise fail closed."""
    path = _publish_transaction_path(registry)
    if not path.exists():
        return
    transaction = _read_publish_transaction(path)
    current_sha256 = _registry_state_sha256(registry._read())
    original_sha256 = transaction["original_registry_sha256"]
    final_sha256 = transaction["final_registry_sha256"]
    artifacts = transaction["artifacts"]

    if current_sha256 == original_sha256:
        for artifact in artifacts:
            kind = artifact["kind"]
            identity = artifact["identity"]
            expected_sha256 = artifact["sha256"]
            artifact_path = store.path_for_testing(kind, identity)
            if not artifact_path.exists():
                continue
            if store.sha256(kind, identity) != expected_sha256:
                raise ValueError(
                    "interrupted factory publish artifact changed before recovery: "
                    f"{kind}:{identity}"
                )
            artifact_path.unlink()
        _unlink_transaction_manifest(path)
        return

    if current_sha256 == final_sha256:
        for artifact in artifacts:
            kind = artifact["kind"]
            identity = artifact["identity"]
            expected_sha256 = artifact["sha256"]
            if not store.path_for_testing(kind, identity).exists():
                raise ValueError(
                    "committed factory registry is missing a transaction artifact: "
                    f"{kind}:{identity}"
                )
            if store.sha256(kind, identity) != expected_sha256:
                raise ValueError(
                    "committed factory transaction artifact hash mismatch: "
                    f"{kind}:{identity}"
                )
        _unlink_transaction_manifest(path)
        return

    raise ValueError(
        "factory publish transaction registry state is neither precommit nor committed"
    )


def _canonical_decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("policy promotion decimal must be finite Decimal")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text



def _validate_counterfactual_artifacts(
    registry,
    artifact_store,
    authority,
    samples: Sequence[dict[str, object]],
    *,
    protocol_id: str,
    protocol_frozen,
    dataset_snapshot_id: str,
) -> None:
    """Validate durable causal ordering for qualification and post-reveal evidence."""
    qualification_identity=f"{authority.authority_id}@{authority.authority_version}"
    qualification=artifact_store.read(
        "counterfactual-qualification", qualification_identity,
        expected_sha256=authority.qualification_evidence_sha256,
    )
    if qualification.get("authority_id")!=authority.authority_id or qualification.get("authority_version")!=authority.authority_version:
        raise ValueError("counterfactual qualification artifact identity mismatch")
    if qualification.get("evaluator_source_sha256")!=authority.evaluator_source_sha256 or qualification.get("reward_definition_sha256")!=authority.reward_definition_sha256:
        raise ValueError("counterfactual qualification artifact semantics mismatch")
    if qualification.get("reward_mode")!=authority.reward_mode.value or qualification.get("scope")!=authority.scope or qualification.get("qualification_status")!=authority.qualification_status:
        raise ValueError("counterfactual qualification artifact does not match frozen authority")
    qualified_at=_impl._instant(qualification.get("qualified_at"),"counterfactual qualification qualified_at")
    if qualified_at>protocol_frozen:
        raise ValueError("counterfactual qualification was not available by protocol freeze")

    receipt=registry.get("CounterfactualQualification",qualification_identity)
    if receipt is None:
        raise ValueError("counterfactual qualification has no durable registry receipt")
    if receipt.payload.get("qualification_artifact_sha256")!=authority.qualification_evidence_sha256:
        raise ValueError("counterfactual qualification registry receipt hash mismatch")
    for key,expected in (
        ("authority_id",authority.authority_id),("authority_version",authority.authority_version),
        ("evaluator_source_sha256",authority.evaluator_source_sha256),("reward_definition_sha256",authority.reward_definition_sha256),
        ("reward_mode",authority.reward_mode.value),("scope",authority.scope),("qualification_status",authority.qualification_status),
    ):
        if receipt.payload.get(key)!=expected:
            raise ValueError("counterfactual qualification registry receipt does not match frozen authority")
    if not registry.causal_precedes("CounterfactualQualification",qualification_identity,"ResearchProtocol",protocol_id):
        raise ValueError("counterfactual qualification was registered after protocol freeze")

    for sample in samples:
        if type(sample) is not dict:
            raise ValueError("policy evaluation sample payload is invalid")
        case_payload=sample.get("case_payload")
        if type(case_payload) is not dict:
            raise ValueError("counterfactual sample lacks immutable source-evidence payload")
        sample_id=_impl._text(case_payload.get("sample_id"),"policy sample_id")
        case_source_sha256=_impl._sha256(case_payload.get("source_evidence_sha256"),"counterfactual case source_evidence_sha256")
        source_identity=f"{authority.authority_id}@{authority.authority_version}:{sample_id}"
        source_receipt=registry.get("CounterfactualSourceEvidence",source_identity)
        if source_receipt is None:
            raise ValueError("counterfactual source evidence lacks durable causal materialization receipt")
        for key,expected in (
            ("authority_id",authority.authority_id),("authority_version",authority.authority_version),("sample_id",sample_id),
            ("source_evidence_sha256",case_source_sha256),("dataset_snapshot_id",dataset_snapshot_id),
            ("observed_at",case_payload.get("observed_at")),("reward_available_at",case_payload.get("reward_available_at")),
        ):
            if source_receipt.payload.get(key)!=expected:
                raise ValueError("counterfactual source evidence receipt does not match evaluated case")
        if not registry.causal_precedes("ResearchProtocol",protocol_id,"CounterfactualSourceEvidence",source_identity):
            raise ValueError("counterfactual source evidence was materialized before protocol freeze")
        if _impl._instant(source_receipt.available_at,"source evidence materialized_at") < _impl._instant(case_payload.get("reward_available_at"),"reward_available_at"):
            raise ValueError("counterfactual source evidence materialized before reward availability")
        source_evidence=artifact_store.read("counterfactual-source-evidence",source_identity,expected_sha256=case_source_sha256)
        bound_case=dict(case_payload); bound_case.pop("source_evidence_sha256",None)
        expected_source_evidence={"schema_version":1,"kind":"autosport-counterfactual-source-evidence-v1","authority_id":authority.authority_id,"authority_version":authority.authority_version,"case":bound_case}
        if source_evidence!=expected_source_evidence:
            raise ValueError("counterfactual source evidence artifact does not match evaluated case")


def _run_policy_candidate_unstaged(
    registry: ScientificRegistry,
    artifact_store,
    spec: _impl.FactoryCandidateSpec,
    evaluation: PolicyPairEvaluation,
    *,
    rule: _impl.PromotionRule,
    policy_artifact_sha256: str,
) -> _impl.FactoryRunResult:
    """Publish one policy-specific causal evaluation into canonical scientific memory."""

    if not isinstance(spec, _impl.FactoryCandidateSpec):
        raise TypeError("spec must be FactoryCandidateSpec")
    if not isinstance(evaluation, PolicyPairEvaluation):
        raise TypeError("evaluation must be PolicyPairEvaluation")
    if not isinstance(rule, _impl.PromotionRule):
        raise TypeError("rule must be PromotionRule")
    policy_artifact_sha256 = _impl._sha256(
        policy_artifact_sha256, "policy_artifact_sha256"
    )
    if rule.primary_metric != "policy_loss":
        raise ValueError("policy-specific promotion primary metric must be policy_loss")

    protocol = registry.get("ResearchProtocol", spec.research_protocol_id)
    dataset = registry.get("DatasetSnapshot", spec.dataset_snapshot_id)
    feature = registry.get("FeatureSet", spec.feature_set_id)
    if protocol is None or dataset is None or feature is None:
        raise ValueError("policy factory foundation is incomplete in ScientificRegistry")
    binding = protocol.payload.get("binding")
    if type(binding) is not dict:
        raise ValueError("policy research protocol lacks frozen binding")
    if binding.get("promotion_rule") != rule.frozen_text:
        raise ValueError("policy promotion rule does not match frozen protocol")
    question_id = binding.get("research_question_id")
    hypothesis_id = binding.get("hypothesis_id")
    if type(question_id) is not str or type(hypothesis_id) is not str:
        raise ValueError("policy protocol lacks frozen question/hypothesis")
    question = registry.get("ResearchQuestion", question_id)
    hypothesis = registry.get("Hypothesis", hypothesis_id)
    if question is None or hypothesis is None:
        raise ValueError("policy frozen question/hypothesis is missing")
    if hypothesis.payload.get("research_question_id") != question_id:
        raise ValueError("policy hypothesis/question lineage mismatch")
    if _impl._canonical_digest(question.payload) != _impl._sha256(
        binding.get("research_question_sha256"), "research_question_sha256"
    ):
        raise ValueError("policy research question does not match frozen protocol")
    if _impl._canonical_digest(hypothesis.payload) != _impl._sha256(
        binding.get("hypothesis_sha256"), "hypothesis_sha256"
    ):
        raise ValueError("policy hypothesis does not match frozen protocol")

    question_available = _impl._instant(
        question.available_at, "research question available_at"
    )
    hypothesis_available = _impl._instant(
        hypothesis.available_at, "hypothesis available_at"
    )
    feature_available = _impl._instant(feature.available_at, "feature set available_at")
    protocol_frozen = _impl._instant(binding.get("frozen_at_utc"), "protocol frozen_at_utc")
    protocol_available = _impl._instant(
        protocol.available_at, "research protocol available_at"
    )
    dataset_available = _impl._instant(
        dataset.available_at, "dataset snapshot available_at"
    )
    experiment_start = _impl._instant(spec.created_at, "experiment created_at")
    if question_available > hypothesis_available:
        raise ValueError("research question must precede policy hypothesis")
    if hypothesis_available > protocol_frozen:
        raise ValueError("policy hypothesis must precede protocol freeze")
    if feature_available > protocol_frozen:
        raise ValueError("policy feature set must be available by protocol freeze")
    if protocol_frozen > protocol_available:
        raise ValueError("policy protocol cannot be persisted before freeze")
    if protocol_available > dataset_available:
        raise ValueError("policy dataset snapshot must not precede protocol")
    if dataset_available > experiment_start:
        raise ValueError("policy scientific foundation was not available at experiment start")

    if hypothesis.payload.get("primary_metric") != rule.primary_metric:
        raise ValueError("policy primary metric does not match frozen hypothesis")
    protective = hypothesis.payload.get("protective_metrics")
    if type(protective) is not list:
        raise ValueError("policy frozen protective metrics are invalid")
    required_metrics = {rule.primary_metric} | {
        name for name, _ in rule.protective_metric_maxima
    }
    if not {name for name, _ in rule.protective_metric_maxima}.issubset(
        set(protective)
    ):
        raise ValueError("policy protective metrics are not frozen in hypothesis")

    dataset_manifest_sha256 = _impl._sha256(
        dataset.payload.get("manifest_sha256"), "dataset manifest_sha256"
    )
    if dataset_manifest_sha256 != _impl._sha256(
        protocol.payload.get("dataset_manifest_sha256"),
        "protocol dataset_manifest_sha256",
    ):
        raise ValueError("policy dataset manifest does not match frozen protocol")
    if dataset_manifest_sha256 != evaluation.dataset_manifest_sha256:
        raise ValueError("policy evaluation cases do not match frozen DatasetSnapshot manifest")
    causal_cutoff = binding.get("causal_cutoff")
    if type(causal_cutoff) is not str:
        raise ValueError("policy protocol lacks causal cutoff")
    causal_cutoff_at = _impl._instant(causal_cutoff, "protocol causal_cutoff")
    if _impl._instant(dataset.payload.get("causal_cutoff"), "dataset causal_cutoff") != causal_cutoff_at:
        raise ValueError("policy dataset causal cutoff does not match protocol")
    for sample in evaluation.samples:
        if type(sample) is not dict:
            raise ValueError("policy evaluation sample payload is invalid")
        if _impl._instant(
            sample.get("observed_at"), "policy sample observed_at"
        ) > causal_cutoff_at:
            raise ValueError("policy evaluation observation exceeds frozen causal cutoff")
        if _impl._instant(
            sample.get("reward_available_at"), "policy sample reward_available_at"
        ) <= protocol_frozen:
            raise ValueError(
                "policy promotion holdout reward was already revealed by protocol freeze"
            )
    dataset_reveal = dataset.payload.get("outcome_reveal_after")
    if dataset_reveal is not None:
        reveal_at = _impl._instant(dataset_reveal, "dataset outcome_reveal_after")
        if reveal_at <= protocol_frozen:
            raise ValueError(
                "policy promotion dataset outcome was already revealed by protocol freeze"
            )
        if reveal_at > _impl._instant(spec.completed_at, "experiment completed_at"):
            raise ValueError(
                "policy promotion dataset outcome was not revealed by experiment completion"
            )

    evaluation_config = PolicyEvaluationConfig.from_frozen_text(
        binding.get("evaluation_design")
    )
    for sample in evaluation.samples:
        if type(sample) is not dict:
            raise ValueError("policy evaluation sample payload is invalid")
    counterfactual_samples = tuple(
        sample
        for sample in evaluation.samples
        if sample.get("reward_mode") != "OBSERVED_ACTION"
    )
    authority = evaluation_config.counterfactual_authority
    if counterfactual_samples:
        if authority is None:
            raise ValueError(
                "promotion-eligible counterfactual rewards lack frozen authority"
            )
        if authority.qualification_status != "QUALIFIED":
            raise ValueError(
                "promotion-eligible counterfactual authority is not qualified"
            )
        if (
            authority.evaluator_source_sha256
            != spec.evaluator_source_sha256.lower()
        ):
            raise ValueError(
                "counterfactual authority evaluator identity does not match factory spec"
            )
        if (
            evaluation.counterfactual_authority_sha256
            != authority.authority_sha256
        ):
            raise ValueError(
                "policy evaluation counterfactual authority hash mismatch"
            )
        _validate_counterfactual_artifacts(
            registry,
            artifact_store,
            authority,
            counterfactual_samples,
            protocol_id=spec.research_protocol_id,
            protocol_frozen=protocol_frozen,
            dataset_snapshot_id=spec.dataset_snapshot_id,
        )
        for sample in counterfactual_samples:
            authority.validate_reference(
                counterfactual_source_id=sample.get("counterfactual_source_id"),
                source_evidence_sha256=sample.get("source_evidence_sha256"),
                reward_mode=sample.get("reward_mode"),
                scope=sample.get("regime_id"),
            )
    elif evaluation.counterfactual_authority_sha256 is not None:
        raise ValueError(
            "observed-only policy evaluation cannot claim counterfactual authority"
        )
    if feature.payload.get("version") != binding.get("feature_set_version"):
        raise ValueError("policy feature version does not match frozen protocol")
    if feature.payload.get("feature_set_id") != evaluation_config.feature_set_id:
        raise ValueError("policy feature identity does not match evaluator config")
    if _impl._sha256(
        feature.payload.get("definition_sha256"), "feature definition_sha256"
    ) != evaluation_config.feature_definition_sha256:
        raise ValueError("policy feature definition does not match evaluator config")
    if _impl._sha256(
        feature.payload.get("source_sha256"), "feature source_sha256"
    ) != evaluation_config.feature_source_sha256:
        raise ValueError("policy feature source does not match evaluator config")

    config_sha256 = _impl._sha256(
        binding.get("code_config_sha256"), "code_config_sha256"
    )
    protocol_sha256 = _impl._sha256(
        protocol.payload.get("protocol_sha256"), "protocol_sha256"
    )
    if evaluation.protocol_id != spec.research_protocol_id:
        raise ValueError("policy evaluation protocol identity mismatch")
    if evaluation.environment_id != spec.environment_sha256.lower():
        raise ValueError("policy evaluation environment identity mismatch")
    if evaluation.challenger_policy_id != spec.strategy_version_id:
        raise ValueError("evaluated challenger policy does not match candidate strategy")
    if evaluation.predecessor_policy_id != spec.predecessor_strategy_version_id:
        raise ValueError("evaluated predecessor policy does not match rollback strategy")

    current_champion = registry.champion_strategy(
        as_of=spec.decided_at,
        canonical_strategy_id=spec.canonical_strategy_id,
    )
    if current_champion != evaluation.predecessor_policy_id:
        raise ValueError("policy evaluator predecessor is not the durable context champion")
    if current_champion is None:
        raise ValueError("policy promotion requires a durable rollback champion")
    champion_strategy = registry.get("StrategyVersion", current_champion)
    if champion_strategy is None:
        raise ValueError("policy durable champion StrategyVersion is missing")
    champion_model_version_id = champion_strategy.payload.get("model_version_id")
    if type(champion_model_version_id) is not str or not champion_model_version_id:
        raise ValueError("policy durable champion model identity is missing")
    if spec.predecessor_model_version_id != champion_model_version_id:
        raise ValueError("policy predecessor model identity does not match durable champion")

    internal_runner = _impl.ExperimentRunner(registry, artifact_store)
    internal_runner._preflight_promotion_history_order(spec)

    predecessor_metrics_all = evaluation.metrics_as_float(challenger=False)
    challenger_metrics_all = evaluation.metrics_as_float(challenger=True)
    missing = sorted(required_metrics - set(challenger_metrics_all))
    if missing:
        raise ValueError(
            "policy evaluator lacks frozen challenger metrics: " + ", ".join(missing)
        )
    predecessor_missing = sorted(required_metrics - set(predecessor_metrics_all))
    if predecessor_missing:
        raise ValueError(
            "policy evaluator lacks frozen predecessor metrics: "
            + ", ".join(predecessor_missing)
        )
    predecessor_metrics = {
        name: predecessor_metrics_all[name] for name in sorted(required_metrics)
    }
    challenger_metrics = {
        name: challenger_metrics_all[name] for name in sorted(required_metrics)
    }

    provisional = _impl.PromotionController.evaluate(
        rule,
        champion_metrics=predecessor_metrics,
        challenger_metrics=challenger_metrics,
        provenance_complete=True,
        rollback_target=current_champion,
    )
    placeholder = _impl.ExperimentRecord(
        spec.experiment_id,
        spec.research_protocol_id,
        spec.dataset_snapshot_id,
        spec.feature_set_id,
        spec.strategy_version_id,
        spec.evaluation_bundle_id,
        spec.seed,
        config_sha256,
        _impl.ResearchOutcome.INCONCLUSIVE,
        spec.created_at,
        model_version_id=spec.model_version_id,
        completed_at=spec.completed_at,
        notes="; ".join(provisional.reasons),
    )
    existing_experiment = registry.get("Experiment", spec.experiment_id)
    if existing_experiment is None:
        if registry.find_experiment_fingerprint(placeholder.fingerprint):
            raise _impl.DuplicateExperimentFingerprintError(
                "experiment fingerprint already has durable history; inspect negative/null results before repeating"
            )
        identities = [
            ("ModelVersion", spec.model_version_id),
            ("StrategyVersion", spec.strategy_version_id),
            ("EvaluationBundle", spec.evaluation_bundle_id),
            ("PromotionDecision", spec.promotion_decision_id),
            ("Postmortem", f"{spec.experiment_id}:postmortem"),
        ]
        for record_type, record_id in identities:
            if registry.get(record_type, record_id) is not None:
                raise ValueError(
                    "policy candidate immutable identity already exists: "
                    f"{record_type}:{record_id}"
                )
        for kind, identity in (
            ("model", spec.model_version_id),
            ("metrics", spec.evaluation_bundle_id),
            ("evaluation", spec.evaluation_bundle_id),
        ):
            if artifact_store.exists(kind, identity):
                raise ValueError(
                    f"policy candidate artifact identity already exists: {kind}:{identity}"
                )
    elif existing_experiment.payload != placeholder.to_payload():
        raise ValueError("conflicting immutable policy experiment identity")

    model_payload = {
        "schema_version": 1,
        "kind": "autosport-transparent-bandit-policy-model-v1",
        "model_version_id": spec.model_version_id,
        "policy_id": evaluation.challenger_policy_id,
        "policy_artifact_sha256": policy_artifact_sha256,
        "research_protocol_id": spec.research_protocol_id,
        "dataset_snapshot_id": spec.dataset_snapshot_id,
        "feature_set_id": spec.feature_set_id,
        "config_sha256": config_sha256,
        "evaluator_config_sha256": evaluation_config.config_sha256,
        "policy_evaluation_sha256": evaluation.evaluation_sha256,
        "dataset_manifest_sha256": dataset_manifest_sha256,
        "seed": spec.seed,
    }
    model_artifact_sha256 = artifact_store.write(
        "model", spec.model_version_id, model_payload
    )
    registry.append(
        _impl.ModelVersion(
            spec.model_version_id,
            "transparent-bandit-policy-v1",
            model_artifact_sha256,
            spec.source_sha256,
            spec.environment_sha256,
            spec.dataset_snapshot_id,
            spec.feature_set_id,
            spec.research_protocol_id,
            spec.seed,
            config_sha256,
            spec.created_at,
            predecessor_model_version_id=spec.predecessor_model_version_id,
        )
    )
    registry.append(
        _impl.StrategyVersion(
            spec.strategy_version_id,
            spec.canonical_strategy_id,
            spec.source_sha256,
            spec.environment_sha256,
            config_sha256,
            spec.created_at,
            model_version_id=spec.model_version_id,
            predecessor_strategy_version_id=spec.predecessor_strategy_version_id,
        )
    )

    metrics_payload = {
        "schema_version": 1,
        "kind": "autosport-factory-metrics-v1",
        "evaluation_bundle_id": spec.evaluation_bundle_id,
        "strategy_version_id": spec.strategy_version_id,
        "model_version_id": spec.model_version_id,
        "metrics": challenger_metrics,
        "predecessor_policy_id": evaluation.predecessor_policy_id,
        "predecessor_metrics": predecessor_metrics,
        "source": "paired-policy-causal-v1",
        "policy_evaluation_sha256": evaluation.evaluation_sha256,
        "evaluator_config_sha256": evaluation_config.config_sha256,
        "dataset_manifest_sha256": dataset_manifest_sha256,
    }
    metrics_sha256 = artifact_store.write(
        "metrics", spec.evaluation_bundle_id, metrics_payload
    )

    paired = evaluation.paired_improvements
    uncertainty_method, effect_low, effect_high = _impl._promotion_effect_interval(
        paired, binding.get("uncertainty_method")
    )
    practical = evaluation.practical_improvement
    conservative_ess = int(evaluation.effective_sample_size)
    if conservative_ess < 1:
        raise ValueError("policy evaluation effective sample size is below one")
    conservative_ess = min(conservative_ess, len(evaluation.samples))
    challenger_metric_decimals = dict(evaluation.challenger_metrics)
    guardrails_passed = all(
        challenger_metric_decimals[name] <= Decimal(str(maximum))
        for name, maximum in rule.protective_metric_maxima
    )
    dataset_source = dataset.payload.get("source_identity")
    dataset_license = dataset.payload.get("license_identity")
    confirmation_trial_family_id = (
        f"{spec.research_protocol_id}:confirmation-trial-family"
    )
    holdout_access_id = _impl.promotion_holdout_access_id(
        research_protocol_id=spec.research_protocol_id,
        dataset_manifest_sha256=dataset_manifest_sha256,
        source_identity=dataset_source,
        license_identity=dataset_license,
        confirmation_trial_family_id=confirmation_trial_family_id,
    )
    same_attempt_identity = {
        "experiment_id": spec.experiment_id,
        "research_protocol_id": spec.research_protocol_id,
        "research_question_id": question_id,
        "hypothesis_id": hypothesis_id,
        "candidate_strategy_version_id": spec.strategy_version_id,
        "candidate_model_version_id": spec.model_version_id,
        "evaluation_bundle_id": spec.evaluation_bundle_id,
        "dataset_snapshot_id": spec.dataset_snapshot_id,
        "confirmation_trial_family_id": confirmation_trial_family_id,
        "holdout_access_id": holdout_access_id,
        "estimand": rule.primary_metric,
        "direction": _impl.PromotionEvidenceDirection.LOWER_IS_BETTER.value,
        "rollback_identity": current_champion,
        "created_at": spec.decided_at,
    }
    holdout_consumed = _impl._holdout_consumed_by_other_evidence(
        (
            prior.payload
            for prior in registry.causal_records(
                "PromotionEvidence", as_of=spec.decided_at
            )
        ),
        same_attempt_identity=same_attempt_identity,
    )
    minimum = Decimal(str(rule.minimum_improvement))
    validity = (
        _impl.PromotionEvidenceValidity.ELIGIBLE
        if conservative_ess >= rule.minimum_effective_sample_size
        and effect_low >= minimum
        and practical >= minimum
        and guardrails_passed
        else _impl.PromotionEvidenceValidity.INCONCLUSIVE
    )
    stopping_sha = hashlib.sha256(
        str(binding.get("stopping_rule")).encode("utf-8")
    ).hexdigest()
    comparison_sha = hashlib.sha256(
        str(binding.get("multiple_comparison_control")).encode("utf-8")
    ).hexdigest()

    evaluation_payload = {
        "schema_version": 1,
        "kind": "autosport-policy-specific-factory-evaluation-v1",
        "evaluation_bundle_id": spec.evaluation_bundle_id,
        "experiment_id": spec.experiment_id,
        "research_protocol_id": spec.research_protocol_id,
        "protocol_sha256": protocol_sha256,
        "promotion_rule_sha256": rule.rule_sha256,
        "dataset_snapshot_id": spec.dataset_snapshot_id,
        "feature_set_id": spec.feature_set_id,
        "model_version_id": spec.model_version_id,
        "strategy_version_id": spec.strategy_version_id,
        "predecessor_strategy_version_id": current_champion,
        "predecessor_policy_id": evaluation.predecessor_policy_id,
        "challenger_policy_id": evaluation.challenger_policy_id,
        "evaluator_source_sha256": spec.evaluator_source_sha256.lower(),
        "evaluator_config": evaluation_config.canonical_payload(),
        "evaluator_config_sha256": evaluation_config.config_sha256,
        "dataset_manifest_sha256": dataset_manifest_sha256,
        "policy_evaluation": evaluation.canonical_payload(),
        "policy_evaluation_sha256": evaluation.evaluation_sha256,
        "candidate_metrics": challenger_metrics,
        "predecessor_metrics": predecessor_metrics,
        "candidate_metrics_artifact_sha256": metrics_sha256,
        "candidate_metrics_source": "paired-policy-causal-v1",
        "effective_sample_size_exact": _canonical_decimal_text(
            evaluation.effective_sample_size
        ),
        "promotion_verdict": provisional.verdict.value,
        "completed_at": spec.completed_at,
        "decided_at": spec.decided_at,
        "truth": {
            "real_money_execution": False,
            "auto_execution_authority": False,
            "counterfactual_rewards_invented": False,
            "llm_arithmetic_authority": False,
        },
    }
    evaluation_bundle_sha256 = artifact_store.write(
        "evaluation", spec.evaluation_bundle_id, evaluation_payload
    )
    registry.append(
        _impl.EvaluationBundleRef(
            spec.evaluation_bundle_id,
            evaluation_bundle_sha256,
            spec.evaluator_source_sha256,
            spec.dataset_snapshot_id,
            protocol_sha256,
            (model_artifact_sha256, metrics_sha256),
            spec.completed_at,
            evaluated_strategy_version_id=spec.strategy_version_id,
            evaluated_model_version_id=spec.model_version_id,
            effective_sample_size=conservative_ess,
            effect_interval_low=_canonical_decimal_text(effect_low),
            effect_interval_high=_canonical_decimal_text(effect_high),
            practical_improvement=_canonical_decimal_text(practical),
        )
    )

    evidence_payload = {
        "schema_version": 1,
        "experiment_id": spec.experiment_id,
        "research_protocol_id": spec.research_protocol_id,
        "research_question_id": question_id,
        "hypothesis_id": hypothesis_id,
        "candidate_strategy_version_id": spec.strategy_version_id,
        "candidate_model_version_id": spec.model_version_id,
        "evaluation_bundle_id": spec.evaluation_bundle_id,
        "evaluation_bundle_sha256": evaluation_bundle_sha256,
        "dataset_snapshot_id": spec.dataset_snapshot_id,
        "holdout_access_id": holdout_access_id,
        "confirmation_trial_family_id": confirmation_trial_family_id,
        "estimand": rule.primary_metric,
        "direction": _impl.PromotionEvidenceDirection.LOWER_IS_BETTER.value,
        "cohort_id": spec.dataset_snapshot_id,
        "effective_sample_size": conservative_ess,
        "minimum_effective_sample_size": rule.minimum_effective_sample_size,
        "effect_interval_low": _canonical_decimal_text(effect_low),
        "effect_interval_high": _canonical_decimal_text(effect_high),
        "practical_improvement": _canonical_decimal_text(practical),
        "guardrails_passed": guardrails_passed,
        "validity": validity.value,
        "holdout_consumed": holdout_consumed,
        "stopping_rule_sha256": stopping_sha,
        "multiple_comparison_control_sha256": comparison_sha,
        "rollback_identity": current_champion,
        "uncertainty_method": uncertainty_method,
        "created_at": spec.decided_at,
    }
    evidence_id = _impl._canonical_digest(evidence_payload)
    typed = dict(evidence_payload)
    typed.pop("schema_version")
    typed["direction"] = _impl.PromotionEvidenceDirection(typed["direction"])
    typed["validity"] = _impl.PromotionEvidenceValidity(typed["validity"])
    promotion_evidence = _impl.PromotionEvidence(
        promotion_evidence_id=evidence_id,
        **typed,
    )
    promotion = _impl.PromotionController.evaluate(
        rule,
        champion_metrics=predecessor_metrics,
        challenger_metrics=challenger_metrics,
        provenance_complete=True,
        rollback_target=current_champion,
        promotion_evidence=promotion_evidence,
    )
    outcome = (
        _impl.ResearchOutcome.POSITIVE
        if promotion.verdict is _impl.PromotionVerdict.PROMOTE
        else (
            _impl.ResearchOutcome.NEGATIVE
            if promotion.verdict is _impl.PromotionVerdict.REJECT
            else _impl.ResearchOutcome.INCONCLUSIVE
        )
    )
    experiment = _impl.ExperimentRecord(
        spec.experiment_id,
        spec.research_protocol_id,
        spec.dataset_snapshot_id,
        spec.feature_set_id,
        spec.strategy_version_id,
        spec.evaluation_bundle_id,
        spec.seed,
        config_sha256,
        outcome,
        spec.created_at,
        model_version_id=spec.model_version_id,
        completed_at=spec.completed_at,
        notes="; ".join(promotion.reasons),
    )
    registry.append(experiment)
    registry.append(promotion_evidence)
    registry.record_promotion(
        _impl.PromotionDecision(
            spec.promotion_decision_id,
            promotion.registry_action,
            spec.strategy_version_id,
            spec.research_protocol_id,
            protocol_sha256,
            spec.evaluation_bundle_id,
            evaluation_bundle_sha256,
            spec.decided_at,
            predecessor_strategy_version_id=current_champion,
            candidate_model_version_id=spec.model_version_id,
            promotion_evidence_id=promotion_evidence.promotion_evidence_id,
            reason="; ".join(promotion.reasons),
        )
    )
    if outcome is not _impl.ResearchOutcome.POSITIVE:
        registry.append(
            _impl.Postmortem(
                f"{spec.experiment_id}:postmortem",
                spec.experiment_id,
                outcome,
                "; ".join(promotion.reasons)
                or "policy-specific promotion evidence was insufficient",
                ("new frozen protocol version or explicitly authorized retest",),
                spec.decided_at,
            )
        )
    reproducibility = registry.reproducibility_bundle(spec.experiment_id)
    return _impl.FactoryRunResult(
        spec.experiment_id,
        spec.model_version_id,
        spec.strategy_version_id,
        spec.evaluation_bundle_id,
        spec.promotion_decision_id,
        evaluation_bundle_sha256,
        reproducibility["bundle_sha256"],
        promotion.verdict,
        promotion.registry_action,
        challenger_metrics,
    )


class ExperimentRunner(_impl.ExperimentRunner):
    """Factory runner with one fail-closed workspace transaction per candidate.

    The existing implementation remains the scientific/evaluation authority. This
    facade changes only the durable commit boundary: all candidate registry rows and
    artifacts are first validated against one lock-protected snapshot, staged in an
    isolated workspace, and published only after the complete promotion lineage is
    accepted. Interrupted publication is recovered from a hash-bound transaction
    manifest before any later candidate may reuse an immutable identity.
    """


    def run_policy_candidate(
        self,
        spec: _impl.FactoryCandidateSpec,
        evaluation: PolicyPairEvaluation,
        *,
        rule: _impl.PromotionRule,
        policy_artifact_sha256: str,
    ) -> _impl.FactoryRunResult:
        """Atomically publish a policy-specific causal candidate and promotion evidence."""

        real_registry = self.registry
        real_store = self.artifact_store
        with WorkspaceEconomicLock(real_registry.path.parent):
            _recover_interrupted_factory_publish(real_registry, real_store)
            original_state = real_registry._read()
            with tempfile.TemporaryDirectory(
                prefix="autosport-policy-factory-transaction-"
            ) as temporary_directory:
                temporary_root = Path(temporary_directory)
                staged_registry_path = temporary_root / "scientific_registry.json"
                atomic_write_json(staged_registry_path, original_state)
                staged_registry = ScientificRegistry(staged_registry_path)
                staged_store = _StagedFactoryArtifactStore(
                    real_store,
                    temporary_root / "factory-artifacts",
                )
                result = _run_policy_candidate_unstaged(
                    staged_registry,
                    staged_store,
                    spec,
                    evaluation,
                    rule=rule,
                    policy_artifact_sha256=policy_artifact_sha256,
                )
                final_state = staged_registry._read()
                transaction_path = _publish_transaction_path(real_registry)
                transaction = {
                    "schema_version": 1,
                    "phase": "prepared",
                    "original_registry_sha256": _registry_state_sha256(original_state),
                    "final_registry_sha256": _registry_state_sha256(final_state),
                    "artifacts": list(staged_store.transaction_artifacts()),
                }
                atomic_write_json(transaction_path, transaction)
                try:
                    created_artifacts = staged_store.publish()
                except Exception as publish_error:
                    _unlink_transaction_manifest(transaction_path, publish_error)
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
                                "policy factory registry rollback also failed: "
                                f"{type(restore_error).__name__}: {restore_error}"
                            )
                        except BaseException:
                            pass
                    staged_store._rollback(created_artifacts, publish_error)
                    _unlink_transaction_manifest(transaction_path, publish_error)
                    raise
                _unlink_transaction_manifest(transaction_path)
                return result

    @staticmethod
    def verify_policy_restart(
        registry_path: str | Path,
        artifact_root: str | Path,
        experiment_id: str,
        *,
        as_of: str,
    ) -> _impl.FactoryRestartEvidence:
        """Verify one persisted policy-specific experiment from durable hashes only."""

        from .transparent_bandit_policy import BanditPolicyState

        registry = ScientificRegistry(registry_path)
        experiment = registry.get("Experiment", experiment_id)
        if experiment is None:
            raise ValueError("policy experiment is missing after restart")
        payload = experiment.payload
        evaluation_bundle_id = payload.get("evaluation_bundle_id")
        model_version_id = payload.get("model_version_id")
        strategy_version_id = payload.get("strategy_version_id")
        dataset_snapshot_id = payload.get("dataset_snapshot_id")
        research_protocol_id = payload.get("research_protocol_id")
        if not all(
            isinstance(value, str) and value
            for value in (
                evaluation_bundle_id,
                model_version_id,
                strategy_version_id,
                dataset_snapshot_id,
                research_protocol_id,
            )
        ):
            raise ValueError("policy experiment lineage is incomplete after restart")

        bundle = registry.get("EvaluationBundle", evaluation_bundle_id)
        model = registry.get("ModelVersion", model_version_id)
        strategy = registry.get("StrategyVersion", strategy_version_id)
        dataset = registry.get("DatasetSnapshot", dataset_snapshot_id)
        protocol = registry.get("ResearchProtocol", research_protocol_id)
        if any(value is None for value in (bundle, model, strategy, dataset, protocol)):
            raise ValueError("policy scientific lineage is incomplete after restart")
        assert bundle is not None
        assert model is not None
        assert strategy is not None
        assert dataset is not None
        assert protocol is not None

        if bundle.payload.get("evaluated_strategy_version_id") != strategy_version_id:
            raise ValueError("policy EvaluationBundle strategy identity mismatch")
        if bundle.payload.get("evaluated_model_version_id") != model_version_id:
            raise ValueError("policy EvaluationBundle model identity mismatch")
        if strategy.payload.get("model_version_id") != model_version_id:
            raise ValueError("policy StrategyVersion model lineage mismatch")
        predecessor_strategy_version_id = strategy.payload.get(
            "predecessor_strategy_version_id"
        )
        if not isinstance(predecessor_strategy_version_id, str) or not predecessor_strategy_version_id:
            raise ValueError("policy StrategyVersion predecessor identity is missing")

        store = FactoryArtifactStore(artifact_root)
        evaluation_payload = store.read(
            "evaluation",
            evaluation_bundle_id,
            expected_sha256=bundle.payload.get("bundle_sha256"),
        )
        if evaluation_payload.get("kind") != "autosport-policy-specific-factory-evaluation-v1":
            raise ValueError("restart evaluation is not policy-specific causal evidence")
        exact_lineage = {
            "experiment_id": experiment_id,
            "research_protocol_id": research_protocol_id,
            "dataset_snapshot_id": dataset_snapshot_id,
            "model_version_id": model_version_id,
            "strategy_version_id": strategy_version_id,
            "predecessor_strategy_version_id": predecessor_strategy_version_id,
            "challenger_policy_id": strategy_version_id,
            "predecessor_policy_id": predecessor_strategy_version_id,
        }
        for key, expected in exact_lineage.items():
            if evaluation_payload.get(key) != expected:
                raise ValueError(f"policy restart evaluation lineage mismatch: {key}")
        if evaluation_payload.get("candidate_metrics_source") != "paired-policy-causal-v1":
            raise ValueError("policy restart evaluation lacks causal metric source")
        if evaluation_payload.get("truth") != {
            "real_money_execution": False,
            "auto_execution_authority": False,
            "counterfactual_rewards_invented": False,
            "llm_arithmetic_authority": False,
        }:
            raise ValueError("policy restart truth boundary mismatch")

        binding = protocol.payload.get("binding")
        if type(binding) is not dict:
            raise ValueError("policy research protocol lacks frozen binding after restart")
        evaluator_config = PolicyEvaluationConfig.from_frozen_text(
            binding.get("evaluation_design")
        )
        if evaluation_payload.get("evaluator_config") != evaluator_config.canonical_payload():
            raise ValueError("policy restart evaluator config payload mismatch")
        if evaluation_payload.get("evaluator_config_sha256") != evaluator_config.config_sha256:
            raise ValueError("policy restart evaluator config hash mismatch")
        dataset_manifest_sha256 = _impl._sha256(
            dataset.payload.get("manifest_sha256"), "dataset manifest_sha256"
        )
        if evaluation_payload.get("dataset_manifest_sha256") != dataset_manifest_sha256:
            raise ValueError("policy restart dataset manifest mismatch")
        if protocol.payload.get("dataset_manifest_sha256") != dataset_manifest_sha256:
            raise ValueError("policy restart protocol/dataset manifest mismatch")

        policy_evaluation = evaluation_payload.get("policy_evaluation")
        if type(policy_evaluation) is not dict:
            raise ValueError("policy restart lacks paired policy evaluation payload")
        if policy_evaluation.get("predecessor_policy_id") != predecessor_strategy_version_id:
            raise ValueError("policy restart predecessor evaluation identity mismatch")
        if policy_evaluation.get("challenger_policy_id") != strategy_version_id:
            raise ValueError("policy restart challenger evaluation identity mismatch")
        if policy_evaluation.get("dataset_manifest_sha256") != dataset_manifest_sha256:
            raise ValueError("policy restart paired cohort identity mismatch")
        policy_evaluation_sha256 = _impl._canonical_digest(policy_evaluation)
        if evaluation_payload.get("policy_evaluation_sha256") != policy_evaluation_sha256:
            raise ValueError("policy restart paired evaluation hash mismatch")

        artifact_hashes = tuple(bundle.payload.get("artifact_hashes", ()))
        model_payload = store.read(
            "model",
            model_version_id,
            expected_sha256=model.payload.get("artifact_sha256"),
        )
        if model.payload.get("artifact_sha256") not in artifact_hashes:
            raise ValueError("policy model artifact is not hash-bound to EvaluationBundle")
        if model_payload.get("kind") != "autosport-transparent-bandit-policy-model-v1":
            raise ValueError("policy restart model artifact kind mismatch")
        if model_payload.get("model_version_id") != model_version_id:
            raise ValueError("policy restart model artifact identity mismatch")
        if model_payload.get("policy_id") != strategy_version_id:
            raise ValueError("policy restart model does not bind evaluated policy")
        if model_payload.get("policy_evaluation_sha256") != policy_evaluation_sha256:
            raise ValueError("policy restart model/evaluation hash mismatch")
        if model_payload.get("dataset_manifest_sha256") != dataset_manifest_sha256:
            raise ValueError("policy restart model cohort manifest mismatch")

        metrics_sha256 = evaluation_payload.get("candidate_metrics_artifact_sha256")
        if not isinstance(metrics_sha256, str) or metrics_sha256 not in artifact_hashes:
            raise ValueError("policy metrics artifact is not hash-bound to EvaluationBundle")
        metrics_payload = store.read(
            "metrics",
            evaluation_bundle_id,
            expected_sha256=metrics_sha256,
        )
        if metrics_payload.get("source") != "paired-policy-causal-v1":
            raise ValueError("policy restart metrics lack paired causal source")
        if metrics_payload.get("strategy_version_id") != strategy_version_id:
            raise ValueError("policy restart metrics strategy identity mismatch")
        if metrics_payload.get("model_version_id") != model_version_id:
            raise ValueError("policy restart metrics model identity mismatch")
        if metrics_payload.get("predecessor_policy_id") != predecessor_strategy_version_id:
            raise ValueError("policy restart metrics predecessor identity mismatch")
        if metrics_payload.get("policy_evaluation_sha256") != policy_evaluation_sha256:
            raise ValueError("policy restart metrics/evaluation hash mismatch")
        if metrics_payload.get("dataset_manifest_sha256") != dataset_manifest_sha256:
            raise ValueError("policy restart metrics cohort manifest mismatch")
        if _impl._metric_map(
            evaluation_payload.get("candidate_metrics"),
            "policy evaluation candidate metrics",
        ) != _impl._metric_map(metrics_payload.get("metrics"), "policy metrics artifact"):
            raise ValueError("policy restart evaluation/metrics values mismatch")

        policy_artifact_sha256 = model_payload.get("policy_artifact_sha256")
        if not isinstance(policy_artifact_sha256, str):
            raise ValueError("policy restart model lacks policy artifact hash")
        policy_artifact = store.read(
            "transparent-bandit-policy",
            strategy_version_id,
            expected_sha256=policy_artifact_sha256,
        )
        if (
            type(policy_artifact) is not dict
            or policy_artifact.get("policy_id") != strategy_version_id
            or type(policy_artifact.get("policy")) is not dict
        ):
            raise ValueError("policy restart policy artifact identity mismatch")
        try:
            policy = BanditPolicyState.from_payload(policy_artifact["policy"])
        except (TypeError, ValueError) as exc:
            raise ValueError("policy restart policy artifact is invalid") from exc
        if policy.policy_id != strategy_version_id:
            raise ValueError("policy restart policy payload hash mismatch")
        if policy.predecessor_policy_id != predecessor_strategy_version_id:
            raise ValueError("policy restart policy predecessor mismatch")
        if policy.environment_id != strategy.payload.get("environment_sha256"):
            raise ValueError("policy restart policy environment mismatch")
        if policy.protocol_id != research_protocol_id:
            raise ValueError("policy restart policy protocol mismatch")
        if policy.config_sha256 != model.payload.get("config_sha256"):
            raise ValueError("policy restart policy config mismatch")
        if policy.seed != model.payload.get("seed"):
            raise ValueError("policy restart policy seed mismatch")

        decisions = tuple(
            decision
            for decision in registry.causal_records(
                "PromotionDecision", as_of=as_of
            )
            if decision.payload.get("candidate_strategy_version_id")
            == strategy_version_id
            and decision.payload.get("evaluation_bundle_id")
            == evaluation_bundle_id
            and decision.payload.get("candidate_model_version_id")
            == model_version_id
        )
        if len(decisions) != 1:
            raise ValueError("policy restart requires one exact promotion decision")
        decision = decisions[0]
        if (
            decision.payload.get("evaluation_bundle_sha256")
            != bundle.payload.get("bundle_sha256")
        ):
            raise ValueError("policy restart promotion/evaluation hash mismatch")
        evidence_id = decision.payload.get("promotion_evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            if decision.payload.get("action") == _impl.PromotionAction.PROMOTE.value:
                raise ValueError("policy restart PROMOTE lacks typed promotion evidence")
        else:
            evidence = registry.get("PromotionEvidence", evidence_id)
            if evidence is None:
                raise ValueError("policy restart promotion evidence is missing")
            ep = evidence.payload
            if ep.get("candidate_strategy_version_id") != strategy_version_id:
                raise ValueError("policy restart promotion evidence strategy mismatch")
            if ep.get("candidate_model_version_id") != model_version_id:
                raise ValueError("policy restart promotion evidence model mismatch")
            if ep.get("evaluation_bundle_sha256") != bundle.payload.get(
                "bundle_sha256"
            ):
                raise ValueError("policy restart promotion evidence bundle hash mismatch")
            if ep.get("rollback_identity") != predecessor_strategy_version_id:
                raise ValueError("policy restart promotion evidence rollback mismatch")

        reproducibility = registry.reproducibility_bundle(experiment_id)
        canonical_strategy_id = strategy.payload.get("canonical_strategy_id")
        if not isinstance(canonical_strategy_id, str) or not canonical_strategy_id:
            raise ValueError("policy restart canonical strategy identity is missing")
        champion = registry.champion_strategy(
            as_of=as_of,
            canonical_strategy_id=canonical_strategy_id,
        )
        return _impl.FactoryRestartEvidence(
            experiment_id,
            payload["fingerprint"],
            bundle.payload["bundle_sha256"],
            model.payload["artifact_sha256"],
            reproducibility["bundle_sha256"],
            champion,
            _impl.ResearchOutcome(payload["outcome"]),
        )

    def run_baseline_candidate(
        self,
        spec: _impl.FactoryCandidateSpec,
        points: Sequence[_impl.TrainingPoint],
        *,
        rule: _impl.PromotionRule,
        minimum_train_size: int | None = None,
    ) -> _impl.FactoryRunResult:
        real_registry = self.registry
        real_store = self.artifact_store

        with WorkspaceEconomicLock(real_registry.path.parent):
            _recover_interrupted_factory_publish(real_registry, real_store)
            original_state = real_registry._read()
            protocol = real_registry.get("ResearchProtocol", spec.research_protocol_id)
            if protocol is None:
                raise ValueError("factory research protocol is missing")
            binding = protocol.payload.get("binding")
            if type(binding) is not dict:
                raise ValueError("factory research protocol lacks frozen binding")
            evaluation_config = _impl.WalkForwardEvaluationConfig.from_frozen_text(
                binding.get("evaluation_design")
            )
            causal_factory = _CausalFinalFitFactory(
                self.baseline_model_factory,
                final_model_id=spec.model_version_id,
                minimum_causal_train_size=evaluation_config.minimum_causal_train_size,
            )
            with tempfile.TemporaryDirectory(
                prefix="autosport-factory-transaction-"
            ) as temporary_directory:
                temporary_root = Path(temporary_directory)
                staged_registry_path = temporary_root / "scientific_registry.json"
                atomic_write_json(staged_registry_path, original_state)
                staged_registry = ScientificRegistry(staged_registry_path)
                staged_store = _StagedFactoryArtifactStore(
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
                final_state = staged_registry._read()
                transaction_path = _publish_transaction_path(real_registry)
                transaction = {
                    "schema_version": 1,
                    "phase": "prepared",
                    "original_registry_sha256": _registry_state_sha256(original_state),
                    "final_registry_sha256": _registry_state_sha256(final_state),
                    "artifacts": list(staged_store.transaction_artifacts()),
                }
                atomic_write_json(transaction_path, transaction)
                try:
                    created_artifacts = staged_store.publish()
                except Exception as publish_error:
                    _unlink_transaction_manifest(transaction_path, publish_error)
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
                                "factory transaction registry rollback also failed: "
                                f"{type(restore_error).__name__}: {restore_error}"
                            )
                        except BaseException:
                            pass
                    staged_store._rollback(created_artifacts, publish_error)
                    _unlink_transaction_manifest(transaction_path, publish_error)
                    raise
                _unlink_transaction_manifest(transaction_path)
                return result


# The implementation split is internal-only. Preserve the original public module
# identity for definitions whose public object was not replaced by a facade subclass.
for _public_name, _public_value in vars(_impl).items():
    if (
        not _public_name.startswith("_")
        and _public_name not in {"ExperimentRunner", "FactoryArtifactStore"}
        and getattr(_public_value, "__module__", None) == _impl.__name__
    ):
        try:
            _public_value.__module__ = __name__
        except (AttributeError, TypeError):
            pass
del _public_name, _public_value