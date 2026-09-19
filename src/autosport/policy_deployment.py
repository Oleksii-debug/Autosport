"""Explicit cross-session deployment authority for promoted paper/shadow policies.

Training identity stays immutable.  A DeploymentScope and ActivationBinding may
authorize the same already-promoted policy for a later causal environment only
when durable scientific evidence and dataset snapshots prove a compatible,
monotonic deployment boundary.  This module grants no promotion, risk, money, or
provider-execution authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from .champion_policy import POLICY_ARTIFACT_KIND
from .integrity import atomic_write_json
from .learning_environment import EnvironmentIdentity
from .scientific_registry import ScientificRegistry
from .strategy_model_factory import FactoryArtifactStore
from .transparent_bandit_policy import BanditPolicyState
from .workspace_lock import WorkspaceEconomicLock


DEPLOYMENT_SCOPE_SCHEMA: Final = "autosport.policy_deployment_scope"
DEPLOYMENT_SCOPE_SCHEMA_VERSION: Final = 1
ACTIVATION_BINDING_SCHEMA: Final = "autosport.policy_activation_binding"
ACTIVATION_BINDING_SCHEMA_VERSION: Final = 1
DEPLOYMENT_AUTHORITY_SCHEMA: Final = "autosport.policy_deployment_authority"
DEPLOYMENT_AUTHORITY_SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")


class PolicyDeploymentError(RuntimeError):
    """Cross-session policy authority is missing, incompatible, or non-causal."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise PolicyDeploymentError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PolicyDeploymentError(f"{name} must be valid UTF-8 text") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise PolicyDeploymentError(f"{name} must be lowercase SHA-256")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyDeploymentError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PolicyDeploymentError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _stable_hash(payload: object) -> str:
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PolicyDeploymentError("deployment payload is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def _actions(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise PolicyDeploymentError("admissible_actions must be a non-empty tuple")
    normalized = tuple(_text(item, "admissible action") for item in value)
    if normalized != tuple(sorted(normalized)) or len(normalized) != len(set(normalized)):
        raise PolicyDeploymentError("admissible_actions must be sorted and unique")
    return normalized


def _causal_record(
    registry: ScientificRegistry,
    *,
    record_type: str,
    record_id: str,
    record_sha256: str,
    as_of: str,
):
    durable = registry.get(record_type, record_id)
    if durable is None:
        raise PolicyDeploymentError(
            f"deployment authority is missing {record_type}:{record_id}"
        )
    if durable.record_sha256 != _sha256(record_sha256, f"{record_type} record_sha256"):
        raise PolicyDeploymentError(f"{record_type} durable record hash mismatch")
    for causal in registry.causal_records(record_type, as_of=as_of):
        if causal.record_id == record_id:
            if causal.record_sha256 != durable.record_sha256:
                raise PolicyDeploymentError(f"{record_type} causal record hash mismatch")
            return durable
    raise PolicyDeploymentError(f"{record_type} was not causally available at activation")


@dataclass(frozen=True, slots=True)
class DeploymentScope:
    """Exact semantic dimensions that may not widen across deployment sessions."""

    canonical_strategy_id: str
    sport_domain: str
    competition_scope: str
    market_semantics_id: str
    provider_source_class: str
    feature_schema_id: str
    protocol_id: str
    action_semantics_id: str
    reward_definition_id: str
    config_sha256: str
    schema: str = DEPLOYMENT_SCOPE_SCHEMA
    schema_version: int = DEPLOYMENT_SCOPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != DEPLOYMENT_SCOPE_SCHEMA:
            raise PolicyDeploymentError("unsupported DeploymentScope schema")
        if self.schema_version != DEPLOYMENT_SCOPE_SCHEMA_VERSION:
            raise PolicyDeploymentError("unsupported DeploymentScope schema version")
        for name in (
            "canonical_strategy_id",
            "sport_domain",
            "competition_scope",
            "market_semantics_id",
            "provider_source_class",
            "feature_schema_id",
            "protocol_id",
            "action_semantics_id",
            "reward_definition_id",
        ):
            _text(getattr(self, name), name)
        _sha256(self.config_sha256, "config_sha256")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "canonical_strategy_id": self.canonical_strategy_id,
            "sport_domain": self.sport_domain,
            "competition_scope": self.competition_scope,
            "market_semantics_id": self.market_semantics_id,
            "provider_source_class": self.provider_source_class,
            "feature_schema_id": self.feature_schema_id,
            "protocol_id": self.protocol_id,
            "action_semantics_id": self.action_semantics_id,
            "reward_definition_id": self.reward_definition_id,
            "config_sha256": self.config_sha256.lower(),
        }

    @property
    def scope_id(self) -> str:
        return _stable_hash(self.to_payload())


