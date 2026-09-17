from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .scientific_registry import PromotionAction, RegistryEntry, ScientificRegistry


class StrategyLifecycleState(StrEnum):
    """Derived factory-facing state; immutable registry history remains authoritative."""

    PROMOTED = "PROMOTED"
    CHALLENGER = "CHALLENGER"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class StrategyStateProjection:
    """Causal promotion-state projection for one canonical strategy key.

    The existing ``canonical_strategy_id`` is the stable class/context key for this
    foundation. The projection is derived on demand and never persists a second
    mutable promotion authority. ``champion_strategy_version_id`` is populated only
    when the registry's canonical global champion belongs to this strategy key.
    """

    canonical_strategy_id: str
    as_of: str
    champion_strategy_version_id: str | None
    promoted: tuple[str, ...]
    challengers: tuple[str, ...]
    rejected: tuple[str, ...]
    retired: tuple[str, ...]

    def state_of(self, strategy_version_id: str) -> StrategyLifecycleState | None:
        if strategy_version_id in self.promoted:
            return StrategyLifecycleState.PROMOTED
        if strategy_version_id in self.challengers:
            return StrategyLifecycleState.CHALLENGER
        if strategy_version_id in self.rejected:
            return StrategyLifecycleState.REJECTED
        if strategy_version_id in self.retired:
            return StrategyLifecycleState.RETIRED
        return None


@dataclass(frozen=True, slots=True)
class ScientificLineageProjection:
    """Deterministic causal read model over the append-only scientific registry."""

    query_type: str
    query_id: str
    as_of: str
    datasets: tuple[RegistryEntry, ...] = ()
    features: tuple[RegistryEntry, ...] = ()
    protocols: tuple[RegistryEntry, ...] = ()
    models: tuple[RegistryEntry, ...] = ()
    strategies: tuple[RegistryEntry, ...] = ()
    experiments: tuple[RegistryEntry, ...] = ()
    evaluations: tuple[RegistryEntry, ...] = ()
    promotions: tuple[RegistryEntry, ...] = ()
    postmortems: tuple[RegistryEntry, ...] = ()


