"""Canonical semantic bridge for cross-session policy deployment.

This module is a consumer of the integrated deployment-semantic authority.  It does
not mint sport/market/provider/feature/action/reward semantics itself.  Instead it
re-resolves both the immutable training context and the later deployment context
from canonical market/scientific/runtime stores, proves that their compatibility
scope is identical, converts that proven scope into the legacy policy-deployment
shape, and pins the exact canonical authority ids beside the durable AgentLoop.

The exact-authority pin is deliberately a consumer witness, not a second semantic
registry.  Deleting it makes restart fail closed; changing an upstream exact witness
while keeping the same compatibility scope also fails closed on restart.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .deployment_runtime_authority import DeploymentRuntimeAuthorityStore
from .deployment_semantic_scope import (
    DeploymentSemanticAuthority,
    DeploymentSemanticScopeError,
    resolve_deployment_semantic_scope,
)
from .integrity import atomic_write_json
from .learning_environment import EnvironmentIdentity
from .policy_deployment import (
    ActivationBinding,
    DeploymentScope,
    PolicyDeploymentError,
    validate_activation_binding,
)
from .scientific_registry import ScientificRegistry
from .storage import SQLiteMarketStore
from .strategy_model_factory import FactoryArtifactStore
from .transparent_bandit_policy import BanditPolicyState
from .workspace_lock import WorkspaceEconomicLock


SEMANTIC_BINDING_SCHEMA: Final = "autosport.policy_deployment_semantic_binding"
SEMANTIC_BINDING_SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")


class PolicyDeploymentSemanticBridgeError(PolicyDeploymentError):
    """Canonical semantic deployment evidence is missing or changed."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise PolicyDeploymentSemanticBridgeError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PolicyDeploymentSemanticBridgeError(f"{name} must be valid UTF-8") from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise PolicyDeploymentSemanticBridgeError(f"{name} must be canonical SHA-256 hex")
    return text


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
        raise PolicyDeploymentSemanticBridgeError(
            "semantic deployment binding is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyDeploymentSemanticBridgeError(
                f"duplicate semantic deployment binding key: {key}"
            )
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise PolicyDeploymentSemanticBridgeError(
        f"non-finite semantic deployment binding value: {value}"
    )


@dataclass(frozen=True, slots=True)
class SemanticResolutionInput:
    """Opaque canonical handles needed to re-resolve one session's semantics."""

    market_event_dedupe_key: str
    feature_set_id: str
    runtime_authority_id: str

    def __post_init__(self) -> None:
        _text(self.market_event_dedupe_key, "market_event_dedupe_key")
        _text(self.feature_set_id, "feature_set_id")
        _sha(self.runtime_authority_id, "runtime_authority_id")


@dataclass(frozen=True, slots=True)
class CrossSessionSemanticInputs:
    """Canonical training/deployment resolver handles for one activation."""

    training: SemanticResolutionInput
    deployment: SemanticResolutionInput

    def __post_init__(self) -> None:
        if not isinstance(self.training, SemanticResolutionInput):
            raise TypeError("training must be SemanticResolutionInput")
        if not isinstance(self.deployment, SemanticResolutionInput):
            raise TypeError("deployment must be SemanticResolutionInput")


@dataclass(frozen=True, slots=True)
class CanonicalSemanticBinding:
    """Exact canonical authorities pinned to one durable activation binding."""

    activation_binding_id: str
    compatibility_scope_id: str
    training_authority_id: str
    deployment_authority_id: str
    training_market_event_dedupe_key: str
    deployment_market_event_dedupe_key: str
    training_runtime_authority_id: str
    deployment_runtime_authority_id: str
    schema: str = SEMANTIC_BINDING_SCHEMA
    schema_version: int = SEMANTIC_BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != SEMANTIC_BINDING_SCHEMA:
            raise PolicyDeploymentSemanticBridgeError("semantic binding schema mismatch")
        if self.schema_version != SEMANTIC_BINDING_SCHEMA_VERSION:
            raise PolicyDeploymentSemanticBridgeError(
                "semantic binding schema version mismatch"
            )
        for name in (
            "activation_binding_id",
            "compatibility_scope_id",
            "training_authority_id",
            "deployment_authority_id",
            "training_runtime_authority_id",
            "deployment_runtime_authority_id",
        ):
            _sha(getattr(self, name), name)
        _text(
            self.training_market_event_dedupe_key,
            "training_market_event_dedupe_key",
        )
        _text(
            self.deployment_market_event_dedupe_key,
            "deployment_market_event_dedupe_key",
        )

    def core_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "activation_binding_id": self.activation_binding_id,
            "compatibility_scope_id": self.compatibility_scope_id,
            "training_authority_id": self.training_authority_id,
            "deployment_authority_id": self.deployment_authority_id,
            "training_market_event_dedupe_key": self.training_market_event_dedupe_key,
            "deployment_market_event_dedupe_key": self.deployment_market_event_dedupe_key,
            "training_runtime_authority_id": self.training_runtime_authority_id,
            "deployment_runtime_authority_id": self.deployment_runtime_authority_id,
        }

    @property
    def binding_sha256(self) -> str:
        return _digest(self.core_payload())

    def to_payload(self) -> dict[str, object]:
        return {**self.core_payload(), "binding_sha256": self.binding_sha256}