@dataclass(frozen=True, slots=True)
class ActivationBinding:
    """Immutable proof envelope for one later paper/shadow policy activation."""

    policy_id: str
    policy_artifact_sha256: str
    training_environment_id: str
    training_data_id: str
    training_dataset_record_sha256: str
    training_cutoff_ts: str
    promotion_decision_id: str
    promotion_decision_record_sha256: str
    promotion_evidence_id: str
    promotion_evidence_record_sha256: str
    evaluation_bundle_id: str
    evaluation_bundle_record_sha256: str
    deployment_scope_id: str
    deployment_environment_id: str
    deployment_data_id: str
    deployment_dataset_record_sha256: str
    deployment_cutoff_ts: str
    snapshot_available_at: str
    activation_at: str
    admissible_actions: tuple[str, ...]
    economic_goal_fingerprint: str
    risk_fingerprint: str
    schema: str = ACTIVATION_BINDING_SCHEMA
    schema_version: int = ACTIVATION_BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != ACTIVATION_BINDING_SCHEMA:
            raise PolicyDeploymentError("unsupported ActivationBinding schema")
        if self.schema_version != ACTIVATION_BINDING_SCHEMA_VERSION:
            raise PolicyDeploymentError("unsupported ActivationBinding schema version")
        for name in (
            "policy_id",
            "policy_artifact_sha256",
            "training_environment_id",
            "training_dataset_record_sha256",
            "promotion_decision_record_sha256",
            "promotion_evidence_id",
            "promotion_evidence_record_sha256",
            "evaluation_bundle_record_sha256",
            "deployment_scope_id",
            "deployment_environment_id",
            "deployment_dataset_record_sha256",
            "economic_goal_fingerprint",
            "risk_fingerprint",
        ):
            _sha256(getattr(self, name), name)
        for name in (
            "training_data_id",
            "promotion_decision_id",
            "evaluation_bundle_id",
            "deployment_data_id",
        ):
            _text(getattr(self, name), name)
        _timestamp(self.training_cutoff_ts, "training_cutoff_ts")
        _timestamp(self.deployment_cutoff_ts, "deployment_cutoff_ts")
        _timestamp(self.snapshot_available_at, "snapshot_available_at")
        _timestamp(self.activation_at, "activation_at")
        _actions(self.admissible_actions)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "policy_id": self.policy_id.lower(),
            "policy_artifact_sha256": self.policy_artifact_sha256.lower(),
            "training_environment_id": self.training_environment_id.lower(),
            "training_data_id": self.training_data_id,
            "training_dataset_record_sha256": self.training_dataset_record_sha256.lower(),
            "training_cutoff_ts": _timestamp(self.training_cutoff_ts, "training_cutoff_ts"),
            "promotion_decision_id": self.promotion_decision_id,
            "promotion_decision_record_sha256": self.promotion_decision_record_sha256.lower(),
            "promotion_evidence_id": self.promotion_evidence_id.lower(),
            "promotion_evidence_record_sha256": self.promotion_evidence_record_sha256.lower(),
            "evaluation_bundle_id": self.evaluation_bundle_id,
            "evaluation_bundle_record_sha256": self.evaluation_bundle_record_sha256.lower(),
            "deployment_scope_id": self.deployment_scope_id.lower(),
            "deployment_environment_id": self.deployment_environment_id.lower(),
            "deployment_data_id": self.deployment_data_id,
            "deployment_dataset_record_sha256": self.deployment_dataset_record_sha256.lower(),
            "deployment_cutoff_ts": _timestamp(self.deployment_cutoff_ts, "deployment_cutoff_ts"),
            "snapshot_available_at": _timestamp(self.snapshot_available_at, "snapshot_available_at"),
            "activation_at": _timestamp(self.activation_at, "activation_at"),
            "admissible_actions": list(self.admissible_actions),
            "economic_goal_fingerprint": self.economic_goal_fingerprint.lower(),
            "risk_fingerprint": self.risk_fingerprint.lower(),
        }

    @property
    def binding_id(self) -> str:
        return _stable_hash(self.to_payload())


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyDeploymentError(
                f"durable deployment authority has duplicate key: {key}"
            )
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise PolicyDeploymentError(
        f"durable deployment authority contains non-finite number: {value}"
    )


