"""Immutable semantic compatibility authority for cross-session PAPER deployment.

This module deliberately does not create a second deployment policy. It resolves one
stable compatibility scope from canonical runtime/scientific evidence so callers cannot
make unrelated strings agree by convention. Exact data/cutoff evidence remains in the
authority witness while ``scope_id`` excludes the monotonic snapshot/cutoff dimensions
that #596 permits to advance across later sessions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Mapping

from .domain import MarketEvent
from .learning_environment import EnvironmentIdentity, Episode
from .paper_settlement_learning import REWARD_RULE
from .scientific_registry import DatasetSnapshot, FeatureSet, ResearchProtocol


SCHEMA: Final = "autosport.deployment_semantic_scope"
SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")


class DeploymentSemanticScopeError(ValueError):
    """Canonical deployment semantic authority is incomplete or inconsistent."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise DeploymentSemanticScopeError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise DeploymentSemanticScopeError(f"{name} must be valid UTF-8") from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise DeploymentSemanticScopeError(f"{name} must be canonical SHA-256 hex")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeploymentSemanticScopeError(f"{name} must be timezone-aware ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeploymentSemanticScopeError(f"{name} must be timezone-aware ISO-8601")
    return parsed.astimezone(timezone.utc)


def _instant_id(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DeploymentSemanticScopeError("semantic scope payload is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ActionSemanticsDefinition:
    """Versioned meanings for the externally admissible learning actions."""

    version: str
    meanings: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _text(self.version, "action semantics version")
        if type(self.meanings) is not tuple or not self.meanings:
            raise DeploymentSemanticScopeError("action semantics meanings must be non-empty tuple")
        normalized: list[tuple[str, str]] = []
        for index, entry in enumerate(self.meanings):
            if type(entry) is not tuple or len(entry) != 2:
                raise DeploymentSemanticScopeError(
                    f"action semantics meanings[{index}] must be a two-item tuple"
                )
            action_type = _text(entry[0], f"action semantics action[{index}]")
            meaning = _text(entry[1], f"action semantics meaning[{index}]")
            normalized.append((action_type, meaning))
        if tuple(normalized) != tuple(sorted(normalized)):
            raise DeploymentSemanticScopeError("action semantics meanings must be sorted")
        action_types = tuple(action for action, _ in normalized)
        if len(action_types) != len(set(action_types)):
            raise DeploymentSemanticScopeError("action semantics action names must be unique")

    @property
    def definition_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.action_semantics",
                "schema_version": 1,
                "version": self.version,
                "meanings": [[action, meaning] for action, meaning in self.meanings],
            }
        )

    @property
    def action_semantics_id(self) -> str:
        return _digest(
            {
                "schema": "autosport.action_semantics.identity",
                "schema_version": 1,
                "version": self.version,
                "definition_sha256": self.definition_sha256,
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "meanings": [[action, meaning] for action, meaning in self.meanings],
            "definition_sha256": self.definition_sha256,
            "action_semantics_id": self.action_semantics_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ActionSemanticsDefinition":
        if not isinstance(raw, Mapping):
            raise DeploymentSemanticScopeError("action semantics payload must be a mapping")
        raw_meanings = raw.get("meanings")
        if type(raw_meanings) is not list:
            raise DeploymentSemanticScopeError("action semantics meanings payload must be a list")
        meanings: list[tuple[str, str]] = []
        for index, entry in enumerate(raw_meanings):
            if type(entry) is not list or len(entry) != 2:
                raise DeploymentSemanticScopeError(
                    f"action semantics meanings[{index}] payload must have two items"
                )
            meanings.append((_text(entry[0], "action type"), _text(entry[1], "action meaning")))
        result = cls(
            version=_text(raw.get("version"), "action semantics version"),
            meanings=tuple(meanings),
        )
        if raw.get("definition_sha256") is not None and _sha(
            raw.get("definition_sha256"), "definition_sha256"
        ) != result.definition_sha256:
            raise DeploymentSemanticScopeError("action semantics definition digest mismatch")
        if raw.get("action_semantics_id") is not None and _sha(
            raw.get("action_semantics_id"), "action_semantics_id"
        ) != result.action_semantics_id:
            raise DeploymentSemanticScopeError("action semantics identity mismatch")
        return result


@dataclass(frozen=True, slots=True)
class DeploymentSemanticScope:
    """Compatibility dimensions that must remain exact across later sessions."""

    sport_domain: str
    competition_scope: str
    market_semantics_id: str
    provider_source_class: str
    dataset_source_identity: str
    dataset_license_identity: str
    feature_set_id: str
    feature_set_version: str
    feature_definition_sha256: str
    feature_source_sha256: str
    research_protocol_id: str
    research_protocol_sha256: str
    config_id: str
    config_sha256: str
    action_semantics_id: str
    action_semantics_definition_sha256: str
    reward_definition_id: str
    schema: str = SCHEMA
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != SCHEMA or self.schema_version != SCHEMA_VERSION:
            raise DeploymentSemanticScopeError("unsupported deployment semantic scope schema")
        for name in (
            "sport_domain",
            "competition_scope",
            "market_semantics_id",
            "provider_source_class",
            "dataset_source_identity",
            "dataset_license_identity",
            "feature_set_id",
            "feature_set_version",
            "research_protocol_id",
            "config_id",
            "reward_definition_id",
        ):
            _text(getattr(self, name), name)
        for name in (
            "feature_definition_sha256",
            "feature_source_sha256",
            "research_protocol_sha256",
            "config_sha256",
            "action_semantics_id",
            "action_semantics_definition_sha256",
        ):
            _sha(getattr(self, name), name)

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "sport_domain": self.sport_domain,
            "competition_scope": self.competition_scope,
            "market_semantics_id": self.market_semantics_id,
            "provider_source_class": self.provider_source_class,
            "dataset_source_identity": self.dataset_source_identity,
            "dataset_license_identity": self.dataset_license_identity,
            "feature_set_id": self.feature_set_id,
            "feature_set_version": self.feature_set_version,
            "feature_definition_sha256": self.feature_definition_sha256,
            "feature_source_sha256": self.feature_source_sha256,
            "research_protocol_id": self.research_protocol_id,
            "research_protocol_sha256": self.research_protocol_sha256,
            "config_id": self.config_id,
            "config_sha256": self.config_sha256,
            "action_semantics_id": self.action_semantics_id,
            "action_semantics_definition_sha256": self.action_semantics_definition_sha256,
            "reward_definition_id": self.reward_definition_id,
        }

    @property
    def scope_id(self) -> str:
        return _digest(self.identity_payload())

    def to_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "scope_id": self.scope_id}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "DeploymentSemanticScope":
        if not isinstance(raw, Mapping):
            raise DeploymentSemanticScopeError("deployment semantic scope payload must be a mapping")
        schema_version = raw.get("schema_version", SCHEMA_VERSION)
        if type(schema_version) is not int:
            raise DeploymentSemanticScopeError("schema_version must be an integer")
        result = cls(
            sport_domain=_text(raw.get("sport_domain"), "sport_domain"),
            competition_scope=_text(raw.get("competition_scope"), "competition_scope"),
            market_semantics_id=_text(raw.get("market_semantics_id"), "market_semantics_id"),
            provider_source_class=_text(raw.get("provider_source_class"), "provider_source_class"),
            dataset_source_identity=_text(raw.get("dataset_source_identity"), "dataset_source_identity"),
            dataset_license_identity=_text(raw.get("dataset_license_identity"), "dataset_license_identity"),
            feature_set_id=_text(raw.get("feature_set_id"), "feature_set_id"),
            feature_set_version=_text(raw.get("feature_set_version"), "feature_set_version"),
            feature_definition_sha256=_sha(raw.get("feature_definition_sha256"), "feature_definition_sha256"),
            feature_source_sha256=_sha(raw.get("feature_source_sha256"), "feature_source_sha256"),
            research_protocol_id=_text(raw.get("research_protocol_id"), "research_protocol_id"),
            research_protocol_sha256=_sha(raw.get("research_protocol_sha256"), "research_protocol_sha256"),
            config_id=_text(raw.get("config_id"), "config_id"),
            config_sha256=_sha(raw.get("config_sha256"), "config_sha256"),
            action_semantics_id=_sha(raw.get("action_semantics_id"), "action_semantics_id"),
            action_semantics_definition_sha256=_sha(
                raw.get("action_semantics_definition_sha256"),
                "action_semantics_definition_sha256",
            ),
            reward_definition_id=_text(raw.get("reward_definition_id"), "reward_definition_id"),
            schema=_text(raw.get("schema", SCHEMA), "schema"),
            schema_version=schema_version,
        )
        if raw.get("scope_id") is not None and _sha(raw.get("scope_id"), "scope_id") != result.scope_id:
            raise DeploymentSemanticScopeError("deployment semantic scope id mismatch")
        return result


