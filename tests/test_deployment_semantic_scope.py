from __future__ import annotations

import copy
import hashlib
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.deployment_runtime_authority import (
    DeploymentRuntimeAuthorityError,
    DeploymentRuntimeAuthorityStore,
)
from autosport.deployment_semantic_scope import (
    ActionSemanticsDefinition,
    DeploymentSemanticScope,
    DeploymentSemanticScopeError,
    resolve_deployment_semantic_scope,
)
from autosport.domain import MarketEvent, MarketType
from autosport.learning_environment import EnvironmentIdentity, Episode
from autosport.paper_settlement_learning import REWARD_RULE
from autosport.scientific_registry import (
    DatasetSnapshot,
    FeatureSet,
    ResearchProtocol,
    ScientificRegistry,
)
from autosport.storage import SQLiteMarketStore
from autosport.strategy_experiment import ScientificProtocolBinding


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _protocol(
    *,
    dataset_manifest_sha256: str = _sha("dataset-manifest"),
    available_at: str = "2026-08-01T00:00:00Z",
    feature_set_version: str = "features-v1",
    causal_cutoff: str = "2026-09-01T00:00:00Z",
) -> ResearchProtocol:
    binding = ScientificProtocolBinding(
        research_protocol_id="protocol-v1",
        research_question_id="question-v1",
        research_question_sha256=_sha("question"),
        hypothesis_id="hypothesis-v1",
        hypothesis_sha256=_sha("hypothesis"),
        inclusion_criteria="causal pre-decision evidence only",
        exclusion_criteria="future or revised evidence excluded",
        lawful_source_requirements="lawful immutable source evidence",
        causal_cutoff=causal_cutoff,
        evaluation_design="walk-forward holdout",
        feature_set_version=feature_set_version,
        uncertainty_method="bootstrap-v1",
        multiple_comparison_control="holm-v1",
        robustness_checks=("regime-split",),
        random_seed_policy="fixed-seed-v1",
        stopping_rule="fixed-window-v1",
        promotion_rule="promotion-v1",
        expected_artifacts=("evaluation",),
        code_config_sha256=_sha("config"),
        frozen_at_utc="2026-08-01T00:00:00Z",
        feature_set_id="features-main",
        feature_definition_sha256=_sha("feature-definition"),
        feature_source_sha256=_sha("feature-source"),
        config_id="paper-agent-config-v1",
    )
    return ResearchProtocol(
        binding=binding,
        source_sha256=_sha("protocol-source"),
        environment_sha256=_sha("protocol-environment"),
        dataset_manifest_sha256=dataset_manifest_sha256,
        available_at_utc=available_at,
    )


def _feature_set(*, available_at: str = "2026-08-01T00:00:00Z") -> FeatureSet:
    return FeatureSet(
        feature_set_id="features-main",
        version="features-v1",
        definition_sha256=_sha("feature-definition"),
        source_sha256=_sha("feature-source"),
        available_at_utc=available_at,
    )


def _dataset(
    *,
    snapshot_id: str = "dataset-001",
    manifest_sha256: str = _sha("dataset-manifest"),
    cutoff: str = "2026-09-01T00:00:00Z",
    available_at: str | None = None,
    source_identity: str = "canonical-market-store",
) -> DatasetSnapshot:
    return DatasetSnapshot(
        dataset_snapshot_id=snapshot_id,
        manifest_sha256=manifest_sha256,
        source_identity=source_identity,
        license_identity="lawful-paper-source-v1",
        causal_cutoff=cutoff,
        available_at_utc=available_at or cutoff,
    )


def _environment(
    *,
    snapshot_id: str = "dataset-001",
    cutoff: str = "2026-09-01T00:00:00Z",
    source_id: str = "canonical-market-store",
) -> EnvironmentIdentity:
    return EnvironmentIdentity(
        source_id=source_id,
        config_id="paper-agent-config-v1",
        data_id=snapshot_id,
        protocol_id="protocol-v1",
        cutoff_ts=cutoff,
        seed=7,
    )


def _episode(environment: EnvironmentIdentity) -> Episode:
    return Episode(
        environment_id=environment.environment_id,
        episode_key="paper-session",
        policy_id="champion-policy-v1",
        admissible_actions=("NO_BET", "PAPER_PROPOSAL"),
    )


def _action_semantics() -> ActionSemanticsDefinition:
    return ActionSemanticsDefinition(
        version="paper-actions-v1",
        meanings=(
            ("NO_BET", "Abstain and create no paper ticket."),
            ("PAPER_PROPOSAL", "Create one bounded PAPER-only proposal; never move real money."),
        ),
    )