def _environment_payload(identity: EnvironmentIdentity) -> dict[str, object]:
    if not isinstance(identity, EnvironmentIdentity):
        raise TypeError("identity must be EnvironmentIdentity")
    return {
        "schema": identity.schema,
        "schema_version": identity.schema_version,
        "source_id": identity.source_id,
        "config_id": identity.config_id,
        "data_id": identity.data_id,
        "protocol_id": identity.protocol_id,
        "cutoff_ts": _timestamp(identity.cutoff_ts, "environment cutoff_ts"),
        "seed": identity.seed,
    }


def _environment_from_payload(
    payload: object, name: str
) -> EnvironmentIdentity:
    expected = {
        "schema",
        "schema_version",
        "source_id",
        "config_id",
        "data_id",
        "protocol_id",
        "cutoff_ts",
        "seed",
    }
    if type(payload) is not dict or set(payload) != expected:
        raise PolicyDeploymentError(f"{name} fields mismatch")
    if type(payload["schema_version"]) is not int:
        raise PolicyDeploymentError(f"{name} schema_version must be an integer")
    if isinstance(payload["seed"], bool) or not isinstance(payload["seed"], int):
        raise PolicyDeploymentError(f"{name} seed must be an integer")
    try:
        return EnvironmentIdentity(
            source_id=_text(payload["source_id"], f"{name} source_id"),
            config_id=_text(payload["config_id"], f"{name} config_id"),
            data_id=_text(payload["data_id"], f"{name} data_id"),
            protocol_id=_text(payload["protocol_id"], f"{name} protocol_id"),
            cutoff_ts=_timestamp(payload["cutoff_ts"], f"{name} cutoff_ts"),
            seed=payload["seed"],
            schema=_text(payload["schema"], f"{name} schema"),
            schema_version=payload["schema_version"],
        )
    except (TypeError, ValueError) as exc:
        raise PolicyDeploymentError(f"{name} is invalid") from exc


def _scope_from_payload(payload: object) -> DeploymentScope:
    expected = {
        "schema",
        "schema_version",
        "canonical_strategy_id",
        "sport_domain",
        "competition_scope",
        "market_semantics_id",
        "provider_source_class",
        "feature_schema_id",
        "protocol_id",
        "action_semantics_id",
        "reward_definition_id",
        "config_sha256",
    }
    if type(payload) is not dict or set(payload) != expected:
        raise PolicyDeploymentError("durable DeploymentScope fields mismatch")
    if type(payload["schema_version"]) is not int:
        raise PolicyDeploymentError("DeploymentScope schema_version must be an integer")
    return DeploymentScope(
        canonical_strategy_id=payload["canonical_strategy_id"],
        sport_domain=payload["sport_domain"],
        competition_scope=payload["competition_scope"],
        market_semantics_id=payload["market_semantics_id"],
        provider_source_class=payload["provider_source_class"],
        feature_schema_id=payload["feature_schema_id"],
        protocol_id=payload["protocol_id"],
        action_semantics_id=payload["action_semantics_id"],
        reward_definition_id=payload["reward_definition_id"],
        config_sha256=payload["config_sha256"],
        schema=payload["schema"],
        schema_version=payload["schema_version"],
    )


