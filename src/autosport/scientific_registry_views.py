from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .scientific_registry import (
    PromotionAction,
    RegistryEntry,
    ScientificRegistry,
    ScientificRegistryError,
)


class StrategyLifecycleState(StrEnum):
    """Deterministic derived state from immutable promotion history."""

    CANDIDATE = "CANDIDATE"
    CHALLENGER = "CHALLENGER"
    CHAMPION = "CHAMPION"
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"
    FORMER_CHAMPION = "FORMER_CHAMPION"


@dataclass(frozen=True, slots=True)
class StrategyStateProjection:
    canonical_strategy_id: str
    strategy_version_id: str
    state: StrategyLifecycleState
    model_version_id: str | None
    predecessor_strategy_version_id: str | None
    latest_decision_id: str | None
    latest_decision_action: PromotionAction | None


@dataclass(frozen=True, slots=True)
class StrategyLineageProjection:
    strategy: RegistryEntry
    models: tuple[RegistryEntry, ...]
    datasets: tuple[RegistryEntry, ...]
    feature_sets: tuple[RegistryEntry, ...]
    protocols: tuple[RegistryEntry, ...]
    experiments: tuple[RegistryEntry, ...]
    evaluations: tuple[RegistryEntry, ...]
    promotion_decisions: tuple[RegistryEntry, ...]
    postmortems: tuple[RegistryEntry, ...]
    predecessor_strategies: tuple[RegistryEntry, ...] = ()


def _causal_map(
    registry: ScientificRegistry,
    record_type: str,
    *,
    as_of: str,
) -> dict[str, RegistryEntry]:
    return {
        entry.record_id: entry
        for entry in registry.causal_records(record_type, as_of=as_of)
    }


def _predecessor_chain(
    by_id: dict[str, RegistryEntry],
    start_id: str,
    field: str,
    record_type: str,
) -> tuple[str, ...]:
    """Return start + causal predecessor chain, failing closed on gaps/cycles."""

    ordered: list[str] = []
    seen: set[str] = set()
    current_id: str | None = start_id
    while current_id is not None:
        if current_id in seen:
            raise ScientificRegistryError(
                f"{record_type} predecessor cycle detected at {current_id}"
            )
        entry = by_id.get(current_id)
        if entry is None:
            raise ScientificRegistryError(
                f"lineage references causally missing {record_type}:{current_id}"
            )
        ordered.append(current_id)
        seen.add(current_id)
        predecessor = entry.payload.get(field)
        if predecessor is None:
            current_id = None
        elif isinstance(predecessor, str) and predecessor:
            current_id = predecessor
        else:
            raise ScientificRegistryError(
                f"{record_type}:{current_id} has invalid {field}"
            )
    return tuple(ordered)


def _expand_model_predecessors(
    models_by_id: dict[str, RegistryEntry],
    roots: set[str],
) -> set[str]:
    expanded: set[str] = set()
    for model_id in sorted(roots):
        expanded.update(
            _predecessor_chain(
                models_by_id,
                model_id,
                "predecessor_model_version_id",
                "ModelVersion",
            )
        )
    return expanded


def strategy_state_projection(
    registry: ScientificRegistry,
    canonical_strategy_id: str,
    *,
    as_of: str,
) -> tuple[StrategyStateProjection, ...]:
    """Project causal champion/challenger lifecycle state for one strategy key."""

    if type(canonical_strategy_id) is not str or not canonical_strategy_id:
        raise ValueError("canonical_strategy_id must be a non-empty string")

    strategies = tuple(
        entry
        for entry in registry.causal_records("StrategyVersion", as_of=as_of)
        if entry.payload.get("canonical_strategy_id") == canonical_strategy_id
    )
    strategy_ids = {entry.record_id for entry in strategies}
    decisions = registry.causal_records("PromotionDecision", as_of=as_of)
    champion = registry.champion_strategy(
        as_of=as_of, canonical_strategy_id=canonical_strategy_id
    )

    latest_candidate_decision: dict[str, RegistryEntry] = {}
    promoted: set[str] = set()
    for decision in decisions:
        payload = decision.payload
        candidate = payload.get("candidate_strategy_version_id")
        if candidate in strategy_ids:
            latest_candidate_decision[candidate] = decision
        if (
            payload.get("action") == PromotionAction.PROMOTE.value
            and candidate in strategy_ids
        ):
            promoted.add(candidate)

    projections: list[StrategyStateProjection] = []
    for strategy in strategies:
        strategy_id = strategy.record_id
        latest = latest_candidate_decision.get(strategy_id)
        latest_action = (
            PromotionAction(latest.payload["action"]) if latest is not None else None
        )
        if strategy_id == champion:
            state = StrategyLifecycleState.CHAMPION
        elif latest_action is PromotionAction.ROLLBACK:
            state = StrategyLifecycleState.ROLLED_BACK
        elif latest_action is PromotionAction.REJECT:
            state = StrategyLifecycleState.REJECTED
        elif latest_action is PromotionAction.RETAIN:
            state = StrategyLifecycleState.CHALLENGER
        elif strategy_id in promoted:
            state = StrategyLifecycleState.FORMER_CHAMPION
        else:
            state = StrategyLifecycleState.CANDIDATE

        projections.append(
            StrategyStateProjection(
                canonical_strategy_id=canonical_strategy_id,
                strategy_version_id=strategy_id,
                state=state,
                model_version_id=strategy.payload.get("model_version_id"),
                predecessor_strategy_version_id=strategy.payload.get(
                    "predecessor_strategy_version_id"
                ),
                latest_decision_id=latest.record_id if latest is not None else None,
                latest_decision_action=latest_action,
            )
        )
    return tuple(projections)