def _event(**overrides: object) -> MarketEvent:
    payload: dict[str, object] = {
        "event_id": "event-1",
        "market_id": "market-1",
        "selection_id": "selection-1",
        "decimal_odds": Decimal("2.10"),
        "observed_ts": "2026-08-31T23:59:00Z",
        "source_id": "paper-provider-a",
        "sequence": 1,
        "market_type": MarketType.WINNER,
        "ingest_ts": "2026-08-31T23:59:01Z",
        "sport": "soccer",
        "competition_id": "england/premier-league-v1",
        "market_semantics_id": "winner/full-time/1x2-v1",
        "provider_source_class": "exchange-v1",
    }
    payload.update(overrides)
    return MarketEvent(**payload)  # type: ignore[arg-type]


def _resolve(
    *,
    event: MarketEvent | None = None,
    dataset: DatasetSnapshot | None = None,
    feature: FeatureSet | None = None,
    protocol: ResearchProtocol | None = None,
    environment: EnvironmentIdentity | None = None,
    actions: ActionSemanticsDefinition | None = None,
    decision_ts: str = "2026-09-02T00:00:00Z",
    reward_definition_id: str = REWARD_RULE,
    feature_set_id: str | None = None,
    persist_event: bool = True,
    register_runtime: bool = True,
):
    event = event or _event()
    dataset = dataset or _dataset()
    feature = feature or _feature_set()
    protocol = protocol or _protocol()
    environment = environment or _environment(
        snapshot_id=dataset.dataset_snapshot_id,
        cutoff=dataset.causal_cutoff,
        source_id=dataset.source_identity,
    )
    actions = actions or _action_semantics()

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = ScientificRegistry.initialize_pristine(root / "scientific-registry.json")
        registry.append(dataset)
        registry.append(feature)
        registry.append(protocol)

        market_store = SQLiteMarketStore(root / "market.db")
        runtime_store = DeploymentRuntimeAuthorityStore.initialize_pristine(
            root / "runtime-authority.json"
        )
        try:
            if persist_event:
                market_store.append(event)
            if register_runtime:
                runtime = runtime_store.append(
                    environment=environment,
                    episode=_episode(environment),
                    action_semantics_version=actions.version,
                    action_semantics_meanings=actions.meanings,
                    available_at="2026-08-01T00:00:00Z",
                )
                runtime_authority_id = runtime.runtime_authority_id
            else:
                runtime_authority_id = _sha("missing-runtime-authority")

            return resolve_deployment_semantic_scope(
                market_store=market_store,
                market_event_dedupe_key=event.dedupe_key,
                scientific_registry=registry,
                dataset_snapshot_id=dataset.dataset_snapshot_id,
                feature_set_id=feature_set_id or feature.feature_set_id,
                research_protocol_id=protocol.record_id,
                runtime_authority_store=runtime_store,
                runtime_authority_id=runtime_authority_id,
                decision_ts=decision_ts,
                reward_definition_id=reward_definition_id,
            )
        finally:
            market_store.close()


def test_market_event_semantic_authority_round_trips_without_changing_quote_identity() -> None:
    event = _event()
    encoded = event.to_dict()
    restored = MarketEvent.from_dict(encoded)

    assert restored == event
    assert restored.quote_key == event.quote_key
    assert encoded["competition_id"] == "england/premier-league-v1"
    assert encoded["market_semantics_id"] == "winner/full-time/1x2-v1"
    assert encoded["provider_source_class"] == "exchange-v1"


def test_legacy_market_event_remains_readable_but_cannot_authorize_deployment() -> None:
    raw = _event().to_dict()
    raw.pop("competition_id")
    raw.pop("market_semantics_id")
    raw.pop("provider_source_class")
    legacy = MarketEvent.from_dict(raw)

    assert legacy.competition_id is None
    assert legacy.market_semantics_id is None
    assert legacy.provider_source_class is None
    with pytest.raises(DeploymentSemanticScopeError, match="competition authority"):
        _resolve(event=legacy)


def test_semantic_scope_is_restart_stable_and_payload_tamper_evident() -> None:
    authority = _resolve()
    restored_scope = DeploymentSemanticScope.from_dict(copy.deepcopy(authority.scope.to_dict()))
    restored_actions = ActionSemanticsDefinition.from_dict(_action_semantics().to_dict())

    assert restored_scope == authority.scope
    assert restored_scope.scope_id == authority.scope.scope_id
    assert restored_actions.action_semantics_id == _action_semantics().action_semantics_id

    tampered = authority.scope.to_dict()
    tampered["competition_scope"] = "spain/la-liga-v1"
    with pytest.raises(DeploymentSemanticScopeError, match="scope id mismatch"):
        DeploymentSemanticScope.from_dict(tampered)