def _binding_from_payload(payload: object) -> ActivationBinding:
    expected = {
        "schema",
        "schema_version",
        "policy_id",
        "policy_artifact_sha256",
        "training_environment_id",
        "training_data_id",
        "training_dataset_record_sha256",
        "training_cutoff_ts",
        "promotion_decision_id",
        "promotion_decision_record_sha256",
        "promotion_evidence_id",
        "promotion_evidence_record_sha256",
        "evaluation_bundle_id",
        "evaluation_bundle_record_sha256",
        "deployment_scope_id",
        "deployment_environment_id",
        "deployment_data_id",
        "deployment_dataset_record_sha256",
        "deployment_cutoff_ts",
        "snapshot_available_at",
        "activation_at",
        "admissible_actions",
        "economic_goal_fingerprint",
        "risk_fingerprint",
    }
    if type(payload) is not dict or set(payload) != expected:
        raise PolicyDeploymentError("durable ActivationBinding fields mismatch")
    if type(payload["schema_version"]) is not int:
        raise PolicyDeploymentError("ActivationBinding schema_version must be an integer")
    actions = payload["admissible_actions"]
    if type(actions) is not list:
        raise PolicyDeploymentError(
            "durable ActivationBinding admissible_actions must be a list"
        )
    return ActivationBinding(
        policy_id=payload["policy_id"],
        policy_artifact_sha256=payload["policy_artifact_sha256"],
        training_environment_id=payload["training_environment_id"],
        training_data_id=payload["training_data_id"],
        training_dataset_record_sha256=payload["training_dataset_record_sha256"],
        training_cutoff_ts=payload["training_cutoff_ts"],
        promotion_decision_id=payload["promotion_decision_id"],
        promotion_decision_record_sha256=payload["promotion_decision_record_sha256"],
        promotion_evidence_id=payload["promotion_evidence_id"],
        promotion_evidence_record_sha256=payload["promotion_evidence_record_sha256"],
        evaluation_bundle_id=payload["evaluation_bundle_id"],
        evaluation_bundle_record_sha256=payload["evaluation_bundle_record_sha256"],
        deployment_scope_id=payload["deployment_scope_id"],
        deployment_environment_id=payload["deployment_environment_id"],
        deployment_data_id=payload["deployment_data_id"],
        deployment_dataset_record_sha256=payload["deployment_dataset_record_sha256"],
        deployment_cutoff_ts=payload["deployment_cutoff_ts"],
        snapshot_available_at=payload["snapshot_available_at"],
        activation_at=payload["activation_at"],
        admissible_actions=tuple(actions),
        economic_goal_fingerprint=payload["economic_goal_fingerprint"],
        risk_fingerprint=payload["risk_fingerprint"],
        schema=payload["schema"],
        schema_version=payload["schema_version"],
    )