@dataclass(frozen=True, slots=True)
class ResolvedCrossSessionSemantics:
    """Canonical exact witnesses plus the legacy scope derived from them."""

    training: DeploymentSemanticAuthority
    deployment: DeploymentSemanticAuthority
    deployment_scope: DeploymentScope
    semantic_binding: CanonicalSemanticBinding


def semantic_binding_path(loop_path: str | Path) -> Path:
    target = Path(loop_path)
    return target.with_name(f"{target.name}.deployment-semantic-binding.json")


def _legacy_scope_from_canonical(
    authority: DeploymentSemanticAuthority,
    *,
    canonical_strategy_id: str,
) -> DeploymentScope:
    scope = authority.scope
    return DeploymentScope(
        canonical_strategy_id=_text(canonical_strategy_id, "canonical_strategy_id"),
        sport_domain=scope.sport_domain,
        competition_scope=scope.competition_scope,
        market_semantics_id=scope.market_semantics_id,
        provider_source_class=scope.provider_source_class,
        feature_schema_id=scope.feature_definition_sha256,
        protocol_id=scope.research_protocol_id,
        action_semantics_id=scope.action_semantics_id,
        reward_definition_id=scope.reward_definition_id,
        config_sha256=scope.config_sha256,
    )


def _resolve_one(
    *,
    input_: SemanticResolutionInput,
    identity: EnvironmentIdentity,
    market_store: SQLiteMarketStore,
    scientific_registry: ScientificRegistry,
    runtime_authority_store: DeploymentRuntimeAuthorityStore,
    decision_ts: str,
) -> DeploymentSemanticAuthority:
    try:
        authority = resolve_deployment_semantic_scope(
            market_store=market_store,
            market_event_dedupe_key=input_.market_event_dedupe_key,
            scientific_registry=scientific_registry,
            dataset_snapshot_id=identity.data_id,
            feature_set_id=input_.feature_set_id,
            research_protocol_id=identity.protocol_id,
            runtime_authority_store=runtime_authority_store,
            runtime_authority_id=input_.runtime_authority_id,
            decision_ts=decision_ts,
        )
    except (DeploymentSemanticScopeError, OSError, RuntimeError, ValueError) as exc:
        raise PolicyDeploymentSemanticBridgeError(
            "canonical deployment semantic authority could not be resolved"
        ) from exc
    if authority.environment_id != identity.environment_id:
        raise PolicyDeploymentSemanticBridgeError(
            "canonical semantic authority environment identity mismatch"
        )
    if authority.dataset_snapshot_id != identity.data_id:
        raise PolicyDeploymentSemanticBridgeError(
            "canonical semantic authority dataset identity mismatch"
        )
    if authority.scope.research_protocol_id != identity.protocol_id:
        raise PolicyDeploymentSemanticBridgeError(
            "canonical semantic authority protocol identity mismatch"
        )
    if authority.scope.config_id != identity.config_id:
        raise PolicyDeploymentSemanticBridgeError(
            "canonical semantic authority config identity mismatch"
        )
    if authority.runtime_authority_id != input_.runtime_authority_id:
        raise PolicyDeploymentSemanticBridgeError(
            "canonical runtime authority identity mismatch"
        )
    return authority