def test_durable_authorities_reresolve_identically_across_restart() -> None:
    dataset = _dataset()
    feature = _feature_set()
    protocol = _protocol()
    environment = _environment()
    episode = _episode(environment)
    actions = _action_semantics()
    event = _event()

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry_path = root / "scientific-registry.json"
        market_path = root / "market.db"
        runtime_path = root / "runtime-authority.json"

        registry = ScientificRegistry.initialize_pristine(registry_path)
        registry.append(dataset)
        registry.append(feature)
        registry.append(protocol)
        market_store = SQLiteMarketStore(market_path)
        market_store.append(event)
        runtime_store = DeploymentRuntimeAuthorityStore.initialize_pristine(runtime_path)
        runtime = runtime_store.append(
            environment=environment,
            episode=episode,
            action_semantics_version=actions.version,
            action_semantics_meanings=actions.meanings,
            available_at="2026-08-01T00:00:00Z",
        )
        first = resolve_deployment_semantic_scope(
            market_store=market_store,
            market_event_dedupe_key=event.dedupe_key,
            scientific_registry=registry,
            dataset_snapshot_id=dataset.dataset_snapshot_id,
            feature_set_id=feature.feature_set_id,
            research_protocol_id=protocol.record_id,
            runtime_authority_store=runtime_store,
            runtime_authority_id=runtime.runtime_authority_id,
            decision_ts="2026-09-02T00:00:00Z",
        )
        market_store.close()

        reopened_market = SQLiteMarketStore(market_path)
        try:
            second = resolve_deployment_semantic_scope(
                market_store=reopened_market,
                market_event_dedupe_key=event.dedupe_key,
                scientific_registry=ScientificRegistry(registry_path),
                dataset_snapshot_id=dataset.dataset_snapshot_id,
                feature_set_id=feature.feature_set_id,
                research_protocol_id=protocol.record_id,
                runtime_authority_store=DeploymentRuntimeAuthorityStore(runtime_path),
                runtime_authority_id=runtime.runtime_authority_id,
                decision_ts="2026-09-02T00:00:00Z",
            )
        finally:
            reopened_market.close()

    assert second == first
    assert second.scope.scope_id == first.scope.scope_id
    assert second.authority_id == first.authority_id


def test_later_append_only_snapshot_keeps_scope_but_changes_exact_authority() -> None:
    first = _resolve()
    later_dataset = _dataset(
        snapshot_id="dataset-002",
        manifest_sha256=_sha("dataset-manifest-2"),
        cutoff="2026-09-08T00:00:00Z",
    )
    later_environment = _environment(snapshot_id="dataset-002", cutoff="2026-09-08T00:00:00Z")
    later_event = _event(
        observed_ts="2026-09-07T23:59:00Z",
        ingest_ts="2026-09-07T23:59:01Z",
        sequence=2,
    )
    later = _resolve(
        event=later_event,
        dataset=later_dataset,
        protocol=_protocol(
            dataset_manifest_sha256=later_dataset.manifest_sha256,
            causal_cutoff=later_dataset.causal_cutoff,
        ),
        environment=later_environment,
        decision_ts="2026-09-09T00:00:00Z",
    )

    assert later.scope.scope_id == first.scope.scope_id
    assert later.authority_id != first.authority_id
    assert later.dataset_snapshot_id != first.dataset_snapshot_id


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"sport": "tennis"}, "sport_domain"),
        ({"competition_id": "spain/la-liga-v1"}, "competition_scope"),
        ({"market_semantics_id": "winner/first-half/1x2-v1"}, "market_semantics_id"),
        ({"provider_source_class": "sportsbook-v1"}, "provider_source_class"),
    ],
)
def test_runtime_semantic_relabel_changes_compatibility_scope(
    overrides: dict[str, object], field: str
) -> None:
    baseline = _resolve()
    changed = _resolve(event=_event(**overrides))

    assert getattr(changed.scope, field) != getattr(baseline.scope, field)
    assert changed.scope.scope_id != baseline.scope.scope_id


def test_unpersisted_caller_market_event_cannot_mint_authority() -> None:
    event = _event()
    with pytest.raises(DeploymentSemanticScopeError, match="canonical market-event history lacks"):
        _resolve(event=event, persist_event=False)


def test_unregistered_runtime_identity_cannot_mint_authority() -> None:
    with pytest.raises(DeploymentSemanticScopeError, match="canonical runtime authority store lacks"):
        _resolve(register_runtime=False)


def test_action_meaning_change_changes_scope_even_when_action_names_do_not() -> None:
    baseline = _resolve()
    changed_actions = ActionSemanticsDefinition(
        version="paper-actions-v1",
        meanings=(
            ("NO_BET", "Abstain and create no paper ticket."),
            ("PAPER_PROPOSAL", "Changed meaning that would silently alter the action contract."),
        ),
    )
    changed = _resolve(actions=changed_actions)

    assert changed.scope.action_semantics_id != baseline.scope.action_semantics_id
    assert changed.scope.scope_id != baseline.scope.scope_id