@dataclass(frozen=True, slots=True)
class DeploymentAuthority:
    """Durable immutable reconstruction record for one activation binding."""

    scope: DeploymentScope
    binding: ActivationBinding
    training_identity: EnvironmentIdentity
    deployment_identity: EnvironmentIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.scope, DeploymentScope):
            raise TypeError("scope must be DeploymentScope")
        if not isinstance(self.binding, ActivationBinding):
            raise TypeError("binding must be ActivationBinding")
        if not isinstance(self.training_identity, EnvironmentIdentity):
            raise TypeError("training_identity must be EnvironmentIdentity")
        if not isinstance(self.deployment_identity, EnvironmentIdentity):
            raise TypeError("deployment_identity must be EnvironmentIdentity")
        if self.binding.deployment_scope_id != self.scope.scope_id:
            raise PolicyDeploymentError(
                "durable activation binding does not bind DeploymentScope"
            )
        if (
            self.binding.training_environment_id
            != self.training_identity.environment_id
        ):
            raise PolicyDeploymentError(
                "durable activation binding does not bind training environment"
            )
        if (
            self.binding.deployment_environment_id
            != self.deployment_identity.environment_id
        ):
            raise PolicyDeploymentError(
                "durable activation binding does not bind deployment environment"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": DEPLOYMENT_AUTHORITY_SCHEMA,
            "schema_version": DEPLOYMENT_AUTHORITY_SCHEMA_VERSION,
            "binding_id": self.binding.binding_id,
            "deployment_scope": self.scope.to_payload(),
            "activation_binding": self.binding.to_payload(),
            "training_identity": _environment_payload(self.training_identity),
            "deployment_identity": _environment_payload(self.deployment_identity),
        }

    @property
    def authority_sha256(self) -> str:
        return _stable_hash(self.to_payload())


def deployment_authority_path(loop_path: str | Path) -> Path:
    target = Path(loop_path)
    return target.with_name(f"{target.name}.activation-authority.json")


def _read_deployment_authority(path: Path) -> DeploymentAuthority:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PolicyDeploymentError("durable deployment authority is missing") from exc
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except json.JSONDecodeError as exc:
        raise PolicyDeploymentError(
            "durable deployment authority must be valid JSON"
        ) from exc

    expected = {
        "schema",
        "schema_version",
        "binding_id",
        "deployment_scope",
        "activation_binding",
        "training_identity",
        "deployment_identity",
        "authority_sha256",
    }
    if type(payload) is not dict or set(payload) != expected:
        raise PolicyDeploymentError("durable deployment authority fields mismatch")
    if payload["schema"] != DEPLOYMENT_AUTHORITY_SCHEMA:
        raise PolicyDeploymentError("durable deployment authority schema mismatch")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != DEPLOYMENT_AUTHORITY_SCHEMA_VERSION
    ):
        raise PolicyDeploymentError(
            "durable deployment authority schema version mismatch"
        )

    claimed = _sha256(payload["authority_sha256"], "authority_sha256")
    core = {key: value for key, value in payload.items() if key != "authority_sha256"}
    if _stable_hash(core) != claimed:
        raise PolicyDeploymentError("durable deployment authority digest mismatch")

    authority = DeploymentAuthority(
        scope=_scope_from_payload(payload["deployment_scope"]),
        binding=_binding_from_payload(payload["activation_binding"]),
        training_identity=_environment_from_payload(
            payload["training_identity"], "training identity"
        ),
        deployment_identity=_environment_from_payload(
            payload["deployment_identity"], "deployment identity"
        ),
    )
    if _sha256(payload["binding_id"], "binding_id") != authority.binding.binding_id:
        raise PolicyDeploymentError("durable deployment authority binding id mismatch")
    if authority.to_payload() != core:
        raise PolicyDeploymentError("durable deployment authority is not canonical")
    return authority


