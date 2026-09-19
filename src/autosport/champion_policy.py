"""Durable activation seam for scientifically promoted transparent policies.

Policy artifacts are immutable evidence only.  ScientificRegistry promotion history
remains the sole authority that decides which artifact may be activated, and the
returned policy can choose only from an externally supplied admissible action set.
"""

from __future__ import annotations

from typing import Final

from .scientific_registry import ScientificRegistry
from .strategy_model_factory import FactoryArtifactStore
from .transparent_bandit_policy import BanditPolicyState


POLICY_ARTIFACT_KIND: Final = "transparent-bandit-policy"
POLICY_ARTIFACT_SCHEMA: Final = "autosport.transparent_bandit_policy_artifact"
POLICY_ARTIFACT_SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")


class ChampionPolicyError(RuntimeError):
    """Champion policy evidence is missing, incompatible, or corrupted."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ChampionPolicyError(f"{name} must be canonical non-empty text")
    value.encode("utf-8", errors="strict")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ChampionPolicyError(f"{name} must be lowercase SHA-256")
    return text


def _artifact(policy: BanditPolicyState) -> dict[str, object]:
    return {
        "schema": POLICY_ARTIFACT_SCHEMA,
        "schema_version": POLICY_ARTIFACT_SCHEMA_VERSION,
        "policy_id": policy.policy_id,
        "policy": policy.to_payload(),
    }


def persist_policy_state(
    artifact_store: FactoryArtifactStore,
    policy: BanditPolicyState,
) -> str:
    """Persist immutable policy evidence; this grants no activation authority."""

    if not isinstance(artifact_store, FactoryArtifactStore):
        raise TypeError("artifact_store must be FactoryArtifactStore")
    if not isinstance(policy, BanditPolicyState):
        raise TypeError("policy must be BanditPolicyState")
    return artifact_store.write(POLICY_ARTIFACT_KIND, policy.policy_id, _artifact(policy))


def load_champion_policy(
    registry: ScientificRegistry,
    artifact_store: FactoryArtifactStore,
    *,
    as_of: str,
    canonical_strategy_id: str,
    environment_id: str,
    protocol_id: str,
    config_sha256: str,
    admissible_actions: frozenset[str],
) -> BanditPolicyState:
    """Load the exact promoted policy for one compatible next episode."""

    if not isinstance(registry, ScientificRegistry):
        raise TypeError("registry must be ScientificRegistry")
    if not isinstance(artifact_store, FactoryArtifactStore):
        raise TypeError("artifact_store must be FactoryArtifactStore")
    strategy_key = _text(canonical_strategy_id, "canonical_strategy_id")
    expected_environment = _sha256(environment_id, "environment_id")
    expected_protocol = _text(protocol_id, "protocol_id")
    expected_config = _sha256(config_sha256, "config_sha256")
    if type(admissible_actions) is not frozenset or not admissible_actions:
        raise ChampionPolicyError("admissible_actions must be a non-empty frozenset")
    expected_actions = frozenset(
        _text(action, "admissible action") for action in admissible_actions
    )

    champion_id = registry.champion_strategy(
        as_of=as_of,
        canonical_strategy_id=strategy_key,
    )
    if champion_id is None:
        raise ChampionPolicyError("no promoted champion is available at as_of")
    _sha256(champion_id, "champion policy_id")

    strategy = registry.get("StrategyVersion", champion_id)
    if strategy is None:
        raise ChampionPolicyError("promoted champion StrategyVersion is missing")
    strategy_payload = strategy.payload
    if (
        strategy_payload.get("strategy_version_id") != champion_id
        or strategy_payload.get("canonical_strategy_id") != strategy_key
    ):
        raise ChampionPolicyError("champion StrategyVersion identity mismatch")
    if strategy_payload.get("environment_sha256") != expected_environment:
        raise ChampionPolicyError("champion strategy environment mismatch")
    if strategy_payload.get("config_sha256") != expected_config:
        raise ChampionPolicyError("champion strategy config mismatch")
    model_id = strategy_payload.get("model_version_id")
    if type(model_id) is not str or not model_id:
        raise ChampionPolicyError("champion strategy model identity is missing")
    model = registry.get("ModelVersion", model_id)
    if model is None:
        raise ChampionPolicyError("champion ModelVersion is missing")
    model_payload = model.payload
    if (
        model_payload.get("model_version_id") != model_id
        or model_payload.get("environment_sha256") != expected_environment
        or model_payload.get("config_sha256") != expected_config
        or model_payload.get("research_protocol_id") != expected_protocol
    ):
        raise ChampionPolicyError("champion model lineage is incompatible")

    try:
        artifact = artifact_store.read(POLICY_ARTIFACT_KIND, champion_id)
    except ValueError as exc:
        raise ChampionPolicyError("champion policy artifact is unavailable") from exc
    if type(artifact) is not dict or set(artifact) != {
        "schema",
        "schema_version",
        "policy_id",
        "policy",
    }:
        raise ChampionPolicyError("champion policy artifact fields mismatch")
    if (
        artifact["schema"] != POLICY_ARTIFACT_SCHEMA
        or type(artifact["schema_version"]) is not int
        or artifact["schema_version"] != POLICY_ARTIFACT_SCHEMA_VERSION
        or artifact["policy_id"] != champion_id
    ):
        raise ChampionPolicyError("champion policy artifact identity mismatch")
    try:
        policy = BanditPolicyState.from_payload(artifact["policy"])
    except (TypeError, ValueError) as exc:
        raise ChampionPolicyError("champion policy payload is invalid") from exc
    if policy.policy_id != champion_id:
        raise ChampionPolicyError("champion policy payload hash mismatch")
    if policy.environment_id != expected_environment:
        raise ChampionPolicyError("champion policy environment mismatch")
    if policy.protocol_id != expected_protocol:
        raise ChampionPolicyError("champion policy protocol mismatch")
    if policy.config_sha256 != expected_config:
        raise ChampionPolicyError("champion policy config mismatch")
    policy_actions = frozenset(item.action_type for item in policy.estimates)
    if policy_actions != expected_actions:
        raise ChampionPolicyError("champion policy action universe mismatch")
    return policy


__all__ = [
    "ChampionPolicyError",
    "POLICY_ARTIFACT_KIND",
    "load_champion_policy",
    "persist_policy_state",
]