def test_action_semantics_must_cover_exact_admissible_action_universe() -> None:
    actions = ActionSemanticsDefinition(
        version="paper-actions-v1",
        meanings=(("NO_BET", "Abstain and create no paper ticket."),),
    )
    with pytest.raises(
        DeploymentRuntimeAuthorityError,
        match="exact episode admissible action universe",
    ):
        _resolve(actions=actions)


def test_reward_rule_cannot_be_silently_redefined() -> None:
    with pytest.raises(DeploymentSemanticScopeError, match="canonical settlement reward rule"):
        _resolve(reward_definition_id="caller-invented-reward-v2")
    assert REWARD_RULE == "paper-net-payout-minus-stake-v1"


def test_environment_must_bind_exact_snapshot_protocol_and_cutoff() -> None:
    dataset = _dataset()
    environment = _environment(snapshot_id="other-snapshot")
    with pytest.raises(DeploymentSemanticScopeError, match="data identity"):
        _resolve(dataset=dataset, environment=environment)

    bad_cutoff = _environment(cutoff="2026-09-02T00:00:00Z")
    with pytest.raises(DeploymentSemanticScopeError, match="cutoff"):
        _resolve(dataset=dataset, environment=bad_cutoff)


def test_future_event_beyond_causal_cutoff_is_rejected() -> None:
    with pytest.raises(DeploymentSemanticScopeError, match="later than the causal dataset cutoff"):
        _resolve(
            event=_event(
                observed_ts="2026-09-01T00:00:01Z",
                ingest_ts="2026-09-01T00:00:01Z",
            )
        )


def test_late_ingested_event_with_old_observed_time_is_rejected() -> None:
    with pytest.raises(DeploymentSemanticScopeError, match="ingested after the causal dataset cutoff"):
        _resolve(
            event=_event(
                observed_ts="2026-08-31T23:59:00Z",
                ingest_ts="2026-09-01T00:00:01Z",
            )
        )


def test_semantic_identity_fields_reject_noncanonical_or_reserved_values() -> None:
    with pytest.raises(ValueError, match="lowercase"):
        _event(competition_id="PremierLeague")
    with pytest.raises(ValueError, match="reserved"):
        _event(provider_source_class="unknown")
    with pytest.raises(ValueError, match="must use lowercase ASCII"):
        _event(market_semantics_id="winner full time")


def test_scientific_records_are_reloaded_from_durable_registry_by_identity() -> None:
    canonical_dataset = _dataset(source_identity="canonical-market-store")
    environment = _environment(source_id="canonical-market-store")
    fabricated = _dataset(source_identity="caller-fabricated-source")
    assert fabricated.dataset_snapshot_id == canonical_dataset.dataset_snapshot_id

    authority = _resolve(dataset=canonical_dataset, environment=environment)

    assert authority.scope.dataset_source_identity == "canonical-market-store"
    assert authority.scope.dataset_source_identity != fabricated.source_identity


def test_missing_canonical_scientific_identity_fails_closed() -> None:
    with pytest.raises(DeploymentSemanticScopeError, match="lacks FeatureSet:missing-features"):
        _resolve(feature_set_id="missing-features")


@pytest.mark.parametrize(
    ("dataset", "feature", "protocol", "message"),
    [
        (
            _dataset(available_at="2026-09-03T00:00:00Z"),
            _feature_set(),
            _protocol(),
            "DatasetSnapshot:dataset-001 was not available at decision time",
        ),
        (
            _dataset(),
            _feature_set(available_at="2026-09-03T00:00:00Z"),
            _protocol(),
            "FeatureSet:features-main was not available at decision time",
        ),
        (
            _dataset(),
            _feature_set(),
            _protocol(available_at="2026-09-03T00:00:00Z"),
            "ResearchProtocol:protocol-v1 was not available at decision time",
        ),
    ],
)
def test_scientific_evidence_available_only_after_decision_is_rejected(
    dataset: DatasetSnapshot,
    feature: FeatureSet,
    protocol: ResearchProtocol,
    message: str,
) -> None:
    with pytest.raises(DeploymentSemanticScopeError, match=message):
        _resolve(
            dataset=dataset,
            feature=feature,
            protocol=protocol,
            decision_ts="2026-09-02T00:00:00Z",
        )


def test_dataset_cutoff_cannot_be_after_decision() -> None:
    dataset = _dataset(
        cutoff="2026-09-03T00:00:00Z",
        available_at="2026-09-02T00:00:00Z",
    )
    environment = _environment(cutoff="2026-09-03T00:00:00Z")
    with pytest.raises(DeploymentSemanticScopeError, match="causal cutoff is later than decision time"):
        _resolve(
            dataset=dataset,
            environment=environment,
            decision_ts="2026-09-02T00:00:00Z",
        )