def resolve_cross_session_semantics(
    *,
    inputs: CrossSessionSemanticInputs,
    training_identity: EnvironmentIdentity,
    deployment_identity: EnvironmentIdentity,
    activation_binding: ActivationBinding,
    canonical_strategy_id: str,
    market_store: SQLiteMarketStore,
    scientific_registry: ScientificRegistry,
    runtime_authority_store: DeploymentRuntimeAuthorityStore,
) -> ResolvedCrossSessionSemantics:
    """Re-resolve and compare training/deployment semantics from canonical stores."""

    if not isinstance(inputs, CrossSessionSemanticInputs):
        raise TypeError("inputs must be CrossSessionSemanticInputs")
    if not isinstance(training_identity, EnvironmentIdentity):
        raise TypeError("training_identity must be EnvironmentIdentity")
    if not isinstance(deployment_identity, EnvironmentIdentity):
        raise TypeError("deployment_identity must be EnvironmentIdentity")
    if not isinstance(activation_binding, ActivationBinding):
        raise TypeError("activation_binding must be ActivationBinding")

    training = _resolve_one(
        input_=inputs.training,
        identity=training_identity,
        market_store=market_store,
        scientific_registry=scientific_registry,
        runtime_authority_store=runtime_authority_store,
        decision_ts=activation_binding.activation_at,
    )
    deployment = _resolve_one(
        input_=inputs.deployment,
        identity=deployment_identity,
        market_store=market_store,
        scientific_registry=scientific_registry,
        runtime_authority_store=runtime_authority_store,
        decision_ts=activation_binding.activation_at,
    )
    if training.scope.scope_id != deployment.scope.scope_id:
        raise PolicyDeploymentSemanticBridgeError(
            "training and deployment canonical semantic scopes are incompatible"
        )
    if training.dataset_record_sha256 != activation_binding.training_dataset_record_sha256:
        raise PolicyDeploymentSemanticBridgeError(
            "training canonical semantic authority DatasetSnapshot digest mismatch"
        )
    if deployment.dataset_record_sha256 != activation_binding.deployment_dataset_record_sha256:
        raise PolicyDeploymentSemanticBridgeError(
            "deployment canonical semantic authority DatasetSnapshot digest mismatch"
        )

    legacy_scope = _legacy_scope_from_canonical(
        deployment,
        canonical_strategy_id=canonical_strategy_id,
    )
    if activation_binding.deployment_scope_id != legacy_scope.scope_id:
        raise PolicyDeploymentSemanticBridgeError(
            "activation binding does not bind the canonical deployment semantic scope"
        )
    semantic_binding = CanonicalSemanticBinding(
        activation_binding_id=activation_binding.binding_id,
        compatibility_scope_id=deployment.scope.scope_id,
        training_authority_id=training.authority_id,
        deployment_authority_id=deployment.authority_id,
        training_market_event_dedupe_key=inputs.training.market_event_dedupe_key,
        deployment_market_event_dedupe_key=inputs.deployment.market_event_dedupe_key,
        training_runtime_authority_id=inputs.training.runtime_authority_id,
        deployment_runtime_authority_id=inputs.deployment.runtime_authority_id,
    )
    return ResolvedCrossSessionSemantics(
        training=training,
        deployment=deployment,
        deployment_scope=legacy_scope,
        semantic_binding=semantic_binding,
    )


def _binding_from_payload(payload: object) -> CanonicalSemanticBinding:
    expected = {
        "schema",
        "schema_version",
        "activation_binding_id",
        "compatibility_scope_id",
        "training_authority_id",
        "deployment_authority_id",
        "training_market_event_dedupe_key",
        "deployment_market_event_dedupe_key",
        "training_runtime_authority_id",
        "deployment_runtime_authority_id",
        "binding_sha256",
    }
    if type(payload) is not dict or set(payload) != expected:
        raise PolicyDeploymentSemanticBridgeError(
            "durable semantic deployment binding fields mismatch"
        )
    schema_version = payload["schema_version"]
    if type(schema_version) is not int:
        raise PolicyDeploymentSemanticBridgeError(
            "durable semantic binding schema_version must be integer"
        )
    result = CanonicalSemanticBinding(
        activation_binding_id=payload["activation_binding_id"],
        compatibility_scope_id=payload["compatibility_scope_id"],
        training_authority_id=payload["training_authority_id"],
        deployment_authority_id=payload["deployment_authority_id"],
        training_market_event_dedupe_key=payload[
            "training_market_event_dedupe_key"
        ],
        deployment_market_event_dedupe_key=payload[
            "deployment_market_event_dedupe_key"
        ],
        training_runtime_authority_id=payload["training_runtime_authority_id"],
        deployment_runtime_authority_id=payload["deployment_runtime_authority_id"],
        schema=payload["schema"],
        schema_version=schema_version,
    )
    if _sha(payload["binding_sha256"], "binding_sha256") != result.binding_sha256:
        raise PolicyDeploymentSemanticBridgeError(
            "durable semantic deployment binding digest mismatch"
        )
    return result