def strategy_lineage_projection(
    registry: ScientificRegistry,
    strategy_version_id: str,
    *,
    as_of: str,
) -> StrategyLineageProjection:
    """Resolve complete causal predecessor/evidence lineage for a strategy."""

    if type(strategy_version_id) is not str or not strategy_version_id:
        raise ValueError("strategy_version_id must be a non-empty string")

    strategies_all = registry.causal_records("StrategyVersion", as_of=as_of)
    strategies_by_id = {entry.record_id: entry for entry in strategies_all}
    strategy = strategies_by_id.get(strategy_version_id)
    if strategy is None:
        raise ScientificRegistryError(
            f"strategy lineage is not causally available: {strategy_version_id}"
        )

    strategy_chain = _predecessor_chain(
        strategies_by_id,
        strategy_version_id,
        "predecessor_strategy_version_id",
        "StrategyVersion",
    )
    strategy_ids = set(strategy_chain)
    predecessor_strategies = tuple(
        entry
        for entry in strategies_all
        if entry.record_id in strategy_ids and entry.record_id != strategy_version_id
    )

    experiments_all = registry.causal_records("Experiment", as_of=as_of)
    experiments = tuple(
        entry
        for entry in experiments_all
        if entry.payload.get("strategy_version_id") in strategy_ids
    )

    model_roots = {
        entry.payload["model_version_id"]
        for entry in predecessor_strategies + (strategy,)
        if isinstance(entry.payload.get("model_version_id"), str)
    }
    model_roots.update(
        entry.payload["model_version_id"]
        for entry in experiments
        if isinstance(entry.payload.get("model_version_id"), str)
    )

    models_all = registry.causal_records("ModelVersion", as_of=as_of)
    models_by_id = {entry.record_id: entry for entry in models_all}
    model_ids = _expand_model_predecessors(models_by_id, model_roots)
    models = tuple(entry for entry in models_all if entry.record_id in model_ids)

    evaluation_ids = {
        entry.payload["evaluation_bundle_id"] for entry in experiments
    }
    evaluations_all = registry.causal_records("EvaluationBundle", as_of=as_of)
    evaluations_by_id = {entry.record_id: entry for entry in evaluations_all}
    missing_evaluations = sorted(evaluation_ids - evaluations_by_id.keys())
    if missing_evaluations:
        raise ScientificRegistryError(
            "strategy lineage references causally missing "
            f"EvaluationBundle:{missing_evaluations[0]}"
        )
    evaluations = tuple(
        entry for entry in evaluations_all if entry.record_id in evaluation_ids
    )

    dataset_ids = {
        entry.payload["dataset_snapshot_id"] for entry in experiments
    }
    feature_ids = {entry.payload["feature_set_id"] for entry in experiments}
    protocol_ids = {
        entry.payload["research_protocol_id"] for entry in experiments
    }
    for model in models:
        dataset_ids.add(model.payload["dataset_snapshot_id"])
        feature_ids.add(model.payload["feature_set_id"])
        protocol_ids.add(model.payload["research_protocol_id"])
    for evaluation in evaluations:
        dataset_ids.add(evaluation.payload["dataset_snapshot_id"])

    def resolve_many(
        record_type: str, ids: set[str]
    ) -> tuple[RegistryEntry, ...]:
        values = registry.causal_records(record_type, as_of=as_of)
        by_id = {entry.record_id: entry for entry in values}
        missing = sorted(ids - by_id.keys())
        if missing:
            raise ScientificRegistryError(
                f"strategy lineage references causally missing "
                f"{record_type}:{missing[0]}"
            )
        return tuple(entry for entry in values if entry.record_id in ids)

    datasets = resolve_many("DatasetSnapshot", dataset_ids)
    feature_sets = resolve_many("FeatureSet", feature_ids)
    protocols = resolve_many("ResearchProtocol", protocol_ids)

    promotion_decisions = tuple(
        entry
        for entry in registry.causal_records("PromotionDecision", as_of=as_of)
        if entry.payload.get("candidate_strategy_version_id") in strategy_ids
        or entry.payload.get("predecessor_strategy_version_id") in strategy_ids
        or entry.payload.get("rollback_to_strategy_version_id") in strategy_ids
    )
    experiment_ids = {entry.record_id for entry in experiments}
    postmortems = tuple(
        entry
        for entry in registry.causal_records("Postmortem", as_of=as_of)
        if entry.payload.get("experiment_id") in experiment_ids
    )

    return StrategyLineageProjection(
        strategy=strategy,
        models=models,
        datasets=datasets,
        feature_sets=feature_sets,
        protocols=protocols,
        experiments=experiments,
        evaluations=evaluations,
        promotion_decisions=promotion_decisions,
        postmortems=postmortems,
        predecessor_strategies=predecessor_strategies,
    )