class ScientificRegistryIndex:
    """Read-only factory-facing indexes derived from canonical registry history.

    No index state is persisted. Every result is recomputed from causal records at
    ``as_of`` so restart/replay behavior stays deterministic and future decisions
    cannot leak into historical queries.
    """

    def __init__(self, registry: ScientificRegistry) -> None:
        if not isinstance(registry, ScientificRegistry):
            raise TypeError("registry must be a ScientificRegistry")
        self.registry = registry

    def _records(self, record_type: str, as_of: str) -> tuple[RegistryEntry, ...]:
        return self.registry.causal_records(record_type, as_of=as_of)

    @staticmethod
    def _only(values: tuple[RegistryEntry, ...], **matches: str) -> tuple[RegistryEntry, ...]:
        return tuple(
            entry
            for entry in values
            if all(entry.payload.get(field) == value for field, value in matches.items())
        )

    @staticmethod
    def _ids(values: tuple[RegistryEntry, ...]) -> set[str]:
        return {entry.record_id for entry in values}

    def strategy_state(self, canonical_strategy_id: str, *, as_of: str) -> StrategyStateProjection:
        versions = self._only(
            self._records("StrategyVersion", as_of),
            canonical_strategy_id=canonical_strategy_id,
        )
        version_ids = self._ids(versions)
        if not version_ids:
            return StrategyStateProjection(
                canonical_strategy_id=canonical_strategy_id,
                as_of=as_of,
                champion_strategy_version_id=None,
                promoted=(),
                challengers=(),
                rejected=(),
                retired=(),
            )

        ever_promoted: set[str] = set()
        latest_action: dict[str, PromotionAction] = {}
        for entry in self._records("PromotionDecision", as_of):
            payload = entry.payload
            candidate = payload["candidate_strategy_version_id"]
            rollback_target = payload.get("rollback_to_strategy_version_id")
            action = PromotionAction(payload["action"])
            if candidate in version_ids:
                latest_action[candidate] = action
            if action is PromotionAction.PROMOTE and candidate in version_ids:
                ever_promoted.add(candidate)
            elif action is PromotionAction.ROLLBACK and rollback_target in version_ids:
                ever_promoted.add(rollback_target)

        global_champion = self.registry.champion_strategy(as_of=as_of)
        champion = global_champion if global_champion in version_ids else None
        rejected = {
            version_id
            for version_id, action in latest_action.items()
            if action is PromotionAction.REJECT and version_id not in ever_promoted
        }
        retired = ever_promoted - ({champion} if champion is not None else set())
        challengers = version_ids - rejected - retired - ({champion} if champion is not None else set())
        promoted = (champion,) if champion is not None else ()
        return StrategyStateProjection(
            canonical_strategy_id=canonical_strategy_id,
            as_of=as_of,
            champion_strategy_version_id=champion,
            promoted=promoted,
            challengers=tuple(sorted(challengers)),
            rejected=tuple(sorted(rejected)),
            retired=tuple(sorted(retired)),
        )

    def by_dataset(self, dataset_snapshot_id: str, *, as_of: str) -> ScientificLineageProjection:
        datasets = tuple(
            entry
            for entry in self._records("DatasetSnapshot", as_of)
            if entry.record_id == dataset_snapshot_id
        )
        models = self._only(
            self._records("ModelVersion", as_of),
            dataset_snapshot_id=dataset_snapshot_id,
        )
        experiments = self._only(
            self._records("Experiment", as_of),
            dataset_snapshot_id=dataset_snapshot_id,
        )
        evaluations = self._only(
            self._records("EvaluationBundle", as_of),
            dataset_snapshot_id=dataset_snapshot_id,
        )
        model_ids = self._ids(models)
        strategy_ids = {entry.payload["strategy_version_id"] for entry in experiments}
        strategy_ids.update(
            entry.payload["evaluated_strategy_version_id"]
            for entry in evaluations
            if entry.payload.get("evaluated_strategy_version_id") is not None
        )
        strategies = tuple(
            entry
            for entry in self._records("StrategyVersion", as_of)
            if entry.payload.get("model_version_id") in model_ids or entry.record_id in strategy_ids
        )
        strategy_ids = self._ids(strategies)
        evaluation_ids = self._ids(evaluations)
        promotions = tuple(
            entry
            for entry in self._records("PromotionDecision", as_of)
            if entry.payload.get("candidate_strategy_version_id") in strategy_ids
            or entry.payload.get("evaluation_bundle_id") in evaluation_ids
        )
        experiment_ids = self._ids(experiments)
        postmortems = tuple(
            entry
            for entry in self._records("Postmortem", as_of)
            if entry.payload.get("experiment_id") in experiment_ids
        )
        feature_ids = {entry.payload["feature_set_id"] for entry in models}
        feature_ids.update(entry.payload["feature_set_id"] for entry in experiments)
        protocol_ids = {entry.payload["research_protocol_id"] for entry in models}
        protocol_ids.update(entry.payload["research_protocol_id"] for entry in experiments)
        features = tuple(
            entry for entry in self._records("FeatureSet", as_of) if entry.record_id in feature_ids
        )
        protocols = tuple(
            entry for entry in self._records("ResearchProtocol", as_of) if entry.record_id in protocol_ids
        )
        return ScientificLineageProjection(
            "DatasetSnapshot",
            dataset_snapshot_id,
            as_of,
            datasets=datasets,
            features=features,
            protocols=protocols,
            models=models,
            strategies=strategies,
            experiments=experiments,
            evaluations=evaluations,
            promotions=promotions,
            postmortems=postmortems,
        )

    def by_model(self, model_version_id: str, *, as_of: str) -> ScientificLineageProjection:
        models = tuple(
            entry
            for entry in self._records("ModelVersion", as_of)
            if entry.record_id == model_version_id
        )
        strategies = self._only(
            self._records("StrategyVersion", as_of),
            model_version_id=model_version_id,
        )
        experiments = self._only(
            self._records("Experiment", as_of),
            model_version_id=model_version_id,
        )
        experiment_evaluation_ids = {entry.payload["evaluation_bundle_id"] for entry in experiments}
        evaluations = tuple(
            entry
            for entry in self._records("EvaluationBundle", as_of)
            if entry.payload.get("evaluated_model_version_id") == model_version_id
            or entry.record_id in experiment_evaluation_ids
        )
        dataset_ids = {entry.payload["dataset_snapshot_id"] for entry in models}
        dataset_ids.update(entry.payload["dataset_snapshot_id"] for entry in experiments)
        dataset_ids.update(entry.payload["dataset_snapshot_id"] for entry in evaluations)
        feature_ids = {entry.payload["feature_set_id"] for entry in models}
        feature_ids.update(entry.payload["feature_set_id"] for entry in experiments)
        protocol_ids = {entry.payload["research_protocol_id"] for entry in models}
        protocol_ids.update(entry.payload["research_protocol_id"] for entry in experiments)
        datasets = tuple(
            entry for entry in self._records("DatasetSnapshot", as_of) if entry.record_id in dataset_ids
        )
        features = tuple(
            entry for entry in self._records("FeatureSet", as_of) if entry.record_id in feature_ids
        )
        protocols = tuple(
            entry for entry in self._records("ResearchProtocol", as_of) if entry.record_id in protocol_ids
        )
        strategy_ids = self._ids(strategies)
        promotions = tuple(
            entry
            for entry in self._records("PromotionDecision", as_of)
            if entry.payload.get("candidate_model_version_id") == model_version_id
            or entry.payload.get("candidate_strategy_version_id") in strategy_ids
        )
        experiment_ids = self._ids(experiments)
        postmortems = tuple(
            entry
            for entry in self._records("Postmortem", as_of)
            if entry.payload.get("experiment_id") in experiment_ids
        )
        return ScientificLineageProjection(
            "ModelVersion",
            model_version_id,
            as_of,
            datasets=datasets,
            features=features,
            protocols=protocols,
            models=models,
            strategies=strategies,
            experiments=experiments,
            evaluations=evaluations,
            promotions=promotions,
            postmortems=postmortems,
        )

    def by_strategy(self, strategy_version_id: str, *, as_of: str) -> ScientificLineageProjection:
        strategies = tuple(
            entry
            for entry in self._records("StrategyVersion", as_of)
            if entry.record_id == strategy_version_id
        )
        experiments = self._only(
            self._records("Experiment", as_of),
            strategy_version_id=strategy_version_id,
        )
        experiment_evaluation_ids = {entry.payload["evaluation_bundle_id"] for entry in experiments}
        evaluations = tuple(
            entry
            for entry in self._records("EvaluationBundle", as_of)
            if entry.payload.get("evaluated_strategy_version_id") == strategy_version_id
            or entry.record_id in experiment_evaluation_ids
        )
        model_ids = {
            entry.payload["model_version_id"]
            for entry in strategies
            if entry.payload.get("model_version_id") is not None
        }
        model_ids.update(
            entry.payload["model_version_id"]
            for entry in experiments
            if entry.payload.get("model_version_id") is not None
        )
        model_ids.update(
            entry.payload["evaluated_model_version_id"]
            for entry in evaluations
            if entry.payload.get("evaluated_model_version_id") is not None
        )
        models = tuple(
            entry for entry in self._records("ModelVersion", as_of) if entry.record_id in model_ids
        )
        dataset_ids = {entry.payload["dataset_snapshot_id"] for entry in models}
        dataset_ids.update(entry.payload["dataset_snapshot_id"] for entry in experiments)
        dataset_ids.update(entry.payload["dataset_snapshot_id"] for entry in evaluations)
        feature_ids = {entry.payload["feature_set_id"] for entry in models}
        feature_ids.update(entry.payload["feature_set_id"] for entry in experiments)
        protocol_ids = {entry.payload["research_protocol_id"] for entry in models}
        protocol_ids.update(entry.payload["research_protocol_id"] for entry in experiments)
        datasets = tuple(
            entry for entry in self._records("DatasetSnapshot", as_of) if entry.record_id in dataset_ids
        )
        features = tuple(
            entry for entry in self._records("FeatureSet", as_of) if entry.record_id in feature_ids
        )
        protocols = tuple(
            entry for entry in self._records("ResearchProtocol", as_of) if entry.record_id in protocol_ids
        )
        promotions = tuple(
            entry
            for entry in self._records("PromotionDecision", as_of)
            if entry.payload.get("candidate_strategy_version_id") == strategy_version_id
            or entry.payload.get("predecessor_strategy_version_id") == strategy_version_id
            or entry.payload.get("rollback_to_strategy_version_id") == strategy_version_id
        )
        experiment_ids = self._ids(experiments)
        postmortems = tuple(
            entry
            for entry in self._records("Postmortem", as_of)
            if entry.payload.get("experiment_id") in experiment_ids
        )
        return ScientificLineageProjection(
            "StrategyVersion",
            strategy_version_id,
            as_of,
            datasets=datasets,
            features=features,
            protocols=protocols,
            models=models,
            strategies=strategies,
            experiments=experiments,
            evaluations=evaluations,
            promotions=promotions,
            postmortems=postmortems,
        )