@dataclass(frozen=True, slots=True)
class DeploymentSemanticAuthority:
    """Exact causal/scientific witness used to resolve one compatibility scope."""

    scope: DeploymentSemanticScope
    event_quote_key: str
    provider_source_id: str
    event_observed_ts: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    dataset_causal_cutoff: str
    dataset_available_at: str
    feature_available_at: str
    protocol_available_at: str
    environment_id: str
    episode_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, DeploymentSemanticScope):
            raise DeploymentSemanticScopeError("scope must be DeploymentSemanticScope")
        for name in ("event_quote_key", "provider_source_id", "dataset_snapshot_id"):
            _text(getattr(self, name), name)
        for name in ("dataset_manifest_sha256", "environment_id", "episode_id"):
            _sha(getattr(self, name), name)
        for name in (
            "event_observed_ts",
            "dataset_causal_cutoff",
            "dataset_available_at",
            "feature_available_at",
            "protocol_available_at",
        ):
            _instant(getattr(self, name), name)

    @property
    def authority_id(self) -> str:
        return _digest(
            {
                "schema": "autosport.deployment_semantic_scope.authority",
                "schema_version": 1,
                "scope_id": self.scope.scope_id,
                "event_quote_key": self.event_quote_key,
                "provider_source_id": self.provider_source_id,
                "event_observed_ts": _instant_id(self.event_observed_ts, "event_observed_ts"),
                "dataset_snapshot_id": self.dataset_snapshot_id,
                "dataset_manifest_sha256": self.dataset_manifest_sha256,
                "dataset_causal_cutoff": _instant_id(
                    self.dataset_causal_cutoff, "dataset_causal_cutoff"
                ),
                "dataset_available_at": _instant_id(
                    self.dataset_available_at, "dataset_available_at"
                ),
                "feature_available_at": _instant_id(
                    self.feature_available_at, "feature_available_at"
                ),
                "protocol_available_at": _instant_id(
                    self.protocol_available_at, "protocol_available_at"
                ),
                "environment_id": self.environment_id,
                "episode_id": self.episode_id,
            }
        )