def persist_deployment_authority(
    loop_path: str | Path,
    *,
    scope: DeploymentScope,
    binding: ActivationBinding,
    training_identity: EnvironmentIdentity,
    deployment_identity: EnvironmentIdentity,
) -> DeploymentAuthority:
    """Atomically create one immutable activation authority sidecar."""

    authority = DeploymentAuthority(
        scope=scope,
        binding=binding,
        training_identity=training_identity,
        deployment_identity=deployment_identity,
    )
    target = deployment_authority_path(loop_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with WorkspaceEconomicLock(target.parent):
        if target.exists():
            existing = _read_deployment_authority(target)
            if existing != authority:
                raise PolicyDeploymentError(
                    "existing durable deployment authority conflicts with activation"
                )
        else:
            payload = authority.to_payload()
            atomic_write_json(
                target,
                {
                    **payload,
                    "authority_sha256": authority.authority_sha256,
                },
            )
    return authority


def load_deployment_authority(
    loop_path: str | Path,
    *,
    expected_binding_id: str | None = None,
) -> DeploymentAuthority:
    """Load and cryptographically verify one immutable activation authority."""

    authority = _read_deployment_authority(deployment_authority_path(loop_path))
    if expected_binding_id is not None and authority.binding.binding_id != _sha256(
        expected_binding_id, "expected_binding_id"
    ):
        raise PolicyDeploymentError(
            "durable deployment authority does not match AgentLoop binding"
        )
    return authority


def validate_activation_binding(
    binding: ActivationBinding,
    *,
    scope: DeploymentScope,
    policy: BanditPolicyState,
    training_identity: EnvironmentIdentity,
    deployment_identity: EnvironmentIdentity,
    registry: ScientificRegistry,
    artifact_store: FactoryArtifactStore,
    canonical_strategy_id: str,
    admissible_actions: frozenset[str],
    economic_goal_fingerprint: str,
    risk_fingerprint: str,
) -> None:
    """Fail closed unless one immutable later-session activation is fully bound."""

    if not isinstance(binding, ActivationBinding):
        raise TypeError("binding must be ActivationBinding")
    if not isinstance(scope, DeploymentScope):
        raise TypeError("scope must be DeploymentScope")
    if not isinstance(policy, BanditPolicyState):
        raise TypeError("policy must be BanditPolicyState")
    if not isinstance(training_identity, EnvironmentIdentity):
        raise TypeError("training_identity must be EnvironmentIdentity")
    if not isinstance(deployment_identity, EnvironmentIdentity):
        raise TypeError("deployment_identity must be EnvironmentIdentity")
    if not isinstance(registry, ScientificRegistry):
        raise TypeError("registry must be ScientificRegistry")
    if not isinstance(artifact_store, FactoryArtifactStore):
        raise TypeError("artifact_store must be FactoryArtifactStore")

    strategy_key = _text(canonical_strategy_id, "canonical_strategy_id")
    if scope.canonical_strategy_id != strategy_key:
        raise PolicyDeploymentError("deployment scope strategy identity mismatch")
    if binding.deployment_scope_id != scope.scope_id:
        raise PolicyDeploymentError("activation binding scope hash mismatch")
    if scope.protocol_id != policy.protocol_id:
        raise PolicyDeploymentError("deployment scope protocol mismatch")
    if scope.config_sha256.lower() != policy.config_sha256:
        raise PolicyDeploymentError("deployment scope config mismatch")

    if binding.policy_id != policy.policy_id:
        raise PolicyDeploymentError("activation binding policy identity mismatch")
    actual_policy_sha = artifact_store.sha256(POLICY_ARTIFACT_KIND, policy.policy_id)
    if actual_policy_sha != binding.policy_artifact_sha256:
        raise PolicyDeploymentError("activation binding policy artifact hash mismatch")

    if training_identity.environment_id != binding.training_environment_id:
        raise PolicyDeploymentError("training environment identity mismatch")
    if policy.environment_id != training_identity.environment_id:
        raise PolicyDeploymentError("policy training environment was rewritten")
    if training_identity.data_id != binding.training_data_id:
        raise PolicyDeploymentError("training data identity mismatch")
    if _instant(training_identity.cutoff_ts, "training cutoff") != _instant(
        binding.training_cutoff_ts, "binding training cutoff"
    ):
        raise PolicyDeploymentError("training cutoff identity mismatch")
    if training_identity.protocol_id != policy.protocol_id:
        raise PolicyDeploymentError("training protocol identity mismatch")

    if deployment_identity.environment_id != binding.deployment_environment_id:
        raise PolicyDeploymentError("deployment environment identity mismatch")
    if deployment_identity.data_id != binding.deployment_data_id:
        raise PolicyDeploymentError("deployment data identity mismatch")
    if _instant(deployment_identity.cutoff_ts, "deployment cutoff") != _instant(
        binding.deployment_cutoff_ts, "binding deployment cutoff"
    ):
        raise PolicyDeploymentError("deployment cutoff identity mismatch")

    # Cross-session deployment may advance data/cutoff only.  Opaque source,
    # config, protocol and seed identities remain exact; semantic dimensions are
    # separately frozen by DeploymentScope.
    for name in ("source_id", "config_id", "protocol_id", "seed"):
        if getattr(training_identity, name) != getattr(deployment_identity, name):
            raise PolicyDeploymentError(
                f"deployment environment changes exact {name} authority"
            )
    if _instant(deployment_identity.cutoff_ts, "deployment cutoff") < _instant(
        training_identity.cutoff_ts, "training cutoff"
    ):
        raise PolicyDeploymentError("deployment cutoff moves backwards")

    training_snapshot = _causal_record(
        registry,
        record_type="DatasetSnapshot",
        record_id=training_identity.data_id,
        record_sha256=binding.training_dataset_record_sha256,
        as_of=binding.activation_at,
    )
    deployment_snapshot = _causal_record(
        registry,
        record_type="DatasetSnapshot",
        record_id=deployment_identity.data_id,
        record_sha256=binding.deployment_dataset_record_sha256,
        as_of=binding.snapshot_available_at,
    )
    tp = training_snapshot.payload
    dp = deployment_snapshot.payload
    if tp.get("dataset_snapshot_id") != training_identity.data_id:
        raise PolicyDeploymentError("training DatasetSnapshot identity mismatch")
    if _instant(tp.get("causal_cutoff"), "training DatasetSnapshot causal_cutoff") != _instant(
        training_identity.cutoff_ts, "training environment cutoff"
    ):
        raise PolicyDeploymentError("training environment cutoff does not bind DatasetSnapshot")
    if dp.get("dataset_snapshot_id") != deployment_identity.data_id:
        raise PolicyDeploymentError("deployment DatasetSnapshot identity mismatch")
    if (
        tp.get("source_identity") != dp.get("source_identity")
        or tp.get("license_identity") != dp.get("license_identity")
    ):
        raise PolicyDeploymentError("deployment snapshot crosses source/license lineage")
    if _instant(dp.get("causal_cutoff"), "deployment DatasetSnapshot causal_cutoff") < _instant(
        tp.get("causal_cutoff"), "training DatasetSnapshot causal_cutoff"
    ):
        raise PolicyDeploymentError("deployment DatasetSnapshot causal cutoff moves backwards")
    if _instant(dp.get("causal_cutoff"), "deployment DatasetSnapshot causal_cutoff") != _instant(
        deployment_identity.cutoff_ts, "deployment environment cutoff"
    ):
        raise PolicyDeploymentError("deployment environment cutoff does not bind DatasetSnapshot")
    if _instant(deployment_snapshot.available_at, "deployment DatasetSnapshot available_at") > _instant(
        binding.snapshot_available_at, "snapshot_available_at"
    ):
        raise PolicyDeploymentError("deployment snapshot was not causally available")
    if _instant(binding.snapshot_available_at, "snapshot_available_at") > _instant(
        binding.activation_at, "activation_at"
    ):
        raise PolicyDeploymentError("deployment snapshot becomes available after activation")

    decision = _causal_record(
        registry,
        record_type="PromotionDecision",
        record_id=binding.promotion_decision_id,
        record_sha256=binding.promotion_decision_record_sha256,
        as_of=binding.activation_at,
    )
    evidence = _causal_record(
        registry,
        record_type="PromotionEvidence",
        record_id=binding.promotion_evidence_id,
        record_sha256=binding.promotion_evidence_record_sha256,
        as_of=binding.activation_at,
    )
    evaluation = _causal_record(
        registry,
        record_type="EvaluationBundle",
        record_id=binding.evaluation_bundle_id,
        record_sha256=binding.evaluation_bundle_record_sha256,
        as_of=binding.activation_at,
    )
    dpayload = decision.payload
    epayload = evidence.payload
    vpayload = evaluation.payload
    if (
        dpayload.get("action") != "PROMOTE"
        or dpayload.get("candidate_strategy_version_id") != policy.policy_id
        or dpayload.get("promotion_evidence_id") != binding.promotion_evidence_id
        or dpayload.get("evaluation_bundle_id") != binding.evaluation_bundle_id
        or dpayload.get("research_protocol_id") != policy.protocol_id
    ):
        raise PolicyDeploymentError("promotion decision does not bind deployed policy")
    if (
        epayload.get("candidate_strategy_version_id") != policy.policy_id
        or epayload.get("evaluation_bundle_id") != binding.evaluation_bundle_id
        or epayload.get("research_protocol_id") != policy.protocol_id
        or epayload.get("dataset_snapshot_id") != training_identity.data_id
        or epayload.get("validity") != "ELIGIBLE"
    ):
        raise PolicyDeploymentError("promotion evidence does not authorize deployed policy")
    if (
        vpayload.get("evaluation_bundle_id") != binding.evaluation_bundle_id
        or vpayload.get("evaluated_strategy_version_id") != policy.policy_id
        or vpayload.get("dataset_snapshot_id") != training_identity.data_id
    ):
        raise PolicyDeploymentError("evaluation bundle does not bind deployed policy")
    bundle_sha = dpayload.get("evaluation_bundle_sha256")
    if (
        epayload.get("evaluation_bundle_sha256") != bundle_sha
        or vpayload.get("bundle_sha256") != bundle_sha
    ):
        raise PolicyDeploymentError("promotion/evaluation bundle digest mismatch")

    promotion_available = max(
        _instant(decision.available_at, "PromotionDecision available_at"),
        _instant(evidence.available_at, "PromotionEvidence available_at"),
        _instant(evaluation.available_at, "EvaluationBundle available_at"),
    )
    activation = _instant(binding.activation_at, "activation_at")
    if promotion_available >= activation:
        raise PolicyDeploymentError(
            "promotion/evaluation evidence must predate deployment activation"
        )
    if _instant(training_identity.cutoff_ts, "training cutoff") > promotion_available:
        raise PolicyDeploymentError("promotion evidence predates training cutoff")

    champion = registry.champion_strategy(
        as_of=binding.activation_at,
        canonical_strategy_id=strategy_key,
    )
    if champion != policy.policy_id:
        raise PolicyDeploymentError("deployed policy is not the durable champion at activation")

    requested = frozenset(_text(x, "admissible action") for x in admissible_actions)
    if not requested or tuple(sorted(requested)) != binding.admissible_actions:
        raise PolicyDeploymentError("activation admissible-action binding mismatch")
    policy_actions = frozenset(item.action_type for item in policy.estimates)
    if not requested.issubset(policy_actions):
        raise PolicyDeploymentError("deployment actions widen policy action universe")
    if binding.economic_goal_fingerprint != _sha256(
        economic_goal_fingerprint, "economic_goal_fingerprint"
    ):
        raise PolicyDeploymentError("activation EconomicGoal fingerprint mismatch")
    if binding.risk_fingerprint != _sha256(risk_fingerprint, "risk_fingerprint"):
        raise PolicyDeploymentError("activation Risk fingerprint mismatch")


__all__ = [
    "ActivationBinding",
    "DeploymentAuthority",
    "DeploymentScope",
    "PolicyDeploymentError",
    "deployment_authority_path",
    "load_deployment_authority",
    "persist_deployment_authority",
    "validate_activation_binding",
]
