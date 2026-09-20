"""Immutable semantic compatibility authority for cross-session PAPER deployment.

The resolver accepts only durable authority handles for runtime/scientific evidence:
``SQLiteMarketStore`` for normalized market events, ``ScientificRegistry`` for frozen
scientific records, and ``DeploymentRuntimeAuthorityStore`` for environment/episode/
action meanings. Caller-authored objects cannot authorize deployment compatibility.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Mapping

from .deployment_runtime_authority import (
    DeploymentRuntimeAuthorityError,
    DeploymentRuntimeAuthorityStore,
)
from .domain import MarketEvent
from .paper_settlement_learning import REWARD_RULE
from .scientific_registry import RegistryEntry, ScientificRegistry
from .storage import SQLiteMarketStore


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


def _payload_text(payload: Mapping[str, object], field: str, owner: str) -> str:
    return _text(payload.get(field), f"{owner}.{field}")


def _payload_sha(payload: Mapping[str, object], field: str, owner: str) -> str:
    return _sha(payload.get(field), f"{owner}.{field}")


def _canonical_registry_record(
    registry: ScientificRegistry,
    *,
    record_type: str,
    record_id: str,
    decision_ts: str,
) -> RegistryEntry:
    if not isinstance(registry, ScientificRegistry):
        raise DeploymentSemanticScopeError("scientific_registry must be ScientificRegistry")
    identity = _text(record_id, f"{record_type} record_id")
    try:
        record = registry.get(record_type, identity)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DeploymentSemanticScopeError(
            f"cannot read canonical scientific registry for {record_type}:{identity}"
        ) from exc
    if record is None:
        raise DeploymentSemanticScopeError(
            f"canonical scientific registry lacks {record_type}:{identity}"
        )
    if record.record_type != record_type or record.record_id != identity:
        raise DeploymentSemanticScopeError("canonical scientific registry identity mismatch")
    decision = _instant(decision_ts, "decision_ts")
    if _instant(record.available_at, f"{record_type}.available_at") > decision:
        raise DeploymentSemanticScopeError(
            f"{record_type}:{identity} was not available at decision time"
        )
    if record.reveal_after is not None and _instant(
        record.reveal_after, f"{record_type}.outcome_reveal_after"
    ) > decision:
        raise DeploymentSemanticScopeError(
            f"{record_type}:{identity} was not causally revealed at decision time"
        )
    if type(record.payload) is not dict:
        raise DeploymentSemanticScopeError(f"{record_type}:{identity} payload is invalid")
    return record


def _canonical_market_event(
    market_store: SQLiteMarketStore,
    *,
    dedupe_key: str,
) -> MarketEvent:
    if not isinstance(market_store, SQLiteMarketStore):
        raise DeploymentSemanticScopeError("market_store must be SQLiteMarketStore")
    identity = _text(dedupe_key, "market_event_dedupe_key")
    try:
        matches = [event for event in market_store.events() if event.dedupe_key == identity]
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        raise DeploymentSemanticScopeError("cannot read canonical market-event history") from exc
    if not matches:
        raise DeploymentSemanticScopeError(
            f"canonical market-event history lacks dedupe key {identity}"
        )
    if len(matches) != 1:
        raise DeploymentSemanticScopeError("canonical market-event history is ambiguous")
    return matches[0]


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
            normalized.append(
                (
                    _text(entry[0], f"action semantics action[{index}]"),
                    _text(entry[1], f"action semantics meaning[{index}]"),
                )
            )
        if tuple(normalized) != tuple(sorted(normalized)):
            raise DeploymentSemanticScopeError("action semantics meanings must be sorted")
        names = tuple(name for name, _meaning in normalized)
        if len(names) != len(set(names)):
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
    """Exact causal/durable witness used to resolve one compatibility scope."""

    scope: DeploymentSemanticScope
    decision_ts: str
    market_event_dedupe_key: str
    market_event_payload_sha256: str
    event_quote_key: str
    provider_source_id: str
    event_observed_ts: str
    dataset_snapshot_id: str
    dataset_record_sha256: str
    dataset_manifest_sha256: str
    dataset_causal_cutoff: str
    dataset_available_at: str
    feature_record_sha256: str
    feature_available_at: str
    protocol_record_sha256: str
    protocol_available_at: str
    runtime_authority_id: str
    runtime_record_sha256: str
    runtime_available_at: str
    environment_id: str
    episode_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, DeploymentSemanticScope):
            raise DeploymentSemanticScopeError("scope must be DeploymentSemanticScope")
        for name in (
            "market_event_dedupe_key",
            "event_quote_key",
            "provider_source_id",
            "dataset_snapshot_id",
        ):
            _text(getattr(self, name), name)
        for name in (
            "market_event_payload_sha256",
            "dataset_record_sha256",
            "dataset_manifest_sha256",
            "feature_record_sha256",
            "protocol_record_sha256",
            "runtime_authority_id",
            "runtime_record_sha256",
            "environment_id",
            "episode_id",
        ):
            _sha(getattr(self, name), name)
        for name in (
            "decision_ts",
            "event_observed_ts",
            "dataset_causal_cutoff",
            "dataset_available_at",
            "feature_available_at",
            "protocol_available_at",
            "runtime_available_at",
        ):
            _instant(getattr(self, name), name)

    @property
    def authority_id(self) -> str:
        return _digest(
            {
                "schema": "autosport.deployment_semantic_scope.authority",
                "schema_version": 1,
                "scope_id": self.scope.scope_id,
                "decision_ts": _instant_id(self.decision_ts, "decision_ts"),
                "market_event_dedupe_key": self.market_event_dedupe_key,
                "market_event_payload_sha256": self.market_event_payload_sha256,
                "event_quote_key": self.event_quote_key,
                "provider_source_id": self.provider_source_id,
                "event_observed_ts": _instant_id(self.event_observed_ts, "event_observed_ts"),
                "dataset_snapshot_id": self.dataset_snapshot_id,
                "dataset_record_sha256": self.dataset_record_sha256,
                "dataset_manifest_sha256": self.dataset_manifest_sha256,
                "dataset_causal_cutoff": _instant_id(
                    self.dataset_causal_cutoff, "dataset_causal_cutoff"
                ),
                "dataset_available_at": _instant_id(
                    self.dataset_available_at, "dataset_available_at"
                ),
                "feature_record_sha256": self.feature_record_sha256,
                "feature_available_at": _instant_id(
                    self.feature_available_at, "feature_available_at"
                ),
                "protocol_record_sha256": self.protocol_record_sha256,
                "protocol_available_at": _instant_id(
                    self.protocol_available_at, "protocol_available_at"
                ),
                "runtime_authority_id": self.runtime_authority_id,
                "runtime_record_sha256": self.runtime_record_sha256,
                "runtime_available_at": _instant_id(
                    self.runtime_available_at, "runtime_available_at"
                ),
                "environment_id": self.environment_id,
                "episode_id": self.episode_id,
            }
        )


def resolve_deployment_semantic_scope(
    *,
    market_store: SQLiteMarketStore,
    market_event_dedupe_key: str,
    scientific_registry: ScientificRegistry,
    dataset_snapshot_id: str,
    feature_set_id: str,
    research_protocol_id: str,
    runtime_authority_store: DeploymentRuntimeAuthorityStore,
    runtime_authority_id: str,
    decision_ts: str,
    reward_definition_id: str = REWARD_RULE,
) -> DeploymentSemanticAuthority:
    """Resolve deployment semantics exclusively from canonical durable evidence."""

    decision_id = _instant_id(decision_ts, "decision_ts")
    event = _canonical_market_event(market_store, dedupe_key=market_event_dedupe_key)
    if _instant(event.observed_ts, "event observed_ts") > _instant(decision_id, "decision_ts"):
        raise DeploymentSemanticScopeError("event was not observed at decision time")

    if not isinstance(runtime_authority_store, DeploymentRuntimeAuthorityStore):
        raise DeploymentSemanticScopeError(
            "runtime_authority_store must be DeploymentRuntimeAuthorityStore"
        )
    runtime_id = _sha(runtime_authority_id, "runtime_authority_id")
    try:
        runtime = runtime_authority_store.get(runtime_id)
    except (OSError, RuntimeError, ValueError, DeploymentRuntimeAuthorityError) as exc:
        raise DeploymentSemanticScopeError("cannot read canonical runtime authority") from exc
    if runtime is None:
        raise DeploymentSemanticScopeError(
            f"canonical runtime authority store lacks {runtime_id}"
        )
    if _instant(runtime.available_at, "runtime_authority.available_at") > _instant(
        decision_id, "decision_ts"
    ):
        raise DeploymentSemanticScopeError("runtime authority was not available at decision time")
    environment = runtime.environment
    episode = runtime.episode
    action_semantics = ActionSemanticsDefinition(
        version=runtime.action_semantics_version,
        meanings=runtime.action_semantics_meanings,
    )
    if action_semantics.definition_sha256 != runtime.action_semantics_definition_sha256:
        raise DeploymentSemanticScopeError("runtime action-semantics definition mismatch")
    if action_semantics.action_semantics_id != runtime.action_semantics_id:
        raise DeploymentSemanticScopeError("runtime action-semantics identity mismatch")

    dataset = _canonical_registry_record(
        scientific_registry,
        record_type="DatasetSnapshot",
        record_id=dataset_snapshot_id,
        decision_ts=decision_id,
    )
    feature = _canonical_registry_record(
        scientific_registry,
        record_type="FeatureSet",
        record_id=feature_set_id,
        decision_ts=decision_id,
    )
    protocol = _canonical_registry_record(
        scientific_registry,
        record_type="ResearchProtocol",
        record_id=research_protocol_id,
        decision_ts=decision_id,
    )

    dataset_payload = dataset.payload
    feature_payload = feature.payload
    protocol_payload = protocol.payload
    binding = protocol_payload.get("binding")
    if type(binding) is not dict:
        raise DeploymentSemanticScopeError("canonical ResearchProtocol lacks binding authority")

    dataset_identity = _payload_text(dataset_payload, "dataset_snapshot_id", "DatasetSnapshot")
    dataset_manifest = _payload_sha(dataset_payload, "manifest_sha256", "DatasetSnapshot")
    dataset_source = _payload_text(dataset_payload, "source_identity", "DatasetSnapshot")
    dataset_license = _payload_text(dataset_payload, "license_identity", "DatasetSnapshot")
    dataset_cutoff = _instant_id(
        dataset_payload.get("causal_cutoff"), "DatasetSnapshot.causal_cutoff"
    )
    if dataset_identity != dataset.record_id:
        raise DeploymentSemanticScopeError("canonical DatasetSnapshot payload identity mismatch")
    if _instant(dataset_cutoff, "dataset causal cutoff") > _instant(decision_id, "decision_ts"):
        raise DeploymentSemanticScopeError("dataset causal cutoff is later than decision time")
    if _instant(dataset.available_at, "DatasetSnapshot.available_at") < _instant(
        dataset_cutoff, "DatasetSnapshot.causal_cutoff"
    ):
        raise DeploymentSemanticScopeError("dataset became available before its causal cutoff")

    feature_identity = _payload_text(feature_payload, "feature_set_id", "FeatureSet")
    feature_version = _payload_text(feature_payload, "version", "FeatureSet")
    feature_definition = _payload_sha(feature_payload, "definition_sha256", "FeatureSet")
    feature_source = _payload_sha(feature_payload, "source_sha256", "FeatureSet")
    if feature_identity != feature.record_id:
        raise DeploymentSemanticScopeError("canonical FeatureSet payload identity mismatch")

    protocol_identity = _payload_text(
        protocol_payload, "research_protocol_id", "ResearchProtocol"
    )
    protocol_sha = _payload_sha(protocol_payload, "protocol_sha256", "ResearchProtocol")
    if protocol_identity != protocol.record_id:
        raise DeploymentSemanticScopeError("canonical ResearchProtocol payload identity mismatch")
    if _payload_text(binding, "feature_set_version", "ResearchProtocol.binding") != feature_version:
        raise DeploymentSemanticScopeError("research protocol feature-set version mismatch")
    config_sha = _payload_sha(binding, "code_config_sha256", "ResearchProtocol.binding")

    if event.sport is None:
        raise DeploymentSemanticScopeError("event lacks canonical sport authority")
    if event.competition_id is None:
        raise DeploymentSemanticScopeError("event lacks canonical competition authority")
    if event.market_semantics_id is None:
        raise DeploymentSemanticScopeError("event lacks versioned market semantics authority")
    if event.provider_source_class is None:
        raise DeploymentSemanticScopeError("event lacks provider/source class authority")
    if _instant(event.observed_ts, "event observed_ts") > _instant(
        dataset_cutoff, "dataset causal cutoff"
    ):
        raise DeploymentSemanticScopeError("event is later than the causal dataset cutoff")

    if environment.source_id != dataset_source:
        raise DeploymentSemanticScopeError("environment source identity does not match dataset source")
    if environment.data_id != dataset_identity:
        raise DeploymentSemanticScopeError("environment data identity does not match dataset snapshot")
    if _instant_id(environment.cutoff_ts, "environment cutoff") != dataset_cutoff:
        raise DeploymentSemanticScopeError("environment cutoff does not match dataset causal cutoff")
    if environment.protocol_id != protocol_identity:
        raise DeploymentSemanticScopeError("environment protocol identity mismatch")
    if episode.environment_id != environment.environment_id:
        raise DeploymentSemanticScopeError("episode environment identity mismatch")

    semantic_actions = tuple(action for action, _meaning in action_semantics.meanings)
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
        dataset_source_identity=dataset_source,
        dataset_license_identity=dataset_license,
        feature_set_id=feature_identity,
        feature_set_version=feature_version,
        feature_definition_sha256=feature_definition,
        feature_source_sha256=feature_source,
        research_protocol_id=protocol_identity,
        research_protocol_sha256=protocol_sha,
        config_id=environment.config_id,
        config_sha256=config_sha,
        action_semantics_id=action_semantics.action_semantics_id,
        action_semantics_definition_sha256=action_semantics.definition_sha256,
        reward_definition_id=reward_rule,
    )
    return DeploymentSemanticAuthority(
        scope=scope,
        decision_ts=decision_id,
        market_event_dedupe_key=event.dedupe_key,
        market_event_payload_sha256=_digest(event.to_dict()),
        event_quote_key=event.quote_key,
        provider_source_id=event.source_id,
        event_observed_ts=event.observed_ts,
        dataset_snapshot_id=dataset_identity,
        dataset_record_sha256=_sha(dataset.record_sha256, "DatasetSnapshot.record_sha256"),
        dataset_manifest_sha256=dataset_manifest,
        dataset_causal_cutoff=dataset_cutoff,
        dataset_available_at=dataset.available_at,
        feature_record_sha256=_sha(feature.record_sha256, "FeatureSet.record_sha256"),
        feature_available_at=feature.available_at,
        protocol_record_sha256=_sha(protocol.record_sha256, "ResearchProtocol.record_sha256"),
        protocol_available_at=protocol.available_at,
        runtime_authority_id=runtime.runtime_authority_id,
        runtime_record_sha256=runtime.record_sha256,
        runtime_available_at=runtime.available_at,
        environment_id=environment.environment_id,
        episode_id=episode.episode_id,
    )