def resolve_deployment_semantic_scope(
    *,
    event: MarketEvent,
    dataset_snapshot: DatasetSnapshot,
    feature_set: FeatureSet,
    research_protocol: ResearchProtocol,
    environment: EnvironmentIdentity,
    episode: Episode,
    action_semantics: ActionSemanticsDefinition,
    reward_definition_id: str = REWARD_RULE,
) -> DeploymentSemanticAuthority:
    """Resolve a fail-closed semantic authority from canonical evidence.

    The frozen research protocol is training/scientific authority. Its training dataset
    manifest is intentionally not required to equal the later deployment snapshot: that
    would make monotonic cross-session data advancement impossible. The deployment
    snapshot must instead match the current EnvironmentIdentity source/data/cutoff, and
    its exact manifest is retained in ``authority_id`` rather than ``scope_id``.
    """

    for expected_type, value, name in (
        (MarketEvent, event, "event"),
        (DatasetSnapshot, dataset_snapshot, "dataset_snapshot"),
        (FeatureSet, feature_set, "feature_set"),
        (ResearchProtocol, research_protocol, "research_protocol"),
        (EnvironmentIdentity, environment, "environment"),
        (Episode, episode, "episode"),
        (ActionSemanticsDefinition, action_semantics, "action_semantics"),
    ):
        if not isinstance(value, expected_type):
            raise DeploymentSemanticScopeError(f"{name} has wrong authority type")

    if event.sport is None:
        raise DeploymentSemanticScopeError("event lacks canonical sport authority")
    if event.competition_id is None:
        raise DeploymentSemanticScopeError("event lacks canonical competition authority")
    if event.market_semantics_id is None:
        raise DeploymentSemanticScopeError("event lacks versioned market semantics authority")
    if event.provider_source_class is None:
        raise DeploymentSemanticScopeError("event lacks provider/source class authority")

    if environment.source_id != dataset_snapshot.source_identity:
        raise DeploymentSemanticScopeError("environment source identity does not match dataset source")
    if environment.data_id != dataset_snapshot.dataset_snapshot_id:
        raise DeploymentSemanticScopeError("environment data identity does not match dataset snapshot")
    if _instant_id(environment.cutoff_ts, "environment cutoff") != _instant_id(
        dataset_snapshot.causal_cutoff, "dataset causal cutoff"
    ):
        raise DeploymentSemanticScopeError("environment cutoff does not match dataset causal cutoff")
    if _instant(event.observed_ts, "event observed_ts") > _instant(
        dataset_snapshot.causal_cutoff, "dataset causal cutoff"
    ):
        raise DeploymentSemanticScopeError("event is later than the causal dataset cutoff")

    if research_protocol.record_id != environment.protocol_id:
        raise DeploymentSemanticScopeError("environment protocol identity mismatch")
    if research_protocol.binding.feature_set_version != feature_set.version:
        raise DeploymentSemanticScopeError("research protocol feature-set version mismatch")
    if episode.environment_id != environment.environment_id:
        raise DeploymentSemanticScopeError("episode environment identity mismatch")

    semantic_actions = tuple(action for action, _ in action_semantics.meanings)
    if semantic_actions != episode.admissible_actions:
        raise DeploymentSemanticScopeError(
            "action semantics must bind the exact sorted admissible action universe"
        )

    reward_rule = _text(reward_definition_id, "reward_definition_id")
    if reward_rule != REWARD_RULE:
        raise DeploymentSemanticScopeError(
            "PAPER deployment reward authority must match the canonical settlement reward rule"
        )

    scope = DeploymentSemanticScope(
        sport_domain=event.sport,
        competition_scope=event.competition_id,
        market_semantics_id=event.market_semantics_id,
        provider_source_class=event.provider_source_class,
        dataset_source_identity=dataset_snapshot.source_identity,
        dataset_license_identity=dataset_snapshot.license_identity,
        feature_set_id=feature_set.feature_set_id,
        feature_set_version=feature_set.version,
        feature_definition_sha256=feature_set.definition_sha256.lower(),
        feature_source_sha256=feature_set.source_sha256.lower(),
        research_protocol_id=research_protocol.record_id,
        research_protocol_sha256=research_protocol.protocol_sha256.lower(),
        config_id=environment.config_id,
        config_sha256=research_protocol.binding.code_config_sha256.lower(),
        action_semantics_id=action_semantics.action_semantics_id,
        action_semantics_definition_sha256=action_semantics.definition_sha256,
        reward_definition_id=reward_rule,
    )
    return DeploymentSemanticAuthority(
        scope=scope,
        event_quote_key=event.quote_key,
        provider_source_id=event.source_id,
        event_observed_ts=event.observed_ts,
        dataset_snapshot_id=dataset_snapshot.dataset_snapshot_id,
        dataset_manifest_sha256=dataset_snapshot.manifest_sha256.lower(),
        dataset_causal_cutoff=dataset_snapshot.causal_cutoff,
        dataset_available_at=dataset_snapshot.available_at,
        feature_available_at=feature_set.available_at,
        protocol_available_at=research_protocol.available_at,
        environment_id=environment.environment_id,
        episode_id=episode.episode_id,
    )