def load_semantic_binding(
    loop_path: str | Path,
    *,
    expected_activation_binding_id: str | None = None,
) -> CanonicalSemanticBinding:
    target = semantic_binding_path(loop_path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PolicyDeploymentSemanticBridgeError(
            "durable canonical semantic binding is missing"
        ) from exc
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except json.JSONDecodeError as exc:
        raise PolicyDeploymentSemanticBridgeError(
            "durable canonical semantic binding must be valid JSON"
        ) from exc
    result = _binding_from_payload(payload)
    if expected_activation_binding_id is not None and result.activation_binding_id != _sha(
        expected_activation_binding_id, "expected_activation_binding_id"
    ):
        raise PolicyDeploymentSemanticBridgeError(
            "durable canonical semantic binding does not match activation"
        )
    return result


def persist_semantic_binding(
    loop_path: str | Path,
    binding: CanonicalSemanticBinding,
) -> CanonicalSemanticBinding:
    if not isinstance(binding, CanonicalSemanticBinding):
        raise TypeError("binding must be CanonicalSemanticBinding")
    target = semantic_binding_path(loop_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with WorkspaceEconomicLock(target.parent):
        if target.exists():
            existing = load_semantic_binding(loop_path)
            if existing != binding:
                raise PolicyDeploymentSemanticBridgeError(
                    "durable canonical semantic binding conflicts with activation"
                )
            return existing
        atomic_write_json(target, binding.to_payload())
    return binding


def validate_canonical_activation_binding(
    activation_binding: ActivationBinding,
    *,
    semantic_inputs: CrossSessionSemanticInputs,
    market_store: SQLiteMarketStore,
    runtime_authority_store: DeploymentRuntimeAuthorityStore,
    loop_path: str | Path,
    require_existing_semantic_binding: bool,
    policy: BanditPolicyState,
    training_identity: EnvironmentIdentity,
    deployment_identity: EnvironmentIdentity,
    registry: ScientificRegistry,
    artifact_store: FactoryArtifactStore,
    canonical_strategy_id: str,
    admissible_actions: frozenset[str],
    economic_goal_fingerprint: str,
    risk_fingerprint: str,
) -> ResolvedCrossSessionSemantics:
    """Validate #596 only after exact #626 authority is canonically re-resolved."""

    resolved = resolve_cross_session_semantics(
        inputs=semantic_inputs,
        training_identity=training_identity,
        deployment_identity=deployment_identity,
        activation_binding=activation_binding,
        canonical_strategy_id=canonical_strategy_id,
        market_store=market_store,
        scientific_registry=registry,
        runtime_authority_store=runtime_authority_store,
    )
    if require_existing_semantic_binding:
        durable = load_semantic_binding(
            loop_path,
            expected_activation_binding_id=activation_binding.binding_id,
        )
        if durable != resolved.semantic_binding:
            raise PolicyDeploymentSemanticBridgeError(
                "canonical semantic authority changed across restart"
            )

    validate_activation_binding(
        activation_binding,
        scope=resolved.deployment_scope,
        policy=policy,
        training_identity=training_identity,
        deployment_identity=deployment_identity,
        registry=registry,
        artifact_store=artifact_store,
        canonical_strategy_id=canonical_strategy_id,
        admissible_actions=admissible_actions,
        economic_goal_fingerprint=economic_goal_fingerprint,
        risk_fingerprint=risk_fingerprint,
    )
    if not require_existing_semantic_binding:
        persist_semantic_binding(loop_path, resolved.semantic_binding)
    return resolved


__all__ = [
    "CanonicalSemanticBinding",
    "CrossSessionSemanticInputs",
    "PolicyDeploymentSemanticBridgeError",
    "ResolvedCrossSessionSemantics",
    "SemanticResolutionInput",
    "load_semantic_binding",
    "persist_semantic_binding",
    "resolve_cross_session_semantics",
    "semantic_binding_path",
    "validate_canonical_activation_binding",
]
