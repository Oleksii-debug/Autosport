import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosport.champion_policy import (
    ChampionPolicyError,
    POLICY_ARTIFACT_KIND,
    load_champion_policy,
    persist_policy_state,
)
from autosport.learning_environment import (
    Action,
    EvidenceTruth,
    RewardEvidence,
    Transition,
)
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore
from autosport.transparent_bandit_policy import BanditPolicyState


ENVIRONMENT_ID = "d" * 64
CONFIG_SHA256 = "c" * 64
PROTOCOL_ID = "protocol-champion-policy-v1"
STRATEGY_ID = "canonical-transparent-bandit"
MODEL_ID = "model-transparent-bandit-v2"
PROMOTED_AT = "2026-09-19T13:10:00Z"


def _policy_successor():
    predecessor = BanditPolicyState.initial(
        environment_id=ENVIRONMENT_ID,
        protocol_id=PROTOCOL_ID,
        config_sha256=CONFIG_SHA256,
        seed=17,
        action_types=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    action = Action(
        environment_id=ENVIRONMENT_ID,
        observation_id="1" * 64,
        action_type="WAIT",
        decided_at="2026-09-19T13:00:00Z",
    )
    reward = RewardEvidence(
        environment_id=ENVIRONMENT_ID,
        action_id=action.action_id,
        outcome_id="2" * 64,
        reward=Decimal("1.25"),
        available_at="2026-09-19T13:05:00Z",
        truth=EvidenceTruth.OBSERVED,
    )
    transition = Transition(
        environment_id=ENVIRONMENT_ID,
        episode_id="3" * 64,
        step_index=1,
        observation_id=action.observation_id,
        action_id=action.action_id,
        outcome_id=reward.outcome_id,
        reward_id=reward.reward_id,
        decision_at=action.decided_at,
        resolved_at=reward.available_at,
    )
    successor, evidence = predecessor.update(
        action=action,
        reward=reward,
        transition=transition,
    )
    return predecessor, successor, evidence


def _registry_entries(policy: BanditPolicyState):
    strategy = SimpleNamespace(
        payload={
            "strategy_version_id": policy.policy_id,
            "canonical_strategy_id": STRATEGY_ID,
            "environment_sha256": policy.environment_id,
            "config_sha256": policy.config_sha256,
            "model_version_id": MODEL_ID,
        }
    )
    model = SimpleNamespace(
        payload={
            "model_version_id": MODEL_ID,
            "environment_sha256": policy.environment_id,
            "config_sha256": policy.config_sha256,
            "research_protocol_id": policy.protocol_id,
        }
    )
    return strategy, model


def _load_with_authority(registry, store, policy, *, as_of=PROMOTED_AT, **overrides):
    strategy, model = _registry_entries(policy)

    def get(_self, kind, record_id):
        if kind == "StrategyVersion" and record_id == policy.policy_id:
            return strategy
        if kind == "ModelVersion" and record_id == MODEL_ID:
            return model
        return None

    arguments = {
        "as_of": as_of,
        "canonical_strategy_id": STRATEGY_ID,
        "environment_id": ENVIRONMENT_ID,
        "protocol_id": PROTOCOL_ID,
        "config_sha256": CONFIG_SHA256,
        "admissible_actions": frozenset({"PAPER_PROPOSAL", "WAIT"}),
    }
    arguments.update(overrides)
    with (
        patch.object(
            ScientificRegistry,
            "champion_strategy",
            autospec=True,
            return_value=policy.policy_id,
        ),
        patch.object(ScientificRegistry, "get", autospec=True, side_effect=get),
    ):
        return load_champion_policy(registry, store, **arguments)


def test_policy_payload_round_trip_preserves_exact_identity_and_decimal():
    _, successor, _ = _policy_successor()

    restored = BanditPolicyState.from_payload(successor.to_payload())

    assert restored == successor
    assert restored.policy_id == successor.policy_id
    assert restored.estimates[-1].reward_sum == Decimal("1.25")


@pytest.mark.parametrize("reward_text", ["NaN", "Infinity", "01.25"])
def test_policy_payload_rejects_nonfinite_or_noncanonical_decimal(reward_text):
    _, successor, _ = _policy_successor()
    payload = successor.to_payload()
    payload["estimates"][-1]["reward_sum"] = reward_text

    with pytest.raises(ValueError, match="finite canonical Decimal"):
        BanditPolicyState.from_payload(payload)


def test_promoted_policy_restarts_and_changes_next_episode_choice(tmp_path):
    predecessor, successor, _ = _policy_successor()
    registry_path = tmp_path / "scientific-registry.json"
    artifact_root = tmp_path / "factory-artifacts"
    ScientificRegistry.initialize_pristine(registry_path)
    store = FactoryArtifactStore(artifact_root)
    first_sha = persist_policy_state(store, successor)
    assert persist_policy_state(store, successor) == first_sha

    reopened_registry = ScientificRegistry(registry_path)
    reopened_store = FactoryArtifactStore(artifact_root)
    activated = _load_with_authority(
        reopened_registry,
        reopened_store,
        successor,
    )

    actions = frozenset({"PAPER_PROPOSAL", "WAIT"})
    assert predecessor.choose(admissible_actions=actions) == "PAPER_PROPOSAL"
    assert activated.choose(admissible_actions=actions) == "WAIT"
    assert activated.policy_id == successor.policy_id


def test_policy_artifact_without_promotion_cannot_activate_rejected_challenger(tmp_path):
    predecessor, rejected, _ = _policy_successor()
    registry = ScientificRegistry.initialize_pristine(tmp_path / "registry.json")
    store = FactoryArtifactStore(tmp_path / "artifacts")
    persist_policy_state(store, predecessor)
    persist_policy_state(store, rejected)

    activated = _load_with_authority(registry, store, predecessor)

    assert activated.policy_id == predecessor.policy_id
    assert activated.policy_id != rejected.policy_id


def test_future_or_missing_champion_fails_closed(tmp_path):
    _, successor, _ = _policy_successor()
    registry = ScientificRegistry.initialize_pristine(tmp_path / "registry.json")
    store = FactoryArtifactStore(tmp_path / "artifacts")
    persist_policy_state(store, successor)

    with patch.object(
        ScientificRegistry,
        "champion_strategy",
        autospec=True,
        return_value=None,
    ):
        with pytest.raises(ChampionPolicyError, match="no promoted champion"):
            load_champion_policy(
                registry,
                store,
                as_of="2026-09-19T13:09:59Z",
                canonical_strategy_id=STRATEGY_ID,
                environment_id=ENVIRONMENT_ID,
                protocol_id=PROTOCOL_ID,
                config_sha256=CONFIG_SHA256,
                admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
            )


def test_missing_or_tampered_champion_artifact_fails_closed(tmp_path):
    _, successor, _ = _policy_successor()
    registry = ScientificRegistry.initialize_pristine(tmp_path / "registry.json")
    store = FactoryArtifactStore(tmp_path / "artifacts")
    strategy, model = _registry_entries(successor)

    def get(_self, kind, _record_id):
        return strategy if kind == "StrategyVersion" else model

    with (
        patch.object(
            ScientificRegistry,
            "champion_strategy",
            autospec=True,
            return_value=successor.policy_id,
        ),
        patch.object(ScientificRegistry, "get", autospec=True, side_effect=get),
    ):
        with pytest.raises(ChampionPolicyError, match="artifact is unavailable"):
            load_champion_policy(
                registry,
                store,
                as_of=PROMOTED_AT,
                canonical_strategy_id=STRATEGY_ID,
                environment_id=ENVIRONMENT_ID,
                protocol_id=PROTOCOL_ID,
                config_sha256=CONFIG_SHA256,
                admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
            )

    persist_policy_state(store, successor)
    path = store.path_for_testing(POLICY_ARTIFACT_KIND, successor.policy_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["policy"]["estimates"][-1]["reward_sum"] = "9.25"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ChampionPolicyError, match="payload hash mismatch"):
        _load_with_authority(registry, store, successor)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"environment_id": "e" * 64}, "environment mismatch"),
        ({"config_sha256": "f" * 64}, "config mismatch"),
        ({"protocol_id": "protocol-other"}, "model lineage is incompatible"),
        (
            {"admissible_actions": frozenset({"WAIT"})},
            "action universe mismatch",
        ),
    ],
)
def test_champion_activation_requires_exact_next_episode_context(
    tmp_path,
    override,
    message,
):
    _, successor, _ = _policy_successor()
    registry = ScientificRegistry.initialize_pristine(tmp_path / "registry.json")
    store = FactoryArtifactStore(tmp_path / "artifacts")
    persist_policy_state(store, successor)

    with pytest.raises(ChampionPolicyError, match=message):
        _load_with_authority(registry, store, successor, **override)
